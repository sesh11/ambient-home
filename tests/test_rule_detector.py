"""Rule detector tests."""

from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from ambient_home.incidents.base import normalize_message
from ambient_home.incidents.rules import Rule, RuleDetector
from ambient_home.telemetry.events import Event


NOW = datetime(2025, 1, 2, 12, tzinfo=timezone.utc)
RULES_PATH = Path(__file__).parents[1] / "src" / "ambient_home" / "incident_rules.toml"


def _event(event_type: str, minutes_ago: int = 0, **data: object) -> Event:
    return Event(
        ts=NOW - timedelta(minutes=minutes_ago),
        type=event_type,
        component="test",
        data=data,
    )


async def test_mic_rule_threshold_boundary_and_where_filter() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    silent = [_event("mic.check", verdict="silent") for _ in range(3)]
    ignored = _event("mic.check", verdict="ok")

    below = await detector.detect([*silent[:2], ignored], NOW)
    ready = await detector.detect([*silent, ignored], NOW)

    assert len(below) == 1
    assert below[0].count == 2
    assert not below[0].ready
    assert ready[0].count == 3
    assert ready[0].ready


async def test_window_excludes_old_events() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    events = [
        _event("mic.check", minutes_ago=60 * 7, verdict="silent"),
        _event("mic.check", verdict="silent"),
    ]

    incidents = await detector.detect(events, NOW)

    assert [incident.count for incident in incidents] == [1]


async def test_group_by_tool_creates_distinct_stable_fingerprints_and_title() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    events = [
        _event("tool.completed", tool="look", ok=False),
        _event("tool.completed", tool="look", ok=False),
        _event("tool.completed", tool="answer_job", ok=False),
    ]

    incidents = await detector.detect(events, NOW)
    again = await detector.detect(events, NOW)

    assert {incident.group["tool"] for incident in incidents} == {"look", "answer_job"}
    assert len({incident.fingerprint for incident in incidents}) == 2
    assert {incident.fingerprint for incident in incidents} == {incident.fingerprint for incident in again}
    assert next(item for item in incidents if item.group["tool"] == "look").title == "Tool look keeps failing"


async def test_group_by_message_normalizes_numbers() -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    events = [
        _event("log.error", message="timeout after 3s"),
        _event("log.error", message="timeout after 5s"),
    ]

    incidents = await detector.detect(events, NOW)

    assert len(incidents) == 1
    assert incidents[0].count == 2
    assert incidents[0].group["message"] == "timeout after <number>s"
    assert normalize_message("timeout after 3s") == normalize_message("timeout after 5s")


async def test_cleared_by_suppresses_recovered_matches_and_pending_matches() -> None:
    rule = Rule(
        name="daemon_lost",
        title="Daemon lost",
        severity="critical",
        types=["daemon.lost"],
        threshold=1,
        window="24h",
        cleared_by="daemon.ready",
        clear_within="10m",
    )
    detector = RuleDetector([rule])

    recovered = await detector.detect(
        [_event("daemon.lost", minutes_ago=20), _event("daemon.ready", minutes_ago=15)],
        NOW,
    )
    unrecovered = await detector.detect([_event("daemon.lost", minutes_ago=12)], NOW)
    pending = await detector.detect([_event("daemon.lost", minutes_ago=5)], NOW)

    assert recovered == []
    assert len(unrecovered) == 1 and unrecovered[0].ready
    assert pending == []


def test_packaged_rules_and_invalid_files(tmp_path) -> None:
    detector = RuleDetector.from_file(RULES_PATH)
    assert {rule.name for rule in detector.rules} == {
        "mic_all_zero",
        "mic_silent_stream",
        "live_error",
        "tool_failed",
        "watchdog_restart",
        "daemon_lost_unrecovered",
        "devin_job_failed",
        "uncaught_error",
    }

    invalid_toml = tmp_path / "invalid.toml"
    invalid_toml.write_text("[[rule]\nname = ")
    with pytest.raises(ValueError, match="invalid incident rules file"):
        RuleDetector.from_file(invalid_toml)

    missing_clear_window = tmp_path / "missing-clear.toml"
    missing_clear_window.write_text(
        '[[rule]]\nname="x"\ntitle="x"\nseverity="low"\ntypes=["x"]\nthreshold=1\nwindow="1h"\ncleared_by="x"\n'
    )
    with pytest.raises(ValueError, match="clear_within is required"):
        RuleDetector.from_file(missing_clear_window)
