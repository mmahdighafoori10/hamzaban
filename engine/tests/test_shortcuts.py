"""shortcuts.py: the Start-menu entry, the desktop shortcut and the hamzaban: link. Nothing real is created: the shortcut maker is a fake that
writes empty files, the registry is a fake tree, and every path is under a temp folder."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

import shortcuts  # noqa: E402

PYW = r"C:\Python\pythonw.exe"
BASE = r"software\classes\hamzaban"


class FakeReg:
    """Just enough of winreg: a tree of keys with values, names compared without regard to case like the real one, a missing key or value
    raising FileNotFoundError, and CreateKey bringing the keys above it into being (the real one does, and a delete has to cope with it)."""
    HKEY_CURRENT_USER = "HKCU"
    HKEY_CLASSES_ROOT = "HKCR"
    REG_SZ = 1
    KEY_READ = 1
    KEY_WRITE = 2

    class Key:
        def __init__(self, root, path):
            self.root, self.path = root, path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def __init__(self, values=None):
        self.keys = {}
        for (root, path), vals in (values or {}).items():
            self.keys[(root, path.lower())] = dict(vals)

    def CreateKey(self, root, path):
        parts = path.lower().split("\\")
        for n in range(1, len(parts) + 1):
            self.keys.setdefault((root, "\\".join(parts[:n])), {})
        return self.Key(root, path.lower())

    def OpenKey(self, root, path, reserved=0, access=0):
        if (root, path.lower()) not in self.keys:
            raise FileNotFoundError("no such key")
        return self.Key(root, path.lower())

    def SetValueEx(self, key, name, reserved, typ, value):
        self.keys[(key.root, key.path)][name] = value

    def QueryValueEx(self, key, name):
        try:
            return self.keys[(key.root, key.path)][name], self.REG_SZ
        except KeyError:
            raise FileNotFoundError("no such value")

    def EnumKey(self, key, index):
        prefix = key.path + "\\"
        kids = sorted({p[len(prefix):].split("\\")[0] for (r, p) in self.keys if r == key.root and p.startswith(prefix)})
        if index >= len(kids):
            raise OSError("no more keys")
        return kids[index]

    def DeleteKey(self, root, path):
        if any(r == root and p.startswith(path.lower() + "\\") for (r, p) in self.keys):
            raise OSError("key has subkeys")
        del self.keys[(root, path.lower())]


def tree(reg, base=BASE):
    """The keys of one subtree (not its look-alike neighbours: 'hamzaban2' is not under 'hamzaban')."""
    return {path for (root, path) in reg.keys if path == base or path.startswith(base + "\\")}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hz-shortcuts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.engine = self.tmp / "engine"
        (self.engine / "assets").mkdir(parents=True)
        for name in ("hamzaban_app.pyw", "hamzaban_open.pyw"):
            (self.engine / name).write_text("# stub\n", encoding="utf-8")
        (self.engine / "assets" / "hamzaban.ico").write_bytes(b"ico")
        for p in (mock.patch.dict(os.environ, {"SUITE_USER_HOME": str(self.home)}), mock.patch.object(shortcuts, "is_windows", lambda: True),
                  mock.patch.object(shortcuts, "pythonw", lambda: PYW)):
            p.start()
            self.addCleanup(p.stop)
        self.calls = []
        self.desktop = self.home / "Desktop" / f"{shortcuts.NAME}.lnk"
        self.menu = self.home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{shortcuts.NAME}.lnk"

    def maker(self, returncode=0, stderr=b"", write=None):
        def run(cmd, env=None, **kw):
            items = json.loads(env["HZ_SHORTCUTS"])
            self.calls.append((cmd, items))
            if returncode == 0 if write is None else write:
                for item in items:
                    Path(item["path"]).write_bytes(b"lnk")
            return SimpleNamespace(returncode=returncode, stdout=b"", stderr=stderr)
        return run


class Creating(Base):
    def test_each_shortcut_is_saved_under_a_plain_name_and_renamed_to_the_bilingual_one(self):
        lines = shortcuts.install(self.engine, links=False, run=self.maker())
        self.assertTrue(self.desktop.is_file() and self.menu.is_file())
        self.assertEqual(len(lines), 2)
        for _, items in self.calls:
            for item in items:
                self.assertTrue(Path(item["path"]).name.isascii(), "Windows' shortcut object cannot save a name with Persian letters")
                self.assertTrue(item["desc"].isascii(), "nor store a Persian description")
        self.assertEqual(sorted(p.name for p in self.desktop.parent.iterdir()), [f"{shortcuts.NAME}.lnk"], "no temporary file is left behind")
        self.assertIn("همزبان", shortcuts.NAME)
        self.assertIn("Hamzaban", shortcuts.NAME)

    def test_what_the_shortcut_starts(self):
        shortcuts.install(self.engine, links=False, run=self.maker())
        item = self.calls[0][1][0]
        self.assertEqual(item["target"], PYW)
        self.assertEqual(item["args"], f'"{self.engine / "hamzaban_app.pyw"}"')
        self.assertEqual(item["cwd"], str(self.engine))
        self.assertEqual(item["icon"], f"{self.engine / 'assets' / 'hamzaban.ico'},0")
        self.assertIn("powershell.exe", self.calls[0][0][0])

    def test_the_desktop_or_the_menu_can_be_left_out(self):
        shortcuts.install(self.engine, desktop=False, links=False, run=self.maker())
        self.assertFalse(self.desktop.exists())
        self.assertTrue(self.menu.is_file())

    def test_installing_again_replaces_the_shortcut(self):
        shortcuts.install(self.engine, links=False, run=self.maker())
        self.desktop.write_bytes(b"old")
        shortcuts.install(self.engine, links=False, run=self.maker())
        self.assertEqual(self.desktop.read_bytes(), b"lnk")
        self.assertEqual(sorted(p.name for p in self.desktop.parent.iterdir()), [f"{shortcuts.NAME}.lnk"])

    def test_a_dry_run_makes_nothing_and_starts_nothing(self):
        lines = shortcuts.install(self.engine, dry_run=True, run=lambda *a, **k: self.fail("a dry run starts nothing"), reg=FakeReg())
        self.assertFalse(self.desktop.exists() or self.menu.exists())
        self.assertTrue(all(ln.startswith("would") for ln in lines), lines)

    def test_a_failure_says_why_and_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError) as ctx:
            shortcuts.install(self.engine, links=False, run=self.maker(returncode=1, stderr=b"Unable to save shortcut", write=True))   # saved the first, failed the second
        self.assertIn("Unable to save shortcut", str(ctx.exception))
        self.assertEqual(list((self.home / "Desktop").glob("*")), [])

    def test_missing_pieces_are_named(self):
        (self.engine / "assets" / "hamzaban.ico").unlink()
        with self.assertRaises(RuntimeError) as ctx:
            shortcuts.install(self.engine, run=self.maker())
        self.assertIn("hamzaban.ico", str(ctx.exception))

    def test_elsewhere_than_windows_nothing_happens_and_it_says_so(self):
        with mock.patch.object(shortcuts, "is_windows", lambda: False):
            for lines in (shortcuts.install(self.engine, run=self.maker()), shortcuts.remove(), shortcuts.status()):
                self.assertTrue(lines[0].lower().count("windows") >= 1, lines)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.desktop.exists())


class TheLink(Base):
    def test_it_is_registered_for_the_current_user_only_and_starts_the_handler(self):
        reg = FakeReg()
        lines = shortcuts.install(self.engine, desktop=False, menu=False, run=self.maker(), reg=reg)
        self.assertIn("registered", lines[-1])
        self.assertEqual(tree(reg), {BASE, BASE + r"\defaulticon", BASE + r"\shell", BASE + r"\shell\open", BASE + r"\shell\open\command"})
        self.assertEqual(reg.keys[("HKCU", BASE)], {"": "URL:Hamzaban", "URL Protocol": ""})
        self.assertEqual(reg.keys[("HKCU", BASE + r"\shell\open\command")][""], f'"{PYW}" "{self.engine / "hamzaban_open.pyw"}" "%1"')
        self.assertTrue(reg.keys[("HKCU", BASE + r"\defaulticon")][""].endswith('hamzaban.ico",0'))
        self.assertTrue(all(root == "HKCU" for root, _ in reg.keys), "never the machine-wide part of the registry: no administrator rights")

    def test_in_the_test_sandbox_the_real_registry_is_left_alone(self):
        lines = shortcuts.install(self.engine, desktop=False, menu=False, run=self.maker())
        self.assertIn("skipped (test sandbox)", lines[-1])
        self.assertIn("left alone", shortcuts.remove()[-1])
        self.assertIn("not checked", shortcuts.status(self.engine)[2])

    def test_status_and_remove_tell_the_truth(self):
        reg = FakeReg()
        shortcuts.install(self.engine, run=self.maker(), reg=reg)
        text = "\n".join(shortcuts.status(self.engine, reg))
        self.assertIn("Desktop shortcut: present", text)
        self.assertIn("Start-menu entry: present", text)
        self.assertIn("registered", text)
        self.assertIn(PYW, text)
        removed = shortcuts.remove(self.engine, reg=reg)
        self.assertFalse(self.desktop.exists() or self.menu.exists())
        self.assertEqual(tree(reg), set(), "the whole tree of keys is gone, the ones in the middle too")
        self.assertTrue(any("removed the hamzaban: link" in ln for ln in removed))
        text = "\n".join(shortcuts.status(self.engine, reg))
        self.assertIn("Desktop shortcut: missing", text)
        self.assertIn("not registered", text)
        self.assertTrue(any("was not registered" in ln for ln in shortcuts.remove(self.engine, reg=reg)))

    def test_unrelated_keys_of_the_user_are_never_touched(self):
        reg = FakeReg({("HKCU", r"Software\Classes\other"): {"": "keep"}, ("HKCU", r"Software\Classes\hamzaban2"): {"": "keep too"}})
        shortcuts.install(self.engine, desktop=False, menu=False, run=self.maker(), reg=reg)
        shortcuts.remove(self.engine, reg=reg)
        self.assertEqual(tree(reg), set())
        self.assertEqual(reg.keys[("HKCU", r"software\classes\other")], {"": "keep"})
        self.assertEqual(reg.keys[("HKCU", r"software\classes\hamzaban2")], {"": "keep too"}, "a look-alike name is not ours")

    def test_a_refused_removal_is_an_error_not_a_quiet_not_registered(self):
        reg = FakeReg()
        shortcuts.install(self.engine, run=self.maker(), reg=reg)
        reg.DeleteKey = mock.Mock(side_effect=PermissionError("access denied"))
        with self.assertRaises(RuntimeError) as ctx:
            shortcuts.remove(self.engine, reg=reg)
        text = str(ctx.exception)
        self.assertIn("could not remove the hamzaban: link", text)
        self.assertIn("access denied", text)
        self.assertNotIn("not registered", text)
        self.assertIn("removed", text, "what was already removed is still reported")


class WhereTheFoldersAre(unittest.TestCase):
    def test_a_moved_desktop_is_found_in_the_registry(self):
        reg = FakeReg({("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"):
                       {"Desktop": r"D:\OneDrive\Desktop", "Programs": r"D:\Menu\Programs"}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUITE_USER_HOME", None)
            found = shortcuts.folders(reg)
        self.assertEqual((found["desktop"], found["menu"]), (Path(r"D:\OneDrive\Desktop"), Path(r"D:\Menu\Programs")))

    def test_pythonw_is_preferred_so_no_console_window_appears(self):
        tmp = Path(tempfile.mkdtemp(prefix="hz-py-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "python.exe").write_bytes(b"")
        with mock.patch.object(shortcuts.sys, "executable", str(tmp / "python.exe")):
            self.assertEqual(shortcuts.pythonw(), str(tmp / "python.exe"), "no windowless twin: the interpreter itself")
            (tmp / "pythonw.exe").write_bytes(b"")
            self.assertEqual(shortcuts.pythonw(), str(tmp / "pythonw.exe"))


class CommandLine(Base):
    def run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = shortcuts.main(list(argv))
        return code, buf.getvalue()

    def test_install_dry_run_status_and_remove(self):
        with mock.patch.object(shortcuts, "ENGINE", self.engine):
            code, out = self.run_main("install", "--dry-run")
            self.assertEqual(code, 0, out)
            self.assertIn("would create", out)
            code, out = self.run_main("status")
            self.assertEqual(code, 0, out)
            self.assertIn("missing", out)
            self.assertEqual(self.run_main("remove")[0], 0)

    def test_an_error_is_printed_and_the_exit_code_says_so(self):
        (self.engine / "hamzaban_app.pyw").unlink()
        with mock.patch.object(shortcuts, "ENGINE", self.engine):
            code, out = self.run_main("install")
        self.assertEqual(code, 1)
        self.assertIn("hamzaban_app.pyw", out)


if __name__ == "__main__":
    unittest.main()
