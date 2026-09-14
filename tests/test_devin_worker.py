"""Devin v3 worker API tests."""

import json

import httpx
import pytest

from ambient_home.jobs.board import Job, JobStatus
from ambient_home.jobs.devin import DevinWorker


@pytest.mark.asyncio
async def test_start_posts_v3_request_with_repository() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"session_id": "session-1", "url": "https://app.devin.ai/s/session-1", "status": "new"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", max_acu=7, http=client)
        job = await worker.start(Job(request="Fix a bug", repository="owner/repo"))

    payload = json.loads(requests[0].content)
    assert job.status is JobStatus.working
    assert job.worker_session_id == "session-1"
    assert requests[0].url.path == "/v3/organizations/org-test/sessions"
    assert payload["repos"] == ["owner/repo"]
    assert "secret_ids" not in payload
    assert "idempotent" not in payload
    assert payload["max_acu_limit"] == 7
    assert "Never merge" in payload["prompt"]


@pytest.mark.asyncio
async def test_start_omits_repository_when_unset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"session_id": "session-1", "url": "https://app.devin.ai/s/session-1", "status": "new"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        await worker.start(Job(request="Fix a bug"))

    assert "repos" not in json.loads(requests[0].content)


@pytest.mark.asyncio
async def test_refresh_maps_running_working_without_fetching_messages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"session_id": "session-1", "status": "running", "status_detail": "working"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        refreshed = await worker.refresh(job)

    assert refreshed.status is JobStatus.working
    assert len(requests) == 1
    assert requests[0].url.path == "/v3/organizations/org-test/sessions/session-1"


@pytest.mark.asyncio
async def test_refresh_records_acus_consumed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "running",
                "status_detail": "working",
                "acus_consumed": 2.5,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        refreshed = await worker.refresh(job)

    assert refreshed.acus_consumed == 2.5


@pytest.mark.asyncio
async def test_refresh_blocked_fetches_last_devin_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"message": "Earlier question", "source": "devin", "created_at": "1", "event_id": "1"},
                        {"message": "User answer", "source": "user", "created_at": "2", "event_id": "2"},
                        {"message": "Latest question", "source": "devin", "created_at": "3", "event_id": "3"},
                    ],
                    "has_next_page": False,
                    "end_cursor": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "running",
                "status_detail": "waiting_for_user",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        blocked = await worker.refresh(job)

    assert blocked.status is JobStatus.blocked
    assert blocked.question == "Latest question"
    assert requests[1].url.path.endswith("/messages")
    assert requests[1].url.params["first"] == "100"


@pytest.mark.asyncio
async def test_refresh_exit_uses_last_pull_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "exit",
                "status_detail": "finished",
                "pull_requests": [
                    {"pr_url": "https://github.com/owner/repo/pull/1", "pr_state": "closed"},
                    {"pr_url": "https://github.com/owner/repo/pull/2", "pr_state": "open"},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        finished = await worker.refresh(job)

    assert finished.status is JobStatus.finished
    assert finished.pr_url == "https://github.com/owner/repo/pull/2"


@pytest.mark.asyncio
async def test_refresh_suspended_out_of_credits_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "suspended",
                "status_detail": "out_of_credits",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        failed = await worker.refresh(job)

    assert failed.status is JobStatus.failed
    assert failed.error == "Devin session suspended: out_of_credits"


@pytest.mark.asyncio
async def test_refresh_error_status_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "session_id": "session-1",
                "status": "error",
                "status_detail": "internal_error",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.working)
        failed = await worker.refresh(job)

    assert failed.status is JobStatus.failed
    assert failed.error == "Devin session error (internal_error)"


@pytest.mark.asyncio
async def test_start_http_error_marks_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="provider unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = await worker.start(Job(request="Fix a bug"))

    assert job.status is JobStatus.failed
    assert "HTTP 500" in (job.error or "")
    assert "provider unavailable" in (job.error or "")


@pytest.mark.asyncio
async def test_answer_posts_v3_message() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", http=client)
        job = Job(request="Fix a bug", worker_session_id="session-1", status=JobStatus.blocked)
        await worker.answer(job, "Use the release branch")

    assert requests[0].url.path == "/v3/organizations/org-test/sessions/session-1/messages"
    assert requests[0].content == b'{"message":"Use the release branch"}'
    assert job.status is JobStatus.working
