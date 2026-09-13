"""Cost tracker and alert tests."""

from datetime import datetime, timezone, timedelta

from ambient_home.costs import CapKind, CostCaps, CostRates, CostTracker, spoken_summary
from ambient_home.spend_log import SpendLog, SpendRecord
from ambient_home.jobs.board import Job, JobBoard


RATES = CostRates(live_usd_per_minute=0.3, acu_usd=2.0)
CAPS = CostCaps(live_daily_s=600.0, acu_monthly=10.0)


def build_tracker(tmp_path, *, live_session_seconds: float = 0.0) -> CostTracker:
    return CostTracker(
        SpendLog(tmp_path / "spend.jsonl"),
        JobBoard(tmp_path / "jobs.json"),
        rates=RATES,
        caps=CAPS,
        history_days=7,
        live_session_seconds=lambda: live_session_seconds,
    )


def test_report_combines_live_minutes_acus_and_history(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    tracker = build_tracker(tmp_path, live_session_seconds=60.0)
    tracker.spend_log.record(SpendRecord(started_at=now, ended_at=now, seconds=120, reason="idle"))
    yesterday = now - timedelta(days=1)
    tracker.spend_log.record(SpendRecord(started_at=yesterday, ended_at=yesterday, seconds=60, reason="idle"))
    tracker.board.add(Job(request="today", acus_consumed=1.5, created_at=now))
    tracker.board.add(Job(request="yesterday", acus_consumed=0.5, created_at=yesterday))

    report = tracker.report(now)
    caps = {item["kind"]: item for item in report["caps"]}

    assert caps["live_daily"]["used"] == 3.0
    assert caps["live_daily"]["usd"] == 0.9
    assert caps["acu_monthly"]["used"] == 2.0
    assert caps["acu_monthly"]["remaining"] == 8.0
    assert report["today_usd"] == 3.9
    assert len(report["history"]) == 7
    assert report["history"][-1]["live_minutes"] == 3.0
    assert report["history"][-2]["acus"] == 0.5
    assert report["alerts"] == []


def test_alerts_fire_once_per_threshold(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    tracker = build_tracker(tmp_path)
    tracker.spend_log.record(SpendRecord(started_at=now, ended_at=now, seconds=480, reason="idle"))

    warnings = tracker.due_alerts(now)
    assert warnings == ["Live audio today is at 80% of its cap of 10 minutes."]
    assert tracker.due_alerts(now) == []

    tracker.spend_log.record(SpendRecord(started_at=now, ended_at=now, seconds=200, reason="idle"))
    assert tracker.due_alerts(now) == ["Live audio today has reached its cap of 10 minutes."]
    assert tracker.due_alerts(now) == []
    assert tracker.report(now)["alerts"] == ["Live audio today has reached its cap of 10 minutes."]


def test_acu_cap_refuses_dispatch_and_announces(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    tracker = build_tracker(tmp_path)
    assert tracker.dispatch_refusal(now) is None

    tracker.board.add(Job(request="expensive", acus_consumed=10.0, created_at=now))
    refusal = tracker.dispatch_refusal(now)

    assert refusal is not None
    assert "monthly limit of 10 ACUs" in refusal
    assert tracker.due_alerts(now) == ["Worker ACUs this month has reached its cap of 10 ACU."]


def test_acus_outside_current_month_are_excluded(tmp_path) -> None:
    now = datetime(2026, 3, 5, 12, tzinfo=timezone.utc)
    tracker = build_tracker(tmp_path)
    tracker.board.add(
        Job(request="last month", acus_consumed=4.0, created_at=datetime(2026, 2, 27, tzinfo=timezone.utc))
    )
    tracker.board.add(Job(request="this month", acus_consumed=1.0, created_at=now))

    assert tracker.acus_this_month(now) == 1.0


def test_spoken_summary_mentions_both_caps(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    tracker = build_tracker(tmp_path)
    tracker.spend_log.record(SpendRecord(started_at=now, ended_at=now, seconds=300, reason="idle"))
    tracker.board.add(Job(request="job", acus_consumed=2.0, created_at=now))

    summary = spoken_summary(tracker, now)

    assert "5 of 10 Live minutes" in summary
    assert "2.0 of 10 ACUs" in summary


def test_usage_uses_cap_kinds(tmp_path) -> None:
    tracker = build_tracker(tmp_path)
    assert [item.kind for item in tracker.usage(datetime.now(timezone.utc))] == [
        CapKind.LIVE_DAILY,
        CapKind.ACU_MONTHLY,
    ]
