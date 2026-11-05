#!/usr/bin/env python3
"""Bring the memories the agents keep for themselves into the shared notebook. Python 3.10, stdlib only.

An agent's own memory is written in that agent's own shape. This module reads those shapes and turns every memory into one
plain proposal in 30-Pending, in the notebook's common language. Once the user approves it, it travels to every other agent
inside the Hamzaban pack.

  Claude Code   ~/.claude/projects/<project>/memory/*.md    notes with front matter: name, description, metadata.type
                                                              (MEMORY.md is only an index and is skipped)
  Codex         ~/.codex/memories_1.sqlite                   table stage1_outputs: raw_memory, rollout_summary, rollout_slug
                                                              (not a documented format: the reader takes what it finds and
                                                              gives up quietly when the columns are not there)

Read-only on the agents' folders (the SQLite file is read from a temporary copy). Nothing becomes a fact without approval:
every memory is a proposal, like every other proposal. Secrets are redacted before anything is written. Running it again
proposes nothing that is already pending, rejected or approved.

  python native_memory.py scan [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
MAX_FILES = 300            # notes read per agent
MAX_FILE_BYTES = 64 * 1024
BODY_CHARS = 700           # how much of a note's text goes into the proposal
DESC_CHARS = 240
PER_PASS = 25              # proposals written per run: the rest waits for the next one
STATE_NAME = "native-memory-state.json"

AGENT_NAME = {"claude-code": "Claude Code", "codex": "Codex"}


# ---------------------------------------------------------------------------------------------------- helpers
def _home() -> Path:
    env = os.environ.get("SUITE_USER_HOME")
    return Path(env).expanduser() if env else Path.home()


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_note(text: str) -> tuple[dict, str]:
    """(meta, body) of a markdown note with a front matter block. Tolerant: nested keys come back as 'parent.key'
    (metadata:\\n  type: feedback -> 'metadata.type'); a note without front matter is all body."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text.strip()
    block, body = text[4:end], text[end + 4:].lstrip("\n")
    meta: dict[str, str] = {}
    parent = ""
    for line in block.split("\n"):
        m = re.match(r"(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.*)$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        indent, key, val = m.group(1), m.group(2), m.group(3).strip().strip("\"'")
        if not indent:
            parent = key if val == "" else ""
            if val != "":
                meta[key] = val
        elif parent:
            meta[f"{parent}.{key}"] = val
    return meta, body.strip()


