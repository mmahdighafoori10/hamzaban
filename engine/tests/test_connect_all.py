"""connect_all.py and merge_claude_settings.py: registering the shared-memory server and the hooks, and taking them out again.

Everything runs as real processes in temp dirs; HOME, APPDATA and LOCALAPPDATA point at fake agent folders.
"""
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

CODEX_BEFORE = """model = "gpt-5"

[mcp_servers.other]
command = "node"
args = ["other.js"]
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="connect-test-"))
        self.eng = self.tmp / "vault" / ".ai-memory"
        self.eng.mkdir(parents=True)
        for name in ("connect_all.py", "merge_claude_settings.py"):
            shutil.copy(SRC / name, self.eng / name)
        (self.eng / "infinity_mcp.py").write_text("# stand-in server\n", encoding="utf-8")
        self.home, self.appdata, self.local = self.tmp / "home", self.tmp / "appdata", self.tmp / "local"
        for d in (self.home, self.appdata, self.local):
            d.mkdir()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", CONNECT_HOME=str(self.home), CONNECT_APPDATA=str(self.appdata),
                        CONNECT_LOCALAPPDATA=str(self.local))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_py(self, script, *args):
        r = subprocess.run([sys.executable, str(self.eng / script), *args], cwd=str(self.eng), env=self.env, capture_output=True, timeout=60)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def connect(self, *args):
        return self.run_py("connect_all.py", *args)


class ClaudeCode(Base):
    def test_add_twice_and_remove_keep_everything_else(self):
        cfg = self.home / ".claude.json"
        cfg.write_text(json.dumps({"numStartups": 7, "mcpServers": {"mine": {"command": "x"}}}), encoding="utf-8")
        code, out, err = self.connect()
        self.assertEqual(code, 0, err)
        self.assertIn("Claude Code: ADDED infinity-memory", out)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["numStartups"], 7)
        self.assertEqual(set(data["mcpServers"]), {"mine", "infinity-memory"})
        self.assertEqual(data["mcpServers"]["infinity-memory"]["args"], [str(self.eng / "infinity_mcp.py")])
        self.assertTrue(list(self.home.glob(".claude.json.bak-*")), "a backup is made before the first change")
        self.assertIn("Claude Code: OK (already present", self.connect()[1])
        code, out, _ = self.connect("--remove")
        self.assertIn("Claude Code: REMOVED infinity-memory", out)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual((data["numStartups"], set(data["mcpServers"])), (7, {"mine"}))
        self.assertIn("nothing to remove", self.connect("--remove")[1])

    def test_a_missing_or_broken_config_is_never_created_or_damaged(self):
        self.assertIn("Claude Code: SKIP", self.connect()[1])
        self.assertFalse((self.home / ".claude.json").exists())
        cfg = self.home / ".claude.json"
        cfg.write_text("{broken", encoding="utf-8")
        before = digest(cfg)
        code, out, _ = self.connect()
        self.assertEqual(code, 1)
        self.assertIn("Claude Code: FAIL", out)
        self.assertEqual(digest(cfg), before)


class Codex(Base):
    def test_the_block_is_appended_once_and_removed_exactly(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(CODEX_BEFORE, encoding="utf-8")
        self.assertIn("Codex: ADDED infinity-memory", self.connect()[1])
        text = cfg.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(CODEX_BEFORE.rstrip("\n")))
        self.assertEqual(text.count("[mcp_servers.infinity-memory]"), 1)
        self.assertIn("Codex: OK (already present)", self.connect()[1])
        self.assertIn("Codex: REMOVED infinity-memory", self.connect("--remove")[1])
        self.assertEqual(cfg.read_text(encoding="utf-8").strip(), CODEX_BEFORE.strip())

    def test_another_server_after_ours_survives_the_removal(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir()
        cfg.write_text(CODEX_BEFORE, encoding="utf-8")
        self.connect()
        cfg.write_text(cfg.read_text(encoding="utf-8") + "\n[mcp_servers.later]\ncommand = \"later\"\n", encoding="utf-8")
        self.connect("--remove")
        text = cfg.read_text(encoding="utf-8")
        self.assertNotIn("infinity-memory", text)
        self.assertIn("[mcp_servers.later]", text)
        self.assertIn("[mcp_servers.other]", text)

    def test_no_codex_folder_means_skip(self):
        self.assertIn("Codex: SKIP", self.connect()[1])
        self.assertFalse((self.home / ".codex").exists())


class ClaudeDesktop(Base):
    def test_every_claude_data_folder_gets_the_entry_and_loses_it_again(self):
        classic = self.appdata / "Claude"
        store = self.local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
        for d in (classic, store):
            d.mkdir(parents=True)
        code, out, err = self.connect()
        self.assertEqual(code, 0, err)
        for d in (classic, store):
            self.assertIn("infinity-memory", json.loads((d / "claude_desktop_config.json").read_text(encoding="utf-8"))["mcpServers"])
        self.assertIn("Claude Desktop: ADDED infinity-memory", out)
        self.connect("--remove")
        for d in (classic, store):
            self.assertEqual(json.loads((d / "claude_desktop_config.json").read_text(encoding="utf-8"))["mcpServers"], {})

    def test_no_claude_folder_means_skip(self):
        self.assertIn("Claude Desktop: SKIP", self.connect()[1])


class DryRun(Base):
    def test_a_dry_run_changes_and_logs_nothing(self):
        (self.home / ".codex").mkdir()
        (self.home / ".claude.json").write_text("{}", encoding="utf-8")
        (self.appdata / "Claude").mkdir()
        (self.home / ".codex" / "config.toml").write_text(CODEX_BEFORE, encoding="utf-8")
        files = [self.home / ".claude.json", self.home / ".codex" / "config.toml"]
        before = [digest(f) for f in files]
        code, out, _ = self.connect("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("WOULD ADD"), 3, out)
        self.assertEqual([digest(f) for f in files], before)
        self.assertFalse((self.appdata / "Claude" / "claude_desktop_config.json").exists())
        self.assertFalse((self.eng / "logs").exists(), "a dry run leaves no log either")
        self.assertFalse(list(self.tmp.rglob("*.bak-*")))


class Hooks(Base):
    def settings(self, data):
        p = self.home / ".claude" / "settings.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_register_then_remove_keeps_the_users_own_hooks(self):
        mine = {"type": "command", "command": "my-own-hook.sh"}
        p = self.settings({"theme": "dark", "hooks": {"Stop": [{"hooks": [mine]}]}})
        self.run_py("merge_claude_settings.py", "--settings", str(p), "--apply")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(set(data["hooks"]), {"UserPromptSubmit", "SessionStart", "PreCompact", "SessionEnd", "Stop"})
        self.assertIn(mine, [h for g in data["hooks"]["Stop"] for h in g["hooks"]])
        code, out, _ = self.run_py("merge_claude_settings.py", "--settings", str(p), "--remove")
        self.assertIn("Dry run", out)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), data, "without --apply nothing is written")
        self.run_py("merge_claude_settings.py", "--settings", str(p), "--remove", "--apply")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["hooks"], {"Stop": [{"hooks": [mine]}]})
        self.assertGreaterEqual(len(list(p.parent.glob("settings.json.bak-*"))), 1, "a backup is made before each change")

    def test_remove_with_nothing_registered_says_so(self):
        p = self.settings({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}})
        self.assertIn("nothing to remove", self.run_py("merge_claude_settings.py", "--settings", str(p), "--remove")[1])


if __name__ == "__main__":
    unittest.main()
