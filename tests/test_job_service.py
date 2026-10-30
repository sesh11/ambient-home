"""Job service tests."""

import asyncio

import pytest

from ambient_home.jobs.board import Job, JobBoard, JobStatus
from ambient_home.jobs.service import JobService, RequestRecord, RequestNotFiled


class FakeWorker:
    name = "devin"

    def __init__(self) -> None:
        self.refresh_calls: list[str] = []
        self.answer_calls: list[str] = []

    async def start(self, job: Job) -> Job:
        job.status = JobStatus.working
        return job

    async def refresh(self, job: Job) -> Job:
        self.refresh_calls.append(job.id)
        job.status = JobStatus.finished
        return job

    async def answer(self, job: Job, answer: str) -> None:
        self.answer_calls.append(job.id)
        job.status = JobStatus.working


class FakeRequestLog:
    def __init__(
        self,
        record: RequestRecord | None = None,
        *,
        covered: bool = True,
        error: Exception | None = None,
        status: dict[str, object] | None = None,
    ) -> None:
        self.record = record
        self.covered = covered
        self.error = error
        self.status_result = status or {"state": "queued", "session_url": None, "pr_url": None}
        self.wants_calls: list[str | None] = []
        self.filed_jobs: list[Job] = []
        self.status_calls: list[int] = []

    def wants(self, repository: str | None) -> bool:
        self.wants_calls.append(repository)
        return self.covered

    async def file(self, job: Job) -> RequestRecord | None:
        self.filed_jobs.append(job)
        if self.error is not None:
            raise self.error
        return self.record

    async def status(self, number: int) -> dict[str, object]:
        self.status_calls.append(number)
        return self.status_result


@pytest.mark.asyncio
async def test_dispatch_returns_queued_then_starts(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), FakeWorker(), lambda text: asyncio.sleep(0))
    job = await service.dispatch("Fix the tests")
    assert job.status is JobStatus.queued
    await asyncio.sleep(0)
    started = service.board.get(job.id)
    assert started is not None
    assert started.status is JobStatus.working


@pytest.mark.asyncio
async def test_direct_dispatch_does_not_file_a_garage_request(tmp_path) -> None:
    request_log = FakeRequestLog(RequestRecord(number=42, url="https://example.test/42"))
    service = JobService(
        JobBoard(tmp_path / "jobs.json"),
        FakeWorker(),
        lambda text: asyncio.sleep(0),
        request_log=request_log,
    )

    job = await service.dispatch("Start a direct session", "sesh11/ambient-home")
    await asyncio.sleep(0)

    started = service.board.get(job.id)
    assert started is not None
    assert started.status is JobStatus.working
    assert request_log.wants_calls == []
    assert request_log.filed_jobs == []


@pytest.mark.asyncio
async def test_file_request_adds_garage_job_after_success(tmp_path) -> None:
    request_log = FakeRequestLog(RequestRecord(number=42, url="https://example.test/42"))
    service = JobService(
        JobBoard(tmp_path / "jobs.json"),
        FakeWorker(),
        lambda text: asyncio.sleep(0),
        request_log=request_log,
    )

    job = await service.file_request("Add the feature", "sesh11/ambient-home")

    assert job.worker == "garage"
    assert job.status is JobStatus.queued
    assert job.issue_number == 42
    assert job.issue_url == "https://example.test/42"
    assert request_log.filed_jobs == [job]
    assert service.board.get(job.id) == job


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_log", "reason"),
    [
        (None, "not_configured"),
        (FakeRequestLog(covered=False), "not_covered"),
        (FakeRequestLog(), "declined"),
        (FakeRequestLog(error=RuntimeError("private filing detail")), "failed"),
    ],
)
async def test_file_request_failures_are_not_added_to_board(
    tmp_path, request_log, reason, caplog, monkeypatch
) -> None:
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        "ambient_home.jobs.service.emit",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    board = JobBoard(tmp_path / "jobs.json")
    service = JobService(board, FakeWorker(), lambda text: asyncio.sleep(0), request_log=request_log)

    with pytest.raises(RequestNotFiled) as error:
        await service.file_request("Sensitive request", "sesh11/ambient-home")

    assert error.value.reason == reason
    assert board.all() == []
    if reason == "failed":
        assert request_log is not None
        assert "RuntimeError" in caplog.text
        assert "private filing detail" not in caplog.text
        assert len(events) == 1
        assert events[0][0] == ("factory.report_failed", "factory", "error")
        assert events[0][1] == {
            "op": "file",
            "job_id": request_log.filed_jobs[0].id,
            "exc_type": "RuntimeError",
        }
    else:
        assert events == []


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


@pytest.mark.asyncio
async def test_poller_refreshes_garage_job_and_announces_pr_without_worker_refresh(tmp_path) -> None:
    class FailingFirstRequestLog(FakeRequestLog):
        async def status(self, number: int) -> dict[str, object]:
            self.status_calls.append(number)
            if number == 41:
                raise RuntimeError("private status detail")
            return self.status_result

    announcements: list[str] = []
    announced = asyncio.Event()

    async def announce(text: str) -> None:
        announcements.append(text)
        announced.set()

    worker = FakeWorker()
    request_log = FailingFirstRequestLog(
        status={
            "state": "finished",
            "session_url": "https://app.devin.ai/sessions/garage-42",
            "pr_url": "https://github.com/sesh11/ambient-home/pull/42",
        }
    )
    service = JobService(
        JobBoard(tmp_path / "jobs.json"),
        worker,
        announce,
        request_log=request_log,
    )
    failed_to_refresh = Job(request="Another issue", worker="garage", issue_number=41)
    job = Job(request="Fix issue", worker="garage", status=JobStatus.queued, issue_number=42)
    service.board.add(failed_to_refresh)
    service.board.add(job)
    poller = asyncio.create_task(service.run_poller(3600))
    try:
        await asyncio.wait_for(announced.wait(), timeout=1)
    finally:
        poller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poller

    refreshed = service.board.get(job.id)
    assert refreshed is not None
    assert refreshed.status is JobStatus.finished
    assert refreshed.worker_url == "https://app.devin.ai/sessions/garage-42"
    assert refreshed.pr_url == "https://github.com/sesh11/ambient-home/pull/42"
    remains_queued = service.board.get(failed_to_refresh.id)
    assert remains_queued is not None
    assert remains_queued.status is JobStatus.queued
    assert request_log.status_calls == [41, 42]
    assert worker.refresh_calls == []
    assert announcements == ["Job Fix issue: finished, PR is up at https://github.com/sesh11/ambient-home/pull/42"]


@pytest.mark.asyncio
async def test_answering_garage_job_does_not_call_worker(tmp_path) -> None:
    worker = FakeWorker()
    service = JobService(JobBoard(tmp_path / "jobs.json"), worker, lambda text: asyncio.sleep(0))
    job = Job(request="Fix issue", worker="garage", status=JobStatus.blocked)
    service.board.add(job)

    answered = await service.answer(job.id, "Please continue")

    assert answered is not None
    assert answered.error == "This request is handled by Garage; answer in its Devin session or GitHub issue."
    assert worker.answer_calls == []


def test_status_summary_includes_worker_and_worker_url(tmp_path) -> None:
    service = JobService(JobBoard(tmp_path / "jobs.json"), FakeWorker(), lambda text: asyncio.sleep(0))
    job = Job(request="Fix issue", worker="garage", worker_url="https://app.devin.ai/sessions/42")
    service.board.add(job)

    assert service.status_summary()[0]["worker"] == "garage"
    assert service.status_summary()[0]["worker_url"] == "https://app.devin.ai/sessions/42"
