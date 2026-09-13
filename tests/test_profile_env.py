"""The Alfred profile must be active without any environment preparation."""

import sys
import subprocess


def test_alfred_profile_is_selected_in_a_fresh_interpreter() -> None:
    code = (
        "import ambient_home.main\n"
        "from reachy_mini_conversation_app.config import config\n"
        "assert config.REACHY_MINI_CUSTOM_PROFILE == 'alfred', config.REACHY_MINI_CUSTOM_PROFILE\n"
        "assert (config.PROFILES_DIRECTORY / 'alfred' / 'profile.md').is_file(), config.PROFILES_DIRECTORY\n"
        "from reachy_mini_conversation_app.prompts import get_session_instructions\n"
        "assert 'Alfred' in get_session_instructions(None)\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
    }
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
