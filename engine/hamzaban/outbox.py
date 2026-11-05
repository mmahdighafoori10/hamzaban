"""The outbox: ready-made packs for every agent, kept fresh in 50-Handoff/.

refresh() is safe to call as often as you like (a hook, the minute-by-minute autosync, by hand):
  * it only writes a file when its content really changed (same content => same bytes => no write)
  * it never deletes anything and never writes outside 50-Handoff/, 30-Pending/, 20-Checkpoints/ and the engine folder
  * it first reads closing blocks that agents printed at the end of their chats (assistant turns only)
    and stages them for approval; the first run only records what already exists (nothing old is ingested)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import handoff

from . import NAME, NAME_FA, VERSION
from . import adapters as adp
from . import closing, render as rnd
from . import pack as packmod

OUT = "50-Handoff"
STATE_NAME = "hamzaban-state.json"
CONFIG_NAME = "hamzaban.json"
DEFAULT_CONFIG = {"targets": list(adp.DEFAULT_TARGETS), "max_topics": 12, "lang": "en", "min_interval_seconds": 120,
                  "min_chunk_age_seconds": 10}
MAX_CHUNKS_PER_PASS = 300       # chunks read per refresh
MAX_NEW_CHECKPOINTS = 3         # closing blocks turned into checkpoints per refresh (a flood guard)

README = f"""---
title: {NAME} handoff
generated-by: {NAME.lower()}/{VERSION}
---

# {NAME} · {NAME_FA}

**English.** This folder is generated. `latest/` holds the newest chat's context, one file per agent
(Claude Code, Codex, Gemini, ChatGPT, ...), all made from the same neutral `pack.json`. Open the file that
matches the agent you are about to use, or paste it as the first message. At the end of a chat the agent
prints a `hamzaban-close` block; it is staged in `30-Pending/` for your approval. Do not edit files here:
they are rewritten when the underlying notes change.

**فارسی.** این پوشه خودکار ساخته می‌شود. در `latest/` برای هر عامل (Claude Code، Codex، Gemini، ChatGPT و ...)
یک فایل هست که همه از یک `pack.json` بی‌طرف ساخته شده‌اند. فایلِ همان عاملی را که می‌خواهی باز کنی بردار و
به‌عنوان پیام اول بده. در پایان چت، عامل یک بلوک `hamzaban-close` می‌نویسد که برای تأیید تو در `30-Pending/` می‌رود.
فایل‌های اینجا را دستی عوض نکن؛ با تغییر یادداشت‌ها دوباره نوشته می‌شوند.
"""


# ---------------------------------------------------------------- small io helpers
def engine_dir(vault: Path) -> Path:
    return Path(vault) / ".ai-memory"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path: Path, text: str) -> None:
    """Temp file + rename. Retries when another program (sync client, antivirus, Obsidian) holds the target
    for a moment, and never leaves the temp file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _read_json(path: Path, default):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return default
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def load_state(vault: Path) -> dict:
    state = _read_json(engine_dir(vault) / STATE_NAME, {})
    state.setdefault("files", {})
    state.setdefault("packs", {})
    state.setdefault("seen", {})   # capture session -> name of the newest chunk already looked at
    state.setdefault("dirm", {})   # capture session -> folder mtime when it was last fully handled
    return state


def save_state(vault: Path, state: dict) -> None:
    write_atomic(engine_dir(vault) / STATE_NAME, json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))


def load_config(vault: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    raw = _read_json(engine_dir(vault) / CONFIG_NAME, {})
    if isinstance(raw.get("targets"), list) and all(isinstance(t, str) for t in raw["targets"]):
        cfg["targets"] = raw["targets"][:20]
    if isinstance(raw.get("max_topics"), int) and not isinstance(raw["max_topics"], bool):
        cfg["max_topics"] = max(0, min(raw["max_topics"], 50))
    if raw.get("lang") in ("en", "fa"):
        cfg["lang"] = raw["lang"]
    for key, top in (("min_interval_seconds", 86400), ("min_chunk_age_seconds", 3600)):
        if isinstance(raw.get(key), int) and not isinstance(raw[key], bool):
            cfg[key] = max(0, min(raw[key], top))
    return cfg


def safe_name(text: str) -> str:
    """A folder name that is valid on Windows and never shared by two different topics."""
    raw = str(text)
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", raw)
    name = cleaned[:50].strip(" .-") or "topic"          # cut first, strip after: no trailing space or dot survives
    if name.split(".")[0].upper() in adp.DEVICES:           # CON, NUL, COM1 ...
        name = "_" + name
    if name != raw:  # anything was replaced, trimmed or cut: tell it apart from a topic that looks the same
        name += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return name


def log_error(vault: Path, message: str) -> None:
    try:
        logs = engine_dir(vault) / "logs"
        if logs.is_symlink():
            return
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "hamzaban-errors.log"
        if path.is_file() and path.stat().st_size > 1_000_000:
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_now_iso()} {message[:400]}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- reading what agents said back
def SID_OK(name: str) -> bool:
    return bool(packmod.SID.fullmatch(name))


