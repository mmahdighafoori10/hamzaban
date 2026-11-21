# Developing

The engine uses Python 3.10+ and its standard library. The macOS application is built with Swift and Apple frameworks on a Mac.

```text
install.py                 the installer (stdlib; copies engine/ into <Vault>/.ai-memory)
engine/                    the code that runs inside a Vault
  hamzaban/                packs, adapters, render, closing block, outbox, SPEC.md; must stay free of subprocess/socket/urllib/ctypes (a test enforces it)
  tests/                   the engine's tests; fixtures in tests/fixtures
skills/                    skills the installer adds ({{VAULT}} and {{ENGINE}} are filled in)
tests/                     the installer's tests
tools/                     make_icon.py draws the icon (SVG, PNG, ICO) from a few constants
macos/                     native app, bundle metadata, and DMG build script
```

## Run the tests

```text
python -X utf8 -B -m unittest discover -s engine/tests     # the engine (about 420 tests, 2-3 minutes)
python -X utf8 -B -m unittest discover -s tests            # the installer, the icon and the repository hygiene (about 25 tests, half a minute)
```

Every test works in temporary folders. `SUITE_NOW` fakes the clock; `SUITE_USER_HOME`, `SUITE_APPDATA`, `CONNECT_HOME` and `HAMZABAN_HOME` relocate "the user's home" so
a test never touches the real machine. A test must never read your real Vault or agent folders.

## Rules the code follows

- **Read-only on agents.** Logs, transcripts, memory folders and SQLite files of other tools are only read (SQLite from a temporary copy).
- **Back up before changing anything outside the Vault**, never delete user files, write atomically (temp file + `os.replace`).
- **Redact first.** Everything that is stored passes `redact.scrub_all`; the tests plant fake secrets and look for them.
- **No network.** Do not import `urllib`, `socket`, `http`, `ssl`, `requests`, `ctypes`. (`ctypes` appears in two root modules only, for a Windows call each: `auto_capture.py`, and `app_links.notify`, which shows a message box when a click on a link fails. The `hamzaban/` package may not use it at all.)
- **A `hamzaban:` link is an action and a tag.** `app_links.py` accepts nothing else. Never add a parameter that carries a path, a command or an address; look the value up in the chat registry instead.
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
4. Let the macOS DMG workflow build and verify the app on a Mac runner.
5. Merge the version change into main. The workflow creates tag `vX.Y.Z` and publishes `Hamzaban-X.Y.Z-macOS.dmg` in GitHub Releases when that version has no release yet. The DMG is ad-hoc signed; notarization requires a Developer ID and a separate signing setup.

## CI

`.github/workflows/ci.yml` runs both suites on Windows (required) and on Ubuntu and macOS (experimental: allowed to fail until they are green) with Python 3.10 and 3.13.

`.github/workflows/macos-dmg.yml` builds on macOS for pull requests, pushes to main, manual runs, and version tags. It verifies the bundle signature and disk image, uploads the DMG as a workflow artifact, and publishes a new version in GitHub Releases when it first reaches main.
