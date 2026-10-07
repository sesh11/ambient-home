"""Hardware test harness: check out a ref and smoke-test it against the attached Reachy Mini.

Built for Devin Outposts sessions on the machine Reachy is plugged into, but
equally usable by hand. The live LaunchAgent checkout is never touched: the
ref under test is checked out into a separate git worktree, the app agent is
paused for the run and always restored, and the app runs with its own data
directory, UI port and env file. Devin credentials are stripped from the app
environment unless ``--allow-dispatch`` is given, so a test run cannot start
paid jobs.
"""

import os
import sys
import json
import time
import shlex
import shutil
import signal
import argparse
import platform
import subprocess
from typing import Any
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from dataclasses import field, asdict, dataclass
from collections.abc import Callable, Iterator, Sequence

import httpx
from dotenv import dotenv_values

from ambient_home.service import APP_LABEL, domain, launchctl


PASS = "pass"
FAIL = "fail"
SKIP = "skip"

DEFAULT_WORK_DIR = Path("~/.ambient-home/hw-test")
DEFAULT_ENV_FILE = Path("~/.config/ambient-home/hw-test.env")
DEFAULT_DAEMON_URL = "http://127.0.0.1:8000"
DEFAULT_UI_PORT = 8766
DISPATCH_ENV_VARS = ("DEVIN_API_KEY", "DEVIN_ORG_ID")
ANTENNA_TEST_RAD = 0.3
ANTENNA_TOLERANCE_RAD = 0.05
EXPECTED_APP_LOG_LINES = ("Reachy daemon ready", "Starting ambient-home")

Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass
class Check:
    """Outcome of one harness step."""

    name: str
    status: str
    detail: str = ""
    duration_s: float = 0.0


@dataclass
class Report:
    """Full harness result, written as JSON and printed as Markdown."""

    ref: str
    commit: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    host: str = field(default_factory=platform.node)
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether every non-skipped check passed."""
        return all(check.status != FAIL for check in self.checks)

    def to_json(self) -> str:
        """Serialize the report."""
        return json.dumps({**asdict(self), "passed": self.passed}, indent=2)

    def to_markdown(self) -> str:
        """Render a summary suitable for a PR comment or chat message."""
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"**ambient-hw-test {verdict}** — `{self.ref}` @ `{self.commit[:12] or 'unknown'}` on `{self.host}`",
            "",
            "| Check | Result | Detail |",
            "| --- | --- | --- |",
        ]
        for check in self.checks:
            detail = check.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {check.name} | {check.status} ({check.duration_s:.1f}s) | {detail} |")
        return "\n".join(lines)


def run_command(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing combined output."""
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


class Harness:
    """Runs the hardware test steps and collects a report."""

    def __init__(self, report: Report, runner: Runner = run_command, client: httpx.Client | None = None) -> None:
        """Create a harness around a report, command runner and HTTP client."""
        self.report = report
        self.runner = runner
        self.client = client or httpx.Client(timeout=5.0)

    def step(self, name: str, fn: Callable[[], tuple[str, str]]) -> Check:
        """Run ``fn`` as a named step, recording its status, detail and duration."""
        started = time.monotonic()
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        check = Check(name, status, detail, time.monotonic() - started)
        self.report.checks.append(check)
        print(f"[{check.status}] {name}: {detail}", flush=True)
        return check

    def skip(self, name: str, reason: str) -> Check:
        """Record a skipped step."""
        return self.step(name, lambda: (SKIP, reason))

    def run(self, args: Sequence[str], cwd: Path) -> tuple[str, str]:
        """Run a command and map its exit code to a step result."""
        result = self.runner(args, cwd)
        output = _tail(result.stdout + result.stderr)
        if result.returncode == 0:
            return PASS, shlex.join(args)
        return FAIL, f"`{shlex.join(args)}` exited {result.returncode}: {output}"


def fetch_spec(ref: str | None, pr: int | None) -> str:
    """Return the git fetch refspec for a PR number or ref."""
    if pr is not None:
        return f"pull/{pr}/head"
    if not ref:
        raise ValueError("either a ref or a PR number is required")
    return ref


def checkout(harness: Harness, repo_dir: Path, worktree: Path, spec: str) -> tuple[str, str]:
    """Fetch ``spec`` from origin and check it out detached into ``worktree``."""
    fetch = harness.runner(["git", "fetch", "--quiet", "origin", spec], repo_dir)
    if fetch.returncode != 0:
        return FAIL, f"git fetch origin {spec} failed: {_tail(fetch.stderr)}"
    if worktree.exists():
        harness.runner(["git", "worktree", "remove", "--force", str(worktree)], repo_dir)
        shutil.rmtree(worktree, ignore_errors=True)
    harness.runner(["git", "worktree", "prune"], repo_dir)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    add = harness.runner(["git", "worktree", "add", "--force", "--detach", str(worktree), "FETCH_HEAD"], repo_dir)
    if add.returncode != 0:
        return FAIL, f"git worktree add failed: {_tail(add.stderr)}"
    rev = harness.runner(["git", "rev-parse", "HEAD"], worktree)
    harness.report.commit = rev.stdout.strip()
    return PASS, f"{spec} -> {harness.report.commit[:12]} in {worktree}"


