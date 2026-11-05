"""Readers for the chat stores of the agents on this machine. READ-ONLY: nothing here writes to any agent's folder.

Each reader turns one chat's log into a few neutral events and is INCREMENTAL: it gets the byte cursor where the last
pass stopped (and a small scratch dict it may keep), and returns (events, new_cursor, scratch). A reader whose store
cannot be read incrementally returns {"k": "reset"} first, meaning "forget what you hold about this chat, here is all of it".

Events (dicts, short keys):
  {"k": "meta", "title", "cwd", "kind": "main"|"subagent", "entry"}
  {"k": "user", "ts", "text"}                       a message the PERSON wrote (never an injected reminder)
  {"k": "assistant", "ts", "text", "final"}         what the agent said back
  {"k": "tool", "ts", "name", "target"}             an edit of a file (Edit/Write/...)
  {"k": "plan", "ts", "items": [{"content","status"}]}   the agent's own to-do list as it stands now
  {"k": "mark", "ts", "name", "detail"}             turn_start | turn_done | turn_aborted | limit | error
                                                    (a limit also carries "until": epoch seconds when it opens again, when the log says)
  {"k": "alive", "ts"}                              the agent produced something: an earlier error was only a hiccup
  {"k": "close", "ts", "topic", "text"}             a chat that ended with a hamzaban-close block
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SID = re.compile(r"^[A-Za-z0-9_-]{6,90}$")
MAX_READ = 24 * 1024 * 1024            # bytes of one log read per chat per pass
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_NOTICE_LEAD = re.compile(r"\A\s*(?:\[Request interrupted by user[^\]\n]*\]|The app was quit while you were working\.?|پاسخ قبلی متوقف شده\.?)\s*", re.I)


def split_notice(text: str) -> tuple[str, str]:
    """(notice, rest). The app writes a line like '[Request interrupted by user]' or 'پاسخ قبلی متوقف شده' when a turn stopped
    half-way, sometimes in front of what the person typed next: it is evidence of a cut, not part of their message."""
    m = _NOTICE_LEAD.match(text or "")
    return (m.group(0).strip(), text[m.end():].strip()) if m else ("", text or "")
LIMIT_RE = re.compile(r"(?i)(usage limit|rate limit|limit (?:reached|will reset)|hit your limit|you(?:'ve| have) reached|"
                      r"prompt is too long|context (?:window|length|limit)|max[_ ]?tokens|credit balance|token limit|quota)")


def _suite():
    import suite
    return suite


def _iso_ms(value) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _plan_apply(plan: list, inp: dict) -> list:
    """The harness's to-do tool sends either the whole list ("items") or small moves (advance/done/start)."""
    plan = [dict(p) for p in plan]
    if isinstance(inp.get("items"), list):
        plan = [{"content": str(i.get("content") or "")[:200], "status": str(i.get("status") or "pending")}
                for i in inp["items"] if isinstance(i, dict)]
    if inp.get("advance") and plan:
        cur = next((k for k, i in enumerate(plan) if i["status"] == "in_progress"), None)
        if cur is not None:
            plan[cur]["status"] = "completed"
        nxt = next((k for k, i in enumerate(plan) if i["status"] == "pending"), None)
        if nxt is not None:
            plan[nxt]["status"] = "in_progress"
    for n in inp.get("done") or []:
        if isinstance(n, int) and 1 <= n <= len(plan):
            plan[n - 1]["status"] = "completed"
    if isinstance(inp.get("start"), int) and 1 <= inp["start"] <= len(plan):
        plan[inp["start"] - 1]["status"] = "in_progress"
    return plan


def _read_chunk(path: Path, cursor: int):
    """(complete lines as bytes, new cursor, reset?) of the next piece of an append-only log."""
    st = _stat(path)
    if st is None:
        return [], cursor, False
    reset = False
    if cursor > st.st_size:
        cursor, reset = 0, True
    with open(path, "rb") as fh:
        fh.seek(cursor)
        data = fh.read(MAX_READ)
    end = data.rfind(b"\n")
    if end < 0:
        # one line longer than the whole read window would block this log for ever: step over it
        return [], cursor + (len(data) if len(data) >= MAX_READ else 0), reset
    return data[: end + 1].splitlines(), cursor + end + 1, reset


