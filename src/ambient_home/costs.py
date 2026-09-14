"""Local cost accounting for Live audio minutes and worker ACUs."""

from datetime import date, datetime, timedelta
from dataclasses import dataclass
from collections.abc import Callable

from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import JobBoard


@dataclass(frozen=True)
class CostRates:
    """Locally configured price estimates."""

    live_usd_per_minute: float
    acu_usd: float


@dataclass(frozen=True)
class DayUsage:
    """One day of Live audio and ACU usage."""

    day: date
    live_seconds: float
    acus: float

    def usd(self, rates: CostRates) -> float:
        """Return the dollar estimate for this day."""
        return self.live_seconds / 60.0 * rates.live_usd_per_minute + self.acus * rates.acu_usd

    def as_dict(self, rates: CostRates) -> dict[str, object]:
        """Return a JSON-serializable view including dollar estimates."""
        return {
            "date": self.day.isoformat(),
            "live_minutes": round(self.live_seconds / 60.0, 2),
            "live_usd": round(self.live_seconds / 60.0 * rates.live_usd_per_minute, 2),
            "acus": round(self.acus, 2),
            "acu_usd": round(self.acus * rates.acu_usd, 2),
            "usd": round(self.usd(rates), 2),
        }


class CostTracker:
    """Aggregate local Live audio and worker spend for the dashboard."""

    def __init__(
        self,
        spend_log: SpendLog,
        board: JobBoard,
        *,
        rates: CostRates,
        history_days: int = 7,
        live_session_seconds: Callable[[], float] = lambda: 0.0,
    ) -> None:
        """Configure spend sources, price estimates, and the history window."""
        self.spend_log = spend_log
        self.board = board
        self.rates = rates
        self.history_days = history_days
        self._live_session_seconds = live_session_seconds

    def history(self, now: datetime) -> list[DayUsage]:
        """Return per-day usage for the configured window, oldest first."""
        today = now.astimezone().date()
        first_day = today - timedelta(days=self.history_days - 1)
        live_by_day = self.spend_log.seconds_by_day(first_day, today)
        live_by_day[today] = live_by_day.get(today, 0.0) + max(0.0, self._live_session_seconds())
        acus_by_day: dict[date, float] = {}
        for job in self.board.all():
            day = job.created_at.astimezone().date()
            if first_day <= day <= today:
                acus_by_day[day] = acus_by_day.get(day, 0.0) + (job.acus_consumed or 0.0)
        days = [first_day + timedelta(days=offset) for offset in range(self.history_days)]
        return [DayUsage(day, live_by_day.get(day, 0.0), acus_by_day.get(day, 0.0)) for day in days]

    def report(self, now: datetime) -> dict[str, object]:
        """Return the dashboard payload: today's usage plus the daily history."""
        history = self.history(now)
        today = history[-1] if history else DayUsage(now.astimezone().date(), 0.0, 0.0)
        return {
            "rates": {
                "live_usd_per_minute": self.rates.live_usd_per_minute,
                "acu_usd": self.rates.acu_usd,
            },
            "today": today.as_dict(self.rates),
            "history": [day.as_dict(self.rates) for day in history],
            "window_usd": round(sum(day.usd(self.rates) for day in history), 2),
        }


def spoken_summary(tracker: CostTracker, now: datetime) -> str:
    """Return a short spoken sentence describing today's spend."""
    today = tracker.history(now)[-1]
    return (
        f"Today we've used {today.live_seconds / 60.0:.0f} Live minutes "
        f"and {today.acus:.1f} worker ACUs, about {today.usd(tracker.rates):.2f} dollars."
    )
