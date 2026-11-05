"""The target of the Hamzaban shortcuts: opens the control room as an application window (no console window)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_window  # noqa: E402

sys.exit(app_window.main())
