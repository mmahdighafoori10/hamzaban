"""Two hooks firing on one event both sync the same index; the loser used to die with
IntegrityError: UNIQUE constraint failed: docs.path (seen on SessionStart/resume)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

WORKER = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.dont_write_bytecode = True
import infinite_context as ic
vault, start = Path(sys.argv[2]), float(sys.argv[3])
while time.time() < start:
    pass
with ic.db_open(vault) as conn:
    print(ic.sync(vault, conn))
"""


class SyncRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sync-race-"))
        self.vault = self.tmp / "vault"
        (self.vault / "20-Checkpoints" / "p").mkdir(parents=True)
        for i in range(150):
            (self.vault / "20-Checkpoints" / "p" / f"note{i:03}.md").write_text(f"# note {i}\n" + "text " * 400, encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_first_syncs_do_not_collide(self):
        start = time.time() + 2.5
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(SRC), str(self.vault), str(start)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in range(6)]
        results = [(p.wait(timeout=90), p.stdout.read().decode(), p.stderr.read().decode()) for p in procs]
        for code, out, err in results:
            self.assertEqual(code, 0, err[-600:])
        changed = sorted(int(out.strip().strip("()").split(",")[0]) for _, out, _ in results)
        self.assertEqual(sum(changed), 150, "every file indexed exactly once across all workers")


if __name__ == "__main__":
    unittest.main()
