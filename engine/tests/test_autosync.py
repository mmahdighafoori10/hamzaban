"""Tests for autosync.py (cross-agent skill fan-out, install queue, project mirroring, notice, doctor check 9).

Everything runs in temp dirs through SUITE_HOME / SUITE_APPDATA / SUITE_USER_HOME; nothing touches a real vault.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
ROOT_NAMES = ("agents", "codex", "claude", "cos")


def snapshot(path):
    """{relative path: sha256} of every regular file under path (symlinks not followed)."""
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(path):
        for fname in filenames:
            full = Path(dirpath) / fname
            if full.is_symlink():
                out[full.relative_to(path).as_posix() + "@link"] = os.readlink(full)
            else:
                out[full.relative_to(path).as_posix()] = hashlib.sha256(full.read_bytes()).hexdigest()
    return out


class AutosyncBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autosync-test-"))
        self.vault = self.tmp / "vault"
        self.eng = self.vault / ".ai-memory"
        self.eng.mkdir(parents=True)
        for f in SRC.glob("*.py"):
            shutil.copy(f, self.eng / f.name)
        self.home = self.tmp / "home"
        self.appdata = self.tmp / "appdata"
        self.roots = {
            "agents": self.home / ".agents" / "skills",
            "codex": self.home / ".codex" / "skills",
            "claude": self.home / ".claude" / "skills",
            "cos": self.appdata / "chat-on-steroids" / "skills",
        }
        for p in self.roots.values():
            p.mkdir(parents=True)
        self.src = self.eng / "skills-src"
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                        SUITE_HOME=str(self.vault), SUITE_APPDATA=str(self.appdata), SUITE_USER_HOME=str(self.home),
                        SUITE_CLAUDE_SETTINGS=str(self.tmp / "nope-settings.json"),
                        SUITE_CODEX_CONFIG=str(self.tmp / "nope-codex.toml"),
                        INFINITE_CTX_VAULT=str(self.vault))
        self.env.pop("AUTOSYNC_TEST_FAIL_ROOTS", None)
        self.env.pop("SUITE_NOW", None)
        self.config = {"version": 1, "skills": [], "autosync_settle_seconds": 0,
                       "targets": {"agents": "{home}/.agents/skills", "codex": "{home}/.codex/skills",
                                   "claude": "{home}/.claude/skills", "cos": "{appdata}/chat-on-steroids/skills"},
                       "converter": None}
        self.write_config()

    def tearDown(self):
        for p in self.tmp.rglob("*"):  # chmod tests leave read-only dirs behind
            try:
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def write_config(self, **extra):
        self.config.update(extra)
        (self.eng / "suite-skills.json").write_text(json.dumps(self.config), encoding="utf-8")

    def mkskill(self, root, name, body="body one", desc="a test skill", extra=None, front_extra="", raw=None):
        d = (self.roots[root] if isinstance(root, str) and root in self.roots else Path(root)) / name
        d.mkdir(parents=True, exist_ok=True)
        text = raw if raw is not None else f"---\nname: {name}\ndescription: {desc}\n{front_extra}---\n{body}\n"
        (d / "SKILL.md").write_text(text, encoding="utf-8", newline="")
        for rel, content in (extra or {}).items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(content, encoding="utf-8")
        return d

    def run_py(self, script, *args, env=None, stdin=None):
        return subprocess.run([sys.executable, str(self.eng / script), *args], input=stdin, capture_output=True,
                              env=env or self.env, timeout=90)

    def once(self, *args, env=None):
        r = self.run_py("autosync.py", "once", *args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"Traceback", r.stderr)
        return r.stdout.decode("utf-8", "replace")

    def state(self):
        return json.loads((self.eng / "autosync-state.json").read_text(encoding="utf-8"))

    def status(self):
        return json.loads((self.eng / "autosync-status.json").read_text(encoding="utf-8"))

    def backups(self):
        return sorted((self.eng / "backups").glob("auto-*")) if (self.eng / "backups").is_dir() else []

    def baseline(self):
        self.once()
        self.assertTrue(self.state()["baseline_done"])

    def installed_everywhere(self, name="alpha", where="agents"):
        """baseline, then a new skill in `where` fans out to every other root and skills-src."""
        self.baseline()
        self.mkskill(where, name, body="v1 body", extra={"scripts/run.py": "print('v1')\n"})
        self.once()
        for r in ROOT_NAMES:
            self.assertTrue((self.roots[r] / name / "SKILL.md").is_file(), r)
        self.assertTrue((self.src / name / "SKILL.md").is_file())


class SkillFanOut(AutosyncBase):
    def test_a_capped_root_is_never_pushed_over_its_limit(self):
        # Chat On Steroids turns its whole Skills library off above 128 skills (its own app.log says so).
        for i in range(127):
            self.mkskill("cos", f"own-{i:03}")
        self.baseline()
        self.mkskill("agents", "shared", body="v1")
        self.once()
        self.assertTrue((self.roots["cos"] / "shared" / "SKILL.md").is_file(), "the 128th skill still fits")
        self.mkskill("agents", "newcomer", body="v1")
        self.once()
        self.assertFalse((self.roots["cos"] / "newcomer").exists(), "a 129th skill must not be installed")
        self.assertTrue((self.roots["claude"] / "newcomer" / "SKILL.md").is_file(), "uncapped roots still get it")
        self.assertTrue((self.src / "newcomer" / "SKILL.md").is_file())
        skipped = [s for s in self.status()["skipped"] if s["skill"] == "newcomer"]
        self.assertEqual([s["root"] for s in skipped], ["cos"])
        self.assertIn("maximum of 128", skipped[0]["reason"])
        self.assertEqual(sum(1 for p in self.roots["cos"].iterdir() if p.is_dir()), 128)
        self.mkskill("agents", "shared", body="v2")  # an UPDATE of a skill cos already holds does not change the count
        self.once()
        self.assertIn("v2", (self.roots["cos"] / "shared" / "SKILL.md").read_text(encoding="utf-8"))

    def test_first_run_is_baseline_only(self):
        self.mkskill("codex", "alpha")
        out = self.once()
        self.assertFalse((self.roots["agents"] / "alpha").exists())
        self.assertFalse((self.src / "alpha").exists())
        self.assertIn("alpha", self.state()["skills"])
        self.assertIn("codex", self.state()["skills"]["alpha"]["roots"])
        self.assertTrue(self.status()["baseline_done"])
        self.assertEqual(self.status()["counts"]["propagated"], 0)
        self.once()  # still nothing
        self.assertFalse((self.roots["claude"] / "alpha").exists())
        self.assertEqual(self.backups(), [])

    def test_new_skill_propagates_to_every_root_and_skills_src(self):
        self.mkskill("codex", "legacy")
        self.baseline()
        crlf = "---\r\nname: alpha\r\ndescription: hello   \r\n---\r\nbody\r\n"
        d = self.mkskill("agents", "alpha", raw=crlf, extra={"scripts/run.py": "print(1)\n", "notes/x.md": "x\n"})
        try:  # creating symlinks needs Developer Mode / admin on Windows; the rest of this test does not
            os.symlink("/etc/hostname", d / "evil-link.txt")
        except (OSError, NotImplementedError):
            pass
        self.once()
        for r in ("codex", "claude", "cos"):
            self.assertTrue((self.roots[r] / "alpha" / "scripts" / "run.py").is_file(), r)
            md = (self.roots[r] / "alpha" / "SKILL.md").read_bytes()
            self.assertTrue(md.startswith(b"---\nname: alpha\ndescription: hello\n---\n"), md)  # front matter normalised by skill_formats
            self.assertFalse((self.roots[r] / "alpha" / "evil-link.txt").exists())
        self.assertEqual((self.src / "alpha" / "SKILL.md").read_bytes(), crlf.encode())  # skills-src keeps the source bytes
        self.assertTrue((self.roots["codex"] / "legacy").exists())
        self.assertFalse((self.src / "legacy").exists())        # baseline skills are not flooded out
        self.assertEqual(self.status()["counts"]["propagated"], 1)
        roots_only = {r: snapshot(self.roots[r]) for r in ROOT_NAMES}
        src_only = snapshot(self.src)
        self.once()                                             # idempotent: a second pass changes nothing
        self.assertEqual({r: snapshot(self.roots[r]) for r in ROOT_NAMES}, roots_only)
        self.assertEqual(snapshot(self.src), src_only)
        self.assertEqual(self.status()["counts"]["propagated"], 0)
        self.assertEqual(len(self.backups()), 0)               # nothing existed before, so nothing to back up

    def test_change_propagates_and_old_copies_are_backed_up(self):
        self.installed_everywhere()
        time.sleep(0.01)
        (self.roots["claude"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: a test skill\n---\nv2 body is longer\n", encoding="utf-8")
        self.once()
        for r in ("agents", "codex", "cos"):
            self.assertIn("v2 body", (self.roots[r] / "alpha" / "SKILL.md").read_text(encoding="utf-8"), r)
        self.assertIn("v2 body", (self.src / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        bk = self.backups()
        self.assertEqual(len(bk), 1)
        for label in ("src", "agents", "codex", "cos"):
            self.assertIn("v1 body", (bk[0] / label / "alpha" / "SKILL.md").read_text(encoding="utf-8"), label)
        self.assertFalse((bk[0] / "claude").exists())           # the source root is not rewritten

    def test_deletion_is_not_propagated(self):
        self.installed_everywhere()
        shutil.rmtree(self.roots["codex"] / "alpha")
        before = {r: snapshot(self.roots[r] / "alpha") for r in ("agents", "claude", "cos")}
        self.once()
        self.assertFalse((self.roots["codex"] / "alpha").exists())
        for r, snap in before.items():
            self.assertEqual(snapshot(self.roots[r] / "alpha"), snap, r)
        self.assertTrue((self.src / "alpha" / "SKILL.md").is_file())
        self.assertIn({"root": "codex", "skill": "alpha"}, self.status()["removed"])
        # a later real change elsewhere still must not re-create the copy the user removed
        (self.roots["agents"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nchanged later\n", encoding="utf-8")
        self.once()
        self.assertFalse((self.roots["codex"] / "alpha").exists())
        self.assertIn("changed later", (self.roots["cos"] / "alpha" / "SKILL.md").read_text(encoding="utf-8"))

    def test_two_sided_change_is_a_conflict_and_writes_nothing(self):
        self.installed_everywhere()
        (self.roots["codex"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nCODEX edit\n", encoding="utf-8")
        (self.roots["claude"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nCLAUDE edit!\n", encoding="utf-8")
        guarded = {r: snapshot(self.roots[r]) for r in ("agents", "cos")}
        src_before = snapshot(self.src)
        nb = len(self.backups())
        out = self.once()
        for r, snap in guarded.items():
            self.assertEqual(snapshot(self.roots[r]), snap, r)
        self.assertEqual(snapshot(self.src), src_before)
        self.assertEqual(len(self.backups()), nb)
        note = self.vault / "30-Pending" / "skill-conflict-alpha.md"
        self.assertTrue(note.is_file())
        text = note.read_text(encoding="utf-8")
        self.assertIn("codex", text)
        self.assertIn("claude", text)
        self.assertEqual(self.status()["conflicts"], ["alpha"])
        note_bytes = note.read_bytes()
        self.once()                                              # stays quiet, note not rewritten
        self.assertEqual(note.read_bytes(), note_bytes)
        self.assertEqual(self.status()["conflicts"], ["alpha"])
        # the user resolves it by making the copies identical: autosync carries on by itself
        shutil.rmtree(self.roots["claude"] / "alpha")
        shutil.copytree(self.roots["codex"] / "alpha", self.roots["claude"] / "alpha")
        self.once()
        self.assertEqual(self.status()["conflicts"], [])
        self.assertIn("CODEX edit", (self.roots["agents"] / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(note.is_file())                          # notes are never deleted

    def test_same_change_in_two_roots_is_not_a_conflict(self):
        self.installed_everywhere()
        new = "---\nname: alpha\ndescription: d\n---\nthe very same edit\n"
        for r in ("codex", "claude"):
            (self.roots[r] / "alpha" / "SKILL.md").write_text(new, encoding="utf-8")
        self.once()
        self.assertEqual(self.status()["conflicts"], [])
        self.assertIn("very same edit", (self.roots["cos"] / "alpha" / "SKILL.md").read_text(encoding="utf-8"))

    def test_locked_root_is_queued_then_retried(self):
        self.baseline()
        self.mkskill("agents", "alpha")
        env = dict(self.env, AUTOSYNC_TEST_FAIL_ROOTS="cos")
        self.once(env=env)
        for r in ("codex", "claude"):
            self.assertTrue((self.roots[r] / "alpha" / "SKILL.md").is_file(), r)
        self.assertFalse((self.roots["cos"] / "alpha").exists())
        queue = [json.loads(x) for x in (self.eng / "autosync-queue.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual([(q["skill"], q["root"]) for q in queue], [("alpha", "cos")])
        self.assertEqual(self.status()["queue_len"], 1)
        self.once(env=env)                                       # still locked: stays queued, not duplicated
        self.assertEqual(self.status()["queue_len"], 1)
        self.once()                                              # lock gone: installed without anyone doing anything
        self.assertTrue((self.roots["cos"] / "alpha" / "SKILL.md").is_file())
        self.assertEqual((self.eng / "autosync-queue.jsonl").read_text(encoding="utf-8").strip(), "")
        self.assertEqual(self.status()["queue_len"], 0)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions and a non-root user")
    def test_real_permission_error_is_queued(self):
        self.baseline()
        self.roots["cos"].chmod(0o555)
        self.mkskill("agents", "alpha")
        self.once()
        self.assertEqual(self.status()["queue_len"], 1)
        self.roots["cos"].chmod(0o755)
        self.once()
        self.assertTrue((self.roots["cos"] / "alpha" / "SKILL.md").is_file())

    def test_symlinked_skill_folder_is_skipped(self):
        self.baseline()
        ext = self.tmp / "elsewhere"
        self.mkskill(ext, "linked")
        try:
            os.symlink(ext / "linked", self.roots["codex"] / "linked", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks needs Developer Mode or admin on Windows")
        self.once()
        for r in ("agents", "claude", "cos"):
            self.assertFalse((self.roots[r] / "linked").exists(), r)
        self.assertFalse((self.src / "linked").exists())
        self.assertTrue(any(s["skill"] == "linked" and "symlink" in s["reason"] for s in self.status()["skipped"]))

    def test_invalid_skill_md_is_skipped(self):
        self.baseline()
        self.mkskill("codex", "nodesc", raw="---\nname: nodesc\n---\nbody\n")
        self.mkskill("codex", "nofront", raw="just text, no front matter\n")
        self.mkskill("codex", "good")
        self.once()
        for bad in ("nodesc", "nofront"):
            for r in ("agents", "claude", "cos"):
                self.assertFalse((self.roots[r] / bad).exists(), (bad, r))
            self.assertFalse((self.src / bad).exists())
        self.assertTrue((self.roots["claude"] / "good" / "SKILL.md").is_file())
        skipped = {s["skill"] for s in self.status()["skipped"]}
        self.assertTrue({"nodesc", "nofront"} <= skipped)

    def test_dot_and_system_folders_are_ignored(self):
        self.baseline()
        self.mkskill("codex", ".system")
        self.mkskill("codex", ".hidden")
        self.mkskill("codex", "visible")
        self.once()
        self.assertFalse((self.roots["claude"] / ".system").exists())
        self.assertFalse((self.roots["claude"] / ".hidden").exists())
        self.assertTrue((self.roots["claude"] / "visible").is_dir())

    def test_missing_root_is_not_created_unless_configured(self):
        shutil.rmtree(self.roots["cos"].parent)
        self.baseline()
        self.mkskill("agents", "alpha")
        self.once()
        self.assertTrue((self.roots["claude"] / "alpha").is_dir())
        self.assertFalse(self.roots["cos"].exists())
        self.assertFalse(self.roots["cos"].parent.exists())
        self.write_config(create_roots=True)
        self.mkskill("agents", "beta")
        self.once()
        self.assertTrue((self.roots["cos"] / "beta" / "SKILL.md").is_file())

    def test_root_that_appears_later_is_baselined_not_flooded(self):
        shutil.rmtree(self.roots["cos"].parent)
        self.baseline()
        self.mkskill("cos", "legacy")                           # 'Chat On Steroids' shows up with an old skill
        self.once()
        for r in ("agents", "codex", "claude"):
            self.assertFalse((self.roots[r] / "legacy").exists(), r)
        self.assertFalse((self.src / "legacy").exists())

    def test_bootstrap_propagates_on_first_run(self):
        self.mkskill("codex", "alpha")
        self.once("--bootstrap")
        for r in ("agents", "claude", "cos"):
            self.assertTrue((self.roots[r] / "alpha" / "SKILL.md").is_file(), r)
        self.assertTrue((self.src / "alpha" / "SKILL.md").is_file())

    def test_unsettled_skill_is_deferred(self):
        self.write_config(autosync_settle_seconds=3600)
        self.baseline()
        self.mkskill("codex", "fresh")
        self.once()
        self.assertFalse((self.roots["claude"] / "fresh").exists())
        self.assertGreaterEqual(self.status()["counts"]["deferred"], 1)
        self.write_config(autosync_settle_seconds=0)
        self.once()
        self.assertTrue((self.roots["claude"] / "fresh").is_dir())

    def test_strip_keys_are_applied_per_target_but_not_in_skills_src(self):
        self.write_config(strip_keys={"codex": ["allowed-tools"]})
        self.baseline()
        self.mkskill("claude", "alpha", front_extra="allowed-tools: Bash\n")
        self.once()
        self.assertNotIn("allowed-tools", (self.roots["codex"] / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("allowed-tools", (self.roots["agents"] / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("allowed-tools", (self.src / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        self.once()                                              # stripped copy must not look like a user change
        self.assertEqual(self.status()["counts"]["propagated"], 0)

    def test_edit_in_skills_src_fans_out_to_all_targets(self):
        self.installed_everywhere()
        (self.src / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nedited in the hub\n", encoding="utf-8")
        self.once()
        for r in ROOT_NAMES:
            self.assertIn("edited in the hub", (self.roots[r] / "alpha" / "SKILL.md").read_text(encoding="utf-8"), r)


class Robustness(AutosyncBase):
    def test_lock_prevents_overlap_and_stale_lock_is_taken_over(self):
        lock = self.eng / "autosync.lock"
        lock.write_text(json.dumps({"pid": 1, "token": "other", "ts": "x"}), encoding="utf-8")
        out = self.once()
        self.assertIn("another pass is running", out)
        self.assertFalse((self.eng / "autosync-state.json").exists())
        self.assertFalse((self.eng / "autosync-status.json").exists())
        old = time.time() - 3600
        os.utime(lock, (old, old))
        out = self.once()
        self.assertTrue((self.eng / "autosync-status.json").is_file())
        self.assertFalse(lock.exists())                          # released at the end of a normal pass

    def test_dry_run_writes_nothing_at_all(self):
        self.mkskill("codex", "alpha")
        self.once("--bootstrap")
        self.mkskill("codex", "beta")
        before = snapshot(self.tmp)
        out = self.once("--dry-run")
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn("DRY-RUN", out)
        self.assertIn("beta", out)

    def test_corrupt_state_means_new_baseline_not_a_flood(self):
        self.installed_everywhere()
        (self.eng / "autosync-state.json").write_text("{not json", encoding="utf-8")
        self.mkskill("codex", "later")
        self.once()
        self.assertFalse((self.roots["claude"] / "later").exists())
        self.assertTrue(list(self.eng.glob("autosync-state.json.corrupt-*")))
        self.assertIn("later", self.state()["skills"])

    def test_garbage_config_and_garbage_queue_never_crash(self):
        (self.eng / "suite-skills.json").write_text("][", encoding="utf-8")
        (self.eng / "autosync-queue.jsonl").write_text("not json\n{\"skill\": \"../x\", \"root\": \"cos\"}\n\x00\n", encoding="utf-8")
        self.once()
        self.assertTrue((self.eng / "autosync-status.json").is_file())
        r = self.run_py("autosync.py", "status")
        self.assertEqual(r.returncode, 0)

    def test_status_command(self):
        r = self.run_py("autosync.py", "status")
        self.assertIn(b"never ran", r.stdout)
        self.once()
        r = self.run_py("autosync.py", "status")
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"last run", r.stdout)

    def test_never_writes_outside_configured_roots_and_engine(self):
        proj = self.tmp / "work" / "listed-project"
        proj.mkdir(parents=True)
        (proj / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        decoys = self.tmp / "work" / "unlisted-project"
        decoys.mkdir(parents=True)
        (decoys / "AGENTS.md").write_text("# not mine\n", encoding="utf-8")
        self.mkskill(self.home / ".other" / "skills", "zeta")
        (self.vault / "10-Facts").mkdir()
        (self.vault / "10-Facts" / "f.md").write_text("fact\n", encoding="utf-8")
        (self.home / ".codex" / "config.toml").write_text("x=1\n", encoding="utf-8")
        (self.vault / "note.md").write_text("n\n", encoding="utf-8")
        self.write_config(projects=[str(proj)])

        def outside():
            allowed = [self.eng, self.vault / "30-Pending", self.vault / "00-Dashboard", proj, *self.roots.values()]
            snap = {}
            for rel, digest in snapshot(self.tmp).items():
                full = self.tmp / rel.replace("@link", "")
                if not any(a == full or a in full.parents for a in allowed):
                    snap[rel] = digest
            return snap

        before = outside()
        self.baseline()
        self.mkskill("codex", "alpha")
        self.mkskill("claude", "beta", extra={"a/b.txt": "x\n"})
        self.once()
        (self.roots["codex"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nlonger edit\n", encoding="utf-8")
        self.once()
        self.assertEqual(outside(), before)
        # inside the roots only skill folders appear
        for root in self.roots.values():
            for child in root.iterdir():
                self.assertIn(child.name, ("alpha", "beta"), child)
        self.assertEqual(sorted(p.name for p in proj.iterdir()), ["AGENTS.md", "CLAUDE.md"])
        self.assertEqual(sorted(p.name for p in decoys.iterdir()), ["AGENTS.md"])

    def test_doctor_check_10(self):
        def statuses(text):
            return {int(m.group(2)): m.group(1) for m in re.finditer(r"^\[(\w+)\s*\] (\d+)\. ", text, re.M)}
        r = subprocess.run([sys.executable, str(self.eng / "suite.py"), "doctor"], capture_output=True, env=self.env, timeout=90)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(statuses(r.stdout.decode())[9], "UNKNOWN")
        self.installed_everywhere()
        r = subprocess.run([sys.executable, str(self.eng / "suite.py"), "doctor"], capture_output=True, env=self.env, timeout=90)
        text = r.stdout.decode()
        self.assertEqual(statuses(text)[9], "PASS", text)
        (self.roots["codex"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nX1 edit\n", encoding="utf-8")
        (self.roots["claude"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nY22 edit\n", encoding="utf-8")
        self.once()
        r = subprocess.run([sys.executable, str(self.eng / "suite.py"), "doctor"], capture_output=True, env=self.env, timeout=90)
        text = r.stdout.decode()
        self.assertEqual(statuses(text)[9], "WARN", text)
        self.assertIn("conflicts waiting: 1", text)
        # a stale heartbeat is a FAIL
        st = self.status()
        st["last_run"] = "2020-01-01T00:00:00Z"
        (self.eng / "autosync-status.json").write_text(json.dumps(st), encoding="utf-8")
        r = subprocess.run([sys.executable, str(self.eng / "suite.py"), "doctor"], capture_output=True, env=self.env, timeout=90)
        self.assertEqual(statuses(r.stdout.decode())[9], "FAIL")


class NoticeHook(AutosyncBase):
    def hook(self, session, event="SessionStart", raw=None):
        payload = raw if raw is not None else json.dumps({"hook_event_name": event, "session_id": session, "source": "startup", "cwd": str(self.tmp)}).encode()
        r = self.run_py("claude_hook.py", env=self.env, stdin=payload)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        out = r.stdout.decode("utf-8", "replace").strip()
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""
        # every SessionStart now also carries the chat's tag (chats.py); these tests are about the autosync notice
        return re.sub(r"\[CHAT TAG\].*?(?:\n\n\n|\Z)", "", text, flags=re.S).strip()

    def test_notice_is_injected_once_per_session(self):
        self.installed_everywhere()
        notice = (self.eng / "autosync-notice.md").read_text(encoding="utf-8")
        self.assertIn("inject: skills updated: alpha", notice)
        first = self.hook("sess-1")
        self.assertIn("skills updated: alpha", first)
        self.assertIn("open a new chat to load them", first)
        self.assertLessEqual(len(first), 600)
        self.assertEqual(self.hook("sess-1"), "")                # same session, same notice: silent
        self.assertIn("skills updated: alpha", self.hook("sess-2"))   # another session is told once as well
        # a newer install is announced again to the session that already saw the old one
        self.mkskill("codex", "gamma")
        self.once(env=dict(self.env, SUITE_NOW="2090-01-01T00:00:00Z"))
        again = self.hook("sess-1")
        self.assertIn("gamma", again)
        self.assertEqual(self.hook("sess-1"), "")
        # only SessionStart carries it
        self.assertEqual(self.hook("sess-3", event="UserPromptSubmit"), "")

    def test_hook_survives_garbage(self):
        self.installed_everywhere()
        (self.eng / "autosync-notice.md").write_bytes(b"\xff\xfe garbage \x00")
        self.assertEqual(self.hook("s", raw=b"{not json"), "")
        self.assertEqual(self.hook("s"), "")
        (self.eng / "logs").mkdir(exist_ok=True)
        (self.eng / "logs" / "autosync-notice-seen.json").write_text("[1,2", encoding="utf-8")
        (self.eng / "autosync-notice.md").write_text("---\ngenerated: 2030-01-01T00:00:00Z\n---\ninject: " + "x" * 5000 + "\n", encoding="utf-8")
        text = self.hook("s9")
        self.assertLessEqual(len(text), 600)

    def test_no_notice_when_nothing_happened(self):
        self.baseline()
        self.assertEqual(self.hook("s1"), "")

    def test_conflict_is_announced(self):
        self.installed_everywhere()
        (self.roots["codex"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nX1 edit\n", encoding="utf-8")
        (self.roots["claude"] / "alpha" / "SKILL.md").write_text("---\nname: alpha\ndescription: d\n---\nY22 edit\n", encoding="utf-8")
        self.once(env=dict(self.env, SUITE_NOW="2091-01-01T00:00:00Z"))
        self.assertIn("conflicts waiting", self.hook("fresh"))


class ProjectMirroring(AutosyncBase):
    def setUp(self):
        super().setUp()
        self.proj = self.tmp / "work" / "proj"
        self.proj.mkdir(parents=True)
        self.write_config(projects=[str(self.proj)])

    def test_agents_only_gets_a_claude_pointer(self):
        (self.proj / "AGENTS.md").write_text("# Rules\nbe nice\n", encoding="utf-8")
        agents_before = (self.proj / "AGENTS.md").read_bytes()
        self.once()
        text = (self.proj / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("@AGENTS.md", text.splitlines())
        self.assertTrue(text.startswith("<!--"))
        self.assertEqual((self.proj / "AGENTS.md").read_bytes(), agents_before)
        mtime = (self.proj / "CLAUDE.md").stat().st_mtime_ns
        self.once()
        self.assertEqual((self.proj / "CLAUDE.md").stat().st_mtime_ns, mtime)
        self.assertEqual(self.status()["conflicts"], [])

    def test_claude_only_gets_an_agents_copy_that_follows_edits(self):
        (self.proj / "CLAUDE.md").write_text("# Claude rules\nuse tabs\n", encoding="utf-8")
        self.once()
        agents = (self.proj / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("autosync: copy of CLAUDE.md", agents)
        self.assertIn("use tabs", agents)
        (self.proj / "CLAUDE.md").write_text("# Claude rules\nuse spaces now\n", encoding="utf-8")
        self.once()
        self.assertIn("use spaces now", (self.proj / "AGENTS.md").read_text(encoding="utf-8"))
        bk = self.backups()
        self.assertEqual(len(bk), 1)
        self.assertIn("use tabs", next(bk[0].rglob("AGENTS.md")).read_text(encoding="utf-8"))
        self.assertEqual(self.status()["conflicts"], [])

    def test_hand_edited_copy_is_a_conflict_not_overwritten(self):
        (self.proj / "CLAUDE.md").write_text("# Claude rules\nuse tabs\n", encoding="utf-8")
        self.once()
        mine = (self.proj / "AGENTS.md").read_text(encoding="utf-8") + "\nmy own codex-only rule\n"
        (self.proj / "AGENTS.md").write_text(mine, encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("# Claude rules\nuse spaces now\n", encoding="utf-8")
        self.once()
        self.assertEqual((self.proj / "AGENTS.md").read_text(encoding="utf-8"), mine)
        self.assertEqual(len(self.status()["conflicts"]), 1)

    def test_two_different_real_files_are_a_conflict_and_untouched(self):
        (self.proj / "AGENTS.md").write_text("codex says A\n", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("claude says B\n", encoding="utf-8")
        a = snapshot(self.proj)
        self.once()
        self.assertEqual(snapshot(self.proj), a)
        notes = list((self.vault / "30-Pending").glob("project-conflict-*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn(str(self.proj), notes[0].read_text(encoding="utf-8"))
        self.assertEqual(len(self.status()["conflicts"]), 1)
        self.assertEqual(self.backups(), [])
        # making them equal resolves it without any write from autosync
        (self.proj / "CLAUDE.md").write_text("codex says A\n", encoding="utf-8")
        self.once()
        self.assertEqual(self.status()["conflicts"], [])

    def test_claude_md_that_imports_agents_plus_extras_is_fine(self):
        (self.proj / "AGENTS.md").write_text("shared rules\n", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("@AGENTS.md\n\nclaude-only extra\n", encoding="utf-8")
        before = snapshot(self.proj)
        self.once()
        self.assertEqual(snapshot(self.proj), before)
        self.assertEqual(self.status()["conflicts"], [])

    def test_identical_files_are_fine_and_missing_project_is_reported_not_created(self):
        (self.proj / "AGENTS.md").write_text("same\n", encoding="utf-8")
        (self.proj / "CLAUDE.md").write_text("same\r\n", encoding="utf-8")
        ghost = self.tmp / "work" / "ghost"
        self.write_config(projects=[str(self.proj), str(ghost), "../escape"])
        self.once()
        self.assertEqual(self.status()["conflicts"], [])
        self.assertFalse(ghost.exists())
        self.assertTrue(any(s["root"] == "project" for s in self.status()["skipped"]))


if __name__ == "__main__":
    unittest.main()



class AgentGroups(AutosyncBase):
    def test_group_siblings_get_no_duplicate(self):
        self.write_config(groups=[["agents", "codex"]])
        self.baseline()
        self.mkskill("codex", "solo")
        self.once()
        self.assertFalse((self.roots["agents"] / "solo").exists())
        for r in ("claude", "cos"):
            self.assertTrue((self.roots[r] / "solo" / "SKILL.md").is_file(), r)
        self.assertTrue((self.src / "solo" / "SKILL.md").is_file())

    def test_src_import_fans_out_once_per_group(self):
        self.write_config(groups=[["agents", "codex"]])
        self.mkskill(self.src, "anchor")   # skills-src must already exist at baseline (as in the real vault)
        self.baseline()
        self.mkskill(self.src, "fromsrc")
        self.once()
        in_group = [r for r in ("agents", "codex") if (self.roots[r] / "fromsrc").exists()]
        self.assertEqual(len(in_group), 1, in_group)
        for r in ("claude", "cos"):
            self.assertTrue((self.roots[r] / "fromsrc" / "SKILL.md").is_file(), r)

    def test_no_groups_keeps_old_behaviour(self):
        self.baseline()
        self.mkskill("codex", "solo2")
        self.once()
        self.assertTrue((self.roots["agents"] / "solo2" / "SKILL.md").is_file())



FOOTER = ("\n\n## Local CoS resource location\n\nThis is the local CoS copy of this skill. Resolve relative resource paths "
          "against the preserved skill directory below.\n\nPreserved skill directory: `C:/x/{n}`\n"
          "Preserved SKILL.md: `C:/x/{n}/SKILL.md`\n")


class SkillSharing(AutosyncBase):
    """Skills are common to every agent: what counts as 'the same', what is delivered nested, and what may catch up."""

    def listed_state(self, skill, target):
        out = self.run_py("suite.py", "skills", "list").stdout.decode("utf-8", "replace")
        inside = False
        for line in out.splitlines():
            if line.startswith(f"{skill}:"):
                inside = True
            elif inside and line.startswith("  "):
                parts = line.split()
                if parts[0] == target:
                    return parts[1]
            elif inside:
                break
        return None

    def with_footer(self, root, name, body):
        d = self.mkskill(root, name, body=body)
        md = d / "SKILL.md"
        md.write_text(md.read_text(encoding="utf-8") + FOOTER.format(n=name), encoding="utf-8")
        return d

    def test_the_cos_import_footer_is_not_drift(self):
        self.write_config(protect_existing=True)
        self.mkskill(self.src, "anchor")
        self.with_footer("cos", "imported", "same body")
        self.mkskill(self.src, "imported", body="same body")
        self.baseline()
        self.once()
        self.assertEqual(self.status()["conflicts"], [])
        self.assertIn("Local CoS resource location", (self.roots["cos"] / "imported" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(self.listed_state("imported", "cos"), "SAME", "the app's own footer is not an edit")

    def test_the_footer_is_cleaned_once_from_shared_copies_but_stays_in_cos(self):
        self.baseline()
        d = self.with_footer(self.src, "polluted", "body text")
        md = d / "SKILL.md"
        md.write_text(md.read_text(encoding="utf-8") + "\nFor child, read /skills/polluted/child/SKILL.md.\n", encoding="utf-8")
        shutil.copytree(d, self.roots["claude"] / "polluted")
        shutil.copytree(d, self.roots["cos"] / "polluted")
        self.once()
        for where in (self.src, self.roots["claude"]):
            text = (where / "polluted" / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("Local CoS resource location", text, where)
            self.assertIn("For child, read /skills/polluted/child/SKILL.md.", text, "the useful line after the footer stays")
        self.assertIn("Local CoS resource location", (self.roots["cos"] / "polluted" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(self.backups(), "touched skills are backed up first")

    def test_a_skill_nested_inside_another_skill_is_not_installed_a_second_time(self):
        self.mkskill("cos", "umbrella")
        self.mkskill(self.roots["cos"] / "umbrella", "child")      # umbrella/child/SKILL.md
        self.baseline()
        self.mkskill("agents", "child")
        self.once()
        self.assertFalse((self.roots["cos"] / "child").exists(), "COS already has it inside 'umbrella'")
        self.assertTrue((self.roots["claude"] / "child" / "SKILL.md").is_file())
        self.assertEqual(self.listed_state("child", "cos"), "IN-UMBRELLA")

    def test_a_skill_too_long_for_cos_is_not_pushed_there(self):
        self.baseline()
        self.mkskill("agents", "huge", body="x " * 50000)           # 100,000 characters: above the 96,000 the app can load
        self.once()
        self.assertFalse((self.roots["cos"] / "huge").exists())
        self.assertTrue((self.roots["claude"] / "huge" / "SKILL.md").is_file())
        self.assertEqual(self.listed_state("huge", "cos"), "OVERSIZE")
        self.assertTrue(any(s["skill"] == "huge" and s["root"] == "cos" for s in self.status()["skipped"]))

    def test_a_skill_that_did_not_fit_arrives_once_there_is_room(self):
        for i in range(128):
            self.mkskill("cos", f"own-{i:03}")
        self.baseline()
        self.mkskill("agents", "late", body="v1")
        self.once()
        self.assertFalse((self.roots["cos"] / "late").exists(), "no room yet")
        shutil.rmtree(self.roots["cos"] / "own-000")
        self.once()
        self.assertTrue((self.roots["cos"] / "late" / "SKILL.md").is_file(), "installed as soon as a slot was free")

    def test_an_old_copy_with_nothing_unique_catches_up_but_an_edited_one_does_not(self):
        self.write_config(protect_existing=True)
        self.mkskill(self.src, "anchor")
        self.mkskill("claude", "oldie", body="shared words")
        self.mkskill("agents", "edited", body="shared words")
        self.baseline()
        for name in ("oldie", "edited"):                             # the shared version grows: same text plus a new file
            self.mkskill(self.src, name, body="shared words", extra={"refs/more.md": "more\n"})
        (self.roots["agents"] / "edited" / "SKILL.md").write_text("---\nname: edited\ndescription: x\n---\nmy own notes\n", encoding="utf-8")
        self.once()
        self.assertTrue((self.roots["claude"] / "oldie" / "refs" / "more.md").is_file(), "a strict subset of the shared copy catches up")
        self.assertIn("my own notes", (self.roots["agents"] / "edited" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse((self.roots["agents"] / "edited" / "refs" / "more.md").exists(), "an edited copy is never overwritten")

    def test_a_skill_written_differently_per_agent_is_a_variant_not_drift(self):
        self.mkskill(self.src, "infinite-context", body="shared text")
        self.mkskill("claude", "infinite-context", body="text written for Claude Code")
        self.baseline()
        self.once()
        self.assertEqual(self.listed_state("infinite-context", "claude"), "VARIANT")
        self.assertIn("written for Claude Code", (self.roots["claude"] / "infinite-context" / "SKILL.md").read_text(encoding="utf-8"))


class ProtectExisting(AutosyncBase):
    def test_baseline_copy_that_differs_is_not_overwritten(self):
        self.write_config(protect_existing=True)
        self.mkskill(self.src, "anchor")
        self.mkskill("cos", "shared1", body="cos own version")
        self.baseline()
        self.mkskill(self.src, "shared1", body="codex version")
        self.once()
        self.assertIn("cos own version", (self.roots["cos"] / "shared1" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("codex version", (self.roots["claude"] / "shared1" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(any(s.get("skill") == "shared1" and s.get("root") == "cos" for s in self.status()["skipped"]))

    def test_copy_written_by_autosync_still_updates(self):
        self.write_config(protect_existing=True)
        self.mkskill(self.src, "anchor")
        self.baseline()
        self.mkskill(self.src, "mine", body="v1")
        self.once()
        self.mkskill(self.src, "mine", body="v2")
        self.once()
        for r in ("cos", "claude"):
            self.assertIn("v2", (self.roots[r] / "mine" / "SKILL.md").read_text(encoding="utf-8"), r)
