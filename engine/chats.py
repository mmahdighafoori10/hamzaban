"""Chat registry: every chat of every local agent gets a stable TAG, the topics inside it get sub-tags, and a chat that was
cut off (tokens ran out, the app closed, the process died) can be found and continued BY TAG.

    CC-7K3QF        the chat (CC Claude Code, CX Codex, CS Chat On Steroids, CA Claude app, GP ChatGPT, GM Gemini)
    CC-7K3QF/T3     the third topic raised inside that chat

It reads what the agents already write to disk (nothing depends on the chat cooperating, on a hook firing, or on a
wrap-up being written), keeps a small SQLite registry in .ai-memory/, writes one note per chat to 60-Chats/ with Obsidian
tags (#chat/CC-7K3QF/T3), and answers "continue from CC-7K3QF/T3" with a compact resume text.

    python chats.py scan                 refresh the registry now (autosync does it every minute)
    python chats.py list [--state interrupted] [--all]
    python chats.py show TAG             topics, last requests, open tasks
    python chats.py resume TAG[/Tn]      the text to paste into a new chat
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chat_sources
from chat_topics import TopicState

VAULT = Path(os.environ.get("INFINITE_CTX_VAULT") or Path(__file__).resolve().parent.parent)

AGENT_CODE = {"claude-code": "CC", "codex": "CX", "cos": "CS", "claude-ai": "CA", "chatgpt": "GP", "gemini": "GM"}
AGENT_FA = {"claude-code": "Claude Code", "codex": "Codex", "cos": "Chat On Steroids", "claude-ai": "Claude (چت)",
            "chatgpt": "ChatGPT", "gemini": "Gemini"}
STATE_FA = {"active": "فعال", "interrupted": "نیمه‌کاره", "idle": "ساکت", "closed": "بسته‌شده", "continued": "ادامه‌یافته"}
REASON_FA = {"limit": "سقف مصرف (توکن) تمام شد", "unanswered": "آخرین پیام تو بی‌پاسخ ماند",
             "tool": "وسط اجرای یک ابزار قطع شد", "aborted": "نوبت نیمه‌کاره متوقف شد", "turn": "وسط یک نوبت قطع شد",
             "error": "با خطا قطع شد"}
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"          # Crockford base32: no I, L, O, U (easy to read and to type)
CODES = set(AGENT_CODE.values()) | {"XX"}
REF_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2})-([0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{5,11})(?:[/.][Tt](\d{1,3}))?(?![A-Za-z0-9])")
LOGIC_VERSION = 8      # bump when the way chats are read or classified changes: the registry is rebuilt once
ACTIVE_SECONDS = 15 * 60   # written to within this long = the person is (probably) still in it
LIVE_SECONDS = 3 * 60      # written to within this long = the agent is working in it right now
ERROR_GRACE = 2 * 60       # an error that is not a usage limit may still be retried by the agent: wait this long before calling it a cut
USER_CLIP, ASSISTANT_CLIP = 1500, 900
NOTE_THROTTLE = 300.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
  tag TEXT PRIMARY KEY, agent TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'main',
  entry TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
  first_ts TEXT NOT NULL DEFAULT '', last_ts TEXT NOT NULL DEFAULT '', user_turns INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'idle', reason TEXT NOT NULL DEFAULT '',
  last_user TEXT NOT NULL DEFAULT '', last_assistant TEXT NOT NULL DEFAULT '',
  files TEXT NOT NULL DEFAULT '[]', open_tasks TEXT NOT NULL DEFAULT '[]', topics TEXT NOT NULL DEFAULT '{}',
  scratch TEXT NOT NULL DEFAULT '{}', parent TEXT NOT NULL DEFAULT '', continued_by TEXT NOT NULL DEFAULT '',
  closed_at TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
  cursor INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0, note_hash TEXT NOT NULL DEFAULT '',
  note_at REAL NOT NULL DEFAULT 0, nudged_at REAL NOT NULL DEFAULT 0, UNIQUE(agent, source_id));
CREATE TABLE IF NOT EXISTS msgs(
  chat TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '', topic INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat, seq));
CREATE INDEX IF NOT EXISTS msgs_topic ON msgs(chat, topic);
"""


# ---------------------------------------------------------------------------------------------------- tags
def candidate_tag(agent: str, source_id: str, length: int = 5) -> str:
    n = int.from_bytes(hashlib.sha256(f"{agent}\0{source_id}".encode("utf-8")).digest()[:8], "big")
    code = ""
    for _ in range(11):
        code = ALPHABET[n % 32] + code
        n //= 32
    return f"{AGENT_CODE.get(agent, 'XX')}-{code[:length]}"


