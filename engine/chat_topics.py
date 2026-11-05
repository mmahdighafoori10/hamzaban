"""Split the messages of one chat into TOPICS, so each topic can carry its own sub-tag (CC-7K3QF/T3).

Pure and offline: no files, no model, no network. It works on the user's messages in order and is INCREMENTAL and
STABLE: a topic keeps its number for ever, a new message is attached to the topic whose vocabulary already covers it,
or opens the next number. The state is plain JSON, so a registry can store it and continue after the next message.

How a message is placed (first rule that applies):
  1. It names a topic itself with a hashtag (`#سئو`, `#hook-error`)             -> that topic (created if new, 'declared').
  2. It is short (an answer, "ادامه بده", "بله")                                 -> the topic that is current.
  3. Most of its informative words already appear in a recent topic            -> that topic.
  4. Otherwise, if it is a real request (long enough)                          -> a new topic.
Informative = rare inside THIS chat (inverse document frequency over the chat's own messages), so the words everybody
keeps repeating in a long chat ("Vault", "hook") do not glue unrelated subjects together.
"""
from __future__ import annotations

import math
import re

from infinite_context import normalized, terms as _terms

SHORT_TERMS = 4          # fewer distinct informative words than this = a reply, not a new subject
SHORT_CHARS = 55
NEW_TOPIC_MIN_CHARS = 90  # a shorter message opens a topic only if it brings (almost) entirely new vocabulary ...
LOW_COVER = 0.12         # ... i.e. if no recent topic holds even this share of its informative weight
COVER_THRESHOLD = 0.30   # share of the message's informative weight a topic must already hold to adopt it
QUICK_FOLLOW_UP = 240    # seconds: a message this soon after the previous one ...
QUICK_COVER = 0.10       # ... that shares at least this much vocabulary with the current topic stays in it
RECENT_TOPICS = 6        # a topic can be returned to while it is among the last few
MAX_TERMS_PER_TOPIC = 80
ASSISTANT_TERMS = 30     # how many words of one assistant answer are added to the current topic
LEARN_TERMS = 30         # how many words of one user message a topic learns
TITLE_CHARS = 60

STOP = {
    "لطفا", "لطفاً", "میشه", "میخوام", "میخواهم", "میخواستم", "باید", "هم", "هر", "اینو", "اون", "اونو", "خیلی", "کن", "کنید",
    "بکن", "بده", "بدید", "بگیر", "ببین", "یه", "یک", "ولی", "اما", "پس", "حالا", "هنوز", "فقط", "همین", "همه", "چیزی", "چی",
    "اینه", "اینکه", "نیست", "نمیشه", "میتونی", "میکنم", "کردم", "کردی", "کنم", "کنی", "باشه", "داره", "دارم", "هست", "بود",
    "please", "can", "you", "the", "and", "for", "that", "this", "with", "use", "just", "dont", "not", "are", "was", "have",
    "from", "into", "should", "would", "could", "also", "now", "then", "about", "what", "how", "why", "when", "which",
}

_HASHTAG = re.compile(r"(?<![\w#&/\\])#([^\s#.,;:!?()\[\]{}<>«»\"'`]{2,40})")
_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"```.*?```", re.DOTALL)
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[\w.\\/ -]{6,}")
_MARKUP = re.compile(r"[*_`>#\[\]|]+")
_ANGLE = re.compile(r"</?[A-Za-z][^<>\n]{0,200}>")   # <pasted_content id=...> and similar wrappers around a message


def informative_terms(text: str) -> list[str]:
    """Distinct words that can tell one subject from another."""
    text = _CODE.sub(" ", text)
    text = _ANGLE.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _PATH.sub(" ", text)
    out = []
    for t in _terms(text):
        if len(t) < 3 or t in STOP or t.isdigit():
            continue
        out.append(t)
    return list(dict.fromkeys(out))


def hashtag_of(text: str) -> str:
    """A topic the user named himself. '# heading' (hash + space) and '#123' are not tags."""
    for m in _HASHTAG.finditer(text):
        label = m.group(1).strip("-_")
        if label and not label.isdigit() and not re.fullmatch(r"[0-9a-fA-F]{3,8}", label):
            return label
    return ""


def title_of(text: str) -> str:
    """First sentence of a request, cleaned up, as a short title."""
    t = _CODE.sub(" ", text)
    t = _ANGLE.sub(" ", t)
    t = _URL.sub(" ", t)
    t = _PATH.sub(" ", t)
    t = _MARKUP.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    for sep in ("؟", "?", ".", "!", "\n", "؛", "،"):
        cut = t.find(sep, 12)
        if 12 <= cut <= 160:
            t = t[:cut]
            break
    if len(t) > TITLE_CHARS:
        t = t[:TITLE_CHARS].rsplit(" ", 1)[0] + "…"
    return t or "(بدون عنوان)"


