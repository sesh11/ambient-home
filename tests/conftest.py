"""Shared pytest fixtures."""

import os


os.environ.setdefault("REACHY_MINI_EXTERNAL_PROFILES_DIRECTORY", "src/ambient_home/profiles")
os.environ.setdefault("REACHY_MINI_EXTERNAL_TOOLS_DIRECTORY", "src/ambient_home/reachy_tools")
os.environ.setdefault("REACHY_MINI_CUSTOM_PROFILE", "alfred")
