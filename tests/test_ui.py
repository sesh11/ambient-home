"""Local status UI tests."""

from fastapi.testclient import TestClient

from ambient_home.ui import create_app
from ambient_home.costs import CostCaps, CostRates, CostTracker
from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import Job, JobBoard
from ambient_home.jobs.devin import NullWorker
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.session_gate import GateState


async def noop_stop() -> None:
    return


def build_tracker(board: JobBoard, tmp_path) -> CostTracker:
    return CostTracker(
        SpendLog(tmp_path / "spend.jsonl"),
        board,
        rates=CostRates(live_usd_per_minute=0.3, acu_usd=2.0),
        caps=CostCaps(live_daily_s=600.0, acu_monthly=10.0),
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


def test_costs_route_reports_caps_and_history(tmp_path) -> None:
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

    caps = {item["kind"]: item for item in payload["caps"]}
    assert caps["acu_monthly"]["used"] == 9.0
    assert caps["acu_monthly"]["usd"] == 18.0
    assert payload["alerts"] == ["Worker ACUs this month is at 90% of its cap of 10 ACU."]
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