# ---------------------------------------------------------------------------------------------------- Claude Code
class ClaudeCode:
    agent = "claude-code"

    def root(self) -> Path:
        return _suite().user_home() / ".claude" / "projects"

    def discover(self, horizon: float) -> list[dict]:
        out = []
        try:
            files = list(self.root().glob("*/*.jsonl"))
        except OSError:
            return out
        for p in files:
            st = _stat(p)
            if st is None or st.st_mtime < horizon or not SID.fullmatch(p.stem):
                continue
            out.append({"source_id": p.stem, "path": str(p), "size": st.st_size, "mtime": st.st_mtime})
        return out

    def read(self, info: dict, cursor: int, scratch: dict):
        from capture_session import flatten, strip_injected
        lines, new_cursor, reset = _read_chunk(Path(info["path"]), cursor)
        sc = {} if reset else dict(scratch or {})
        events: list[dict] = [{"k": "reset"}] if reset else []
        open_tools = set(sc.get("open_tools", []))
        plan = list(sc.get("plan", []))
        for raw in lines:
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("isSidechain"):
                continue
            t, ts = r.get("type"), str(r.get("timestamp") or "")
            if not sc.get("meta") and r.get("cwd"):
                events.append({"k": "meta", "cwd": str(r["cwd"]), "kind": "main", "entry": str(r.get("entrypoint") or "")})
                sc["meta"] = True
            if t == "custom-title" and r.get("customTitle"):
                events.append({"k": "meta", "title": str(r["customTitle"])[:120]})
            elif t == "summary" and r.get("summary") and not sc.get("title_from_summary"):
                events.append({"k": "meta", "title": str(r["summary"])[:120], "weak": True})
                sc["title_from_summary"] = True
            elif t == "user":
                if r.get("isMeta") or r.get("isCompactSummary"):
                    continue
                msg = r.get("message") if isinstance(r.get("message"), dict) else {}
                content = msg.get("content", r.get("content"))
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            open_tools.discard(b.get("tool_use_id"))
                text, _tools, only_results = flatten(content)
                if only_results:
                    continue
                notice, text = split_notice(strip_injected(text))
                if notice:
                    events.append({"k": "mark", "ts": ts, "name": "turn_aborted", "detail": notice[:80]})
                if text.strip():
                    events.append({"k": "user", "ts": ts, "text": text})
            elif t == "attachment":
                att = r.get("attachment")
                # a message the person sent while the agent was still working arrives as a "queued_command" attachment
                if isinstance(att, dict) and att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
                    text = strip_injected(att["prompt"])
                    if text.strip():
                        events.append({"k": "user", "ts": ts, "text": text, "queued": True})
            elif t == "assistant":
                msg = r.get("message") if isinstance(r.get("message"), dict) else {}
                content = msg.get("content")
                blocks = content if isinstance(content, list) else ([{"type": "text", "text": content}] if isinstance(content, str) else [])
                texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(x for x in texts if isinstance(x, str)).strip()
                for b in blocks:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    if b.get("id"):
                        open_tools.add(b["id"])
                    name, inp = str(b.get("name") or ""), b.get("input") if isinstance(b.get("input"), dict) else {}
                    if name in EDIT_TOOLS and (inp.get("file_path") or inp.get("notebook_path")):
                        events.append({"k": "tool", "ts": ts, "name": name, "target": str(inp.get("file_path") or inp.get("notebook_path"))})
                    elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                        plan = [{"content": str(x.get("content") or x.get("activeForm") or "")[:200], "status": str(x.get("status") or "pending")}
                                for x in inp["todos"] if isinstance(x, dict)]
                        events.append({"k": "plan", "ts": ts, "items": plan})
                    elif name.endswith("update_plan"):
                        plan = _plan_apply(plan, inp)
                        events.append({"k": "plan", "ts": ts, "items": plan})
                stop = msg.get("stop_reason")
                cut = isinstance(stop, str) and stop in ("max_tokens", "model_context_window_exceeded")
                if r.get("isApiErrorMessage"):
                    # the app's own error text ("You've hit your limit..."), not something the agent said
                    detail = (text or str(stop or ""))[:200]
                    events.append({"k": "mark", "ts": ts, "name": "limit" if (cut or LIMIT_RE.search(detail)) else "error", "detail": detail})
                else:
                    if blocks:   # the agent is producing something again: an error row before this one was only a hiccup
                        events.append({"k": "alive", "ts": ts})
                    if text:
                        events.append({"k": "assistant", "ts": ts, "text": text, "final": stop == "end_turn"})
                    if cut:
                        events.append({"k": "mark", "ts": ts, "name": "limit", "detail": str(stop)})
            elif t == "system" and (r.get("level") == "error" or "error" in str(r.get("subtype") or "")):
                detail = str(r.get("content") or r.get("subtype") or "")[:200]
                events.append({"k": "mark", "ts": ts, "name": "limit" if LIMIT_RE.search(detail) else "error", "detail": detail})
        sc["open_tools"], sc["plan"] = sorted(open_tools)[:50], plan[:40]
        return events, new_cursor, sc


