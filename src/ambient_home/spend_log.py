"""Persistent local Live audio spend accounting."""

from pathlib import Path
from datetime import date, datetime

from pydantic import BaseModel


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
        if not self.path.exists():
            return 0.0
        total = 0.0
        target_date: date = now.astimezone().date()
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = SpendRecord.model_validate_json(line)
                if record.started_at.astimezone().date() == target_date:
                    total += record.seconds
        return total
