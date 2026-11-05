# Security policy

## What matters here

Hamzaban reads the local logs of AI agents and stores redacted excerpts in a folder on the user's computer. The interesting risks are:

- a secret that gets stored because redaction missed it;
- a file written outside the Vault, or an agent's own files modified, deleted or overwritten;
- the control room page or a pack turning chat text into markup, script or instructions (injection);
- a path traversal or symlink trick through the MCP server's `memory_read`;
- anything that opens a network connection.

## Reporting

Please do **not** open a public issue for a vulnerability. Use GitHub's **private vulnerability reporting** (the *Security* tab of this repository → *Report a vulnerability*).
Describe what you found, how to reproduce it with fake data, and what you expect to happen. Do not include real chats or real secrets.

You can expect an answer within a week. Fixes are released as a new version with a note in `CHANGELOG.md`; credit is given if you want it.

## Supported versions

The latest release. This is a young project: please update before reporting.

## What is out of scope

- Secrets that the user pasted into a chat and that the agent itself sent to its provider.
- Anything that needs the attacker to already control the user's account or the Vault folder.
- The behaviour of the agents (Claude Code, Codex, ...) themselves.