GATE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "ruff", "format", "--check", "."),
    ("uv", "run", "mypy", "--pretty", "--show-error-codes"),
    ("uv", "run", "pytest", "tests/", "-q"),
)


def gate(harness: Harness, worktree: Path) -> tuple[str, str]:
    """Run lint, format check, type check and unit tests without modifying files."""
    for command in GATE_COMMANDS:
        status, detail = harness.run(command, worktree)
        if status != PASS:
            return status, detail
    return PASS, "ruff, ruff format, mypy, pytest"


def wait_for_daemon_running(client: httpx.Client, daemon_url: str, timeout_s: float) -> tuple[str, str]:
    """Poll the Reachy daemon until it reports ``running``."""
    deadline = time.monotonic() + timeout_s
    state = "unreachable"
    while True:
        try:
            payload = client.get(f"{daemon_url}/api/daemon/status").json()
            state = str(payload.get("state")) if isinstance(payload, dict) else "invalid"
            if state == "running":
                mode = (
                    "mockup-sim"
                    if payload.get("mockup_sim_enabled")
                    else "sim"
                    if payload.get("simulation_enabled")
                    else "hardware"
                )
                return PASS, f"state=running mode={mode} version={payload.get('version')}"
        except (httpx.HTTPError, ValueError):
            state = "unreachable"
        if time.monotonic() >= deadline:
            return FAIL, f"daemon not running after {timeout_s:.0f}s (state={state}) at {daemon_url}"
        time.sleep(1.0)


