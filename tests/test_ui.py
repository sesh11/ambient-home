"""Local status UI tests."""

from fastapi.testclient import TestClient

from ambient_home.ui import create_app
from ambient_home.jobs.board import JobBoard
from ambient_home.jobs.devin import NullWorker
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.session_gate import GateState


def test_status_shape_and_stop_route(tmp_path) -> None:
    stopped = False

    async def stop() -> None:
        nonlocal stopped
        stopped = True

    service = JobService(JobBoard(tmp_path / "jobs.json"), NullWorker(), lambda text: stop())
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 3.0, 10.0, None),
        service,
        stop,
    )
    with TestClient(app) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        assert set(response.json()) == {"live", "jobs"}
        assert response.json()["live"]["state"] == "asleep"
        assert client.post("/api/stop").json() == {"ok": True}

    assert stopped
