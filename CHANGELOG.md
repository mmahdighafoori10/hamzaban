# Changelog

## Unreleased

**The application (Windows)**
- Hamzaban has an identity now: an icon (`engine/assets`, drawn by `tools/make_icon.py`), a Start-menu entry and a desktop shortcut named *Hamzaban همزبان*, and its own window (`app_window.py`: Edge or Chrome in app mode with a profile of its own). The installer's new `shortcuts` step creates them (it asks first; `--skip shortcuts`), `python shortcuts.py install|status|remove` does it by hand, and `--uninstall` undoes it. Current user only, no administrator rights.
- A **کجاست؟** ("where is it?") button on every chat, every part of a chat and every *now* entry: the application the chat took place in (the Claude app, Claude Code in a terminal, the Codex app, Codex in a terminal, Chat On Steroids, or the web service of a closing-block chat), the part of the chat (message numbers, time, a search phrase), the chat file and folder, and buttons that open the application, open Codex on that very chat (`codex://threads/<id>`), show the file in Explorer, open the folder or continue in a terminal. Only what is still on the disk gets a button; what is gone is said so, and a Codex log that has been packed into `.jsonl.zst` is found.
- The `hamzaban:` link (registered under `HKEY_CURRENT_USER` only) carries an action and a chat tag and nothing else; the folder, file, application and command are looked up in the chat registry. No server, no network code. See `docs/app.md`.
- Not in this change: shortcuts for macOS and Linux, and a link into an old Claude desktop session or a Chat On Steroids chat (neither application has one).

**Continuity packages**
- `continuity.py` / `hamzaban continuity write`: a portable `project-continuity/` folder for one project or one chat (README, INSTRUCTIONS, CONTEXT, a dated HANDOFF, DECISIONS, TEST-LOG, OPEN-QUESTIONS), filled from the Vault, the chat registry and read-only git and manifest facts, with five evidence labels. What needs a look at the code stays `TODO(agent)`.
- A cut-off chat's handoff says why it stopped and when the usage window opens again. Chats and proposals from other projects are counted, never quoted. A file you edited is never overwritten (the fresh copy lands beside it as `<name>.generated.md`).
- `continuity.py parity` / `hamzaban parity`: do `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` and the other harness files carry the user's rules? States `CURRENT`, `STALE`, `OWN`, `MISSING`, `MIXED`; `--home` checks the three agents' own folders; `--strict` for CI.
- New skill `hamzaban-continuity`: how an agent completes and uses a package safely. Installed by the installer with the others.
- The repository stays separate from anybody's chats and backups: `project-continuity/`, chat logs (`*.jsonl`) and transfer folders are git-ignored, `tests/test_repo_hygiene.py` fails if private-looking data is ever tracked, and the chat ids and tags used in examples and fixtures are made up.
- `pack` and `export` take `--rules-only`: only the user's standing rules, preference notes and memory tools, no project state, so the same text can sit in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md` or `~/.gemini/GEMINI.md` for every project. Its id changes only when the rules do. `parity --home` lists a generated copy next to the user's own file, reads the pack id of the xml dialect too, and compares ids within the project files and within the home files.

## 1.0.0 — first public release

**Shared memory**
- One neutral pack (`pack.json`) rendered in the dialect of Claude Code, Claude.ai, Codex, ChatGPT, Gemini, Cursor, Copilot, three chat-API shapes and plain text; new agents are one JSON file.
- The closing block (`hamzaban-close`) brings web chats in; facts and decisions wait in `30-Pending` until approved.
- `native_memory.py` proposes the agents' own memories (Claude Code notes, Codex memories) to the shared notebook.
- A read-only MCP server (`infinity-memory`) for Claude Code, Codex and Claude Desktop.

**Chats**
- Stable tags for every chat (`CC-7K3QF`) and sub-tags for topics (`CC-7K3QF/T3`), read from Claude Code, Codex and Chat On Steroids logs.
- Cut-off detection: usage limits (with the time the tokens return), refused requests, stopped turns, unfinished tool calls and unanswered messages; transient errors that the agent recovers from are not reported.
- Resume text by tag or topic (`memory_handoff`, `hamzaban-continue`).

**Skills and control room**
- Skills kept in step between agents without ever deleting or overwriting; respects Chat On Steroids' limits.
- An offline control room (chats, "now", skills matrix, memory path).

**Installer**
- `install.py` / `INSTALL.cmd`: dry run, per-step consent, backups before every change outside the Vault, `--uninstall`, `--doctor`. Windows task, launchd and cron backends.

**Not in this release**
- No network sync, no web chats tracking, no English control-room text (the page is Persian, RTL). macOS and Linux are experimental.
