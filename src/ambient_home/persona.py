"""Profile loading for the conversation persona."""

import tomllib
from pathlib import Path
from dataclasses import dataclass


DEFAULT_GREETING_PROMPT = (
    "Start the conversation now with a brief, spontaneous greeting in character. "
    "Keep it to one sentence, invite the user in naturally, and vary the wording each time."
)


class ProfileError(ValueError):
    """A profile is missing or does not match the supported format."""


@dataclass(frozen=True)
class Profile:
    """Immutable instructions and defaults for one conversation persona."""

    name: str
    instructions: str
    voice: str | None
    greeting: str | None
    tools: tuple[str, ...]


def load_profile(name: str = "alfred", directory: Path | None = None) -> Profile:
    """Load a Markdown profile with TOML front matter."""
    root = directory or Path(__file__).resolve().parent / "profiles"
    path = root / name / "profile.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "+++":
            raise ValueError("expected TOML front matter starting with '+++'")
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "+++")
        metadata = tomllib.loads("\n".join(lines[1:closing]))
        instructions = "\n".join(lines[closing + 1 :]).strip()
        if metadata.get("schema_version") != 1:
            raise ValueError("unsupported profile schema_version")
        voice = metadata.get("voice")
        greeting = metadata.get("greeting")
        tools = metadata.get("default_tools")
        if voice is not None and not isinstance(voice, str):
            raise ValueError("voice must be a string")
        if greeting is not None and not isinstance(greeting, str):
            raise ValueError("greeting must be a string")
        if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
            raise ValueError("default_tools must be a list of strings")
        if not instructions:
            raise ValueError("profile instructions must not be empty")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, StopIteration, ValueError) as exc:
        raise ProfileError(f"invalid profile {name!r}: {exc}") from exc
    return Profile(name, instructions, voice, greeting, tuple(tools))
