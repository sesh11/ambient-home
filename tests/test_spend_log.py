"""Spend log tests."""

from datetime import datetime, timezone, timedelta

from ambient_home.spend_log import SpendLog, SpendRecord


def test_spend_log_appends_and_sums_today(tmp_path) -> None:
    path = tmp_path / "spend.jsonl"
    log = SpendLog(path)
    now = datetime.now(timezone.utc)
    log.record(SpendRecord(started_at=now, ended_at=now, seconds=3, reason="idle"))
    yesterday = now - timedelta(days=1)
    log.record(SpendRecord(started_at=yesterday, ended_at=yesterday, seconds=8, reason="idle"))
    assert log.seconds_today(now) == 3
