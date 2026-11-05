## What and why

<!-- One change per pull request. -->

## Checklist

- [ ] Tests added or changed (a bug fix has a test that fails without the fix)
- [ ] `python -X utf8 -B -m unittest discover -s engine/tests` and `... -s tests` pass
- [ ] No real chat, note, key, token or personal path in code, tests, fixtures or docs
- [ ] Still no network and no dependency (standard library only)
- [ ] Anything written outside the Vault is backed up first
- [ ] `README.md`, `docs/` and `CHANGELOG.md` updated if behaviour changed
- [ ] If the way logs are read or classified changed: `LOGIC_VERSION` in `engine/chats.py` bumped
