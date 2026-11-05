# Developing

Python 3.10+, standard library only. There is nothing to build.

```text
install.py                 the installer (stdlib; copies engine/ into <Vault>/.ai-memory)
engine/                    the code that runs inside a Vault
  hamzaban/                packs, adapters, render, closing block, outbox, SPEC.md; must stay free of subprocess/socket/urllib/ctypes (a test enforces it)
  tests/                   the engine's tests; fixtures in tests/fixtures
skills/                    skills the installer adds ({{VAULT}} and {{ENGINE}} are filled in)
tests/                     the installer's tests
```

## Run the tests

```text
python -X utf8 -B -m unittest discover -s engine/tests     # the engine (about 330 tests, 1-2 minutes)
python -X utf8 -B -m unittest discover -s tests            # the installer (15 tests)
```

Every test works in temporary folders. `SUITE_NOW` fakes the clock; `SUITE_USER_HOME`, `SUITE_APPDATA`, `CONNECT_HOME` and `HAMZABAN_HOME` relocate "the user's home" so
a test never touches the real machine. A test must never read your real Vault or agent folders.

## Rules the code follows

- **Read-only on agents.** Logs, transcripts, memory folders and SQLite files of other tools are only read (SQLite from a temporary copy).
- **Back up before changing anything outside the Vault**, never delete user files, write atomically (temp file + `os.replace`).
- **Redact first.** Everything that is stored passes `redact.scrub_all`; the tests plant fake secrets and look for them.
- **No network.** Do not import `urllib`, `socket`, `http`, `ssl`, `requests`, `ctypes`.
- **Derived data can be rebuilt.** The chat registry is rebuilt when `LOGIC_VERSION` in `chats.py` changes; bump it whenever the way logs are read or classified changes.
- **The agents' log formats are not documented promises.** A reader must tolerate unknown rows and missing fields, and a change in a format needs a test that fails before the fix.
- **Persian and UTF-8 everywhere**: ZWNJ, RTL text, Arabic yeh/kaf variants. The control room escapes all chat text and isolates Latin words inside RTL text.

## Adding an agent

*A dialect only:* one JSON file in `.ai-memory/adapters.d/` (format in `engine/hamzaban/SPEC.md`). *A chat source:* a reader class in `engine/chat_sources.py` that turns one
log into the neutral events documented at the top of that file, plus fixtures in `engine/tests/test_chats.py` that follow the real format.

## Releasing

1. Update `VERSION` in `engine/hamzaban/__init__.py` and `CHANGELOG.md`.
2. Run both test suites on Windows, and let CI run them on Linux and macOS.
3. Run `python install.py --vault <empty folder> --yes --skip schedule --no-open` against a fake home (see `tests/test_install.py`) and open the control room.
4. Tag the commit `vX.Y.Z` and attach the ZIP GitHub builds from the tag.

## CI

`.github/workflows/ci.yml` runs both suites on Windows (required) and on Ubuntu and macOS (experimental: allowed to fail until they are green) with Python 3.10 and 3.13.