def scan_captures(vault: Path, state: dict, min_age: int = 10) -> int:
    """Stage the closing blocks of chunks written since the last pass.

    Per capture session we remember the name of the newest chunk already looked at (chunk names start with a
    timestamp), so a pass only reads what is new, however large the archive grows. The first pass only records
    what exists: nothing old is ingested. A chunk younger than `min_age` seconds may still be being written and
    is left for the next pass. Only the last closing block of a chunk counts, and at most
    MAX_NEW_CHECKPOINTS become checkpoints per pass.
    """
    root = Path(vault) / "40-Conversation-Capture"
    seen, dirm = state["seen"], state["dirm"]
    try:
        if root.is_symlink():
            return 0
        dirs = sorted((d for d in root.iterdir() if SID_OK(d.name) and d.is_dir() and not d.is_symlink()),
                      key=lambda d: d.name) if root.is_dir() else []
    except OSError:
        return 0
    first = not state.get("baseline")
    now = time.time()
    count = made = processed = 0
    alive = set()
    for d in dirs:
        alive.add(d.name)
        try:
            mtime = d.stat().st_mtime_ns
            if not first and dirm.get(d.name) == mtime:
                continue  # nothing was added since the last full pass
            names = sorted(p.name for p in d.glob("chunk-*.md") if not p.is_symlink())
        except OSError:
            continue
        if first:
            if names:
                seen[d.name] = names[-1]
            dirm[d.name] = mtime
            continue
        last = seen.get(d.name, "")
        deferred = False
        for name in (n for n in names if n > last):
            path = d / name
            try:
                young = now - path.stat().st_mtime < min_age
            except OSError:
                deferred = True
                break
            if young or processed >= MAX_CHUNKS_PER_PASS:
                deferred = True
                break
            processed += 1
            body = handoff.parse_note(handoff.read_plain(path, 400000) or "")[1]
            blocks = closing.parse_blocks(closing.assistant_text(body))[-1:]
            if blocks:
                if made >= MAX_NEW_CHECKPOINTS:
                    deferred = True
                    break
                res = closing.ingest(vault, blocks[0], session=d.name)
                if res["pending"] or res["checkpoint"]:
                    count += 1
                if res["checkpoint"]:
                    made += 1
            seen[d.name] = name
        if not deferred:
            dirm[d.name] = mtime
    state["baseline"] = True
    for table in (seen, dirm):  # forget sessions that no longer exist
        for sid in [k for k in table if k not in alive]:
            del table[sid]
    return count


# ---------------------------------------------------------------- the refresh
def _has_content(pack: dict) -> bool:
    st = pack["state"]
    return bool(pack["harness"]["rules"] or pack["user"]["preferences"] or pack["recent"] or st.get("project")
                or st.get("checkpoint") or st["facts"] or st["tasks"])


def _topics(vault: Path, limit: int) -> list[str]:
    """Topics worth a pack of their own: most recently checkpointed first, then project notes without a checkpoint."""
    if limit <= 0:
        return []
    rows: dict[str, str] = {}  # topic -> name of its newest checkpoint file ("" when it has none)
    try:
        for d in handoff.checkpoint_dirs(vault):
            latest = handoff.latest_checkpoint(vault, d.name)
            if latest:
                rows[d.name] = latest.name
        for stem in handoff.project_notes(vault):
            try:
                rows.setdefault(handoff._slug(stem), "")
            except ValueError:
                continue
    except Exception:
        return []
    with_ckpt = sorted((t for t, n in rows.items() if n), key=lambda t: (rows[t], t), reverse=True)
    without = sorted(t for t, n in rows.items() if not n)
    return (with_ckpt + without)[:limit]


