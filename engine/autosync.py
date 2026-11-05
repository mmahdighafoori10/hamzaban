#!/usr/bin/env python3
"""autosync.py - automatic cross-agent sync for the memory suite (Python 3.10, stdlib only, no network).

  python autosync.py once [--bootstrap] [--dry-run]   one sync pass (run every minute by a scheduled task, pythonw)
  python autosync.py status                           print the heartbeat / queue / conflicts

What one pass does (never deletes anything, never crashes, exit code is always 0 for `once`):
  1. Skills fan-out: scans every skills root (suite-skills.json "targets" + skills-src). A skill that is NEW or CHANGED
     in exactly one root since the last pass is imported into skills-src and pushed (through skill_formats) to every
     other root. Changed in two roots with different content = CONFLICT: nothing is written, a note goes to 30-Pending.
     The first pass only records a baseline (nothing is propagated) unless --bootstrap is given. Each root is baselined
     the first time it is seen, so a folder that appears later cannot flood the others.
  2. Install queue (autosync-queue.jsonl): a write that fails (locked file, no permission) is queued and retried.
  3. Project instructions: for folders listed under "projects" in suite-skills.json, AGENTS.md (Codex) and CLAUDE.md
     (Claude Code) are kept in step: a missing one is created, two different real files are reported, never merged.
  4. autosync-notice.md (read by claude_hook.py at SessionStart), autosync-status.json (heartbeat),
     logs/autosync-errors.log, and a lock file so two passes never overlap.
Anything that is modified is first copied to backups/auto-<ts>/. Nothing is ever written outside the configured roots,
the listed project folders and this engine folder (+ 30-Pending notes). SQLite is never touched.

Test-only environment switch: AUTOSYNC_TEST_FAIL_ROOTS=codex,cos makes writes into those roots raise PermissionError.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import suite
except Exception as _exc:  # pragma: no cover - reported by main()
    suite = None
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _IMPORT_ERROR = ""

VERSION = 1
LOCK_STALE_SECONDS = 600
RUN_BUDGET_SECONDS = 90
# Chat On Steroids switches its WHOLE Skills library off ("supports at most 128 skills", seen in its app.log) once its
# skills folder holds more than 128, and cannot load a SKILL.md above 96,000 characters. Autosync must never be what
# pushes a root over such a limit (the numbers live in suite.py so the doctor uses the same ones).
ROOT_CAPS = getattr(suite, "ROOT_CAPS", {"cos": 128})
ROOT_MAX_CHARS = getattr(suite, "ROOT_MAX_CHARS", {"cos": 96_000})
# Bump when the canonical form used to compare copies changes: cached canon values are then recomputed once.
CANON_VERSION = 2
# One-time clean-ups of what older versions left behind (ids are recorded in state["migrations"]).
MIGRATION_FOOTER = "strip-cos-footer-2"
EVENT_KEEP = timedelta(days=7)
NOTICE_WINDOW = timedelta(hours=24)
MAX_SKILL_FILES = 3000
MAX_FILE_BYTES = 50_000_000
MAX_SKILL_BYTES = 150_000_000
INJECT_MAX = 600
COPY_HEADER_RE = re.compile(r"\A<!-- autosync: copy of CLAUDE\.md sha256=([0-9a-f]{12})[^\n]*-->\n\n")
POINTER_HEADER = "<!-- autosync: pointer to AGENTS.md (Codex instructions). Edit AGENTS.md; this file only imports it. -->\n@AGENTS.md\n"


# ---------------------------------------------------------------- small helpers
def engine() -> Path:
    return suite.ENGINE


def stamp_now() -> str:
    return suite.iso(suite.now())


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def log_error(message: str) -> None:
    try:
        logs = engine() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "autosync-errors.log"
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp_now()} {message}\n")
    except Exception:
        pass


def is_lock_error(exc: BaseException) -> bool:
    # "file is in use / no permission" style failures that are worth queueing and retrying
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        return getattr(exc, "winerror", None) in (5, 32, 33) or exc.errno in (13, 16, 1, 26, 30)  # EACCES EBUSY EPERM ETXTBSY EROFS
    return False


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def norm_text(text: str) -> str:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n") + "\n"


def safe_label(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:60] or "root"


class Lock:
    """Exclusive create of autosync.lock; a lock older than LOCK_STALE_SECONDS is considered abandoned."""

    def __init__(self) -> None:
        self.path = engine() / "autosync.lock"
        self.token = f"{os.getpid()}-{time.time_ns()}"
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "token": self.token, "ts": stamp_now()})
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < LOCK_STALE_SECONDS:
                return False
            try:  # abandoned: take over
                atomic_write(self.path, payload)
            except OSError:
                return False
            self.held = True
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        self.held = True
        return True

    def release(self) -> None:
        if not self.held:
            return
        try:
            data = read_json(self.path)
            if isinstance(data, dict) and data.get("token") == self.token:
                self.path.unlink()
        except OSError:
            pass
        self.held = False


class Root:
    def __init__(self, name: str, path: Path, kind: str) -> None:
        self.name, self.path, self.kind = name, path, kind
        self.exists = path.is_dir()
        self.readable = self.exists  # set False when listing fails
        self.label = safe_label(name)


# ---------------------------------------------------------------- the pass
class Run:
    def __init__(self, bootstrap: bool, dry: bool) -> None:
        self.bootstrap, self.dry = bootstrap, dry
        self.t0 = time.time()
        self.ts = stamp_now()
        self.cfg, self.cfg_note = suite.load_config()
        self.state, self.state_dirty = self.load_state()
        self.queue = self.load_queue()
        self.queue_dirty = False
        self.errors: list[str] = []
        self.skipped: list[dict] = []
        self.removed: list[dict] = []
        self.cap_added: dict[str, int] = {}   # new installs queued into capped roots during this pass
        self.fresh_roots: set[str] = set()    # roots first seen in this pass: baseline only, nothing is pushed into them yet
        self.plan: list[str] = []
        self.new_events: list[dict] = []
        self.counts = {"skills_seen": 0, "propagated": 0, "queued": 0, "conflicts": 0, "errors": 0, "roots_reachable": 0,
                       "projects_checked": 0, "deferred": 0}
        self.backup_dir: Path | None = None
        self.fail_roots = {x for x in os.environ.get("AUTOSYNC_TEST_FAIL_ROOTS", "").split(",") if x}
        self.settle = float(self.cfg.get("autosync_settle_seconds", 20))
        self.roots: list[Root] = []
        self.census: dict[str, dict[str, dict]] = {}
        self.strip_union = sorted({k for v in (self.cfg.get("strip_keys") or {}).values() for k in v})
        self.adapter = suite.skill_adapter()
        self.group_of: dict[str, set] = {}
        for grp in self.cfg.get("groups") or []:
            for member in grp:
                self.group_of.setdefault(member, set()).update(grp)
        self.partial = False
        self._converter = False  # False = not loaded yet

    # ---- state / queue persistence
    def load_state(self):
        fresh = {"version": VERSION, "baselined_roots": [], "baseline_done": False, "skills": {}, "events": [],
                 "conflicts": {}, "project_conflicts": {}}
        path = engine() / "autosync-state.json"
        if not path.is_file():
            return fresh, False
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
            log_error(f"{path.name} is corrupt; starting a new baseline (nothing will be propagated this pass)")
            try:
                shutil.copyfile(path, path.with_name(f"{path.name}.corrupt-{suite.stamp()}"))
            except OSError:
                pass
            return fresh, True
        for key, default in fresh.items():
            data.setdefault(key, default)
        return data, False

    def load_queue(self) -> list[dict]:
        path = engine() / "autosync-queue.jsonl"
        items = []
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict) and suite.valid_skill_name(item.get("skill")) and isinstance(item.get("root"), str):
                        items.append(item)
            except OSError:
                pass
        return items

    def save(self) -> None:
        if self.dry:
            return
        eng = engine()
        if self.state_dirty:
            self.prune_events()
            atomic_write(eng / "autosync-state.json", json.dumps(self.state, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
        if self.queue_dirty:
            atomic_write(eng / "autosync-queue.jsonl", "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in self.queue))

    def prune_events(self) -> None:
        cutoff = suite.now() - EVENT_KEEP
        keep = [e for e in self.state["events"] if (suite.parse_ts(e.get("ts")) or cutoff) >= cutoff]
        self.state["events"] = keep[-300:]

    # ---- reporting
    def error(self, message: str) -> None:
        self.errors.append(message[:300])
        log_error(message)

    def skip(self, root: str, skill: str, reason: str) -> None:
        item = {"root": root, "skill": skill, "reason": reason}
        if item not in self.skipped:
            self.skipped.append(item)

    def event(self, kind: str, **fields) -> None:
        rec = {"ts": self.ts, "kind": kind, **fields}
        self.new_events.append(rec)
        if not self.dry:
            self.state["events"].append(rec)
            self.state_dirty = True

    def backup_root(self) -> Path:
        if self.backup_dir is None:
            base = engine() / "backups"
            i = 0
            while True:
                cand = base / (f"auto-{suite.stamp()}" + (f"-{i}" if i else ""))
                try:
                    cand.mkdir(parents=True)
                    break
                except FileExistsError:
                    i += 1
            self.backup_dir = cand
        return self.backup_dir

    def backup_tree(self, label: str, name: str, path: Path) -> Path:
        dest = self.backup_root() / safe_label(label) / name
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{name}~{n}")
            n += 1
        shutil.copytree(path, dest, symlinks=True)
        return dest

    def backup_file(self, label: str, name: str, path: Path) -> Path:
        dest = self.backup_root() / safe_label(label) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{name}~{n}")
            n += 1
        shutil.copyfile(path, dest)
        return dest

    # ---- roots
    def discover_roots(self) -> None:
        src = suite.SRC_SKILLS
        try:
            src_real = src.resolve()
        except (OSError, RuntimeError):
            src_real = src
        self.roots = [Root("src", src, "src")]
        seen = {src_real}
        for tname, tpl in self.cfg["targets"].items():
            path = suite.resolve_target(tpl)
            try:
                real = path.resolve()
            except (OSError, RuntimeError):
                self.skip(tname, "*", "cannot resolve root path")
                continue
            if real in seen:
                continue
            if suite._inside(real, src_real) or suite._inside(src_real, real):
                self.skip(tname, "*", "root is skills-src (or contains/inside it): ignored")
                continue
            seen.add(real)
            self.roots.append(Root(tname, path, "target"))
        if self.cfg.get("create_roots") and not self.dry:
            for root in self.roots:
                if not root.exists and root.kind == "target":
                    try:
                        root.path.mkdir(parents=True, exist_ok=True)
                        root.exists = root.readable = True
                    except OSError as exc:
                        self.error(f"cannot create root {root.path}: {type(exc).__name__}")
        self.counts["roots_reachable"] = sum(1 for r in self.roots if r.exists)

    # ---- scanning
    def scan_skill(self, sdir: Path, prev: dict | None) -> dict:
        rec = {"state": "ok", "path": str(sdir)}
        skill_md = sdir / "SKILL.md"
        if skill_md.is_symlink():
            return dict(rec, state="invalid", reason="SKILL.md is a symlink")
        rels, sig_parts, newest, total = [], [], 0.0, 0
        try:
            for dirpath, dirnames, filenames in os.walk(sdir, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not os.path.islink(os.path.join(dirpath, d)))
                for fname in sorted(filenames):
                    full = Path(dirpath) / fname
                    if full.is_symlink() or fname.startswith(".suite-tmp-"):
                        continue
                    st = full.stat()
                    if st.st_size > MAX_FILE_BYTES:
                        return dict(rec, state="invalid", reason=f"{fname} is larger than {MAX_FILE_BYTES // 1_000_000} MB")
                    total += st.st_size
                    relname = full.relative_to(sdir).as_posix()
                    rels.append(relname)
                    sig_parts.append(f"{relname}|{st.st_size}|{st.st_mtime_ns}")
                    newest = max(newest, st.st_mtime)
                if len(rels) > MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
                    return dict(rec, state="invalid", reason="skill folder is too large")
        except OSError as exc:
            return dict(rec, state="unreadable", reason=f"{type(exc).__name__}")
        if "SKILL.md" not in rels:
            return dict(rec, state="invalid", reason="no SKILL.md")
        if self.settle and time.time() - newest < self.settle:
            return dict(rec, state="unsettled", reason="files changed a moment ago")
        sig = hashlib.sha1("\n".join(sig_parts).encode("utf-8")).hexdigest()
        rec.update(rels=rels, sig=sig, newest=newest)
        if prev and prev.get("sig") == sig and prev.get("hash") and prev.get("canon") and prev.get("cv") == CANON_VERSION:
            rec.update(hash=prev["hash"], canon=prev["canon"])
            return rec
        try:
            files = {r: suite.file_digest(sdir / r) for r in rels}
            md = (sdir / "SKILL.md").read_bytes()
        except OSError as exc:
            return dict(rec, state="unreadable", reason=f"{type(exc).__name__}")
        if self.adapter is not None:
            try:
                canon_md = self.adapter.adapt(md, None, self.strip_union)
            except self.adapter.SkillFormatError as exc:
                return dict(rec, state="invalid", reason=str(exc))
        else:
            canon_md = md
        canon_files = dict(files, **{"SKILL.md": hashlib.sha256(canon_md.replace(b"\r\n", b"\n")).hexdigest()})
        for r in rels:  # nested sub-skills are compared in the same canonical form
            if r.endswith("/SKILL.md"):
                try:
                    d = suite._canon_md_digest((sdir / r).read_bytes())
                except OSError:
                    d = None
                if d:
                    canon_files[r] = d
        rec.update(hash=suite.tree_hash(files), canon=suite.tree_hash(canon_files))
        return rec

    def scan_root(self, root: Root) -> dict | None:
        out: dict[str, dict] = {}
        try:
            entries = sorted(os.scandir(root.path), key=lambda e: e.name)
        except FileNotFoundError:
            root.exists = root.readable = False
            return None
        except OSError as exc:
            root.readable = False
            self.error(f"cannot list {root.path}: {type(exc).__name__}")
            return None
        for ent in entries:
            name = ent.name
            if name.startswith("."):
                continue  # .system and other plugin/system folders
            try:
                if ent.is_symlink():
                    if (Path(ent.path) / "SKILL.md").exists():
                        out[name] = {"state": "symlink"}
                        self.skip(root.name, name, "skill folder is a symlink")
                    continue
                if not ent.is_dir(follow_symlinks=False):
                    continue
                if not os.path.lexists(os.path.join(ent.path, "SKILL.md")):
                    continue
            except OSError:
                continue
            if not suite.valid_skill_name(name):
                self.skip(root.name, name, "invalid skill folder name")
                continue
            prev = self.state["skills"].get(name, {}).get("roots", {}).get(root.name)
            rec = self.scan_skill(Path(ent.path), prev)
            out[name] = rec
            if rec["state"] == "invalid":
                self.skip(root.name, name, rec.get("reason", "invalid"))
            elif rec["state"] in ("unsettled", "unreadable"):
                self.counts["deferred"] += 1
        return out

    @staticmethod
    def entry(rec: dict, ts: str) -> dict:
        return {"hash": rec["hash"], "canon": rec["canon"], "sig": rec["sig"], "seen": ts, "cv": CANON_VERSION}

    # ---- capped roots (the app turns its whole library off above a limit)
    def has_room(self, root: Root) -> bool:
        cap = ROOT_CAPS.get(root.name)
        if cap is None:
            return True
        held = sum(1 for r in self.census.get(root.name, {}).values() if r["state"] == "ok") + self.cap_added.get(root.name, 0)
        return held < cap

    def nested_of(self, root: Root) -> dict[str, str]:
        """{sub-skill: parent} for the skills a root carries as nested folders of other skills (cached for the pass)."""
        cache = self.__dict__.setdefault("_nested", {})
        if root.name not in cache:
            cache[root.name] = suite.nested_index(root.path)
        return cache[root.name]

    def too_big_for(self, root: Root, src_dir: Path) -> bool:
        limit = ROOT_MAX_CHARS.get(root.name)
        if not limit:
            return False
        try:
            return len((src_dir / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")) > limit
        except OSError:
            return False

    def clean_shared_footers(self) -> None:
        """One time: the footer Chat On Steroids appends to the skills it imports (its own bookkeeping, with private
        paths) had been imported INTO the shared source and from there into other agents' copies. Strip it everywhere
        except in COS itself, where it is the app's own."""
        done = self.state.setdefault("migrations", [])
        if MIGRATION_FOOTER in done or self.dry or self.adapter is None:
            return
        strip, cleaned, touched = self.adapter.strip_cos_import_footer, 0, 0
        for root in self.roots:
            if root.name == "cos" or not root.exists or not root.readable:
                continue
            for sdir in sorted(p for p in root.path.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")):
                dirty = []
                for f in sorted(sdir.rglob("SKILL.md")):
                    if f.is_symlink() or not f.is_file():
                        continue
                    try:
                        raw = f.read_bytes().decode("utf-8")
                    except (OSError, UnicodeError):
                        continue
                    new = strip(raw)
                    if new != raw:
                        dirty.append((f, new))
                if not dirty:
                    continue
                self.backup_tree(root.name, sdir.name, sdir)
                for f, new in dirty:
                    try:
                        atomic_write(f, new)
                        cleaned += 1
                    except OSError as exc:
                        self.error(f"cannot clean footer in {f}: {type(exc).__name__}")
                touched += 1
        done.append(MIGRATION_FOOTER)
        self.state_dirty = True
        self.event("migration", id=MIGRATION_FOOTER, files=cleaned, skills=touched)

    # ---- main skill pass
    def skills_pass(self) -> None:
        if self.adapter is None:
            self.error("skill_formats.py is missing or broken: skill sync skipped")
            return
        if self.state.get("baseline_done"):
            try:
                self.clean_shared_footers()
            except Exception as exc:
                self.error(f"footer clean-up: {type(exc).__name__}: {str(exc)[:200]}")
        for root in self.roots:
            if root.exists:
                scanned = self.scan_root(root)
                if scanned is not None:
                    self.census[root.name] = scanned
        scanned_roots = [r for r in self.roots if r.name in self.census]
        # per-root baseline: the first time a root is seen, only record what it holds
        baselined = set(self.state["baselined_roots"])
        for root in scanned_roots:
            if root.name in baselined:
                continue
            self.fresh_roots.add(root.name)
            if not self.bootstrap:
                for name, rec in self.census[root.name].items():
                    if rec["state"] == "ok":
                        sk = self.state["skills"].setdefault(name, {"roots": {}})
                        sk["roots"].setdefault(root.name, self.entry(rec, self.ts))
                self.plan.append(f"baseline root {root.name} ({sum(1 for r in self.census[root.name].values() if r['state'] == 'ok')} skill(s) recorded, none propagated)")
            if not self.dry:
                self.state["baselined_roots"].append(root.name)
            self.state_dirty = True
        if not self.dry and scanned_roots:
            self.state["baseline_done"] = True
        names = sorted({n for rs in self.census.values() for n, rec in rs.items() if rec["state"] == "ok"}
                       | set(self.state["skills"].keys()))
        self.counts["skills_seen"] = len({n for rs in self.census.values() for n, rec in rs.items() if rec["state"] == "ok"})
        for name in names:
            if time.time() - self.t0 > RUN_BUDGET_SECONDS:
                self.partial = True
                self.error("time budget used up; remaining skills continue next pass")
                break
            try:
                self.process_skill(name, scanned_roots)
            except Exception as exc:  # one bad skill must not stop the others
                self.error(f"skill {name}: {type(exc).__name__}: {str(exc)[:200]}")
        if not self.partial:
            try:
                self.heal_gaps(scanned_roots)
            except Exception as exc:
                self.error(f"heal gaps: {type(exc).__name__}: {str(exc)[:200]}")

    def is_subset_of_src(self, root: Root, name: str) -> bool:
        """True when every file of the copy in `root` also exists, with the same content, in the shared source (SKILL.md
        compared in its canonical form). Such a copy holds nothing of its own: updating it to the shared version loses
        nothing, so it does not need the 'a different copy was already here' protection."""
        tdir, sdir = root.path / name, suite.SRC_SKILLS / name

        def digests(base: Path) -> dict[str, str]:
            out = {}
            for rel in self.src_rels(base):
                out[rel] = (suite._canon_md_digest((base / rel).read_bytes()) or "?") if rel.endswith("SKILL.md") else suite.file_digest(base / rel)
            return out

        try:
            have, want = digests(tdir), digests(sdir)
        except OSError:
            return False
        return "SKILL.md" in have and all(want.get(rel) == dig for rel, dig in have.items())

    def heal_gaps(self, roots: list[Root]) -> None:
        """Shared means shared. process_skill only acts when something CHANGED, so a skill that could not be installed
        earlier (a locked folder, a full capped root) would never arrive, and an old copy would never catch up. Every
        pass, for each skill of skills-src and each agent that should have it:
          * missing  -> install it, unless the user removed it on purpose, the agent already has it (through a sibling
                        root, or nested inside another skill), it is too long for that app, or the root has no room;
          * stale    -> bring an old copy up to date when it is one autosync manages (or a strict subset of the shared
                        version, so nothing unique can be lost) and has not been edited since autosync last saw it.
        Both go through install_to_root, which backs the old copy up first."""
        if self.bootstrap or not self.state.get("baseline_done"):
            return  # the first look at the machine only records what is there
        variants = set(self.cfg.get("variants") or [])
        for name, srec in sorted(self.census.get("src", {}).items()):
            if srec.get("state") != "ok":
                continue
            if time.time() - self.t0 > RUN_BUDGET_SECONDS:
                self.partial = True
                return
            src_dir = suite.SRC_SKILLS / name
            man = self.state["skills"].get(name, {}).get("roots", {})
            for root in roots:
                if root.kind == "src" or not root.exists or not root.readable or root.name in self.fresh_roots:
                    continue
                have = self.census.get(root.name, {}).get(name)
                if have is None:
                    if name in self.nested_of(root):
                        continue  # delivered inside another skill's folder there
                    if any(name in self.census.get(o, {}) for o in self.group_of.get(root.name, set()) - {root.name}):
                        continue  # the same agent already has it in a sibling root
                    if man.get(root.name, {}).get("removed"):
                        self.skip(root.name, name, "skill was removed here earlier: not re-installed")
                        continue
                    if self.too_big_for(root, src_dir):
                        continue
                    if not self.has_room(root):
                        self.skip(root.name, name, f"{root.name} already holds its maximum of {ROOT_CAPS[root.name]} skills; one more would switch its whole library off")
                        continue
                    if self.dry:
                        self.plan.append(f"heal {name}: from src -> {root.name} (missing there)")
                        self.cap_added[root.name] = self.cap_added.get(root.name, 0) + 1
                        continue
                    status, _ = self.install_to_root(root, name, srec["canon"])
                    if status == "ok":
                        self.cap_added[root.name] = self.cap_added.get(root.name, 0) + 1
                        self.record_after(name, [root.name], srec["canon"])
                        self.mark_managed(name, root.name)
                        self.counts["propagated"] += 1
                        self.event("install", skill=name, source="src", to=[root.name], queued=[], healed=True)
                    continue
                # present: is it an old copy that can safely catch up?
                if have.get("state") != "ok" or have.get("canon") == srec["canon"] or name in variants or self.too_big_for(root, src_dir):
                    continue
                ent = man.get(root.name)
                if not isinstance(ent, dict) or ent.get("hash") != have.get("hash"):
                    continue  # unknown, or edited since autosync last saw it: never overwrite
                if not ent.get("autosync") and not self.is_subset_of_src(root, name):
                    continue
                if self.dry:
                    self.plan.append(f"update {name}: from src -> {root.name} (older copy, nothing unique in it)")
                    continue
                self.mark_managed(name, root.name)
                status, _ = self.install_to_root(root, name, srec["canon"])
                if status == "ok":
                    self.record_after(name, [root.name], srec["canon"])
                    self.mark_managed(name, root.name)
                    self.counts["propagated"] += 1
                    self.event("update", skill=name, source="src", to=[root.name], queued=[], healed=True)

    def mark_managed(self, name: str, root_name: str) -> None:
        ent = self.state["skills"].setdefault(name, {"roots": {}})["roots"].get(root_name)
        if isinstance(ent, dict) and not ent.get("autosync"):
            ent["autosync"] = True
            self.state_dirty = True

    def process_skill(self, name: str, roots: list[Root]) -> None:
        sk = self.state["skills"].setdefault(name, {"roots": {}})
        man: dict = sk["roots"]
        present: dict[str, dict] = {}
        deferred = False
        for root in roots:
            rec = self.census[root.name].get(name)
            if rec is None:
                continue
            if rec["state"] == "ok":
                present[root.name] = rec
            elif rec["state"] in ("unsettled", "unreadable"):
                deferred = True
        # removed somewhere: only reported, never propagated
        for root in roots:
            if name not in self.census[root.name] and root.name in man and not man[root.name].get("removed"):
                man[root.name]["removed"] = True
                self.state_dirty = True
                self.removed.append({"root": root.name, "skill": name})
                self.event("removed", skill=name, root=root.name)
        if deferred:
            return
        changed = [r for r, rec in present.items() if r not in man or man[r].get("hash") != rec["hash"]]
        for r, rec in present.items():
            if r in changed:
                continue
            ent = man[r]
            if ent.get("removed"):
                ent.pop("removed", None)
                self.state_dirty = True
            if ent.get("sig") != rec["sig"]:
                ent["sig"] = rec["sig"]
                self.state_dirty = True
            if ent.get("canon") != rec["canon"] or ent.get("cv") != CANON_VERSION:
                ent["canon"], ent["cv"] = rec["canon"], CANON_VERSION  # the comparison form changed; same files
                self.state_dirty = True
            srec = self.census.get("src", {}).get(name)
            if r != "src" and not ent.get("autosync") and srec and srec.get("state") == "ok" and rec["canon"] == srec["canon"]:
                # identical (canonical) content to the shared source: there is nothing of the user's to protect here
                ent["autosync"] = True
                self.state_dirty = True
        if not changed:
            if name in self.state["conflicts"]:
                self.resolve_conflict(name)
            return
        groups: dict[str, list[str]] = {}
        for r in changed:
            groups.setdefault(present[r]["canon"], []).append(r)
        if len(groups) > 1:
            self.conflict(name, changed, present)
            return
        members = next(iter(groups.values()))
        source = "src" if "src" in members else max(members, key=lambda r: present[r]["newest"])
        canon = present[source]["canon"]
        src_root = self.roots[0]
        was_known = bool(man)
        targets, synced = [], []
        chosen: set = set()
        for root in roots:
            if root.name == source:
                continue
            if root.kind != "src" and name not in self.census.get(root.name, {}) and name in self.nested_of(root):
                continue  # delivered inside another skill's folder there: no second, top-level copy
            if root.kind != "src" and self.too_big_for(root, suite.SRC_SKILLS / name):
                continue  # too long for that app's skill limit; it keeps its own short entry
            if root.kind != "src" and man.get(root.name, {}).get("removed"):
                self.skip(root.name, name, "skill was removed here earlier: not re-installed")
                continue
            sibs = self.group_of.get(root.name, set()) - {root.name}
            if any(o == source or o in chosen or present.get(o) is not None for o in sibs):
                continue  # same agent already has it via a sibling root: avoid a duplicate copy
            rec = present.get(root.name)
            other = self.census[root.name].get(name)
            if other and other["state"] in ("invalid", "symlink"):
                continue  # never touch a copy we cannot read safely
            if rec is not None and rec["canon"] == canon:
                synced.append(root.name)
                continue
            cap = ROOT_CAPS.get(root.name)
            if rec is None and cap is not None:  # a NEW skill in a capped root; updates do not change the count
                if not self.has_room(root):
                    self.skip(root.name, name, f"{root.name} already holds its maximum of {cap} skills; one more would switch its whole library off")
                    continue
                self.cap_added[root.name] = self.cap_added.get(root.name, 0) + 1
            targets.append(root)
            chosen.add(root.name)
        if self.dry:
            dests = [t.name for t in targets if t.kind != "src"]
            if source != "src" and "src" not in synced:
                dests.insert(0, "src")
            self.plan.append(("update " if was_known else "install ") + f"{name}: from {source} -> {', '.join(dests) or 'nobody'}")
            return
        src_dir = src_root.path / name
        if source != "src" and "src" not in synced:
            if not self.import_to_src(roots_by_name(roots)[source], name):
                return  # source stays 'changed' and is retried on the next pass
        done, queued = [], []
        for root in targets:
            if root.kind == "src":
                continue
            if not src_root.path.joinpath(name, "SKILL.md").is_file():
                break
            status, detail = self.install_to_root(root, name, canon)
            if status == "ok":
                done.append(root.name)
            elif status == "queued":
                queued.append(root.name)
            elif status in ("conflict", "unsafe"):
                pass
        # record what is now on disk
        self.record_after(name, [source, "src"] + synced + done, canon)
        for rn in done:
            ent = self.state["skills"][name]["roots"].get(rn)
            if isinstance(ent, dict):
                ent["autosync"] = True
        if name in self.state["conflicts"]:
            self.resolve_conflict(name)
        self.counts["propagated"] += 1
        self.event("update" if was_known else "install", skill=name, source=source,
                   to=sorted(set(done + (["src"] if source != "src" and "src" not in synced else []))), queued=queued)
        self.state_dirty = True

    def record_after(self, name: str, root_names: list[str], canon: str) -> None:
        man = self.state["skills"][name]["roots"]
        by_name = roots_by_name(self.roots)
        for rn in dict.fromkeys(root_names):
            root = by_name.get(rn)
            if root is None:
                continue
            root.exists = root.path.is_dir()  # skills-src may have just been created by the import
            if not root.exists:
                continue
            tdir = root.path / name
            if not tdir.is_dir():
                continue
            rec = self.scan_skill_now(tdir)
            if rec["state"] == "ok":
                man[rn] = self.entry(rec, self.ts)
                self.census.setdefault(rn, {})[name] = rec
                if rn not in self.state["baselined_roots"]:  # we created/filled it ourselves: it is known from now on
                    self.state["baselined_roots"].append(rn)
        self.state_dirty = True

    def scan_skill_now(self, tdir: Path) -> dict:
        old = self.settle
        self.settle = 0  # we just wrote it ourselves
        try:
            return self.scan_skill(tdir, None)
        finally:
            self.settle = old

    # ---- writing into skills-src and into the other roots
    def import_to_src(self, root: Root, name: str) -> bool:
        sdir, ddir = root.path / name, suite.SRC_SKILLS / name
        try:
            if suite.SRC_SKILLS.is_symlink() or ddir.is_symlink():
                self.error(f"skills-src or {name} is a symlink: refusing to write")
                return False
            if ddir.is_dir():
                self.backup_tree("src", name, ddir)
            rels = [r for r in (self.census[root.name][name].get("rels") or [])]
            for relname in rels:
                srcf, dst = sdir / relname, ddir / relname
                if srcf.is_symlink():
                    continue
                data = None
                if root.name == "cos" and relname.endswith("SKILL.md") and self.adapter is not None:
                    # the app's own footer (private paths) must not travel into the shared, agent-neutral source
                    try:
                        raw = srcf.read_bytes().decode("utf-8")
                        cleaned = self.adapter.strip_cos_import_footer(raw)
                        data = cleaned.encode("utf-8") if cleaned != raw else None
                    except (OSError, UnicodeError):
                        data = None
                want = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() if data is not None else suite.file_digest(srcf)
                if dst.is_file() and not dst.is_symlink() and suite.file_digest(dst) == want:
                    continue
                suite.safe_copy(srcf, ddir, relname, data)
            return True
        except Exception as exc:
            self.error(f"import {name} from {root.name} into skills-src failed: {type(exc).__name__}: {str(exc)[:160]}")
            return False

    def converter(self):
        if self._converter is False:
            self._converter = None
            path = suite.find_converter(self.cfg)
            if path:
                try:
                    spec = importlib.util.spec_from_file_location("skill_convert_plugin", path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    fn = getattr(mod, "convert", None)
                    self._converter = fn if callable(fn) else None
                except Exception as exc:
                    self.error(f"cannot load converter {path.name}: {type(exc).__name__}")
        return self._converter

    def install_to_root(self, root: Root, name: str, canon: str) -> tuple[str, str]:
        tdir, src_dir = root.path / name, suite.SRC_SKILLS / name
        man = self.state["skills"].setdefault(name, {"roots": {}})["roots"]
        try:
            if root.name in self.fail_roots:
                raise PermissionError(13, "simulated locked folder (AUTOSYNC_TEST_FAIL_ROOTS)")
            if self.too_big_for(root, src_dir):
                self.skip(root.name, name, f"SKILL.md is longer than the {ROOT_MAX_CHARS[root.name]} characters {root.name} can load; its own short entry stays")
                return "unsafe", "too long for this app"
            why = suite.unsafe_target_reason({"root": root.path, "dir": tdir})
            if why:
                self.skip(root.name, name, why)
                return "unsafe", why
            if not root.path.is_dir():
                return "unreachable", ""
            if tdir.is_dir():
                cur = self.scan_skill_now(tdir)
                if cur["state"] in ("invalid", "unreadable"):
                    self.skip(root.name, name, f"existing copy is not usable ({cur.get('reason', cur['state'])}): left alone")
                    return "unsafe", "existing copy not usable"
                ent = man.get(root.name)
                if cur["canon"] == canon:
                    return "ok", "already same"
                if ent is None or ent.get("hash") != cur["hash"]:
                    self.conflict(name, [root.name, "src"], {root.name: cur, "src": self.scan_skill_now(src_dir)})
                    return "conflict", "target changed locally"
                if self.cfg.get("protect_existing") and not ent.get("autosync"):
                    self.skip(root.name, name, "a different copy was already here (not written by autosync): left alone")
                    return "unsafe", "protected existing copy"
                self.backup_tree(root.name, name, tdir)
            conv = self.converter()
            if conv:
                params = inspect.signature(conv).parameters
                if "target_name" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
                    conv(src_dir, tdir, target_name=root.name)
                else:
                    conv(src_dir, tdir)
            else:
                adapted, bad = suite.adapted_skill_md(self.cfg, src_dir, root.name)
                if bad:
                    self.skip(root.name, name, f"SKILL.md not accepted: {bad}")
                    return "unsafe", bad
                for relname in self.src_rels(src_dir):
                    srcf, dst = src_dir / relname, tdir / relname
                    data = adapted if (adapted is not None and relname == "SKILL.md") else None
                    want = (hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() if data is not None else suite.file_digest(srcf))
                    if dst.is_file() and not dst.is_symlink() and suite.file_digest(dst) == want:
                        continue
                    suite.safe_copy(srcf, tdir, relname, data)
            self.dequeue(name, root.name)
            return "ok", ""
        except suite.UnsafeTarget as exc:
            self.skip(root.name, name, f"unsafe: {exc}")
            return "unsafe", str(exc)
        except Exception as exc:
            self.enqueue(name, root.name, f"{type(exc).__name__}: {str(exc)[:160]}", locked=is_lock_error(exc))
            return "queued", str(exc)

    @staticmethod
    def src_rels(src_dir: Path) -> list[str]:
        rels = []
        for dirpath, dirnames, filenames in os.walk(src_dir, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not os.path.islink(os.path.join(dirpath, d)))
            for fname in sorted(filenames):
                full = Path(dirpath) / fname
                if not full.is_symlink() and not fname.startswith(".suite-tmp-"):
                    rels.append(full.relative_to(src_dir).as_posix())
        return rels

    # ---- queue
    def enqueue(self, name: str, root_name: str, err: str, locked: bool) -> None:
        self.queue_dirty = True
        for item in self.queue:
            if item["skill"] == name and item["root"] == root_name:
                item.update(attempts=int(item.get("attempts", 0)) + 1, last_try=self.ts, last_error=err)
                return
        self.queue.append({"skill": name, "root": root_name, "queued_at": self.ts, "attempts": 1, "last_try": self.ts,
                           "last_error": err, "locked": locked})
        self.counts["queued"] += 1
        self.event("queued", skill=name, root=root_name, error=err[:100])
        log_error(f"queued {name} -> {root_name}: {err}")

    def dequeue(self, name: str, root_name: str) -> None:
        before = len(self.queue)
        self.queue = [q for q in self.queue if not (q["skill"] == name and q["root"] == root_name)]
        if len(self.queue) != before:
            self.queue_dirty = True

    def process_queue(self) -> None:
        by_name = roots_by_name(self.roots)
        for item in list(self.queue):
            name, rn = item["skill"], item["root"]
            last = suite.parse_ts(item.get("last_try"))
            if int(item.get("attempts", 0)) >= 10 and last and suite.now() - last < timedelta(minutes=30):
                continue  # backoff for items that keep failing
            root = by_name.get(rn)
            src_dir = suite.SRC_SKILLS / name
            srec = self.scan_skill_now(src_dir) if src_dir.is_dir() else {"state": "missing"}
            if root is None or srec["state"] != "ok":
                self.dequeue(name, rn)
                log_error(f"queue item {name}->{rn} dropped ({'root no longer configured' if root is None else 'source gone/invalid'})")
                continue
            if not root.exists:
                continue
            if self.dry:
                self.plan.append(f"retry queued install {name} -> {rn}")
                continue
            status, _ = self.install_to_root(root, name, srec["canon"])
            if status == "ok":
                self.record_after(name, [rn], srec["canon"])
                self.event("installed-late", skill=name, root=rn)
                self.dequeue(name, rn)
            elif status in ("conflict", "unsafe"):
                self.dequeue(name, rn)

    # ---- conflicts
    def conflict(self, name: str, roots: list[str], recs: dict[str, dict]) -> None:
        info = {r: {"path": recs[r].get("path", ""), "hash": recs[r].get("hash", "")} for r in roots}
        old = self.state["conflicts"].get(name)
        if self.dry:
            self.plan.append(f"CONFLICT {name}: changed differently in {', '.join(sorted(roots))}; nothing would be written")
            return
        if old and old.get("roots") == info:
            return
        self.state["conflicts"][name] = {"since": old["since"] if old else self.ts, "roots": info}
        self.state_dirty = True
        self.event("conflict", skill=name, roots=sorted(info))
        self.write_note(f"skill-conflict-{name}.md", conflict_note(name, info, self.ts))

    def resolve_conflict(self, name: str) -> None:
        if self.dry:
            return
        self.state["conflicts"].pop(name, None)
        self.state_dirty = True
        self.event("conflict-resolved", skill=name)

    def write_note(self, filename: str, text: str) -> None:
        try:
            path = suite.PENDING / filename
            if path.exists():
                old = path.read_text(encoding="utf-8", errors="replace")
                strip = lambda t: re.sub(r"(?m)^updated: .*$", "", t)
                if strip(old) == strip(text):
                    return
                if "source: autosync" not in old:
                    self.error(f"{filename} exists and is not an autosync note: not overwritten")
                    return
                self.backup_file("30-Pending", filename, path)
            atomic_write(path, text)
        except Exception as exc:
            self.error(f"cannot write note {filename}: {type(exc).__name__}: {str(exc)[:120]}")

    # ---- project instruction mirroring
    def mirror_projects(self) -> None:
        for raw in self.cfg.get("projects") or []:
            try:
                self.mirror_one(raw)
            except Exception as exc:
                self.error(f"project {str(raw)[:80]}: {type(exc).__name__}: {str(exc)[:160]}")

    def mirror_one(self, raw: str) -> None:
        pdir = suite.resolve_target(raw)
        if pdir.is_symlink() or not pdir.is_dir():
            self.skip("project", str(pdir), "folder missing or a symlink")
            return
        if suite._inside(pdir.resolve(), suite.ENGINE.resolve()):
            self.skip("project", str(pdir), "inside the engine folder")
            return
        self.counts["projects_checked"] += 1
        agents, claude = pdir / "AGENTS.md", pdir / "CLAUDE.md"
        for p in (agents, claude):
            if p.is_symlink() or (p.exists() and not p.is_file()):
                self.skip("project", str(p), "not a regular file")
                return
        has_a, has_c = agents.is_file(), claude.is_file()
        if not has_a and not has_c:
            return
        key = str(pdir)
        try:
            if has_a and not has_c:
                self.create_file(claude, POINTER_HEADER, "project-pointer", key)
                return
            if has_c and not has_a:
                body = norm_text(claude.read_text(encoding="utf-8-sig"))
                header = (f"<!-- autosync: copy of CLAUDE.md sha256={sha12(body)} ; refreshed automatically while you only edit CLAUDE.md. "
                          "If you edit this file by hand, autosync stops and reports a conflict. -->\n\n")
                self.create_file(agents, header + body, "project-copy", key)
                return
            ta, tc = agents.read_text(encoding="utf-8-sig"), claude.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            self.skip("project", key, "AGENTS.md or CLAUDE.md is not valid UTF-8")
            return
        if any(line.strip() == "@AGENTS.md" for line in tc.splitlines()):
            return self.project_ok(key)
        m = COPY_HEADER_RE.match(ta)
        if m:
            body = ta[m.end():]
            untouched = sha12(norm_text(body)) == m.group(1)
            if untouched and sha12(norm_text(tc)) == m.group(1):
                return self.project_ok(key)
            if untouched:  # our own generated copy, never edited: refresh it from CLAUDE.md
                new_body = norm_text(tc)
                header = (f"<!-- autosync: copy of CLAUDE.md sha256={sha12(new_body)} ; refreshed automatically while you only edit CLAUDE.md. "
                          "If you edit this file by hand, autosync stops and reports a conflict. -->\n\n")
                if self.dry:
                    self.plan.append(f"refresh {agents} from CLAUDE.md")
                else:
                    self.backup_file(f"projects/{safe_label(pdir.name)}", "AGENTS.md", agents)
                    atomic_write(agents, header + new_body)
                    self.event("project-refresh", path=key)
                return self.project_ok(key)
            return self.project_conflict(pdir, "AGENTS.md is a generated copy that was edited by hand, and CLAUDE.md is different")
        if norm_text(ta) == norm_text(tc):
            return self.project_ok(key)
        self.project_conflict(pdir, "AGENTS.md and CLAUDE.md are both real files and differ")

    def create_file(self, path: Path, text: str, kind: str, key: str) -> None:
        if self.dry:
            self.plan.append(f"create {path}")
            return
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except FileExistsError:
            return
        self.event(kind, path=str(path))
        self.project_ok(key)

    def project_ok(self, key: str) -> None:
        if key in self.state["project_conflicts"] and not self.dry:
            self.state["project_conflicts"].pop(key)
            self.state_dirty = True
            self.event("project-conflict-resolved", path=key)

    def project_conflict(self, pdir: Path, why: str) -> None:
        key = str(pdir)
        if self.dry:
            self.plan.append(f"PROJECT CONFLICT {key}: {why}")
            return
        if key in self.state["project_conflicts"]:
            return
        self.state["project_conflicts"][key] = {"since": self.ts, "why": why}
        self.state_dirty = True
        self.event("project-conflict", path=key)
        slug = f"{safe_label(pdir.name)}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:6]}"
        self.write_note(f"project-conflict-{slug}.md", project_note(key, why, self.ts))

    # ---- notice, status, orchestration
    def notice_text(self) -> str:
        now_dt = suite.now()
        cutoff = now_dt - NOTICE_WINDOW
        updated: list[str] = []
        lines, latest = [], None
        for e in self.state["events"]:
            ts = suite.parse_ts(e.get("ts"))
            if ts is None or ts < cutoff:
                continue
            if e.get("kind") in ("install", "update", "installed-late"):
                if e.get("skill") in updated:
                    updated.remove(e["skill"])
                updated.append(e["skill"])
                latest = max(latest, ts) if latest else ts
                lines.append(f"- {e['ts']} {e['kind']} {e.get('skill')}" + (f" from {e['source']}" if e.get("source") else "")
                             + (f" to {', '.join(e.get('to') or [])}" if e.get("to") else "")
                             + (f" (queued: {', '.join(e['queued'])})" if e.get("queued") else ""))
            elif e.get("kind") in ("conflict", "project-conflict"):
                lines.append(f"- {e['ts']} {e['kind']} {e.get('skill') or e.get('path')}")
        conflicts = sorted(self.state["conflicts"]) + [Path(k).name for k in self.state["project_conflicts"]]
        for c in self.state["conflicts"].values():
            ts = suite.parse_ts(c.get("since"))
            if ts:
                latest = max(latest, ts) if latest else ts
        for c in self.state["project_conflicts"].values():
            ts = suite.parse_ts(c.get("since"))
            if ts:
                latest = max(latest, ts) if latest else ts
        parts = []
        if updated:
            shown = ", ".join(updated[-8:]) + (f" (+{len(updated) - 8} more)" if len(updated) > 8 else "")
            parts.append(f"skills updated: {shown} — open a new chat to load them")
        if conflicts:
            parts.append(f"conflicts waiting for the user: {', '.join(conflicts[:5])} (see 30-Pending)")
        inject = ". ".join(parts)[:INJECT_MAX]
        generated = suite.iso(latest) if latest else ""
        body = "---\nkind: autosync-notice\n"
        body += f"generated: {generated}\n---\n" if inject else "generated:\n---\n"
        body += (f"inject: {inject}\n" if inject else "nothing new in the last 24 hours\n")
        if lines:
            body += "\n## Last 24h\n" + "\n".join(lines[-20:]) + "\n"
        return body

    def write_notice(self) -> None:
        if self.dry:
            return
        path = engine() / "autosync-notice.md"
        text = self.notice_text()
        try:
            if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
                return
            atomic_write(path, text)
        except OSError as exc:
            self.error(f"cannot write autosync-notice.md: {type(exc).__name__}")

    def write_status(self) -> None:
        self.counts["errors"] = len(self.errors)
        self.counts["conflicts"] = len(self.state["conflicts"]) + len(self.state["project_conflicts"])
        data = {
            "version": VERSION, "last_run": self.ts, "duration_ms": int((time.time() - self.t0) * 1000),
            "ok": not self.errors, "partial": self.partial, "baseline_done": bool(self.state.get("baseline_done")),
            "bootstrap": self.bootstrap, "counts": self.counts,
            "roots": [{"name": r.name, "path": str(r.path), "exists": r.exists,
                       "skills": sum(1 for rec in self.census.get(r.name, {}).values() if rec["state"] == "ok")} for r in self.roots],
            "conflicts": sorted(self.state["conflicts"]) + [Path(k).name for k in self.state["project_conflicts"]],
            "queue_len": len(self.queue), "skipped": self.skipped[:20], "removed": self.removed[:20],
            "errors": self.errors[:10],
        }
        if self.cfg_note:
            data["config_note"] = self.cfg_note
        try:
            atomic_write(engine() / "autosync-status.json", json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        except OSError as exc:
            log_error(f"cannot write status: {type(exc).__name__}")

    def go(self) -> None:
        try:
            self.discover_roots()
            self.process_queue()
            self.skills_pass()
            self.mirror_projects()
        except Exception as exc:
            self.error(f"pass failed: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            if not self.dry:
                for step in (self.write_notice, self.save, self.write_status):
                    try:
                        step()
                    except Exception as exc:
                        log_error(f"{step.__name__} failed: {type(exc).__name__}: {str(exc)[:160]}")


# ---------------------------------------------------------------- module level helpers / notes
def roots_by_name(roots) -> dict:
    return {r.name: r for r in roots}


def conflict_note(name: str, info: dict, ts: str) -> str:
    rows = "\n".join(f"- {r}: `{v['path']}` (version {v['hash']})" for r, v in sorted(info.items()))
    return (f"---\ntitle: Skill conflict - {name}\nstatus: needs-user\nsource: autosync\nupdated: {ts}\n---\n\n"
            f"# تداخل در مهارت «{name}»\n\n"
            "این مهارت از آخرین همگام‌سازی، در بیش از یک جا و به شکل متفاوت تغییر کرده است. "
            "برای اینکه چیزی از دست نرود، هیچ‌کدام را بازنویسی نکردم و منتظر تصمیم تو هستم.\n\n"
            f"{rows}\n\n"
            "## چه کنم؟\n"
            "۱. نسخهٔ درست را انتخاب کن.\n"
            "۲. پوشهٔ همان نسخه را روی پوشهٔ همین مهارت در جاهای دیگر کپی کن تا همه یکی شوند.\n"
            "۳. دقیقه‌ای بعد همگام‌سازی خودکار خودش ادامه می‌دهد. بعد از آن می‌توانی این یادداشت را پاک کنی.\n\n"
            f"English: skill `{name}` changed differently in more than one place since the last sync. "
            "Autosync wrote nothing. Make the copies identical (copy the one you want over the others) and syncing resumes by itself.\n")


def project_note(path: str, why: str, ts: str) -> str:
    return (f"---\ntitle: Project instructions conflict\nstatus: needs-user\nsource: autosync\nupdated: {ts}\n---\n\n"
            "# تداخل در دستورهای پروژه\n\n"
            f"پروژه: `{path}`\n\n"
            "فایل‌های `AGENTS.md` (برای Codex) و `CLAUDE.md` (برای Claude Code) با هم فرق دارند و هیچ‌کدام را عوض نکردم.\n\n"
            "ساده‌ترین راه: متن اصلی را در `AGENTS.md` نگه دار و در `CLAUDE.md` فقط همین یک خط را بنویس: `@AGENTS.md`\n\n"
            f"English: {why}. Nothing was changed.\n")


def print_status() -> int:
    eng = engine()
    data = read_json(eng / "autosync-status.json")
    if not isinstance(data, dict):
        print("autosync: never ran (no autosync-status.json). Install the scheduled task with INSTALL-AUTOSYNC.cmd on Windows.")
        return 0
    ts = suite.parse_ts(data.get("last_run"))
    age = suite.fmt_age(suite.age_of(ts)) if ts else "?"
    c = data.get("counts") or {}
    print(f"autosync: last run {data.get('last_run')} ({age} ago), ok={data.get('ok')}, baseline_done={data.get('baseline_done')}")
    print(f"  skills seen {c.get('skills_seen')}, propagated {c.get('propagated')}, queued {c.get('queued')}, "
          f"conflicts {c.get('conflicts')}, errors {c.get('errors')}, projects {c.get('projects_checked')}")
    for r in data.get("roots") or []:
        print(f"  root {r.get('name'):<7} {'ok     ' if r.get('exists') else 'MISSING'} {r.get('skills')} skill(s)  {r.get('path')}")
    for name in data.get("conflicts") or []:
        print(f"  CONFLICT {name} (see 30-Pending)")
    print(f"  queue: {data.get('queue_len')} item(s)")
    for e in (data.get("errors") or [])[:3]:
        print(f"  error: {e}")
    return 0


def run_pass(bootstrap: bool, dry: bool) -> int:
    if suite is None:
        print(f"autosync: cannot import suite.py ({_IMPORT_ERROR})")
        return 0
    lock = Lock()
    if not dry and not lock.acquire():
        print("autosync: another pass is running (lock held); skipped")
        return 0
    try:
        run = Run(bootstrap, dry)
        run.go()
        label = "DRY-RUN (nothing written)" if dry else "pass finished"
        roots_txt = ", ".join(r.name + "=" + ("yes" if r.exists else "no") for r in run.roots)
        print(f"autosync {label}: roots {roots_txt}")
        print(f"  skills seen {run.counts['skills_seen']}, propagated {run.counts['propagated']}, queued {run.counts['queued']}, "
              f"conflicts {len(run.state['conflicts']) + len(run.state['project_conflicts'])}, errors {len(run.errors)}, "
              f"deferred {run.counts['deferred']}")
        for line in run.plan:
            print(f"  plan: {line}")
        for e in run.new_events:
            print(f"  event: {e['kind']} {e.get('skill') or e.get('path') or ''}")
        for s in run.skipped[:15]:
            print(f"  skipped: {s['root']}/{s['skill']}: {s['reason']}")
        for e in run.errors[:5]:
            print(f"  error: {e}")
        if not dry:
            try:  # every chat of every agent: a stable tag, topic sub-tags, cut-off detection (chats.py)
                import chats
                stats = chats.Registry().scan(budget=20.0)
                atomic_write(engine() / "chats-status.json", json.dumps(dict(stats, last_run=stamp_now()), ensure_ascii=False, indent=1) + "\n")
                if stats.get("errors"):
                    log_error("chats: " + "; ".join(stats["errors"][:3]))
            except Exception as exc:  # cosmetic for the sync itself: never fail it because of the chat registry
                log_error(f"chats: {type(exc).__name__}: {str(exc)[:200]}")
            try:  # what the agents keep in their own memory -> proposals in the shared notebook (native_memory.py)
                import native_memory
                nm = native_memory.scan()
                if nm.get("errors"):
                    log_error("native memory: " + "; ".join(nm["errors"][:3]))
            except Exception as exc:
                log_error(f"native memory: {type(exc).__name__}: {str(exc)[:200]}")
            try:
                import dashboard
                dashboard.write()
            except Exception as exc:  # the dashboard is cosmetic: never fail the sync because of it
                log_error(f"dashboard: {type(exc).__name__}: {str(exc)[:200]}")
            try:
                from hamzaban import outbox  # optional: ready-made packs for every agent
                outbox.refresh_quiet()
            except ImportError:
                pass
            except Exception as exc:
                log_error(f"hamzaban: {type(exc).__name__}: {str(exc)[:200]}")
    except Exception as exc:
        log_error(f"unexpected: {type(exc).__name__}: {str(exc)[:200]}")
        print(f"autosync: unexpected error logged ({type(exc).__name__})")
    finally:
        lock.release()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autosync.py", description="Automatic cross-agent skill/instruction sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    once = sub.add_parser("once", help="one sync pass (for the scheduled task)")
    once.add_argument("--bootstrap", action="store_true", help="on the first sight of a root, propagate its skills instead of only recording them")
    once.add_argument("--dry-run", action="store_true", help="compute and print what would happen; write nothing at all")
    sub.add_parser("status", help="print the last heartbeat")
    args = ap.parse_args(argv)
    try:
        if suite is None:
            print(f"autosync: cannot import suite.py ({_IMPORT_ERROR})")
            return 0
        return print_status() if args.cmd == "status" else run_pass(args.bootstrap, args.dry_run)
    except Exception as exc:
        log_error(f"fatal: {type(exc).__name__}: {str(exc)[:200]}")
        return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    sys.exit(main())
