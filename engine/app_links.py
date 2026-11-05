#!/usr/bin/env python3
"""Where a chat lives and how to get there. Python 3.10, standard library only; nothing here opens a network connection.

A chat is written by an application (Claude, Codex, Chat On Steroids ...). For every chat this module answers three questions:

  * which application is it?  From the agent and the "entry" that the agent's own log names (claude-desktop, vscode, cli ...).
  * can that application open this very chat?  Codex can: its documented link `codex://threads/<thread id>` opens a local chat. Claude's desktop
    app has no link for an old Code session (only `claude://code/new`), so the application is opened and the page shows the title and the part
    of the chat to look for.
  * where is it on disk?  The project folder and the agent's log file, which can be shown in the file manager.

where() gives the control room everything for its "where is it?" button. The buttons that need this computer (open an application, show a
file, resume in a terminal) are `hamzaban:` links that shortcuts.py registers for the current user; handle() answers them. A link never carries a
path or a command: only an action and a chat tag (or an agent name). Everything else is looked up in the chat registry, so a hostile web page
that fires such a link can at most open an application or a folder the user already has.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

VAULT = Path(os.environ.get("INFINITE_CTX_VAULT") or ENGINE.parent)
CACHE = ENGINE / "app-launchers.json"
CACHE_SECONDS = 12 * 3600
SCHEME = "hamzaban"
ACTIONS = ("app", "reveal", "folder", "resume")
TAG_RE = re.compile(r"[A-Za-z]{2}-[0-9A-Za-z]{5,11}")
ID_RE = re.compile(r"[0-9A-Za-z_-]{6,90}")
LINK_RE = re.compile(r"hamzaban:(?://)?([a-z]{3,10})/?\?([A-Za-z0-9=&_.-]{1,120})", re.I)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The applications a chat can live in. "names": how the app is called in the Start menu; "web": where to go when there is no app;
# "chat_link": the application's own link that opens one chat by its id (None: it has none).
APPS = {
    "claude": {"fa": "برنامهٔ Claude (تب Code)", "short": "برنامهٔ Claude", "names": ("Claude",), "web": "https://claude.ai/", "chat_link": None,
               "note": "برنامهٔ Claude برای باز کردن یک جلسهٔ Code قدیمی لینک مستقیم ندارد. برنامه باز می‌شود؛ در تب Code همین عنوان را پیدا کن "
                       "یا عبارتِ زیر را در جست‌وجوی برنامه بزن."},
    "claude-cli": {"fa": "Claude Code (ترمینال یا افزونهٔ ویرایشگر)", "short": "Claude Code", "names": (), "web": None, "chat_link": None,
                   "note": "این چت در ترمینال یا ویرایشگر اجرا شده؛ با دستور ادامه در همان پوشه به آن برمی‌گردی."},
    "codex": {"fa": "برنامهٔ Codex", "short": "Codex", "names": ("ChatGPT", "Codex"), "web": None, "chat_link": "codex://threads/{id}", "note": ""},
    "codex-cli": {"fa": "Codex در ترمینال", "short": "Codex", "names": (), "web": None, "chat_link": None,
                  "note": "این چت در ترمینال اجرا شده؛ با دستور ادامه در همان پوشه به آن برمی‌گردی."},
    "cos": {"fa": "Chat On Steroids", "short": "Chat On Steroids", "names": ("Chat On Steroids",), "web": None, "chat_link": None,
            "note": "Chat On Steroids برای باز کردن یک چت قدیمی لینک مستقیم ندارد. برنامه باز می‌شود؛ چت را با عنوان یا عبارتِ زیر پیدا کن."},
    "claude-ai": {"fa": "Claude (چت)", "short": "Claude", "names": ("Claude",), "web": "https://claude.ai/recents", "chat_link": None,
                  "note": "این چت فقط از راه «بلوک پایان چت» ثبت شده و فایل لاگ ندارد؛ برنامه یا سایت باز می‌شود و باید خودت پیدایش کنی."},
    "chatgpt": {"fa": "ChatGPT", "short": "ChatGPT", "names": ("ChatGPT",), "web": "https://chatgpt.com/", "chat_link": None,
                "note": "این چت فقط از راه «بلوک پایان چت» ثبت شده؛ برنامه یا سایت باز می‌شود و باید خودت پیدایش کنی."},
    "gemini": {"fa": "Gemini", "short": "Gemini", "names": ("Gemini",), "web": "https://gemini.google.com/app", "chat_link": None,
               "note": "این چت فقط از راه «بلوک پایان چت» ثبت شده؛ برنامه یا سایت باز می‌شود و باید خودت پیدایش کنی."},
}
AGENT_APP = {"claude-code": "claude", "codex": "codex", "cos": "cos", "claude-ai": "claude-ai", "chatgpt": "chatgpt", "gemini": "gemini"}


class LinkError(Exception):
    """A link that cannot be followed; the text is meant to be shown to the person."""


def app_key(agent: str, entry: str = "") -> str:
    """claude-code written by the desktop app is 'claude'; written by a terminal or an editor it is 'claude-cli'; likewise for Codex."""
    e = (entry or "").lower()
    if agent == "claude-code":
        return "claude" if "desktop" in e else "claude-cli"
    if agent == "codex":
        return "codex-cli" if e in ("cli", "exec", "mcp") else "codex"
    return {"cos": "cos", "claude-ai": "claude-ai", "chatgpt": "chatgpt", "gemini": "gemini"}.get(agent, "")


def thread_id(agent: str, source_id: str) -> str:
    """The id the application itself uses for the chat. A Codex log is named <thread id>_<id of this part>, and Codex knows the thread."""
    return (source_id or "").split("_")[0] if agent == "codex" else (source_id or "")


def resume_cmd(agent: str, source_id: str) -> str | None:
    """The command that reopens the chat in a terminal, for the agents that have one."""
    tid = thread_id(agent, source_id)
    if not ID_RE.fullmatch(tid):
        return None
    return {"claude-code": f"claude --resume {tid}", "codex": f"codex resume {tid}"}.get(agent)


def search_phrase(text: str, words: int = 7, limit: int = 60) -> str:
    """The first few words of a message: what to type into the application's own search to land on that part of the chat."""
    text = re.sub(r"[`*_#>\\|]+", " ", text or "")
    kept = [w for w in text.split() if re.search(r"[^\W\d_]", w) or len(w) > 1]      # drop stray punctuation and lone digits
    return " ".join(kept[:words])[:limit].rstrip()


