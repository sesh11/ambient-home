"""Garage request connector HTTP contract."""

import json
from datetime import datetime

import httpx
import pytest

from ambient_home.config import settings_from_env
from ambient_home.jobs.board import Job
from ambient_home.connectors.garage_requests import HttpRequestLog


@pytest.mark.asyncio
async def test_file_and_status_use_expected_paths_and_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/requests":
            return httpx.Response(200, json={"issue": {"number": 42, "url": "https://example.test/42"}})
        return httpx.Response(
            200,
            json={
                "number": 42,
                "dispatch": True,
                "state": "working",
                "session_url": "https://app.devin.ai/sessions/42",
                "pr_url": None,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        log = HttpRequestLog("http://garage.test/", frozenset({"owner/repo"}), client=client)
        job = Job(request="Fix the issue", repository="owner/repo")
        record = await log.file(job)
        status = await log.status(42)

    assert record is not None
    assert record.number == 42
    assert record.url == "https://example.test/42"
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/requests"
    payload = json.loads(requests[0].content)
    assert payload == {
        "id": job.id,
        "request": job.request,
        "created_at": job.created_at.isoformat(),
        "repository": job.repository,
    }
    assert datetime.fromisoformat(payload["created_at"]).utcoffset() is not None
    assert requests[1].method == "GET"
    assert requests[1].url.path == "/api/requests/42"
    assert status == {
        "number": 42,
        "dispatch": True,
        "state": "working",
        "session_url": "https://app.devin.ai/sessions/42",
        "pr_url": None,
    }
    assert "authorization" not in requests[0].headers
    assert "authorization" not in requests[1].headers


@pytest.mark.asyncio
async def test_bearer_token_is_sent_on_file_and_status_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/requests":
            return httpx.Response(200, json={"issue": None})
        return httpx.Response(200, json={"state": "queued"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        log = HttpRequestLog("http://garage.test", frozenset(), client=client, token="test-token")
        await log.file(Job(request="Fix the issue"))
        await log.status(42)

    assert len(requests) == 2
    assert all(request.headers["authorization"] == "Bearer test-token" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [409, 502])
async def test_status_raises_for_garage_errors(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "request could not be updated"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        log = HttpRequestLog("http://garage.test", frozenset(), client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await log.status(42)


def test_wants_is_repository_allowlisted() -> None:
    log = HttpRequestLog("http://garage.test", frozenset({"sesh11/ambient-home"}))
    for repository in (
        "sesh11/ambient-home",
        "SESH11/Ambient-Home",
        "https://github.com/sesh11/ambient-home.git",
        "https://github.com/sesh11/ambient-home/",
        "ambient-home",
    ):
        assert log.wants(repository)
    assert not log.wants("other/repo")
    assert not log.wants(None)
    assert not log.wants("")


@pytest.mark.asyncio
async def test_file_preserves_job_repository_value() -> None:
    repository = "https://github.com/sesh11/ambient-home.git"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"issue": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await HttpRequestLog("http://garage.test", frozenset(), client=client).file(
            Job(request="Fix issue", repository=repository)
        )

    payload = json.loads(requests[0].content)
    assert payload["repository"] == repository


def test_garage_token_is_optional_environment_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMBIENT_GARAGE_TOKEN", raising=False)
    monkeypatch.delenv("AMBIENT_GARAGE_URL", raising=False)
    settings = settings_from_env()
    assert settings.garage_url is None
    assert settings.garage_token is None
    monkeypatch.setenv("AMBIENT_GARAGE_TOKEN", "test-token")
    assert settings_from_env().garage_token == "test-token"
