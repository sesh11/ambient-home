"""Small local status and control UI."""

from pathlib import Path
from collections.abc import Callable, Awaitable

from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import FileResponse

from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus


class _AnswerRequest(BaseModel):
    answer: str


def create_app(
    status_provider: Callable[[], LiveStatus],
    job_service: JobService,
    stop_callback: Callable[[], Awaitable[None]],
) -> FastAPI:
    """Create the loopback-only status and control application."""
    app = FastAPI(title="ambient-home")
    index_path = Path(__file__).parent / "ui" / "index.html"

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/api/status")
    async def status() -> dict[str, object]:
        live = status_provider()
        return {
            "live": {
                "state": live.state.value,
                "session_elapsed_s": live.session_elapsed_s,
                "seconds_used_today": live.seconds_used_today,
                "seconds_remaining_today": live.seconds_remaining_today,
                "last_close_reason": live.last_close_reason.value if live.last_close_reason else None,
            },
            "jobs": job_service.status_summary(),
        }

    @app.post("/api/stop")
    async def stop() -> dict[str, bool]:
        await stop_callback()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/answer")
    async def answer(job_id: str, request: _AnswerRequest) -> dict[str, object]:
        job = await job_service.answer(job_id, request.answer)
        if job is None:
            return {"error": "unknown job"}
        return {"job_id": job.id, "status": job.status.value}

    return app
