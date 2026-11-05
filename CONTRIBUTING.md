# Contributing

Thank you for helping. Hamzaban is small on purpose: Python standard library only, no network, plain files you can read.

## Before you start

- **Bugs and ideas**: open an issue first for anything bigger than a typo. Say which agent, which OS and what `python install.py --doctor` printed.
- **No real data**: never paste a real chat, a real Vault note, a path with your name, a key or a token into an issue, a test or a fixture. Tests use fake users
  (`C:\Users\demo`) and clearly fake secrets (built by concatenation, like `"AKIA" + "ABCDEFGHIJKLMNOP"`).
- **Your own backups stay yours**: Vaults, chat transcripts and `project-continuity/` folders are git-ignored here, and `tests/test_repo_hygiene.py` fails if one is ever
  tracked. If you make a continuity package while working on this project, keep it out of commits and out of issues. Chat tags and ids in examples are made up.
- **Scope**: new *dialects* (adapters), new *chat readers*, fixes for macOS/Linux, translations of the control room and better docs are very welcome.
  A feature that needs a network, a server or a dependency is not a fit.

## Working on the code

```text
python -X utf8 -B -m unittest discover -s engine/tests
python -X utf8 -B -m unittest discover -s tests
```

Read [docs/developing.md](docs/developing.md) first: it lists the rules the code follows (read-only on agents, backups before changes, redact before storing,
no network, derived data is rebuildable, tolerant readers).

For a bug fix, add a test that fails before the fix and passes after. For a change to how a log is read or classified, bump `LOGIC_VERSION` in `engine/chats.py`.
For anything that writes outside the Vault, show the backup in the test.

## Pull requests

- one change per pull request, with the tests;
- keep the style of the surrounding code (comments explain *why*, names are plain, no clever one-liners);
- update `README.md`/`docs/` and `CHANGELOG.md` when behaviour changes;
- Windows must stay green. Linux and macOS are experimental: a fix there is welcome even if it is all you can test.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
