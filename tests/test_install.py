"""install.py: the installer, run for real against a fake home with fake agent folders (nothing touches the real machine).

HAMZABAN_HOME relocates "the user's home" for the installer and for every engine script it starts. The background-task step
is replaced by a fake: registering a real task is not something a test may do.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import install  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hz-install-test-"))
        self.home, self.vault = self.tmp / "home", self.tmp / "vault"
        for d in (".claude/skills/my-own-skill", ".codex", "AppData/Roaming/Claude"):
            (self.home / d).mkdir(parents=True)
        self.claude_json = self.home / ".claude.json"
        self.claude_json.write_text(json.dumps({"numStartups": 3, "mcpServers": {}}), encoding="utf-8")
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-hook.sh"}]}]}}), encoding="utf-8")
        self.codex = self.home / ".codex" / "config.toml"
        self.codex.write_text('model = "x"\n', encoding="utf-8")
        (self.home / ".claude" / "skills" / "my-own-skill" / "SKILL.md").write_text("---\nname: my-own-skill\ndescription: mine\n---\nhello\n", encoding="utf-8")
        self.desktop = self.home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        patches = [mock.patch.dict(os.environ, {"HAMZABAN_HOME": str(self.home)}),
                   mock.patch.object(install, "SETTLE_WAIT", 0), mock.patch.object(install.time, "sleep", lambda *_: None),
                   mock.patch.object(install, "schedule", lambda engine, h, ui, remove=False, run=None: [f"fake schedule {'removed' if remove else 'registered'}"])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_install(self, *args, interactive=False, answers=()):
        lines = []
        feed = iter(answers)
        code = install.main(list(args), inp=lambda *_: next(feed), out=lines.append, interactive=interactive)
        return code, "\n".join(lines)

    def agent_files(self):
        return [self.claude_json, self.settings, self.codex, self.desktop]


class FreshInstall(Sandbox):
    def test_everything_lands_where_it_should_and_nothing_of_the_users_is_lost(self):
        code, out = self.run_install("--vault", str(self.vault), "--yes", "--no-open")
        self.assertEqual(code, 0, out)
        eng = self.vault / ".ai-memory"
        for rel in ("suite.py", "autosync.py", "infinity_mcp.py", "hamzaban/cli.py", "native_memory.py", "connect_all.py"):
            self.assertTrue((eng / rel).is_file(), rel)
        self.assertFalse((eng / "tests").exists(), "the tests are not installed")
        for d in install.VAULT_DIRS:
            self.assertTrue((self.vault / d).is_dir(), d)
        self.assertTrue((self.vault / "00-Context" / "Welcome.md").is_file())
        self.assertIn("Reply in the language I write in", (self.vault / "00-Context" / "Harness.md").read_text(encoding="utf-8"))
        cfg = json.loads((eng / "suite-skills.json").read_text(encoding="utf-8"))
        self.assertTrue(cfg["protect_existing"])
        # skills: ours are added with the real paths filled in, the user's own is copied to the other agent, never removed
        for name in ("hamzaban-close", "hamzaban-continue", "infinite-context"):
            text = (eng / "skills-src" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("{{", text)
        self.assertIn(str(self.vault.resolve()), (eng / "skills-src" / "infinite-context" / "SKILL.md").read_text(encoding="utf-8"))
        # the installer waits 22 s for new files to settle before the sharing pass; here the wait is switched off, so let the
        # sync see files that are "old enough" (settle window 0) and run its next passes
        cfg_path = eng / "suite-skills.json"
        cfg_path.write_text(json.dumps(dict(json.loads(cfg_path.read_text(encoding="utf-8")), autosync_settle_seconds=0)), encoding="utf-8")
        for _ in range(3):
            install.run_engine(eng, self.home, "autosync.py", "once")
            if (self.home / ".codex" / "skills" / "my-own-skill" / "SKILL.md").is_file():
                break
        self.assertTrue((self.home / ".codex" / "skills" / "my-own-skill" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "skills" / "my-own-skill" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "skills" / "hamzaban-close" / "SKILL.md").is_file())
        # the memory server is registered everywhere, next to what was already there
        self.assertIn("infinity-memory", json.loads(self.claude_json.read_text(encoding="utf-8"))["mcpServers"])
        self.assertEqual(json.loads(self.claude_json.read_text(encoding="utf-8"))["numStartups"], 3)
        self.assertIn("[mcp_servers.infinity-memory]", self.codex.read_text(encoding="utf-8"))
        self.assertIn("infinity-memory", json.loads(self.desktop.read_text(encoding="utf-8"))["mcpServers"])
        hooks = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks), {"UserPromptSubmit", "SessionStart", "PreCompact", "SessionEnd", "Stop"})
        self.assertIn("my-hook.sh", json.dumps(hooks["Stop"]), "the user's own hook stays")
        # backups before every change outside the Vault
        self.assertTrue(list(self.home.glob(".claude.json.bak-*")))
        self.assertTrue(list((self.home / ".claude").glob("settings.json.bak-*")))
        # a first run happened and the app is usable
        self.assertTrue((self.vault / "00-Dashboard" / "control-room.html").is_file())
        self.assertTrue((eng / "context-index.sqlite3").is_file())
        record = json.loads(install.record_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(Path(record["vault"]).resolve(), self.vault.resolve())   # the OS may spell the temp dir differently
        self.assertIn("0 FAIL", out)
        self.assertNotIn("Traceback", out)

    def test_a_dry_run_changes_nothing_at_all(self):
        before = [digest(p) for p in self.agent_files()]
        code, out = self.run_install("--vault", str(self.vault), "--yes", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.vault.exists())
        self.assertFalse(install.record_path(self.home).exists())
        self.assertEqual([digest(p) for p in self.agent_files()], before)
        self.assertFalse(list(self.home.rglob("*.bak-*")))
        self.assertIn("dry run", out)
        self.assertIn("would", out)

    def test_without_yes_and_without_a_terminal_only_the_vault_is_touched(self):
        before = [digest(p) for p in self.agent_files()]
        code, out = self.run_install("--vault", str(self.vault), "--no-open")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.vault / ".ai-memory" / "suite.py").is_file())
        self.assertEqual([digest(p) for p in self.agent_files()], before, "no terminal and no --yes: nothing outside the Vault changes")
        self.assertIn("not done", out)

    def test_interactive_answers_decide_each_step(self):
        # order of questions: skills, mcp, hooks, schedule
        code, out = self.run_install("--vault", str(self.vault), "--no-open", interactive=True, answers=["n", "y", "n", "n"])
        self.assertEqual(code, 0, out)
        self.assertIn("infinity-memory", self.claude_json.read_text(encoding="utf-8"))
        self.assertNotIn("claude_hook", self.settings.read_text(encoding="utf-8"))
        self.assertFalse((self.vault / ".ai-memory" / "skills-src").exists())

    def test_skipped_steps_are_skipped(self):
        code, out = self.run_install("--vault", str(self.vault), "--yes", "--no-open", "--skip", "mcp,hooks,skills,schedule")
        self.assertEqual(code, 0, out)
        self.assertNotIn("infinity-memory", self.claude_json.read_text(encoding="utf-8"))
        self.assertEqual(out.count("skipped (--skip)"), 4)


class UpdateAndUninstall(Sandbox):
    def test_a_second_run_changes_nothing_and_a_damaged_engine_file_is_restored_with_a_backup(self):
        self.run_install("--vault", str(self.vault), "--yes", "--no-open", "--skip", "skills")
        eng = self.vault / ".ai-memory"
        before = [digest(p) for p in self.agent_files()]
        harness = self.vault / "00-Context" / "Harness.md"
        harness.write_text("my own rules\n", encoding="utf-8")
        (self.vault / "10-Facts").mkdir(exist_ok=True)
        (self.vault / "10-Facts" / "mine.md").write_text("a fact of mine\n", encoding="utf-8")
        (eng / "suite-skills.json").write_text('{"version": 1, "skills": [], "targets": {}}', encoding="utf-8")
        (eng / "suite.py").write_text("# damaged\n", encoding="utf-8")
        code, out = self.run_install("--vault", str(self.vault), "--yes", "--no-open", "--skip", "skills")
        self.assertEqual(code, 0, out)
        self.assertIn("1 updated", out)
        self.assertEqual(digest(eng / "suite.py"), digest(install.ENGINE_SRC / "suite.py"))
        self.assertEqual((next((eng / "backups").glob("install-*")) / "suite.py").read_text(encoding="utf-8"), "# damaged\n")
        self.assertEqual(harness.read_text(encoding="utf-8"), "my own rules\n")
        self.assertEqual((self.vault / "10-Facts" / "mine.md").read_text(encoding="utf-8"), "a fact of mine\n")
        self.assertIn('"targets": {}', (eng / "suite-skills.json").read_text(encoding="utf-8"))
        self.assertEqual([digest(p) for p in self.agent_files()], before, "already registered: the agents' files are not rewritten")

    def test_uninstall_takes_the_integrations_out_and_keeps_every_note(self):
        self.run_install("--vault", str(self.vault), "--yes", "--no-open")
        (self.vault / "10-Facts").mkdir(exist_ok=True)
        (self.vault / "10-Facts" / "mine.md").write_text("a fact of mine\n", encoding="utf-8")
        code, out = self.run_install("--uninstall", "--yes")
        self.assertEqual(code, 0, out)
        self.assertNotIn("infinity-memory", self.claude_json.read_text(encoding="utf-8"))
        self.assertNotIn("infinity-memory", self.codex.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(self.desktop.read_text(encoding="utf-8"))["mcpServers"], {})
        hooks = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks), {"Stop"})
        self.assertIn("my-hook.sh", json.dumps(hooks))
        self.assertEqual((self.vault / "10-Facts" / "mine.md").read_text(encoding="utf-8"), "a fact of mine\n")
        self.assertTrue((self.vault / ".ai-memory" / "suite.py").is_file(), "the engine and its data stay")
        self.assertIn("fake schedule removed", out)

    def test_uninstall_without_an_install_says_so(self):
        code, out = self.run_install("--uninstall", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("No installed copy", out)

    def test_doctor_reads_the_installed_copy(self):
        self.run_install("--vault", str(self.vault), "--yes", "--no-open", "--skip", "skills")
        code, out = self.run_install("--doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("Summary:", out)


class Guards(Sandbox):
    def test_a_vault_that_would_be_a_mistake_is_refused(self):
        for bad in (self.home, Path(self.home.anchor), ROOT / "engine"):
            self.assertIsNotNone(install.check_vault(bad.resolve(), self.home), str(bad))
        (self.tmp / "afile").write_text("x", encoding="utf-8")
        self.assertEqual(install.check_vault(self.tmp / "afile", self.home), "that path is a file")
        self.assertIsNone(install.check_vault(self.tmp / "fine", self.home))
        code, out = self.run_install("--vault", str(self.home), "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Cannot use", out)

    def test_unknown_steps_and_an_old_python_are_reported(self):
        with self.assertRaises(SystemExit):
            self.run_install("--skip", "coffee")
        with mock.patch.object(install, "MIN_PY", (99, 0)):
            code, out = self.run_install("--vault", str(self.vault), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Python 99.0 or newer", out)
        self.assertFalse(self.vault.exists())

    def test_an_obsidian_vault_is_offered_and_a_typed_path_wins(self):
        base = (install.appdata(self.home) / "obsidian" if install.is_windows()
                else self.home / "Library" / "Application Support" / "obsidian" if sys.platform == "darwin" else self.home / ".config" / "obsidian")
        known = self.tmp / "MyNotes"
        known.mkdir()
        base.mkdir(parents=True)
        (base / "obsidian.json").write_text(json.dumps({"vaults": {"abc": {"path": str(known), "ts": 1}}}), encoding="utf-8")
        self.assertEqual(install.obsidian_vaults(self.home), [known])
        ui = install.Ui(inp=lambda *_: "2", out=lambda *_: None, interactive=True)
        self.assertEqual(install.choose_vault(ui, None, self.home), known.resolve())
        typed = self.tmp / "typed"
        ui = install.Ui(inp=lambda *_: str(typed), out=lambda *_: None, interactive=True)
        self.assertEqual(install.choose_vault(ui, None, self.home), typed.resolve())
        ui = install.Ui(yes=True, out=lambda *_: None, interactive=False)
        self.assertEqual(install.choose_vault(ui, None, self.home), (self.home / "Documents" / "Hamzaban-Vault").resolve())


class Scheduling(unittest.TestCase):
    """The text and the commands for the background task on macOS and Linux (the Windows task is made by manage_autosync.ps1)."""

    ENGINE = Path("/Users/me/Vault & Notes/.ai-memory")

    def test_launchd_plist_is_valid_xml_and_runs_autosync_once_a_minute(self):
        doc = xml.dom.minidom.parseString(install.launchd_plist("/usr/bin/python3", self.ENGINE))
        strings = [n.firstChild.data for n in doc.getElementsByTagName("string") if n.firstChild]
        self.assertIn("com.hamzaban.autosync", strings)
        self.assertIn(str(self.ENGINE / "autosync.py"), strings)
        self.assertEqual(strings[strings.index("/usr/bin/python3") + 2], "once")
        self.assertEqual(doc.getElementsByTagName("integer")[0].firstChild.data, "60")

    def test_cron_edit_replaces_only_our_line(self):
        line = install.cron_line("/usr/bin/python3", self.ENGINE)
        self.assertIn(install.CRON_MARK, line)
        self.assertTrue(line.startswith("* * * * * cd "))
        mine = "0 3 * * * backup.sh"
        added = install.cron_edit(mine + "\n", line)
        self.assertEqual(added.splitlines(), [mine, line])
        newer = install.cron_line("/opt/python3.12", self.ENGINE)
        self.assertEqual(install.cron_edit(added, newer).splitlines(), [mine, newer])
        self.assertEqual(install.cron_edit(added, None), mine + "\n")
        self.assertEqual(install.cron_edit("", None), "")

    def test_linux_writes_a_crontab_and_macos_a_launch_agent(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get("input")))
            return mock.Mock(returncode=0, stdout=b"0 3 * * * backup.sh\n", stderr=b"")

        tmp = Path(tempfile.mkdtemp(prefix="hz-sched-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        ui = install.Ui(yes=True, out=lambda *_: None, interactive=False)
        with mock.patch.object(install, "is_windows", lambda: False), mock.patch.object(install.shutil, "which", lambda n: "/usr/bin/crontab"), \
                mock.patch.object(install.sys, "platform", "linux"):
            install.schedule(tmp / "eng", tmp, ui, run=fake_run)
        sent = [inp for cmd, inp in calls if cmd[:2] == ["crontab", "-"]][0].decode("utf-8")
        self.assertIn("0 3 * * * backup.sh", sent)
        self.assertIn(install.CRON_MARK, sent)
        calls.clear()
        with mock.patch.object(install, "is_windows", lambda: False), mock.patch.object(install.sys, "platform", "darwin"):
            lines = install.schedule(tmp / "eng", tmp, ui, run=fake_run)
        plist = tmp / "Library" / "LaunchAgents" / "com.hamzaban.autosync.plist"
        self.assertTrue(plist.is_file())
        self.assertTrue(any(cmd[:3] == ["launchctl", "load", "-w"] for cmd, _ in calls))
        self.assertIn("loaded", lines[0])
        calls.clear()
        with mock.patch.object(install, "is_windows", lambda: False), mock.patch.object(install.sys, "platform", "darwin"):
            install.schedule(tmp / "eng", tmp, ui, remove=True, run=fake_run)
        self.assertFalse(plist.exists())


if __name__ == "__main__":
    unittest.main()
