"""Two-minute hardware check for the Reachy Mini Lite: ``uv run ambient-hw-test``."""

import os
import sys
import json
import time
import shutil
import asyncio
import argparse
import platform
import subprocess
from types import TracebackType
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from collections.abc import Callable, Sequence

import httpx
import numpy as np
from dotenv import load_dotenv

from ambient_home.config import AmbientSettings, settings_from_env
from ambient_home.daemon import READY_STATE, fetch_daemon_state
from ambient_home.service import APP_LABEL, domain, launchctl
from ambient_home.telemetry import EventLog, scrub_text


PASS = "pass"
FAIL = "fail"
SKIP = "skip"

_HEAD_NOD_DEG = 10.0
_HEAD_MIN_MOVE_DEG = 6.0


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one hardware check."""

    name: str
    status: str
    detail: str
    seconds: float

    @property
    def ok(self) -> bool:
        """Return whether the check did not fail."""
        return self.status != FAIL


class CheckFailed(Exception):
    """Raised by a check to fail with a readable detail."""


CheckFn = Callable[[], str]


@dataclass(frozen=True)
class Check:
    """A named hardware check; ``needs`` lists checks that must pass first."""

    name: str
    run: CheckFn
    needs: tuple[str, ...] = ()


def run_checks(checks: Sequence[Check], clock: Callable[[], float] = time.monotonic) -> list[CheckResult]:
    """Run checks in order, skipping any whose prerequisites did not pass."""
    results: list[CheckResult] = []
    status_by_name: dict[str, str] = {}
    for check in checks:
        blocked = [name for name in check.needs if status_by_name.get(name) != PASS]
        if blocked:
            result = CheckResult(check.name, SKIP, f"needs {', '.join(blocked)}", 0.0)
        else:
            started = clock()
            try:
                detail = check.run()
                status = PASS
            except CheckFailed as exc:
                detail, status = str(exc), FAIL
            except Exception as exc:
                detail, status = f"{type(exc).__name__}: {exc}", FAIL
            result = CheckResult(check.name, status, scrub_text(detail), clock() - started)
        status_by_name[check.name] = result.status
        results.append(result)
    return results


class PausedApp:
    """Stop the always-on app LaunchAgent for the test and always start it again."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        plist: Path | None = None,
        run: Callable[..., subprocess.CompletedProcess[str]] = launchctl,
        system: Callable[[], str] = platform.system,
    ) -> None:
        """Configure which agent to pause."""
        self.enabled = enabled
        self.plist = plist or Path("~/Library/LaunchAgents").expanduser() / f"{APP_LABEL}.plist"
        self._run = run
        self._system = system
        self.paused = False
        self.restored: bool | None = None

    def __enter__(self) -> "PausedApp":
        """Boot out the app agent if it is loaded."""
        if not self.enabled or self._system() != "Darwin":
            return self
        target = f"{domain()}/{APP_LABEL}"
        if self._run("print", target, check=False).returncode != 0:
            return self
        result = self._run("bootout", target, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"could not stop {APP_LABEL}: {result.stderr.strip() or result.stdout.strip()}")
        self.paused = True
        time.sleep(1.0)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Bootstrap the app agent again if this context stopped it."""
        if not self.paused:
            return
        result = self._run("bootstrap", domain(), str(self.plist), check=False)
        self.restored = result.returncode == 0
        if not self.restored:
            print(
                f"warning: could not restart {APP_LABEL}: {result.stderr.strip() or result.stdout.strip()}\n"
                f"run: launchctl bootstrap {domain()} {self.plist}",
                file=sys.stderr,
            )


def wake_phrase(wake_word: str) -> str:
    """Turn a wake-word model name such as ``hey_alfred`` into a spoken phrase."""
    return Path(wake_word).stem.replace("_", " ").strip()


def head_pitch_deg(pose: np.ndarray) -> float:
    """Return the pitch of a 4x4 head pose in degrees."""
    rotation = np.asarray(pose, dtype=np.float64)[:3, :3]
    return float(np.degrees(np.arcsin(-np.clip(rotation[2, 0], -1.0, 1.0))))


def settled_pitch_deg(
    read_pose: Callable[[], np.ndarray],
    *,
    timeout_s: float = 1.5,
    poll_s: float = 0.1,
    tolerance_deg: float = 0.2,
) -> float:
    """Read the head pitch until two readings agree, so a lagging pose isn't measured mid-move."""
    deadline = time.monotonic() + timeout_s
    previous = head_pitch_deg(read_pose())
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        current = head_pitch_deg(read_pose())
        if abs(current - previous) <= tolerance_deg:
            return current
        previous = current
    return previous


