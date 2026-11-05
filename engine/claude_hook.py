"""Single Claude Code hook entrypoint for Infinite Context.

  python claude_hook.py   (hook JSON on stdin)

UserPromptSubmit / SessionStart -> topic recall (+ same-session checkpoint after compact/resume)
PreCompact / SessionEnd / Stop  -> capture both sides of the session, build checkpoints
Never blocks the session: always exits 0, errors go to logs/errors.log.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
VAULT = Path(os.environ.get("INFINITE_CTX_VAULT") or HERE.parent)
LOGS = VAULT / ".ai-memory" / "logs"
MAX_CONTEXT = 3600


def log(name: str, record: dict) -> None:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        path = LOGS / name
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(path.suffix + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def session_checkpoint(sid: str) -> str:
    folder = VAULT / "20-Checkpoints" / f"session-{sid}"
    if not sid or not folder.is_dir():
        return ""
    files = sorted(folder.glob("*.md"))
    if not files:
        return ""
    text = files[-1].read_text(encoding="utf-8-sig")
    return text[:1800]


NOTICE_MAX = 600


def autosync_notice(data: dict) -> str:
    """One short line from autosync-notice.md, only if it is newer than what this session was already told."""
    try:
        engine = VAULT / ".ai-memory"
        text = (engine / "autosync-notice.md").read_text(encoding="utf-8-sig")
        generated = inject = ""
        for line in text.splitlines():
            if line.startswith("generated:"):
                generated = line.split(":", 1)[1].strip()
            elif line.startswith("inject:"):
                inject = line.split(":", 1)[1].strip()
        if not generated or not inject:
            return ""
        seen_path = LOGS / "autosync-notice-seen.json"
        try:
            seen = json.loads(seen_path.read_text(encoding="utf-8"))
            seen = seen if isinstance(seen, dict) else {}
        except (OSError, ValueError):
            seen = {}
        sid = str(data.get("session_id") or "-")[:80]
        if str(seen.get(sid) or "") >= generated:
            return ""
        seen.pop(sid, None)
        seen[sid] = generated
        while len(seen) > 300:
            seen.pop(next(iter(seen)))
        LOGS.mkdir(parents=True, exist_ok=True)
        tmp = seen_path.with_name(seen_path.name + ".tmp")
        tmp.write_text(json.dumps(seen), encoding="utf-8")
        os.replace(tmp, seen_path)
        from redact import scrub_all
        return ("[AUTOSYNC] " + scrub_all(inject))[:NOTICE_MAX]
    except Exception:
        return ""


def recall(data: dict) -> dict | None:
    import topic_router
    from redact import scrub_all
    event = str(data.get("hook_event_name") or "")
    parts = []
    routed = topic_router.codex_hook(data)
    if routed:
        parts.append(routed["hookSpecificOutput"]["additionalContext"])
    if event == "SessionStart" and str(data.get("source") or "") in ("compact", "resume"):
        cp = session_checkpoint(str(data.get("session_id") or ""))
        if cp:
            parts.insert(0, "[SESSION CHECKPOINT — historical, extractive, verify against files]\n" + scrub_all(cp))
    notice = autosync_notice(data) if event == "SessionStart" else ""
    chat = chat_context(data) if event == "SessionStart" else ""
    if not parts and not notice and not chat:
        return None
    # the chat tag and the autosync notice are short and must never be the part that gets cut off: reserve their room first
    reserved = [x for x in (chat, notice) if x]
    body = "\n\n\n".join(parts)[:max(0, MAX_CONTEXT - sum(len(x) + 3 for x in reserved))]
    text = "\n\n\n".join(x for x in ([chat] if chat else []) + ([body] if body else []) + ([notice] if notice else []))
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:MAX_CONTEXT]}}


def chat_context(data: dict) -> str:
    """This chat's tag, and chats of the same project that were cut off (chats.py). A hook must never break a session."""
    try:
        import chats
        sid = str(data.get("session_id") or "")
        fresh = str(data.get("source") or "") in ("startup", "clear", "")
        return chats.Registry(VAULT).hook_context("claude-code", sid, str(data.get("cwd") or ""), fresh=fresh)[:900]
    except Exception:
        return ""


def refresh_handoff():
    """Rebuild the cross-agent packs (Hamzaban) when a chat ends. Optional: a missing package or any error is harmless."""
    try:
        from hamzaban import outbox
    except ImportError:
        return "not-installed"
    res = outbox.refresh_quiet(VAULT, force=True, topics=False)  # only the latest pack: fast; autosync does the rest
    return "failed" if res is None else {"written": len(res["written"]), "ingested": res["ingested"]}


def main() -> int:
    started = time.time()
    record = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        data = json.loads(sys.stdin.read(2_000_000))
        event = str(data.get("hook_event_name") or "")
        record.update(event=event, session=str(data.get("session_id") or "")[:12], source=str(data.get("source") or data.get("trigger") or ""))
        output = None
        if event in ("UserPromptSubmit", "SessionStart"):
            output = recall(data)
            record["injected_chars"] = len(output["hookSpecificOutput"]["additionalContext"]) if output else 0
        elif event in ("PreCompact", "SessionEnd", "Stop"):
            import capture_session
            record["result"] = capture_session.handle(event, data)
            if event == "SessionEnd":
                record["hamzaban"] = refresh_handoff()
        if output:
            print(json.dumps(output, ensure_ascii=False))
    except Exception as exc:  # never block a real session
        record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        log("errors.log", record)
    record["ms"] = int((time.time() - started) * 1000)
    log("hook-events.jsonl", record)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
