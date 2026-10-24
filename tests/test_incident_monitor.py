"""Incident monitor and detector swap-point tests."""

import asyncio
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

from ambient_home.ui import create_app
from ambient_home.costs import CostRates, CostTracker
from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import JobBoard
from ambient_home.jobs.devin import NullWorker
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.session_gate import GateState
from ambient_home.incidents.base import Incident
from ambient_home.telemetry.events import EventLog
from ambient_home.incidents.monitor import IncidentMonitor


class TinyDetector:
    """Small swappable detector used by monitor and API tests."""

    name = "tiny"

    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first

    async def detect(self, events, now):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("temporary detector error")
        if not any(event.type == "trigger" for event in events):
            return []
        return [
            Incident(
                fingerprint="tiny:incident",
                rule="tiny_rule",
                title="Tiny incident",
                severity="high",
                count=1,
                threshold=1,
                window_s=60.0,
                first_seen=events[0].ts,
                last_seen=events[0].ts,
                ready=True,
                samples=[events[0]],
                detector=self.name,
            )
        ]


async def test_monitor_emits_observed_and_ready_once(tmp_path) -> None:
    event_log = EventLog(tmp_path / "events")
    monitor = IncidentMonitor(TinyDetector(), event_log, lookback=timedelta(hours=1), debounce_s=0)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.01)
    event_log.emit("trigger", "test")
    await asyncio.sleep(0.05)
    event_log.emit("trigger", "test")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    events = event_log.read(datetime.now(timezone.utc) - timedelta(minutes=1))
    assert [event.type for event in events].count("incident.observed") == 1
    assert [event.type for event in events].count("incident.ready") == 1


async def test_factory_and_heartbeat_events_do_not_trigger_evaluation(tmp_path) -> None:
    event_log = EventLog(tmp_path / "events")
    detector = TinyDetector()
    monitor = IncidentMonitor(detector, event_log, lookback=timedelta(hours=1), debounce_s=0)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.02)
    initial_calls = detector.calls

    event_log.emit("incident.observed", "factory")
    event_log.emit("app.heartbeat", "app")
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert detector.calls == initial_calls


async def test_detector_exception_does_not_kill_monitor(tmp_path) -> None:
    event_log = EventLog(tmp_path / "events")
    detector = TinyDetector(fail_first=True)
    monitor = IncidentMonitor(detector, event_log, lookback=timedelta(hours=1), debounce_s=0)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.02)
    event_log.emit("trigger", "test")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert detector.calls >= 2
    assert monitor.latest


async def test_fake_detector_works_with_create_app_and_monitor(tmp_path) -> None:
    board = JobBoard(tmp_path / "jobs.json")
    service = JobService(board, NullWorker(), lambda text: asyncio.sleep(0))
    event_log = EventLog(tmp_path / "events")
    detector = TinyDetector()
    monitor = IncidentMonitor(detector, event_log, lookback=timedelta(hours=1), debounce_s=0)
    event_log.emit("trigger", "test")
    tracker = CostTracker(SpendLog(tmp_path / "spend.jsonl"), board, rates=CostRates(0.3, 2.0))
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        lambda: asyncio.sleep(0),
        tracker,
        event_log=event_log,
        monitor=monitor,
        detector=detector,
    )

    with TestClient(app) as client:
        response = client.get("/api/incidents")

    assert response.status_code == 200
    assert response.json()["detector"] == "tiny"
    assert response.json()["incidents"][0]["detector"] == "tiny"