def check_daemon(settings: AmbientSettings) -> str:
    """Pass when the Reachy daemon reports it is running."""

    async def fetch() -> str | None:
        async with httpx.AsyncClient() as client:
            return await fetch_daemon_state(client, settings.daemon_status_url)

    state = asyncio.run(fetch())
    if state != READY_STATE:
        raise CheckFailed(f"daemon state={state or 'unreachable'} at {settings.daemon_status_url}")
    return f"state={state}"


def check_audio_device() -> str:
    """Pass when the Reachy audio board answers with its firmware version."""
    from ambient_home.mic_check import read_firmware_version

    version = read_firmware_version()
    if not version[:1].isdigit():
        raise CheckFailed(version)
    return f"firmware {version}"


def check_microphone(seconds: float) -> str:
    """Pass when a short recording has real sound, rebooting the XMOS chip once if it is all zeros like the app does."""
    from ambient_home.mic_check import probe_microphone, restart_audio_processor

    probe = probe_microphone(seconds)
    detail = f"verdict={probe.verdict} frames={probe.frames} peak={probe.peak:.4f}"
    if probe.verdict == "silent" and probe.frames > 0:
        if not restart_audio_processor():
            raise CheckFailed(f"{detail}; audio board not found for reboot")
        probe = probe_microphone(seconds)
        detail = f"silent until XMOS reboot, then verdict={probe.verdict} frames={probe.frames} peak={probe.peak:.4f}"
    if probe.verdict != "ok":
        raise CheckFailed(f"{detail}; check the microphone cable")
    return detail


def check_wake_word(settings: AmbientSettings, timeout_s: float, speak: bool) -> str:
    """Say the wake word through the Mac speaker and wait for the local detector to fire."""
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    from ambient_home.audio import frame_to_mono_int16
    from ambient_home.wake_word import OpenWakeWordDetector

    detector = OpenWakeWordDetector(settings.wake_word, settings.wake_threshold)
    try:
        detector.feed(np.zeros(1280, dtype=np.int16))
    except Exception as exc:
        raise CheckFailed(
            f"wake model {settings.wake_word} did not load ({type(exc).__name__}); "
            "a fresh venv may need openwakeword.utils.download_models()"
        ) from exc
    detector.reset()
    phrase = wake_phrase(settings.wake_word)
    audio = GStreamerAudio()
    audio.start_recording()
    speaker: subprocess.Popen[bytes] | None = None
    try:
        settle_until = time.monotonic() + 0.5
        while time.monotonic() < settle_until:
            audio.get_audio_sample()
            time.sleep(0.01)
        if speak:
            say = shutil.which("say")
            if say is None:
                raise CheckFailed("macOS `say` not found; rerun with --no-speak and say the wake word yourself")
            speaker = subprocess.Popen([say, phrase])
        else:
            print(f"say '{phrase}' now (waiting {timeout_s:.0f} s)")
        deadline = time.monotonic() + timeout_s
        best = 0.0
        while time.monotonic() < deadline:
            frame = audio.get_audio_sample()
            if frame is None:
                time.sleep(0.01)
                continue
            if detector.feed(frame_to_mono_int16(frame)):
                return f"'{phrase}' detected, score={detector.last_score:.2f}"
            best = max(best, detector.take_peak_score())
        raise CheckFailed(
            f"'{phrase}' not detected in {timeout_s:.0f} s (best score {best:.2f}, threshold {settings.wake_threshold})"
        )
    finally:
        audio.stop_recording()
        if speaker is not None and speaker.poll() is None:
            speaker.terminate()


