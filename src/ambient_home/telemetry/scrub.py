"""Privacy scrubbing for telemetry data."""

import re
from pathlib import Path
from collections.abc import Mapping


DROPPED_KEYS = frozenset(
    {"transcript", "text", "audio", "request", "prompt", "content", "question", "answer", "message_text"}
)

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)(?=(?:\D*\d){10})\+?[\d(). -]{10,}(?!\w)")
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bapk_(?:user_)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bAuthorization\s*:\s*[^\s,;]+(?:\s+[^\s,;]+)?"),
    re.compile(
        r"\b(?=[A-Za-z0-9_\-+/=]{32,}\b)(?=[A-Za-z0-9_\-+/=]*[A-Za-z])(?=[A-Za-z0-9_\-+/=]*\d)[A-Za-z0-9_\-+/=]+\b"
    ),
)
_USER_PATH = re.compile(r"/(?:Users|home)/[^/\s]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def scrub_text(value: str) -> str:
    """Replace common personal data and secrets in a string."""
    text = value.replace(str(Path.home()), "~")
    text = _USER_PATH.sub(
        lambda match: "/Users/<user>" if match.group().startswith("/Users/") else "/home/<user>", text
    )
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("<secret>", text)
    text = _EMAIL.sub("<email>", text)
    text = _PHONE.sub("<phone>", text)

    def replace_ip(match: re.Match[str]) -> str:
        address = match.group()
        octets = address.split(".")
        if address == "127.0.0.1" or any(int(octet) > 255 for octet in octets):
            return address
        return "<ip>"

    text = _IPV4.sub(replace_ip, text)
    if len(text) > 500:
        return text[:500] + "…"
    return text


def scrub_data(data: Mapping[str, object]) -> dict[str, str | int | float | bool | None]:
    """Drop sensitive fields and reduce values to scrubbed JSON scalars."""
    scrubbed: dict[str, str | int | float | bool | None] = {}
    for key, value in data.items():
        if key.lower() in DROPPED_KEYS:
            continue
        if isinstance(value, str):
            scrubbed[key] = scrub_text(value)
        elif value is None or isinstance(value, (bool, int, float)):
            scrubbed[key] = value
        else:
            scrubbed[key] = scrub_text(str(value))
    return scrubbed
