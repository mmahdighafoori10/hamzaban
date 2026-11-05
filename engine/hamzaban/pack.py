"""The canonical pack: the one neutral format every adapter reads (the "single language").

build(vault, ...)   read-only; collects user rules, harness, project state and the last turns
validate(pack)      list of problems ([] means valid)
sanitize(pack)      re-redact and re-limit a pack that came from outside (a file, another machine)
digest(pack)        stable id; ignores the generated time so identical content has an identical id

Everything that goes into a pack is redacted (handoff.scrub). Private facts never enter it.
Nothing here writes to the Vault.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import CLOSE_MARKER, CLOSE_VERSION, PACK_FORMAT, PACK_VERSION, VERSION

import handoff  # engine module (path is set up in hamzaban/__init__.py)

SID = re.compile(r"^[A-Za-z0-9_-]{6,90}$")
HARNESS_REL = "00-Context/Harness.md"
PREFS_REL = "00-Context/Work-Preferences.md"

LIMITS = {"rules": 30, "prefs": 12, "skills": 80, "facts": 20, "tasks": 12, "recent": 6, "projects": 8}

_HEAD = re.compile(r"(?m)^#{1,4}[ \t]+(.*?)[ \t]*$")
_BULLET = re.compile(r"^[ \t]*(?:[-*•]|\d{1,2}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?(.+?)[ \t]*$")
_EMPTY = {"ثبت نشده", "none", "n/a", "-", "موردی نبود", "تعریف نشده"}
_TURN = re.compile(r"(?m)^###[ \t]+(user|assistant|tool)[ \t]+[—–-][ \t]+(.*)$")
_MARK = re.compile(re.escape(CLOSE_MARKER), re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CLOSE_FENCE = re.compile(r"(?ims)^[ \t]*(`{3,}|~{3,})[ \t]*" + re.escape(CLOSE_MARKER).replace("\\-", "[-_]")
                          + r"[ \t]*\r?\n.*?\r?\n[ \t]*\1[ \t]*$")
_PENDING_HEAD = re.compile(r"30-Pending|در انتظار تأیید", re.IGNORECASE)
EXPORT_MARK = "hamzaban-export:v1"  # written at the top of files `export` puts into a project folder

TOOLS = [
    {"name": "memory_handoff", "description": "Get this pack in your own dialect. Arguments: target, optional topic."},
    {"name": "memory_context", "description": "Latest checkpoint, active facts and open tasks for a topic."},
    {"name": "memory_search", "description": "Full-text search of the vault (Persian-aware)."},
    {"name": "memory_read", "description": "Read one note by vault-relative path."},
    {"name": "memory_propose", "description": "Propose a fact for the user to approve; it never becomes a fact by itself."},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- small text helpers
def neutralize(text: str) -> str:
    """Untrusted text must never be able to fake the closing marker."""
    return _MARK.sub(CLOSE_MARKER.replace("-", "_"), text)


def line(text, limit: int) -> str:
    return neutralize(handoff.one_line(text, limit))


def block(text, limit: int) -> str:
    return neutralize(handoff.clip(handoff.scrub(str(text)), limit))


def sections(body: str) -> list[tuple[str, str]]:
    parts = _HEAD.split(body)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def bullets(text: str, limit: int = 40) -> list[str]:
    out = []
    for raw in text.splitlines():
        m = _BULLET.match(raw)
        if not m:
            continue
        item = m.group(1).strip()
        if item in _EMPTY or item.lower() in _EMPTY:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def languages_of(*texts: str) -> list[str]:
    joined = " ".join(t for t in texts if t)
    langs = []
    if re.search(r"[؀-ۿ]", joined):
        langs.append("fa")
    if re.search(r"[A-Za-z]{3,}", joined):
        langs.append("en")
    return langs or ["en"]


# ---------------------------------------------------------------- safe reads
def read_note(vault: Path, rel: str, max_bytes: int = 60000):
    """(meta, body) of a plain, non-symlinked note under the vault, or None."""
    cur = Path(vault)
    for part in rel.split("/"):
        cur = cur / part
        try:
            if cur.is_symlink():
                return None
        except OSError:
            return None
    text = handoff.read_plain(cur, max_bytes)
    if text is None:
        return None
    return handoff.parse_note(text)


def agent_from_source(source: str) -> str:
    s = (source or "").lower()
    if s.startswith("hamzaban-close:"):
        name = re.sub(r"[^a-z0-9._-]", "", s.split(":", 1)[1])[:40]
        return name or "unknown"
    if "claude-code" in s:
        return "claude-code"
    if "cos" in s or "chat-on-steroids" in s:
        return "chat-on-steroids"
    if "codex" in s:
        return "codex"
    return "unknown"


# ---------------------------------------------------------------- user rules / harness
def read_preferences(vault: Path) -> list[dict]:
    got = read_note(vault, PREFS_REL, 20000)
    if not got or handoff.is_dead(got[0]):
        return []
    return [{"text": line(b, 400), "source": PREFS_REL} for b in bullets(got[1], LIMITS["prefs"])]


def read_harness(vault: Path) -> list[dict]:
    """Standing rules the user wrote in 00-Context/Harness.md (only while status is active)."""
    got = read_note(vault, HARNESS_REL, 40000)
    if not got:
        return []
    meta, body = got
    if meta.get("status", "active").lower() != "active" or handoff.is_dead(meta):
        return []
    rules = []
    for head, text in sections(body):
        h = head.lower()
        if any(k in h for k in ("skill", "مهارت", "tool", "ابزار")):
            continue  # informational headings, not rules
        for b in bullets(text, 60):
            rules.append({"text": line(b, 500), "section": line(head, 60), "source": HARNESS_REL})
    return rules[:LIMITS["rules"]]


def read_skills(vault: Path) -> list[dict]:
    base = Path(vault) / ".ai-memory" / "skills-src"
    out: list[dict] = []
    try:
        if base.is_symlink() or not base.is_dir():
            return out
        entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for d in entries:
        try:
            if d.is_symlink() or not d.is_dir() or d.name.startswith("."):
                continue
        except OSError:
            continue
        text = handoff.read_plain(d / "SKILL.md", 8000)
        if text is None:
            continue
        meta, body = handoff.parse_note(text)
        desc = meta.get("description", "")
        if not desc or desc[:1] in (">", "|"):
            desc = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
        out.append({"name": line(meta.get("name") or d.name, 80), "description": line(desc, 200),
                    "source": f"skills-src/{d.name}"})
        if len(out) >= LIMITS["skills"]:
            break
    return out


def _user_home() -> Path:
    return Path(os.environ.get("SUITE_USER_HOME") or Path.home())


def _appdata() -> Path:
    return Path(os.environ.get("SUITE_APPDATA") or os.environ.get("APPDATA") or (_user_home() / "AppData" / "Roaming"))


def read_project_instructions(vault: Path) -> list[dict]:
    """AGENTS.md / CLAUDE.md of the folders listed under "projects" in suite-skills.json."""
    cfg_path = Path(vault) / ".ai-memory" / "suite-skills.json"
    text = handoff.read_plain(cfg_path, 200000)
    if text is None:
        return []
    try:
        cfg = json.loads(text)
        raw_list = cfg.get("projects") if isinstance(cfg, dict) else []
    except ValueError:
        return []
    out = []
    for raw in (raw_list or [])[:LIMITS["projects"]]:
        if not isinstance(raw, str) or not raw.strip():
            continue
        t = (raw.replace("{home}", str(_user_home())).replace("{appdata}", str(_appdata()))
             .replace("{vault}", str(vault)).replace("{engine}", str(Path(vault) / ".ai-memory")))
        pdir = Path(t).expanduser()
        pdir = pdir if pdir.is_absolute() else Path(vault) / pdir
        try:
            if pdir.is_symlink() or not pdir.is_dir():
                continue
        except OSError:
            continue
        for name in ("AGENTS.md", "CLAUDE.md"):
            body = handoff.read_plain(pdir / name, 20000)
            if body is None:
                continue
            if EXPORT_MARK in body[:3000]:  # a pack we exported ourselves: never read it back as the project's rules
                continue
            body = _HTML_COMMENT.sub("", body).strip()
            if re.fullmatch(r"@\S+", body):  # a one-line pointer to the other file
                continue
            out.append({"name": line(pdir.name, 60), "file": name, "text": block(body, 3000)})
            break
    return out


# ---------------------------------------------------------------- vault state
def session_checkpoints(vault: Path, only: str | None = None) -> list[tuple[str, str, str, Path]]:
    """(timestamp-prefix, filename, session id, path) of every session checkpoint, newest first."""
    base = Path(vault) / "20-Checkpoints"
    rows: list[tuple[str, str, str, Path]] = []
    try:
        if base.is_symlink() or not base.is_dir():
            return rows
        dirs = [d for d in base.iterdir() if d.name.startswith("session-") and d.is_dir() and not d.is_symlink()]
    except OSError:
        return rows
    for d in dirs:
        sid = d.name[len("session-"):]
        if only and sid != only:
            continue
        try:
            files = [p for p in d.glob("*.md") if not p.is_symlink()]
        except OSError:
            continue
        for p in files:
            rows.append((_stamp_of(p), p.name, sid, p))
    rows.sort(reverse=True)
    return rows


_STAMP_NAME = re.compile(r"^(\d{8}T\d{6}Z)")
_MS_NAME = re.compile(r"^turn-(\d{13})\b")


def _stamp_of(path: Path) -> str:
    """A sortable UTC stamp for a checkpoint file, whatever agent wrote it.

    Claude Code and Hamzaban name files "20261104T111316Z-...", Chat On Steroids "turn-<epoch ms>-...";
    anything else falls back to the file's modification time. Comparing raw names would put every
    "turn-" file after every dated one, so an old chat would always look like the newest.
    """
    m = _STAMP_NAME.match(path.name)
    if m:
        return m.group(1)
    m = _MS_NAME.match(path.name)
    try:
        ts = int(m.group(1)) / 1000 if m else path.stat().st_mtime
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except (OSError, ValueError, OverflowError):
        return "00000000T000000Z"


def strip_pending(body: str) -> str:
    """Drop sections that list candidates still waiting in 30-Pending: unapproved text must not travel in a pack."""
    marks = list(_HEAD.finditer(body))
    if not marks:
        return body
    out = [body[:marks[0].start()]]
    for i, m in enumerate(marks):
        if _PENDING_HEAD.search(m.group(1)):
            continue
        out.append(body[m.start():marks[i + 1].start() if i + 1 < len(marks) else len(body)])
    return "".join(out)


def _checkpoint_dict(vault: Path, path: Path) -> tuple[dict, str] | None:
    text = handoff.read_plain(path, 60000)
    if text is None:
        return None
    meta, body = handoff.parse_note(text)
    if handoff.is_dead(meta):
        return None
    body = strip_pending(body)
    rel = path.relative_to(vault).as_posix()
    return ({"source": rel, "updated": line(meta.get("updated", ""), 40), "type": line(meta.get("type", ""), 40),
             "text": block(body, 6000)}, body)


def project_note(vault: Path, resolved: str) -> dict | None:
    try:
        notes = handoff.project_notes(vault)
    except Exception:
        return None
    for stem, info in notes.items():
        try:
            if handoff._slug(stem) != resolved:
                continue
        except ValueError:
            continue
        got = read_note(vault, f"10-Projects/{stem}.md", 40000)
        if got and not handoff.is_dead(got[0]):
            return {"source": f"10-Projects/{stem}.md", "title": line(info.get("title") or stem, 100),
                    "text": block(got[1], 6000)}
    return None


def parse_turns(body: str) -> list[dict]:
    out = []
    parts = _TURN.split(body)  # [pre, role, ts, text, role, ts, text, ...]
    for i in range(1, len(parts) - 2, 3):
        role, ts, text = parts[i], parts[i + 1].strip(), parts[i + 2]
        text = _CLOSE_FENCE.sub("[closing block omitted: it is staged for the user's approval]", text)
        kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(("[tool:", "[tool result:"))]
        clean = "\n".join(kept).strip()
        if role in ("user", "assistant") and clean:
            out.append({"role": role, "text": clean, "ts": ts[:40]})
    return out


def recent_turns(vault: Path, sid: str, n: int = 6, chars: int = 700) -> list[dict]:
    if not sid or not SID.fullmatch(sid):
        return []
    d = Path(vault) / "40-Conversation-Capture" / sid
    try:
        if d.is_symlink() or not d.is_dir():
            return []
        chunks = sorted((p for p in d.glob("chunk-*.md") if not p.is_symlink()), key=lambda p: p.name, reverse=True)[:8]
    except OSError:
        return []
    turns: list[dict] = []
    for p in chunks:  # newest first; prepend so the list stays chronological
        text = handoff.read_plain(p, 400000) or ""
        _, body = handoff.parse_note(text)
        turns = parse_turns(body) + turns
        if len(turns) >= n:
            break
    out = []
    for t in turns[-n:]:
        out.append({"role": t["role"], "ts": t["ts"], "text": block(t["text"], chars),
                    "source": f"40-Conversation-Capture/{sid}"})
    return out


def _newest_capture_session(vault: Path) -> str:
    base = Path(vault) / "40-Conversation-Capture"
    best = ("", "")
    try:
        if base.is_symlink() or not base.is_dir():
            return ""
        for d in base.iterdir():
            if d.is_symlink() or not d.is_dir() or not SID.fullmatch(d.name):
                continue
            names = [p.name for p in d.glob("chunk-*.md")]
            if names and max(names) > best[0]:
                best = (max(names), d.name)
    except OSError:
        return ""
    return best[1]


# ---------------------------------------------------------------- build
def _cached(cache: dict | None, key: str, fn):
    if cache is None:
        return fn()
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def _facts(vault: Path, cache: dict | None) -> list[dict]:
    """Active facts, read once per refresh, each with its normalized search text."""
    def load():
        rows = handoff.active_facts(vault)
        for f in rows:
            if not f.get("private"):
                m = f["meta"]
                f["_hay"] = handoff._norm(" ".join([m.get("title", ""), m.get("project", ""), m.get("topic", ""),
                                                    m.get("tags", ""), f.get("text", "")]))
        return rows
    return _cached(cache, "facts", load)


def _fact_matches(fact: dict, topic: str, resolved: str, cache: dict | None) -> bool:
    from infinite_context import terms
    for cand in (topic, resolved):
        ts = _cached(cache, "terms:" + cand, lambda: terms(cand))
        if ts and all(t in fact["_hay"] for t in ts):
            return True
    return False


def _gather(vault: Path, topic: str, cache: dict | None) -> dict:
    """Like handoff.gather, but facts come from the per-refresh cache and pending sections are dropped."""
    resolved, how = handoff.resolve_topic(vault, topic)
    g: dict = {"resolved": resolved, "how": how, "checkpoint": None, "tasks": [], "facts": []}
    cp = handoff.latest_checkpoint(vault, resolved)
    got = _checkpoint_dict(vault, cp) if cp else None
    if got:
        ckpt, body = got
        g["checkpoint"] = ckpt
        for kind, item in handoff.checkpoint_tasks(body):
            g["tasks"].append({"kind": kind, "text": item, "source": ckpt["source"]})
    for fact in _facts(vault, cache):
        if fact.get("private"):
            continue  # private facts are never matched, counted or released
        if _fact_matches(fact, topic, resolved, cache):
            sens = fact["meta"].get("sensitivity", "").lower()
            g["facts"].append({"id": fact["id"], "source": fact["path"].relative_to(vault).as_posix(),
                               "label": "unreviewed" if sens in ("", "unreviewed") else "approved", "text": fact["text"]})
    return g


def build(vault, topic: str | None = None, session: str | None = None, generated: str | None = None,
          recent_n: int | None = None, cache: dict | None = None) -> dict:
    vault = Path(vault)
    if session is not None and not SID.fullmatch(session or ""):
        raise ValueError("session id must be 6-90 letters, digits, '-' or '_'")
    n_recent = LIMITS["recent"] if recent_n is None else max(0, min(int(recent_n), 20))
    ckpt: dict | None = None
    tasks: list[dict] = []
    facts: list[dict] = []
    project: dict | None = None
    sid = session or ""
    conv = ""
    agent = "unknown"

    if topic and topic.strip():
        g = _gather(vault, topic.strip()[:200], cache)
        topic_info = {"name": line(topic, 80), "resolved": g["resolved"], "how": g["how"]}
        ckpt = g["checkpoint"]
        tasks = [{"kind": t["kind"], "text": line(t["text"], 300), "source": t["source"], "status": "unverified"}
                 for t in g["tasks"]][:LIMITS["tasks"]]
        facts = [{"id": f["id"], "label": f["label"], "text": block(f["text"], 700), "source": f["source"]}
                 for f in g["facts"]][:LIMITS["facts"]]
        project = project_note(vault, g["resolved"])
    else:
        rows = session_checkpoints(vault, only=session or None)
        picked = None
        for _ts, _name, row_sid, path in rows:
            picked = _checkpoint_dict(vault, path)
            if picked:
                sid = sid or row_sid
                break
        if picked:
            ckpt, body = picked
            tasks = [{"kind": k, "text": line(t, 300), "source": ckpt["source"], "status": "unverified"}
                     for k, t in handoff.checkpoint_tasks(body)][:LIMITS["tasks"]]
            meta = (read_note(vault, ckpt["source"], 60000) or ({}, ""))[0]
            agent = agent_from_source(meta.get("source", ""))
            c = meta.get("conversation", "")
            if SID.fullmatch(c) and (Path(vault) / "40-Conversation-Capture" / c).is_dir():
                conv = c  # the real chat this checkpoint was written for (hz checkpoints have their own folder name)
        if not sid:
            sid = _newest_capture_session(vault)
        topic_info = {"name": "latest", "resolved": sid, "how": "session" if session else "newest-session"}
        for f in _facts(vault, cache):
            if f.get("private"):
                continue
            sens = f["meta"].get("sensitivity", "").lower()
            facts.append({"id": line(f["id"], 40), "label": "unreviewed" if sens in ("", "unreviewed") else "approved",
                          "text": block(f["text"], 700), "source": f["path"].relative_to(vault).as_posix()})
            if len(facts) >= LIMITS["facts"]:
                break

    pack = {
        "format": PACK_FORMAT, "version": PACK_VERSION, "generator": f"hamzaban/{VERSION}",
        "id": "", "generated": generated or now_iso(),
        "topic": topic_info,
        "source": {"agent": agent, "session": sid, "conversation": conv or sid, "checkpoint": ckpt["source"] if ckpt else ""},
        "languages": {"pivot": "en", "content": ["en"]},
        "user": {"preferences": _cached(cache, "prefs", lambda: read_preferences(vault))},
        "harness": {"rules": _cached(cache, "rules", lambda: read_harness(vault)),
                    "skills": _cached(cache, "skills", lambda: read_skills(vault)), "tools": [dict(t) for t in TOOLS],
                    "projects": _cached(cache, "projects", lambda: read_project_instructions(vault))},
        "state": {"project": project, "checkpoint": ckpt, "tasks": tasks, "facts": facts},
        "recent": recent_turns(vault, conv or sid, n_recent) if (conv or sid) and n_recent else [],
        "protocol": {"close_marker": CLOSE_MARKER, "version": CLOSE_VERSION},
    }
    langs = languages_of(ckpt["text"] if ckpt else "", " ".join(r["text"] for r in pack["harness"]["rules"]),
                         project["text"] if project else "", " ".join(f["text"] for f in facts))
    pack["languages"]["content"] = langs
    pack["id"] = digest(pack)
    return pack


# ---------------------------------------------------------------- identity, validation, external packs
def canonical(pack: dict) -> str:
    return json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(pack: dict) -> str:
    body = {k: v for k, v in pack.items() if k not in ("id", "generated")}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()[:16]


def dumps(pack: dict) -> str:
    return json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate(pack) -> list[str]:
    problems: list[str] = []
    if not isinstance(pack, dict):
        return ["pack must be a JSON object"]
    if pack.get("format") != PACK_FORMAT:
        problems.append("format must be " + PACK_FORMAT)
    if pack.get("version") != PACK_VERSION:
        problems.append(f"unsupported version {pack.get('version')!r} (this build reads {PACK_VERSION})")
    for key, typ in (("topic", dict), ("source", dict), ("user", dict), ("harness", dict), ("state", dict),
                     ("recent", list), ("protocol", dict), ("languages", dict)):
        if not isinstance(pack.get(key), typ):
            problems.append(f"{key} must be a {typ.__name__}")
    if problems:
        return problems
    h, s, u = pack["harness"], pack["state"], pack["user"]
    for key in ("rules", "skills", "tools", "projects"):
        if not isinstance(h.get(key), list):
            problems.append(f"harness.{key} must be a list")
    for key in ("tasks", "facts"):
        if not isinstance(s.get(key), list):
            problems.append(f"state.{key} must be a list")
    if not isinstance(u.get("preferences"), list):
        problems.append("user.preferences must be a list")
    if problems:
        return problems

    def texts(items, label):
        for i, it in enumerate(items):
            if not isinstance(it, dict) or not isinstance(it.get("text", it.get("description", "")), str):
                problems.append(f"{label}[{i}] must be an object with text")

    texts(h["rules"], "harness.rules")
    texts(s["tasks"], "state.tasks")
    texts(s["facts"], "state.facts")
    texts(u["preferences"], "user.preferences")
    for i, t in enumerate(pack["recent"]):
        if not isinstance(t, dict) or t.get("role") not in ("user", "assistant") or not isinstance(t.get("text"), str):
            problems.append(f"recent[{i}] needs role user|assistant and text")
    if pack.get("protocol", {}).get("close_marker") != CLOSE_MARKER:
        problems.append("protocol.close_marker mismatch")
    if not problems and pack.get("id") not in ("", digest(pack)):
        problems.append("id does not match the content (was the pack edited by hand?)")
    return problems


_PATH_KEYS = ("source", "checkpoint", "file")
_SAFE_PATH = re.compile(r"^[\w .()/-]{1,200}$")  # \w covers Persian letters too


def _walk(value, fn, key=None, depth=0):
    if depth > 12:
        return None
    if isinstance(value, str):
        return fn(key, value)
    if isinstance(value, list):
        return [_walk(v, fn, key, depth + 1) for v in value[:500]]
    if isinstance(value, dict):
        return {str(k)[:60]: _walk(v, fn, str(k), depth + 1) for k, v in list(value.items())[:200]}
    return value


def _clean_text(key, text: str) -> str:
    if key == "close_marker":  # validated afterwards; defusing it would break the protocol
        return text[:40]
    if key in _PATH_KEYS and _SAFE_PATH.fullmatch(text) and ".." not in text and not text.startswith("/"):
        # a vault-relative path: redact segment by segment, because the long-token rule would otherwise
        # swallow a whole "folder/session-id/file" path as if it were one secret
        return neutralize("/".join(handoff.scrub(seg) for seg in text.split("/")))
    return neutralize(handoff.scrub(text))[:12000]


def sanitize(pack: dict) -> dict:
    """For packs from outside: redact every string again, cap sizes, fix the id."""
    clean = _walk(pack, _clean_text)
    if isinstance(clean, dict):
        clean["id"] = ""
        if not validate(clean):
            clean["id"] = digest(clean)
    return clean


def load(path) -> dict:
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise ValueError("pack file not found or not a regular file")
    if p.stat().st_size > 5_000_000:
        raise ValueError("pack file is too large")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError("pack file is not valid JSON") from exc
    problems = validate(data)
    if problems:
        raise ValueError("invalid pack: " + "; ".join(problems[:4]))
    return sanitize(data)
