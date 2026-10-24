"""Telemetry privacy scrubbing tests."""

from pathlib import Path

from ambient_home.telemetry import EventLog, scrub_text


def test_emit_scrubs_sensitive_values_and_drops_content_fields(tmp_path) -> None:
    event_log = EventLog(tmp_path / "events")
    sensitive = {
        "email": "alice@example.com",
        "phone": "+1 (415) 555-0199",
        "openai_key": "sk-liveAbCdEf1234567890",
        "devin_key": "apk_user_AbCdEf1234567890",
        "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.tokenvalue",
        "blob": "a1b2c3d4e5f6a7b8c9d0" * 2,
        "home_path": str(Path.home() / "private" / "file.txt"),
        "other_path": "/Users/alice/private/file.txt",
        "public_ip": "203.0.113.42",
        "transcript": "private spoken words",
        "request": "private requested task",
    }
    event = event_log.emit("test.privacy", "test", **sensitive)
    assert event is not None
    raw = next((tmp_path / "events").glob("*.jsonl")).read_bytes()

    for value in (
        "alice@example.com",
        "415) 555-0199",
        "sk-liveAbCdEf1234567890",
        "apk_user_AbCdEf1234567890",
        "eyJhbGciOiJIUzI1NiJ9.tokenvalue",
        "a1b2c3d4e5f6a7b8c9d0",
        str(Path.home()),
        "/Users/alice",
        "203.0.113.42",
        "private spoken words",
        "private requested task",
    ):
        assert value.encode() not in raw
    assert b'"transcript"' not in raw
    assert b'"request"' not in raw


def test_scrub_patterns_and_harmless_values() -> None:
    cases = {
        "mail alice@example.com": "<email>",
        "call +1 415-555-0199": "<phone>",
        "key sk-AbCdEf1234567890": "<secret>",
        "key apk_user_AbCdEf1234567890": "<secret>",
        "key ghp_AbCdEf1234567890": "<secret>",
        "key gho_AbCdEf1234567890": "<secret>",
        "key github_pat_AbCdEf1234567890": "<secret>",
        "header Bearer eyJhbGciOiJIUzI1NiJ9": "<secret>",
        "Authorization: Basic abc123": "<secret>",
        "mixed a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6": "<secret>",
        "ip 203.0.113.42": "<ip>",
        "/Users/alice/project": "/Users/<user>/project",
        "/home/bob/project": "/home/<user>/project",
    }
    for value, replacement in cases.items():
        assert replacement in scrub_text(value)

    assert scrub_text("verdict ok") == "verdict ok"
    assert scrub_text("127.0.0.1") == "127.0.0.1"
    assert scrub_text("x" * 501) == "x" * 500 + "…"


def test_current_home_path_is_replaced() -> None:
    assert scrub_text(str(Path.home() / "secret.txt")) == "~/secret.txt"
