"""Persistent, privacy-conscious user memory."""

import os
import json
import time
import random
import string
import logging
import threading
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping

from ambient_home.config import AmbientSettings


logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_FACTS = 60
MAX_FACT_CHARS = 280
MEMORY_FILENAME = "memory.v1.json"

_STORE_LOCK = threading.Lock()


@dataclass(frozen=True)
class MemoryFact:
    """One short long-term memory fact."""

    id: str
    text: str
    created_at: int

    def to_json(self) -> dict[str, object]:
        """Return the persisted memory shape."""
        return {"id": self.id, "text": self.text, "createdAt": self.created_at}


@dataclass(frozen=True)
class ForgetMemoryResult:
    """Result of removing a memory fact."""

    removed: MemoryFact | None
    candidates: tuple[MemoryFact, ...]


def memory_path(settings: AmbientSettings) -> Path:
    """Resolve the configured or baseline-compatible memory file path."""
    if settings.memory_file is not None:
        return settings.memory_file.expanduser()

    data_home = os.getenv("XDG_DATA_HOME")
    data_root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return data_root / "reachy_mini_conversation_app" / MEMORY_FILENAME


def normalize_memory_text(text: str) -> str:
    """Collapse whitespace and truncate facts to the persisted size limit."""
    normalized = " ".join(text.split()).strip()
    if len(normalized) <= MAX_FACT_CHARS:
        return normalized
    return f"{normalized[: MAX_FACT_CHARS - 3]}..."


def _make_id() -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"m_{int(time.time() * 1000)}_{suffix}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fact_from_json(value: object) -> MemoryFact | None:
    if not isinstance(value, Mapping):
        return None

    fact_id = value.get("id")
    text = value.get("text")
    created_at = value.get("createdAt")
    if not isinstance(fact_id, str) or not isinstance(text, str) or not isinstance(created_at, (int, float)):
        return None

    normalized = normalize_memory_text(text)
    if not normalized:
        return None
    return MemoryFact(id=fact_id, text=normalized, created_at=int(created_at))


def _read_memory_file(path: Path) -> list[MemoryFact]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Memory file not found at %s", path)
        return []
    except OSError as exc:
        logger.warning("Failed to read memory file at %s: %s", path, exc)
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse memory file at %s: %s", path, exc)
        return []

    if not isinstance(parsed, Mapping) or not isinstance(parsed.get("facts"), list):
        logger.warning("Ignoring invalid memory file at %s", path)
        return []

    facts = [fact for item in parsed["facts"] if (fact := _fact_from_json(item)) is not None]
    return facts[:MAX_FACTS]


def _write_memory_file(path: Path, facts: list[MemoryFact]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": SCHEMA_VERSION, "facts": [fact.to_json() for fact in facts[:MAX_FACTS]]}
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove temporary memory file at %s", tmp_path)


def list_memory_facts(path: Path) -> list[MemoryFact]:
    """Return stored memory facts, newest first."""
    with _STORE_LOCK:
        return list(_read_memory_file(path))


def add_memory_fact(path: Path, text: str) -> MemoryFact | None:
    """Store one short fact, deduplicating exact case-insensitive matches."""
    normalized = normalize_memory_text(text)
    if not normalized:
        return None

    with _STORE_LOCK:
        facts = _read_memory_file(path)
        existing = next((fact for fact in facts if fact.text.lower() == normalized.lower()), None)
        if existing is not None:
            return existing

        fact = MemoryFact(id=_make_id(), text=normalized, created_at=_now_ms())
        _write_memory_file(path, [fact, *facts][:MAX_FACTS])
        return fact


def forget_memory_fact(path: Path, *, query: str | None = None) -> ForgetMemoryResult:
    """Remove the first matching fact using a case-insensitive substring query."""
    with _STORE_LOCK:
        facts = _read_memory_file(path)
        normalized_query = normalize_memory_text(query or "").lower()
        if not normalized_query:
            return ForgetMemoryResult(removed=None, candidates=())

        candidates = tuple(fact for fact in facts if normalized_query in fact.text.lower())
        if not candidates:
            return ForgetMemoryResult(removed=None, candidates=())

        removed = candidates[0]
        _write_memory_file(path, [fact for fact in facts if fact.id != removed.id])
        return ForgetMemoryResult(removed=removed, candidates=candidates)


def clear_memory_facts(path: Path) -> None:
    """Remove all stored memory facts."""
    with _STORE_LOCK:
        _write_memory_file(path, [])


def format_memory_for_prompt(path: Path) -> str:
    """Format saved facts as the baseline session prompt block."""
    facts = list_memory_facts(path)
    if not facts:
        return ""

    bullets = "\n".join(f"- {fact.text}" for fact in facts)
    return "\n".join(
        [
            "Things you remember about the user (use this context naturally,",
            "do not recite the list verbatim):",
            bullets,
        ]
    )
