"""Capture sent Chat On Steroids user messages and session checkpoints.

Uses the app's *existing* local session records. No browser injection, no
network, no writes to the Chat On Steroids app directory. Read-only input.
Assistant replies are NOT represented in session-input.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

VAULT = Path(__file__).resolve().parent.parent
COS = Path(os.environ.get("COS_APP_DATA", str(Path.home() / "AppData/Roaming/chat-on-steroids")))
CAPTURE_ROOT = VAULT / "40-Conversation-Capture"
STATE_DIR = VAULT / ".ai-memory"
DB_PATH = STATE_DIR / "capture-state.sqlite3"
SUMMARY_LIMIT = 1200
MAX_MESSAGE = 100_000
UUID = re.compile(r"^[a-zA-Z0-9_-]{6,90}$")
SENSITIVE = (
    (re.compile(r"(?i)(\b(?:sk-[a-zA-Z0-9_-]{16,}|gh[pousr]_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,})\b)"), "[REDACTED-CREDENTIAL]"),
    (re.compile(r"(?im)\b(authorization\s*:\s*bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?im)\b(password|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret)\b(\s*[:=]\s*)([^\s\"']+)"), r"\1\2[REDACTED]"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
)


def utc_ms(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown"


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_id(value: object) -> str:
    value = str(value or "")
    if not UUID.fullmatch(value):
        raise ValueError("Unsafe conversation identifier")
    return value


def scrub(value: str) -> str:
    for regex, replacement in SENSITIVE:
        value = regex.sub(replacement, value)
    return value


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def db_open():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, conversation TEXT NOT NULL, content_hash TEXT NOT NULL, saved_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS pressure (conversation TEXT PRIMARY KEY, recorded_token INTEGER NOT NULL, triggered_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS progress (conversation TEXT PRIMARY KEY, session TEXT NOT NULL, turn TEXT NOT NULL, saved_at TEXT NOT NULL)")
    return conn


def write_new(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
        return True
    except FileExistsError:
        return False


def recent_sessions() -> dict[str, dict]:
    result: dict[str, dict] = {}
    folder = COS / "sessions"
    if not folder.is_dir():
        return result
    for item in folder.iterdir():
        if not item.is_dir():
            continue
        meta = read_json(item / "meta.json")
        if not isinstance(meta, dict):
            continue
        conversation = meta.get("conversationId")
        if not isinstance(conversation, str) or not UUID.fullmatch(conversation):
            continue
        old = result.get(conversation)
        if not old or int(meta.get("updatedAt") or 0) > int(old.get("updatedAt") or 0):
            result[conversation] = {"meta": meta, "folder": item, "updatedAt": meta.get("updatedAt", 0)}
    return result


def capture_inputs(conn: sqlite3.Connection) -> tuple[int, int]:
    items = read_json(COS / "state" / "session-input.json")
    if not isinstance(items, list):
        return 0, 0
    saved, skipped = 0, 0
    for item in items:
        if not isinstance(item, dict) or item.get("state") != "sent":
            continue
        try:
            conversation = safe_id(item.get("conversationId"))
            ident = safe_id(item.get("id"))
        except ValueError:
            skipped += 1
            continue
        body = item.get("text")
        if not isinstance(body, str) or not body.strip():
            continue
        key = conversation + ":" + ident
        if conn.execute("SELECT 1 FROM seen WHERE id=?", (key,)).fetchone():
            continue
        redacted = scrub(body)
        limited = redacted[:MAX_MESSAGE]
        extra = "\n\n[TRUNCATED: message exceeded safe capture length]" if len(redacted) > MAX_MESSAGE else ""
        written = utc_ms(item.get("createdAt"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        folder = CAPTURE_ROOT / conversation
        note = (f"---\ntitle: User message — {written}\n"
                f"conversation: {conversation}\nmessage_id: {ident}\n"
                f"updated: {written}\nsource: cos-session-input\n"
                f"role: user\nstatus: sent\n---\n\n"
                f"# User message\n\n{limited}{extra}\n")
        target = folder / f"message-{ident}.md"
        write_new(target, note)
        conn.execute(
            "INSERT INTO seen(id,conversation,content_hash,saved_at) VALUES (?,?,?,?)",
            (key, conversation, digest, now_stamp()),
        )
        saved += 1
    conn.commit()
    return saved, skipped


MAX_SHARD_BYTES = 2_000_000
SHARD_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
MAX_SESSIONS = 50


def all_sessions() -> list[tuple[str, Path, dict]]:
    """(conversation key, folder, meta) for the newest sessions. A session without a conversationId (newer app
    versions) is keyed by its own session id, so its messages are still captured."""
    folder = COS / "sessions"
    found = []
    if not folder.is_dir():
        return found
    for item in folder.iterdir():
        meta = read_json(item / "meta.json") if item.is_dir() else None
        if not isinstance(meta, dict):
            continue
        key = meta.get("conversationId")
        if not isinstance(key, str) or not UUID.fullmatch(key):
            key = item.name if UUID.fullmatch(item.name) else None
        if key:
            found.append((key, item, meta))
    found.sort(key=lambda row: int(row[2].get("updatedAt") or 0), reverse=True)
    return found[:MAX_SESSIONS]


def session_user_messages(folder: Path) -> list[dict]:
    """Canonical user messages of one session, as the app stores them: messages.json (key -> event) and
    messages/<sha256(key)>.json shards. Events whose key does not match their messageId are ignored, like the app does."""
    events: dict[str, dict] = {}

    def consider(event: object, name_ok) -> None:
        if not isinstance(event, dict) or event.get("kind") != "user_message":
            return
        mid = event.get("messageId")
        if not isinstance(mid, str) or not mid:
            return
        key = "user_message\0" + mid
        if name_ok(key):
            events[key] = event

    whole = read_json(folder / "messages.json")
    if isinstance(whole, dict):
        for stored_key, event in whole.items():
            consider(event, lambda key, stored_key=stored_key: key == stored_key)
    shards = folder / "messages"
    if shards.is_dir():
        for path in sorted(shards.iterdir()):
            if not SHARD_NAME.fullmatch(path.name):
                continue
            try:
                if path.stat().st_size > MAX_SHARD_BYTES:
                    continue
            except OSError:
                continue
            consider(read_json(path), lambda key, name=path.name: hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json" == name)
    return sorted(events.values(), key=lambda e: int(e["seq"]) if isinstance(e.get("seq"), (int, float)) else 0)


def capture_messages(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """New capture path: user messages from sessions/*/messages.json. Returns (saved, skipped, uncaptured), where
    uncaptured = user messages the app says it has (meta.userMessages) minus those we hold: a silent-failure alarm."""
    saved = skipped = uncaptured = 0
    for conversation, folder, meta in all_sessions():
        for event in session_user_messages(folder):
            try:
                ident = safe_id(event["messageId"])
            except ValueError:
                skipped += 1
                continue
            stored = event.get("message")
            body = stored.get("text") if isinstance(stored, dict) else stored
            if not isinstance(body, str) or not body.strip():
                continue
            key = conversation + ":" + ident
            if conn.execute("SELECT 1 FROM seen WHERE id=?", (key,)).fetchone():
                continue
            redacted = scrub(body)
            cut = len(redacted) > MAX_MESSAGE or (isinstance(stored, dict) and stored.get("truncated"))
            extra = "\n\n[TRUNCATED: message exceeded safe capture length]" if cut else ""
            written = utc_ms(event.get("authoredAt") or event.get("time"))
            note = (f"---\ntitle: User message — {written}\n"
                    f"conversation: {conversation}\nmessage_id: {ident}\n"
                    f"updated: {written}\nsource: cos-session-messages\n"
                    f"role: user\nstatus: recorded\n---\n\n"
                    f"# User message\n\n{redacted[:MAX_MESSAGE]}{extra}\n")
            write_new(CAPTURE_ROOT / conversation / f"message-{ident}.md", note)
            conn.execute("INSERT INTO seen(id,conversation,content_hash,saved_at) VALUES (?,?,?,?)",
                         (key, conversation, hashlib.sha256(body.encode("utf-8")).hexdigest(), now_stamp()))
            saved += 1
        try:
            claimed = int(meta.get("userMessages") or 0)
        except (ValueError, TypeError):
            claimed = 0
        held = conn.execute("SELECT COUNT(*) FROM seen WHERE conversation=?", (conversation,)).fetchone()[0]
        uncaptured += max(0, claimed - held)
    conn.commit()
    return saved, skipped, uncaptured


def config_threshold() -> int:
    config = read_json(COS / "config.json") or {}
    compaction = config.get("compaction") or {}
    sessions = config.get("sessions") or {}
    try:
        max_tokens = int(compaction.get("autoTokens") or sessions.get("advisoryTokens") or 400000)
        return max(5000, int(max_tokens * 0.8))
    except (ValueError, TypeError):
        return 320000


def latest_user_excerpts(conversation: str, count: int = 4) -> str:
    inputs = read_json(COS / "state" / "session-input.json")
    if not isinstance(inputs, list):
        return "- No saved input record"
    filtered = sorted(
        (x for x in inputs if isinstance(x, dict) and x.get("conversationId") == conversation
         and x.get("state") == "sent" and isinstance(x.get("text"), str)),
        key=lambda x: int(x.get("createdAt") or 0),
    )
    output = []
    for item in filtered[-count:]:
        snippet = scrub(item["text"]).replace("\r", " ").strip()[:SUMMARY_LIMIT]
        output.append(f"- {utc_ms(item.get('createdAt'))} (user): {snippet}")
    return "\n".join(output) if output else "- No saved input record"


def completed_turn_checkpoint(conn: sqlite3.Connection, conversation: str, entry: dict) -> int:
    meta = entry["meta"]
    timeline = meta.get("timelineTurns")
    if not isinstance(timeline, dict):
        return 0
    completed = [
        (int(turn.get("endTime")), ident, turn)
        for ident, turn in timeline.items()
        if isinstance(turn, dict) and turn.get("endTime")
    ]
    if not completed:
        return 0
    completed.sort(reverse=True)
    end_time, turn_id, turn_data = completed[0]
    previous = conn.execute("SELECT turn FROM progress WHERE conversation=?", (conversation,)).fetchone()
    if previous and previous[0] == turn_id:
        return 0
    last_excerpt = latest_user_excerpts(conversation)
    last_assistant_excerpt = scrub(str(meta.get("lastAnswerLine") or "")).strip()[:600]
    stamp = now_stamp()
    note = (f"---\ntitle: Automatic turn checkpoint — {utc_ms(end_time)}\n"
            f"conversation: {conversation}\nsource: cos-session-record-plus-input\n"
            f"updated: {stamp}\ntype: auto-turn-checkpoint\n"
            f"---\n\n# Automatic turn checkpoint\n\n"
            f"- Session: {entry['folder'].name}\n"
            f"- Completed turn: {turn_id}\n"
            f"- Turn completed: {utc_ms(end_time)}\n"
            f"- Tool call count in the session: {int(meta.get('toolCalls') or 0)}\n"
            f"- Estimated context tokens: {int(meta.get('contextTokens') or 0)}\n"
            f"- Outcome: {str(meta.get('lastTurnOutcome') or 'unknown')[:80]}\n\n"
            f"## Latest recorded user messages\n\n{last_excerpt}\n\n"
            + (f"## Last assistant answer line (partial)\n\n{last_assistant_excerpt}\n\n" if last_assistant_excerpt else "")
            + f"## Evidence limits\n\n"
            f"These are user prompts and CoS metadata, not an assistant-generated summary of completed work. "
            f"Assistant responses are not available in this session-input archive. "
            f"Check source project files and tool results to establish what really happened.\n")
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:10]
    target = VAULT / "20-Checkpoints" / ("session-" + conversation) / (
        f"turn-{end_time}-{digest}.md")
    write_new(target, note)
    conn.execute("INSERT OR REPLACE INTO progress(conversation,session,turn,saved_at) VALUES (?,?,?,?)",
                 (conversation, entry["folder"].name, turn_id, stamp))
    conn.commit()
    return 1


def inspect_session(conn: sqlite3.Connection, conversation: str, entry: dict, threshold: int) -> int:
    meta = entry["meta"]
    folder = entry["folder"]
    session_id = folder.name
    try:
        tokens = int(meta.get("contextTokens") or 0)
    except (ValueError, TypeError):
        tokens = 0
    turn = str(meta.get("activeTurnId") or "")
    if not tokens:
        return 0
    last = conn.execute("SELECT recorded_token FROM pressure WHERE conversation=?", (conversation,)).fetchone()
    # Write one checkpoint once threshold is crossed; repeats only on material growth.
    near_limit = tokens >= threshold and (not last or tokens - last[0] >= 40_000)
    # Also checkpoint on a completed turn after a prior threshold entry.
    if not near_limit:
        return 0
    ts = now_stamp()
    num_messages = conn.execute("SELECT COUNT(*) FROM seen WHERE conversation=?", (conversation,)).fetchone()[0]
    tool_calls = int(meta.get("toolCalls") or 0)
    outcome = str(meta.get("lastTurnOutcome") or "unknown")[:70]
    note = (f"---\ntitle: Automatic context pressure checkpoint — {ts}\n"
            f"conversation: {conversation}\nsource: cos-session-metadata\n"
            f"updated: {ts}\ntype: auto-checkpoint\n"
            f"---\n\n# Automatic context checkpoint\n\n"
            f"- Session: {session_id}\n- Conversation: {conversation}\n"
            f"- Context tokens reported by CoS: {tokens}\n- Warning threshold: {threshold}\n"
            f"- Captured sent-user messages: {num_messages}\n"
            f"- Recorded tool call count: {tool_calls}\n- Last turn outcome: {outcome}\n"
            f"- Active turn ID: {turn}\n"
            f"\n## Latest recorded user messages\n\n{latest_user_excerpts(conversation)}\n\n"
            f"- Full assistant responses are not present in CoS local session-input; this is a metadata checkpoint, not a verified project summary.\n"
            f"- For continuation, retrieve files under `40-Conversation-Capture/{conversation}/`, then check actual project files and `20-Checkpoints/`.\n")
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()[:10]
    target = VAULT / "20-Checkpoints" / ("session-" + conversation) / (ts.replace("-", "").replace(":", "") + "-" + digest + ".md")
    if write_new(target, note):
        conn.execute("INSERT OR REPLACE INTO pressure VALUES (?,?,?)", (conversation, tokens, ts))
        conn.commit()
        return 1
    return 0


def capture_cycle() -> dict[str, int]:
    with db_open() as conn:
        messages, skipped = capture_inputs(conn)          # older app versions: state/session-input.json
        more, more_skipped, uncaptured = capture_messages(conn)  # newer: sessions/*/messages.json
        messages, skipped = messages + more, skipped + more_skipped
        sessions = recent_sessions()
        turns = sum(completed_turn_checkpoint(conn, cid, entry) for cid, entry in sessions.items())
        checkpoints = sum(inspect_session(conn, cid, entry, config_threshold()) for cid, entry in sessions.items())
        count = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    return {"new_messages": messages, "skipped": skipped, "turn_checkpoints": turns,
            "pressure_checkpoints": checkpoints, "total": count, "uncaptured": uncaptured}