# ---------------------------------------------------------------------------------------------------- readers
def read_claude(home: Path) -> tuple[list[dict], list[str]]:
    """Every note under ~/.claude/projects/*/memory/, as {agent, key, kind, name, description, body, stamp}."""
    out, skipped = [], []
    root = home / ".claude" / "projects"
    try:
        folders = sorted(d for d in root.glob("*/memory") if d.is_dir() and not d.is_symlink())
    except OSError:
        return out, skipped
    for folder in folders:
        try:
            files = sorted(p for p in folder.glob("*.md") if p.name.lower() != "memory.md")
        except OSError:
            continue
        for p in files[:MAX_FILES]:
            try:
                st = p.stat()
                if p.is_symlink() or not p.is_file():
                    skipped.append(f"{p.name}: not a regular file")
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    skipped.append(f"{p.name}: larger than {MAX_FILE_BYTES // 1024} KB")
                    continue
                meta, body = parse_note(p.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError as exc:
                skipped.append(f"{p.name}: {type(exc).__name__}")
                continue
            out.append({"agent": "claude-code", "key": f"claude-code:{folder.parent.name}/{p.name}",
                        "kind": meta.get("metadata.type") or meta.get("type") or "",
                        "name": meta.get("name") or p.stem, "description": meta.get("description", ""), "body": body,
                        "stamp": [st.st_size, st.st_mtime_ns]})
    return out, skipped


def read_codex(home: Path) -> tuple[list[dict], list[str]]:
    """Rows of ~/.codex/memories_1.sqlite stage1_outputs that hold a memory. The file is copied first and never opened in place."""
    db = home / ".codex" / "memories_1.sqlite"
    try:
        if not db.is_file() or db.is_symlink():
            return [], []
        st = db.stat()
    except OSError:
        return [], []
    tmp = Path(tempfile.mkdtemp(prefix="native-memory-"))
    try:
        shutil.copyfile(db, tmp / "m.sqlite")
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.is_file():
                shutil.copyfile(side, tmp / ("m.sqlite" + suffix))
        con = sqlite3.connect(str(tmp / "m.sqlite"))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(stage1_outputs)")}
            if "raw_memory" not in cols:
                return [], []
            want = [c for c in ("thread_id", "raw_memory", "rollout_summary", "rollout_slug") if c in cols]
            rows = con.execute(f"SELECT {', '.join(want)} FROM stage1_outputs LIMIT {MAX_FILES}").fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return [], [f"{db.name}: unreadable"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = []
    for row in rows:
        r = dict(zip(want, row))
        raw, summary = str(r.get("raw_memory") or "").strip(), str(r.get("rollout_summary") or "").strip()
        if not raw and not summary:
            continue
        ident = str(r.get("thread_id") or r.get("rollout_slug") or len(out))
        out.append({"agent": "codex", "key": f"codex:{ident}", "kind": "memory", "name": str(r.get("rollout_slug") or ident[:12]),
                    "description": summary, "body": raw, "stamp": [st.st_size, st.st_mtime_ns]})
    return out, []


READERS = (read_claude, read_codex)


# ---------------------------------------------------------------------------------------------------- proposals
def proposal_text(mem: dict) -> str:
    """One memory as the plain statement that goes to 30-Pending, secrets already redacted."""
    from redact import scrub_all
    agent = AGENT_NAME.get(mem["agent"], mem["agent"])
    kind = f" ({mem['kind']})" if mem.get("kind") else ""
    desc, body = _clip(mem.get("description", ""), DESC_CHARS), mem.get("body", "").strip()
    head = f"{agent} memory{kind} — {mem['name']}" + (f": {desc}" if desc else "")
    if body and _clip(body, DESC_CHARS) != desc:
        head += "\n\n" + _clip(re.sub(r"\n{3,}", "\n\n", body), BODY_CHARS)
    return scrub_all(head).strip()


def _state_path() -> Path:
    import pending
    return pending.VAULT / ".ai-memory" / STATE_NAME


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def scan(home: Path | None = None, dry_run: bool = False, limit: int = PER_PASS, state_path: Path | None = None) -> dict:
    """Read every agent's own memory and propose what the notebook does not have yet. Returns the counts."""
    import pending
    home = home or _home()
    state_path = state_path or _state_path()
    state = _load_state(state_path)
    seen: dict = dict(state.get("seen") or {})
    stats = {"found": 0, "proposed": 0, "known": 0, "unchanged": 0, "deferred": 0, "skipped": [], "errors": [], "items": []}
    for reader in READERS:
        try:
            mems, skipped = reader(home)
        except Exception as exc:  # one agent's odd store never stops the others
            stats["errors"].append(f"{reader.__name__}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        stats["skipped"] += skipped
        for mem in mems:
            stats["found"] += 1
            if seen.get(mem["key"]) == mem["stamp"]:
                stats["unchanged"] += 1
                continue
            text = proposal_text(mem)
            if len(text) < 12:
                seen[mem["key"]] = mem["stamp"]
                continue
            if stats["proposed"] >= limit:
                stats["deferred"] += 1
                continue
            ident = pending.fact_id(text)
            if pending._exists(ident):           # pending, rejected or already a fact
                stats["known"] += 1
                seen[mem["key"]] = mem["stamp"]
                continue
            stats["items"].append({"agent": mem["agent"], "name": mem["name"], "kind": mem["kind"], "id": ident})
            if not dry_run:
                pending.stage(text, source=f"native-memory:{mem['agent']}", session=ident[:8])
                seen[mem["key"]] = mem["stamp"]
            stats["proposed"] += 1
    if not dry_run and seen != state.get("seen"):
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps({"seen": seen}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, state_path)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="native_memory.py", description="Propose the agents' own memories to the shared notebook")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="read the agents' memories and propose the new ones (30-Pending)")
    s.add_argument("--dry-run", action="store_true", help="list what would be proposed; write nothing")
    args = ap.parse_args(argv)
    res = scan(dry_run=args.dry_run)
    verb = "would propose" if args.dry_run else "proposed"
    print(f"native memory: found {res['found']}, {verb} {res['proposed']}, already known {res['known']}, unchanged {res['unchanged']}"
          + (f", deferred {res['deferred']}" if res["deferred"] else ""))
    for item in res["items"]:
        print(f"  + {AGENT_NAME.get(item['agent'], item['agent'])} · {item['kind'] or 'note'} · {item['name']}  ({item['id']})")
    for line in res["skipped"][:10]:
        print(f"  skipped: {line}")
    for line in res["errors"][:5]:
        print(f"  error: {line}")
    if res["proposed"] and not args.dry_run:
        print("Review them: python pending.py list   (approve or reject each one; nothing is a fact before that)")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
