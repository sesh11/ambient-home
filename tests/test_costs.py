"""Cost tracker tests."""

from datetime import datetime, timezone, timedelta

from ambient_home.costs import CostRates, CostTracker, spoken_summary
from ambient_home.spend_log import SpendLog, SpendRecord
from ambient_home.jobs.board import Job, JobBoard


RATES = CostRates(live_usd_per_minute=0.3, acu_usd=2.0)


def build_tracker(tmp_path, *, live_session_seconds: float = 0.0) -> CostTracker:
    return CostTracker(
        SpendLog(tmp_path / "spend.jsonl"),
        JobBoard(tmp_path / "jobs.json"),
        rates=RATES,
        history_days=7,
        live_session_seconds=lambda: live_session_seconds,
    )


def test_report_combines_today_and_daily_history(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    tracker = build_tracker(tmp_path, live_session_seconds=60.0)
    tracker.spend_log.record(SpendRecord(started_at=now, ended_at=now, seconds=120, reason="idle"))
    tracker.spend_log.record(SpendRecord(started_at=yesterday, ended_at=yesterday, seconds=60, reason="idle"))
    tracker.board.add(Job(request="today", acus_consumed=1.5, created_at=now))
    tracker.board.add(Job(request="yesterday", acus_consumed=0.5, created_at=yesterday))

    report = tracker.report(now)

    assert report["today"] == {
        "date": now.astimezone().date().isoformat(),
        "live_minutes": 3.0,
        "live_usd": 0.9,
        "acus": 1.5,
        "acu_usd": 3.0,
        "usd": 3.9,
    }
    assert len(report["history"]) == 7
    assert report["history"][-2]["live_minutes"] == 1.0
    assert report["history"][-2]["acus"] == 0.5
    assert report["history"][0]["usd"] == 0.0
    assert report["window_usd"] == 5.2


def test_history_excludes_days_outside_the_window(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    tracker = build_tracker(tmp_path)
    old = now - timedelta(days=9)
    tracker.spend_log.record(SpendRecord(started_at=old, ended_at=old, seconds=600, reason="idle"))
    tracker.board.add(Job(request="old", acus_consumed=3.0, created_at=old))

    assert tracker.report(now)["window_usd"] == 0.0


def test_spoken_summary_reports_today(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    tracker = build_tracker(tmp_path)
    tracker.spend_log.record(SpendRecord(started_at=now, ended_at=now, seconds=300, reason="idle"))
    tracker.board.add(Job(request="job", acus_consumed=2.0, created_at=now))

    summary = spoken_summary(tracker, now)

    assert "5 Live minutes" in summary
    assert "2.0 worker ACUs" in summary
    assert "5.50 dollars" in summary
