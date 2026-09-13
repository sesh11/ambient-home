"""Devin worker API tests."""

import httpx
import pytest

from ambient_home.jobs.board import Job, JobStatus
from ambient_home.jobs.devin import DevinWorker


@pytest.mark.asyncio
async def test_start_builds_documented_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"session_id": "session-1", "url": "https://app.devin.ai/s/session-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", max_acu=7, http=client)
        job = await worker.start(Job(request="Fix a bug", repository="owner/repo"))

    payload = httpx.Request("POST", "https://example.invalid", content=requests[0].content).read()
    assert job.status is JobStatus.working
    assert job.worker_session_id == "session-1"
    assert requests[0].url.path == "/v1/sessions"
    assert b"Never merge" in payload
    assert b'"secret_ids":[]' in payload
    assert b'"max_acu_limit":7' in payload


@pytest.mark.asyncio
async def test_refresh_maps_blocked_and_finished() -> None:
    responses = [
        httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "Blocked",
                "status_enum": "blocked",
                "messages": [
                    {"type": "devin_message", "event_id": "1", "message": "Which branch?", "timestamp": "now"}
                ],
            },
        ),
        httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "Finished",
                "status_enum": "finished",
                "messages": [],
                "pull_request": {"url": "https://github.com/owner/repo/pull/1"},
            },
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", max_acu=5, http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        blocked = await worker.refresh(job)
        assert blocked.status is JobStatus.blocked
        assert blocked.question == "Which branch?"
        finished = await worker.refresh(blocked)

    assert finished.status is JobStatus.finished
    assert finished.pr_url == "https://github.com/owner/repo/pull/1"


@pytest.mark.asyncio
async def test_start_http_error_marks_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", max_acu=5, http=client)
        job = await worker.start(Job(request="Fix a bug"))

    assert job.status is JobStatus.failed
    assert "HTTP 500" in (job.error or "")
    assert "provider unavailable" in (job.error or "")


@pytest.mark.asyncio
async def test_answer_posts_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", max_acu=5, http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.blocked)
        await worker.answer(job, "Use the release branch")

    assert requests[0].url.path == "/v1/sessions/session-1/message"
    assert requests[0].content == b'{"message":"Use the release branch"}'
    assert job.status is JobStatus.working