# ---------------------------------------------------------------------------------------------------- Codex
_CODEX_NAME = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)$")
_CODEX_INJECTED = re.compile(r"\A\s*(?:<environment_context>|<user_instructions>|<INSTRUCTIONS>|<turn_aborted>|<permissions|<skills|"
                             r"<collaboration|<user_action>|<subagent|<system-reminder>|<recent_conversation>|# AGENTS\.md instructions)", re.I)
_CODEX_WANT = (b'"session_meta"', b"UserMessage", b"AgentMessage", b'"task_started"', b'"task_complete"', b'"turn_aborted"')
_CODEX_REQUEST = re.compile(r"##\s*My request for Codex:\s*(.*)\Z", re.S)
_WINDOW_FULL = re.compile(rb'"used_percent"\s*:\s*(?:100|[1-9]\d\d)')   # a usage window (5 hours, a week) is used up


def _epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class Codex:
    agent = "codex"

    def root(self) -> Path:
        return _suite().user_home() / ".codex" / "sessions"

    def discover(self, horizon: float) -> list[dict]:
        out = []
        try:
            files = list(self.root().rglob("rollout-*.jsonl"))
        except OSError:
            return out
        for p in files:
            st = _stat(p)
            m = _CODEX_NAME.match(p.stem)
            if st is None or m is None or st.st_mtime < horizon or not SID.fullmatch(m.group(1)):
                continue
            out.append({"source_id": m.group(1), "path": str(p), "size": st.st_size, "mtime": st.st_mtime})
        return out

    @staticmethod
    def _texts(content) -> str:
        if isinstance(content, str):
            return content
        return "\n".join(c.get("text", "") for c in (content or []) if isinstance(c, dict) and isinstance(c.get("text"), str)).strip()

    def read(self, info: dict, cursor: int, scratch: dict):
        lines, new_cursor, reset = _read_chunk(Path(info["path"]), cursor)
        sc = {} if reset else dict(scratch or {})
        events: list[dict] = [{"k": "reset"}] if reset else []
        for raw in lines:
            if not any(w in raw for w in _CODEX_WANT):
                if b'"token_count"' not in raw:
                    continue
                reached = b'"rate_limit_reached_type"' in raw and not (
                    b'"rate_limit_reached_type":null' in raw or b'"rate_limit_reached_type": null' in raw)
                if not (reached or _WINDOW_FULL.search(raw)):
                    continue   # the ordinary token count: nothing to learn from it
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            ts, p = str(r.get("timestamp") or ""), r.get("payload") if isinstance(r.get("payload"), dict) else {}
            if r.get("type") == "session_meta":
                src = p.get("source")
                events.append({"k": "meta", "cwd": str(p.get("cwd") or ""), "kind": "subagent" if isinstance(src, dict) else "main",
                               "entry": str(src if not isinstance(src, dict) else "subagent")})
                continue
            if r.get("type") != "event_msg":
                continue
            et = p.get("type")
            if et == "item_completed":
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                if item.get("type") == "UserMessage":
                    text = self._texts(item.get("content"))
                    mine = _CODEX_REQUEST.search(text)      # the VS Code extension lists attached files before the request
                    if mine:
                        text = mine.group(1).strip()
                    elif text.lstrip().startswith("# Files mentioned by the user"):
                        continue                              # attachments only: no request in this message
                    elif text.lstrip().startswith('{"'):
                        continue                              # a tool's structured answer echoed back, not the person
                    notice, text = split_notice(text)
                    if notice:
                        events.append({"k": "mark", "ts": ts, "name": "turn_aborted", "detail": notice[:80]})
                    if text.strip() and not _CODEX_INJECTED.match(text):
                        events.append({"k": "user", "ts": ts, "text": text})
                elif item.get("type") == "AgentMessage":
                    text = self._texts(item.get("content"))
                    if text.strip():
                        events.append({"k": "assistant", "ts": ts, "text": text, "final": item.get("phase") == "final_answer"})
                        sc["last_final"] = hash(text) if item.get("phase") == "final_answer" else sc.get("last_final")
            elif et == "task_started":
                events.append({"k": "mark", "ts": ts, "name": "turn_start", "detail": ""})
            elif et == "task_complete":
                events.append({"k": "mark", "ts": ts, "name": "turn_done", "detail": ""})
                last = p.get("last_agent_message")
                if isinstance(last, str) and last.strip() and hash(last) != sc.get("last_final"):
                    events.append({"k": "assistant", "ts": ts, "text": last, "final": True})
                if p.get("error"):   # the turn did not finish: usage limit, a refused request, a lost connection ...
                    events.append(self._cut(ts, p["error"], sc))
            elif et == "turn_aborted":
                events.append({"k": "mark", "ts": ts, "name": "turn_aborted", "detail": str(p.get("reason") or "")[:80]})
            elif et == "token_count":
                rl = p.get("rate_limits") if isinstance(p.get("rate_limits"), dict) else {}
                full = [w for w in (rl.get("primary"), rl.get("secondary"))
                        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)) and w["used_percent"] >= 100]
                if full:   # the next request will be refused: remember when the window opens again, for the error that follows
                    opens = [int(w["resets_at"]) for w in full if isinstance(w.get("resets_at"), (int, float))]
                    sc["window_full"] = {"ts": ts, "until": max(opens) if opens else 0}
                if rl.get("rate_limit_reached_type"):
                    mark = {"k": "mark", "ts": ts, "name": "limit", "detail": str(rl["rate_limit_reached_type"])[:80]}
                    if sc.get("window_full", {}).get("until"):
                        mark["until"] = sc["window_full"]["until"]
                    events.append(mark)
        return events, new_cursor, sc

    @staticmethod
    def _cut(ts: str, err, sc: dict) -> dict:
        """The mark for a turn that ended in an error. Codex writes it as task_complete.error = {message, codex_error_info};
        a usage limit says so there ("usage_limit_exceeded"), and the last token count before it says when it opens again."""
        info = str(err.get("codex_error_info") or "") if isinstance(err, dict) else ""
        text = str(err.get("message") or "") if isinstance(err, dict) else str(err)
        limit = bool(re.search(r"(?i)usage_limit|rate_limit|quota", info) or LIMIT_RE.search(text))
        mark = {"k": "mark", "ts": ts, "name": "limit" if limit else "error", "detail": (text or info)[:200]}
        until = (sc.get("window_full") or {}).get("until") or 0
        if limit and until and until >= _epoch(ts) - 60:   # a window that was already open again by then is not the reason
            mark["until"] = int(until)
        return mark


