"""Stable interface and common models for incident detectors."""

import re
import hashlib
from typing import Literal, Protocol
from datetime import datetime, timedelta
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from ambient_home.telemetry.events import Event


Severity = Literal["low", "medium", "high", "critical"]
_DURATION = re.compile(r"^(\d+)([smhd])$")


class Incident(BaseModel):
    """One grouped incident observation."""

    fingerprint: str
    rule: str
    title: str
    severity: Severity
    count: int
    threshold: int
    window_s: float
    first_seen: datetime
    last_seen: datetime
    ready: bool
    group: dict[str, str] = {}
    samples: list[Event]
    detector: str


class Detector(Protocol):
    """Async interface implemented by any incident detection strategy."""

    name: str

    async def detect(self, events: Sequence[Event], now: datetime) -> list[Incident]:
        """Return incidents derived only from the supplied event snapshot."""
        ...


def parse_duration(value: str) -> timedelta:
    """Parse integer seconds, minutes, hours, or days."""
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid duration {value!r}; expected values such as 90s, 10m, 6h, or 1d")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def normalize_message(text: str) -> str:
    """Normalize volatile message fragments for stable grouping."""
    normalized = text.lower()
    normalized = re.sub(r"""(["']).*?\1""", "<quoted>", normalized)
    normalized = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<uuid>",
        normalized,
    )
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", normalized)
    normalized = re.sub(r"(?:~|/[\w.-]+(?:/[\w.-]+)+)", "<path>", normalized)
    normalized = re.sub(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", "<number>", normalized)
    return " ".join(normalized.split())


def make_fingerprint(rule: str, group: Mapping[str, str]) -> str:
    """Build a stable short fingerprint from a rule and sorted group values."""
    serialized = "\n".join(f"{key}={value}" for key, value in sorted(group.items()))
    digest = hashlib.sha1(serialized.encode("utf-8")).hexdigest()[:10]
    return f"{rule}:{digest}"
