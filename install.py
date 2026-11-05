#!/usr/bin/env python3
"""Hamzaban installer. Python 3.10+, standard library only. Windows is the main target; macOS and Linux are experimental.

  python install.py                    ask where the Vault (your shared-memory folder) should live, then ask before each
                                       change outside it
  python install.py --vault PATH       use this folder (created when missing; existing notes are never touched)
  python install.py --yes              say yes to every step (still prints and logs everything)
  python install.py --dry-run          print what would happen; change nothing at all
  python install.py --skip mcp,hooks   leave steps out: skills, mcp, hooks, schedule, shortcuts, open
  python install.py --uninstall        remove the scheduled task, the hooks, the MCP entries and the shortcuts this installer added.
                                       Your Vault and all your notes stay exactly as they are.
  python install.py --doctor           health check of an installed copy

What it does, in order (the first three stay inside the Vault; the rest ask first, unless --yes):
  1. folders and a short Welcome note in the Vault; 2. the engine into <Vault>/.ai-memory (an older copy is backed up first);
  3. a starter rules file (00-Context/Harness.md) if you have none; 4. skills: hamzaban-continue, hamzaban-close and
  infinite-context are added to the shared skills folder, and the background sync then shares them with your agents;
  5. mcp: the memory server "infinity-memory" is registered in Claude Code, Codex and Claude Desktop (backup first);
  6. hooks: Claude Code hooks that record chats and hand over context (backup first);
  7. schedule: a background task, once a minute, no admin rights (Windows Task Scheduler / launchd / cron);
  8. shortcuts (Windows): a Start-menu entry and a desktop shortcut with the Hamzaban icon, and the hamzaban: link that lets the
     control room open the application or the file of a chat;
  9. first run, then the control room opens as its own window.
Nothing is uploaded anywhere. Nothing of yours is deleted. Every change outside the Vault is backed up first and logged to
<Vault>/.ai-memory/logs/install.log.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent
ENGINE_SRC = ROOT / "engine"
SKILLS_SRC = ROOT / "skills"
MIN_PY = (3, 10)
STEPS = ("skills", "mcp", "hooks", "schedule", "shortcuts", "open")
VAULT_DIRS = ("00-Context", "00-Dashboard", "10-Facts", "10-Projects", "20-Checkpoints", "30-Pending",
              "40-Conversation-Capture", "50-Handoff", "60-Chats")
SKIP_NAMES = {"__pycache__", "tests", ".pytest_cache"}
CRON_MARK = "# hamzaban-autosync"
LAUNCHD_LABEL = "com.hamzaban.autosync"
TASK_NAME = "MemorySuiteAutoSync"
SETTLE_WAIT = 22     # seconds: the sync leaves files younger than 20 s alone (autosync_settle_seconds)

WELCOME = """---
title: Welcome
type: note
---

# Welcome to your Hamzaban Vault

This folder is the shared memory of your AI agents. Everything in it is plain Markdown that you own, can read and can edit.
Nothing in it leaves this computer.

