"""Capture BOTH sides of Claude Code sessions into the Obsidian Vault. Hook-safe.

Reads the session transcript JSONL (transcript_path from the hook payload), writes
redacted chunk notes incrementally and idempotently (byte offset per session), and
builds extractive checkpoints on PreCompact / SessionEnd (and every ~30 new turns on Stop).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from redact import scrub_all
import pending
from infinite_context import normalized

VAULT = Path(os.environ.get("INFINITE_CTX_VAULT") or Path(__file__).resolve().parent.parent)
STATE = VAULT / ".ai-memory" / "claude-capture-state.sqlite3"
CAPTURE_ROOT = VAULT / "40-Conversation-Capture"
CHECK_ROOT = VAULT / "20-Checkpoints"
SID = re.compile(r"^[A-Za-z0-9_-]{6,90}$")
MAX_TEXT = 20_000
MAX_TOOL = 300
MAX_READ = 16 * 1024 * 1024
THROTTLE_SECONDS = 600
THROTTLE_MIN_BYTES = 4000
STOP_CHECKPOINT_EVERY = 30
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
CUES = [normalized(x) for x in (
    "از این به بعد", "از این پس", "تصمیم گرفتم", "تصمیم گرفتیم", "قرار شد", "ترجیح میدم",
    "ترجیح می‌دهم", "فراموش نکن که", "from now on", "i decided", "we decided",
    "remember that", "i prefer", "please always", "please never")]
INJECTED = (
    re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL),
    re.compile(r"<user-prompt-submit-hook>.*?</user-prompt-submit-hook>", re.DOTALL),
    re.compile(r"<(task-notification|local-command-caveat|local-command-stdout|local-command-stderr|command-name|command-message|command-args)>.*?</\1>", re.DOTALL),
    re.compile(r"\[AUTO-RECALLED PROJECT BACKGROUND.*\Z", re.DOTALL),
    re.compile(r"\[SESSION CHECKPOINT .*?\]\n.*?(?=\n\n\n|\Z)", re.DOTALL),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def strip_injected(text: str) -> str:
    for regex in INJECTED:
        text = regex.sub("", text)
    return text.strip()


def flatten(content) -> tuple[str, list[dict], bool]:
    """Return (text, tool_events, only_tool_results)."""
    if isinstance(content, str):
        return content, [], False
    if not isinstance(content, list):
        return "", [], False
    parts, tools, results = [], [], 0
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif kind == "tool_use":
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            target = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
            command = str(inp.get("command") or "")[:120]
            event = {"name": str(block.get("name") or "?"), "target": str(target), "command": command}
            if event["name"] == "TodoWrite" and isinstance(inp.get("todos"), list):
                event["todos"] = [t for t in inp["todos"] if isinstance(t, dict)]
            tools.append(event)
            parts.append(f"[tool: {event['name']} {target or command}]".strip())
        elif kind == "tool_result":
            results += 1
            raw = block.get("content")
            if isinstance(raw, list):
                raw = " ".join(str(b.get("text", "")) for b in raw if isinstance(b, dict))
            parts.append("[tool result: " + re.sub(r"\s+", " ", str(raw or ""))[:MAX_TOOL] + "]")
    only_results = results > 0 and results == len([b for b in content if isinstance(b, dict)])
    return "\n".join(parts), tools, only_results


def entry_turn(entry) -> dict | None:
    if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant") or entry.get("isMeta"):
        return None
    msg = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    role = msg.get("role") or entry.get("type")
    text, tools, only_results = flatten(msg.get("content", entry.get("content")))
    if role == "user":
        text = strip_injected(text)
    if not text.strip():
        return None
    return {"role": "tool" if only_results else role, "text": text, "tools": tools,
            "ts": str(entry.get("timestamp") or "")}


def read_new(path: str, offset: int) -> tuple[list[dict], int]:
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if offset > size:
            offset = 0
        handle.seek(offset)
        data = handle.read(MAX_READ)
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    chunk = data[: end + 1]
    turns = []
    for line in chunk.splitlines():
        if not line.strip():
            continue
        try:
            turn = entry_turn(json.loads(line.decode("utf-8", errors="replace")))
        except (ValueError, TypeError):
            continue
        if turn:
            turns.append(turn)
    return turns, offset + len(chunk)


def chat_front(sid: str) -> str:
    """Front-matter lines that tie a note to its chat: `chat: CC-7K3QF` and an Obsidian tag, so #chat/CC-7K3QF finds every note
    of that chat. Never raises (a hook must not break a session)."""
    try:
        import chats
        tag = chats.peek_tag(VAULT, "claude-code", sid)
        return f"chat: {tag}\ntags: [chat/{tag}]\n"
    except Exception:
        return ""


def write_new(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
        return True
    except FileExistsError:
        return False


def db() -> sqlite3.Connection:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, transcript TEXT, "
                 "offset INTEGER NOT NULL DEFAULT 0, last_run REAL NOT NULL DEFAULT 0, "
                 "turns INTEGER NOT NULL DEFAULT 0, cp_turns INTEGER NOT NULL DEFAULT 0)")
    return conn


# A line inside a turn that looks like a turn header ("### assistant — ...") would split the turn when the chunk is
# read back and could pass text off as another speaker; a leading backslash makes it ordinary text.
_FAKE_TURN_HEADER = re.compile(r"(?m)^([ \t]*)(###[ \t]+(?:user|assistant|tool)[ \t]+[—–-])")


def write_chunk(sid: str, cwd: str, turns: list[dict]) -> Path | None:
    if not turns:
        return None
    body, used = [], 0
    for t in turns:
        text = _FAKE_TURN_HEADER.sub(lambda m: m.group(1) + "\\" + m.group(2), scrub_all(t["text"])[:MAX_TEXT])
        block = f"### {t['role']} — {t['ts'] or 'n/a'}\n\n{text}\n"
        used += len(block)
        body.append(block)
        if used > 400_000:
            body.append("\n[TRUNCATED: capture size cap reached for this run]\n")
            break
    note = (f"---\ntitle: Claude Code capture — {sid[:8]}\nconversation: {sid}\nupdated: {now_iso()}\n"
            f"cwd: {scrub_all(cwd)}\nsource: claude-code-transcript\ntype: raw-capture\n{chat_front(sid)}---\n\n" + "\n".join(body))
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:10]
    target = CAPTURE_ROOT / sid / f"chunk-{stamp()}-{digest}.md"
    return target if write_new(target, note) else None


def sentences_with_cue(text: str) -> list[str]:
    found = []
    for sentence in re.split(r"(?<=[.!?؟])\s+|\n+", text):
        s = sentence.strip()
        if 15 <= len(s) <= 400 and any(cue in normalized(s) for cue in CUES):
            found.append(s)
    return found


def build_checkpoint(sid: str, cwd: str, trigger: str, turns_all: list[dict]) -> Path | None:
    users = [t for t in turns_all if t["role"] == "user"]
    assistants = [t for t in turns_all if t["role"] == "assistant"]
    files: list[str] = []
    todos: list[dict] = []
    for t in turns_all:
        for ev in t["tools"]:
            if ev["name"] in EDIT_TOOLS and ev["target"] and ev["target"] not in files:
                files.append(ev["target"])
            if ev["name"] == "TodoWrite" and "todos" in ev:
                todos = ev["todos"]
    open_tasks = [str(x.get("content") or x.get("activeForm") or "")[:200] for x in todos if x.get("status") != "completed"]
    candidates: list[str] = []
    for t in users:
        for s in sentences_with_cue(t["text"]):
            if s not in candidates:
                candidates.append(s)
    staged = [i for i in (pending.stage(scrub_all(c), source="claude-code-user-directive", session=sid) for c in candidates[:8]) if i]

    def bullets(items, empty="- ثبت نشده"):
        return "\n".join("- " + scrub_all(str(i)).replace("\n", " ")[:400] for i in items) if items else empty

    last_users = [t["text"].strip().replace("\n", " ")[:350] for t in users[-6:]]
    last_assistant = scrub_all(assistants[-1]["text"].strip())[:900] if assistants else ""
    project = Path(cwd.replace("\\", "/")).name if cwd else ""
    note = (f"---\ntitle: Claude Code checkpoint — {sid[:8]} — {now_iso()}\nconversation: {sid}\nproject: {project}\n"
            f"updated: {now_iso()}\nsource: claude-code-hook\ntype: auto-checkpoint\ntrigger: {trigger}\n{chat_front(sid)}---\n\n"
            f"# چک‌پوینت خودکار Claude Code\n\n- جلسه: {sid}\n- مسیر کار: {scrub_all(cwd)}\n"
            f"- نوبت‌های ثبت‌شده تا اینجا: {len(turns_all)} (کاربر: {len(users)}، دستیار: {len(assistants)})\n\n"
            f"## آخرین درخواست‌های کاربر\n{bullets(last_users)}\n\n"
            f"## آخرین پاسخ دستیار (گزیده)\n{last_assistant or '- ثبت نشده'}\n\n"
            f"## فایل‌های تغییرداده‌شده\n{bullets(files[-25:])}\n\n"
            f"## کارهای باز (از TodoWrite)\n{bullets(open_tasks)}\n\n"
            f"## ترجیح/تصمیم‌های پیشنهادی (در انتظار تأیید در 30-Pending)\n{bullets([f'{i}: {c}' for i, c in zip(staged, candidates)]) if staged else '- موردی نبود'}\n\n"
            f"## محدودیت شواهد\nاین چک‌پوینت استخراجی (heuristic) است، نه خلاصهٔ تأییدشده. "
            f"پیش از اتکا، با فایل‌های واقعی و پیام‌های جدید کاربر تطبیق بده.\n")
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:10]
    target = CHECK_ROOT / f"session-{sid}" / f"{stamp()}-{digest}.md"
    return target if write_new(target, note) else None


def handle(event: str, data: dict) -> dict:
    sid = str(data.get("session_id") or "")
    transcript = data.get("transcript_path")
    cwd = str(data.get("cwd") or "")
    if not SID.fullmatch(sid):
        return {"skipped": "bad-session-id"}
    if not isinstance(transcript, str) or not os.path.isfile(transcript):
        return {"skipped": "no-transcript"}
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT offset,last_run,turns,cp_turns FROM sessions WHERE session_id=?", (sid,)).fetchone()
        offset, last_run, total, cp_turns = row if row else (0, 0.0, 0, 0)
        if event == "Stop" and not os.environ.get("INFINITE_CTX_NO_THROTTLE"):
            grown = os.path.getsize(transcript) - offset
            if now - last_run < THROTTLE_SECONDS or grown < THROTTLE_MIN_BYTES:
                return {"skipped": "throttled"}
        turns, new_offset = read_new(transcript, offset)
        chunk = write_chunk(sid, cwd, turns)
        total += len(turns)
        made_cp = None
        want_cp = event in ("PreCompact", "SessionEnd") or (event == "Stop" and total - cp_turns >= STOP_CHECKPOINT_EVERY)
        if want_cp and total > 0:
            all_turns, _ = read_new(transcript, 0)
            made_cp = build_checkpoint(sid, cwd, event, all_turns)
            cp_turns = total
        conn.execute("INSERT OR REPLACE INTO sessions(session_id,transcript,offset,last_run,turns,cp_turns) VALUES (?,?,?,?,?,?)",
                     (sid, transcript, new_offset, now, total, cp_turns))
        conn.commit()
    return {"new_turns": len(turns), "chunk": chunk.name if chunk else None,
            "checkpoint": made_cp.name if made_cp else None}
