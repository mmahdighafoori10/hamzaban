"""Adapters: how the neutral pack is spoken in each agent's dialect.

An adapter is DATA, not code: a style (markdown / xml / plain / json-openai / json-anthropic / json-gemini)
plus a file name, a size budget and a few switches. That is why a new agent costs ten lines of JSON and why
nothing has to execute code to add one:

    .ai-memory/adapters.d/my-agent.json
    {"name": "my-agent", "extends": "generic", "title": "My agent", "filename": "my-agent.md", "budget": 9000}

The built-in dialect choices follow each vendor's public convention (CLAUDE.md, AGENTS.md, GEMINI.md,
.cursor/rules, .github/copilot-instructions.md, and the three chat-API message shapes). Conventions change:
treat the file names as a starting point and check them against the agent you actually use.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import handoff

STYLES = ("markdown", "xml", "plain", "json-openai", "json-anthropic", "json-gemini")
SECTIONS = ("rules", "prefs", "skills", "tools", "instructions", "project", "checkpoint", "tasks", "facts", "recent")
SYSTEM_MODES = ("system", "developer", "user")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
PART_RE = re.compile(r"^[A-Za-z0-9._-]{1,60}$")
# names Windows treats as devices (with or without an extension) and files the outbox writes itself
DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
RESERVED_FILES = {"pack.json", "readme.md", "hamzaban-state.json", "hamzaban.json"}
EXTS = {".md", ".json", ".txt", ".mdc"}
MIN_BUDGET = 2500
MAX_BUDGET = 200000
MAX_USER_FILES = 40

_MDC = ("---\ndescription: Hamzaban handoff: the user's rules and project memory carried over from another agent\n"
        "alwaysApply: true\n---\n")

BUILTIN: dict[str, dict] = {
    "claude-code": {"title": "Claude Code", "style": "xml", "filename": "claude-code.md", "export": "CLAUDE.md",
                    "budget": 12000, "aliases": ["claude", "cc"]},
    "claude-ai": {"title": "Claude.ai chat or project", "style": "xml", "filename": "claude-ai.md",
                  "export": "claude-ai-project-instructions.md", "budget": 12000, "aliases": ["claude-web", "claude-desktop"]},
    "codex": {"title": "OpenAI Codex", "style": "markdown", "filename": "codex.md", "export": "AGENTS.md",
              "budget": 12000, "aliases": ["openai-codex"]},
    "chatgpt": {"title": "ChatGPT", "style": "markdown", "filename": "chatgpt.md", "export": "chatgpt-first-message.md",
                "budget": 9000, "aliases": ["gpt", "openai"]},
    "gemini": {"title": "Google Gemini (app, AI Studio, CLI)", "style": "markdown", "filename": "gemini.md",
               "export": "GEMINI.md", "budget": 12000, "aliases": ["google", "gemini-cli", "aistudio"]},
    "cursor": {"title": "Cursor", "style": "markdown", "filename": "cursor.mdc", "export": ".cursor/rules/hamzaban.mdc",
               "budget": 8000, "frontmatter": _MDC, "aliases": []},
    "copilot": {"title": "GitHub Copilot", "style": "markdown", "filename": "copilot.md",
                "export": ".github/copilot-instructions.md", "budget": 6000, "aliases": ["github-copilot"]},
    "generic": {"title": "Any other agent (AGENTS.md style)", "style": "markdown", "filename": "generic.md",
                "export": "AGENTS.md", "budget": 10000, "aliases": ["any", "agents-md"]},
    "plain": {"title": "Plain text (small or local models)", "style": "plain", "filename": "plain.txt",
              "export": "hamzaban.txt", "budget": 6000, "aliases": ["text"]},
    "api-openai": {"title": "OpenAI-compatible chat API (OpenAI, Ollama, LM Studio, OpenRouter ...)", "style": "json-openai",
                   "filename": "api-openai.json", "export": "hamzaban-openai.json", "budget": 20000,
                   "system_mode": "system", "aliases": ["openai-api", "ollama", "lmstudio"]},
    "api-anthropic": {"title": "Anthropic Messages API", "style": "json-anthropic", "filename": "api-anthropic.json",
                      "export": "hamzaban-anthropic.json", "budget": 20000, "aliases": ["anthropic-api"]},
    "api-gemini": {"title": "Gemini generateContent API", "style": "json-gemini", "filename": "api-gemini.json",
                   "export": "hamzaban-gemini.json", "budget": 20000, "aliases": ["gemini-api"]},
}

DEFAULT_TARGETS = ("claude-code", "codex", "gemini", "chatgpt", "generic")


class UnknownTarget(KeyError):
    pass


def _device(part: str) -> bool:
    return part.split(".")[0].strip().upper() in DEVICES


def valid_filename(name) -> bool:
    """A plain, Windows-safe file name with one of the allowed extensions (no streams, devices, dots at the end)."""
    return (isinstance(name, str) and bool(FILE_RE.fullmatch(name)) and ".." not in name and not name.endswith(".")
            and Path(name).suffix.lower() in EXTS and not _device(name))


def _check(raw: dict, known: dict) -> tuple[dict | None, str]:
    """Validate one user adapter; returns (adapter, "") or (None, reason)."""
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None, "name must be 2-31 chars: lowercase letters, digits, '-'"
    base: dict = {}
    parent = raw.get("extends")
    if parent is not None:
        if not isinstance(parent, str) or parent not in known:
            return None, f"extends {parent!r}: unknown adapter"
        base = dict(known[parent])
        base.pop("aliases", None)
    adapter = dict(base)
    adapter["name"] = name
    adapter["user"] = True
    adapter["title"] = str(raw.get("title") or base.get("title") or name)[:80]
    style = raw.get("style", base.get("style", "markdown"))
    if style not in STYLES:
        return None, f"style must be one of {', '.join(STYLES)}"
    adapter["style"] = style
    # a derived adapter gets its own file name: inheriting the parent's would make both write the same file
    filename = raw.get("filename", f"{name}{Path(base.get('filename') or 'x.md').suffix}")
    if not valid_filename(filename) or filename.lower() in RESERVED_FILES:
        return None, "filename must be a plain file name ending in .md .json .txt or .mdc (not pack.json, README.md or a device name)"
    adapter["filename"] = filename
    export = raw.get("export", base.get("export") or filename)
    if not isinstance(export, str) or not _safe_relative(export):
        return None, "export must be a relative path without '..' and without a drive"
    adapter["export"] = export
    budget = raw.get("budget", base.get("budget", 10000))
    if isinstance(budget, bool) or not isinstance(budget, int) or not MIN_BUDGET <= budget <= MAX_BUDGET:
        return None, f"budget must be an integer between {MIN_BUDGET} and {MAX_BUDGET}"
    adapter["budget"] = budget
    mode = raw.get("system_mode", base.get("system_mode", "system"))
    if mode not in SYSTEM_MODES:
        return None, "system_mode must be system, developer or user"
    adapter["system_mode"] = mode
    sections = raw.get("sections", base.get("sections"))
    if sections is not None:
        if not isinstance(sections, list) or not sections or any(s not in SECTIONS for s in sections):
            return None, "sections must be a non-empty list drawn from: " + ", ".join(SECTIONS)
        adapter["sections"] = list(sections)
    adapter["closing"] = bool(raw.get("closing", base.get("closing", True)))
    lang = raw.get("lang", base.get("lang"))
    if lang is not None and lang not in ("en", "fa"):
        return None, "lang must be en or fa"
    adapter["lang"] = lang
    pre = raw.get("preamble", base.get("preamble"))
    if pre is not None:
        if not isinstance(pre, str) or len(pre) > 1500:
            return None, "preamble must be text of at most 1500 characters"
        adapter["preamble"] = handoff.scrub(pre)
    fm = raw.get("frontmatter", base.get("frontmatter"))
    if fm is not None:
        if not isinstance(fm, str) or len(fm) > 600 or not fm.startswith("---\n"):
            return None, "frontmatter must be a short block that starts with '---'"
        adapter["frontmatter"] = fm
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 5 or any(not isinstance(a, str) or not NAME_RE.fullmatch(a) for a in aliases):
        return None, "aliases must be up to 5 short names"
    adapter["aliases"] = list(aliases)
    return adapter, ""


def _safe_relative(path: str) -> bool:
    p = path.replace("\\", "/")
    if not p or p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p) or "\x00" in p or len(p) > 120:
        return False
    parts = p.split("/")
    return (all(part not in ("", ".", "..") and PART_RE.fullmatch(part) and not part.endswith(".") and not _device(part)
                for part in parts) and Path(parts[-1]).suffix.lower() in EXTS)


def load(vault=None) -> tuple[dict[str, dict], list[str]]:
    """(adapters by name, problems). Never raises: a broken user file is reported and skipped."""
    adapters = {k: dict(v, name=k, user=False, system_mode=v.get("system_mode", "system"), closing=True)
                for k, v in BUILTIN.items()}
    problems: list[str] = []
    if vault is None:
        return adapters, problems
    folder = Path(vault) / ".ai-memory" / "adapters.d"
    try:
        if folder.is_symlink():
            return adapters, ["adapters.d is a symlink; ignored"]
        if not folder.is_dir():
            return adapters, problems
        files = sorted(p for p in folder.glob("*.json") if p.is_file() and not p.is_symlink())[:MAX_USER_FILES]
    except OSError:
        return adapters, problems
    for p in files:
        try:
            if p.stat().st_size > 20000:
                problems.append(f"{p.name}: file too large")
                continue
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            problems.append(f"{p.name}: not valid JSON")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{p.name}: must be a JSON object")
            continue
        adapter, why = _check(raw, adapters)
        if adapter is None:
            problems.append(f"{p.name}: {why}")
            continue
        clash = next((o["name"] for o in adapters.values()
                      if o["name"] != adapter["name"] and o["filename"].lower() == adapter["filename"].lower()), None)
        if clash:  # two adapters writing the same file would overwrite each other on every refresh
            problems.append(f"{p.name}: filename {adapter['filename']} is already used by adapter {clash}")
            continue
        adapters[adapter["name"]] = adapter
    return adapters, problems


def resolve(name: str, adapters: dict[str, dict]) -> dict:
    key = (name or "").strip().lower()
    if key in adapters:
        return adapters[key]
    for adapter in adapters.values():
        if key in adapter.get("aliases", []):
            return adapter
    raise UnknownTarget(f"unknown target {name!r}; known: {', '.join(sorted(adapters))}")
