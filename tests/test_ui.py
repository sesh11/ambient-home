"""Local status UI tests."""

from garage_sdk import EventLog
from fastapi.testclient import TestClient

from ambient_home.ui import create_app
from ambient_home.costs import CostRates, CostTracker
from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import Job, JobBoard
from ambient_home.conversation import LiveStatus
from ambient_home.jobs.service import JobService
from ambient_home.session_gate import GateState
from ambient_home.connectors.coding.devin import NullWorker


async def noop_stop() -> None:
    return


def build_tracker(board: JobBoard, tmp_path) -> CostTracker:
    return CostTracker(
        SpendLog(tmp_path / "spend.jsonl"),
        board,
        rates=CostRates(live_usd_per_minute=0.3, acu_usd=2.0),
    )


def _app(tmp_path, *, devices=None, event_log=None):
    board = JobBoard(tmp_path / "jobs.json")
    service = JobService(board, NullWorker(), lambda text: noop_stop())
    return create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 3.0, 10.0, None),
        service,
        noop_stop,
        build_tracker(board, tmp_path),
        devices=devices,
        event_log=event_log,
    )


def test_status_stop_and_devices_routes(tmp_path) -> None:
    devices = [{"id": "desk", "kind": "fake", "state": "connected", "health": "ok", "owner": False}]
    with TestClient(_app(tmp_path, devices=lambda: devices)) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        assert set(response.json()) == {"live", "jobs", "devices"}
        assert response.json()["live"]["state"] == "asleep"
        assert response.json()["devices"] == devices
        assert client.get("/api/devices").json() == devices
        assert client.post("/api/stop").json() == {"ok": True}


def test_pages_serve_ui_and_root_redirects(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code in {302, 307}
        assert root.headers["location"] == "/jobs"
        for path in ("/jobs", "/costs"):
            page = client.get(path)
            assert page.status_code == 200
            assert 'href="#/costs"' in page.text
        assert client.get("/incidents").status_code == 404


def test_costs_route_reports_today_and_history(tmp_path) -> None:
    board = JobBoard(tmp_path / "jobs.json")
    board.add(Job(request="job", acus_consumed=9.0))
    service = JobService(board, NullWorker(), lambda text: noop_stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 3.0, 10.0, None),
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


def test_events_route_filters_and_rejects_bad_since(tmp_path) -> None:
    event_log = EventLog(tmp_path / "events")
    event_log.emit("mic.check", "mic", verdict="ok")
    event_log.emit("device.connected", "gateway")
    with TestClient(_app(tmp_path, event_log=event_log)) as client:
        response = client.get("/api/events?since=12h&type=mic.check&limit=1")
        invalid = client.get("/api/events?since=soon")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["events"][0]["type"] == "mic.check"
    assert invalid.status_code == 400


def test_answer_rejects_empty_input(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        response = client.post("/api/jobs/missing/answer", json={"answer": " \t\n"})
    assert response.status_code == 400
    assert response.json() == {"error": "answer is required"}
