"""Answers hamzaban: links (open an application, show a chat's file or folder). Windows starts it with the link; shortcuts.py registers it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_links  # noqa: E402

sys.exit(app_links.handler_main())
