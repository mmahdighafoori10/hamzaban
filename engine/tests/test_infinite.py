"""Tests for the Claude Code capture/recall layer. Runs hooks as real subprocesses on a temp copy of the vault code."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
from redact import scrub_all  # noqa: E402


def U(text, ts="2026-10-31T10:00:00Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def A(blocks, ts="2026-10-31T10:00:05Z"):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": blocks}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mem = self.tmp / ".ai-memory"
        self.mem.mkdir()
        for f in SRC.glob("*.py"):
            shutil.copy(f, self.mem / f.name)
        fixtures = SRC / "tests" / "fixtures"          # neutral demo projects: the tests never read a real vault
        shutil.copytree(fixtures / "10-Projects", self.tmp / "10-Projects")
        shutil.copy(fixtures / "topics.json", self.mem / "topics.json")
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", INFINITE_CTX_VAULT=str(self.tmp), INFINITE_CTX_NO_THROTTLE="1")
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.tpath = self.tmp / "t.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, script, *args, stdin=b""):
        return subprocess.run([sys.executable, str(self.mem / script), *args], input=stdin, capture_output=True, env=self.env)

    def hook(self, payload=None, raw=None):
        data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self.run_py("claude_hook.py", stdin=data)

    def payload(self, event, **extra):
        return dict(hook_event_name=event, session_id=self.sid, transcript_path=str(self.tpath), cwd="C:/Users/demo/work", **extra)

    def write_lines(self, lines, mode="w"):
        with open(self.tpath, mode, encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def chunks(self):
        return sorted((self.tmp / "40-Conversation-Capture" / self.sid).glob("chunk-*.md"))


class TestRedaction(unittest.TestCase):
    def test_secrets_removed_and_normal_text_kept(self):
        secrets = [
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "Authorization: Bearer abc123xyz789",
            "password=hunter2hunter2",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
            "AKIAIOSFODNN7EXAMPLE",
            "0123456789abcdef" * 4,
            "4111 1111 1111 1111",
            "dGhpc2lzYXZlcnlsb25nYmFzZTY0c2VjcmV0dG9rZW4xMjM0NTY3ODkw",
            "WALLET_PRIVATE_KEY=abcd1234efgh",
        ]
        out = scrub_all("\n".join(secrets))
        for needle in ("sk-abcdefgh", "abc123xyz789", "hunter2", "eyJhbGci", "AKIAIOSF", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "4111 1111", "dGhpc2lz", "abcd1234efgh"):
            self.assertNotIn(needle, out, needle)
        keep = "turn-1792190610575-9866c8271e commit 0123456789abcdef0123456789abcdef01234567 کارت 1234 5678 9012 3456 سلام"
        self.assertEqual(scrub_all(keep), keep)


class TestCapture(Base):
    def test_both_sides_idempotent_incremental_partial_line(self):
        self.write_lines([
            U("سلام، رمز: password=topsecret99 و ادامه بده"),
            A([{"type": "text", "text": "باشه، شروع می‌کنم"}, {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:\\x\\a.py"}}]),
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok done"}]}},
            U("<system-reminder>INJECTED-NOISE</system-reminder>سؤال بعدی"),
            U("[AUTO-RECALLED PROJECT BACKGROUND — x]\nJUNK-RECALL"),
        ])
        self.assertEqual(self.hook(self.payload("Stop")).returncode, 0)
        files = self.chunks()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        for needle in ("سلام", "باشه، شروع می‌کنم", "tool: Edit", "سؤال بعدی"):
            self.assertIn(needle, text)
        for needle in ("topsecret99", "INJECTED-NOISE", "JUNK-RECALL"):
            self.assertNotIn(needle, text)
        self.hook(self.payload("Stop"))
        self.assertEqual(len(self.chunks()), 1, "second run must not duplicate")
        self.write_lines([U("پیام تازه یک")], mode="a")
        self.hook(self.payload("Stop"))
        self.assertEqual(len(self.chunks()), 2)
        with open(self.tpath, "a", encoding="utf-8", newline="") as f:
            f.write('{"type":"user","message":{"role":"user","content":"نیمه')
        self.hook(self.payload("Stop"))
        self.assertEqual(len(self.chunks()), 2, "partial line must wait")
        with open(self.tpath, "a", encoding="utf-8", newline="") as f:
            f.write('کاره"}}\n')
        self.hook(self.payload("Stop"))
        self.assertEqual(len(self.chunks()), 3)
        joined = "".join(c.read_text(encoding="utf-8") for c in self.chunks())
        self.assertIn("نیمه" + "کاره", joined)

    def test_throttle_applies_without_override(self):
        self.write_lines([U("x" * 10)])
        env = dict(self.env)
        env.pop("INFINITE_CTX_NO_THROTTLE")
        p = subprocess.run([sys.executable, str(self.mem / "claude_hook.py")], input=json.dumps(self.payload("Stop")).encode(), capture_output=True, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(self.chunks()), 0, "tiny growth must be throttled on Stop")

    def test_precompact_checkpoint_pending_and_sessionstart_reinject(self):
        todos = [{"content": "TASK-OPEN-ALPHA", "status": "pending"}, {"content": "TASK-DONE-BETA", "status": "completed"}]
        self.write_lines([
            U("از این به بعد همیشه جواب‌ها را کوتاه و فارسی بنویس."),
            A([{"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}},
               {"type": "tool_use", "name": "Write", "input": {"file_path": "C:\\proj\\main.py"}},
               {"type": "text", "text": "فایل ساخته شد."}]),
            U("خوب است، ادامه بده"),
        ])
        self.assertEqual(self.hook(self.payload("PreCompact", trigger="auto")).returncode, 0)
        cps = list((self.tmp / "20-Checkpoints" / f"session-{self.sid}").glob("*.md"))
        self.assertEqual(len(cps), 1)
        cp = cps[0].read_text(encoding="utf-8")
        self.assertIn("TASK-OPEN-ALPHA", cp)
        self.assertNotIn("TASK-DONE-BETA", cp)
        self.assertIn("C:\\proj\\main.py", cp)
        self.assertIn("فایل ساخته شد", cp)
        pend = list((self.tmp / "30-Pending").glob("*.md"))
        self.assertEqual(len(pend), 1)
        self.assertIn("status: pending", pend[0].read_text(encoding="utf-8"))
        res = self.hook(self.payload("SessionStart", source="compact"))
        out = json.loads(res.stdout.decode("utf-8"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("SESSION CHECKPOINT", out["additionalContext"])
        self.assertIn("TASK-OPEN-ALPHA", out["additionalContext"])
        self.assertLessEqual(len(out["additionalContext"]), 3600)
        started = self.hook(self.payload("SessionStart", source="startup")).stdout
        ctx = json.loads(started.decode("utf-8"))["hookSpecificOutput"]["additionalContext"] if started else ""
        self.assertNotIn("SESSION CHECKPOINT", ctx, "a brand-new chat is not given an old checkpoint")
        self.assertIn("[CHAT TAG]", ctx, "but it is told its own tag")

    def test_hook_never_blocks_on_garbage_and_logs(self):
        for raw in (b"not json", b"", b"{}", b'{"hook_event_name":"Stop","session_id":"../../x","transcript_path":"/nope"}'):
            p = self.hook(raw=raw)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout, b"")
        log = (self.mem / "logs" / "hook-events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event"', log)
        self.assertTrue((self.mem / "logs" / "errors.log").exists())


class TestRouting(Base):
    def prompt(self, text):
        res = self.hook(dict(hook_event_name="UserPromptSubmit", session_id="s-" + self.sid, cwd="C:/Users/demo", prompt=text))
        return json.loads(res.stdout.decode("utf-8")) if res.stdout.strip() else None

    def test_positive_paraphrase_and_letter_variants(self):
        # Alpha: front-matter vocabulary + aliases; Beta: aliases only (typed once with Arabic yeh/kaf); Gamma: topics.json
        for text, topic in [("alpha planner رو ادامه بده", "Alpha"), ("دوباره برگردیم سراغ آلفا پلنر", "Alpha"),
                            ("alphaplan و برنامه‌ریز رو ادامه بده", "Alpha"),
                            ("كيهان مارکت رو ادامه بده", "Beta"), ("kayhan market رو ادامه بدیم", "Beta"),
                            ("ادامه ربات گاما و servo", "Gamma")]:
            out = self.prompt(text)
            self.assertIsNotNone(out, text)
            self.assertIn("Detected: " + topic, out["hookSpecificOutput"]["additionalContext"], text)

    def test_negative_cases_inject_nothing(self):
        for text in ("صفحهٔ اول بروشور رو طراحی کن", "یه تابع پایتون برای مرتب‌سازی بنویس", "what is the capital of France",
                     "بودجه", "gamma ray burst"):       # a weak word alone, and an alias the user told the router to ignore
            self.assertIsNone(self.prompt(text), text)


class TestPending(Base):
    def search(self, token):
        return self.run_py("infinite_context.py", "search", token).stdout.decode("utf-8")

    def test_staging_approval_rejection_and_superseded_exclusion(self):
        a = self.run_py("pending.py", "add", "qzxwvunique91 ترجیح: همیشه فارسی").stdout.decode().strip()
        b = self.run_py("pending.py", "add", "rejectme55 چیز دیگر").stdout.decode().strip()
        self.assertIn(a, self.run_py("pending.py", "list").stdout.decode("utf-8"))
        self.assertIn("No matches", self.search("qzxwvunique91"), "pending must not be retrievable")
        self.run_py("pending.py", "approve", a)
        fact = (self.tmp / "10-Facts" / f"{a}.md").read_text(encoding="utf-8")
        self.assertIn("status: active", fact)
        self.assertIn("sensitivity: unreviewed", fact)
        self.assertIn("qzxwvunique91", self.search("qzxwvunique91"))
        self.run_py("pending.py", "reject", b)
        self.assertIn("No matches", self.search("rejectme55"))
        self.assertEqual(self.run_py("pending.py", "add", "qzxwvunique91 ترجیح: همیشه فارسی").stdout.decode().strip(), "duplicate or empty")
        old = self.tmp / "10-Facts" / "old.md"
        old.write_text("---\ntitle: old\nstatus: superseded\n---\nsupersededtokenxyz here\n", encoding="utf-8")
        self.assertIn("No matches", self.search("supersededtokenxyz"))


class TestBaselineRouter(Base):
    def test_detect_cli_uses_notes_front_matter_and_topics_json(self):
        def detect(text):
            return json.loads(self.run_py("topic_router.py", "detect", text).stdout.decode("utf-8"))
        self.assertEqual(detect("alpha planner")["topic"], "Alpha")
        self.assertEqual(detect("كيهان مارکت")["topic"], "Beta")
        self.assertEqual(detect("robotarm servo")["topic"], "Gamma")
        self.assertIsNone(detect("صفحهٔ اول بروشور"))

    def test_without_topics_json_only_notes_speak(self):
        (self.mem / "topics.json").unlink()
        def detect(text):
            return json.loads(self.run_py("topic_router.py", "detect", text).stdout.decode("utf-8"))
        self.assertEqual(detect("alpha planner")["topic"], "Alpha")
        self.assertIsNone(detect("robotarm servo"), "Gamma's vocabulary lived in topics.json")
        (self.mem / "topics.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(detect("alpha planner")["topic"], "Alpha", "a broken file means no extra vocabulary, never an error")


class TestMerge(Base):
    def test_merge_preserves_user_hooks_and_replaces_old_ones(self):
        settings = self.tmp / "settings.json"
        settings.write_text(json.dumps({
            "theme": "dark",
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python C:/x/topic_router.py codex-hook"},
                                                {"type": "command", "command": "my-own-hook.sh"}]},
                                     # command + args form: the script is only named in `args` (this used to survive the merge)
                                     {"hooks": [{"type": "command", "command": "C:\\Python310\\python.exe",
                                                 "args": ["C:\\x\\topic_router.py", "codex-hook"], "timeout": 15}]}],
                "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "python C:/x/infinite_context.py resume"}]},
                                 {"matcher": "startup|resume", "hooks": [{"type": "command", "command": "C:\\Python310\\python.exe",
                                                                          "args": ["C:\\x\\topic_router.py", "codex-hook"]}]}],
            }}), encoding="utf-8")
        dry = self.run_py("merge_claude_settings.py", "--settings", str(settings))
        self.assertIn("Dry run", dry.stdout.decode())
        self.assertNotIn("claude_hook", settings.read_text(encoding="utf-8"), "dry run must not write")
        self.run_py("merge_claude_settings.py", "--settings", str(settings), "--apply")
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        cmds = [h["command"] for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertIn("my-own-hook.sh", cmds)
        self.assertFalse(any("topic_router" in c for c in cmds))
        for event in ("UserPromptSubmit", "SessionStart"):
            leftovers = [h for g in data["hooks"][event] for h in g["hooks"] if "topic_router" in " ".join(h.get("args", []))]
            self.assertEqual(leftovers, [], f"{event}: legacy hook given as command+args must be replaced too")
        self.assertEqual(sum("claude_hook.py" in c for c in cmds), 1)
        for event in ("SessionStart", "PreCompact", "SessionEnd", "Stop"):
            self.assertEqual(sum("claude_hook.py" in h["command"] for g in data["hooks"][event] for h in g["hooks"]), 1, event)
        self.assertEqual(data["hooks"]["SessionStart"][-1]["matcher"], "startup|resume|compact|clear")
        self.assertTrue(list(self.tmp.glob("settings.json.bak-*")))
        self.run_py("merge_claude_settings.py", "--settings", str(settings), "--apply")
        again = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(sum("claude_hook.py" in h["command"] for g in again["hooks"]["Stop"] for h in g["hooks"]), 1, "idempotent")



class HarnessNoiseTests(unittest.TestCase):
    def test_harness_blocks_are_stripped(self):
        import capture_session as cs
        raw = ("<task-notification> <task-id>x</task-id> done </task-notification>"
               "<command-name>/model</command-name><command-args>opus</command-args>"
               "<local-command-stdout>Set model</local-command-stdout>بله، R3")
        self.assertEqual(cs.strip_injected(raw), "بله، R3")
        self.assertEqual(cs.strip_injected("<task-notification>\nonly\n</task-notification>"), "")

if __name__ == "__main__":
    unittest.main(verbosity=2)