def _wait_for_moves(client: httpx.Client, daemon_url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while client.get(f"{daemon_url}/api/move/running").json():
        if time.monotonic() >= deadline:
            raise TimeoutError("move did not finish")
        time.sleep(0.1)


def _antennas(client: httpx.Client, daemon_url: str) -> list[float]:
    positions = client.get(f"{daemon_url}/api/state/present_antenna_joint_positions").json()
    return [float(value) for value in positions]


def motion_smoke(client: httpx.Client, daemon_url: str, amplitude: float = ANTENNA_TEST_RAD) -> tuple[str, str]:
    """Wiggle the antennas only (no head or body motion) and verify the reported positions."""
    start = _antennas(client, daemon_url)
    target = [amplitude, -amplitude]
    try:
        response = client.post(f"{daemon_url}/api/move/goto", json={"antennas": target, "duration": 1.0})
        response.raise_for_status()
        _wait_for_moves(client, daemon_url, timeout_s=5.0)
        reached = _antennas(client, daemon_url)
    finally:
        client.post(f"{daemon_url}/api/move/goto", json={"antennas": start, "duration": 1.0})
        _wait_for_moves(client, daemon_url, timeout_s=5.0)
    error = max(abs(got - want) for got, want in zip(reached, target, strict=True))
    detail = f"antennas {_fmt(start)} -> {_fmt(reached)} (target {_fmt(target)}, max error {error:.3f} rad)"
    return (PASS if error <= ANTENNA_TOLERANCE_RAD else FAIL), detail


def _fmt(values: Sequence[float]) -> str:
    return "[" + ", ".join(f"{value:+.2f}" for value in values) + "]"


@contextmanager
def paused_app_agent(home: Path) -> Iterator[bool]:
    """Boot out the ambient-home app LaunchAgent for the duration of the block, then restore it.

    Yields whether the agent was loaded. Two copies of the app would compete for
    the microphone and UI port, so the live copy must be paused during a run.
    """
    if platform.system() != "Darwin":
        yield False
        return
    target = f"{domain()}/{APP_LABEL}"
    loaded = launchctl("print", target, check=False).returncode == 0
    if loaded:
        launchctl("bootout", target, check=False)
        print(f"paused {APP_LABEL}", flush=True)
    try:
        yield loaded
    finally:
        if loaded:
            plist = home / "Library" / "LaunchAgents" / f"{APP_LABEL}.plist"
            result = launchctl("bootstrap", domain(), str(plist), check=False)
            print(f"restored {APP_LABEL} (exit {result.returncode})", flush=True)


def app_environment(env_file: Path | None, data_dir: Path, ui_port: int, allow_dispatch: bool) -> dict[str, str]:
    """Build the environment for the app under test."""
    env = dict(os.environ)
    if env_file is not None and env_file.is_file():
        env.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
    env["AMBIENT_DATA_DIR"] = str(data_dir)
    env["AMBIENT_UI_HOST"] = "127.0.0.1"
    env["AMBIENT_UI_PORT"] = str(ui_port)
    if not allow_dispatch:
        for name in DISPATCH_ENV_VARS:
            env.pop(name, None)
    return env


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def app_smoke(
    client: httpx.Client,
    worktree: Path,
    env: dict[str, str],
    log_path: Path,
    timeout_s: float,
    wake_command: str | None = None,
) -> tuple[str, str]:
    """Start the app from the worktree, wait for its UI to report asleep, optionally wake it, then stop it."""
    ui = f"http://127.0.0.1:{env['AMBIENT_UI_PORT']}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.Popen(["uv", "run", "ambient-home"], cwd=worktree, env=env, stdout=log, stderr=log)
        try:
            state = _wait_for_live_state(client, ui, process, timeout_s, lambda s: s == "asleep")
            if state is None:
                return FAIL, f"UI never reported asleep within {timeout_s:.0f}s; {_app_log_tail(log_path)}"
            missing = [line for line in EXPECTED_APP_LOG_LINES if line not in log_path.read_text(errors="replace")]
            if missing:
                return FAIL, f"app log missing {missing}; {_app_log_tail(log_path)}"
            if wake_command:
                subprocess.run(wake_command, shell=True, cwd=worktree, check=False)
                woke = _wait_for_live_state(client, ui, process, 15.0, lambda s: s != "asleep")
                client.post(f"{ui}/api/stop")
                if woke is None:
                    return FAIL, f"wake command did not open a session: {_app_log_tail(log_path)}"
                return PASS, f"UI asleep, woke to {woke} after `{wake_command}`"
            return PASS, f"UI {ui} reported asleep; log has {list(EXPECTED_APP_LOG_LINES)}"
        finally:
            _stop_process(process)


def _wait_for_live_state(
    client: httpx.Client,
    ui: str,
    process: subprocess.Popen[bytes],
    timeout_s: float,
    accept: Callable[[str], bool],
) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        try:
            payload: Any = client.get(f"{ui}/api/status").json()
            state = str(payload["live"]["state"])
            if accept(state):
                return state
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        time.sleep(1.0)
    return None


def _app_log_tail(log_path: Path) -> str:
    return "app log tail: " + _tail(log_path.read_text(errors="replace"), 8)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(prog="ambient-hw-test", description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int, help="pull request number to test (fetches pull/<n>/head)")
    target.add_argument("--ref", help="branch, tag or commit to test")
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd(), help="ambient-home clone to fetch into")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR, help="worktree, logs and report location")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="env file for the app under test")
    parser.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL)
    parser.add_argument("--daemon-wait-s", type=float, default=60.0)
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
    parser.add_argument("--app-timeout-s", type=float, default=90.0)
    parser.add_argument("--skip-gate", action="store_true", help="skip ruff/mypy/pytest")
    parser.add_argument("--skip-mic", action="store_true", help="skip ambient-mic-check (e.g. on a --no-media daemon)")
    parser.add_argument("--skip-motion", action="store_true", help="do not move the antennas")
    parser.add_argument("--skip-app", action="store_true", help="do not start the app")
    parser.add_argument(
        "--wake-command",
        help="shell command that speaks the wake word near Reachy, e.g. \"say 'hey jarvis'\" (opens a paid Live session)",
    )
    parser.add_argument(
        "--allow-dispatch", action="store_true", help="keep DEVIN_* credentials in the app environment"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``ambient-hw-test``."""
    args = parse_args(argv)
    repo_dir = args.repo_dir.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    worktree = work_dir / "worktree"
    spec = fetch_spec(args.ref, args.pr)
    report = Report(ref=f"PR #{args.pr}" if args.pr is not None else spec)
    harness = Harness(report)

    if harness.step("checkout", lambda: checkout(harness, repo_dir, worktree, spec)).status == PASS:
        synced = harness.step("uv sync", lambda: harness.run(["uv", "sync"], worktree)).status == PASS
        if args.skip_gate:
            harness.skip("gate", "--skip-gate")
        elif synced:
            harness.step("gate", lambda: gate(harness, worktree))

        with paused_app_agent(Path.home()):
            daemon = harness.step(
                "daemon", lambda: wait_for_daemon_running(harness.client, args.daemon_url, args.daemon_wait_s)
            )
            if args.skip_mic:
                harness.skip("mic", "--skip-mic")
            elif synced:
                harness.step(
                    "mic", lambda: harness.run(["uv", "run", "ambient-mic-check", "--seconds", "2"], worktree)
                )
            if args.skip_motion:
                harness.skip("motion", "--skip-motion")
            elif daemon.status == PASS:
                harness.step("motion", lambda: motion_smoke(harness.client, args.daemon_url))
            if args.skip_app:
                harness.skip("app", "--skip-app")
            elif synced and daemon.status == PASS:
                data_dir = work_dir / "data"
                shutil.rmtree(data_dir, ignore_errors=True)
                env_file = args.env_file.expanduser()
                env = app_environment(env_file, data_dir, args.ui_port, args.allow_dispatch)
                harness.step(
                    "app",
                    lambda: app_smoke(
                        harness.client, worktree, env, work_dir / "app.log", args.app_timeout_s, args.wake_command
                    ),
                )

    report_path = work_dir / "report.json"
    work_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json(), encoding="utf-8")
    print()
    print(report.to_markdown())
    print(f"\nreport: {report_path}\napp log: {work_dir / 'app.log'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
