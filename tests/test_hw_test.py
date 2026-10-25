"""Hardware test runner tests (no robot needed)."""

import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pytest
from reachy_mini.utils import create_head_pose

from ambient_home import hw_test
from ambient_home.hw_test import (
    FAIL,
    PASS,
    SKIP,
    Check,
    PausedApp,
    CheckFailed,
    CheckResult,
    run_checks,
    wake_phrase,
    head_pitch_deg,
    check_telemetry,
    format_markdown,
)
from ambient_home.telemetry import EventLog


def _fail() -> str:
    raise CheckFailed("broken")


def _crash() -> str:
    raise OSError("no device")


def test_run_checks_skips_dependents_of_failures() -> None:
    results = run_checks(
        [
            Check("a", lambda: "fine"),
            Check("b", _fail),
            Check("c", lambda: "fine", needs=("b",)),
            Check("d", _crash, needs=("a",)),
        ]
    )

    assert [(r.name, r.status) for r in results] == [("a", PASS), ("b", FAIL), ("c", SKIP), ("d", FAIL)]
    assert results[1].detail == "broken"
    assert results[2].detail == "needs b"
    assert results[3].detail == "OSError: no device"


def test_run_checks_scrubs_details() -> None:
    results = run_checks([Check("a", lambda: "token sk-abcdefghijklmnop in /Users/nithin/x")])

    assert "sk-" not in results[0].detail
    assert "nithin" not in results[0].detail


def test_wake_phrase() -> None:
    assert wake_phrase("hey_alfred") == "hey alfred"
    assert wake_phrase("/models/hey_jarvis.onnx") == "hey jarvis"


def test_head_pitch_deg_reads_pitch() -> None:
    assert head_pitch_deg(create_head_pose(pitch=10, degrees=True)) == pytest.approx(10.0)
    assert head_pitch_deg(np.eye(4)) == pytest.approx(0.0)


def test_check_telemetry_round_trip(tmp_path: Path) -> None:
    log = EventLog(tmp_path)

    assert "read back" in check_telemetry(log, "run-1")
    assert [event.type for event in log.read(datetime(2000, 1, 1, tzinfo=timezone.utc))] == ["hw_test.probe"]


class FakeLaunchctl:
    def __init__(self, loaded: bool, bootstrap_rc: int = 0) -> None:
        self.loaded = loaded
        self.bootstrap_rc = bootstrap_rc
        self.calls: list[str] = []

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append(args[0])
        rc = {"print": 0 if self.loaded else 113, "bootout": 0, "bootstrap": self.bootstrap_rc}[args[0]]
        return subprocess.CompletedProcess(["launchctl", *args], rc, "", "")


def test_paused_app_restarts_even_when_checks_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw_test.time, "sleep", lambda seconds: None)
    fake = FakeLaunchctl(loaded=True)
    pause = PausedApp(run=fake, system=lambda: "Darwin")

    with pytest.raises(ValueError), pause:
        raise ValueError("boom")

    assert fake.calls == ["print", "bootout", "bootstrap"]
    assert pause.restored is True


def test_paused_app_leaves_unloaded_agent_alone() -> None:
    fake = FakeLaunchctl(loaded=False)

    with PausedApp(run=fake, system=lambda: "Darwin") as pause:
        pass

    assert fake.calls == ["print"]
    assert pause.paused is False
    assert pause.restored is None


def test_paused_app_noop_off_macos_or_disabled() -> None:
    fake = FakeLaunchctl(loaded=True)

    with PausedApp(run=fake, system=lambda: "Linux"):
        pass
    with PausedApp(enabled=False, run=fake, system=lambda: "Darwin"):
        pass

    assert fake.calls == []


def test_format_markdown_reports_failure_and_restart() -> None:
    text = format_markdown(
        [CheckResult("mic", PASS, "ok", 1.0), CheckResult("head", FAIL, "a|b", 2.0)],
        total_s=42.0,
        app_restored=True,
    )

    assert "FAIL (42 s)" in text
    assert "| head | FAIL | a\\|b |" in text
    assert "restarted afterwards: yes" in text


def test_main_writes_reports_and_returns_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMBIENT_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setattr(
        hw_test,
        "build_checks",
        lambda *args, **kwargs: [Check("ok", lambda: "fine"), Check("bad", _fail)],
    )

    code = hw_test.main(["--no-pause", "--json", str(tmp_path / "r.json"), "--markdown", str(tmp_path / "r.md")])

    assert code == 1
    payload = json.loads((tmp_path / "r.json").read_text())
    assert payload["ok"] is False
    assert [c["status"] for c in payload["checks"]] == [PASS, FAIL]
    assert "FAIL" in (tmp_path / "r.md").read_text()
    events = EventLog(tmp_path / "events").read(datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert [e.type for e in events] == ["hw_test.check", "hw_test.check", "hw_test.completed"]
    assert events[-1].data["ok"] is False
