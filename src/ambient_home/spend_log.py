"""Persistent local Live audio spend accounting."""

import logging
from pathlib import Path
from datetime import date, datetime

from pydantic import BaseModel


logger = logging.getLogger(__name__)


class SpendRecord(BaseModel):
    """One completed Live session spend record."""

    started_at: datetime
    ended_at: datetime
    seconds: float
    reason: str
    reported_usage_seconds: float | None = None


class SpendLog:
    """Append-only JSONL spend log."""

    def __init__(self, path: Path) -> None:
        """Create a spend log at the supplied path."""
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: SpendRecord) -> None:
        """Append one record."""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def seconds_today(self, now: datetime) -> float:
        """Sum records whose start date is the local date of ``now``."""
        target_date: date = now.astimezone().date()
        return self.seconds_by_day(target_date, target_date).get(target_date, 0.0)

    def seconds_by_day(self, first_day: date, last_day: date) -> dict[date, float]:
        """Sum seconds per local start date across an inclusive date range."""
        totals: dict[date, float] = {}
        if not self.path.exists():
            return totals
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = SpendRecord.model_validate_json(line)
                except ValueError:
                    logger.warning("Skipping unreadable spend record in %s", self.path)
                    continue
                day = record.started_at.astimezone().date()
                if first_day <= day <= last_day:
                    totals[day] = totals.get(day, 0.0) + record.seconds
        return totals
