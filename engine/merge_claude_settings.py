"""Safely register the Infinite Context hooks in Claude Code's settings.json.

Run on the Windows machine (so sys.executable is the right Python):
  python merge_claude_settings.py            # dry run: shows exactly what would change
  python merge_claude_settings.py --apply    # writes a timestamped backup first, then merges

Existing user hooks are preserved. Older Infinite Context entries (topic_router / infinite_context /
claude_hook) are replaced by the single dispatcher, so context is never injected twice.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVENTS = {"UserPromptSubmit": None, "SessionStart": "startup|resume|compact|clear",
          "PreCompact": None, "SessionEnd": None, "Stop": None}
OURS = ("topic_router", "infinite_context", "claude_hook")


def is_ours(hook: dict) -> bool:
    """An Infinite Context hook, whether its script is named in `command` or (command + args form) in `args`."""
    args = hook.get("args")
    parts = [str(hook.get("command", ""))] + ([str(a) for a in args] if isinstance(args, list) else [])
    return any(k in " ".join(parts) for k in OURS)


def command() -> str:
    py = Path(sys.executable).as_posix()
    script = (HERE / "claude_hook.py").as_posix()
    return f'"{py}" "{script}"'


def merge(settings: dict) -> tuple[dict, list[str]]:
    notes = []
    hooks = settings.setdefault("hooks", {})
    for event, matcher in EVENTS.items():
        groups = hooks.get(event, [])
        kept = []
        for group in groups:
            inner = [h for h in group.get("hooks", []) if not is_ours(h)]
            removed = len(group.get("hooks", [])) - len(inner)
            if removed:
                notes.append(f"{event}: replaced {removed} older Infinite Context hook(s)")
            if inner:
                group = dict(group, hooks=inner)
                kept.append(group)
        entry = {"hooks": [{"type": "command", "command": command(), "timeout": 20}]}
        if matcher:
            entry["matcher"] = matcher
        kept.append(entry)
        hooks[event] = kept
        notes.append(f"{event}: dispatcher registered" + (f" (matcher {matcher})" if matcher else ""))
    return settings, notes


def unmerge(settings: dict) -> tuple[dict, list[str]]:
    """The opposite of merge(): take only the Infinite Context / Hamzaban hooks out and leave every other hook as it was."""
    notes = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings, ["no hooks section: nothing to remove"]
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept, removed = [], 0
        for group in groups:
            inner = [h for h in group.get("hooks", []) if not is_ours(h)]
            removed += len(group.get("hooks", [])) - len(inner)
            if inner:
                kept.append(dict(group, hooks=inner))
        if removed:
            notes.append(f"{event}: removed {removed} hook(s)")
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    return settings, notes or ["none of our hooks were registered: nothing to remove"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove", action="store_true", help="take our hooks out again instead of registering them")
    args = parser.parse_args()
    path = args.settings
    current = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    merged, notes = (unmerge if args.remove else merge)(json.loads(json.dumps(current)))
    print("\n".join(notes))
    if not args.apply:
        print("\nDry run only. Re-run with --apply to write (a backup is created first).")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
        shutil.copy2(path, backup)
        print("Backup:", backup)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Written:", path)


if __name__ == "__main__":
    main()