class TopicState:
    """JSON-serialisable state: {"n": last used number, "msgs": messages seen, "df": {term: messages containing it},
    "topics": [{"n", "title", "source", "terms": {term: count}, "last": index of its latest message}], "cur": number}."""

    def __init__(self, data: dict | None = None):
        d = data or {}
        self.n = int(d.get("n", 0))
        self.msgs = int(d.get("msgs", 0))
        self.df = dict(d.get("df", {}))
        self.topics = [dict(t) for t in d.get("topics", [])]
        self.cur = int(d.get("cur", 0))

    def to_json(self) -> dict:
        return {"n": self.n, "msgs": self.msgs, "df": self.df, "topics": self.topics, "cur": self.cur}

    # -- helpers
    def topic(self, n: int) -> dict | None:
        return next((t for t in self.topics if t["n"] == n), None)

    def _idf(self, term: str) -> float:
        return math.log(1.0 + (self.msgs + 1.0) / (self.df.get(term, 0) + 1.0))

    def _coverage(self, words: list[str], topic: dict) -> float:
        total = sum(self._idf(w) for w in words)
        if total <= 0:
            return 0.0
        have = sum(self._idf(w) for w in words if w in topic["terms"])
        return have / total

    def _open(self, title: str, source: str, words: list[str]) -> dict:
        self.n += 1
        t = {"n": self.n, "title": title, "source": source, "terms": {}, "last": self.msgs}
        self.topics.append(t)
        self._learn(t, words)
        return t

    def _learn(self, topic: dict, words: list[str]) -> None:
        for w in words:
            topic["terms"][w] = topic["terms"].get(w, 0) + 1
        if len(topic["terms"]) > MAX_TERMS_PER_TOPIC:  # keep the most repeated words
            keep = sorted(topic["terms"].items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_TERMS_PER_TOPIC]
            topic["terms"] = dict(keep)

    def declare(self, title: str) -> int:
        """A topic named outright (by the user, or by a closing block): found if it exists, opened if not."""
        label = title.strip()
        t = next((x for x in self.topics if normalized(x["title"]) == normalized(label)), None) or self._open(label, "declared", [])
        self.cur = t["n"]
        return t["n"]

    def note_assistant(self, text: str) -> None:
        """What the assistant answered belongs to the current topic: its words let a short follow-up ("همون", "حذف کن")
        find the topic it is about, and keep unrelated later requests from being absorbed (they are scored on
        the user's words only)."""
        t = self.topic(self.cur)
        if t is not None:
            self._learn(t, informative_terms(text)[:ASSISTANT_TERMS])

    def add(self, text: str, gap: float | None = None) -> int:
        """Place one more user message; returns its topic number. `gap` = seconds since the previous message, when known:
        a quick follow-up that shares some vocabulary with the current topic is part of it."""
        words = informative_terms(text)
        label = hashtag_of(text)
        chosen: dict | None = None
        cur = self.topic(self.cur) if self.cur else None
        if label:
            chosen = next((t for t in self.topics if normalized(t["title"]) == normalized(label)), None) \
                or self._open(label, "declared", [])
        elif cur is not None and (len(words) < SHORT_TERMS or len(text.strip()) < SHORT_CHARS):
            chosen = cur
        elif self.topics:
            recent = self.topics[-RECENT_TOPICS:]
            best = max(recent, key=lambda t: (self._coverage(words, t), t["last"]))
            if words and self._coverage(words, best) >= COVER_THRESHOLD:
                chosen = best
            elif cur is not None and gap is not None and gap < QUICK_FOLLOW_UP and words and self._coverage(words, cur) >= QUICK_COVER:
                chosen = cur
            elif cur is not None and len(text.strip()) < NEW_TOPIC_MIN_CHARS and self._coverage(words, best) >= LOW_COVER:
                chosen = cur  # a medium-length remark that shares some vocabulary is not a new subject
        if chosen is None:
            chosen = self._open(title_of(text), "auto", [])
        self._learn(chosen, words[:LEARN_TERMS])   # the start of a message names its subject; a long paste must not take the topic over
        for w in words:
            self.df[w] = self.df.get(w, 0) + 1
        self.msgs += 1
        chosen["last"] = self.msgs
        self.cur = chosen["n"]
        return chosen["n"]


def segment(messages: list[str]) -> tuple[list[int], TopicState]:
    """Convenience for tests and one-off use: the topic number of each message, plus the final state."""
    st = TopicState()
    return [st.add(m) for m in messages], st
