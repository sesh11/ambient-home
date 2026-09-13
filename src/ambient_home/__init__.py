"""Ambient Reachy Mini voice assistant."""

import os
from pathlib import Path


# reachy_mini_conversation_app.config reads these at import time, so they must be
# in place before any module in this package imports it.
_PACKAGE_ROOT = Path(__file__).parent
os.environ.setdefault("REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY", str(_PACKAGE_ROOT / "profiles"))
os.environ.setdefault("REACHY_MINI_CUSTOM_PROFILE", "alfred")
os.environ.setdefault("REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY", str(_PACKAGE_ROOT / "reachy_tools"))