def check_head() -> str:
    """Nod the head a little and read the pose back to confirm it moved."""
    from reachy_mini import ReachyMini
    from reachy_mini.utils import create_head_pose

    with ReachyMini(media_backend="no_media") as robot:
        robot.enable_motors()
        start = robot.get_current_head_pose()
        start_pitch = head_pitch_deg(start)
        nod = np.asarray(start, dtype=np.float64) @ create_head_pose(pitch=_HEAD_NOD_DEG, degrees=True)
        robot.goto_target(head=nod, duration=0.8, body_yaw=None)
        moved = settled_pitch_deg(robot.get_current_head_pose) - start_pitch
        robot.goto_target(head=start, duration=0.8, body_yaw=None)
    if abs(moved) < _HEAD_MIN_MOVE_DEG:
        raise CheckFailed(f"head pitch changed {moved:+.1f} deg (asked for {_HEAD_NOD_DEG:+.0f})")
    return f"head pitch moved {moved:+.1f} deg (asked for {_HEAD_NOD_DEG:+.0f}) and returned"


def check_telemetry(event_log: EventLog, run_id: str) -> str:
    """Write an event to the events directory and read it back."""
    written = event_log.emit("hw_test.probe", "hw_test", run_id=run_id)
    if written is None:
        raise CheckFailed(f"could not write to {event_log.directory}")
    found = [
        event
        for event in event_log.read(written.ts)
        if event.type == "hw_test.probe" and event.data.get("run_id") == run_id
    ]
    if not found:
        raise CheckFailed(f"event written but not read back from {event_log.directory}")
    return f"event written and read back in {event_log.directory}"


def _app_command() -> list[str]:
    return [sys.executable, "-c", "from ambient_home.main import main; main()"]