# ---------------------------------------------------------------------------------------------------- links
def build_uri(action: str, tag: str | None = None, agent: str | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    if tag:
        if not TAG_RE.fullmatch(tag):
            raise ValueError("not a chat tag")
        return f"{SCHEME}://{action}?tag={tag}"
    if agent and agent in AGENT_APP:
        return f"{SCHEME}://{action}?agent={agent}"
    raise ValueError("a link needs a chat tag or an agent")


def parse_uri(uri: str) -> tuple[str, dict[str, str]]:
    """Strict on purpose: only an action and a tag (or an agent name, for 'app') are accepted, nothing else."""
    m = LINK_RE.fullmatch((uri or "").strip().strip('"'))
    if not m or m.group(1).lower() not in ACTIONS:
        raise LinkError("این لینک را نمی‌شناسم.")
    action, params = m.group(1).lower(), {}
    for part in m.group(2).split("&"):
        key, _, value = part.partition("=")
        if key not in ("tag", "agent") or key in params or not value:
            raise LinkError("این لینک را نمی‌شناسم.")
        params[key] = value
    if ("tag" in params) == ("agent" in params):                  # exactly one of them
        raise LinkError("این لینک را نمی‌شناسم.")
    if "tag" in params:
        if not TAG_RE.fullmatch(params["tag"]):
            raise LinkError("تگ چت درست نیست.")
        params["tag"] = params["tag"].upper()
    elif action != "app" or params["agent"] not in AGENT_APP:
        raise LinkError("این لینک را نمی‌شناسم.")
    return action, params


# ---------------------------------------------------------------------------------------------------- the applications on this computer
def scheme_registered(name: str, reg=None) -> bool:
    """Has an application registered this URL scheme for the current user? (Windows; elsewhere the answer is 'assume yes'.)"""
    if reg is None:
        try:
            import winreg as reg
        except ImportError:
            return True
    try:
        with reg.OpenKey(reg.HKEY_CLASSES_ROOT, name):
            return True
    except OSError:
        return False


def _read_cache() -> dict:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def start_apps(refresh: bool = False, run=subprocess.run) -> dict[str, str]:
    """{name: application id} of the apps in the Start menu (classic and Store apps alike), remembered for half a day."""
    data = _read_cache()
    if not refresh and data.get("apps") and time.time() - float(data.get("at", 0)) < CACHE_SECONDS:
        return data["apps"]
    if os.name != "nt":
        return data.get("apps") or {}
    script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Get-StartApps | ConvertTo-Json -Compress"
    try:
        r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=60, creationflags=NO_WINDOW)
        items = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
        items = [items] if isinstance(items, dict) else items
        apps = {i["Name"]: i["AppID"] for i in items if isinstance(i, dict) and i.get("Name") and i.get("AppID")}
    except Exception:
        apps = {}
    if apps:
        tmp = CACHE.with_name(CACHE.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"at": time.time(), "apps": apps}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, CACHE)
        except OSError:
            pass
    return apps or data.get("apps") or {}


