"""native_memory.py: the memories the agents keep for themselves become proposals in the shared notebook.

Everything runs in temp dirs. The Claude Code fixtures follow the real memory-file shape (front matter with nested
metadata.type, plus a MEMORY.md index); the Codex fixture follows the columns of ~/.codex/memories_1.sqlite.
"""
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

import native_memory  # noqa: E402
import pending  # noqa: E402

FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"   # fake, matches the AWS-key pattern only

NOTE = """---
name: finish-end-to-end
description: The user wants fixes done end to end, not handed back
metadata:
  node_type: memory
  type: feedback
---

When the user asks for a fix, implement it, test it and launch it yourself.

**Why:** handing steps back frustrated them.
"""


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode() + p.read_bytes())
    return h.hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="native-memory-test-"))
        self.vault, self.home = self.tmp / "vault", self.tmp / "home"
        (self.vault / ".ai-memory").mkdir(parents=True)
        self.home.mkdir()
        for attr, value in (("PENDING", self.vault / "30-Pending"), ("FACTS", self.vault / "10-Facts"), ("VAULT", self.vault)):
            p = mock.patch.object(pending, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def memory_dir(self, project="C--proj"):
        d = self.home / ".claude" / "projects" / project / "memory"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def note(self, name, text=NOTE, project="C--proj"):
        p = self.memory_dir(project) / name
        p.write_text(text, encoding="utf-8")
        return p

    def scan(self, **kw):
        return native_memory.scan(home=self.home, **kw)

    def staged(self):
        return sorted((self.vault / "30-Pending").glob("*.md")) if (self.vault / "30-Pending").is_dir() else []


class ClaudeCodeMemory(Base):
    def test_a_note_becomes_one_proposal_with_its_kind_name_and_text(self):
        self.note("feedback_finish.md")
        self.note("MEMORY.md", "- [Finish](feedback_finish.md) — hook\n")        # the index is not a memory
        res = self.scan()
        self.assertEqual((res["found"], res["proposed"]), (1, 1))
        [path] = self.staged()
        text = path.read_text(encoding="utf-8")
        self.assertIn("source: native-memory:claude-code", text)
        self.assertIn("status: pending", text)
        for needle in ("Claude Code memory (feedback) — finish-end-to-end", "done end to end", "launch it yourself", "Why:"):
            self.assertIn(needle, text)
        self.assertNotIn("node_type", text, "front matter is not copied into the proposal")

    def test_secrets_are_redacted_before_anything_is_written(self):
        self.note("k.md", NOTE.replace("launch it yourself.", f"launch it yourself. key {FAKE_AWS} and password: hunter2supersecret"))
        self.scan()
        text = self.staged()[0].read_text(encoding="utf-8")
        self.assertNotIn(FAKE_AWS, text)
        self.assertNotIn("hunter2supersecret", text)

    def test_running_again_proposes_nothing_and_approved_or_rejected_notes_stay_gone(self):
        self.note("a.md")
        self.note("b.md", NOTE.replace("finish-end-to-end", "second-note").replace("launch it yourself", "check the real files"))
        self.assertEqual(self.scan()["proposed"], 2)
        self.assertEqual(self.scan()["proposed"], 0)
        a, b = [p.stem for p in self.staged()]
        pending.approve(a, "private")
        pending.reject(b)
        os.remove(native_memory._state_path())            # forget what was seen: the notebook itself must still say "known"
        res = self.scan()
        self.assertEqual((res["proposed"], res["known"]), (0, 2))
        self.assertEqual(self.staged(), [])

    def test_an_edited_note_is_a_new_proposal(self):
        p = self.note("a.md")
        self.scan()
        p.write_text(NOTE.replace("launch it yourself", "also tell me what is not verified"), encoding="utf-8")
        self.assertEqual(self.scan()["proposed"], 1)
        self.assertEqual(len(self.staged()), 2)

    def test_the_cap_per_run_defers_the_rest_to_the_next_run(self):
        for i in range(5):
            self.note(f"n{i}.md", NOTE.replace("finish-end-to-end", f"note-{i}").replace("launch it yourself", f"step {i}"))
        first = self.scan(limit=3)
        self.assertEqual((first["proposed"], first["deferred"]), (3, 2))
        second = self.scan(limit=3)
        self.assertEqual((second["proposed"], second["deferred"]), (2, 0))
        self.assertEqual(len(self.staged()), 5)

    def test_a_dry_run_writes_nothing_and_the_agents_folders_are_never_changed(self):
        self.note("a.md")
        before = tree_digest(self.home)
        res = self.scan(dry_run=True)
        self.assertEqual((res["proposed"], res["items"][0]["name"]), (1, "finish-end-to-end"))
        self.assertEqual(self.staged(), [])
        self.assertFalse(native_memory._state_path().exists())
        self.scan()
        self.assertEqual(tree_digest(self.home), before)

    def test_odd_files_are_skipped_and_never_stop_the_run(self):
        self.note("big.md", NOTE + "x" * (native_memory.MAX_FILE_BYTES + 10))
        self.note("plain.md", "just a line of text with no front matter at all, long enough to keep\n")
        (self.memory_dir() / "bad.md").write_bytes(b"\xff\xfe\x00 not utf-8 \xff")
        res = self.scan()
        self.assertTrue(any("big.md" in s for s in res["skipped"]))
        self.assertGreaterEqual(res["proposed"], 1)
        self.assertEqual(res["errors"], [])
        self.assertIn("plain", "".join(p.read_text(encoding="utf-8") for p in self.staged()))

    def test_front_matter_without_nesting_and_crlf(self):
        meta, body = native_memory.parse_note("---\r\nname: x\r\ndescription: d\r\ntype: user\r\n---\r\nBody.\r\n")
        self.assertEqual((meta, body), ({"name": "x", "description": "d", "type": "user"}, "Body."))
        meta, body = native_memory.parse_note("no front matter")
        self.assertEqual((meta, body), ({}, "no front matter"))


class CodexMemory(Base):
    def make_db(self, rows, columns="thread_id TEXT, raw_memory TEXT, rollout_summary TEXT, rollout_slug TEXT"):
        db = self.home / ".codex" / "memories_1.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        con.execute(f"CREATE TABLE stage1_outputs({columns})")
        con.executemany("INSERT INTO stage1_outputs VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()
        return db

    def test_rows_with_a_memory_become_proposals_and_empty_rows_do_not(self):
        db = self.make_db([("t1", "The user keeps invoices in A4, right to left.", "invoice layout", "invoice-layout"),
                           ("t2", "", "", "empty-one")])
        before = (db.stat().st_size, hashlib.sha256(db.read_bytes()).hexdigest())
        res = self.scan()
        self.assertEqual((res["found"], res["proposed"]), (1, 1))
        text = self.staged()[0].read_text(encoding="utf-8")
        self.assertIn("source: native-memory:codex", text)
        self.assertIn("Codex memory (memory) — invoice-layout: invoice layout", text)
        self.assertIn("right to left", text)
        self.assertEqual(before, (db.stat().st_size, hashlib.sha256(db.read_bytes()).hexdigest()), "the database is only ever read from a copy")
        self.assertEqual([p.name for p in db.parent.iterdir()], [db.name], "no journal or copy left next to it")

    def test_a_different_schema_is_tolerated(self):
        db = self.home / ".codex" / "memories_1.sqlite"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE something_else(a)")
        con.commit()
        con.close()
        res = self.scan()
        self.assertEqual((res["found"], res["errors"]), (0, []))
        db.write_bytes(b"not a database" * 20)
        self.assertEqual(self.scan()["found"], 0)


class CommandLine(Base):
    def test_the_command_prints_what_it_did(self):
        self.note("a.md")
        with mock.patch.dict(os.environ, {"SUITE_USER_HOME": str(self.home)}):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(native_memory.main(["scan"]), 0)
        out = buf.getvalue()
        self.assertIn("proposed 1", out)
        self.assertIn("finish-end-to-end", out)
        self.assertIn("nothing is a fact before that", out)


if __name__ == "__main__":
    unittest.main()
