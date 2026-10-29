"""Persona profile loading tests."""

import pytest

from ambient_home.persona import ProfileError, load_profile


def test_loads_packaged_alfred_profile() -> None:
    profile = load_profile()
    assert profile.voice == "ballad"
    assert "end_conversation" in profile.tools
    assert "dispatch_job" in profile.tools
    assert "Alfred" in profile.instructions
    assert profile.greeting == "Good day, sir. How may I be of service?"


def test_invalid_profile_frontmatter_raises(tmp_path) -> None:
    profile_dir = tmp_path / "broken"
    profile_dir.mkdir()
    (profile_dir / "profile.md").write_text("+++\nnot = [valid\n+++\nInstructions")
    with pytest.raises(ProfileError):
        load_profile("broken", tmp_path)
