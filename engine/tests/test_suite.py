"""Tests for suite.py. Everything runs in temp dirs via SUITE_HOME / SUITE_APPDATA / SUITE_USER_HOME overrides."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"          # fake, matches the AWS-key pattern only
FAKE_PW = "password: hunter2" + "supersecret"   # fake


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="suite-test-"))
        self.vault = self.tmp / "vault"
        self.eng = self.vault / ".ai-memory"
        self.eng.mkdir(parents=True)
        for name in ("suite.py", "redact.py", "auto_capture.py"):
            shutil.copy(SRC / name, self.eng / name)
        self.home = self.tmp / "home"
        self.appdata = self.tmp / "appdata"
        self.home.mkdir()
        self.appdata.mkdir()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                        SUITE_HOME=str(self.vault), SUITE_APPDATA=str(self.appdata), SUITE_USER_HOME=str(self.home),
                        SUITE_CLAUDE_SETTINGS=str(self.tmp / "nope-settings.json"),
                        SUITE_CODEX_CONFIG=str(self.tmp / "nope-codex.toml"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_suite(self, *args, env=None, stdin=b""):
        return subprocess.run([sys.executable, str(self.eng / "suite.py"), *args], input=stdin,
                              capture_output=True, env=env or self.env, text=False, timeout=60)

    def out(self, *args, **kw):
        r = self.run_suite(*args, **kw)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def mkfact(self, ident, text, status="active", sens="public-ok", extra=""):
        FACTS = self.vault / "10-Facts"
        FACTS.mkdir(exist_ok=True)
        sens_line = f"sensitivity: {sens}\n" if sens is not None else ""
        (FACTS / f"{ident}.md").write_text(f"---\ntitle: F {ident}\nid: {ident}\nstatus: {status}\n{sens_line}{extra}---\n\n{text}\n", encoding="utf-8")


def doctor_statuses(output):
    return {int(m.group(2)): m.group(1) for m in re.finditer(r"^\[(\w+)\s*\] (\d+)\. ", output, re.M)}


class DoctorTests(Base):
    def test_empty_vault_runs_and_exits_zero(self):
        shutil.rmtree(self.eng)  # not even an engine dir; script runs from SRC
        env = dict(self.env)
        r = subprocess.run([sys.executable, str(SRC / "suite.py"), "doctor"], capture_output=True, env=env, timeout=60)
        text = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertNotIn("Traceback", text + r.stderr.decode())
        st = doctor_statuses(text)
        self.assertEqual(sorted(st), list(range(1, 10)))
        self.assertEqual(st[4], "UNKNOWN")
        self.assertEqual(st[5], "UNKNOWN")
        self.assertIn("Summary:", text)

    def test_garbage_inputs_never_raise(self):
        (self.eng / "capture-service-status.json").write_text("{not json", encoding="utf-8")
        (self.eng / "logs").mkdir()
        (self.eng / "logs" / "hook-events.jsonl").write_text("garbage\n\x00\xff\n", encoding="utf-8", errors="replace")
        (self.eng / "context-index.sqlite3").write_bytes(b"this is not a database" * 50)
        (self.eng / "capture-state.sqlite3").write_bytes(b"\x00" * 100)
        (self.eng / "suite-skills.json").write_text("][", encoding="utf-8")
        Path(self.env["SUITE_CLAUDE_SETTINGS"]).write_text("{{{", encoding="utf-8")
        code, out, err = self.out("doctor")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", out + err)
        st = doctor_statuses(out)
        self.assertEqual(sorted(st), list(range(1, 10)))
        self.assertEqual(st[7], "FAIL")
        code, out, err = self.out("status")
        self.assertEqual(code, 0, err)

    def test_heartbeat_freshness_with_fixed_clock(self):
        (self.eng / "capture-service-status.json").write_text(json.dumps({"timestamp": "2026-10-31T10:00:00Z", "total": 1}), encoding="utf-8")
        env = dict(self.env, SUITE_NOW="2026-10-31T10:01:00Z")
        self.assertEqual(doctor_statuses(self.out("doctor", env=env)[1])[1], "PASS")
        env = dict(self.env, SUITE_NOW="2026-11-02T10:00:00Z")
        self.assertEqual(doctor_statuses(self.out("doctor", env=env)[1])[1], "FAIL")

    def test_claude_registration(self):
        settings = Path(self.env["SUITE_CLAUDE_SETTINGS"])
        hooks = {ev: [{"hooks": [{"type": "command", "command": '"py" "x/claude_hook.py"'}]}]
                 for ev in ("UserPromptSubmit", "SessionStart", "PreCompact", "SessionEnd", "Stop")}
        settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        self.assertEqual(doctor_statuses(self.out("doctor")[1])[4], "PASS")
        settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        self.assertEqual(doctor_statuses(self.out("doctor")[1])[4], "FAIL")
        settings.unlink()
        self.assertEqual(doctor_statuses(self.out("doctor")[1])[4], "UNKNOWN")

    def test_index_integrity_and_journal(self):
        db = self.eng / "context-index.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("create table t(x)")
        con.commit()
        con.close()
        out = self.out("doctor")[1]
        self.assertEqual(doctor_statuses(out)[7], "PASS")
        self.assertIn(": ok", out)
        (self.eng / "context-index.sqlite3-journal").write_bytes(b"")
        out = self.out("doctor")[1]
        self.assertEqual(doctor_statuses(out)[7], "WARN")
        self.assertIn("leftover journal", out)

    def test_queue_counts(self):
        self.mkfact("a1", "x", sens="public-ok")
        self.mkfact("b2", "y", sens="private")
        self.mkfact("c3", "z", sens="unreviewed")
        (self.vault / "30-Pending").mkdir()
        (self.vault / "30-Pending" / "p1.md").write_text("---\nstatus: pending\n---\nhello\n", encoding="utf-8")
        (self.vault / "20-Checkpoints" / "session-1").mkdir(parents=True)
        (self.vault / "20-Checkpoints" / "session-1" / "c.md").write_text("cp", encoding="utf-8")
        out = self.out("doctor")[1]
        self.assertIn("30-Pending candidates: 1", out)
        self.assertIn("private=1", out)
        self.assertIn("public-ok=1", out)
        self.assertIn("unreviewed=1", out)
        self.assertIn("1 note(s) in 1 folder(s)", out)


class SkillsTests(Base):
    def setUp(self):
        super().setUp()
        src = self.eng / "skills-src" / "demo"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("---\nname: demo\n---\nversion one\n", encoding="utf-8")
        (src / "ref.md").write_text("reference\n", encoding="utf-8")
        (self.home / ".agents").mkdir()
        (self.appdata / "chat-on-steroids").mkdir()
        cfg = {"skills": ["demo"], "converter": None,
               "targets": {"agents": "{home}/.agents/skills", "cos": "{appdata}/chat-on-steroids/skills",
                           "claude": "{home}/.claude/skills"}}   # .claude absent -> unreachable
        (self.eng / "suite-skills.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.t_agents = self.home / ".agents" / "skills" / "demo"
        self.t_cos = self.appdata / "chat-on-steroids" / "skills" / "demo"

    def snapshot(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*"))

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        code, out, err = self.out("skills", "push")
        self.assertEqual(code, 0, err)
        self.assertIn("DRY-RUN", out)
        self.assertIn("WOULD CREATE", out)
        self.assertIn("SKIP", out)  # unreachable claude root
        self.assertEqual(self.snapshot(), before)

    def test_apply_creates_then_backs_up_without_deleting(self):
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.t_agents / "SKILL.md").read_text(encoding="utf-8"), "---\nname: demo\n---\nversion one\n")
        self.assertTrue((self.t_cos / "ref.md").is_file())
        self.assertFalse((self.home / ".claude").exists())            # unreachable root not created
        self.assertFalse((self.eng / "backups").exists() and list((self.eng / "backups").glob("skills-*")))  # nothing to back up on create
        # user edits the target and adds an extra file
        (self.t_agents / "SKILL.md").write_text("LOCAL EDIT\n", encoding="utf-8")
        (self.t_agents / "mine.txt").write_text("keep me", encoding="utf-8")
        code, out, _ = self.out("skills", "push")                      # dry-run: unchanged
        self.assertIn("WOULD UPDATE", out)
        self.assertEqual((self.t_agents / "SKILL.md").read_text(encoding="utf-8"), "LOCAL EDIT\n")
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.t_agents / "SKILL.md").read_text(encoding="utf-8"), "---\nname: demo\n---\nversion one\n")
        self.assertTrue((self.t_agents / "mine.txt").is_file())        # never deleted
        backups = list((self.eng / "backups").glob("skills-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "agents" / "demo" / "SKILL.md").read_text(encoding="utf-8"), "LOCAL EDIT\n")
        self.assertTrue((backups[0] / "agents" / "demo" / "mine.txt").is_file())

    def test_drift_detected_by_doctor_list_diff(self):
        self.out("skills", "push", "--apply")
        out = self.out("doctor")[1]
        self.assertEqual(doctor_statuses(out)[6], "PASS")
        (self.t_cos / "SKILL.md").write_text("drifted\n", encoding="utf-8")
        out = self.out("doctor")[1]
        self.assertEqual(doctor_statuses(out)[6], "WARN")
        self.assertIn("DIFFERS", out)
        code, out, _ = self.out("skills", "list")
        self.assertRegex(out, r"cos\s+DIFFERS")
        code, out, _ = self.out("skills", "diff")
        self.assertIn("-drifted", out)
        self.assertIn("+version one", out)

    def test_crlf_copy_is_not_drift(self):
        self.out("skills", "push", "--apply")
        p = self.t_agents / "SKILL.md"
        # idempotent on purpose: on Windows the copy may already have CRLF, and "\n" -> "\r\n" again would make "\r\r\n"
        p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertRegex(self.out("skills", "list")[1], r"agents\s+SAME")

    def test_no_reachable_target_is_unknown(self):
        shutil.rmtree(self.home / ".agents")
        shutil.rmtree(self.appdata / "chat-on-steroids")
        shutil.rmtree(self.appdata)
        shutil.rmtree(self.home)
        self.home.mkdir()
        self.assertEqual(doctor_statuses(self.out("doctor")[1])[6], "UNKNOWN")

    def test_converter_slot_invoked_when_present(self):
        (self.eng / "skill-convert.py").write_text(
            "from pathlib import Path\n"
            "def convert(skill_dir, target, target_name=None):\n"
            "    Path(target).mkdir(parents=True, exist_ok=True)\n"
            "    (Path(target) / 'CONVERTED.txt').write_text(f'{target_name}:{Path(skill_dir).name}', encoding='utf-8')\n",
            encoding="utf-8")
        code, out, _ = self.out("skills", "push")
        self.assertFalse(self.t_agents.exists())                       # dry-run never calls it
        self.assertIn("converter", out)
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(code, 0, err + out)
        self.assertEqual((self.t_agents / "CONVERTED.txt").read_text(encoding="utf-8"), "agents:demo")
        self.assertEqual((self.t_cos / "CONVERTED.txt").read_text(encoding="utf-8"), "cos:demo")
        self.assertFalse((self.t_agents / "SKILL.md").exists())        # converter replaced plain copy

    def test_converter_failure_is_reported_not_fatal(self):
        (self.eng / "skill-convert.py").write_text("def convert(skill_dir, target):\n    raise RuntimeError('boom')\n", encoding="utf-8")
        code, out, _ = self.out("skills", "push", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("boom", out)


import importlib.util


def load_suite_module():
    spec = importlib.util.spec_from_file_location("suite_under_test", SRC / "suite.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FactsParseTests(Base):
    """10-Facts front matter is read strictly: a file that cannot be read plainly is counted as blocked and never trusted."""

    def rawfact(self, name, content):
        d = self.vault / "10-Facts"
        d.mkdir(exist_ok=True)
        (d / f"{name}.md").write_bytes(content.encode("utf-8") if isinstance(content, str) else content)

    def facts_line(self):
        return next(ln for ln in self.out("doctor")[1].splitlines() if "10-Facts:" in ln)

    def test_well_formed_facts_are_counted_by_their_sensitivity_even_with_bom_and_crlf(self):
        self.rawfact("a", "---\nid: a\nstatus: active\nsensitivity: public-ok\n---\nText.\n")
        self.rawfact("b", b"\xef\xbb\xbf---\r\nid: b\r\nstatus: active\r\nsensitivity: private\r\n---\r\nBOM and CRLF.\r\n")
        line = self.facts_line()
        for needle in ("2 total", "private=1", "public-ok=1"):
            self.assertIn(needle, line)
        self.assertNotIn("blocked", line)

    def test_unparseable_front_matter_is_counted_as_blocked(self):
        ok = "id: {i}\nstatus: active\nsensitivity: public-ok\n"
        bad = {
            "nested": f"---\n{ok.format(i='nested')}meta:\n  sensitivity: private\n---\nBody.\n",
            "tabbed": f"---\n{ok.format(i='tabbed')}\tnote: x\n---\nBody.\n",
            "scalar": f"---\n{ok.format(i='scalar')}note: |\n---\nBody.\n",
            "dupkey": "---\nid: dupkey\nstatus: active\nsensitivity: private\nsensitivity: public-ok\n---\nBody.\n",
            "unterminated": f"---\n{ok.format(i='unterminated')}\nBody never closes.\n",
            "empty": "---\n---\nBody.\n",
            "nofront": "Just text, no front matter.\n",
        }
        for name, content in bad.items():
            self.rawfact(name, content)
        line = self.facts_line()
        self.assertIn(f"{len(bad)} total", line)
        self.assertIn(f"(blocked-frontmatter)={len(bad)}", line)
        self.assertNotIn("public-ok", line)


class NoUploadBridgeTests(Base):
    """The product has no upload bridge: the checks are the nine local ones and the CLI has no command that exports facts."""

    def test_doctor_and_status_only_know_local_things(self):
        for args in (("doctor",), ("status",)):
            code, out, err = self.out(*args)
            self.assertEqual(code, 0, err)
            self.assertNotRegex(out + err, r"(?i)promote|bundle|ledger|namespace")

    def test_there_is_no_promote_command(self):
        code, out, err = self.out("promote")
        self.assertNotEqual(code, 0)
        self.assertIn("invalid choice", err)


class SkillsSafetyTests(Base):
    def setUp(self):
        super().setUp()
        src = self.eng / "skills-src" / "demo"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("---\nname: demo\n---\nversion one\n", encoding="utf-8")
        (src / "ref.md").write_text("reference\n", encoding="utf-8")
        (self.home / ".agents").mkdir()
        self.cfg = {"skills": ["demo"], "converter": None, "targets": {"agents": "{home}/.agents/skills"}}
        self.write_cfg()
        self.victim = self.tmp / "victim.txt"
        self.victim.write_text("VICTIM\n", encoding="utf-8")
        self.t_agents = self.home / ".agents" / "skills" / "demo"

    def write_cfg(self):
        (self.eng / "suite-skills.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def symlink(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")

    def test_symlinked_target_file_not_written_through(self):
        self.out("skills", "push", "--apply")
        (self.t_agents / "SKILL.md").unlink()
        self.symlink(self.victim, self.t_agents / "SKILL.md")
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM\n")
        self.assertTrue((self.t_agents / "SKILL.md").is_symlink())
        self.assertIn("SKIP-UNSAFE", out)

    def test_symlinked_parent_dir_not_written_through(self):
        (self.eng / "skills-src" / "demo" / "sub").mkdir()
        (self.eng / "skills-src" / "demo" / "sub" / "x.md").write_text("x\n", encoding="utf-8")
        self.out("skills", "push", "--apply")
        shutil.rmtree(self.t_agents / "sub")
        outside = self.tmp / "outside-dir"
        outside.mkdir()
        self.symlink(outside, self.t_agents / "sub")
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("SKIP-UNSAFE", out)

    def test_symlinked_skill_dir_not_written_through(self):
        outside = self.tmp / "outside-skill"
        outside.mkdir()
        (self.home / ".agents" / "skills").mkdir()
        self.symlink(outside, self.t_agents)
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("SKIP-UNSAFE", out)

    def test_hardlinked_target_file_is_replaced_not_overwritten(self):
        self.out("skills", "push", "--apply")
        target = self.t_agents / "SKILL.md"
        target.unlink()
        try:
            os.link(self.victim, target)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks not available")
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "VICTIM\n")          # shared inode untouched
        self.assertEqual(target.read_text(encoding="utf-8"), "---\nname: demo\n---\nversion one\n")
        self.assertEqual([p.name for p in self.t_agents.iterdir() if p.name.startswith(".suite-tmp")], [])

    def converter(self):
        (self.eng / "skill-convert.py").write_text(
            "from pathlib import Path\n"
            "def convert(skill_dir, target, target_name=None):\n"
            "    Path(target).mkdir(parents=True, exist_ok=True)\n"
            "    (Path(target) / 'CONVERTED.txt').write_text('x', encoding='utf-8')\n", encoding="utf-8")

    def test_target_equal_to_source_is_not_clobbered(self):
        self.converter()
        self.cfg["targets"] = {"loop": "{engine}/skills-src"}
        self.write_cfg()
        code, out, err = self.out("skills", "push", "--apply")
        self.assertFalse((self.eng / "skills-src" / "demo" / "CONVERTED.txt").exists())
        self.assertIn("SKIP-UNSAFE", out)

    def test_symlink_to_source_root_is_not_clobbered(self):
        self.converter()
        self.symlink(self.eng / "skills-src", self.home / "lnk")
        self.cfg["targets"] = {"loop": "{home}/lnk"}
        self.write_cfg()
        code, out, err = self.out("skills", "push", "--apply")
        self.assertFalse((self.eng / "skills-src" / "demo" / "CONVERTED.txt").exists())
        self.assertIn("SKIP-UNSAFE", out)

    def test_invalid_skill_names_from_config_are_skipped(self):
        evil = self.eng / "evil"
        evil.mkdir()
        (evil / "SKILL.md").write_text("evil\n", encoding="utf-8")
        self.cfg["skills"] = ["../evil", "demo", "/abs", "a/b", "..", ".hidden", "a\\b", "ok..bad"]
        self.write_cfg()
        code, out, err = self.out("skills", "push", "--apply")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("[skip] invalid skill name"), 7)
        self.assertFalse((self.home / ".agents" / "evil").exists())
        self.assertFalse((self.home / ".agents" / "skills" / ".." / "evil").exists())
        self.assertTrue((self.t_agents / "SKILL.md").is_file())

    def test_list_and_diff_and_dry_run_do_not_seed(self):
        shutil.rmtree(self.eng / "skills-src")
        seed = self.eng / "claude-ai-skill" / "infinite-context"
        seed.mkdir(parents=True)
        (seed / "SKILL.md").write_text("seed\n", encoding="utf-8")
        self.cfg["skills"] = []
        self.write_cfg()
        for args in (("list",), ("diff",), ("push",)):
            code, out, err = self.out("skills", *args)
            self.assertFalse((self.eng / "skills-src").exists(), args)
            self.assertIn("missing", out)
        code, out, err = self.out("skills", "init")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.eng / "skills-src" / "infinite-context" / "SKILL.md").is_file())
        self.assertTrue((seed / "SKILL.md").is_file())

    def test_failure_names_the_backup(self):
        self.out("skills", "push", "--apply")
        (self.t_agents / "SKILL.md").write_text("LOCAL\n", encoding="utf-8")
        (self.eng / "skill-convert.py").write_text("def convert(skill_dir, target):\n    raise RuntimeError('boom')\n", encoding="utf-8")
        code, out, _ = self.out("skills", "push", "--apply")
        self.assertEqual(code, 1)
        fail = [ln for ln in out.splitlines() if "FAIL" in ln][0]
        self.assertIn("boom", fail)
        self.assertRegex(fail, r"backup: .*skills-")


class DoctorHardeningTests(Base):
    def doctor(self, **envextra):
        return doctor_statuses(self.out("doctor", env=dict(self.env, **envextra))[1])

    def test_future_heartbeat_is_not_pass(self):
        (self.eng / "capture-service-status.json").write_text(json.dumps({"timestamp": "2026-10-31T10:00:00Z"}), encoding="utf-8")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T09:00:00Z")[1], "WARN")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T09:57:00Z")[1], "PASS")       # small skew (3 min ahead) tolerated

    def test_timezone_less_heartbeat_is_not_pass(self):
        (self.eng / "capture-service-status.json").write_text(json.dumps({"timestamp": "2026-10-31T10:00:00"}), encoding="utf-8")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T10:01:00Z")[1], "WARN")

    def test_future_hook_event_is_not_pass(self):
        (self.eng / "logs").mkdir()
        (self.eng / "logs" / "hook-events.jsonl").write_text(json.dumps({"ts": "2026-10-31T12:00:00Z", "event": "Stop", "ms": 3}) + "\n", encoding="utf-8")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T09:00:00Z")[3], "WARN")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T12:01:00Z")[3], "PASS")

    def test_file_mtime_is_not_evidence_of_last_message(self):
        d = self.vault / "40-Conversation-Capture" / "c1"
        d.mkdir(parents=True)
        (d / "m.md").write_text("no front matter, but brand-new mtime\n", encoding="utf-8")
        self.assertEqual(self.doctor()[2], "UNKNOWN")
        (d / "m.md").write_text("---\nupdated: 2026-10-31T09:59:00Z\n---\nbody\n", encoding="utf-8")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T10:00:00Z")[2], "PASS")
        (d / "m.md").write_text("---\nupdated: 2026-10-31T23:59:00Z\n---\nbody\n", encoding="utf-8")
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T10:00:00Z")[2], "WARN")

    def test_capture_state_saved_at_counts_as_evidence(self):
        con = sqlite3.connect(str(self.eng / "capture-state.sqlite3"))
        con.execute("create table seen(id text, saved_at text)")
        con.execute("insert into seen values('a', '2026-10-31T09:59:00Z')")
        con.commit()
        con.close()
        self.assertEqual(self.doctor(SUITE_NOW="2026-10-31T10:00:00Z")[2], "PASS")

    def test_claude_registration_needs_the_dispatcher_command(self):
        settings = Path(self.env["SUITE_CLAUDE_SETTINGS"])
        events = ("UserPromptSubmit", "SessionStart", "PreCompact", "SessionEnd", "Stop")
        hooks = {ev: [{"hooks": [{"type": "command", "command": "python other.py # claude_hook"}]}] for ev in events}
        settings.write_text(json.dumps({"hooks": hooks, "note": "claude_hook.py"}), encoding="utf-8")
        self.assertEqual(self.doctor()[4], "FAIL")
        hooks = {ev: [{"hooks": [{"type": "command", "command": "python x/not_claude_hook.py.bak"}]}] for ev in events}
        settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        self.assertEqual(self.doctor()[4], "FAIL")
        settings.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.doctor()[4], "WARN")

    def test_codex_comment_is_not_a_reference(self):
        cfg = Path(self.env["SUITE_CODEX_CONFIG"])
        cfg.write_text("# command = \"py x/claude_hook.py\"\n   # claude_hook.py\n[hooks]\nname = \"x\" # claude_hook.py\n", encoding="utf-8")
        self.assertEqual(self.doctor()[5], "WARN")
        cfg.write_text("[hooks]\ncommand = \"py x/claude_hook.py\" # real\n", encoding="utf-8")
        self.assertEqual(self.doctor()[5], "PASS")
        cfg.write_text("[hooks]\ncommand = \"py x/topic_router.py\"\n", encoding="utf-8")
        self.assertEqual(self.doctor()[5], "WARN")

    def test_a_fresh_install_without_chat_on_steroids_or_a_session_is_not_an_alarm(self):
        statuses = self.doctor()
        self.assertEqual(statuses[1], "UNKNOWN", "no Chat On Steroids here, so no capture service to watch")
        self.assertEqual(statuses[3], "UNKNOWN", "the hooks have not had a session yet")
        (self.appdata / "chat-on-steroids").mkdir()
        self.assertEqual(self.doctor()[1], "FAIL", "with the app installed, a capture service that never ran is a problem")

    def test_codex_counts_as_connected_through_the_memory_server(self):
        cfg = Path(self.env["SUITE_CODEX_CONFIG"])
        cfg.write_text('model = "x"\n[mcp_servers.other]\ncommand = "y"\n', encoding="utf-8")
        self.assertEqual(self.doctor()[5], "WARN")
        cfg.write_text('model = "x"\n\n# added\n[mcp_servers.infinity-memory]\ncommand = "py"\nargs = ["x"]\n', encoding="utf-8")
        self.assertEqual(self.doctor()[5], "PASS")
        cfg.write_text('[mcp_servers."infinity-memory"] # ours\ncommand = "py"\n', encoding="utf-8")
        self.assertEqual(self.doctor()[5], "PASS")
        cfg.write_text('# [mcp_servers.infinity-memory]\n', encoding="utf-8")
        self.assertEqual(self.doctor()[5], "WARN", "a comment is not a registration")

    def test_index_check_copies_wal_and_shm(self):
        mod = load_suite_module()
        db = self.tmp / "wal.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("pragma journal_mode=wal")
        con.execute("pragma wal_autocheckpoint=0")
        con.execute("create table t(x)")
        con.executemany("insert into t values(?)", [(i,) for i in range(5)])
        con.commit()
        try:
            self.assertTrue(Path(str(db) + "-wal").stat().st_size > 0)
            count = mod.sqlite_copy_query(db, lambda c: c.execute("select count(*) from t").fetchone()[0])
            self.assertEqual(count, 5)
        finally:
            con.close()

    def test_appdata_falls_back_to_appdata_env(self):
        roaming = self.tmp / "roaming"
        (roaming / "cos").mkdir(parents=True)
        (self.eng / "skills-src" / "demo").mkdir(parents=True)
        (self.eng / "skills-src" / "demo" / "SKILL.md").write_text("x\n", encoding="utf-8")
        (self.eng / "suite-skills.json").write_text(json.dumps({"skills": ["demo"], "targets": {"cos": "{appdata}/cos/skills"}}), encoding="utf-8")
        env = {k: v for k, v in self.env.items() if k != "SUITE_APPDATA"}
        env["APPDATA"] = str(roaming)
        out = self.out("skills", "list", env=env)[1]
        self.assertIn(str(roaming / "cos" / "skills" / "demo"), out)
        env["SUITE_APPDATA"] = str(self.appdata)                     # explicit override still wins
        out = self.out("skills", "list", env=env)[1]
        self.assertIn(str(self.appdata / "cos" / "skills" / "demo"), out)


if __name__ == "__main__":
    unittest.main()
