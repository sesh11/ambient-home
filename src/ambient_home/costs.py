"""Local cost accounting for Live audio minutes and worker ACUs."""

import asyncio
import logging
from enum import Enum
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import JobBoard


logger = logging.getLogger(__name__)

WARN_FRACTION = 0.8
Announcer = Callable[[str], Awaitable[None]]


class CapKind(Enum):
    """Caps tracked by the dashboard."""

    LIVE_DAILY = "live_daily"
    ACU_MONTHLY = "acu_monthly"


@dataclass(frozen=True)
class CostRates:
    """Locally configured price estimates."""

    live_usd_per_minute: float
    acu_usd: float


@dataclass(frozen=True)
class CostCaps:
    """Spend ceilings enforced by the assistant."""

    live_daily_s: float
    acu_monthly: float


@dataclass(frozen=True)
class CapUsage:
    """Usage of one cap in its own unit plus a dollar estimate."""

    kind: CapKind
    label: str
    unit: str
    used: float
    cap: float
    usd: float

    @property
    def fraction(self) -> float:
        """Return used/cap, or 1.0 when the cap is zero and anything was used."""
        if self.cap <= 0:
            return 1.0 if self.used > 0 else 0.0
        return self.used / self.cap

    @property
    def remaining(self) -> float:
        """Return unused capacity, never negative."""
        return max(0.0, self.cap - self.used)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serializable view for the UI and voice tools."""
        return {
            "kind": self.kind.value,
            "label": self.label,
            "unit": self.unit,
            "used": round(self.used, 3),
            "cap": round(self.cap, 3),
            "remaining": round(self.remaining, 3),
            "usd": round(self.usd, 2),
            "fraction": round(self.fraction, 4),
        }


@dataclass(frozen=True)
class DayUsage:
    """One day of Live audio and ACU usage."""

    day: date
    live_seconds: float
    acus: float

    def as_dict(self, rates: CostRates) -> dict[str, object]:
        """Return a JSON-serializable view including dollar estimates."""
        live_usd = self.live_seconds / 60.0 * rates.live_usd_per_minute
        acu_usd = self.acus * rates.acu_usd
        return {
            "date": self.day.isoformat(),
            "live_minutes": round(self.live_seconds / 60.0, 2),
            "live_usd": round(live_usd, 2),
            "acus": round(self.acus, 2),
            "acu_usd": round(acu_usd, 2),
            "usd": round(live_usd + acu_usd, 2),
        }


class CostTracker:
    """Aggregate local spend, expose it for the UI, and fire cap alerts once."""

    def __init__(
        self,
        spend_log: SpendLog,
        board: JobBoard,
        *,
        rates: CostRates,
        caps: CostCaps,
        history_days: int = 7,
        live_session_seconds: Callable[[], float] = lambda: 0.0,
    ) -> None:
        """Configure sources, prices, and caps for cost reporting."""
        self.spend_log = spend_log
        self.board = board
        self.rates = rates
        self.caps = caps
        self.history_days = history_days
        self._live_session_seconds = live_session_seconds
        self._announced: set[tuple[CapKind, str, int]] = set()

    def live_seconds_today(self, now: datetime) -> float:
        """Return logged Live seconds today plus any in-flight session."""
        return self.spend_log.seconds_today(now) + max(0.0, self._live_session_seconds())

    def acus_this_month(self, now: datetime) -> float:
        """Return ACUs consumed by jobs created in the local calendar month."""
        month = now.astimezone().date().replace(day=1)
        return sum(job.acus_consumed or 0.0 for job in self.board.all() if job.created_at.astimezone().date() >= month)

    def usage(self, now: datetime) -> list[CapUsage]:
        """Return usage for every tracked cap."""
        live_seconds = self.live_seconds_today(now)
        acus = self.acus_this_month(now)
        return [
            CapUsage(
                kind=CapKind.LIVE_DAILY,
                label="Live audio today",
                unit="minutes",
                used=live_seconds / 60.0,
                cap=self.caps.live_daily_s / 60.0,
                usd=live_seconds / 60.0 * self.rates.live_usd_per_minute,
            ),
            CapUsage(
                kind=CapKind.ACU_MONTHLY,
                label="Worker ACUs this month",
                unit="ACU",
                used=acus,
                cap=self.caps.acu_monthly,
                usd=acus * self.rates.acu_usd,
            ),
        ]

    def history(self, now: datetime) -> list[DayUsage]:
        """Return per-day usage for the configured history window, oldest first."""
        today = now.astimezone().date()
        first_day = today - timedelta(days=self.history_days - 1)
        live_by_day = self.spend_log.seconds_by_day(first_day, today)
        acus_by_day: dict[date, float] = {}
        for job in self.board.all():
            day = job.created_at.astimezone().date()
            if first_day <= day <= today:
                acus_by_day[day] = acus_by_day.get(day, 0.0) + (job.acus_consumed or 0.0)
        live_by_day[today] = live_by_day.get(today, 0.0) + max(0.0, self._live_session_seconds())
        days = [first_day + timedelta(days=offset) for offset in range(self.history_days)]
        return [DayUsage(day, live_by_day.get(day, 0.0), acus_by_day.get(day, 0.0)) for day in days]

    def report(self, now: datetime) -> dict[str, object]:
        """Return the full dashboard payload."""
        usage = self.usage(now)
        history = self.history(now)
        today = history[-1] if history else DayUsage(now.astimezone().date(), 0.0, 0.0)
        live_usd = self.live_seconds_today(now) / 60.0 * self.rates.live_usd_per_minute
        acu_usd = self.acus_this_month(now) * self.rates.acu_usd
        return {
            "rates": {
                "live_usd_per_minute": self.rates.live_usd_per_minute,
                "acu_usd": self.rates.acu_usd,
            },
            "caps": [item.as_dict() for item in usage],
            "history": [day.as_dict(self.rates) for day in history],
            "today_usd": round(live_usd + today.acus * self.rates.acu_usd, 2),
            "month_usd": round(acu_usd, 2),
            "alerts": [self._alert_text(item) for item in usage if item.fraction >= WARN_FRACTION],
        }

    def dispatch_refusal(self, now: datetime) -> str | None:
        """Return a spoken reason when new worker jobs must be refused."""
        for item in self.usage(now):
            if item.kind is CapKind.ACU_MONTHLY and item.fraction >= 1.0:
                return (
                    f"We're at the monthly limit of {item.cap:.0f} ACUs, so I can't start new engineering jobs. "
                    "Raise AMBIENT_MONTHLY_ACU_CAP to keep going."
                )
        return None

    def due_alerts(self, now: datetime) -> list[str]:
        """Return alerts crossing 80% or 100% that have not been announced yet."""
        period_live = now.astimezone().date().isoformat()
        period_month = period_live[:7]
        messages: list[str] = []
        for item in self.usage(now):
            period = period_live if item.kind is CapKind.LIVE_DAILY else period_month
            for threshold in (100, 80):
                if item.fraction * 100 < threshold:
                    continue
                key = (item.kind, period, threshold)
                if key in self._announced:
                    break
                self._announced.add(key)
                self._announced.add((item.kind, period, 80))
                messages.append(self._alert_text(item))
                break
        return messages

    async def run_alert_poller(self, interval_s: float, announcer: Announcer) -> None:
        """Announce newly crossed cap thresholds at a fixed interval."""
        while True:
            try:
                for message in self.due_alerts(datetime.now().astimezone()):
                    logger.warning("Cost alert: %s", message)
                    await announcer(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cost alert poller iteration failed")
            await asyncio.sleep(interval_s)

    @staticmethod
    def _alert_text(item: CapUsage) -> str:
        """Return the alert sentence for a cap at or above a threshold."""
        percent = int(item.fraction * 100)
        if item.fraction >= 1.0:
            return f"{item.label} has reached its cap of {item.cap:.0f} {item.unit}."
        return f"{item.label} is at {percent}% of its cap of {item.cap:.0f} {item.unit}."


def spoken_summary(tracker: CostTracker, now: datetime) -> str:
    """Return a short spoken sentence describing today's spend."""
    usage = {item.kind: item for item in tracker.usage(now)}
    live = usage[CapKind.LIVE_DAILY]
    acu = usage[CapKind.ACU_MONTHLY]
    return (
        f"Today we've used {live.used:.0f} of {live.cap:.0f} Live minutes, about {live.usd:.2f} dollars. "
        f"This month engineering jobs have used {acu.used:.1f} of {acu.cap:.0f} ACUs, "
        f"about {acu.usd:.0f} dollars."
    )
