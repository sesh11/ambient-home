"""Job service tests."""

import asyncio

import pytest

from ambient_home.jobs.board import Job, JobBoard, JobStatus
from ambient_home.jobs.service import JobService


class FakeWorker:
    name = "fake"

    async def start(self, job: Job) -> Job:
        job.status = JobStatus.working
        return job

    async def refresh(self, job: Job) -> Job:
        job.status = JobStatus.finished
        return job

    async def answer(self, job: Job, answer: str) -> None:
        job.status = JobStatus.working


@pytest.mark.asyncio
async def test_dispatch_returns_queued_then_starts(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), FakeWorker(), lambda text: asyncio.sleep(0))
    job = await service.dispatch("Fix the tests")
    assert job.status is JobStatus.queued
    await asyncio.sleep(0)
    assert service.board.get(job.id).status is JobStatus.working


@pytest.mark.asyncio
async def test_poller_announces_terminal_status_once(tmp_path) -> None:
    announcements: list[str] = []

    async def announce(text: str) -> None:
        announcements.append(text)

    service = JobService(JobBoard(tmp_path / "jobs.json"), FakeWorker(), announce)
    job = Job(request="Fix the failing tests", status=JobStatus.working)
    service.board.add(job)
    poller = asyncio.create_task(service.run_poller(0.01))
    await asyncio.sleep(0.04)
    poller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poller

    assert announcements == ["Job Fix the failing tests: finished"]
