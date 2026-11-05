#!/bin/sh
# Hamzaban installer for macOS and Linux (experimental). Finds Python 3.10+ and runs install.py; extra arguments are passed on.
cd "$(dirname "$0")" || exit 1
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    exec "$py" install.py "$@"
  fi
done
echo "Hamzaban needs Python 3.10 or newer, and it was not found." >&2
echo "Install it (python.org, Homebrew or your package manager) and run this again." >&2
exit 1
