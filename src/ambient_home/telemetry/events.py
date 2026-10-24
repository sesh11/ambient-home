"""Local structured event logging."""

import logging
import threading
from typing import Literal
from pathlib import Path
from datetime import date, datetime, timezone
from collections.abc import Callable

from pydantic import Field, BaseModel, field_validator

from ambient_home.telemetry.scrub import scrub_data


logger = logging.getLogger(__name__)
EventLevel = Literal["info", "warning", "error"]


class Event(BaseModel):
    """One privacy-scrubbed telemetry event."""

    ts: datetime
    type: str
    component: str
    level: EventLevel = "info"
    data: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)


class EventLog:
    """Thread-safe append-only, day-partitioned JSONL event store."""

    def __init__(
        self,
        directory: Path,
        *,
        retention_days: int = 14,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """Configure the event directory and injected clock."""
        self.directory = directory
        self.retention_days = retention_days
        self.clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Event], None]] = []
        self._last_pruned_day: date | None = None

    def emit(
        self,
        type: str,
        component: str,
        level: EventLevel = "info",
        **data: object,
    ) -> Event | None:
        """Scrub and append an event without allowing logging failures to escape."""
        try:
            timestamp = self.clock()
            event = Event(
                ts=timestamp,
                type=type,
                component=component,
                level=level,
                data=scrub_data(data),
            )
            day = event.ts.date()
            with self._lock:
                if self._last_pruned_day != day:
                    self._prune(event.ts)
                    self._last_pruned_day = day
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / f"{day:%Y-%m-%d}.jsonl"
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(event.model_dump_json() + "\n")
                listeners = list(self._listeners)
            for listener in listeners:
                try:
                    listener(event)
                except Exception:
                    logger.debug("Event listener failed", exc_info=True)
            return event
        except (OSError, ValueError, TypeError):
            logger.debug("Could not write telemetry event", exc_info=True)
            return None
        except Exception:
            logger.debug("Could not write telemetry event", exc_info=True)
            return None

    def read(self, since: datetime, until: datetime | None = None) -> list[Event]:
        """Read valid events in the inclusive timestamp range."""
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must be timezone-aware")
        start = since.astimezone(timezone.utc)
        end_value = until or self.clock()
        if end_value.tzinfo is None or end_value.utcoffset() is None:
            raise ValueError("until must be timezone-aware")
        end = end_value.astimezone(timezone.utc)
        if start > end:
            return []
        result: list[Event] = []
        for ordinal in range(start.date().toordinal(), end.date().toordinal() + 1):
            path = self.directory / f"{date.fromordinal(ordinal):%Y-%m-%d}.jsonl"
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        try:
                            event = Event.model_validate_json(line)
                        except ValueError:
                            logger.debug("Skipping malformed telemetry event in %s", path)
                            continue
                        if start <= event.ts <= end:
                            result.append(event)
            except FileNotFoundError:
                continue
            except OSError:
                logger.debug("Could not read telemetry file %s", path, exc_info=True)
        return sorted(result, key=lambda event: event.ts)

    def prune(self) -> int:
        """Delete day files older than the configured retention interval."""
        return self._prune(self.clock())

    def _prune(self, now: datetime) -> int:
        cutoff = now.astimezone(timezone.utc).date().toordinal() - self.retention_days
        removed = 0
        try:
            for path in self.directory.glob("????-??-??.jsonl"):
                try:
                    day = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                if day.toordinal() < cutoff:
                    try:
                        path.unlink()
                        removed += 1
                    except FileNotFoundError:
                        continue
        except OSError:
            logger.debug("Could not prune telemetry files", exc_info=True)
        return removed

    def add_listener(self, callback: Callable[[Event], None]) -> None:
        """Register a callback invoked after each successfully appended event."""
        with self._lock:
            self._listeners.append(callback)


_event_log: EventLog | None = None
_singleton_lock = threading.Lock()


def configure(log: EventLog | None) -> None:
    """Set the process-local event log."""
    global _event_log
    with _singleton_lock:
        _event_log = log


def get_event_log() -> EventLog | None:
    """Return the configured process-local event log, if any."""
    with _singleton_lock:
        return _event_log


def emit(
    type: str,
    component: str,
    level: EventLevel = "info",
    **data: object,
) -> None:
    """Emit through the configured event log, if one exists."""
    log = get_event_log()
    if log is not None:
        log.emit(type, component, level, **data)
