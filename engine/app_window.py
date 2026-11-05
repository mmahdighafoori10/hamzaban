#!/usr/bin/env python3
"""Opens the control room as its own application window: no tabs, no address bar, its own icon. Python 3.10, standard library only.

Started by the Start-menu and desktop shortcuts (through hamzaban_app.pyw, so no console window appears) or by hand:  python app_window.py

It refreshes the page first when it is missing or old, then opens it in Microsoft Edge (or Chrome) in "app" mode with a profile of its own, so
closing it never touches the person's browser and the window is not one of its tabs. When neither browser is installed the page opens
in the default browser. Nothing here opens a network connection: the page is a local file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

VAULT = Path(os.environ.get("INFINITE_CTX_VAULT") or ENGINE.parent)
PAGE_MAX_AGE = 600           # seconds: an older page is rebuilt before it is shown (a running autosync rebuilds it every minute anyway)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def page_path() -> Path:
    return VAULT / "00-Dashboard" / "control-room.html"


def refresh_page(max_age: int = PAGE_MAX_AGE) -> bool:
    """Build the page when it is missing or older than max_age seconds. True when a page exists afterwards."""
    page = page_path()
    try:
        import time
        if page.is_file() and time.time() - page.stat().st_mtime < max_age:
            return True
    except OSError:
        pass
    try:
        import control_room
        import dashboard
        dashboard.build()
        control_room.write(dashboard.DASH_DIR, dashboard.LAST)
    except Exception:
        pass                                           # an old page is better than none
    return page.is_file()


def browser() -> str | None:
    """Edge first (every Windows 10/11 has it), then Chrome. HAMZABAN_BROWSER names one explicitly."""
    forced = os.environ.get("HAMZABAN_BROWSER")
    if forced and Path(forced).is_file():
        return forced
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    names = (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe"))
    for parts in names:
        for root in roots:
            if root and (Path(root).joinpath(*parts)).is_file():
                return str(Path(root).joinpath(*parts))
    for exe in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def profile_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "Hamzaban" / "app-profile"


def command(exe: str, page: Path, profile: Path) -> list[str]:
    return [exe, f"--app={page.resolve().as_uri()}", f"--user-data-dir={profile}", "--window-size=1280,880", "--no-first-run",
            "--no-default-browser-check"]


def open_window(page: Path, popen=subprocess.Popen, find=browser, web=webbrowser.open) -> list[str]:
    """Open the page as an application window. Returns the command that was started (empty when the default browser had to be used)."""
    exe = find()
    if not exe:
        web(page.resolve().as_uri())
        return []
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    cmd = command(exe, page, profile)
    popen(cmd, creationflags=DETACHED, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cmd


def main() -> int:
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:                  # pythonw has no console
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
    if not refresh_page():
        import app_links
        app_links.notify("صفحهٔ اتاق کنترل ساخته نشد. در پوشهٔ .ai-memory دستور «python control_room.py» را اجرا کن و پیام خطا را ببین.")
        return 1
    open_window(page_path())
    return 0


if __name__ == "__main__":
    sys.exit(main())