# ---------------------------------------------------------------------------------------------------- Chat On Steroids
def _cos_event_text(ev: dict) -> str:
    msg = ev.get("message")
    if isinstance(msg, dict):
        return msg.get("text") if isinstance(msg.get("text"), str) else ""
    return msg if isinstance(msg, str) else ""


class Cos:
    """sessions/<id>/messages.json (+ messages/<sha256>.json shards): the newer app keeps both sides of the chat here."""
    agent = "cos"

    def root(self) -> Path:
        return _suite().appdata() / "chat-on-steroids" / "sessions"

    def discover(self, horizon: float) -> list[dict]:
        out = []
        try:
            dirs = [d for d in self.root().iterdir() if d.is_dir() and SID.fullmatch(d.name)]
        except OSError:
            return out
        for d in dirs:
            stamps = [_stat(d / n) for n in ("meta.json", "messages.json")]
            stamps = [s for s in stamps if s]
            if not stamps:
                continue
            mtime = max(s.st_mtime for s in stamps)
            if mtime < horizon:
                continue
            shard_count = len(list((d / "messages").glob("*.json"))) if (d / "messages").is_dir() else 0
            out.append({"source_id": d.name, "path": str(d), "size": sum(s.st_size for s in stamps) + shard_count, "mtime": mtime})
        return out

    def read(self, info: dict, cursor: int, scratch: dict):
        from auto_capture import read_json
        d = Path(info["path"])
        events: list[dict] = [{"k": "reset"}]
        meta = read_json(d / "meta.json")
        meta = meta if isinstance(meta, dict) else {}
        events.append({"k": "meta", "title": str(meta.get("title") or "")[:120], "kind": "main", "entry": "chat-on-steroids"})
        found: dict[str, dict] = {}
        whole = read_json(d / "messages.json")
        if isinstance(whole, dict):
            found.update({k: v for k, v in whole.items() if isinstance(v, dict)})
        shards = d / "messages"
        if shards.is_dir():
            for p in sorted(shards.glob("*.json"))[:5000]:
                ev = read_json(p)
                if isinstance(ev, dict):
                    found[p.name] = ev
        rows = [e for e in found.values() if e.get("kind") in ("user_message", "assistant_message") and isinstance(e.get("seq"), (int, float))]
        for e in sorted(rows, key=lambda e: e["seq"]):
            text = _cos_event_text(e)
            if not text.strip():
                continue
            ts = _iso_ms(e.get("authoredAt") or e.get("time"))
            if e["kind"] == "user_message":
                events.append({"k": "user", "ts": ts, "text": text})
            else:
                events.append({"k": "assistant", "ts": ts, "text": text, "final": e.get("state", "final") == "final"})
        return events, int(info.get("size", 0)), {}