def parse_ref(text: str):
    """'CC-7K3QF', 'cc-7k3qf/T3' or 'CC-7K3QF.T3' -> ('CC-7K3QF', 3 | None); None when it is not a chat tag."""
    m = REF_RE.search(text or "")
    if not m or m.group(1).upper() not in CODES:
        return None
    return f"{m.group(1).upper()}-{m.group(2).upper()}", (int(m.group(3)) if m.group(3) else None)


def is_ref(text: str) -> bool:
    return bool(text) and parse_ref(text) is not None and len(text.strip()) <= 24


def _parse_ts(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _now() -> datetime:
    """The current time; SUITE_NOW (an ISO timestamp) fakes it for tests, like everywhere else in this engine."""
    return _parse_ts(os.environ.get("SUITE_NOW", "")) or datetime.now(timezone.utc)


def activity(chat: dict) -> datetime:
    """When the chat last did anything: the later of its last message and the last write to its log (tool calls and
    long-running commands write to the log without producing a message)."""
    a = _parse_ts(chat.get("last_ts", ""))
    b = datetime.fromtimestamp(chat["mtime"], tz=timezone.utc) if chat.get("mtime") else None
    return max([x for x in (a, b) if x] or [datetime.fromtimestamp(0, tz=timezone.utc)])


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _one(text: str, limit: int) -> str:
    return _clip(re.sub(r"\s+", " ", text or ""), limit)


def peek_tag(vault: Path, agent: str, source_id: str) -> str:
    """The tag a chat has (or will get), read-only and cheap: what a hook can say before the registry has seen the chat."""
    db = Path(vault) / ".ai-memory" / "chat-registry.sqlite3"
    try:
        if db.is_file():
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute("SELECT tag FROM chats WHERE agent=? AND source_id=?", (agent, source_id)).fetchone()
            finally:
                con.close()
            if row:
                return row[0]
    except sqlite3.Error:
        pass
    return candidate_tag(agent, source_id)


class Registry:
    def __init__(self, vault: Path | None = None, sources=None):
        self.vault = Path(vault or VAULT)
        self.engine = self.vault / ".ai-memory"
        self.db_path = self.engine / "chat-registry.sqlite3"
        self.sources = sources if sources is not None else chat_sources.all_sources(self.vault)

    # ------------------------------------------------------------------------------------------ storage
    def connect(self, create: bool = True) -> sqlite3.Connection:
        if create:
            self.engine.mkdir(parents=True, exist_ok=True)
        elif not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        con = sqlite3.connect(str(self.db_path), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        if create:
            con.executescript(SCHEMA)
            if con.execute("PRAGMA user_version").fetchone()[0] != LOGIC_VERSION:
                # everything here is derived from the agents' own logs: when the rules for reading them change, start over
                con.execute("DELETE FROM msgs")
                con.execute("DELETE FROM chats")
                con.execute(f"PRAGMA user_version={LOGIC_VERSION}")
                con.commit()
        return con

    def _alloc_tag(self, con: sqlite3.Connection, agent: str, source_id: str) -> str:
        for n in range(5, 12):
            tag = candidate_tag(agent, source_id, n)
            row = con.execute("SELECT agent, source_id FROM chats WHERE tag=?", (tag,)).fetchone()
            if row is None or (row["agent"], row["source_id"]) == (agent, source_id):
                return tag
        raise RuntimeError("no free tag")

    # ------------------------------------------------------------------------------------------ scanning
    def scan(self, budget: float = 25.0, now: datetime | None = None, horizon_days: int = 120, notes: bool = True) -> dict:
        t0, now = time.time(), now or _now()
        stats = {"seen": 0, "ingested": 0, "events": 0, "partial": False, "errors": [], "chats": 0}
        con = self.connect()
        try:
            jobs = []
            for src in self.sources:
                try:
                    jobs += [(src, info) for info in src.discover(now.timestamp() - horizon_days * 86400)]
                except Exception as exc:  # one broken store must not hide the others
                    stats["errors"].append(f"{type(src).__name__}: {type(exc).__name__}: {str(exc)[:120]}")
            jobs.sort(key=lambda j: -j[1]["mtime"])
            stats["seen"] = len(jobs)
            while True:   # a big log is read in pieces: keep sweeping while time remains and something was left unread
                progressed = 0
                for src, info in jobs:
                    if time.time() - t0 > budget:
                        stats["partial"] = True
                        break
                    agent = info.get("agent") or src.agent
                    row = con.execute("SELECT size, mtime FROM chats WHERE agent=? AND source_id=?", (agent, info["source_id"])).fetchone()
                    if row and row["size"] == info["size"] and abs(row["mtime"] - info["mtime"]) < 1e-6:
                        continue
                    try:
                        with con:
                            stats["events"] += self._ingest(con, src, info, agent)
                        stats["ingested"] += 1
                        progressed += 1
                    except Exception as exc:
                        stats["errors"].append(f"{agent}/{info['source_id'][:12]}: {type(exc).__name__}: {str(exc)[:120]}")
                if stats["partial"] or not progressed:
                    break
                if not con.execute("SELECT 1 FROM chats WHERE json_extract(scratch, '$.incomplete') = 1 LIMIT 1").fetchone():
                    break
            with con:
                self._reclassify(con, now)
            stats["chats"] = con.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            if notes and not self._read_only_vault():
                self.write_notes(con, now)
        finally:
            con.close()
        return stats

    def _read_only_vault(self) -> bool:
        return not os.access(self.vault, os.W_OK)

    def _ingest(self, con: sqlite3.Connection, src, info: dict, agent: str) -> int:
        row = con.execute("SELECT * FROM chats WHERE agent=? AND source_id=?", (agent, info["source_id"])).fetchone()
        cursor = row["cursor"] if row else 0
        scratch = json.loads(row["scratch"]) if row else {}
        events, new_cursor, scratch = src.read(info, cursor, scratch)
        if row is None:
            tag = self._alloc_tag(con, agent, info["source_id"])
            con.execute("INSERT INTO chats(tag, agent, source_id, path) VALUES (?,?,?,?)", (tag, agent, info["source_id"], info.get("path", "")))
            row = con.execute("SELECT * FROM chats WHERE tag=?", (tag,)).fetchone()
        chat = dict(row)
        if events and events[0].get("k") == "reset":
            con.execute("DELETE FROM msgs WHERE chat=?", (chat["tag"],))
            chat.update(user_turns=0, first_ts="", last_ts="", last_user="", last_assistant="", files="[]", open_tasks="[]",
                        topics="{}", title="", parent=chat["parent"], closed_at="")
            scratch = {k: v for k, v in scratch.items() if k in ("open_tools", "plan")}
            events = events[1:]
        complete = not (new_cursor > cursor and new_cursor + 1 < info["size"])
        scratch["incomplete"] = not complete   # a big log is read in pieces: its state is only known once all of it was read
        self._apply(con, chat, events, scratch)
        chat.update(path=info.get("path", ""), cursor=new_cursor, mtime=info["mtime"], size=info["size"] if complete else new_cursor)
        if not chat["project"] and chat["cwd"]:
            chat["project"] = Path(chat["cwd"].replace("\\", "/")).name
        self._save(con, chat)
        return len(events)

    def _save(self, con: sqlite3.Connection, chat: dict) -> None:
        cols = ["kind", "entry", "cwd", "project", "title", "first_ts", "last_ts", "user_turns", "last_user", "last_assistant", "files",
                "open_tasks", "topics", "scratch", "parent", "continued_by", "closed_at", "path", "size", "cursor", "mtime"]
        con.execute("UPDATE chats SET " + ",".join(f"{c}=?" for c in cols) + " WHERE tag=?", [chat[c] for c in cols] + [chat["tag"]])

    def _apply(self, con: sqlite3.Connection, chat: dict, events: list, scratch: dict) -> None:
        from redact import scrub_all
        state = TopicState(json.loads(chat["topics"] or "{}"))
        files, tasks = json.loads(chat["files"] or "[]"), json.loads(chat["open_tasks"] or "[]")
        users = chat["user_turns"]
        for ev in events:
            k, ts = ev.get("k"), ev.get("ts") or ""
            if k == "meta":
                if ev.get("title") and (not chat["title"] or not ev.get("weak")):
                    chat["title"] = _one(scrub_all(ev["title"]), 120)
                for key in ("cwd", "kind", "entry"):
                    if ev.get(key):
                        chat[key] = str(ev[key])
                if ev.get("oneway"):
                    scratch["oneway"] = True
            elif k == "user":
                text = scrub_all(ev.get("text", "")).strip()
                if not text:
                    continue
                prev, now_ts = _parse_ts(scratch.get("prev_ts", "")), _parse_ts(ts)
                n = state.add(text, gap=(now_ts - prev).total_seconds() if prev and now_ts else None)
                t = state.topic(n)
                t["turns"], t["first_ts"], t["last_ts"] = t.get("turns", 0) + 1, t.get("first_ts") or ts, ts or t.get("last_ts", "")
                con.execute("INSERT OR REPLACE INTO msgs(chat, seq, ts, role, text, topic) VALUES (?,?,?,?,?,?)",
                            (chat["tag"], 2 * users, ts, "user", _clip(text, USER_CLIP), n))
                if users < 3:
                    self._link_lineage(con, chat, text)
                users += 1
                chat["last_user"] = _one(text, 300)
                chat["first_ts"] = chat["first_ts"] or ts
                chat["last_ts"] = max(chat["last_ts"], ts)
                scratch.update(last_role="user", limit=False, prev_ts=ts)
                scratch.pop("marker", None)   # the person carried on: an earlier abort is no longer the end of the chat
            elif k == "assistant":
                text = scrub_all(ev.get("text", "")).strip()
                if not text or users == 0:
                    continue
                n = state.cur
                con.execute("INSERT OR REPLACE INTO msgs(chat, seq, ts, role, text, topic) VALUES (?,?,?,?,?,?)",
                            (chat["tag"], 2 * (users - 1) + 1, ts, "assistant", _clip(text, ASSISTANT_CLIP), n))
                state.note_assistant(text)
                chat["last_assistant"] = _one(text, 400)
                chat["last_ts"] = max(chat["last_ts"], ts)
                scratch.update(last_role="assistant", prev_ts=ts, limit=False)   # it answered: an earlier error was only a hiccup
                if ev.get("final"):
                    scratch.pop("marker", None)
            elif k == "tool":
                scratch["limit"] = False
                if ev.get("target") and ev["target"] not in files:
                    files.append(ev["target"])
                    files = files[-25:]
            elif k == "alive":
                scratch["limit"] = False
            elif k == "plan":
                tasks = [_one(i.get("content", ""), 160) for i in ev.get("items", []) if i.get("status") != "completed" and i.get("content")][:12]
            elif k == "mark":
                name = ev.get("name")
                scratch["marker"] = {"name": name, "ts": ts, "detail": ev.get("detail", "")}
                if name == "turn_start":
                    scratch.update(turn_open=True, limit=False)   # a new turn began: the chat is going on again
                elif name == "turn_done":
                    scratch.update(turn_open=False, limit=False)
                elif name == "turn_aborted":
                    scratch["turn_open"] = False
                elif name in ("limit", "error"):
                    scratch.update(limit=True, limit_kind=name, limit_ts=ts, limit_detail=_one(scrub_all(ev.get("detail", "")), 200))
                    if ev.get("until"):
                        scratch["limit_until"] = int(ev["until"])   # when the usage window opens again (epoch seconds)
                    else:
                        scratch.pop("limit_until", None)
                chat["last_ts"] = max(chat["last_ts"], ts)
            elif k == "close":
                label = _one(ev.get("topic") or chat["title"] or "closing block", 80)
                n = state.declare(label)
                t = state.topic(n)
                t["turns"], t["first_ts"], t["last_ts"] = 1, ts, ts
                con.execute("INSERT OR REPLACE INTO msgs(chat, seq, ts, role, text, topic) VALUES (?,?,?,?,?,?)",
                            (chat["tag"], 0, ts, "assistant", _clip(scrub_all(ev.get("text", "")), 2400), n))
                users = max(users, 1)
                chat.update(closed_at=ts, last_ts=max(chat["last_ts"], ts), first_ts=chat["first_ts"] or ts,
                            last_assistant=_one(ev.get("text", ""), 400))
        if not chat["title"] and users:
            first = con.execute("SELECT text FROM msgs WHERE chat=? AND role='user' ORDER BY seq LIMIT 1", (chat["tag"],)).fetchone()
            if first:
                from chat_topics import title_of
                chat["title"] = title_of(first["text"])
        chat.update(user_turns=users, files=json.dumps(files, ensure_ascii=False), open_tasks=json.dumps(tasks, ensure_ascii=False),
                    topics=json.dumps(state.to_json(), ensure_ascii=False), scratch=json.dumps(scratch, ensure_ascii=False))

    def _link_lineage(self, con: sqlite3.Connection, chat: dict, text: str) -> None:
        """A new chat that starts with 'continue from CC-7K3QF' becomes that chat's continuation."""
        if chat["parent"]:
            return
        for m in REF_RE.finditer(text):
            ref = parse_ref(m.group(0))
            if ref and ref[0] != chat["tag"] and con.execute("SELECT 1 FROM chats WHERE tag=?", (ref[0],)).fetchone():
                chat["parent"] = ref[0]
                con.execute("UPDATE chats SET continued_by=? WHERE tag=? AND continued_by=''", (chat["tag"], ref[0]))
                return

    # ------------------------------------------------------------------------------------------ state
    @staticmethod
    def classify(chat: dict, now: datetime) -> tuple[str, str]:
        sc = json.loads(chat["scratch"] or "{}")
        if chat["closed_at"]:
            return "closed", ""
        if chat["continued_by"]:
            return "continued", ""
        if sc.get("incomplete"):
            return "idle", ""   # not read to the end yet: do not call it cut off on half the evidence
        if sc.get("limit"):
            # the agent's own log says the chat stopped (usage limit, refused request ...): that is shown at once, not after
            # the quiet period below. Only an error that is not a usage limit gets a moment, because the agent may retry it.
            cut_at = _parse_ts(sc.get("limit_ts", ""))
            retrying = sc.get("limit_kind") == "error" and cut_at is not None and (now - cut_at).total_seconds() < ERROR_GRACE
            if not retrying:
                return "interrupted", "error" if sc.get("limit_kind") == "error" else "limit"
        if (now - activity(chat)).total_seconds() < ACTIVE_SECONDS:
            return "active", ""
        if sc.get("open_tools"):
            return "interrupted", "tool"
        if (sc.get("marker") or {}).get("name") == "turn_aborted":
            return "interrupted", "aborted"
        if sc.get("turn_open"):
            return "interrupted", "turn"
        if sc.get("last_role") == "user" and not sc.get("oneway"):
            return "interrupted", "unanswered"
        return "idle", ""

    @staticmethod
    def is_live(chat: dict, now: datetime) -> bool:
        """An active chat the agent is working in right now: written to a moment ago, or in the middle of a turn / a tool call."""
        if chat["state"] != "active":
            return False
        sc = json.loads(chat["scratch"] or "{}")
        quiet = (now - activity(chat)).total_seconds()
        return quiet < LIVE_SECONDS or bool(sc.get("turn_open") or sc.get("open_tools"))

    def _reclassify(self, con: sqlite3.Connection, now: datetime) -> None:
        for row in con.execute("SELECT * FROM chats").fetchall():
            state, reason = self.classify(dict(row), now)
            if (state, reason) != (row["state"], row["reason"]):
                con.execute("UPDATE chats SET state=?, reason=? WHERE tag=?", (state, reason, row["tag"]))

    # ------------------------------------------------------------------------------------------ queries
    def _topic_rows(self, con: sqlite3.Connection, chat: dict) -> list[dict]:
        st = json.loads(chat["topics"] or "{}")
        msgs = con.execute("SELECT seq, role, text, topic, ts FROM msgs WHERE chat=? ORDER BY seq", (chat["tag"],)).fetchall()
        out = []
        for t in st.get("topics", []):
            mine = [m for m in msgs if m["topic"] == t["n"]]
            users = [m for m in mine if m["role"] == "user"]
            last_user = users[-1] if users else None
            reply = next((m for m in mine if last_user is not None and m["seq"] == last_user["seq"] + 1), None)
            out.append({"tag": f"{chat['tag']}/T{t['n']}", "n": t["n"], "title": t["title"], "source": t.get("source", "auto"),
                        "turns": t.get("turns", len(users)), "first_ts": t.get("first_ts", ""), "last_ts": t.get("last_ts", ""),
                        "first_user": _one(users[0]["text"], 240) if users else "",
                        "last_user": _one(last_user["text"], 240) if last_user is not None else "",
                        "last_assistant": _one(reply["text"], 320) if reply is not None else "",
                        "current": t["n"] == st.get("cur")})
        return out

    def get(self, ref: str, con: sqlite3.Connection | None = None) -> dict | None:
        parsed = parse_ref(ref)
        if not parsed:
            return None
        own = con is None
        con = con or self.connect(create=False)
        try:
            row = con.execute("SELECT * FROM chats WHERE tag=?", (parsed[0],)).fetchone()
            if row is None:
                return None
            chat = dict(row)
            chat["topic_list"] = self._topic_rows(con, chat)
            chat["focus"] = parsed[1]
            return chat
        finally:
            if own:
                con.close()

    def list(self, states: tuple | None = None, include_subagents: bool = False, limit: int = 200) -> list[dict]:
        try:
            con = self.connect(create=False)
        except FileNotFoundError:
            return []
        try:
            rows = [dict(r) for r in con.execute("SELECT * FROM chats ORDER BY last_ts DESC, mtime DESC").fetchall()]
        finally:
            con.close()
        return [r for r in rows if (include_subagents or r["kind"] != "subagent") and (not states or r["state"] in states)][:limit]

    def snapshot(self, now: datetime | None = None, full: int = 40) -> dict:
        """Everything the interface needs, as plain data."""
        now = now or _now()
        out = {"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "counts": {}, "chats": []}
        try:
            con = self.connect(create=False)
        except FileNotFoundError:
            return out
        try:
            rows = [dict(r) for r in con.execute("SELECT * FROM chats ORDER BY last_ts DESC, mtime DESC").fetchall()]
            counts: dict = {}
            for r in rows:
                if r["kind"] != "subagent":
                    counts[r["state"]] = counts.get(r["state"], 0) + 1
            out["counts"] = dict(counts, total=sum(counts.values()), subagents=sum(1 for r in rows if r["kind"] == "subagent"))
            order = {"interrupted": 0, "active": 1, "idle": 2, "continued": 3, "closed": 4}
            rows.sort(key=lambda r: (order.get(r["state"], 9), -activity(r).timestamp()))
            for i, r in enumerate(rows):
                item = {"tag": r["tag"], "agent": r["agent"], "agent_fa": AGENT_FA.get(r["agent"], r["agent"]), "title": r["title"] or r["last_user"][:70] or "(بدون عنوان)",
                        "project": r["project"], "cwd": r["cwd"], "kind": r["kind"], "state": r["state"], "state_fa": STATE_FA.get(r["state"], r["state"]),
                        "reason": r["reason"], "reason_fa": REASON_FA.get(r["reason"], ""), "first_ts": r["first_ts"],
                        "last_ts": activity(r).strftime("%Y-%m-%dT%H:%M:%SZ") if (r["last_ts"] or r["mtime"]) else "",
                        "turns": r["user_turns"], "last_user": r["last_user"], "last_assistant": r["last_assistant"],
                        "files": json.loads(r["files"] or "[]")[-8:], "open_tasks": json.loads(r["open_tasks"] or "[]"),
                        "parent": r["parent"], "continued_by": r["continued_by"],
                        "phrase": f"ادامه بده از {r['tag']}", "note": f"60-Chats/{r['tag']}",
                        "live": self.is_live(r, now), "cut_at": "", "cut_detail": "", "reset_at": "", "last_user_ts": ""}
                sc = json.loads(r["scratch"] or "{}")
                if r["state"] == "interrupted" and r["reason"] in ("limit", "error"):
                    item["cut_at"], item["cut_detail"] = sc.get("limit_ts", ""), sc.get("limit_detail", "")
                    if r["reason"] == "limit" and sc.get("limit_until"):
                        item["reset_at"] = datetime.fromtimestamp(int(sc["limit_until"]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                elif r["state"] == "interrupted":
                    item["cut_at"] = (sc.get("marker") or {}).get("ts", "")
                if r["kind"] != "subagent" and i < full:
                    last = con.execute("SELECT ts FROM msgs WHERE chat=? AND role='user' ORDER BY seq DESC LIMIT 1", (r["tag"],)).fetchone()
                    item["last_user_ts"] = last["ts"] if last else ""
                if i < full and r["kind"] != "subagent":
                    chat = dict(r)
                    item["topics"] = self._topic_rows(con, chat)
                    item["resume"] = self._resume(con, chat, None, 4200) if r["state"] in ("interrupted", "active", "idle") else ""
                    if r["state"] in ("interrupted", "active"):   # the chats a person is likely to continue: each topic is resumable too
                        for t in item["topics"]:
                            t["resume"] = self._resume(con, chat, t["n"], 2800)
                else:
                    item["topics"] = [{"tag": f"{r['tag']}/T{t['n']}", "n": t["n"], "title": t["title"], "turns": t.get("turns", 0)}
                                      for t in json.loads(r["topics"] or "{}").get("topics", [])]
                    item["resume"] = ""
                out["chats"].append(item)
        finally:
            con.close()
        return out

    # ------------------------------------------------------------------------------------------ resume
    def resume_text(self, ref: str, max_chars: int = 6000) -> str:
        parsed = parse_ref(ref)
        if not parsed:
            raise ValueError("not a chat tag: expected something like CC-7K3QF or CC-7K3QF/T3")
        con = self.connect(create=False)
        try:
            row = con.execute("SELECT * FROM chats WHERE tag=?", (parsed[0],)).fetchone()
            if row is None:
                raise ValueError(f"unknown chat tag {parsed[0]}; run `python chats.py list` to see the known ones")
            return self._resume(con, dict(row), parsed[1], max_chars)
        finally:
            con.close()

    def _resume(self, con: sqlite3.Connection, chat: dict, focus: int | None, max_chars: int) -> str:
        topics = self._topic_rows(con, chat)
        if focus is not None and not any(t["n"] == focus for t in topics):
            raise ValueError(f"{chat['tag']} has no topic T{focus}; its topics: " + ", ".join(f"T{t['n']}" for t in topics))
        agent = AGENT_FA.get(chat["agent"], chat["agent"])
        lines = [f"[CHAT RESUME — extractive notes from the chat's own log, not verified; check the real files before relying on them]",
                 f"Chat {chat['tag']} · {agent} · project: {chat['project'] or '—'} · {chat['user_turns']} user messages",
                 f"State: {STATE_FA.get(chat['state'], chat['state'])}" + (f" — {REASON_FA.get(chat['reason'], chat['reason'])}" if chat["reason"] else "")
                 + f" · started {chat['first_ts'][:16].replace('T', ' ') or '—'} · last activity {chat['last_ts'][:16].replace('T', ' ') or '—'}"]
        if chat["title"]:
            lines.append(f"Title: {chat['title']}")
        sc = json.loads(chat["scratch"] or "{}")
        if chat["state"] == "interrupted" and chat["reason"] in ("limit", "error") and sc.get("limit_ts"):
            cut = f"Cut off at {sc['limit_ts'][:16].replace('T', ' ')} UTC — the agent's own log says: {sc.get('limit_detail') or chat['reason']}"
            if chat["reason"] == "limit" and sc.get("limit_until"):
                cut += " (the usage window opens again at " + datetime.fromtimestamp(int(sc["limit_until"]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ")"
            lines.append(cut)
        if chat["parent"]:
            lines.append(f"Continues: {chat['parent']}")
        shown = [t for t in topics if focus is None or t["n"] == focus]
        if focus is not None:
            t = shown[0]
            lines += ["", f"FOCUS — topic {t['tag']} · {t['title']} ({t['turns']} messages)"]
            mine = con.execute("SELECT seq, role, text FROM msgs WHERE chat=? AND topic=? ORDER BY seq", (chat["tag"], focus)).fetchall()
            for m in mine[-10:]:
                lines.append(f"- {'user' if m['role'] == 'user' else 'agent'}: {_one(m['text'], 420)}")
            others = [x for x in topics if x["n"] != focus]
            if others:
                lines += ["", "Other topics in this chat: " + " | ".join(f"{x['tag']} {x['title']}" for x in others[-8:])]
        else:
            lines += ["", "TOPICS"]
            for t in topics[-14:]:
                lines.append(f"- {t['tag']} · {t['title']} ({t['turns']} msgs){' ← last' if t['current'] else ''}")
                if t["current"] or t is topics[-1]:
                    if t["last_user"]:
                        lines.append(f"    user: {t['last_user']}")
                    if t["last_assistant"]:
                        lines.append(f"    agent: {t['last_assistant']}")
            tasks = json.loads(chat["open_tasks"] or "[]")
            if tasks:
                lines += ["", "OPEN TASKS (the agent's own list when the chat stopped)"] + [f"- {x}" for x in tasks[:8]]
            files = json.loads(chat["files"] or "[]")
            if files:
                lines += ["", "FILES LAST CHANGED"] + [f"- {x}" for x in files[-8:]]
        try:
            from hamzaban import pack as packmod
            rules = [r["text"] for r in packmod.read_harness(self.vault)][:8]
        except Exception:
            rules = []
        if rules:
            lines += ["", "THE USER'S STANDING RULES"] + [f"- {_one(r, 200)}" for r in rules]
        lines += ["", f"To continue: reply to the user's last message above, after checking what exists on disk. Tag: {chat['tag']}"
                      + (f"/T{focus}" if focus else "")]
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"

    # ------------------------------------------------------------------------------------------ notes
    def _note(self, chat: dict, topics: list[dict]) -> str:
        agent = AGENT_FA.get(chat["agent"], chat["agent"])
        tags = [f"chat/{chat['tag']}", f"agent/{chat['agent']}", f"state/{chat['state']}"] + [f"chat/{chat['tag']}/T{t['n']}" for t in topics[:30]]
        proj = re.sub(r"[^\w-]+", "-", chat["project"], flags=re.UNICODE).strip("-")
        if proj:
            tags.append(f"project/{proj}")
        title = (chat["title"] or chat["last_user"] or "(بدون عنوان)").replace('"', "'")[:100]
        out = ["---", f'title: "چت {chat["tag"]} — {title}"', "tags: [" + ", ".join(tags) + "]", f"chat: {chat['tag']}", f"agent: {chat['agent']}",
               f"session: {chat['source_id']}", f"state: {chat['state']}", f"type: chat-registry", f"updated: {chat['last_ts'] or ''}", "---", "",
               f"# {chat['tag']} — {title}", "",
               f"- عامل: {agent} · پروژه: {chat['project'] or '—'} · پیام‌های کاربر: {chat['user_turns']}",
               f"- وضعیت: **{STATE_FA.get(chat['state'], chat['state'])}**" + (f" — {REASON_FA.get(chat['reason'], '')}" if chat["reason"] else ""),
               f"- شروع: {chat['first_ts'][:16].replace('T', ' ') or '—'} · آخرین فعالیت: {chat['last_ts'][:16].replace('T', ' ') or '—'}"]
        if chat["parent"]:
            out.append(f"- ادامهٔ چت [[{chat['parent']}]]")
        if chat["continued_by"]:
            out.append(f"- ادامه‌یافته در [[{chat['continued_by']}]]")
        out += ["", "## ادامه دادن", f"به عامل بگو: «ادامه بده از {chat['tag']}» یا برای یک موضوع: «ادامه بده از {chat['tag']}/T{{n}}».", "", "## موضوع‌ها"]
        for t in topics:
            out += [f"### #chat/{chat['tag']}/T{t['n']} — {t['title']}",
                    f"- {t['turns']} پیام" + (" · موضوع جاری" if t["current"] else "") + (" · ثبت‌شده توسط کاربر" if t["source"] == "declared" else "")]
            if t["last_user"]:
                out.append(f"- آخرین درخواست: {t['last_user']}")
            if t["last_assistant"]:
                out.append(f"- آخرین نتیجه: {t['last_assistant']}")
            out.append("")
        tasks = json.loads(chat["open_tasks"] or "[]")
        if tasks:
            out += ["## کارهای باز"] + [f"- [ ] {x}" for x in tasks] + [""]
        files = json.loads(chat["files"] or "[]")
        if files:
            out += ["## فایل‌های تغییرداده‌شده"] + [f"- `{x}`" for x in files[-10:]] + [""]
        out += ["> [!note] این یادداشت خودکار ساخته می‌شود و از لاگ خود چت استخراج شده؛ تأییدشده نیست."]
        return "\n".join(out) + "\n"

    def write_notes(self, con: sqlite3.Connection, now: datetime | None = None) -> int:
        folder = self.vault / "60-Chats"
        if folder.is_symlink():
            return 0
        wrote, clock = 0, time.time()
        for row in con.execute("SELECT * FROM chats WHERE kind != 'subagent'").fetchall():
            chat = dict(row)
            text = self._note(chat, self._topic_rows(con, chat))
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            if digest == chat["note_hash"]:
                continue
            if chat["state"] == "active" and clock - chat["note_at"] < NOTE_THROTTLE and chat["note_hash"]:
                continue
            folder.mkdir(exist_ok=True)
            target = folder / f"{chat['tag']}.md"
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp, target)
            con.execute("UPDATE chats SET note_hash=?, note_at=? WHERE tag=?", (digest, clock, chat["tag"]))
            con.commit()
            wrote += 1
        return wrote

    # ------------------------------------------------------------------------------------------ hooks
    def hook_context(self, agent: str, session_id: str, cwd: str = "", fresh: bool = True, now: datetime | None = None) -> str:
        """A few lines for a session's start: this chat's tag, and chats that were cut off in the same project. Cheap and
        read-only, safe to call from a hook; returns '' when there is nothing to say."""
        now = now or _now()
        tag = peek_tag(self.vault, agent, session_id) if session_id else ""
        lines = []
        if tag:
            lines.append(f"[CHAT TAG] This chat is {tag}. The user can continue it later, even after it is cut off, by saying "
                         f"\"ادامه بده از {tag}\" (a topic of it: \"{tag}/T2\"). Tell them the tag if they ask.")
        try:
            con = self.connect(create=False)
        except FileNotFoundError:
            return "\n".join(lines)
        try:
            if fresh:
                project = Path((cwd or "").replace("\\", "/")).name
                cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows = con.execute("SELECT * FROM chats WHERE state='interrupted' AND kind!='subagent' AND last_ts>=? AND tag!=? "
                                   "ORDER BY last_ts DESC LIMIT 25", (cutoff, tag)).fetchall()
                mine = [r for r in rows if project and r["project"] == project] or []
                if mine:
                    lines.append("[UNFINISHED CHATS in this project — offer to continue one if it fits what the user asks]")
                    for r in mine[:3]:
                        why = REASON_FA.get(r["reason"], "")
                        lines.append(f"- {r['tag']} ({AGENT_FA.get(r['agent'], r['agent'])}, {r['last_ts'][:10]}) {why}: {_one(r['last_user'], 110)}")
                    lines.append("  To continue one: call memory_handoff with topic=<TAG>.")
        finally:
            con.close()
        return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- command line
def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args.pop(0) if args else "list"
    reg = Registry()
    if cmd == "scan":
        print(json.dumps(reg.scan(budget=120.0), ensure_ascii=False))
        return 0
    if cmd == "list":
        states = (args[args.index("--state") + 1],) if "--state" in args else None
        rows = reg.list(states, include_subagents="--all" in args)
        for r in rows:
            print(f"{r['tag']:<10} {r['state']:<11} {AGENT_FA.get(r['agent'], r['agent']):<17} {r['last_ts'][:16].replace('T', ' '):<16} "
                  f"{r['user_turns']:>4}  {(r['title'] or r['last_user'])[:60]}")
        print(f"{len(rows)} chats")
        return 0
    if cmd in ("show", "resume") and args:
        try:
            if cmd == "resume":
                chars = int(args[args.index("--chars") + 1]) if "--chars" in args else 6000
                print(reg.resume_text(args[0], chars))
            else:
                chat = reg.get(args[0])
                if chat is None:
                    print("unknown tag")
                    return 1
                print(f"{chat['tag']} · {AGENT_FA.get(chat['agent'], chat['agent'])} · {chat['state']} {REASON_FA.get(chat['reason'], '')}")
                for t in chat["topic_list"]:
                    print(f"  {t['tag']:<14} {t['turns']:>3} msgs  {t['title']}")
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}")
            return 1
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
