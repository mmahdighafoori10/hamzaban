# Continuity packages and one language for every harness

Two things in one page, because they answer one worry: *"if I switch from Claude to Codex to Gemini, or my tokens run out in the middle of a job,
can the next agent pick it up, and will it follow the same rules?"*

1. A **continuity package** is a folder of seven Markdown files that describes one project or one chat well enough for any agent, on any
   machine, to continue from it cold.
2. **`parity`** checks that the instruction files of your agents (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md` ...) all carry the same rules.

Both are text. They do not move a model's hidden state, tools or logins.

## Make a package

From your engine folder (`<Vault>/.ai-memory`), or through `python hamzaban/cli.py continuity ...`:

```text
python continuity.py write --dir "C:\work\my-app"                  # a project folder
python continuity.py write --chat CX-7K3QF --dir "C:\work\my-app"   # a chat that was cut off, with its project
python continuity.py write --topic my-app                           # only what the memory knows
python continuity.py write --dir "C:\work\my-app" --dry-run         # show what would be written, write nothing
```

The result is `my-app/project-continuity/` (or, with no `--dir`, `<Vault>/50-Handoff/continuity/<name>/project-continuity/`):

| File | What it is for |
|---|---|
| `README.md` | what this is, when it was made, in what order to read the rest, a privacy note |
| `INSTRUCTIONS.md` | **your standing rules, word for word** (the same text for every agent), the project's own rule files, the commands found in its manifests |
| `CONTEXT.md` | the project note, the opening of its README, approved facts, the latest checkpoint, the chat and its topics, git and manifest facts |
| `HANDOFF-YYYY-MM-DD.md` | the dated, standalone status report: what is done, in progress and left (prioritised), how to resume, and a starter prompt that names this exact file |
| `DECISIONS.md` | decisions with a status and a source |
| `TEST-LOG.md` | what was run and what was not |
| `OPEN-QUESTIONS.md` | what is still unknown and who can settle it |

For a chat that stopped at a usage limit, the handoff says so and when the tokens return, for example *"cut at 2026-11-05 09:26 UTC; the usage
window opens again at 2026-11-05 12:44 UTC"*.

### Labels

Every statement is marked with one of five labels, the same everywhere:

| Label | Meaning |
|---|---|
| `CONFIRMED` | seen directly: a file, a log line, a git fact |
| `INFERRED` | follows from what was seen but is not stated |
| `UNKNOWN` | could not be determined, or was not looked at |
| `NOT TESTED` | exists, but nothing was run to check it |
| `NEEDS VERIFICATION` | someone else's notes (an agent's checkpoint, a chat log), not checked against the files |

### What the generator does not do

It reads the Vault, the chat registry, git state (read-only; credentials in remote URLs are stripped) and the *names* of manifests and test
folders, plus the opening of the README. **It does not read or judge your source code.** Whatever only an inspection can answer is left as
`TODO(agent)`. The `hamzaban-continuity` skill tells an agent how to fill those in safely: read-only, no installs, no secrets, only safe
checks, every claim labelled. Ask any agent *"make a continuity package for this project"* and it runs the generator and then does the inspection.

Only what belongs to the subject goes in. Chats and waiting proposals that belong to other projects are counted but never quoted. A chat
whose working folder is not the project folder gives a warning, so you notice when two subjects were mixed up by mistake.

### Refreshing it, and editing it

Run the same command again. Files nobody touched are rewritten; a file that you or an agent edited is **kept**, and the fresh version is written
next to it as `<name>.generated.md` so you can merge what changed. A new day produces a new `HANDOFF-<date>.md`; the old ones stay as history.

## Privacy

The folder holds your personal rules, notes taken from your chats and local file paths. Secrets are redacted by pattern (which is not perfect),
but treat the folder as private: keep it on your machine or in a private repository. If it is inside a git repository and not ignored, the tool
says so (`WARNING ... add project-continuity/ to .gitignore`). It writes nowhere but the output folder, refuses a symlinked folder, and never
opens a network connection.

## One language for every harness

Hamzaban keeps your rules in one place (`00-Context/Harness.md`) and renders them in each agent's own file name. `parity` checks what is actually
on disk:

```text
python continuity.py parity --dir "C:\work\my-app"          # the project's agent files
python continuity.py parity --home                          # ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md, ~/.gemini/GEMINI.md
python continuity.py parity --dir "C:\work\my-app" --strict # exit code 1 if something generated is stale or mixed
```

| State | Meaning |
|---|---|
| `CURRENT` | generated by Hamzaban and every standing rule of your Harness is in it |
| `STALE` | generated, but a rule has been added or changed since |
| `OWN` | written by you or another tool; shows how many rules appear word for word (information, not a verdict) |
| `MISSING` | no such file |
| `MIXED` | generated files come from different snapshots (different `_pack` ids) |

To bring a project's file in line: `python hamzaban/cli.py export --to <agent> --dir <project> --topic <name>`. It never overwrites: next to an
existing file it writes a `.hamzaban` sibling for you to compare.

**For your own agent folders use `--rules-only`.** A plain export carries one project's state (checkpoint, tasks, facts, last turns). Frozen into
`~/.claude/CLAUDE.md`, that state would be read by every later session of every project. `--rules-only` writes only your standing rules, your
preference notes and the memory tools: the same text for every project, and its id changes only when your rules do.

```text
python hamzaban/cli.py export --to claude-code --dir ~/.claude --rules-only    # creates ~/.claude/CLAUDE.md
python hamzaban/cli.py export --to codex --dir ~/.codex --rules-only           # ~/.codex/AGENTS.hamzaban.md, next to your own AGENTS.md
python hamzaban/cli.py export --to gemini --dir ~/.gemini --rules-only         # only if you use the Gemini CLI
python hamzaban/cli.py pack --to chatgpt --rules-only                          # text to paste into ChatGPT's custom instructions
```

ChatGPT, Claude.ai and the Gemini apps keep their instructions in a web page that cannot be read from disk: paste the output of
`pack --to chatgpt` (or `claude-ai`, `gemini`) `--rules-only` there. A file you wrote yourself (an existing `~/.codex/AGENTS.md`) is never
changed: the generated copy sits beside it for you to merge. `parity --home` then prints `ONE LANGUAGE (home)` when every generated file comes
from the same snapshot, and each file turns `STALE` when your rules change. Export never overwrites, so to refresh a generated file delete it
first (it carries a `hamzaban-export` mark on its first line) and export again. Writing into `~/.claude`, `~/.codex` or `~/.gemini` changes
how every future session behaves, so do it on purpose.

## Honest limits

- A package is a snapshot of text. It is only as good as the Vault, the chat log and the agent that completed it; check it against the files.
- Web chats (claude.ai, ChatGPT, Gemini) have no local log. They join through a closing block (`hamzaban-close`).
- `parity` compares text. It cannot prove that a given model obeys what it reads.
- Output folders that are symlinks are refused; a Windows *junction* is not recognised as one by the Python versions before 3.12.
