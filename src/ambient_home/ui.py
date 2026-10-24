"""Small local status and control UI."""

from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Callable, Awaitable

from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from ambient_home.costs import CostTracker
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.incidents.base import Detector, parse_duration
from ambient_home.telemetry.events import EventLog
from ambient_home.incidents.monitor import IncidentMonitor


class _AnswerRequest(BaseModel):
    answer: str


def create_app(
    status_provider: Callable[[], LiveStatus],
    job_service: JobService,
    stop_callback: Callable[[], Awaitable[None]],
    cost_tracker: CostTracker,
    *,
    event_log: EventLog | None = None,
    monitor: IncidentMonitor | None = None,
    detector: Detector | None = None,
) -> FastAPI:
    """Create the loopback-only status and control application."""
    app = FastAPI(title="ambient-home")
    app.state.event_log = event_log
    app.state.monitor = monitor
    app.state.detector = detector
    index_path = Path(__file__).parent / "ui" / "index.html"

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/jobs")

    @app.get("/jobs")
    async def jobs_page() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/costs")
    async def costs_page() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/incidents")
    async def incidents_page() -> FileResponse:
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

    @app.get("/api/costs")
    async def costs() -> dict[str, object]:
        return cost_tracker.report(datetime.now().astimezone())

    @app.get("/api/events", response_model=None)
    async def events(
        since: str = "12h", type: str | None = None, limit: int = 500
    ) -> dict[str, object] | JSONResponse:
        now = datetime.now(timezone.utc)
        try:
            start = now - parse_duration(since)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if event_log is None:
            return {"since": start.isoformat(), "count": 0, "events": []}
        matched = event_log.read(start, now)
        if type is not None:
            matched = [event for event in matched if event.type == type]
        matched = sorted(matched, key=lambda event: event.ts, reverse=True)[: max(0, min(limit, 5000))]
        return {
            "since": start.isoformat(),
            "count": len(matched),
            "events": [event.model_dump(mode="json") for event in matched],
        }

    @app.get("/api/incidents", response_model=None)
    async def incidents(since: str = "24h") -> dict[str, object] | JSONResponse:
        now = datetime.now(timezone.utc)
        try:
            start = now - parse_duration(since)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        detector_name = detector.name if detector is not None else None
        found = []
        if event_log is not None and detector is not None:
            found = await detector.detect(event_log.read(start, now), now)
        return {
            "detector": detector_name,
            "generated_at": now.isoformat(),
            "incidents": [incident.model_dump(mode="json") for incident in found],
        }

    @app.post("/api/stop")
    async def stop() -> dict[str, bool]:
        await stop_callback()
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/answer", response_model=None)
    async def answer(job_id: str, request: _AnswerRequest) -> dict[str, object] | JSONResponse:
        answer_text = request.answer.strip()
        if not answer_text:
            return JSONResponse(status_code=400, content={"error": "answer is required"})
        job = await job_service.answer(job_id, answer_text)
        if job is None:
            return JSONResponse(status_code=404, content={"error": "unknown job"})
        return {"job_id": job.id, "status": job.status.value}

    return app
