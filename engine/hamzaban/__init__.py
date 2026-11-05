"""Hamzaban (همزبان): one shared language for carrying context and harness between AI agents.

The idea in one line: every agent reads and writes the same neutral "pack" (English labels, the
user's own words inside), and a small adapter turns that pack into the dialect each agent likes.

Python stdlib only. Nothing here talks to the network. Importing this package has no side effects.
Design note: the hub-and-spoke shape (one neutral format in the middle, one adapter per agent) is
the same idea that API gateways such as OmniRoute (MIT) use for chat messages; no code was copied.
"""
from __future__ import annotations

import sys
from pathlib import Path

NAME = "Hamzaban"
NAME_FA = "همزبان"
VERSION = "1.0.0"
PACK_FORMAT = "hamzaban-pack"
PACK_VERSION = 1
CLOSE_MARKER = "hamzaban-close"
CLOSE_VERSION = 1

ENGINE = Path(__file__).resolve().parent.parent  # the .ai-memory folder
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
