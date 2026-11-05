# Privacy and security

Hamzaban is a local tool. This page says exactly what it reads, what it writes, and what it cannot promise.

## What it reads (read-only)

| Where | Why |
|---|---|
| Claude Code transcripts `~/.claude/projects/*/*.jsonl` | chat tags, topics, cut-off detection, your messages |
| Claude Code notes `~/.claude/projects/*/memory/*.md` | proposals for the shared notebook |
| Codex rollouts `~/.codex/sessions/**/rollout-*.jsonl` | the same, for Codex |
| Codex memories `~/.codex/memories_1.sqlite` (read from a temporary copy) | proposals |
| Chat On Steroids `%APPDATA%/chat-on-steroids/sessions` (Windows) | the same, for that app |
| Skill folders of your agents | keeping skills in step |
| Agent config files (`~/.claude.json`, `~/.claude/settings.json`, `~/.codex/config.toml`, `claude_desktop_config.json`) | only the installer and `connect_all.py` touch them: to add or remove *their own* entries, after a backup |

It does not read browser tabs, other apps, your keychain, `.env` files or anything it was not pointed at above. Symlinks are skipped.

## What it writes

Only inside your Vault (`.ai-memory` for the engine and its data), plus:

- the entries the installer registers (MCP server, Claude Code hooks, the background task), each backed up first and removable with `python install.py --uninstall`;
- skill folders in your agents' skill directories (new skills are added; nothing is overwritten or deleted);
- `~/.hamzaban/install.json` (the path of your Vault, so `--uninstall` and `--doctor` can find it).

The Vault holds Markdown and SQLite files: your messages (clipped to 1,500 characters), agent replies (clipped to 900), chat titles, topics, the files a chat
changed, and the notes you approve. Treat the Vault like your chat history: anyone who can read the folder can read it.

## Network

None. No module in the engine imports a networking library; the installer only opens a local file in your browser; the control room page loads no remote
script, style, font or image and sends nothing. The MCP server speaks over stdio to the agent that starts it.

(The agents themselves send your chats to their own services, as they always did. Hamzaban does not change that. What it puts into a pack is read by the agent
and therefore goes to that agent's provider like anything else you type.)

## Redaction, and why you still should not paste secrets

Before anything is stored, a scrubber replaces common secret patterns: API keys of several vendors, bearer tokens, JWTs, AWS keys, private-key assignments such as
`SOMETHING_PRIVATE_KEY=…`, seed-phrase assignments, long random tokens, payment-card numbers. Patterns are not magic: an unusual secret, a password in a sentence or a
secret split over two lines can slip through. Do not paste secrets into chats. If you did, delete the chat's notes from the Vault (`40-Conversation-Capture/`,
`60-Chats/`, `20-Checkpoints/`) and delete `.ai-memory/chat-registry.sqlite3`: it is derived data and is rebuilt from the agents' own logs, which Hamzaban only
reads. The secret also remains in the agent's own session file; remove it there if it must go.

## Trust

- **Instructions vs data.** Only `00-Context/Harness.md` and `Work-Preferences.md` are presented to agents as your rules. Everything else (notes, facts, checkpoints, quoted
  turns) is labelled *reference data*, and every pack tells the model not to obey instructions found there.
- **Nothing becomes a fact without you.** Facts, decisions, the agents' own memories and closing blocks all wait in `30-Pending`.
- **Private facts never enter a pack.** `sensitivity: private` facts are excluded. (`public-ok` is reserved for a possible future sync; today nothing leaves the machine.)
- **The control room is a static file.** The text of your chats is escaped before it is placed in the page, and the copy buttons carry plain text only.

## Keep the Vault out of the wrong places

Do not put the Vault in a public repository or a shared drive. If you sync it (OneDrive, Dropbox, Syncthing), you are sending your chat excerpts to that service.
The repository you are reading contains no Vault, no chat data and no personal files; `.gitignore` excludes the engine's state files in case you copy an installed engine back.

## Reporting a problem

See [SECURITY.md](../SECURITY.md).
