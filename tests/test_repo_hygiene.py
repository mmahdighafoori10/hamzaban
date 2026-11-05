"""The repository stays separate from anybody's chats, backups and notes: it is an app that many people run locally, each on their own data."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_PATHS = re.compile(r"\.(sqlite3?|db|jsonl|zip|bundle|log)$|(^|/)(backups|logs|50-Handoff|80-Transfer|30-Pending|10-Facts|10-Projects|20-Checkpoints|"
                           r"40-Conversation-Capture|60-Chats|project-continuity)(/|$)|\.hamzaban-continuity")
FIXTURES = "engine/tests/fixtures/"      # made-up notes the tests need; they are exempt from the path check only


class RepoStaysSeparate(unittest.TestCase):
    def test_gitignore_keeps_vaults_chat_logs_and_continuity_packages_out(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for wanted in ("*.sqlite3", "*.jsonl", "project-continuity/", ".hamzaban-continuity.json", "/50-Handoff/", "/80-Transfer/", "/30-Pending/",
                       "/engine/topics.json", "/engine/suite-skills.json"):
            self.assertIn(wanted, lines)

    @unittest.skipUnless((ROOT / ".git").exists() and shutil.which("git"), "needs a git checkout")
    def test_no_tracked_file_looks_like_a_vault_a_backup_or_a_chat_log(self):
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(out.returncode, 0)
        bad = [f for f in out.stdout.splitlines() if PRIVATE_PATHS.search(f) and not f.startswith(FIXTURES)]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