def status() -> dict:
    with db_open() as conn:
        total = conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        conversations = conn.execute("SELECT COUNT(DISTINCT conversation) FROM seen").fetchone()[0]
        alerts = conn.execute("SELECT COUNT(*) FROM pressure").fetchone()[0]
    return {"messages": total, "conversations": conversations, "pressure_alerts": alerts, "running": live_pid()}


def pid_file() -> Path:
    return STATE_DIR / "capture-service.pid"


def live_pid() -> int | None:
    if not pid_file().exists():
        return None
    try:
        pid = int(pid_file().read_text(encoding="ascii").strip())
        if os.name == "nt":
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            kernel.CloseHandle(handle)
        else:
            os.kill(pid, 0)
        return pid
    except (ValueError, OSError, SystemError):
        return None


def watch(interval: int):
    existing = live_pid()
    if existing and existing != os.getpid():
        raise ValueError(f"Capture service already running with PID {existing}")
    pid_file().write_text(str(os.getpid()), encoding="ascii")
    try:
        while True:
            try:
                result = capture_cycle()
                (STATE_DIR / "capture-service-status.json").write_text(
                    json.dumps({"timestamp": now_stamp(), **result}, ensure_ascii=False), encoding="utf-8")
            except (OSError, sqlite3.Error, ValueError) as exc:
                with (STATE_DIR / "capture-service-errors.log").open("a", encoding="utf-8") as log:
                    log.write(f"{now_stamp()} {type(exc).__name__}: {str(exc)[:300]}\n")
            time.sleep(interval)
    finally:
        if pid_file().exists() and pid_file().read_text(encoding="ascii").strip() == str(os.getpid()):
            pid_file().unlink()


def main():
    parser = argparse.ArgumentParser(description="Continuous, local CoS → Obsidian user-message memory")
    parser.add_argument("action", choices=["once", "watch", "status"])
    parser.add_argument("--interval", type=int, default=20)
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    elif args.action == "once":
        result = capture_cycle()
        # Heartbeat: proves the scheduled task really ran (previously only `watch` wrote this file).
        (STATE_DIR / "capture-service-status.json").write_text(
            json.dumps({"timestamp": now_stamp(), **result}, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    else:
        watch(max(5, args.interval))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        try:  # pythonw has no console: without this a failing scheduled task is silent.
            with (STATE_DIR / "capture-service-errors.log").open("a", encoding="utf-8") as log:
                log.write(f"{now_stamp()} {type(exc).__name__}: {str(exc)[:300]}\n")
        except OSError:
            pass
        sys.exit(1)