def find_app(key: str, apps: dict[str, str] | None = None) -> tuple[str, str] | None:
    """(name, application id) of the application on this computer, or None."""
    spec = APPS.get(key)
    if not spec or not spec["names"]:
        return None
    apps = start_apps() if apps is None else apps
    lowered = {n.lower(): (n, i) for n, i in apps.items()}
    for wanted in spec["names"]:
        if wanted.lower() in lowered:
            return lowered[wanted.lower()]
    return None


# ---------------------------------------------------------------------------------------------------- what the control room shows
def where(chat: dict, schemes=None, which=shutil.which) -> dict:
    """Everything the 'where is it?' panel needs about one chat, as plain data. `chat` is one item of Registry.snapshot()."""
    agent, tag = chat.get("agent", ""), chat.get("tag", "")
    sid = thread_id(agent, chat.get("source_id", ""))
    key = app_key(agent, chat.get("entry", ""))
    app = APPS.get(key)
    out = {"key": key, "app_fa": app["fa"] if app else (chat.get("agent_fa") or agent), "entry": chat.get("entry", ""), "primary": None,
           "note": app["note"] if app else "", "resume": None, "reveal": bool(chat.get("path")) and not str(chat.get("path")).endswith(".md"),
           "folder": bool(chat.get("cwd")), "reveal_href": "", "folder_href": ""}
    if TAG_RE.fullmatch(tag or ""):
        out["reveal_href"], out["folder_href"] = build_uri("reveal", tag=tag), build_uri("folder", tag=tag)
    if app and app["chat_link"] and ID_RE.fullmatch(sid or ""):
        scheme = app["chat_link"].split(":")[0]
        if (scheme in schemes) if schemes is not None else scheme_registered(scheme):
            out["primary"] = {"href": app["chat_link"].format(id=sid), "label": f"باز کردن همین چت در {app['short']}", "exact": True}
            out["note"] = ""
    if out["primary"] is None and app and (app["names"] or app["web"]) and TAG_RE.fullmatch(tag or ""):
        out["primary"] = {"href": build_uri("app", tag=tag), "label": f"باز کردن {app['short']}", "exact": False}
    cmd = resume_cmd(agent, sid)
    if cmd:
        cli = cmd.split(" ")[0]
        out["resume"] = {"cmd": cmd, "available": bool(which(cli)), "href": build_uri("resume", tag=tag) if TAG_RE.fullmatch(tag or "") else ""}
    return out