class CosLegacy:
    """The user messages auto_capture.py saved to 40-Conversation-Capture/<conversation>/message-*.md (older app versions
    kept nothing else). Conversations that still have a live session folder are left to `Cos`."""
    agent = "cos"

    def __init__(self, vault: Path):
        self.vault = Path(vault)

    def discover(self, horizon: float) -> list[dict]:
        base = self.vault / "40-Conversation-Capture"
        out = []
        try:
            live = {d.name for d in Cos().root().iterdir()} if Cos().root().is_dir() else set()
            dirs = [d for d in base.iterdir() if d.is_dir() and SID.fullmatch(d.name) and d.name not in live]
        except OSError:
            return out
        for d in dirs:
            files = [p for p in d.glob("message-*.md") if not p.is_symlink()]
            if not files:
                continue
            stats = [_stat(p) for p in files]
            stats = [s for s in stats if s]
            mtime = max(s.st_mtime for s in stats)
            if mtime < horizon:
                continue
            out.append({"source_id": d.name, "path": str(d), "size": sum(s.st_size for s in stats), "mtime": mtime})
        return out

    def read(self, info: dict, cursor: int, scratch: dict):
        d = Path(info["path"])
        rows = []
        for p in d.glob("message-*.md"):
            try:
                text = p.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", text, re.S)
            if not m:
                continue
            fm = dict(ln.split(":", 1) for ln in m.group(1).splitlines() if ":" in ln)
            if not str(fm.get("source", "")).strip().startswith("cos-session"):
                continue
            body = re.sub(r"\A\s*# User message\s*", "", m.group(2)).replace("\n\n[TRUNCATED: message exceeded safe capture length]", "").strip()
            if body:
                rows.append((fm.get("updated", "").strip(), body))
        # only the person's side was ever saved: "the last message got no answer" would be a guess, not a finding
        events: list[dict] = [{"k": "reset"}, {"k": "meta", "kind": "main", "entry": "chat-on-steroids (captured messages)", "oneway": True}]
        for ts, body in sorted(rows):
            events.append({"k": "user", "ts": ts, "text": body})
        return events, int(info.get("size", 0)), {}


# ---------------------------------------------------------------------------------------------------- closed chats
class Closed:
    """Chats of agents that cannot be read from disk (claude.ai, ChatGPT, Gemini...) but ended with a hamzaban-close block:
    their checkpoint is in 20-Checkpoints/session-hz-*/."""

    def __init__(self, vault: Path):
        self.vault = Path(vault)

    def discover(self, horizon: float) -> list[dict]:
        base = self.vault / "20-Checkpoints"
        out = []
        try:
            dirs = [d for d in base.iterdir() if d.is_dir() and d.name.startswith("session-hz-") and not d.is_symlink()]
        except OSError:
            return out
        for d in dirs:
            files = sorted(p for p in d.glob("*.md") if not p.is_symlink())
            if not files:
                continue
            st = _stat(files[-1])
            if st is None or st.st_mtime < horizon:
                continue
            m = re.search(r"(?m)^source:\s*hamzaban-close:([\w.-]+)", files[-1].read_text(encoding="utf-8-sig", errors="replace")[:1500])
            out.append({"source_id": d.name[len("session-"):], "path": str(files[-1]), "size": st.st_size, "mtime": st.st_mtime,
                        "agent": (m.group(1) if m else "other")})
        return out

    def read(self, info: dict, cursor: int, scratch: dict):
        text = Path(info["path"]).read_text(encoding="utf-8-sig", errors="replace")
        m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", text, re.S)
        fm = dict(ln.split(":", 1) for ln in (m.group(1) if m else "").splitlines() if ":" in ln)
        body = (m.group(2) if m else text).strip()
        title = re.search(r"(?m)^#\s+(.+)$", body)
        ts = fm.get("updated", "").strip()
        return [{"k": "reset"}, {"k": "meta", "kind": "main", "title": (title.group(1).strip() if title else fm.get("title", "").strip())[:120],
                                  "entry": "hamzaban-close"},
                {"k": "close", "ts": ts, "topic": (title.group(1).strip() if title else "")[:80], "text": body[:3000]}], int(info.get("size", 0)), {}


def all_sources(vault: Path):
    return [ClaudeCode(), Codex(), Cos(), CosLegacy(vault), Closed(vault)]
