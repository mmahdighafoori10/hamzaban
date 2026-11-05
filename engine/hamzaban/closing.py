"""The "closing block": the one thing every agent says back, in the same words.

At the end of a chat the receiving agent is asked to print one fenced block:

    ```hamzaban-close
    v: 1
    agent: ...
    topic: ...
    summary: ...
    decisions:
    - ...
    facts:
    - ...
    tasks:
    - ...
    ```

parse_blocks() finds such blocks in any text, ingest() turns one into (a) pending facts that still need
the user's approval and (b) an unverified checkpoint that the next "latest" pack can pick up.
Nothing here ever writes to 10-Facts. All text is redacted and size-limited.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from . import CLOSE_MARKER, CLOSE_VERSION

import handoff
import pending

MAX_PENDING_FILES = 200
MAX_BLOCKS_PER_FILE = 5   # one pasted file can never stage more than this many closing blocks
LIST_KEYS = ("decisions", "facts", "tasks")
SCALAR_KEYS = ("v", "agent", "topic", "summary")
LIMITS = {"summary": 1500, "item": 400, "items": 20, "topic": 80, "agent": 40}

_FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[ \t]*" + re.escape(CLOSE_MARKER) + r"[ \t]*\r?\n(.*?)\r?\n[ \t]*\1[ \t]*$")
_KEY = re.compile(r"^[ \t]*(v|agent|topic|summary|decisions|facts|tasks)[ \t]*[:：][ \t]*(.*)$", re.IGNORECASE)
_BULLET = re.compile(r"^[ \t]*(?:[-*•]|\d{1,2}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?(.*)$")
_PLACEHOLDER = re.compile(r"^<[^<>]{0,120}>$")
_SID = re.compile(r"^[A-Za-z0-9_-]{6,90}$")
_TURN = re.compile(r"(?m)^###[ \t]+(user|assistant|tool)[ \t]+[—–-][ \t]+.*$\n?")

EXAMPLE = {
    "en": ("```" + CLOSE_MARKER + "\nv: 1\nagent: <your product name>\ntopic: <short topic>\n"
           "summary: <3-6 lines: what happened and where things stand>\ndecisions:\n- <a decision that was made>\n"
           "facts:\n- <a durable fact or preference the user stated>\ntasks:\n- <the next step>\n```"),
}
EXAMPLE["fa"] = EXAMPLE["en"]  # the block itself is always the same text: that is the point of a shared language

PROTOCOL = {
    "en": ("When the user ends the conversation or asks you to wrap up, finish with ONE fenced block in exactly this shape "
           "so the next agent can continue. Plain text, short, no secrets, written in the language of the user's own notes. "
           "Only list facts the user actually stated or confirmed.\n" + EXAMPLE["en"]),
    "fa": ("وقتی کاربر گفتگو را تمام می‌کند یا می‌گوید «جمع‌بندی کن»، در پایان فقط یک بلوک با همین قالب بنویس تا "
           "عامل بعدی بتواند ادامه بدهد. متن ساده و کوتاه، بدون رمز و کلید، به زبانِ یادداشت‌های خود کاربر. "
           "فقط چیزهایی را در facts بنویس که کاربر خودش گفته یا تأیید کرده.\n" + EXAMPLE["fa"]),
}


def protocol_text(lang: str = "en") -> str:
    return PROTOCOL.get(lang, PROTOCOL["en"])


# ---------------------------------------------------------------- parsing
def _is_placeholder(text: str) -> bool:
    t = text.strip()
    return not t or bool(_PLACEHOLDER.match(t)) or t in ("...", "…", "-")


def parse_body(body: str) -> dict | None:
    data: dict = {k: "" for k in SCALAR_KEYS}
    for k in LIST_KEYS:
        data[k] = []
    current: str | None = None
    for raw in body.splitlines():
        m = _KEY.match(raw)
        if m and not _BULLET.match(raw):
            current = m.group(1).lower()
            inline = m.group(2).strip()
            if current in LIST_KEYS:
                if inline and not _is_placeholder(inline):
                    data[current].append(inline)
            elif inline:
                data[current] = inline
            continue
        if current is None or not raw.strip():
            continue
        if current in LIST_KEYS:
            b = _BULLET.match(raw)
            if b:
                if b.group(1).strip():
                    data[current].append(b.group(1).strip())
            elif data[current]:
                data[current][-1] += " " + raw.strip()  # a wrapped bullet
        else:
            data[current] = (data[current] + " " + raw.strip()).strip()
    if str(data["v"]).strip().split(".")[0] != str(CLOSE_VERSION):
        return None
    out = {
        "agent": re.sub(r"[^a-z0-9._-]+", "-", handoff.one_line(data["agent"], LIMITS["agent"]).lower()).strip("-") or "unknown",
        "topic": "" if _is_placeholder(data["topic"]) else handoff.one_line(data["topic"], LIMITS["topic"]),
        "summary": "" if _is_placeholder(data["summary"]) else handoff.clip(handoff.scrub(data["summary"]), LIMITS["summary"]),
    }
    for k in LIST_KEYS:
        items = []
        for it in data[k]:
            if _is_placeholder(it):
                continue
            text = handoff.one_line(it, LIMITS["item"])
            if text and text not in items:
                items.append(text)
            if len(items) >= LIMITS["items"]:
                break
        out[k] = items
    if not (out["summary"] or out["decisions"] or out["facts"] or out["tasks"]):
        return None  # an empty template or the protocol's own example
    return out


def parse_blocks(text: str) -> list[dict]:
    found = []
    for m in _FENCE.finditer(text or ""):
        parsed = parse_body(m.group(2))
        if parsed:
            found.append(parsed)
    return found


def assistant_text(chunk_body: str) -> str:
    """Only what the assistant said: a closing block inside a user turn (pasted protocol) must never count."""
    parts = _TURN.split(chunk_body)  # [before, role, text, role, text, ...]
    return "\n".join(parts[i + 1] for i in range(1, len(parts) - 1, 2) if parts[i] == "assistant")


# ---------------------------------------------------------------- ingest
def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def block_digest(parsed: dict) -> str:
    key = "\n".join([parsed["agent"], parsed["topic"], parsed["summary"], *parsed["decisions"], "--", *parsed["facts"], "--", *parsed["tasks"]])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def ingest(vault, parsed: dict, session: str = "") -> dict:
    """Stage the block. Returns {"pending": [...], "checkpoint": rel|None, "skipped": reason|None}."""
    vault = Path(vault)
    pending.VAULT = vault
    pending.PENDING = vault / "30-Pending"
    pending.FACTS = vault / "10-Facts"
    result: dict = {"pending": [], "checkpoint": None, "skipped": None, "digest": block_digest(parsed)}
    pdir = pending.PENDING
    if pdir.is_symlink():
        result["skipped"] = "30-Pending is a symlink"
        return result
    cp_root = vault / "20-Checkpoints"
    if cp_root.is_symlink():
        result["skipped"] = "20-Checkpoints is a symlink"
        return result
    agent = parsed["agent"]
    full = pdir.is_dir() and sum(1 for _ in pdir.glob("*.md")) >= MAX_PENDING_FILES
    source = f"hamzaban-close:{agent}"
    if not full:
        for kind, items in (("Decision: ", parsed["decisions"]), ("", parsed["facts"])):
            for item in items:
                try:
                    ident = pending.stage(kind + item, source=source, session=session or result["digest"])
                except OSError:
                    ident = None
                if ident:
                    result["pending"].append(ident)
    else:
        result["skipped"] = "30-Pending is full; ask the user to review pending facts first"
    if parsed["summary"] or parsed["tasks"] or parsed["decisions"]:
        folder = cp_root / f"session-hz-{agent}-{result['digest']}"
        name = f"{_stamp()}-{result['digest']}.md"
        conversation = session if _SID.fullmatch(session or "") else f"hz-{result['digest']}"
        n_dec = len(parsed["decisions"])
        # Decisions are NOT copied here: they wait in 30-Pending, and rejecting them there must remove them everywhere.
        body = ["# " + (parsed["topic"] or "Closing block") + f" ({agent})", "",
                "## Summary / خلاصه", parsed["summary"] or "-", "",
                "## Next actions / اقدام‌های بعدی"] + (["- [ ] " + t for t in parsed["tasks"]] or ["- -"]) + [
                "", "## Evidence limit",
                "Written by the agent itself at the end of a chat. Not verified by the user."
                + (f" {n_dec} decision(s) wait for the user's approval." if n_dec else "")]
        note = (f"---\ntitle: Closing block from {agent}\nconversation: {conversation}\nupdated: {_iso()}\n"
                f"source: {source}\ntype: auto-checkpoint\nstatus: unverified\n---\n\n" + "\n".join(body) + "\n")
        folder.mkdir(parents=True, exist_ok=True)
        try:  # an atomic claim: the hook and autosync may ingest the same block at the same moment
            with (folder / ".claim").open("x", encoding="utf-8") as handle:
                handle.write(_iso())
        except FileExistsError:
            return result  # same block ingested before
        try:
            with (folder / name).open("x", encoding="utf-8") as handle:
                handle.write(note)
            result["checkpoint"] = f"20-Checkpoints/{folder.name}/{name}"
        except FileExistsError:
            pass
    return result


def ingest_text(vault, text: str, session: str = "") -> list[dict]:
    return [ingest(vault, p, session) for p in parse_blocks(text)[-MAX_BLOCKS_PER_FILE:]]
