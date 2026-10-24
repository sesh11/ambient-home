"""Local status UI tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from ambient_home.ui import create_app
from ambient_home.costs import CostRates, CostTracker
from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import Job, JobBoard
from ambient_home.jobs.devin import NullWorker
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.session_gate import GateState
from ambient_home.incidents.rules import RuleDetector
from ambient_home.telemetry.events import EventLog


async def noop_stop() -> None:
    return


def build_tracker(board: JobBoard, tmp_path) -> CostTracker:
    return CostTracker(
        SpendLog(tmp_path / "spend.jsonl"),
        board,
        rates=CostRates(live_usd_per_minute=0.3, acu_usd=2.0),
    )


def test_status_shape_and_stop_route(tmp_path) -> None:
    stopped = False

    async def stop() -> None:
        nonlocal stopped
        stopped = True

    board = JobBoard(tmp_path / "jobs.json")
    service = JobService(board, NullWorker(), lambda text: stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 3.0, 10.0, None),
        service,
        stop,
        build_tracker(board, tmp_path),
    )
    with TestClient(app) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        assert set(response.json()) == {"live", "jobs"}
        assert response.json()["live"]["state"] == "asleep"
        assert client.post("/api/stop").json() == {"ok": True}

    assert stopped


def test_pages_serve_ui_and_root_redirects(tmp_path) -> None:
    board = JobBoard(tmp_path / "jobs.json")
    service = JobService(board, NullWorker(), lambda text: noop_stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        noop_stop,
        build_tracker(board, tmp_path),
    )

    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code in {302, 307}
        assert root.headers["location"] == "/jobs"
        for path in ("/jobs", "/costs"):
            page = client.get(path)
            assert page.status_code == 200
            assert 'href="#/costs"' in page.text


def test_costs_route_reports_today_and_history(tmp_path) -> None:
    board = JobBoard(tmp_path / "jobs.json")
    board.add(Job(request="job", acus_consumed=9.0))
    service = JobService(board, NullWorker(), lambda text: noop_stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 600.0, None),
        service,
        noop_stop,
        build_tracker(board, tmp_path),
    )

    with TestClient(app) as client:
        payload = client.get("/api/costs").json()

    assert payload["today"]["acus"] == 9.0
    assert payload["today"]["usd"] == 18.0
    assert payload["window_usd"] == 18.0
    assert len(payload["history"]) == 7


def test_answer_rejects_empty_input_without_calling_service(tmp_path, monkeypatch) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), NullWorker(), lambda text: noop_stop())
    called = False

    async def answer(job_id: str, answer_text: str) -> Job | None:
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(service, "answer", answer)
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        noop_stop,
        build_tracker(service.board, tmp_path),
    )

    with TestClient(app) as client:
        response = client.post("/api/jobs/missing/answer", json={"answer": " \t\n"})

    assert response.status_code == 400
    assert response.json() == {"error": "answer is required"}
    assert not called


def test_answer_unknown_job_returns_not_found(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), NullWorker(), lambda text: noop_stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        noop_stop,
        build_tracker(service.board, tmp_path),
    )

    with TestClient(app) as client:
        response = client.post("/api/jobs/missing/answer", json={"answer": "No"})

    assert response.status_code == 404
    assert response.json() == {"error": "unknown job"}


def test_incidents_page_serves_ui(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), NullWorker(), lambda text: noop_stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        noop_stop,
        build_tracker(service.board, tmp_path),
    )

    with TestClient(app) as client:
        response = client.get("/incidents")

    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower()


def test_events_route_filters_since_type_and_limit_and_rejects_bad_since(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), NullWorker(), lambda text: noop_stop())
    event_log = EventLog(tmp_path / "events")
    event_log.emit("mic.check", "mic", verdict="ok")
    event_log.emit("daemon.ready", "daemon")
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        noop_stop,
        build_tracker(service.board, tmp_path),
        event_log=event_log,
    )

    with TestClient(app) as client:
        response = client.get("/api/events?since=12h&type=mic.check&limit=1")
        invalid = client.get("/api/events?since=soon")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert len(payload["events"]) == 1
    assert payload["events"][0]["type"] == "mic.check"
    assert invalid.status_code == 400
    assert "error" in invalid.json()


def test_incidents_route_detects_from_event_log(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), NullWorker(), lambda text: noop_stop())
    event_log = EventLog(tmp_path / "events")
    for _ in range(3):
        event_log.emit("mic.check", "mic", verdict="silent")
    rules_path = Path(__file__).parents[1] / "src" / "ambient_home" / "incident_rules.toml"
    detector = RuleDetector.from_file(rules_path)
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        service,
        noop_stop,
        build_tracker(service.board, tmp_path),
        event_log=event_log,
        detector=detector,
    )

    with TestClient(app) as client:
        response = client.get("/api/incidents?since=24h")
        invalid = client.get("/api/incidents?since=soon")

    assert response.status_code == 200
    assert response.json()["detector"] == "rules"
    incident = response.json()["incidents"][0]
    assert incident["rule"] == "mic_all_zero"
    assert incident["count"] == 3
    assert incident["ready"]
    assert invalid.status_code == 400
