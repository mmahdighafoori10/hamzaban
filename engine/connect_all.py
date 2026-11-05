"""Connect every agent to the shared memory in one run. Run via CONNECT-ALL.cmd (Windows) or `python connect_all.py`.

What it does (each step backs up the file it changes and never deletes anything of yours):
  1. Claude Code  : adds MCP server "infinity-memory" to ~/.claude.json (user scope).
  2. Codex        : adds [mcp_servers.infinity-memory] to ~/.codex/config.toml (append only).
  3. Claude Desktop: adds "infinity-memory" to claude_desktop_config.json in every Claude data folder that exists:
                    Windows %APPDATA%/Claude (classic installer) and the Microsoft Store / MSIX copy under
                    %LOCALAPPDATA%/Packages/Claude_*/LocalCache/Roaming/Claude; macOS ~/Library/Application Support/Claude;
                    Linux ~/.config/Claude.
  4. Chat On Steroids (Windows only): writes a READ-ONLY diagnostic (file names, sizes, times - no contents)
                    to logs/cos-diagnostic.json so capture problems can be found.
Everything is logged to logs/connect-all.log. Existing entries with the same name are left untouched.

  python connect_all.py              add the entries
  python connect_all.py --dry-run    say what would happen; change nothing
  python connect_all.py --remove     take only the "infinity-memory" entries out again (backup first)

Env overrides for tests: CONNECT_HOME, CONNECT_APPDATA, CONNECT_LOCALAPPDATA.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("CONNECT_HOME") or Path.home())
APPDATA = Path(os.environ.get("CONNECT_APPDATA") or os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
LOCALAPPDATA = Path(os.environ.get("CONNECT_LOCALAPPDATA") or os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
LOG = ENGINE / "logs" / "connect-all.log"
STAMP = datetime.now().strftime("%Y%m%dT%H%M%S")
MCP_NAME = "infinity-memory"
SECRET_WORDS = ("key", "token", "secret", "auth", "cred", "password", "cookie", "session-key", "wallet")
DRY = False   # set by main(): describe, do not write


def log(msg: str) -> None:
    if not DRY:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")
    try:
        print(msg)
    except Exception:
        pass


def python_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.is_file():
            return str(cand)
    return str(exe)


def server_spec() -> dict:
    return {"command": python_exe(), "args": [str(ENGINE / "infinity_mcp.py")]}


def backup(path: Path) -> None:
    if path.is_file():
        dst = path.with_name(path.name + ".bak-" + STAMP)
        shutil.copy2(path, dst)
        log(f"  backup: {dst}")


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp-connect")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _load_servers(path: Path, label: str, create: bool):
    """(data, mcp, error). `mcp` is the mcpServers object (created in `data` when absent); error is a ready FAIL/SKIP message."""
    if not path.is_file():
        if not create:
            return None, None, f"{label}: SKIP ({path} not found - is the app installed?)"
        return {}, {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except ValueError as exc:
        return None, None, f"{label}: FAIL ({path.name} is not valid JSON: {exc}); nothing changed"
    if not isinstance(data, dict):
        return None, None, f"{label}: FAIL ({path.name} is not a JSON object); nothing changed"
    mcp = data.get("mcpServers", {})
    if not isinstance(mcp, dict):
        return None, None, f"{label}: FAIL (mcpServers is not an object); nothing changed"
    return data, mcp, None


def merge_json_servers(path: Path, servers: dict, label: str, create: bool) -> str:
    data, mcp, err = _load_servers(path, label, create)
    if err:
        return err
    added = [n for n in servers if n not in mcp]
    kept = [n for n in servers if n in mcp]
    if not added:
        return f"{label}: OK (already present: {', '.join(kept)})"
    if DRY:
        return f"{label}: WOULD ADD {', '.join(added)} -> {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    mcp = data.setdefault("mcpServers", {})
    for n in added:
        mcp[n] = servers[n]
    write_json_atomic(path, data)
    return f"{label}: ADDED {', '.join(added)}" + (f" (kept existing: {', '.join(kept)})" if kept else "") + f" -> {path}"


def remove_json_servers(path: Path, names: list[str], label: str) -> str:
    data, mcp, err = _load_servers(path, label, create=False)
    if err:
        return err
    gone = [n for n in names if n in mcp]
    if not gone:
        return f"{label}: OK (nothing to remove)"
    if DRY:
        return f"{label}: WOULD REMOVE {', '.join(gone)} -> {path}"
    backup(path)
    for n in gone:
        del data["mcpServers"][n]
    write_json_atomic(path, data)
    return f"{label}: REMOVED {', '.join(gone)} -> {path}"


def toml_str(value: str) -> str:
    if "'" not in value and "\n" not in value:
        return "'" + value + "'"
    return json.dumps(value)


def _codex_header_lines(text: str) -> tuple[list[str], int | None]:
    lines = text.splitlines()
    wanted = (f"[mcp_servers.{MCP_NAME}]", f'[mcp_servers."{MCP_NAME}"]')
    return lines, next((i for i, ln in enumerate(lines) if ln.strip() in wanted), None)


def codex() -> str:
    path = HOME / ".codex" / "config.toml"
    if not path.parent.is_dir():
        return "Codex: SKIP (~/.codex not found)"
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    header = f"[mcp_servers.{MCP_NAME}]"
    if _codex_header_lines(text)[1] is not None:
        return "Codex: OK (already present)"
    if DRY:
        return f"Codex: WOULD ADD {MCP_NAME} -> {path}"
    spec = server_spec()
    block = (f"\n# added by Memory Suite connect_all.py {STAMP}\n{header}\n"
             f"command = {toml_str(spec['command'])}\n"
             f"args = [{', '.join(toml_str(a) for a in spec['args'])}]\n")
    backup(path)
    tmp = path.with_name(path.name + ".tmp-connect")
    tmp.write_text(text.rstrip("\n") + "\n" + block if text else block.lstrip("\n"), encoding="utf-8")
    os.replace(tmp, path)
    return f"Codex: ADDED {MCP_NAME} -> {path}"


def codex_remove() -> str:
    path = HOME / ".codex" / "config.toml"
    if not path.is_file():
        return "Codex: OK (nothing to remove)"
    text = path.read_text(encoding="utf-8-sig")
    lines, i = _codex_header_lines(text)
    if i is None:
        return "Codex: OK (nothing to remove)"
    if DRY:
        return f"Codex: WOULD REMOVE {MCP_NAME} -> {path}"
    j = i + 1
    while j < len(lines) and not lines[j].lstrip().startswith("["):
        j += 1
    start = i - 1 if i > 0 and lines[i - 1].startswith("# added by Memory Suite connect_all.py") else i
    if start > 0 and not lines[start - 1].strip():
        start -= 1                                   # the blank line the add step put in front
    kept = lines[:start] + lines[j:]
    backup(path)
    tmp = path.with_name(path.name + ".tmp-connect")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    os.replace(tmp, path)
    return f"Codex: REMOVED {MCP_NAME} -> {path}"


def cos_diagnostic() -> str:
    root = APPDATA / "chat-on-steroids"
    out = ENGINE / "logs" / "cos-diagnostic.json"
    if not root.is_dir():
        return f"Chat On Steroids: SKIP ({root} not found)"
    if DRY:
        return "Chat On Steroids: WOULD WRITE a read-only diagnostic (names, sizes, times only)"
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in ("skills", "node_modules", "Cache", "Code Cache", "GPUCache")
                       and len(rel_dir.parts) < 4]
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"path": str(p.relative_to(root)).replace("\\", "/"), "bytes": st.st_size,
                          "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "secret_like_name": any(w in fn.lower() for w in SECRET_WORDS)})
    files.sort(key=lambda f: f["modified"], reverse=True)
    si = root / "state" / "session-input.json"
    summary = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "root": str(root),
               "session_input_exists": si.is_file(), "file_count": len(files), "newest_60": files[:60]}
    if si.is_file():
        st = si.stat()
        summary["session_input"] = {"bytes": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        try:
            data = json.loads(si.read_text(encoding="utf-8-sig"))
            summary["session_input"]["shape"] = type(data).__name__
            if isinstance(data, dict):
                summary["session_input"]["top_keys"] = sorted(map(str, data))[:20]
                summary["session_input"]["entries"] = len(data)
            elif isinstance(data, list):
                summary["session_input"]["entries"] = len(data)
        except ValueError as exc:
            summary["session_input"]["parse_error"] = str(exc)[:200]
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"Chat On Steroids: diagnostic written ({len(files)} files listed, names/sizes/times only) -> {out}"


def desktop_folders() -> list[Path]:
    """Claude Desktop data folders that exist: the classic one (any OS) and any Microsoft Store (MSIX) copy."""
    found: list[Path] = []
    custom = bool(os.environ.get("CONNECT_APPDATA"))
    if sys.platform == "darwin" and not custom:
        classic = HOME / "Library" / "Application Support" / "Claude"
    elif sys.platform.startswith("linux") and not custom:
        classic = HOME / ".config" / "Claude"
    else:
        classic = APPDATA / "Claude"
    if classic.is_dir() and not classic.is_symlink():
        found.append(classic)
    packages = LOCALAPPDATA / "Packages"
    try:
        for pkg in sorted(packages.glob("Claude_*")) if packages.is_dir() else []:
            d = pkg / "LocalCache" / "Roaming" / "Claude"
            if d.is_dir() and not d.is_symlink():
                found.append(d)
    except OSError:
        pass
    return found


def claude_desktop() -> str:
    folders = desktop_folders()
    if not folders:
        return ("Claude Desktop: SKIP (no Claude data folder found; open Claude Desktop once, then run this again)")
    msgs = [merge_json_servers(d / "claude_desktop_config.json", {MCP_NAME: server_spec()}, "Claude Desktop", create=True)
            for d in folders]
    return msgs[0] if len(msgs) == 1 else "Claude Desktop: " + " | ".join(m.split(": ", 1)[1] for m in msgs)


def claude_desktop_remove() -> str:
    folders = desktop_folders()
    if not folders:
        return "Claude Desktop: SKIP (no Claude data folder found)"
    msgs = [remove_json_servers(d / "claude_desktop_config.json", [MCP_NAME], "Claude Desktop") for d in folders]
    return msgs[0] if len(msgs) == 1 else "Claude Desktop: " + " | ".join(m.split(": ", 1)[1] for m in msgs)


def main(argv=None) -> int:
    global DRY
    ap = argparse.ArgumentParser(description="Connect Claude Code, Codex and Claude Desktop to the shared memory")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen; change nothing")
    ap.add_argument("--remove", action="store_true", help=f'take the "{MCP_NAME}" entries out again (backup first)')
    args = ap.parse_args(argv)
    DRY = args.dry_run
    log(f"=== connect_all {STAMP}{' remove' if args.remove else ''}{' (dry run)' if DRY else ''} (python {python_exe()}) ===")
    if not (ENGINE / "infinity_mcp.py").is_file():
        log("FAIL: infinity_mcp.py not found next to this script")
        return 1
    if args.remove:
        steps = [
            ("Claude Code", lambda: remove_json_servers(HOME / ".claude.json", [MCP_NAME], "Claude Code")),
            ("Codex", codex_remove),
            ("Claude Desktop", claude_desktop_remove),
        ]
    else:
        steps = [
            ("Claude Code", lambda: merge_json_servers(HOME / ".claude.json", {MCP_NAME: {"type": "stdio", **server_spec()}},
                                                       "Claude Code", create=False)),
            ("Codex", codex),
            ("Claude Desktop", claude_desktop),
            ("Chat On Steroids", cos_diagnostic),
        ]
    bad = 0
    for label, fn in steps:
        try:
            msg = fn()
        except Exception as exc:  # one failing step never stops the others
            msg = f"{label}: FAIL ({type(exc).__name__}: {str(exc)[:200]}); nothing changed by this step"
        bad += msg.split(": ", 1)[1].startswith("FAIL")
        log(msg)
    log("done" + (f" with {bad} failure(s)" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
