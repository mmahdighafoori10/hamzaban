# Instructions for AI agents

This file is for an AI coding agent (Codex, Claude Code, Cursor, Gemini CLI ...) working in this folder.

## When the person asks you to install Hamzaban

Hamzaban installs into a folder of the person's choice (their *Vault*) and, after asking, connects itself to their agents. Do it with them, not for them:

1. Check Python: `python --version` (Windows) or `python3 --version` must say **3.10 or newer**. If it does not, stop and tell them to install it from python.org (on Windows with *Add python.exe to PATH*).
2. Show them what would happen, changing nothing: `python install.py --dry-run`. Summarise the output in their language.
3. Ask where the Vault should live. The default is `~/Documents/Hamzaban-Vault`; an existing Obsidian vault works too. Ask which steps they want: `skills`, `mcp`, `hooks`, `schedule`, `shortcuts` (Windows), `open`. All are on by default.
4. Only after they say yes, install: `python install.py --vault "<the folder>" --yes`, adding `--skip <steps>` for any step they declined. `--yes` exists because an agent cannot answer the installer's questions; it is not a licence to choose for them.
5. Check the result: `python install.py --doctor` (it never changes anything). Report each `WARN` or `FAIL` with its reason.
6. Tell them what changed: the Vault folder, the background task, and the entries added to their agents' settings (every one of those files was backed up first; the log is `<Vault>/.ai-memory/logs/install.log`). They need a **new** chat in Claude Code or Codex, and a restart of Claude Desktop, for the memory server to appear.

The installer writes outside this folder (the Vault, `~/.claude`, `~/.codex`, the Start menu on Windows), so a sandboxed agent will be asked for permission; that is expected.

To undo: `python install.py --uninstall` (removes the background task, hooks, MCP entries and shortcuts; never touches the notes).

## Rules while you do it

- Never read, print or copy secrets: `.env` files, keys, tokens, passwords. The installer does not need any.
- Never edit the person's agent settings by hand; the installer adds and removes only its own entries, after a backup.
- Never delete anything in the Vault.
- Talk to the person in their language.

## When you change the code

Python 3.10+, standard library only, no network code. Run both suites before you say it works:

```text
python -X utf8 -B -m unittest discover -s engine/tests
python -X utf8 -B -m unittest discover -s tests
```

More: [docs/developing.md](docs/developing.md).