# ---------------------------------------------------------------------------------------------------- doing things on this computer
def notify(text: str, title: str = "همزبان") -> None:
    """A message the person sees even though the program has no window (it is started by a link or a shortcut)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        sys.stderr.write(text + "\n")


def reveal_file(path: Path, popen=subprocess.Popen) -> None:
    if sys.platform.startswith("win"):
        if '"' in str(path):                              # no Windows path has one; a value that does is not a path
            raise LinkError("مسیر فایل درست نیست.")
        popen(f'explorer.exe /select,"{path}"')           # one string: Explorer wants the quotes around the path exactly like this
    elif sys.platform == "darwin":
        popen(["open", "-R", str(path)])
    else:
        popen(["xdg-open", str(path.parent)])


def open_folder(path: Path, popen=subprocess.Popen) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))                           # noqa: S606 - a folder that the registry named, opened in the file manager
    else:
        popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def launch_app(key: str, popen=subprocess.Popen, apps: dict[str, str] | None = None, startfile=None) -> str:
    """Open the application (or its website when it has no app on this computer). Returns what was opened."""
    spec = APPS.get(key)
    if not spec:
        raise LinkError("برای این چت برنامه‌ای شناخته نیست.")
    found = find_app(key, apps)
    if found and sys.platform.startswith("win"):
        popen(["explorer.exe", "shell:AppsFolder\\" + found[1]])
        return found[0]
    if found and sys.platform == "darwin":
        popen(["open", "-a", found[0]])
        return found[0]
    if spec["web"]:
        (startfile or (os.startfile if sys.platform.startswith("win") else lambda u: popen(["open" if sys.platform == "darwin" else "xdg-open", u])))(spec["web"])
        return spec["web"]
    raise LinkError(f"{spec['short']} روی این کامپیوتر پیدا نشد.")


def open_terminal(cwd: Path, command: str, popen=subprocess.Popen, which=shutil.which) -> None:
    folder = str(cwd) if cwd.is_dir() else str(Path.home())
    wt = which("wt.exe") or which("wt")
    if wt:
        popen([wt, "-d", folder, "powershell.exe", "-NoExit", "-Command", command])
    elif sys.platform.startswith("win"):
        popen(["cmd.exe", "/k", command], cwd=folder, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    else:
        raise LinkError("ترمینال پیدا نشد.")


def handle(uri: str, registry=None, popen=subprocess.Popen, which=shutil.which, startfile=None, apps: dict[str, str] | None = None) -> str:
    """Do what a hamzaban: link asks and say what was done. Raises LinkError (with a message for the person) when it cannot."""
    action, params = parse_uri(uri)
    if "agent" in params:                                         # app?agent=claude-code
        return "opened " + launch_app(AGENT_APP[params["agent"]], popen, apps, startfile)
    if registry is None:
        import chats
        registry = chats.Registry(VAULT, sources=[])
    try:
        chat = registry.get(params["tag"])
    except FileNotFoundError:
        chat = None
    if not chat:
        raise LinkError(f"چتی با تگ {params['tag']} در حافظه نیست.")
    if action == "app":
        return "opened " + launch_app(app_key(chat["agent"], chat["entry"]), popen, apps, startfile)
    if action == "reveal":
        path = Path(chat["path"] or "")
        if not chat["path"] or not path.exists():
            raise LinkError("فایل این چت دیگر سر جایش نیست.")
        reveal_file(path, popen)
        return f"revealed {path}"
    if action == "folder":
        folder = Path(chat["cwd"] or "")
        if not chat["cwd"] or not folder.is_dir():
            raise LinkError("پوشهٔ کار این چت دیگر سر جایش نیست.")
        open_folder(folder, popen)
        return f"opened {folder}"
    cmd = resume_cmd(chat["agent"], chat["source_id"])
    if not cmd:
        raise LinkError("برای این چت دستور ادامه‌ای وجود ندارد.")
    if not which(cmd.split(" ")[0]):
        raise LinkError(f"دستور {cmd.split(' ')[0]} روی این کامپیوتر نصب نیست؛ دستور را کپی کن و جایی که نصب است اجرا کن.")
    open_terminal(Path(chat["cwd"] or ""), cmd, popen, which)
    return f"resumed in a terminal: {cmd}"


def handler_main(argv: list[str] | None = None) -> int:
    """Entry point of hamzaban_open.pyw, started by Windows with the link as the only argument."""
    argv = sys.argv[1:] if argv is None else argv
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:                          # pythonw has no console
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
    try:
        handle(argv[0] if argv else "")
        return 0
    except LinkError as exc:
        notify(str(exc))
    except Exception as exc:                                       # never a traceback window for a click on a link
        notify(f"کار نکرد: {type(exc).__name__}")
    return 1


if __name__ == "__main__":
    sys.exit(handler_main())