def _put(vault: Path, state: dict, rel: str, text: str, result: dict) -> None:
    path = Path(vault).joinpath(*rel.split("/"))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if state["files"].get(rel) == digest and path.is_file():
        result["unchanged"] += 1
        return
    cur = Path(vault)
    for part in rel.split("/")[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise OSError(f"{cur.name} is a symlink; refusing to write")
    write_atomic(path, text)
    state["files"][rel] = digest
    result["written"].append(rel)


def refresh(vault=None, force: bool = False, topics: bool = True) -> dict:
    """Rebuild the packs. `topics=False` refreshes only the "latest" pack (what a session-end hook needs)."""
    vault = Path(vault) if vault else handoff.default_vault()
    result: dict = {"skipped": None, "written": [], "unchanged": 0, "errors": [], "ingested": 0, "topics": [], "targets": []}
    cfg = load_config(vault)
    state = load_state(vault)
    now = time.time()
    if not force and now - float(state.get("last_refresh_ts") or 0) < cfg["min_interval_seconds"]:
        result["skipped"] = "throttled"
        return result
    out_root = vault / OUT
    if out_root.is_symlink():
        result["skipped"] = f"{OUT} is a symlink"
        return result

    try:
        result["ingested"] = scan_captures(vault, state, cfg["min_chunk_age_seconds"])
    except Exception as exc:
        result["errors"].append(f"ingest: {type(exc).__name__}: {str(exc)[:120]}")

    adapters, problems = adp.load(vault)
    chosen, used_files = [], {}
    for name in cfg["targets"]:
        try:
            adapter = adp.resolve(name, adapters)
        except adp.UnknownTarget:
            result["errors"].append(f"unknown target in {CONFIG_NAME}: {name[:40]}")
            continue
        if adapter in chosen:
            continue
        other = used_files.get(adapter["filename"].lower())
        if other:  # two targets writing one file would overwrite each other on every refresh
            result["errors"].append(f"target {adapter['name']} skipped: file {adapter['filename']} is already used by {other}")
            continue
        used_files[adapter["filename"].lower()] = adapter["name"]
        chosen.append(adapter)
    result["targets"] = [a["name"] for a in chosen]

    plan = [("latest", None, f"{OUT}/latest")]
    taken = {"latest"}
    if topics:
        for t in _topics(vault, cfg["max_topics"]):
            folder = safe_name(t)
            if folder.lower() in taken:  # two names that differ only by letter case are one folder on Windows
                folder = folder[:50] + "-" + hashlib.sha1(t.encode("utf-8")).hexdigest()[:6]
            taken.add(folder.lower())
            plan.append((t, t, f"{OUT}/topics/{folder}"))
    cache: dict = {}  # facts, preferences, skills ... are read once per refresh, not once per pack
    wrote_any = False
    for key, topic, folder in plan:
        try:
            pack = packmod.build(vault, topic, cache=cache)
        except Exception as exc:
            result["errors"].append(f"{key[:40]}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if not _has_content(pack):
            continue
        prev = state["packs"].get(key) or {}
        if prev.get("digest") == pack["id"] and prev.get("generated"):
            pack["generated"] = prev["generated"]  # same content => same bytes => nothing to rewrite
        else:
            state["packs"][key] = {"digest": pack["id"], "generated": pack["generated"]}
        try:
            _put(vault, state, f"{folder}/pack.json", packmod.dumps(pack), result)
            for adapter in chosen:
                res = rnd.render(pack, adapter, lang=cfg["lang"])
                _put(vault, state, f"{folder}/{adapter['filename']}", res["text"], result)
            wrote_any = True
            result["topics"].append(key)
        except OSError as exc:
            result["errors"].append(f"{key[:40]}: {type(exc).__name__}: {str(exc)[:120]}")
    if wrote_any:
        try:
            _put(vault, state, f"{OUT}/README.md", README, result)
        except OSError as exc:
            result["errors"].append(f"README: {str(exc)[:120]}")

    state["last_refresh_ts"] = now
    state["last"] = {"at": _now_iso(), "written": len(result["written"]), "unchanged": result["unchanged"],
                     "topics": result["topics"], "targets": result["targets"], "errors": result["errors"][:5],
                     "ingested": result["ingested"], "adapter_problems": problems[:5], "version": VERSION}
    try:
        save_state(vault, state)
    except OSError as exc:
        result["errors"].append(f"state: {str(exc)[:120]}")
    return result


def refresh_quiet(vault=None, force: bool = False, topics: bool = True) -> dict | None:
    """For hooks and autosync: never raises, logs problems instead."""
    try:
        res = refresh(vault, force=force, topics=topics)
        v = Path(vault) if vault else handoff.default_vault()
        for e in res.get("errors", [])[:3]:
            log_error(v, e)
        return res
    except Exception as exc:
        try:
            log_error(Path(vault) if vault else handoff.default_vault(), f"refresh: {type(exc).__name__}: {str(exc)[:200]}")
        except Exception:
            pass
        return None


def status(vault=None) -> dict:
    vault = Path(vault) if vault else handoff.default_vault()
    state = load_state(vault)
    last = state.get("last") or {}
    return {"version": VERSION, "last": last, "files": len(state.get("files") or {}),
            "pending_closing_blocks": None, "outbox_exists": (vault / OUT).is_dir()}
