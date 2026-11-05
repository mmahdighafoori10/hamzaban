#!/usr/bin/env python3
"""suite.py - unified orchestrator for the local memory suite (Python 3.10, stdlib only).

  python suite.py doctor                         9 health checks (always exit 0)
  python suite.py status                         one-screen summary
  python suite.py skills init|list|diff|push     skill sync (source of truth: skills-src/<name>/); list/diff are read-only,
                                                 only init and push --apply may seed skills-src
  python suite.py skills push [--apply]          dry-run unless --apply; backs up, never deletes

Environment overrides (used by tests):
  SUITE_HOME             vault root (the folder that contains .ai-memory)
  SUITE_APPDATA          replaces %APPDATA% (Windows ...\\AppData\\Roaming) for Chat On Steroids files
  SUITE_USER_HOME        replaces the user's home (~/.claude, ~/.codex, ~/.agents)
  SUITE_CLAUDE_SETTINGS  explicit path of Claude Code settings.json
  SUITE_CODEX_CONFIG     explicit path of a Codex hooks/config file
  SUITE_NOW              fixed "now" (ISO-8601) for deterministic tests

Converter plugin slot: if .ai-memory/skill-convert.py exists (or the path in suite-skills.json
key "converter"), `skills push --apply` calls  convert(skill_dir, target[, target_name=...])
instead of the plain copy. skill_dir = source folder; target = destination skill folder (Path).
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TEXT_SUFFIXES = {".md", ".txt", ".json", ".py", ".yaml", ".yml", ".toml", ".js", ".ts", ".ps1", ".sh"}


def _vault() -> Path:
    env = os.environ.get("SUITE_HOME")
    if env:
        p = Path(env).expanduser()
        return p.parent if p.name == ".ai-memory" else p
    return SCRIPT_DIR.parent


VAULT = _vault()
ENGINE = VAULT / ".ai-memory"
FACTS = VAULT / "10-Facts"
PENDING = VAULT / "30-Pending"
CHECKPOINTS = VAULT / "20-Checkpoints"
CAPTURE = VAULT / "40-Conversation-Capture"
SRC_SKILLS = ENGINE / "skills-src"
CONFIG_PATH = ENGINE / "suite-skills.json"

DEFAULT_CONFIG = {
    "version": 1,
    "skills": [],  # empty = every folder in skills-src that has a SKILL.md
    "targets": {
        "agents": "{home}/.agents/skills",
        "codex": "{home}/.codex/skills",
        "claude": "{home}/.claude/skills",
        "cos": "{appdata}/chat-on-steroids/skills",
    },
    "converter": None,
}

PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"


# ---------------------------------------------------------------- helpers
def now() -> datetime:
    raw = os.environ.get("SUITE_NOW")
    if raw:
        parsed = parse_ts(raw)
        if parsed:
            return parsed
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return now().strftime("%Y%m%dT%H%M%SZ")


def age_of(dt: datetime) -> timedelta:
    delta = now() - dt
    return delta if delta.total_seconds() > 0 else timedelta(0)


def fmt_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    return f"{secs // 86400}d {secs % 86400 // 3600}h"


def rel(path: Path) -> str:
    try:
        return path.relative_to(VAULT).as_posix()
    except ValueError:
        return str(path)


def user_home() -> Path:
    return Path(os.environ.get("SUITE_USER_HOME") or Path.home())


def appdata() -> Path:
    env = os.environ.get("SUITE_APPDATA") or os.environ.get("APPDATA")
    return Path(env) if env else user_home() / "AppData" / "Roaming"


def parse_ts_aware(value) -> tuple[datetime | None, bool]:
    # (datetime, has_timezone). A timestamp with no timezone is returned as UTC-assumed but flagged False,
    # so freshness checks can refuse to judge it instead of guessing.
    if not isinstance(value, str) or not value.strip():
        return None, False
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None, False
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc), False
    return dt, True


FUTURE_SLACK = timedelta(minutes=5)


def is_future(dt: datetime) -> bool:
    return dt - now() > FUTURE_SLACK


_FM_LINE = re.compile(r"([A-Za-z0-9_-]+):(?:[ \t]+(.*))?\Z")
_FM_BAD_CHARS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse_front_matter(text: str) -> tuple[dict, str, list[str]]:
    # STRICT: only column-0 `key: value` lines inside the first ---/--- block are accepted.
    # Anything else (indent, block scalar, duplicate key, empty/missing/unterminated block, control chars)
    # is reported in `problems`; callers must treat a note with problems as untrusted metadata (fail closed).
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if lines[0].rstrip(" \t") != "---":
        return {}, text, ["no front matter block at the top of the file"]
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip(" \t") == "---"), None)
    if end is None:
        return {}, text, ["front matter block is not terminated"]
    block, body = lines[1:end], "\n".join(lines[end + 1:])
    problems: list[str] = []
    meta: dict[str, str] = {}
    if not any(ln.strip() for ln in block):
        problems.append("front matter block is empty")
    for idx, line in enumerate(block, 2):
        if not line.strip():
            continue
        if _FM_BAD_CHARS.search(line):
            problems.append(f"line {idx}: control or line-separator character")
            continue
        if line[0] in " \t":
            problems.append(f"line {idx}: indented front-matter line")
            continue
        m = _FM_LINE.match(line)
        if not m:
            problems.append(f"line {idx}: not a plain 'key: value' line")
            continue
        key, val = m.group(1).lower(), (m.group(2) or "").strip()
        if val[:1] in ("|", ">"):
            problems.append(f"line {idx}: block scalar for '{key}'")
            continue
        if key in meta:
            problems.append(f"line {idx}: duplicate key '{key}'")
            continue
        meta[key] = _unquote(val)
    return meta, body, problems


def read_note(path: Path) -> tuple[dict, str, list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_front_matter(text)


def sqlite_copy_query(db: Path, fn):
    # Run fn(conn) on a COPY of db in a temp dir (never opens the original). -wal/-shm siblings are copied
    # too (so committed-but-uncheckpointed data is visible); the rollback journal is deliberately not copied.
    tmp = Path(tempfile.mkdtemp(prefix="suite-db-"))
    try:
        dst = tmp / "copy.sqlite3"
        shutil.copyfile(db, dst)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.is_file():
                shutil.copyfile(side, tmp / ("copy.sqlite3" + suffix))
        conn = sqlite3.connect(str(dst))
        try:
            return fn(conn)
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def last_jsonl_record(path: Path):
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 65536))
        chunk = handle.read().decode("utf-8", errors="replace")
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    if not lines:
        return None, 0
    try:
        return json.loads(lines[-1]), len(lines)
    except ValueError:
        return {"_unparsed": lines[-1][:80]}, len(lines)


# ---------------------------------------------------------------- facts
def load_facts() -> list[dict]:
    rows = []
    if not FACTS.is_dir() or FACTS.is_symlink():
        return rows
    for path in sorted(FACTS.glob("*.md")):
        problems: list[str] = []
        meta, body = {}, ""
        try:
            if path.is_symlink():
                problems.append("fact file is a symlink")
            meta, body, fm = read_note(path)
            problems += fm
        except OSError as exc:
            problems.append(f"unreadable ({type(exc).__name__})")
        rows.append({
            "id": meta.get("id") or path.stem,
            "path": path,
            "status": (meta.get("status") or "").lower(),
            "sensitivity": (meta.get("sensitivity") or "").lower(),
            "superseded_by": meta.get("superseded_by") or meta.get("superseded-by") or "",
            "text": body.strip(),
            "problems": problems,
        })
    return rows


# ---------------------------------------------------------------- skills model
# Limits of the agents' own skill libraries (read from Chat On Steroids' code and app.log): above ROOT_CAPS skills the
# whole library is switched off; a SKILL.md longer than ROOT_MAX_CHARS cannot be loaded.
ROOT_CAPS = {"cos": 128}
ROOT_MAX_CHARS = {"cos": 96_000}
# Written differently for each agent on purpose (it describes how THAT agent reaches the memory), so never drift.
SKILL_VARIANTS = ("infinite-context",)


def nested_index(troot: Path) -> dict[str, str]:
    """{sub-skill name: parent skill} for every skill folder of `troot` that carries other skills as nested folders.
    A shared skill that sits nested inside such a parent is delivered there; it does not need its own top-level folder
    (which matters where the folder count is capped)."""
    index: dict[str, str] = {}
    if not troot.is_dir():
        return index
    try:
        parents = sorted(p for p in troot.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    except OSError:
        return index
    for parent in parents:
        try:
            for sub in parent.iterdir():
                if sub.is_dir() and not sub.is_symlink() and (sub / "SKILL.md").is_file():
                    index.setdefault(sub.name, parent.name)
        except OSError:
            continue
    return index


def load_config() -> tuple[dict, str]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["strip_keys"] = {}
    cfg["projects"] = []          # autosync: project folders whose AGENTS.md / CLAUDE.md are mirrored
    cfg["create_roots"] = False   # autosync: create missing skills roots (default: never)
    cfg["autosync_settle_seconds"] = 20
    cfg["groups"] = []            # autosync: roots read by the SAME agent; a skill lives in only one of them
    cfg["protect_existing"] = False  # autosync: never overwrite a differing copy that autosync did not write itself
    cfg["variants"] = list(SKILL_VARIANTS)  # skills that are deliberately different per agent (never "drift")
    note = ""
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for key in ("skills", "targets", "converter"):
                    if key in data:
                        cfg[key] = data[key]
                if not (isinstance(cfg["targets"], dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in cfg["targets"].items())):
                    cfg["targets"] = DEFAULT_CONFIG["targets"]
                    note = f"{CONFIG_PATH.name}: 'targets' must be a name->path mapping; defaults used"
                if isinstance(data.get("projects"), list):
                    cfg["projects"] = [x for x in data["projects"] if isinstance(x, str) and x.strip()]
                if data.get("create_roots") is True:
                    cfg["create_roots"] = True
                ss = data.get("autosync_settle_seconds")
                if isinstance(ss, (int, float)) and not isinstance(ss, bool) and 0 <= ss <= 600:
                    cfg["autosync_settle_seconds"] = ss
                if data.get("protect_existing") is True:
                    cfg["protect_existing"] = True
                gr = data.get("groups")
                if isinstance(gr, list):
                    cfg["groups"] = [[x for x in g if isinstance(x, str)] for g in gr
                                     if isinstance(g, list) and len([x for x in g if isinstance(x, str)]) > 1]
                sk = data.get("strip_keys")
                if isinstance(sk, dict):
                    cfg["strip_keys"] = {str(k): [x for x in v if isinstance(x, str)] for k, v in sk.items() if isinstance(v, list)}
                vr = data.get("variants")
                if isinstance(vr, list):
                    cfg["variants"] = [x for x in vr if valid_skill_name(x)]
        except (OSError, ValueError) as exc:
            note = f"{CONFIG_PATH.name} unreadable ({type(exc).__name__}); defaults used"
    else:
        note = f"{CONFIG_PATH.name} not found; defaults used (run `skills init` to write it)"
    return cfg, note


def resolve_target(template: str) -> Path:
    text = (str(template).replace("{home}", str(user_home())).replace("{appdata}", str(appdata()))
            .replace("{vault}", str(VAULT)).replace("{engine}", str(ENGINE)))
    path = Path(text).expanduser()
    return path if path.is_absolute() else VAULT / path


SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def valid_skill_name(name) -> bool:
    return (isinstance(name, str) and SKILL_NAME_RE.fullmatch(name) is not None
            and ".." not in name and not name.endswith("."))


def split_skill_names(cfg: dict) -> tuple[list[str], list[str]]:
    # -> (valid names, rejected names). Names become path components, so they must be plain.
    wanted = cfg.get("skills") or []
    if not isinstance(wanted, list):
        return [], [f"<skills is {type(wanted).__name__}, expected a list>"]
    if wanted:
        names, rejected = [], []
        for n in wanted:
            if valid_skill_name(n):
                if n not in names:
                    names.append(n)
            else:
                rejected.append(repr(n)[:60])
        return names, rejected
    if not SRC_SKILLS.is_dir():
        return [], []
    found, rejected = [], []
    for p in sorted(SRC_SKILLS.iterdir()):
        if (p / "SKILL.md").is_file():
            (found if valid_skill_name(p.name) and not p.is_symlink() else rejected).append(p.name)
    return found, rejected


def skill_names(cfg: dict) -> list[str]:
    return split_skill_names(cfg)[0]


def file_digest(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def tree_files(root: Path) -> dict[str, str]:
    files = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                files[p.relative_to(root).as_posix()] = file_digest(p)
    return files


def tree_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for k in sorted(files):
        h.update(k.encode() + b"\0" + files[k].encode() + b"\0")
    return h.hexdigest()[:12]


def root_reachable(troot: Path) -> bool:
    return troot.is_dir() or troot.parent.is_dir()


_ADAPTER = []


def skill_adapter():
    # Built-in front-matter adapter (skill_formats.py next to suite.py); None if the file is absent/broken.
    if not _ADAPTER:
        mod = None
        path = SCRIPT_DIR / "skill_formats.py"
        if path.is_file():
            try:
                spec = importlib.util.spec_from_file_location("skill_formats", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception:
                mod = None
        _ADAPTER.append(mod)
    return _ADAPTER[0]


def _canon_md_digest(data: bytes):
    mod = skill_adapter()
    if mod is None:
        return None
    try:
        return hashlib.sha256(mod.adapt(data, None, []).replace(b"\r\n", b"\n")).hexdigest()
    except Exception:
        return None


def adapted_skill_md(cfg: dict, src: Path, target: str):
    # -> (adapted bytes | None, error | None). None/None = adapter not applicable (plain copy).
    mod = skill_adapter()
    if mod is None or target not in mod.KNOWN_TARGETS or find_converter(cfg) or not (src / "SKILL.md").is_file():
        return None, None
    try:
        data = (src / "SKILL.md").read_bytes()
        return mod.adapt(data, target, mod.strip_for(cfg.get("strip_keys"), target)), None
    except mod.SkillFormatError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


# Pointer lines the shared source adds for apps that cannot hold every skill at the top level ("read the nested file").
_POINTER_LINE = re.compile(r"(?m)^(?:Related skill [^\n]*|For [^\n,]+, read /skills/[^\n]*)\n?")


def _flat_equivalent(row: dict, tops: dict[str, set]) -> bool:
    """A copy that differs from the shared one ONLY because it keeps each sub-skill as its own top-level skill (instead of
    nested inside the parent, with pointer lines) is equivalent: nothing is missing there. `tops` = top-level skill
    names of every root of this agent."""
    nested = {p.name for p in row["src"].iterdir() if p.is_dir() and (p / "SKILL.md").is_file()} if row["src"].is_dir() else set()
    if not nested:
        return False
    for rel in row["changed"]:
        head = rel.split("/", 1)[0]
        if rel == "SKILL.md":
            continue
        if head not in nested or head not in tops.get("*", set()):
            return False
    if "SKILL.md" in row["changed"]:
        a = _POINTER_LINE.sub("", _read_text(row["src"] / "SKILL.md"))
        b = _POINTER_LINE.sub("", _read_text(row["dir"] / "SKILL.md"))
        da, db = _canon_md_digest(a.encode("utf-8")), _canon_md_digest(b.encode("utf-8"))
        if not da or da != db:
            return False
    return True


def _top_level_names(troot: Path) -> set:
    try:
        return {p.name for p in troot.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()}
    except OSError:
        return set()


def skill_states(cfg: dict) -> list[dict]:
    rows = []
    nested: dict[str, dict] = {}
    for name in skill_names(cfg):
        src = SRC_SKILLS / name
        src_files = tree_files(src)
        for tname, tpl in cfg["targets"].items():
            troot = resolve_target(tpl)
            tdir = troot / name
            row = {"skill": name, "target": tname, "root": troot, "dir": tdir, "src": src, "src_files": src_files,
                   "want": src_files, "adapted": {}, "changed": [], "extra": [], "state": ""}
            adapted, bad = adapted_skill_md(cfg, src, tname) if src_files else (None, None)
            if adapted is not None:
                row["adapted"] = {"SKILL.md": adapted}
                row["want"] = dict(src_files, **{"SKILL.md": hashlib.sha256(adapted.replace(b"\r\n", b"\n")).hexdigest()})
            if not src_files:
                row["state"] = "NOSOURCE"
            elif bad:
                row["state"] = "INVALID"
                row["reason"] = bad
            elif not root_reachable(troot):
                row["state"] = "UNREACHABLE"
            elif tname in ROOT_MAX_CHARS and len(_read_text(src / "SKILL.md")) > ROOT_MAX_CHARS[tname]:
                row["state"] = "OVERSIZE"  # the app cannot load it; it keeps its own short entry that points to the full file
            elif not tdir.is_dir():
                # delivered inside another skill's folder there (a nested sub-skill of a parent): not missing
                nidx = nested.setdefault(tname, nested_index(troot))
                row["state"] = "IN-UMBRELLA" if name in nidx else "MISSING"
            else:
                tfiles = tree_files(tdir)
                for rel_md in [k for k in tfiles if k.endswith("SKILL.md") and k in row["want"]]:
                    # same canonical content (front matter normalised, the COS importer's footer and trailing blank
                    # lines ignored) is not drift; this holds for nested sub-skills' SKILL.md files too
                    want_bytes = row["adapted"].get(rel_md) or (src / rel_md).read_bytes()
                    a, b = _canon_md_digest((tdir / rel_md).read_bytes()), _canon_md_digest(want_bytes)
                    if a and a == b:
                        tfiles[rel_md] = row["want"][rel_md]
                row["changed"] = [k for k, v in row["want"].items() if tfiles.get(k) != v]
                row["extra"] = [k for k in tfiles if k not in src_files]
                row["state"] = "DIFFERS" if row["changed"] else "SAME"
                if row["state"] == "DIFFERS" and name in (cfg.get("variants") or []):
                    row["state"] = "VARIANT"
                elif row["state"] == "DIFFERS":
                    members = {tname} | {m for g in (cfg.get("groups") or []) if tname in g for m in g}
                    tops = {"*": set().union(*(_top_level_names(resolve_target(cfg["targets"][m])) for m in members if m in cfg["targets"]))}
                    if _flat_equivalent(row, tops):
                        row["state"] = "FLAT"
            rows.append(row)
    return rows


def find_converter(cfg: dict) -> Path | None:
    conf = cfg.get("converter")
    if conf:
        p = Path(str(conf)).expanduser()
        p = p if p.is_absolute() else ENGINE / p
    else:
        p = ENGINE / "skill-convert.py"
    return p if p.is_file() else None


# ---------------------------------------------------------------- doctor checks
def chk_heartbeat():
    p = ENGINE / "capture-service-status.json"
    if not p.is_file():
        if not (appdata() / "chat-on-steroids").is_dir():
            return UNKNOWN, ["not applicable: Chat On Steroids is not installed on this computer, so there is no capture service to watch"]
        return FAIL, [f"{rel(p)} is missing - the capture service has never written a heartbeat"]
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        raw_ts = data.get("timestamp")
    except (OSError, ValueError, AttributeError) as exc:
        return WARN, [f"{rel(p)} unreadable: {type(exc).__name__}"]
    ts, aware = parse_ts_aware(raw_ts)
    if ts is None:
        return WARN, [f"{rel(p)} has no valid 'timestamp' field"]
    if not aware:
        return WARN, [f"{rel(p)} timestamp {str(raw_ts)[:40]!r} has no timezone (expected ISO-8601 with 'Z'); its age cannot be judged"]
    if is_future(ts):
        return WARN, [f"heartbeat timestamp {iso(ts)} is in the FUTURE (now {iso(now())}): clock skew or a bad writer; freshness cannot be judged"]
    age = age_of(ts)
    ev = [f"heartbeat {iso(ts)} ({fmt_age(age)} ago); new_messages={data.get('new_messages')} total={data.get('total')}",
          "scheduled task should refresh it every minute"]
    secs = age.total_seconds()
    return (PASS if secs <= 300 else WARN if secs <= 3600 else FAIL), ev


CAPTURE_SCAN_LIMIT = 3000


def capture_times():
    # Evidence = capture-state saved_at values and the front-matter 'updated' of capture notes.
    # File mtimes are NOT evidence (anything can touch a file); they only choose which notes to open.
    valid, future, notes = [], [], []
    naive = 0

    def consider(val):
        nonlocal naive
        dt, aware = parse_ts_aware(val)
        if dt is None:
            return
        if not aware:
            naive += 1
        elif is_future(dt):
            future.append(dt)
        else:
            valid.append(dt)

    for name in ("capture-state.sqlite3", "claude-capture-state.sqlite3"):
        db = ENGINE / name
        if not db.is_file():
            continue

        def q(conn):
            out = []
            for (tbl,) in conn.execute("select name from sqlite_master where type='table'").fetchall():
                cols = [c[1] for c in conn.execute(f'pragma table_info("{tbl}")').fetchall()]
                if "saved_at" in cols:
                    cnt = conn.execute(f'select count(*) from "{tbl}"').fetchone()[0]
                    vals = [r[0] for r in conn.execute(f'select saved_at from "{tbl}" order by saved_at desc limit 200').fetchall()]
                    out.append((tbl, cnt, vals))
            return out
        try:
            for tbl, cnt, vals in sqlite_copy_query(db, q):
                before = len(valid)
                for v in vals:
                    consider(v)
                newest = max(valid[before:], default=None)
                notes.append(f"{name}:{tbl} newest saved_at {iso(newest) if newest else '(none usable)'} ({cnt} rows)")
        except sqlite3.Error as exc:
            notes.append(f"{name} unreadable: {type(exc).__name__}")
    if CAPTURE.is_dir():
        files = []
        for p in CAPTURE.rglob("*.md"):
            try:
                if not p.is_symlink():
                    files.append((p.stat().st_mtime, p))
            except OSError:
                continue
        files.sort(key=lambda t: t[0], reverse=True)
        opened = 0
        got = []
        for _, p in files[:CAPTURE_SCAN_LIMIT]:
            try:
                with p.open("rb") as handle:
                    head = handle.read(4096).decode("utf-8-sig", errors="replace")
            except OSError:
                continue
            meta, _body, probs = parse_front_matter(head)
            opened += 1
            if not probs and meta.get("updated"):
                before = len(valid)
                consider(meta["updated"])
                got += valid[before:]
        if opened:
            notes.append(f"{opened} capture note(s) opened for front-matter 'updated'; newest {iso(max(got)) if got else '(none usable)'} (file mtimes ignored)")
    if naive:
        notes.append(f"{naive} timestamp(s) without a timezone were ignored (cannot be judged)")
    return (max(valid) if valid else None), future, notes


def chk_last_message():
    latest, future, notes = capture_times()
    if future:
        return WARN, [f"{len(future)} capture timestamp(s) are in the FUTURE (newest {iso(max(future))}); clock skew or bad data"] + notes
    if latest is None:
        return UNKNOWN, ["no timestamped capture evidence (capture state rows / note front matter); file mtimes are deliberately not used"] + notes
    age = age_of(latest)
    ev = [f"last capture {iso(latest)} ({fmt_age(age)} ago)"] + notes
    if age.total_seconds() <= 86400:
        return PASS, ev
    return WARN, ev + ["older than 24h: either you were idle, or capture is not running (compare with check 1)"]


def chk_hooks():
    p = ENGINE / "logs" / "hook-events.jsonl"
    if not p.is_file():
        return UNKNOWN, [f"{rel(p)} does not exist yet: no Claude Code session has run since the hooks were registered. "
                         "Open a new chat there, then check again (check 4 says whether they are registered)"]
    rec, _ = last_jsonl_record(p)
    ev = []
    if rec is None:
        return WARN, [f"{rel(p)} exists but is empty"]
    ts, aware = parse_ts_aware(rec.get("ts")) if isinstance(rec, dict) else (None, False)
    if ts is None:
        ev.append(f"last line has no parseable 'ts' (file mtime {iso(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))}, not used as evidence)")
        status = WARN
    elif not aware:
        ev.append(f"last hook event 'ts' {str(rec.get('ts'))[:40]!r} has no timezone; age cannot be judged")
        status = WARN
    elif is_future(ts):
        ev.append(f"last hook event {iso(ts)} is in the FUTURE (now {iso(now())}); clock skew or bad data")
        status = WARN
    else:
        age = age_of(ts)
        ev.append(f"last hook event {iso(ts)} ({fmt_age(age)} ago): {rec.get('event')} ms={rec.get('ms')}")
        status = PASS if age.total_seconds() <= 86400 else WARN
    if isinstance(rec, dict) and rec.get("error"):
        ev.append(f"last event carried an error: {str(rec['error'])[:100]}")
        status = WARN
    err = ENGINE / "logs" / "errors.log"
    if err.is_file() and err.stat().st_size:
        ev.append(f"{rel(err)} is non-empty ({err.stat().st_size} bytes)")
    return status, ev


# the dispatcher must appear as a real path token (not as a substring of a comment or another name)
HOOK_CMD_RE = re.compile(r"(?:^|[\s\"'/\\])claude_hook\.py(?=$|[\s\"'])")
LEGACY_HOOK_RE = re.compile(r"topic_router|infinite_context")
CODEX_MCP_RE = re.compile(r'^\s*\[mcp_servers\.("?)infinity-memory\1\]\s*(?:#.*)?$', re.M)


def chk_claude_reg():
    env = os.environ.get("SUITE_CLAUDE_SETTINGS")
    p = Path(env) if env else user_home() / ".claude" / "settings.json"
    if not p.is_file():
        return UNKNOWN, [f"{p} not reachable from here (not a failure). Run `python .ai-memory/suite.py doctor` on the Windows machine."]
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        hooks = data.get("hooks") or {}
    except (OSError, ValueError, AttributeError) as exc:
        return WARN, [f"{p} unreadable or not valid JSON ({type(exc).__name__}); registration cannot be judged"]
    events = ["UserPromptSubmit", "SessionStart", "PreCompact", "SessionEnd", "Stop"]
    ok, legacy, missing = [], [], []
    for ev_name in events:
        cmds = []
        for group in (hooks.get(ev_name) or []) if isinstance(hooks, dict) and isinstance(hooks.get(ev_name) or [], list) else []:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                cmds += [str(h.get("command", "")) for h in group["hooks"] if isinstance(h, dict)]
        has_new = any(HOOK_CMD_RE.search(c) for c in cmds)
        has_old = any(LEGACY_HOOK_RE.search(c) for c in cmds)
        (ok if has_new else missing).append(ev_name)
        if has_new and has_old:
            legacy.append(ev_name)
    ev = [f"{p}: claude_hook.py command registered for {len(ok)}/5 events ({', '.join(ok) or 'none'})"]
    if len(ok) == 5 and not legacy:
        return PASS, ev + ["reference present; actual firing is judged by check 3, not here"]
    if not ok:
        return FAIL, ev + ["settings.json readable but no claude_hook.py hook command: run merge_claude_settings.py --apply"]
    if missing:
        ev.append("missing: " + ", ".join(missing))
    if legacy:
        ev.append("legacy topic_router/infinite_context hooks coexist on: " + ", ".join(legacy) + " (double injection)")
    return WARN, ev


def _json_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _json_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _json_strings(v)


def _strip_toml_comment(line: str) -> str:
    out, quote, i = [], None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def chk_codex():
    env = os.environ.get("SUITE_CODEX_CONFIG")
    cands = [Path(env)] if env else [user_home() / ".codex" / "hooks.json", user_home() / ".codex" / "config.toml"]
    existing = [c for c in cands if c.is_file()]
    if not existing:
        return UNKNOWN, ["no Codex config reachable from here: " + ", ".join(str(c) for c in cands)]
    hits, legacy_only, warns, mcp_hits = [], [], [], []
    for c in existing:
        try:
            text = c.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if c.suffix.lower() == ".toml" and CODEX_MCP_RE.search(text):
            mcp_hits.append(f"{c}: registers the MCP server infinity-memory")
        if c.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except ValueError:
                warns.append(f"{c} is not valid JSON; cannot judge")
                continue
            scope = data.get("hooks", data) if isinstance(data, dict) else data
            strings = list(_json_strings(scope))
        else:  # TOML: comment lines and trailing comments are not references
            strings = [_strip_toml_comment(ln) for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        if any(HOOK_CMD_RE.search(s) for s in strings):
            hits.append(f"{c}: references the current dispatcher claude_hook.py")
        elif any(LEGACY_HOOK_RE.search(s) for s in strings):
            legacy_only.append(f"{c}: only LEGACY topic_router/infinite_context references (no claude_hook.py)")
    if hits:
        return PASS, hits + mcp_hits + legacy_only + warns + ["reference present; it firing is not verified"]
    if mcp_hits:   # Codex reads the shared memory through the MCP server; a hook is optional
        return PASS, mcp_hits + legacy_only + warns + ["registered; that Codex really calls it is not verified"]
    if warns and not legacy_only:
        return WARN, warns
    return WARN, legacy_only + warns + [f"{c} readable but has no hook command and no infinity-memory MCP server" for c in existing]


SKILL_OK_STATES = ("SAME", "IN-SIBLING", "UNREACHABLE", "IN-UMBRELLA", "OVERSIZE", "VARIANT", "FLAT")
MATRIX_FILE = ENGINE / "skills-matrix.json"


def skills_signature(cfg: dict) -> str:
    """A cheap fingerprint (names, sizes, mtimes: no file is read) of everything the skills comparison depends on, so the
    comparison, which hashes every file of every copy, only runs again when something changed."""
    h = hashlib.sha256()
    for root in [SRC_SKILLS] + [resolve_target(t) for t in cfg["targets"].values()]:
        h.update(str(root).encode("utf-8", "replace") + b"\n")
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for fname in sorted(filenames):
                p = os.path.join(dirpath, fname)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    for extra in (CONFIG_PATH, ENGINE / "autosync-state.json"):
        try:
            st = extra.stat()
            h.update(f"{extra.name}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            pass
    return h.hexdigest()[:20]


def finalize_skill_rows(cfg: dict, rows: list[dict]) -> list[dict]:
    """autosync semantics on top of skill_states: a skill kept in ONE root of a same-agent group is not missing elsewhere in
    that group, and a differing copy autosync never wrote is deliberately left alone (protect_existing), not drift."""
    groups: dict = {}
    for grp in cfg.get("groups") or []:
        for m in grp:
            groups.setdefault(m, set()).update(grp)
    try:
        ast = json.loads((ENGINE / "autosync-state.json").read_text(encoding="utf-8")).get("skills") or {}
    except (OSError, ValueError):
        ast = {}
    by_key = {(r["skill"], r["target"]): r for r in rows}
    for r in rows:
        if r["state"] == "MISSING":
            sibs = groups.get(r["target"], set()) - {r["target"]}
            if any(by_key.get((r["skill"], o), {}).get("state") in ("SAME", "DIFFERS", "PROTECTED", "IN-SIBLING", "VARIANT", "IN-UMBRELLA", "FLAT") for o in sibs):
                r["state"] = "IN-SIBLING"
        elif r["state"] == "DIFFERS" and cfg.get("protect_existing"):
            ent = ((ast.get(r["skill"]) or {}).get("roots") or {}).get(r["target"])
            if not (isinstance(ent, dict) and ent.get("autosync")):
                r["state"] = "PROTECTED"
    return rows


def skills_matrix(cfg: dict | None = None, use_cache: bool = True) -> dict:
    """Every shared skill x every agent with its final state, plus how full each capped root is. Cached in
    skills-matrix.json until the fingerprint of the skills folders changes."""
    cfg = cfg or load_config()[0]
    sig = skills_signature(cfg)
    if use_cache:
        try:
            cached = json.loads(MATRIX_FILE.read_text(encoding="utf-8"))
            if cached.get("version") == 1 and cached.get("sig") == sig:
                return cached
        except (OSError, ValueError):
            pass
    names, _bad = split_skill_names(cfg)
    rows = finalize_skill_rows(cfg, skill_states(cfg))
    slim = [{"skill": r["skill"], "target": r["target"], "state": r["state"], "changed": r["changed"][:3], "extra": len(r["extra"]),
             "reason": r.get("reason", "")} for r in rows]
    counts: dict = {}
    for r in slim:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    caps = {t: {"limit": lim, "count": len(_top_level_names(resolve_target(cfg["targets"][t])))} for t, lim in ROOT_CAPS.items() if t in cfg["targets"]}
    data = {"version": 1, "sig": sig, "generated": iso(now()), "targets": list(cfg["targets"]), "groups": cfg.get("groups") or [],
            "skills": names, "rows": slim, "counts": counts, "caps": caps}
    try:
        tmp = MATRIX_FILE.with_name(MATRIX_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, MATRIX_FILE)
    except OSError:
        pass
    return data


def chk_skills():
    cfg, note = load_config()
    ev = [note] if note else []
    if not SRC_SKILLS.is_dir():
        return WARN, ev + [f"{rel(SRC_SKILLS)} missing - run `suite.py skills init` once to seed it from claude-ai-skill/"]
    names, rejected = split_skill_names(cfg)
    ev += [f"invalid skill name {b} in config ignored (letters, digits, . _ - only)" for b in rejected]
    if not names:
        return WARN, ev + ["no valid skills found in skills-src/config"]
    conv = find_converter(cfg)
    if conv:
        ev.append(f"converter present ({conv.name}): converted targets legitimately differ from source")
    m = skills_matrix(cfg)
    rows, counts = m["rows"], m["counts"]
    ev.append("skills x targets: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for r in rows:
        if r["state"] in SKILL_OK_STATES:
            continue
        extra = f", +{r['extra']} extra file(s)" if r["extra"] else ""
        chg = f" changed: {', '.join(r['changed'][:3])}" if r["changed"] else ""
        ev.append(f"  {r['skill']} @ {r['target']:<7} {r['state']:<10}{chg}{extra}")
    reach = [r for r in rows if r["state"] not in ("UNREACHABLE", "NOSOURCE")]
    if not reach:
        return UNKNOWN, ev + ["no target skills folder reachable from here (run on Windows)"]
    if rejected:
        return WARN, ev + ["config contains invalid skill names (skipped)"]
    if any(r["state"] in ("DIFFERS", "MISSING") for r in reach):
        return WARN, ev + ["drift: use `skills diff` then `skills push` (dry-run)"]
    return PASS, ev


def chk_index():
    db = ENGINE / "context-index.sqlite3"
    ev = []
    status = PASS
    journals = sorted(ENGINE.glob("*-journal")) if ENGINE.is_dir() else []
    if not db.is_file():
        status = WARN
        ev.append(f"{rel(db)} missing - build it with infinite_context.py")
    else:
        try:
            rows = sqlite_copy_query(db, lambda c: c.execute("PRAGMA integrity_check").fetchall())
            res = "; ".join(str(r[0]) for r in rows)[:200]
            sides = [s for s in ("-wal", "-shm") if db.with_name(db.name + s).is_file()]
            ev.append(f"integrity_check on a /tmp copy of {db.name} ({db.stat().st_size} bytes{', with ' + '/'.join(sides) + ' copied too' if sides else ''}): {res}")
            if res.strip().lower() != "ok":
                status = FAIL
        except (sqlite3.Error, OSError) as exc:
            status = FAIL
            ev.append(f"cannot open copy of {db.name}: {type(exc).__name__}: {str(exc)[:100]}")
    for j in journals:
        size = j.stat().st_size
        ev.append(f"leftover journal {j.name}: {size} bytes" + (" (empty, harmless, safe to delete)" if size == 0 else " (NON-EMPTY: interrupted write; the integrity result may not reflect it)"))
        if status == PASS:
            status = WARN
    return status, ev


def queue_counts() -> dict:
    # autosync conflict notes live in 30-Pending too but are not fact candidates
    pending = len([p for p in PENDING.glob("*.md") if not p.name.startswith(("skill-conflict-", "project-conflict-"))]) if PENDING.is_dir() else 0
    rejected = len(list((PENDING / "rejected").glob("*.md"))) if (PENDING / "rejected").is_dir() else 0
    facts = load_facts()
    sens: dict[str, int] = {}
    for f in facts:
        key = "(blocked-frontmatter)" if f["problems"] else (f["sensitivity"] or "(missing)")
        if f["status"] != "active" and not f["problems"]:
            key += f"/{f['status'] or 'no-status'}"
        sens[key] = sens.get(key, 0) + 1
    sessions = [d for d in CHECKPOINTS.iterdir() if d.is_dir()] if CHECKPOINTS.is_dir() else []
    cp_files = len(list(CHECKPOINTS.rglob("*.md"))) if CHECKPOINTS.is_dir() else 0
    return {"pending": pending, "rejected": rejected, "facts": len(facts), "sens": sens, "cp_dirs": len(sessions), "cp_files": cp_files}


def chk_queue():
    q = queue_counts()
    sens = ", ".join(f"{k}={v}" for k, v in sorted(q["sens"].items())) or "none"
    ev = [f"30-Pending candidates: {q['pending']} (rejected: {q['rejected']})",
          f"10-Facts: {q['facts']} total; by sensitivity: {sens}",
          f"20-Checkpoints: {q['cp_files']} note(s) in {q['cp_dirs']} folder(s)"]
    return (WARN if q["pending"] > 25 else PASS), ev + (["many pending candidates: review with pending.py list"] if q["pending"] > 25 else [])


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def chk_autosync():
    p = ENGINE / "autosync-status.json"
    if not p.is_file():
        return UNKNOWN, [f"{rel(p)} not found - autosync has never run (install: double-click INSTALL-AUTOSYNC.cmd on Windows)"]
    data = _read_json_file(p)
    if not isinstance(data, dict):
        return WARN, [f"{rel(p)} unreadable or not a JSON object"]
    raw_ts = data.get("last_run")
    ts, aware = parse_ts_aware(raw_ts)
    if ts is None or not aware:
        return WARN, [f"{rel(p)} has no valid timezone-aware 'last_run' ({str(raw_ts)[:40]!r}); age cannot be judged"]
    if is_future(ts):
        return WARN, [f"autosync last_run {iso(ts)} is in the FUTURE (now {iso(now())}); freshness cannot be judged"]
    age = age_of(ts)
    secs = age.total_seconds()
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    conflicts = data.get("conflicts") if isinstance(data.get("conflicts"), list) else []
    qpath = ENGINE / "autosync-queue.jsonl"
    qlen, oldest = 0, None
    if qpath.is_file():
        try:
            for line in qpath.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    qlen += 1
                    try:
                        q_ts = parse_ts(json.loads(line).get("queued_at"))
                    except (ValueError, AttributeError):
                        q_ts = None
                    if q_ts and (oldest is None or q_ts < oldest):
                        oldest = q_ts
        except OSError:
            pass
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    ev = [f"last run {iso(ts)} ({fmt_age(age)} ago); baseline_done={data.get('baseline_done')}; "
          f"skills seen={counts.get('skills_seen')}, propagated last run={counts.get('propagated')}, roots reachable={counts.get('roots_reachable')}",
          f"conflicts waiting: {len(conflicts)}" + (f" ({', '.join(str(c) for c in conflicts[:5])}) - see 30-Pending" if conflicts else ""),
          f"install queue: {qlen} item(s)" + (f", oldest {fmt_age(age_of(oldest))}" if oldest else "")]
    if errors:
        ev.append(f"last run had {len(errors)} error(s); first: {str(errors[0])[:140]} (logs/autosync-errors.log)")
    status = PASS if secs <= 300 else WARN if secs <= 3600 else FAIL
    if status == PASS and (conflicts or errors):
        status = WARN
    if status == PASS and qlen and oldest and age_of(oldest) > timedelta(minutes=30):
        status = WARN
        ev.append("queue items are older than 30 minutes: a target folder may be locked or unreachable")
    if secs > 300:
        ev.append("the scheduled task MemorySuiteAutoSync should refresh this every minute (manage_autosync.ps1 status)")
    return status, ev


CHECKS = [
    ("Capture heartbeat", chk_heartbeat),
    ("Last captured message", chk_last_message),
    ("Hook activity", chk_hooks),
    ("Claude Code registration", chk_claude_reg),
    ("Codex hook reference", chk_codex),
    ("Skills drift", chk_skills),
    ("Index integrity", chk_index),
    ("Queue counts", chk_queue),
    ("Autosync", chk_autosync),
]


def run_check(fn):
    try:
        return fn()
    except Exception as exc:  # doctor must never raise
        return UNKNOWN, [f"check crashed: {type(exc).__name__}: {str(exc)[:150]}"]


def cmd_doctor(_args) -> int:
    print(f"== Memory Suite doctor == vault={VAULT}  now={iso(now())}")
    tally = {PASS: 0, WARN: 0, FAIL: 0, UNKNOWN: 0}
    for i, (title, fn) in enumerate(CHECKS, 1):
        status, ev = run_check(fn)
        tally[status] += 1
        print(f"[{status:<7}] {i}. {title}")
        for line in ev:
            print(f"           {line}" if line else "")
    print(f"Summary: {tally[PASS]} PASS, {tally[WARN]} WARN, {tally[FAIL]} FAIL, {tally[UNKNOWN]} UNKNOWN (doctor always exits 0)")
    return 0


def cmd_status(_args) -> int:
    print(f"Memory Suite status  {iso(now())}\nVault: {VAULT}")
    hb = run_check(chk_heartbeat)
    lm = run_check(chk_last_message)
    hk = run_check(chk_hooks)
    print(f"Capture : heartbeat {hb[0]} | last message {lm[0]} | hooks {hk[0]}")
    print(f"          {hb[1][0]}")
    print(f"          {lm[1][0]}")
    q = queue_counts()
    sens = ", ".join(f"{k}={v}" for k, v in sorted(q["sens"].items())) or "none"
    print(f"Queue   : pending {q['pending']} | facts {q['facts']} ({sens}) | checkpoints {q['cp_files']} in {q['cp_dirs']} folder(s)")
    try:
        cfg, _ = load_config()
        rows = skill_states(cfg) if SRC_SKILLS.is_dir() else []
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        print("Skills  : " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "skills-src missing (run `skills init`)"))
    except Exception as exc:
        print(f"Skills  : UNKNOWN ({type(exc).__name__})")
    ax = run_check(chk_autosync)
    print(f"Autosync: {ax[0]} | {ax[1][0]}")
    print("Details : python .ai-memory/suite.py doctor")
    return 0


# ---------------------------------------------------------------- skills commands
def ensure_src() -> None:
    seed = ENGINE / "claude-ai-skill" / "infinite-context"
    if not SRC_SKILLS.exists() and seed.is_dir():
        shutil.copytree(seed, SRC_SKILLS / "infinite-context")
        print(f"[seed] created {rel(SRC_SKILLS)}/infinite-context from {rel(seed)} (nothing deleted). "
              "Check that this is the version you want as source of truth.")


def cmd_skills(args) -> int:
    sub = args.skills_cmd
    if sub == "import":
        return skills_import(args)
    if sub == "init":
        if CONFIG_PATH.exists():
            print(f"{rel(CONFIG_PATH)} already exists; not overwritten")
        else:
            ENGINE.mkdir(parents=True, exist_ok=True)
            with CONFIG_PATH.open("x", encoding="utf-8") as handle:
                json.dump(DEFAULT_CONFIG, handle, indent=2)
                handle.write("\n")
            print(f"wrote {rel(CONFIG_PATH)}")
        ensure_src()  # only `init` and `push --apply` may create skills-src; list/diff/dry-run are read-only
        return 0
    if sub == "push" and args.apply:
        ensure_src()
    cfg, note = load_config()
    if note:
        print(f"[note] {note}")
    for bad in split_skill_names(cfg)[1]:
        print(f"[skip] invalid skill name {bad} (allowed: letters, digits, . _ -; no path separators or '..')")
    if not SRC_SKILLS.is_dir():
        print(f"{rel(SRC_SKILLS)} is missing; nothing to {sub}. Run `skills init` (or `skills push --apply`) to seed it from claude-ai-skill/ if present.")
        return 1
    rows = skill_states(cfg)
    if args.skill:
        rows = [r for r in rows if r["skill"] == args.skill]
    if getattr(args, "target", None):
        rows = [r for r in rows if r["target"] == args.target]
    if not rows:
        print("no skills/targets matched (is skills-src populated?)")
        return 1
    if sub == "list":
        seen = set()
        for r in rows:
            if r["skill"] not in seen:
                seen.add(r["skill"])
                print(f"{r['skill']}: source {rel(r['src'])} tree {tree_hash(r['src_files'])} ({len(r['src_files'])} file(s))")
            extra = f" +{len(r['extra'])} extra" if r["extra"] else ""
            print(f"  {r['target']:<7} {r['state']:<11} {r['dir']}{extra}")
        return 0
    if sub == "diff":
        for r in rows:
            print(f"--- {r['skill']} -> {r['target']}: {r['state']} ({r['dir']})")
            if r["state"] == "MISSING":
                print("  target copy absent")
            for relname in r["changed"]:
                a = (r["dir"] / relname)
                s = r["src"] / relname
                if a.is_symlink():
                    print(f"  {relname}: target is a symlink (not read)")
                    continue
                try:
                    old = a.read_text(encoding="utf-8", errors="replace").splitlines() if a.is_file() else []
                    new = (r["adapted"][relname].decode("utf-8", "replace") if relname in r["adapted"]
                           else s.read_text(encoding="utf-8", errors="replace")).splitlines()
                except OSError:
                    print(f"  {relname}: unreadable")
                    continue
                diff = list(difflib.unified_diff(old, new, f"target/{relname}", f"source/{relname}", lineterm=""))
                print("\n".join(diff[:120]) + ("\n  ... (truncated)" if len(diff) > 120 else ""))
            if r["extra"]:
                print(f"  extra files only in target (never deleted): {', '.join(r['extra'])}")
        return 0
    return push(cfg, rows, args.apply, args.create_roots)


class UnsafeTarget(Exception):
    pass


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def unsafe_target_reason(r: dict) -> str | None:
    # Why this target must not be written, or None. Guards the target==source loop and symlinked skill dirs.
    try:
        rsrc, rroot = SRC_SKILLS.resolve(), r["root"].resolve()
        tdir = r["dir"]
        if _inside(rroot, rsrc):
            return "target skills root is the source skills-src dir (or inside it): refusing the loop"
        if tdir.is_symlink():
            return "target skill dir is a symlink: never written through"
        rd = tdir.resolve()
        if _inside(rd, rsrc):
            return "target skill dir resolves into the source skills-src dir"
        if rd == rroot or not _inside(rd, rroot):
            return "target skill dir does not resolve inside its skills root"
    except (OSError, RuntimeError) as exc:
        return f"cannot resolve target path ({type(exc).__name__})"
    return None


def safe_copy(srcf: Path, tdir: Path, relname: str, data: bytes | None = None) -> None:
    # Copy one file under tdir without ever writing through a symlink: temp file in the same dir + os.replace.
    parts = Path(relname).parts
    if not parts or any(p in ("..", "") for p in parts) or Path(relname).is_absolute():
        raise UnsafeTarget(f"unsafe relative path {relname!r}")
    if not tdir.exists():
        tdir.mkdir(parents=True)
    tdir_real = tdir.resolve()
    cur = tdir
    for part in parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise UnsafeTarget(f"parent dir {cur} is a symlink")
        if cur.exists():
            if not cur.is_dir():
                raise UnsafeTarget(f"{cur} exists and is not a directory")
        else:
            cur.mkdir()
    dst = cur / parts[-1]
    if dst.is_symlink():
        raise UnsafeTarget("destination file is a symlink")
    if dst.exists() and not dst.is_file():
        raise UnsafeTarget("destination exists and is not a regular file")
    rd = dst.resolve()
    if rd == tdir_real or not _inside(rd, tdir_real):
        raise UnsafeTarget("destination does not resolve inside the target dir")
    fd, tmp = tempfile.mkstemp(prefix=".suite-tmp-", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            if data is not None:
                out.write(data)
            else:
                with srcf.open("rb") as inp:
                    shutil.copyfileobj(inp, out)
        shutil.copystat(srcf, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def push(cfg: dict, rows: list[dict], apply: bool, create_roots: bool) -> int:
    conv_path = find_converter(cfg)
    if cfg.get("converter") and not conv_path:
        print(f"[note] converter '{cfg['converter']}' not found; using plain copy")
    converter = None
    if conv_path and apply:
        try:
            spec = importlib.util.spec_from_file_location("skill_convert_plugin", conv_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            converter = getattr(mod, "convert", None)
            if not callable(converter):
                print(f"[error] {conv_path.name} has no convert(skill_dir, target)")
                return 1
        except Exception as exc:
            print(f"[error] cannot load converter {conv_path.name}: {type(exc).__name__}: {exc}")
            return 1
    print(("APPLY" if apply else "DRY-RUN (use --apply to write)") + (f"; converter: {conv_path}" if conv_path else "; plain copy"))
    backup_root = ENGINE / "backups" / f"skills-{stamp()}"
    failures = unsafe = invalid = 0
    for r in rows:
        tag = f"{r['skill']} -> {r['target']} ({r['dir']})"
        if r["state"] == "NOSOURCE":
            print(f"  SKIP     {tag}: source folder empty/missing")
            continue
        if r["state"] == "INVALID":
            invalid += 1
            print(f"  SKIP-INVALID {tag}: {r.get('reason')} (nothing written; fix skills-src SKILL.md)")
            continue
        why = unsafe_target_reason(r)
        if why:
            unsafe += 1
            print(f"  SKIP-UNSAFE {tag}: {why}")
            continue
        if r["state"] == "UNREACHABLE" and not create_roots:
            print(f"  SKIP     {tag}: skills root not reachable here (parent missing; --create-roots to force)")
            continue
        if r["state"] == "SAME" and not conv_path:
            print(f"  SAME     {tag}")
            continue
        if r["state"] in ("IN-UMBRELLA", "OVERSIZE", "VARIANT", "FLAT"):
            why = {"IN-UMBRELLA": "delivered inside another skill's folder there",
                   "OVERSIZE": "too long for that app's skill limit",
                   "VARIANT": "deliberately different per agent",
                   "FLAT": "each sub-skill is its own top-level skill there: equivalent"}[r["state"]]
            print(f"  SKIP     {tag}: {why}")
            continue
        action = "CREATE" if r["state"] in ("MISSING", "UNREACHABLE") else ("CONVERT" if conv_path else "UPDATE")
        detail = ", ".join(r["changed"][:5]) if r["changed"] else ""
        if not apply:
            print(f"  WOULD {action:<7} {tag} {detail}")
            continue
        dest_bk = None
        try:
            if r["dir"].is_dir():
                dest_bk = backup_root / r["target"] / r["skill"]
                shutil.copytree(r["dir"], dest_bk, symlinks=True)
                print(f"  backup   {r['dir']} -> {dest_bk}")
            if converter:
                params = inspect.signature(converter).parameters
                if "target_name" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
                    converter(r["src"], r["dir"], target_name=r["target"])
                else:
                    converter(r["src"], r["dir"])
                print(f"  CONVERT  {tag} via {conv_path.name}")
            else:
                skipped = 0
                for relname in r["src_files"]:
                    srcf, dst = r["src"] / relname, r["dir"] / relname
                    try:
                        if srcf.is_symlink():
                            raise UnsafeTarget("source file is a symlink")
                        if dst.is_file() and not dst.is_symlink() and file_digest(dst) == r["want"][relname]:
                            continue
                        safe_copy(srcf, r["dir"], relname, r["adapted"].get(relname))
                    except UnsafeTarget as exc:
                        skipped += 1
                        unsafe += 1
                        print(f"  SKIP-UNSAFE {tag}: {relname}: {exc}")
                print(f"  {action:<8} {tag} {detail}" + (f" ({skipped} file(s) skipped as unsafe)" if skipped else ""))
            if r["extra"]:
                print(f"           kept {len(r['extra'])} extra target file(s) (never deleted)")
        except Exception as exc:
            failures += 1
            print(f"  FAIL     {tag}: {type(exc).__name__}: {exc} (backup: {dest_bk if dest_bk else 'none was needed/made'})")
    if unsafe:
        print(f"[warn] {unsafe} unsafe target(s)/file(s) were skipped; nothing was written through a symlink or into the source dir")
    if invalid:
        print(f"[warn] {invalid} target(s) skipped: SKILL.md needs front matter with name and description")
    return 1 if (failures or (invalid and apply)) else 0


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- skills import
def skills_import(args) -> int:
    srcdir = Path(args.from_dir).expanduser()
    if not srcdir.is_dir():
        print(f"--from {srcdir} is not a directory")
        return 1
    try:
        rsrc, rdst = srcdir.resolve(), SRC_SKILLS.resolve()
    except (OSError, RuntimeError):
        print("cannot resolve paths")
        return 1
    if _inside(rsrc, rdst) or _inside(rdst, rsrc):
        print("--from must not be skills-src, inside it, or contain it")
        return 1
    if SRC_SKILLS.is_symlink():
        print(f"{rel(SRC_SKILLS)} is a symlink; refusing")
        return 1
    mod = skill_adapter()
    apply = args.apply
    print(("APPLY" if apply else "DRY-RUN (use --apply to write)") + f": import from {srcdir} into {rel(SRC_SKILLS)}")
    backup_root = ENGINE / "backups" / f"skills-import-{stamp()}"
    done = collisions = skipped = 0
    for d in sorted(srcdir.iterdir()):
        if not d.is_dir():
            continue
        if d.is_symlink():
            skipped += 1
            print(f"  SKIP     {d.name}: symlinked folder")
            continue
        if not (d / "SKILL.md").is_file():
            skipped += 1
            print(f"  SKIP     {d.name}: no SKILL.md")
            continue
        if not valid_skill_name(d.name):
            skipped += 1
            print(f"  SKIP     {d.name}: invalid skill name (letters, digits, . _ -; must start with letter/digit)")
            continue
        files = {k: v for k, v in tree_files(d).items() if not (d / k).is_symlink()}
        warn = ""
        if mod is not None:
            try:
                mod.adapt((d / "SKILL.md").read_bytes())
            except Exception as exc:
                warn = f" [warn: {exc}; push will skip it until fixed]"
        dest = SRC_SKILLS / d.name
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink() or not dest.is_dir():
                skipped += 1
                print(f"  SKIP     {d.name}: destination is a symlink/not a folder")
                continue
            have = tree_files(dest)
            if all(have.get(k) == v for k, v in files.items()):
                print(f"  SAME     {d.name}: already in skills-src")
                continue
            collisions += 1
            differing = [k for k, v in files.items() if have.get(k) != v]
            if not args.overwrite:
                print(f"  COLLISION {d.name}: differs in {', '.join(differing[:5])}; kept existing (use --overwrite to replace with backup)")
                continue
            action = "OVERWRITE"
        else:
            action = "NEW"
            differing = list(files)
        if not apply:
            print(f"  WOULD {action:<9} {d.name} ({len(differing)} file(s)){warn}")
            continue
        try:
            if action == "OVERWRITE":
                bk = backup_root / d.name
                shutil.copytree(dest, bk, symlinks=True)
                print(f"  backup   {dest} -> {bk}")
            for relname in differing:
                safe_copy(d / relname, dest, relname)
            done += 1
            print(f"  {action:<9} {d.name} ({len(differing)} file(s)){warn}")
        except Exception as exc:
            print(f"  FAIL     {d.name}: {type(exc).__name__}: {exc}")
            return 1
    print(f"summary: {done} written, {collisions} collision(s), {skipped} skipped" + ("" if apply else " (dry-run: nothing written)"))
    return 0


# ---------------------------------------------------------------- handoff / mcp-config
def _load_handoff():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import handoff as mod
    return mod


def cmd_handoff(args) -> int:
    try:
        hmod = _load_handoff()
    except Exception as exc:
        print(f"handoff needs handoff.py, infinite_context.py and redact.py next to suite.py ({type(exc).__name__})")
        return 1
    if args.list_topics:
        rows = hmod.list_topics(VAULT)
        if not rows:
            print("no topics yet (no 20-Checkpoints/<project> folders or 10-Projects notes)")
        for r in rows:
            print(f"{r['topic']}: checkpoint {r['checkpoint'] or '-'}; active facts {r.get('facts', 0)}"
                  + (f"; project note {r['project_note']}" if r["project_note"] else ""))
        return 0
    if not args.topic:
        print("--topic is required (or use --list-topics)")
        return 2
    try:
        text = hmod.build_pack(VAULT, args.topic, args.to, max(400, args.max_chars))
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    if not args.out:
        print(text)
        return 0
    name = args.out
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name) or ".." in name:
        print("--out must be a plain file name; it is written under .ai-memory/handoff/ only")
        return 1
    hdir = ENGINE / "handoff"
    if hdir.is_symlink():
        print("handoff folder is a symlink; refusing")
        return 1
    hdir.mkdir(parents=True, exist_ok=True)
    dst = hdir / name
    if dst.is_symlink() or (dst.exists() and not dst.is_file()):
        print("destination is a symlink or not a file; refusing")
        return 1
    if dst.exists():
        bk = ENGINE / "backups" / f"handoff-{stamp()}"
        bk.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, bk / name)
        print(f"backup   {dst} -> {bk / name}")
    fd, tmp = tempfile.mkstemp(prefix=".handoff-tmp-", dir=str(hdir))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(text.encode("utf-8"))
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"wrote {rel(dst)} ({len(text)} chars)")
    return 0


MCP_NAME = "infinity-memory"
PY_PH, VAULT_PH = "<PYTHON_EXE>", "<VAULT_DIR>"


def mcp_config_text(agent: str) -> str:
    script = f"{VAULT_PH}\\.ai-memory\\infinity_mcp.py"
    script_json = script.replace("\\", "\\\\")
    blocks = {
        "claude-code": (f"# Claude Code (run once in a terminal; fill the placeholders, e.g. {PY_PH} = C:\\\\Python312\\\\python.exe)\n"
                        f'claude mcp add --transport stdio --scope user {MCP_NAME} -- "{PY_PH}" "{script}"\n'
                        f"# verify: claude mcp list"),
        "codex": (f"# Codex (command)\ncodex mcp add {MCP_NAME} -- \"{PY_PH}\" \"{script}\"\n"
                  f"# or add to ~/.codex/config.toml (single quotes keep Windows backslashes literal)\n"
                  f"[mcp_servers.{MCP_NAME}]\ncommand = '{PY_PH}'\nargs = ['{script}']\nstartup_timeout_sec = 20"),
        "claude-desktop": (f"# Claude Desktop: merge into %APPDATA%\\Claude\\claude_desktop_config.json, then restart the app\n"
                           f'{{\n  "mcpServers": {{\n    "{MCP_NAME}": {{\n      "command": "{PY_PH}",\n      "args": ["{script_json}"]\n    }}\n  }}\n}}'),
        "cursor": (f"# Cursor: merge into %USERPROFILE%\\.cursor\\mcp.json (or <project>\\.cursor\\mcp.json)\n"
                   f'{{\n  "mcpServers": {{\n    "{MCP_NAME}": {{\n      "command": "{PY_PH}",\n      "args": ["{script_json}"]\n    }}\n  }}\n}}'),
    }
    keys = [agent] if agent else list(blocks)
    note = (f"# Placeholders to fill: {PY_PH} = full path of the Windows python.exe, {VAULT_PH} = Obsidian Vault folder.\n"
            "# Nothing is written by this command and no secrets are needed. Optional env: INFINITE_CTX_VAULT.")
    return note + "\n\n" + "\n\n".join(blocks[k] for k in keys)


def cmd_mcp_config(args) -> int:
    print(mcp_config_text(args.agent))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="suite.py", description="Memory suite orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="9 health checks; always exit 0")
    sub.add_parser("status", help="one-screen summary")
    sk = sub.add_parser("skills", help="skill sync")
    ssub = sk.add_subparsers(dest="skills_cmd", required=True)
    ssub.add_parser("init", help="write default suite-skills.json if absent")
    for nm in ("list", "diff", "push"):
        s = ssub.add_parser(nm)
        s.add_argument("--skill")
        s.add_argument("--target")
        if nm == "push":
            s.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
            s.add_argument("--create-roots", action="store_true", help="also create missing target skills folders")
    imp = ssub.add_parser("import", help="copy skill folders from a Codex-style skills dir into skills-src")
    imp.add_argument("--from", dest="from_dir", required=True)
    imp.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
    imp.add_argument("--overwrite", action="store_true", help="replace differing skills-src folders (after backup)")
    ho = sub.add_parser("handoff", help="universal context pack for continuing a topic in another agent")
    ho.add_argument("--topic")
    ho.add_argument("--to", default="generic", choices=["claude-code", "codex", "chatgpt", "claude-ai", "generic"])
    ho.add_argument("--max-chars", type=int, default=6000)
    ho.add_argument("--out", help="plain file name, written under .ai-memory/handoff/ only")
    ho.add_argument("--list-topics", action="store_true")
    mc = sub.add_parser("mcp-config", help="print (never write) the config to register infinity_mcp.py")
    mc.add_argument("--agent", choices=["claude-code", "codex", "claude-desktop", "cursor"])
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "skills":
        return cmd_skills(args)
    if args.cmd == "handoff":
        return cmd_handoff(args)
    if args.cmd == "mcp-config":
        return cmd_mcp_config(args)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