def check_live(
    settings: AmbientSettings,
    event_log: EventLog,
    timeout_s: float,
    speak: bool,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> str:
    """Run the real app briefly, wake it, and wait for a paid Live session to open."""
    started_at = datetime.now(timezone.utc)
    env = {
        **os.environ,
        "AMBIENT_EVENTS_DIR": str(event_log.directory),
        "AMBIENT_IDLE_TIMEOUT_S": "8",
        "AMBIENT_MAX_SESSION_S": "20",
        "AMBIENT_HEARTBEAT_S": "30",
    }
    process = popen(_app_command(), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    phrase = wake_phrase(settings.wake_word)

    def seen(event_type: str) -> bool:
        return any(event.type == event_type for event in event_log.read(started_at))

    try:
        deadline = time.monotonic() + timeout_s
        while not seen("app.started"):
            if process.poll() is not None:
                raise CheckFailed(f"app exited early with code {process.returncode}")
            if time.monotonic() > deadline:
                raise CheckFailed("app did not start in time")
            time.sleep(0.5)
        time.sleep(3.0)
        if speak and shutil.which("say"):
            subprocess.run(["say", phrase], check=False)
        else:
            print(f"say '{phrase}' now")
        while not seen("live.session_started"):
            if process.poll() is not None:
                raise CheckFailed(f"app exited with code {process.returncode} before a Live session opened")
            if time.monotonic() > deadline:
                raise CheckFailed("no Live session opened before the timeout")
            time.sleep(0.5)
        detail = "Live session opened"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    dispatched = [
        event
        for event in event_log.read(started_at)
        if event.type == "tool.completed" and event.data.get("tool") == "dispatch_job"
    ]
    if dispatched:
        detail += f"; WARNING {len(dispatched)} dispatch_job call(s) during the test"
    return detail


def build_checks(
    settings: AmbientSettings,
    event_log: EventLog,
    run_id: str,
    *,
    mic_seconds: float,
    wake_timeout_s: float,
    speak: bool,
    live: bool,
    live_timeout_s: float,
) -> list[Check]:
    """Return the hardware checks in the order they run."""
    checks = [
        Check("telemetry", lambda: check_telemetry(event_log, run_id)),
        Check("daemon", lambda: check_daemon(settings)),
        Check("audio device", check_audio_device),
        Check("microphone", lambda: check_microphone(mic_seconds), needs=("audio device",)),
        Check("wake word", lambda: check_wake_word(settings, wake_timeout_s, speak), needs=("microphone",)),
        Check("head", check_head, needs=("daemon",)),
    ]
    if live:
        checks.append(
            Check(
                "live session",
                lambda: check_live(settings, event_log, live_timeout_s, speak),
                needs=("daemon", "wake word"),
            )
        )
    return checks


def format_table(results: Sequence[CheckResult]) -> str:
    """Render results as a plain-text table for the terminal."""
    width = max((len(result.name) for result in results), default=5)
    lines = [f"{'check':<{width}}  result  time   detail"]
    for result in results:
        lines.append(f"{result.name:<{width}}  {result.status.upper():<6}  {result.seconds:4.1f}s  {result.detail}")
    return "\n".join(lines)


def format_markdown(results: Sequence[CheckResult], *, total_s: float, app_restored: bool | None) -> str:
    """Render a scrubbed Markdown summary suitable for a PR description."""
    passed = all(result.ok for result in results)
    lines = [
        f"### `ambient-hw-test`: {'PASS' if passed else 'FAIL'} ({total_s:.0f} s)",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for result in results:
        detail = result.detail.replace("|", "\\|")
        lines.append(f"| {result.name} | {result.status.upper()} | {detail} |")
    if app_restored is not None:
        lines += ["", f"Always-on app restarted afterwards: {'yes' if app_restored else 'NO'}"]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the hardware checks and print a pass/fail table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also open one paid OpenAI Live session")
    parser.add_argument("--no-pause", action="store_true", help="don't stop the always-on app LaunchAgent")
    parser.add_argument("--no-speak", action="store_true", help="don't use `say`; you speak the wake word")
    parser.add_argument("--mic-seconds", type=float, default=2.0)
    parser.add_argument("--wake-timeout", type=float, default=10.0)
    parser.add_argument("--live-timeout", type=float, default=60.0)
    parser.add_argument("--markdown", type=Path, default=None, help="write a scrubbed Markdown summary here")
    parser.add_argument("--json", type=Path, default=None, help="write machine-readable results here")
    args = parser.parse_args(argv)

    load_dotenv()
    settings = settings_from_env()
    event_log = EventLog(settings.events_dir, retention_days=settings.event_retention_days)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = time.monotonic()
    checks = build_checks(
        settings,
        event_log,
        run_id,
        mic_seconds=args.mic_seconds,
        wake_timeout_s=args.wake_timeout,
        speak=not args.no_speak,
        live=args.live,
        live_timeout_s=args.live_timeout,
    )
    pause = PausedApp(enabled=not args.no_pause)
    try:
        with pause:
            if pause.paused:
                print(f"paused {APP_LABEL}; it will be restarted when the test ends")
            results = run_checks(checks)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    total_s = time.monotonic() - started
    passed = all(result.ok for result in results) and pause.restored is not False
    for result in results:
        event_log.emit(
            "hw_test.check",
            "hw_test",
            "info" if result.ok else "error",
            run_id=run_id,
            check=result.name,
            status=result.status,
            seconds=round(result.seconds, 2),
        )
    event_log.emit("hw_test.completed", "hw_test", run_id=run_id, ok=passed, seconds=round(total_s, 1))

    print(format_table(results))
    print(f"\n{'PASS' if passed else 'FAIL'} in {total_s:.0f} s")
    if args.markdown is not None:
        args.markdown.write_text(format_markdown(results, total_s=total_s, app_restored=pause.restored))
    if args.json is not None:
        payload = {
            "ok": passed,
            "run_id": run_id,
            "seconds": round(total_s, 1),
            "app_restored": pause.restored,
            "checks": [asdict(result) for result in results],
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
