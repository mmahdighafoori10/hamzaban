#!/usr/bin/env python3
"""Gives Hamzaban a place on this computer: a Start-menu entry and a desktop shortcut with its own icon, and the `hamzaban:` link that the
control room uses to open applications and show files. For the current user only; no administrator rights. Python 3.10, standard library only.

    python shortcuts.py install [--no-desktop] [--no-menu] [--no-links] [--dry-run]
    python shortcuts.py remove
    python shortcuts.py status

Windows only for now: elsewhere every command says so and changes nothing. The shortcuts start hamzaban_app.pyw with pythonw (no console
window); the link is registered under HKEY_CURRENT_USER\\Software\\Classes\\hamzaban and starts hamzaban_open.pyw. `remove` takes all of it away again.
Under a test sandbox (SUITE_USER_HOME is set) the shortcuts go into that fake home and the registry is not touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

NAME = "Hamzaban همزبان"          # both spellings in one name, so typing either one into the Start menu finds it
DESCRIPTION = "Hamzaban: one shared memory for all your AI agents"          # plain letters: the shortcut object stores a description in the ANSI code page
PROTOCOL = "hamzaban"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LNK_SCRIPT = ("$ErrorActionPreference='Stop';$sh=New-Object -ComObject WScript.Shell;"
              "foreach($i in (ConvertFrom-Json $env:HZ_SHORTCUTS)){$s=$sh.CreateShortcut($i.path);$s.TargetPath=$i.target;$s.Arguments=$i.args;"
              "$s.WorkingDirectory=$i.cwd;$s.IconLocation=$i.icon;$s.Description=$i.desc;$s.WindowStyle=7;$s.Save()}")


def is_windows() -> bool:
    return os.name == "nt"


def sandboxed() -> bool:
    return bool(os.environ.get("SUITE_USER_HOME"))


def folders(reg=None) -> dict[str, Path]:
    """Where this user's Desktop and Start-menu Programs folder really are (they can be moved, for example into OneDrive)."""
    if sandboxed():
        home = Path(os.environ["SUITE_USER_HOME"])
        return {"desktop": home / "Desktop", "menu": home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"}
    home = Path.home()
    found = {"desktop": home / "Desktop", "menu": Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / "Microsoft" / "Windows" / "Start Menu" / "Programs"}
    try:
        if reg is None:
            import winreg as reg
        with reg.OpenKey(reg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
            for name, slot in (("Desktop", "desktop"), ("Programs", "menu")):
                try:
                    value = reg.QueryValueEx(key, name)[0]
                    if value:
                        found[slot] = Path(value)
                except OSError:
                    pass
    except (ImportError, OSError):
        pass
    return found


def pythonw() -> str:
    exe = Path(sys.executable)
    twin = exe.with_name("pythonw.exe")
    return str(twin if exe.name.lower() == "python.exe" and twin.is_file() else exe)


def specs(engine: Path, where: dict[str, Path], desktop: bool = True, menu: bool = True) -> list[dict]:
    base = {"target": pythonw(), "args": f'"{engine / "hamzaban_app.pyw"}"', "cwd": str(engine),
            "icon": f"{engine / 'assets' / 'hamzaban.ico'},0", "desc": DESCRIPTION}
    out = []
    if desktop:
        out.append(dict(base, path=str(where["desktop"] / f"{NAME}.lnk")))
    if menu:
        out.append(dict(base, path=str(where["menu"] / f"{NAME}.lnk")))
    return out


def create_shortcuts(items: list[dict], run=subprocess.run) -> None:
    """Windows' shortcut object cannot save a file whose name has Persian letters, so each shortcut is saved under a plain name first and
    then renamed (a rename has no such limit)."""
    work = []
    for n, item in enumerate(items):
        final = Path(item["path"])
        final.parent.mkdir(parents=True, exist_ok=True)
        work.append(dict(item, path=str(final.with_name(f"hamzaban-new-{n}.lnk"))))
    env = dict(os.environ, HZ_SHORTCUTS=json.dumps(work, ensure_ascii=True))
    try:
        r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", LNK_SCRIPT], env=env,
                capture_output=True, timeout=60, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("could not create the shortcuts: " + (r.stderr or b"").decode("utf-8", "replace").strip()[:200])
        for item, made in zip(items, work):
            os.replace(made["path"], item["path"])
    finally:
        for made in work:
            Path(made["path"]).unlink(missing_ok=True)


def _reg(reg):
    if reg is not None:
        return reg
    import winreg
    return winreg


def register_links(engine: Path, reg=None) -> str:
    reg = _reg(reg)
    command = f'"{pythonw()}" "{engine / "hamzaban_open.pyw"}" "%1"'
    base = rf"Software\Classes\{PROTOCOL}"
    for sub, value, extra in (("", "URL:Hamzaban", ("URL Protocol", "")), ("\\DefaultIcon", f'"{engine / "assets" / "hamzaban.ico"}",0', None),
                              ("\\shell\\open\\command", command, None)):
        with reg.CreateKey(reg.HKEY_CURRENT_USER, base + sub) as key:
            reg.SetValueEx(key, "", 0, reg.REG_SZ, value)
            if extra:
                reg.SetValueEx(key, extra[0], 0, reg.REG_SZ, extra[1])
    return command


def _delete_tree(reg, root, path: str) -> bool:
    """True when the key was there and is gone now, False when it was never there; any other failure (access denied, say) is raised."""
    try:
        with reg.OpenKey(root, path, 0, reg.KEY_READ | reg.KEY_WRITE) as key:
            names = []
            i = 0
            while True:
                try:
                    names.append(reg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except FileNotFoundError:
        return False
    for child in names:
        _delete_tree(reg, root, path + "\\" + child)
    reg.DeleteKey(root, path)
    return True


def unregister_links(reg=None) -> bool:
    reg = _reg(reg)
    return _delete_tree(reg, reg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}")


def registered_command(reg=None) -> str | None:
    reg = _reg(reg)
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, rf"Software\Classes\{PROTOCOL}\shell\open\command") as key:
            return reg.QueryValueEx(key, "")[0]
    except OSError:
        return None


def install(engine: Path | None = None, desktop: bool = True, menu: bool = True, links: bool = True, dry_run: bool = False,
            run=subprocess.run, reg=None) -> list[str]:
    engine = engine or ENGINE
    if not is_windows():
        return ["Shortcuts and links are only made on Windows for now; nothing was changed."]
    missing = [p.name for p in (engine / "hamzaban_app.pyw", engine / "hamzaban_open.pyw", engine / "assets" / "hamzaban.ico") if not p.is_file()]
    if missing:
        raise RuntimeError("missing from the engine folder: " + ", ".join(missing))
    where = folders(reg)
    items = specs(engine, where, desktop, menu)
    lines = [("would create " if dry_run else "created ") + i["path"] for i in items]
    if items and not dry_run:
        create_shortcuts(items, run)
    if links:
        if sandboxed() and reg is None:
            lines.append("hamzaban: links: skipped (test sandbox)")
        elif dry_run:
            lines.append(f"would register the {PROTOCOL}: link for this user (HKEY_CURRENT_USER\\Software\\Classes\\{PROTOCOL})")
        else:
            register_links(engine, reg)
            lines.append(f"registered the {PROTOCOL}: link for this user")
    if not dry_run and not sandboxed():
        try:
            import app_links
            lines.append(f"found {len(app_links.start_apps(refresh=True, run=run))} applications in the Start menu (for the open-the-app buttons)")
        except Exception:
            pass
    return lines


def remove(engine: Path | None = None, run=subprocess.run, reg=None) -> list[str]:
    if not is_windows():
        return ["Nothing to remove: shortcuts are only made on Windows."]
    lines = []
    where = folders(reg)
    for key in ("desktop", "menu"):
        p = where[key] / f"{NAME}.lnk"
        if p.is_file():
            p.unlink()
            lines.append(f"removed {p}")
    if sandboxed() and reg is None:
        lines.append("hamzaban: links: left alone (test sandbox)")
    else:
        try:
            lines.append(f"removed the {PROTOCOL}: link" if unregister_links(reg) else f"the {PROTOCOL}: link was not registered")
        except OSError as exc:
            raise RuntimeError("; ".join(lines + [f"could not remove the {PROTOCOL}: link: {exc}"])) from exc
    return lines


def status(engine: Path | None = None, reg=None) -> list[str]:
    engine = engine or ENGINE
    if not is_windows():
        return ["Shortcuts are only made on Windows for now."]
    where = folders(reg)
    lines = []
    for key, label in (("desktop", "Desktop shortcut"), ("menu", "Start-menu entry")):
        p = where[key] / f"{NAME}.lnk"
        lines.append(f"{label}: {'present' if p.is_file() else 'missing'}  {p}")
    if sandboxed() and reg is None:
        lines.append(f"{PROTOCOL}: link: not checked (test sandbox)")
    else:
        command = registered_command(reg)
        lines.append(f"{PROTOCOL}: link: " + (f"registered  {command}" if command else "not registered"))
    lines.append(f"icon: {'present' if (engine / 'assets' / 'hamzaban.ico').is_file() else 'missing'}")
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="shortcuts.py", description="Start-menu entry, desktop shortcut and hamzaban: link for the control room")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="create the shortcuts and register the link")
    p.add_argument("--no-desktop", action="store_true")
    p.add_argument("--no-menu", action="store_true")
    p.add_argument("--no-links", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("remove", help="remove the shortcuts and the link")
    sub.add_parser("status", help="what is installed")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "install":
            out = install(desktop=not args.no_desktop, menu=not args.no_menu, links=not args.no_links, dry_run=args.dry_run)
        elif args.cmd == "remove":
            out = remove()
        else:
            out = status()
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}")
        return 1
    for line in out:
        print(line)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
