"""Chat On Steroids capture: the newer app stores user messages in sessions/<id>/messages.json (and
messages/<sha256(key)>.json shards) instead of state/session-input.json. Fixtures follow the app's own record shape
(kind/messageId/seq/message{text,truncated}); they are NOT yet checked against a real message from the live app."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
SESSION = "cf6e2584-3a1e-46c2-ac20-c414dfc15b5c"


def user(mid, text, seq, **extra):
    return {"kind": "user_message", "messageId": mid, "seq": seq, "time": 1793800000000 + seq,
            "source": "extension", "message": {"text": text, "truncated": False, "chars": len(text)}, **extra}


def key(event):
    return "user_message\0" + event["messageId"]


class CosCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cos-cap-"))
        self.vault = self.tmp / "vault"
        self.eng = self.vault / ".ai-memory"
        self.eng.mkdir(parents=True)
        shutil.copy(SRC / "auto_capture.py", self.eng / "auto_capture.py")
        self.cos = self.tmp / "cos"
        (self.cos / "state").mkdir(parents=True)
        self.env = dict(os.environ, COS_APP_DATA=str(self.cos), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def session(self, sid=SESSION, conversation=None, user_messages=0, messages=None):
        d = self.cos / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"id": sid, "conversationId": conversation, "updatedAt": 1793800000000,
                                                 "userMessages": user_messages, "timelineTurns": {}}), encoding="utf-8")
        if messages is not None:
            (d / "messages.json").write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
        return d

    def once(self):
        r = subprocess.run([sys.executable, str(self.eng / "auto_capture.py"), "once"], env=self.env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return json.loads(r.stdout.decode("utf-8"))

    def notes(self, conv=SESSION):
        return sorted((self.vault / "40-Conversation-Capture" / conv).glob("message-*.md"))

    def test_empty_cos_does_not_crash(self):
        res = self.once()
        self.assertEqual((res["new_messages"], res["uncaptured"]), (0, 0))

    def test_session_without_conversation_id_is_captured_once(self):
        a, b = user("msg-aaaa-0001", "سلام، این یک آزمایش است", 1), user("msg-bbbb-0002", "second", 2)
        assistant = {"kind": "assistant_message", "messageId": "msg-cccc-0003", "seq": 3, "message": {"text": "reply"}}
        self.session(user_messages=2, messages={key(a): a, key(b): b, "assistant_message\0msg-cccc-0003": assistant})
        first = self.once()
        self.assertEqual((first["new_messages"], first["uncaptured"]), (2, 0))
        notes = self.notes()
        self.assertEqual(len(notes), 2)
        text = notes[0].read_text(encoding="utf-8")
        self.assertIn("source: cos-session-messages", text)
        self.assertIn("سلام، این یک آزمایش است", text)
        self.assertNotIn("reply", "".join(n.read_text(encoding="utf-8") for n in notes), "assistant replies are not captured")
        self.assertEqual(self.once()["new_messages"], 0, "second pass must not duplicate")
        self.assertEqual(len(self.notes()), 2)

    def test_conversation_id_is_used_when_present(self):
        conv = "6ad63d91-1b48-83ea-b369-2692385b1fcb"
        a = user("msg-aaaa-0001", "hello", 1)
        self.session(conversation=conv, user_messages=1, messages={key(a): a})
        self.assertEqual(self.once()["new_messages"], 1)
        self.assertEqual(len(self.notes(conv)), 1)

    def test_secret_in_message_is_redacted(self):
        a = user("msg-aaaa-0001", "my api_key = sk-abcdefghijklmnop1234567890 ok", 1)
        self.session(user_messages=1, messages={key(a): a})
        self.once()
        body = self.notes()[0].read_text(encoding="utf-8")
        self.assertNotIn("sk-abcdefghijklmnop1234567890", body)
        self.assertIn("[REDACTED", body)

    def test_event_with_mismatched_key_is_ignored(self):
        a = user("msg-aaaa-0001", "forged", 1)
        self.session(user_messages=1, messages={"user_message\0someone-else": a})
        res = self.once()
        self.assertEqual(res["new_messages"], 0)
        self.assertEqual(res["uncaptured"], 1, "app claims 1 message we could not read: the alarm must say so")

    def test_shards_need_the_right_file_name(self):
        good, bad = user("msg-good-0001", "from shard", 1), user("msg-evil-0002", "wrong name", 2)
        d = self.session(user_messages=2, messages={})
        shards = d / "messages"
        shards.mkdir()
        (shards / (hashlib.sha256(key(good).encode()).hexdigest() + ".json")).write_text(json.dumps(good), encoding="utf-8")
        (shards / (hashlib.sha256(b"something else").hexdigest() + ".json")).write_text(json.dumps(bad), encoding="utf-8")
        (shards / "notes.json").write_text(json.dumps(bad), encoding="utf-8")
        res = self.once()
        self.assertEqual((res["new_messages"], res["uncaptured"]), (1, 1))
        self.assertIn("from shard", self.notes()[0].read_text(encoding="utf-8"))

    def test_alarm_counts_messages_the_app_has_but_we_do_not(self):
        a = user("msg-aaaa-0001", "only one", 1)
        self.session(user_messages=3, messages={key(a): a})
        self.assertEqual(self.once()["uncaptured"], 2)

    def test_older_session_input_format_still_works(self):
        conv = "6ad63d91-1b48-83ea-b369-2692385b1fcb"
        (self.cos / "state" / "session-input.json").write_text(json.dumps([
            {"id": "inp-0001-aaaa", "conversationId": conv, "state": "sent", "text": "old format", "createdAt": 1793800000000},
            {"id": "inp-0002-bbbb", "conversationId": conv, "state": "failed", "text": "never sent", "createdAt": 1793800000001},
        ]), encoding="utf-8")
        self.assertEqual(self.once()["new_messages"], 1)
        self.assertEqual(len(self.notes(conv)), 1)


if __name__ == "__main__":
    unittest.main()
