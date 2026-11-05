# Hamzaban · همزبان

**One shared memory for all your AI agents. Local, plain text, yours.**

Claude Code, Codex, ChatGPT, Gemini and the rest each remember in their own way, and none of them knows what the others did.
Hamzaban keeps your rules, decisions, facts and the state of your projects in one folder of Markdown files on your own computer and
hands them to every agent in the dialect that agent likes. It also gives every chat a stable **tag**, so a chat that was cut off by a
usage limit or a closed window is never lost: open any agent and say *"continue from CX-7K3QF"*.

There is no server, no account and no network code. Python standard library only. Everything stays in a folder you can read, edit and delete.

[فارسی](README.fa.md) · [How it works](docs/how-it-works.md) · [Agents](docs/agents.md) · [Continuity](docs/continuity.md) · [Privacy](docs/privacy.md) · [Troubleshooting](docs/troubleshooting.md)

---

## What it does

| | |
|---|---|
| **A shared notebook in a common language** | Your standing rules, approved facts, decisions and project state live as Markdown in your *Vault* (an Obsidian-compatible folder; Obsidian itself is optional). One neutral pack is rendered for each agent: `claude-code.md`, `codex.md`, `gemini.md`, `chatgpt.md`, `cursor`, `copilot`, three chat-API shapes, and any agent you add with a ten-line JSON file. |
| **Tags for every chat and topic** | `CC-7K3QF` is a chat, `CC-7K3QF/T3` is the third topic inside it. Tags are read from the agents' own logs (Claude Code, Codex, Chat On Steroids), so they exist even for chats you never "saved". |
| **Cut-off detection** | It reads how a chat ended. A Codex turn that stopped with *"You've hit your usage limit … try again at 2:32 AM"* shows as **cut off**, with the time the tokens come back. A chat that only hit a hiccup and carried on does not. |
| **Continue anywhere** | `memory_handoff` (MCP) or the `hamzaban-continue` skill turns a tag into a resume text: why it stopped, the topics, the last request and answer, the files it changed. |
| **A portable backup of any project or chat** | `python .ai-memory/hamzaban/cli.py continuity write --dir <project>` writes a `project-continuity/` folder (README, your rules word for word, context, a dated handoff, decisions, a test log, open questions) with evidence labels (`CONFIRMED`, `INFERRED`, `UNKNOWN` ...), so another agent or another computer can pick the work up cold. The `hamzaban-continuity` skill has an agent finish the parts that need a look at the code. `parity` checks that `CLAUDE.md`, `AGENTS.md`, `GEMINI.md` and the rest all carry your rules. See [docs/continuity.md](docs/continuity.md). |
| **The agents' own memories, translated** | Notes Claude Code keeps for itself and the memories Codex extracts are proposed to your notebook (`30-Pending`). Nothing becomes a fact until you approve it. |
| **Shared skills** | A skill you install in one agent is copied to the others. It never deletes and never overwrites a skill that differs; a conflict becomes a note for you. |
| **A control room** | One offline HTML page: active chats, cut-off chats with a *copy "continue" command* button, the skills matrix, and how the memory flows. |

Honest limits: it moves **text**, not a model's hidden state, tools or logins. A resume text is extracted from a log and is not verified, so the
receiving agent should check the real files. Topic splitting is heuristic. Web chats (claude.ai, ChatGPT, Gemini) have no local log; they
join through a *closing block* you copy once. The control room and the tags are written in Persian (RTL); the docs are English and Persian.

## Install

You need **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/); on Windows tick *Add python.exe to PATH*).

**Windows** (the main target)

1. Download this project (green **Code** button → *Download ZIP*) and unzip it, or `git clone` it.
2. Double-click **`INSTALL.cmd`**. It asks where your Vault should live and then asks before each change outside it.

**macOS / Linux** (experimental: written to be portable and covered by the same tests, but not yet field-tested the way Windows is)

```bash
sh install.sh
```

Useful options (the same on every system):

