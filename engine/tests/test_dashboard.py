import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DASH_NAME = "داشبورد حافظه.md"


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dash-test-"))
        self.vault = self.tmp / "vault"
        self.eng = self.vault / ".ai-memory"
        self.eng.mkdir(parents=True)
        for f in SRC.glob("*.py"):
            shutil.copy(f, self.eng / f.name)
        home = self.tmp / "home"
        home.mkdir()
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", SUITE_HOME=str(self.vault),
                        SUITE_APPDATA=str(self.tmp / "appdata"), SUITE_USER_HOME=str(home), INFINITE_CTX_VAULT=str(self.vault),
                        SUITE_CLAUDE_SETTINGS=str(self.tmp / "nope.json"), SUITE_CODEX_CONFIG=str(self.tmp / "nope.toml"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def py(self, code):
        r = subprocess.run([sys.executable, "-c", code], cwd=str(self.eng), env=self.env, capture_output=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return r.stdout.decode("utf-8", "replace").strip()

    def test_empty_vault_writes_note_without_crashing(self):
        self.assertEqual(self.py("import dashboard; print(dashboard.write())"), "True")
        note = (self.vault / "00-Dashboard" / DASH_NAME).read_text(encoding="utf-8")
        self.assertIn("CONNECT-ALL.cmd", note)          # connect-all never ran -> it is a to-do
        self.assertIn("| ", note)
        self.assertIn("[!note]-", note)                  # technical details are folded away

    def test_unchanged_state_is_not_rewritten(self):
        self.assertEqual(self.py("import dashboard; print(dashboard.write())"), "True")
        self.assertEqual(self.py("import dashboard; print(dashboard.write())"), "False")

    def test_connect_log_is_reflected(self):
        logs = self.eng / "logs"
        logs.mkdir()
        (logs / "connect-all.log").write_text(
            "2026-11-04T10:00:00Z === connect_all 1 (python x) ===\n"
            "2026-11-04T10:00:01Z Claude Code: ADDED infinity-memory -> x\n"
            "2026-11-04T10:00:01Z Codex: FAIL (boom); nothing changed by this step\n", encoding="utf-8")
        note_text = self.py("import dashboard; print(dashboard.build()[0])")
        self.assertNotIn("CONNECT-ALL.cmd` در", note_text)  # no longer asked to run it
        self.assertIn("Codex", note_text)
        self.assertIn("logs/connect-all.log", note_text)              # the failure becomes a to-do

    def test_autosync_pass_refreshes_dashboard(self):
        r = subprocess.run([sys.executable, str(self.eng / "autosync.py"), "once"], env=self.env, capture_output=True, timeout=90)
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.vault / "00-Dashboard" / DASH_NAME).is_file())

    def test_dashboard_folder_is_not_indexed(self):
        sys.path.insert(0, str(SRC))
        import infinite_context
        self.assertIn("00-Dashboard", infinite_context.EXCLUDE)


if __name__ == "__main__":
    unittest.main()