- `00-Context/Harness.md` - your standing rules for every agent. Edit it; every agent that reads a pack gets these rules.
- `00-Dashboard/control-room.html` - the control room: your chats and their tags, the shared skills, how the memory flows.
- `30-Pending/` - proposals (facts, decisions, the agents' own memories) waiting for your approval.
  Approve: `python .ai-memory/pending.py approve <id>`, reject: `python .ai-memory/pending.py reject <id>`.
- `50-Handoff/latest/` - the same memory written in each agent's own dialect (claude-code.md, codex.md, gemini.md, ...).
- `60-Chats/` - one note per chat, with its tag (for example `CC-7K3QF`). Tell any agent "continue from CC-7K3QF".
- `.ai-memory/` - the engine. Safe to leave alone.

همزبان: این پوشه حافظهٔ مشترک عامل‌های هوش مصنوعی توست؛ همه‌چیز متن ساده و فقط روی همین کامپیوتر است.
"""


# ---------------------------------------------------------------------------------------------------- small helpers
def home() -> Path:
    return Path(os.environ.get("HAMZABAN_HOME") or Path.home())


def is_windows() -> bool:
    return sys.platform == "win32"


def version() -> str:
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', (ENGINE_SRC / "hamzaban" / "__init__.py").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


class Ui:
    """Talks to the person. `yes` answers every question yes; without a terminal and without --yes nothing outside the Vault changes."""

    def __init__(self, yes=False, dry=False, inp=input, out=print, interactive=None):
        self.yes, self.dry, self.inp, self.out = yes, dry, inp, out
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.log_path: Path | None = None

    def say(self, text: str = "") -> None:
        self.out(text)
        self.log(text)

    def log(self, text: str) -> None:
        if self.log_path and not self.dry and text.strip():
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} {text}\n")
            except OSError:
                pass

    def ask(self, question: str, default: bool = True) -> bool:
        if self.yes:
            return True
        if not self.interactive:
            return False
        try:
            ans = self.inp(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        except EOFError:
            return False
        return default if not ans else ans.startswith("y")


# ---------------------------------------------------------------------------------------------------- the machine
def appdata(h: Path) -> Path:
    """%APPDATA% on Windows. With a relocated home (tests) everything is looked up under that home instead."""
    if os.environ.get("CONNECT_APPDATA"):
        return Path(os.environ["CONNECT_APPDATA"])
    if os.environ.get("HAMZABAN_HOME"):
        return h / "AppData" / "Roaming"
    return Path(os.environ.get("APPDATA") or h / "AppData" / "Roaming")


def obsidian_vaults(h: Path) -> list[Path]:
    """Vault folders Obsidian already knows (optional: the memory folder is plain Markdown and works without Obsidian)."""
    if is_windows():
        base = appdata(h) / "obsidian"
    elif sys.platform == "darwin":
        base = h / "Library" / "Application Support" / "obsidian"
    else:
        base = h / ".config" / "obsidian"
    try:
        data = json.loads((base / "obsidian.json").read_text(encoding="utf-8-sig"))
        paths = [Path(v["path"]) for v in (data.get("vaults") or {}).values() if isinstance(v, dict) and v.get("path")]
    except (OSError, ValueError, AttributeError):
        return []
    return [p for p in paths if p.is_dir()]


def detect_agents(h: Path) -> dict[str, bool]:
    roaming = appdata(h)
    desktop = [roaming / "Claude", h / "Library" / "Application Support" / "Claude", h / ".config" / "Claude"]
    return {"Claude Code": (h / ".claude").is_dir(), "Codex": (h / ".codex").is_dir(),
            "Claude Desktop": any(d.is_dir() for d in desktop),
            "Chat On Steroids": is_windows() and (roaming / "chat-on-steroids").is_dir()}


def _is_inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base)
        return True
    except ValueError:
        return False


def check_vault(path: Path, h: Path) -> str | None:
    """Why this folder cannot be the Vault, or None."""
    if path.exists() and not path.is_dir():
        return "that path is a file"
    if path.parent == path or path == h or path == h.resolve():
        return "that is a drive root or your home folder: pick a dedicated folder"
    low = str(path).replace("\\", "/").lower().rstrip("/")
    inside_home = any(_is_inside(path, base) for base in (h, h.resolve(), Path(tempfile.gettempdir()).resolve()))
    for bad in () if inside_home else ("c:/windows", "c:/program files", "c:/program files (x86)", "/usr", "/etc", "/bin", "/sbin", "/var", "/system", "/library"):
        if low == bad or low.startswith(bad + "/"):
            return "that is a system folder"
    try:
        path.resolve().relative_to(ROOT.resolve())
        return "that is inside the installer's own folder"
    except ValueError:
        pass
    return None


def choose_vault(ui: Ui, given: str | None, h: Path) -> Path:
    if given:
        return Path(given).expanduser().resolve()
    fresh = (h / "Documents" / "Hamzaban-Vault")
    options = [fresh] + [p for p in obsidian_vaults(h) if p != fresh]
    if ui.yes or not ui.interactive:
        return fresh.resolve()
    ui.say("Where should your shared memory (the \"Vault\") live?")
    for i, p in enumerate(options, 1):
        ui.say(f"  {i}) {p}" + ("   (new folder)" if i == 1 else "   (an Obsidian vault you already have)"))
    ui.say("  or type another folder path")
    while True:
        ans = ui.inp("> ").strip().strip('"')
        if not ans:
            return fresh.resolve()
        pick = options[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(options) else Path(ans).expanduser()
        problem = check_vault(pick.resolve(), h)
        if problem:
            ui.say(f"  {problem}; try again")
            continue
        return pick.resolve()


# ---------------------------------------------------------------------------------------------------- the steps
def engine_files() -> list[Path]:
    out = []
    for p in sorted(ENGINE_SRC.rglob("*")):
        rel = p.relative_to(ENGINE_SRC)
        if p.is_file() and not any(part in SKIP_NAMES for part in rel.parts) and p.suffix != ".pyc":
            out.append(rel)
    return out


def copy_engine(vault: Path, ui: Ui, stamp: str) -> dict:
    dst = vault / ".ai-memory"
    counts = {"added": 0, "updated": 0, "same": 0}
    for rel in engine_files():
        s, d = ENGINE_SRC / rel, dst / rel
        if d.is_file() and d.read_bytes() == s.read_bytes():
            counts["same"] += 1
            continue
        counts["updated" if d.exists() else "added"] += 1
        if ui.dry:
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.is_file():
            keep = dst / "backups" / f"install-{stamp}" / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(d, keep)
        tmp = d.with_name(d.name + ".tmp-install")
        shutil.copyfile(s, tmp)
        os.replace(tmp, d)
    return counts


def make_vault(vault: Path, ui: Ui) -> None:
    if ui.dry:
        return
    for d in VAULT_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)
    welcome = vault / "00-Context" / "Welcome.md"
    if not welcome.exists():
        welcome.write_text(WELCOME, encoding="utf-8")


def write_config(engine: Path, ui: Ui) -> str:
    p = engine / "suite-skills.json"
    if p.exists():
        return "kept your suite-skills.json"
    targets = {"agents": "{home}/.agents/skills", "codex": "{home}/.codex/skills", "claude": "{home}/.claude/skills"}
    if is_windows():
        targets["cos"] = "{appdata}/chat-on-steroids/skills"
    cfg = {"version": 1, "skills": [], "targets": targets, "groups": [["agents", "codex"]], "protect_existing": True, "converter": None}
    if not ui.dry:
        p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return "wrote suite-skills.json (a skill that differs between agents is never overwritten)"


def agent_env(h: Path) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if os.environ.get("HAMZABAN_HOME"):          # a relocated home (used by the tests): point the engine at it too
        env.update(SUITE_USER_HOME=str(h), CONNECT_HOME=str(h), CONNECT_APPDATA=str(h / "AppData" / "Roaming"),
                   SUITE_APPDATA=str(h / "AppData" / "Roaming"), CONNECT_LOCALAPPDATA=str(h / "AppData" / "Local"))
    return env


def run_engine(engine: Path, h: Path, script: str, *args: str, timeout: int = 300):
    cmd = [sys.executable, "-X", "utf8", str(engine / script), *args]
    r = subprocess.run(cmd, cwd=str(engine), env=agent_env(h), capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def install_skills(engine: Path, vault: Path, h: Path, ui: Ui) -> list[str]:
    """Add our skills to the shared skills folder (never over one that is already there); the sync then shares them."""
    done = []
    dst_root = engine / "skills-src"
    for skill in sorted(p for p in SKILLS_SRC.iterdir() if (p / "SKILL.md").is_file()) if SKILLS_SRC.is_dir() else []:
        target = dst_root / skill.name
        if target.exists():
            done.append(f"{skill.name}: kept yours")
            continue
        done.append(f"{skill.name}: added")
        if ui.dry:
            continue
        shutil.copytree(skill, target, ignore=shutil.ignore_patterns("__pycache__"))
        for f in target.rglob("*.md"):
            f.write_text(f.read_text(encoding="utf-8").replace("{{VAULT}}", str(vault)).replace("{{ENGINE}}", str(engine)), encoding="utf-8")
    if not ui.dry:
        code, out, err = run_engine(engine, h, "hamzaban/cli.py", "install-skill", "--vault-path", str(vault))
        done.append("hamzaban-continue: " + ("added" if "created" in out else "kept yours" if "exists" in out else f"failed ({err.strip()[:80]})"))
        for agent_dir in (h / ".claude", h / ".codex"):   # an installed agent without a skills folder yet gets one, so the sync can fill it
            if agent_dir.is_dir() and not (agent_dir / "skills").exists():
                (agent_dir / "skills").mkdir()
                done.append(f"created {agent_dir / 'skills'}")
    else:
        done.append("hamzaban-continue: added")
    return done


def register_mcp(engine: Path, h: Path, ui: Ui) -> list[str]:
    if ui.dry:
        names = [n for n, found in detect_agents(h).items() if found and n != "Chat On Steroids"]
        return [f"would register the memory server in: {', '.join(names) or 'no agent found'}"]
    code, out, err = run_engine(engine, h, "connect_all.py")
    return [ln for ln in out.splitlines() if re.match(r"^(Claude Code|Codex|Claude Desktop|Chat On Steroids): ", ln)] or [err.strip()[:200] or "no output"]


def register_hooks(engine: Path, h: Path, ui: Ui) -> list[str]:
    settings = h / ".claude" / "settings.json"
    if not (h / ".claude").is_dir():
        return ["Claude Code not found (~/.claude): nothing to register"]
    if ui.dry:
        return [f"would register the Claude Code hooks in {settings} (backup first)"]
    code, out, err = run_engine(engine, h, "merge_claude_settings.py", "--settings", str(settings), "--apply")
    if code != 0:
        return [f"failed: {(err or out).strip()[:200]}"]
    return [ln for ln in out.splitlines() if ln.strip()][:7]


# ---- the background task
def launchd_plist(python: str, engine: Path) -> str:
    args = "".join(f"<string>{xml_escape(a)}</string>" for a in (python, str(engine / "autosync.py"), "once"))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict>\n<key>Label</key><string>{LAUNCHD_LABEL}</string>\n'
            f'<key>ProgramArguments</key><array>{args}</array>\n<key>StartInterval</key><integer>60</integer>\n'
            f'<key>WorkingDirectory</key><string>{xml_escape(str(engine))}</string>\n<key>RunAtLoad</key><true/>\n'
            f'<key>StandardErrorPath</key><string>{xml_escape(str(engine / "logs" / "autosync-launchd.err"))}</string>\n'
            '</dict></plist>\n')


def cron_line(python: str, engine: Path) -> str:
    q = lambda s: '"' + str(s).replace('"', '\\"') + '"'   # noqa: E731
    return f"* * * * * cd {q(engine)} && {q(python)} {q(engine / 'autosync.py')} once >/dev/null 2>&1 {CRON_MARK}"


def cron_edit(current: str, line: str | None) -> str:
    """The crontab text with our line replaced (or removed when `line` is None); every other line is kept as it was."""
    kept = [ln for ln in current.splitlines() if CRON_MARK not in ln]
    if line:
        kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


def schedule(engine: Path, h: Path, ui: Ui, remove: bool = False, run=subprocess.run) -> list[str]:
    verb = "remove" if remove else "register"
    if is_windows():
        ps1 = engine / "manage_autosync.ps1"
        if ui.dry:
            return [f"would {verb} the scheduled task {TASK_NAME} (runs autosync.py once a minute, no admin rights)"]
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Action", "unregister" if remove else "register"]
        r = run(cmd, capture_output=True, timeout=120)
        text = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if r.returncode != 0:
            return [f"failed: {' '.join(lines)[:240]}"]
        return [ln for ln in lines if "Task" in ln or "task" in ln][:3] or ["done"]
    if sys.platform == "darwin":
        plist = h / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if ui.dry:
            return [f"would {verb} the launchd agent {plist}"]
        if remove:
            run(["launchctl", "unload", "-w", str(plist)], capture_output=True)
            plist.unlink(missing_ok=True)
            return [f"removed {plist}"]
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(sys.executable, engine), encoding="utf-8")
        run(["launchctl", "unload", str(plist)], capture_output=True)
        r = run(["launchctl", "load", "-w", str(plist)], capture_output=True)
        return [f"launchd agent {plist} " + ("loaded" if r.returncode == 0 else "written; launchctl load failed")]
    # Linux and the rest: a cron line
    if ui.dry:
        return [f"would {verb} a crontab line (every minute): {cron_line(sys.executable, engine)}"]
    if not shutil.which("crontab"):
        return ["crontab not found: run `python autosync.py once` yourself or from your own scheduler"]
    cur = run(["crontab", "-l"], capture_output=True)
    text = (cur.stdout or b"").decode("utf-8", "replace") if cur.returncode == 0 else ""
    new = cron_edit(text, None if remove else cron_line(sys.executable, engine))
    r = run(["crontab", "-"], input=new.encode("utf-8"), capture_output=True)
    return ["crontab line " + ("removed" if remove else "installed") if r.returncode == 0 else "crontab failed"]


# ---------------------------------------------------------------------------------------------------- record, plan, summary
def record_path(h: Path) -> Path:
    return h / ".hamzaban" / "install.json"


def write_record(h: Path, vault: Path, ui: Ui, done: list[str]) -> None:
    if ui.dry:
        return
    p = record_path(h)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"vault": str(vault), "engine": str(vault / ".ai-memory"), "version": version(),
                             "installed": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "steps": done}, indent=2) + "\n", encoding="utf-8")


def read_record(h: Path) -> dict:
    try:
        return json.loads(record_path(h).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def doctor_summary(engine: Path, h: Path) -> list[str]:
    code, out, err = run_engine(engine, h, "suite.py", "doctor", timeout=180)
    lines = [ln for ln in out.splitlines() if ln.startswith("[") or ln.startswith("Summary")]
    return lines or [(err or "doctor printed nothing").strip()[:200]]


# ---------------------------------------------------------------------------------------------------- main flows
def shortcuts_supported() -> bool:
    return is_windows()


def shortcuts_step(engine: Path, h: Path, ui: Ui, remove: bool = False) -> list[str]:
    """Windows: a Start-menu entry and a desktop shortcut with the Hamzaban icon, and the hamzaban: link the control room's buttons use."""
    if not (engine / "shortcuts.py").is_file():
        return ["shortcuts.py is missing from the engine folder; nothing done"]
    verb = "remove" if remove else "install"
    if ui.dry:
        return [f"would {verb} the Start-menu entry, the desktop shortcut and the hamzaban: link (this user only, no administrator rights)"]
    code, out, err = run_engine(engine, h, "shortcuts.py", verb)
    if code != 0:
        return [f"failed: {(err or out).strip()[:200]}"]
    return [ln.strip() for ln in out.splitlines() if ln.strip()][:6] or ["done"]


def do_install(args, ui: Ui) -> int:
    h = home()
    if sys.version_info < MIN_PY:
        ui.say(f"Python {MIN_PY[0]}.{MIN_PY[1]} or newer is needed (this is {sys.version.split()[0]}). Install it from python.org and run this again.")
        return 1
    if not ENGINE_SRC.is_dir():
        ui.say("The 'engine' folder is missing next to install.py. Download the whole project again.")
        return 1
    skip = set(args.skip)
    ui.say(f"Hamzaban {version()} installer" + ("  (dry run: nothing will be changed)" if ui.dry else ""))
    found = detect_agents(h)
    ui.say("Agents found on this computer: " + (", ".join(n for n, f in found.items() if f) or "none (the memory still works; connect an agent later)"))
    vault = choose_vault(ui, args.vault, h)
    problem = check_vault(vault, h)
    if problem:
        ui.say(f"Cannot use {vault}: {problem}.")
        return 2
    engine = vault / ".ai-memory"
    ui.log_path = engine / "logs" / "install.log"
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    ui.say(f"Vault: {vault}")

    make_vault(vault, ui)
    counts = copy_engine(vault, ui, stamp)
    ui.say(f"Engine: {counts['added']} added, {counts['updated']} updated (old copies backed up), {counts['same']} already current")
    if not ui.dry:
        ui.say("Rules: " + (run_engine(engine, h, "hamzaban/cli.py", "init")[1].strip().splitlines() or ["?"])[0])
    ui.say("Config: " + write_config(engine, ui))

    done: list[str] = []
    questions = {
        "skills": ("Share skills between your agents? (adds hamzaban-continue, hamzaban-close and infinite-context; skills you already have are "
                   "copied to your other agents; nothing is deleted or overwritten)", install_skills),
        "mcp": ("Register the memory server in Claude Code, Codex and Claude Desktop (their config files are backed up first)?", register_mcp),
        "hooks": ("Register Claude Code hooks (record chats, hand over context; settings.json is backed up first)?", register_hooks),
        "schedule": ("Run the background sync once a minute (no admin rights, never deletes anything)?", schedule),
        "shortcuts": ("Put Hamzaban in the Start menu and on the desktop with its own icon, and let the control room open the application or "
                      "the file of a chat (this user only, no admin rights)?", shortcuts_step),
    }
    for step in ("skills", "mcp", "hooks", "schedule", "shortcuts"):
        question, fn = questions[step]
        if step in skip:
            ui.say(f"- {step}: skipped (--skip)")
            continue
        if step == "shortcuts" and not shortcuts_supported():
            ui.say("- shortcuts: skipped (Windows only for now)")
            continue
        if not ui.dry and not ui.ask(question):
            ui.say(f"- {step}: not done. {'Use --yes or answer y to include it.' if not ui.interactive and not ui.yes else 'You said no.'}")
            continue
        ui.say(f"- {step}:")
        try:
            lines = fn(engine, vault, h, ui) if step == "skills" else fn(engine, h, ui)
        except Exception as exc:  # one step failing must not stop the rest
            lines = [f"failed: {type(exc).__name__}: {str(exc)[:160]}"]
        for ln in lines:
            ui.say(f"    {ln}")
        done.append(step)

    if not ui.dry:
        run_engine(engine, h, "infinite_context.py", "index")
        code, out, err = run_engine(engine, h, "autosync.py", "once")
        if code == 0 and "skills" in done:
            # the first pass only takes a snapshot, and files written a moment ago are left alone for 20 seconds (so a half-written
            # skill is never copied); the second pass then shares the skills
            ui.say(f"- waiting {SETTLE_WAIT} s for the new skill files to settle, then sharing them ...")
            time.sleep(SETTLE_WAIT)
            code, out, err = run_engine(engine, h, "autosync.py", "once")
        ui.say("First run: " + ("ok" if code == 0 else f"autosync exited {code}: {(err or out).strip()[:160]}"))
        run_engine(engine, h, "control_room.py")
        for ln in doctor_summary(engine, h):
            ui.say("  " + ln)
        write_record(h, vault, ui, done)
        page = vault / "00-Dashboard" / "control-room.html"
        if "open" not in skip and not args.no_open and page.is_file() and ui.ask("Open the control room now?"):
            if is_windows() and (engine / "app_window.py").is_file():
                subprocess.Popen([sys.executable, str(engine / "app_window.py")], cwd=str(engine))     # its own window, with its own profile
            elif is_windows() and (engine / "CONTROL-ROOM.cmd").is_file():
                subprocess.Popen(["cmd", "/c", str(engine / "CONTROL-ROOM.cmd")], cwd=str(engine))
            else:
                webbrowser.open(page.as_uri())
    ui.say("")
    ui.say("Done. Next: open a NEW chat in your agent and say \"continue from my last chat\" (or from a tag such as CC-7K3QF).")
    ui.say(f"Control room: {vault / '00-Dashboard' / 'control-room.html'}")
    ui.say(f"Undo the integrations any time: python install.py --uninstall   (your notes in {vault} are never touched)")
    return 0


def do_uninstall(args, ui: Ui) -> int:
    h = home()
    rec = read_record(h)
    vault = Path(args.vault).expanduser().resolve() if args.vault else Path(rec["vault"]) if rec.get("vault") else None
    if not vault or not (vault / ".ai-memory").is_dir():
        ui.say("No installed copy found. Pass --vault PATH if it lives somewhere I cannot see.")
        return 1
    engine = vault / ".ai-memory"
    ui.log_path = engine / "logs" / "install.log"
    ui.say(f"Removing the integrations of {vault}" + ("  (dry run)" if ui.dry else ""))
    if not ui.dry and not ui.ask("Remove the background task, the shortcuts, the Claude Code hooks and the memory-server entries?"):
        ui.say("Nothing changed.")
        return 0
    try:
        for ln in schedule(engine, h, ui, remove=True):
            ui.say(f"- schedule: {ln}")
    except Exception as exc:
        ui.say(f"- schedule: failed: {type(exc).__name__}: {str(exc)[:160]}")
    if shortcuts_supported():
        try:
            for ln in shortcuts_step(engine, h, ui, remove=True):
                ui.say(f"- shortcuts: {ln}")
        except Exception as exc:
            ui.say(f"- shortcuts: failed: {type(exc).__name__}: {str(exc)[:160]}")
    settings = h / ".claude" / "settings.json"
    if (engine / "merge_claude_settings.py").is_file() and settings.is_file():
        if ui.dry:
            ui.say(f"- hooks: would take our hooks out of {settings}")
        else:
            code, out, err = run_engine(engine, h, "merge_claude_settings.py", "--settings", str(settings), "--remove", "--apply")
            ui.say("- hooks: " + ("; ".join(ln for ln in out.splitlines() if "removed" in ln or "nothing" in ln) or (err.strip()[:160] or "done")))
    if (engine / "connect_all.py").is_file():
        code, out, err = run_engine(engine, h, "connect_all.py", *(["--dry-run", "--remove"] if ui.dry else ["--remove"]))
        for ln in out.splitlines():
            if re.match(r"^(Claude Code|Codex|Claude Desktop): ", ln):
                ui.say(f"- mcp: {ln}")
    ui.say("Kept: your Vault and every note in it, the engine folder and its data. Delete the folder yourself if you want it gone.")
    return 0


def do_doctor(args, ui: Ui) -> int:
    h = home()
    rec = read_record(h)
    vault = Path(args.vault).expanduser().resolve() if args.vault else Path(rec["vault"]) if rec.get("vault") else None
    if not vault or not (vault / ".ai-memory").is_dir():
        ui.say("No installed copy found. Pass --vault PATH.")
        return 1
    ui.say(f"Vault: {vault}   installed version: {rec.get('version', '?')}   this installer: {version()}")
    for ln in doctor_summary(vault / ".ai-memory", h):
        ui.say(ln)
    return 0


def main(argv=None, *, inp=input, out=print, interactive=None) -> int:
    ap = argparse.ArgumentParser(prog="install.py", description="Install Hamzaban (shared memory for your AI agents)")
    ap.add_argument("--vault", help="the folder that will hold your shared memory (created when missing)")
    ap.add_argument("--yes", "-y", action="store_true", help="say yes to every step")
    ap.add_argument("--dry-run", action="store_true", help="print what would happen; change nothing")
    ap.add_argument("--skip", default="", help="comma-separated steps to leave out: " + ", ".join(STEPS))
    ap.add_argument("--no-open", action="store_true", help="do not open the control room at the end")
    ap.add_argument("--uninstall", action="store_true", help="remove the integrations (background task, hooks, MCP entries); keeps all notes")
    ap.add_argument("--doctor", action="store_true", help="health check of an installed copy")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    args.skip = [s.strip() for s in args.skip.split(",") if s.strip()]
    bad = [s for s in args.skip if s not in STEPS]
    if bad:
        ap.error(f"unknown step(s) in --skip: {', '.join(bad)} (known: {', '.join(STEPS)})")
    if args.version:
        out(f"Hamzaban {version()}")
        return 0
    ui = Ui(yes=args.yes, dry=args.dry_run, inp=inp, out=out, interactive=interactive)
    if args.uninstall:
        return do_uninstall(args, ui)
    if args.doctor:
        return do_doctor(args, ui)
    return do_install(args, ui)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing half-written is left behind: run it again any time.")
        sys.exit(130)