```text
python install.py --dry-run              show everything it would do, change nothing
python install.py --vault ~/Memory       choose the folder
python install.py --yes                  say yes to every step
python install.py --skip hooks,schedule  leave steps out (skills, mcp, hooks, schedule, open)
python install.py --doctor               health check of an installed copy
python install.py --uninstall            remove the background task, hooks and MCP entries; keeps all notes
```

What the installer does, in order. The first three stay inside the Vault; every later step asks first (or use `--yes`):

1. folders and a short Welcome note in the Vault; 2. the engine into `<Vault>/.ai-memory` (an older copy is backed up first);
3. a starter rules file `00-Context/Harness.md` if you have none; 4. **skills**: `hamzaban-continue`, `hamzaban-continuity`, `hamzaban-close`, `infinite-context`;
5. **mcp**: the memory server `infinity-memory` in Claude Code, Codex and Claude Desktop (their config files are backed up first);
6. **hooks**: Claude Code hooks that record chats and hand over context (backup first);
7. **schedule**: a background task once a minute, no admin rights (Windows Task Scheduler; launchd or cron elsewhere);
8. a first run, then the control room opens.

Every change outside the Vault is backed up first and logged to `<Vault>/.ai-memory/logs/install.log`.

## Using it

- **Start a new chat** in Claude Code or Codex and say *"continue from my last chat"*, or *"continue from CX-7K3QF"* (a tag from the control room).
- **Open the control room**: `00-Dashboard/control-room.html` in your Vault (on Windows: `CONTROL-ROOM.cmd` in `.ai-memory`). Each chat card has a *copy "continue" command* button.
- **Review proposals**: `python .ai-memory/pending.py list`, then `approve <id>` or `reject <id>`. Facts, decisions, the agents' own memories and closing blocks all wait here.
- **Make a work or a chat transferable**: ask an agent *"make a continuity package for this project"*, or run `python .ai-memory/hamzaban/cli.py continuity write --dir <project>`. Check that your agents' instruction files agree with `python .ai-memory/hamzaban/cli.py parity --dir <project> --home`.
- **Close a web chat**: ask the agent *"wrap up for memory"*; it writes one `hamzaban-close` block. Copy it and run `HAMZABAN-CLOSE.cmd` (Windows, reads the clipboard) or `python .ai-memory/hamzaban/cli.py ingest <file>`.
- **Your rules**: edit `00-Context/Harness.md`. Every agent that reads a pack gets them as *your* instructions; everything else it reads is labelled reference data.

## Privacy in one paragraph

It reads the local logs of the agents you use, redacts common secret patterns, and stores short excerpts (your messages clipped to 1,500 characters,
replies to 900) as Markdown and SQLite inside your Vault. It never opens a network connection (no networking module is imported anywhere
in the engine, and the control room page loads nothing remote). Redaction is pattern-based and cannot catch everything: do not paste secrets into chats and
keep the Vault out of cloud sync folders you do not trust. Details: [docs/privacy.md](docs/privacy.md).

## Project layout

```text
install.py · INSTALL.cmd · install.sh   the installer (and UNINSTALL.cmd)
engine/                                 what gets copied to <Vault>/.ai-memory (plain Python, standard library only)
  hamzaban/                             packs, dialect adapters, the closing block, SPEC.md (the format)
  tests/                                the engine's tests (not installed)
skills/                                 the skills the installer adds
tests/                                  the installer's tests
docs/                                   how it works, per-agent guide, privacy, troubleshooting, development
```

## Contributing

Bug reports, adapters for more agents, translations of the control room and macOS/Linux fixes are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
Security issues: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Hamzaban is an independent project and is not affiliated with Anthropic, OpenAI, Google, Obsidian or Chat On Steroids; product names are used
only to say which tools it works with. The hub-and-spoke idea (one neutral format, one thin adapter per agent) was checked against the MIT-licensed
OmniRoute project; only the idea was used, no code was copied.
