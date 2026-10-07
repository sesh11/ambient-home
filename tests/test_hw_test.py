"""Hardware test harness tests."""

import json
import subprocess
from pathlib import Path
from collections.abc import Sequence

import httpx
import pytest

from ambient_home import hw_test
from ambient_home.hw_test import FAIL, PASS, SKIP, Check, Report, Harness


def _completed(
    args: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.failures = failures or {}

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), cwd))
        joined = " ".join(args)
        for needle, code in self.failures.items():
            if needle in joined:
                return _completed(args, code, stderr=f"{needle} broke")
        if args[:2] == ["git", "rev-parse"]:
            return _completed(args, stdout="0123456789abcdef\n")
        return _completed(args)


def test_fetch_spec_prefers_pr() -> None:
    assert hw_test.fetch_spec(None, 42) == "pull/42/head"
    assert hw_test.fetch_spec("feature/x", None) == "feature/x"
    with pytest.raises(ValueError):
        hw_test.fetch_spec(None, None)


def test_checkout_uses_detached_worktree(tmp_path: Path) -> None:
    runner = FakeRunner()
    harness = Harness(Report(ref="PR #7"), runner=runner)
    worktree = tmp_path / "hw" / "worktree"

    status, detail = hw_test.checkout(harness, tmp_path, worktree, "pull/7/head")

    assert status == PASS
    assert harness.report.commit == "0123456789abcdef"
    commands = [args for args, _ in runner.calls]
    assert ["git", "fetch", "--quiet", "origin", "pull/7/head"] in commands
    assert ["git", "worktree", "add", "--force", "--detach", str(worktree), "FETCH_HEAD"] in commands
    assert "0123456789ab" in detail


def test_checkout_reports_fetch_failure(tmp_path: Path) -> None:
    harness = Harness(Report(ref="x"), runner=FakeRunner({"fetch": 128}))

    status, detail = hw_test.checkout(harness, tmp_path, tmp_path / "wt", "missing")

    assert status == FAIL
    assert "fetch broke" in detail


def test_gate_stops_at_first_failure(tmp_path: Path) -> None:
    runner = FakeRunner({"mypy": 1})
    harness = Harness(Report(ref="x"), runner=runner)

    status, detail = hw_test.gate(harness, tmp_path)

    assert status == FAIL
    assert "mypy" in detail and "exited 1" in detail
    assert not any("pytest" in args for args, _ in runner.calls)


def test_step_records_exceptions_as_failures() -> None:
    harness = Harness(Report(ref="x"), runner=FakeRunner())

    def boom() -> tuple[str, str]:
        raise RuntimeError("no robot")

    check = harness.step("motion", boom)

    assert check.status == FAIL
    assert "RuntimeError: no robot" in check.detail
    assert harness.report.checks == [check]


def test_report_passes_with_skips_and_renders_markdown() -> None:
    report = Report(ref="PR #3", commit="abcdef0123456789", host="mini")
    report.checks = [Check("gate", PASS, "ok", 1.0), Check("mic", SKIP, "--skip-mic")]

    assert report.passed
    markdown = report.to_markdown()
    assert markdown.startswith("**ambient-hw-test PASS** — `PR #3` @ `abcdef012345` on `mini`")
    assert "| mic | skip (0.0s) | --skip-mic |" in markdown
    assert json.loads(report.to_json())["passed"] is True

    report.checks.append(Check("app", FAIL, "a|b"))
    assert not report.passed
    assert "a\\|b" in report.to_markdown()


class FakeDaemon:
    """Minimal stand-in for the Reachy daemon HTTP API."""

    def __init__(self, state: str = "running", track: float = 1.0) -> None:
        self.state = state
        self.track = track
        self.antennas = [0.0, 0.0]
        self.gotos: list[list[float]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/daemon/status":
            return httpx.Response(200, json={"state": self.state, "version": "1.10.0", "mockup_sim_enabled": True})
        if path == "/api/move/goto":
            target = json.loads(request.content)["antennas"]
            self.gotos.append(target)
            self.antennas = [value * self.track for value in target]
            return httpx.Response(200, json={"uuid": "00000000-0000-0000-0000-000000000000"})
        if path == "/api/move/running":
            return httpx.Response(200, json=[])
        if path == "/api/state/present_antenna_joint_positions":
            return httpx.Response(200, json=self.antennas)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_daemon_check_reports_mode() -> None:
    status, detail = hw_test.wait_for_daemon_running(FakeDaemon().client(), "http://d", timeout_s=0.0)

    assert status == PASS
    assert "mode=mockup-sim" in detail


def test_daemon_check_fails_when_not_running() -> None:
    status, detail = hw_test.wait_for_daemon_running(FakeDaemon(state="error").client(), "http://d", timeout_s=0.0)

    assert status == FAIL
    assert "state=error" in detail


def test_motion_smoke_moves_antennas_and_restores_start() -> None:
    daemon = FakeDaemon()
    daemon.antennas = [0.1, -0.1]

    status, _ = hw_test.motion_smoke(daemon.client(), "http://d")

    assert status == PASS
    assert daemon.gotos == [[hw_test.ANTENNA_TEST_RAD, -hw_test.ANTENNA_TEST_RAD], [0.1, -0.1]]


def test_motion_smoke_fails_when_antennas_do_not_track() -> None:
    status, detail = hw_test.motion_smoke(FakeDaemon(track=0.5).client(), "http://d")

    assert status == FAIL
    assert "max error 0.150" in detail


def test_app_environment_isolates_and_strips_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVIN_API_KEY", "cog_secret")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1")
    env_file = tmp_path / "test.env"
    env_file.write_text("OPENAI_API_KEY=sk-test\nAMBIENT_UI_PORT=1\n")

    env = hw_test.app_environment(env_file, tmp_path / "data", 8766, allow_dispatch=False)

    assert env["OPENAI_API_KEY"] == "sk-test"
    assert env["AMBIENT_UI_PORT"] == "8766"
    assert env["AMBIENT_UI_HOST"] == "127.0.0.1"
    assert env["AMBIENT_DATA_DIR"] == str(tmp_path / "data")
    assert "DEVIN_API_KEY" not in env and "DEVIN_ORG_ID" not in env

    kept = hw_test.app_environment(None, tmp_path / "data", 8766, allow_dispatch=True)
    assert kept["DEVIN_API_KEY"] == "cog_secret"


def test_paused_app_agent_is_noop_off_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hw_test.platform, "system", lambda: "Linux")

    with hw_test.paused_app_agent(tmp_path) as loaded:
        assert loaded is False


def test_paused_app_agent_restores_loaded_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _completed(["launchctl", *args])

    monkeypatch.setattr(hw_test.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hw_test, "launchctl", fake_launchctl)
    monkeypatch.setattr(hw_test, "domain", lambda: "gui/501")

    with pytest.raises(RuntimeError), hw_test.paused_app_agent(tmp_path):
        raise RuntimeError("test failed mid-run")

    plist = str(tmp_path / "Library" / "LaunchAgents" / "ai.ambient-home.app.plist")
    assert calls == [
        ("print", "gui/501/ai.ambient-home.app"),
        ("bootout", "gui/501/ai.ambient-home.app"),
        ("bootstrap", "gui/501", plist),
    ]
