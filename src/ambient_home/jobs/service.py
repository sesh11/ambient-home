"""Application service for dispatching and announcing background jobs."""

import asyncio
import logging
from typing import Protocol
from collections.abc import Callable, Awaitable

from ambient_home.telemetry import emit
from ambient_home.jobs.board import Job, JobBoard, JobStatus
from ambient_home.jobs.worker import Worker
from ambient_home.factory.github import IssueRecord


logger = logging.getLogger(__name__)
Announcer = Callable[[str], Awaitable[None]]


class RequestLog(Protocol):
    """Records voice requests somewhere durable (a GitHub issue) before work starts."""

    def wants(self, repository: str | None) -> bool:
        """Return whether requests for this repository should be recorded."""
        ...

    async def file(self, job: Job) -> IssueRecord | None:
        """Record the request and return the created issue, if any."""
        ...

    async def mark_fixing(self, number: int) -> None:
        """Mark the recorded request as being worked on."""
        ...


class JobService:
    """Coordinate a persistent job board with a remote worker."""

    def __init__(
        self,
        board: JobBoard,
        worker: Worker,
        announcer: Announcer,
        *,
        request_log: RequestLog | None = None,
    ) -> None:
        """Configure job persistence, execution, spoken announcements, and optional request issues."""
        self.board = board
        self.worker = worker
        self.announcer = announcer
        self.request_log = request_log

    async def dispatch(self, request: str, repository: str | None = None) -> Job:
        """Persist a queued job and start it without blocking the caller."""
        job = Job(request=request, repository=repository, worker=self.worker.name)
        self.board.add(job)
        asyncio.create_task(self._start(job), name=f"job-start-{job.id}")
        return job

    async def _file_request(self, job: Job) -> None:
        if self.request_log is None or not self.request_log.wants(job.repository):
            return
        try:
            issue = await self.request_log.file(job)
        except Exception as exc:
            logger.exception("Could not file a GitHub issue for job %s", job.id)
            emit(
                "factory.report_failed", "factory", "error", job_id=job.id, exc_type=type(exc).__name__, error=str(exc)
            )
            return
        if issue is not None:
            job.issue_number = issue.number
            job.issue_url = issue.url
            self.board.update(job)

    async def _mark_fixing(self, job: Job) -> None:
        if self.request_log is None or job.issue_number is None or job.status is not JobStatus.working:
            return
        try:
            await self.request_log.mark_fixing(job.issue_number)
        except Exception as exc:
            logger.exception("Could not relabel issue #%s for job %s", job.issue_number, job.id)
            emit(
                "factory.report_failed", "factory", "error", job_id=job.id, exc_type=type(exc).__name__, error=str(exc)
            )

    async def _start(self, job: Job) -> None:
        await self._file_request(job)
        try:
            updated = await self.worker.start(job)
            self.board.update(updated)
            await self._mark_fixing(updated)
        except Exception as exc:
            logger.exception("Failed to start job %s", job.id)
            emit("job.start_failed", "jobs", "error", job_id=job.id, exc_type=type(exc).__name__, error=str(exc))
            job.status = JobStatus.failed
            job.error = str(exc)
            self.board.update(job)

    async def answer(self, job_id: str, answer: str) -> Job | None:
        """Answer a blocked job and return its updated state."""
        job = self.board.get(job_id)
        if job is None:
            return None
        try:
            await self.worker.answer(job, answer)
            self.board.update(job)
        except Exception as exc:
            logger.exception("Failed to answer job %s", job_id)
            emit("job.answer_failed", "jobs", "error", job_id=job_id, exc_type=type(exc).__name__, error=str(exc))
            job.error = str(exc)
            self.board.update(job)
        return job

    def status_summary(self) -> list[dict[str, object]]:
        """Return compact job state for tools and the local UI."""
        return [
            {
                "id": job.id,
                "status": job.status.value,
                "request": job.request,
                "pr_url": job.pr_url,
                "issue_url": job.issue_url,
                "question": job.question,
                "error": job.error,
                "acus_consumed": job.acus_consumed,
            }
            for job in self.board.recent()
        ]

    async def run_poller(self, interval_s: float) -> None:
        """Refresh open jobs and announce terminal status changes."""
        while True:
            try:
                for job in self.board.open_jobs():
                    if job.status not in {JobStatus.working, JobStatus.blocked}:
                        continue
                    before = job.model_dump()
                    previous_status = job.status
                    refreshed = await self.worker.refresh(job)
                    if refreshed.model_dump() != before:
                        self.board.update(refreshed)
                    if refreshed.status is not previous_status:
                        emit(
                            "job.status_changed",
                            "jobs",
                            job_id=job.id,
                            status=refreshed.status.value,
                            acus_consumed=refreshed.acus_consumed,
                        )
                for job in self.board.unannounced():
                    await self.announcer(self._announcement(job))
                    self.board.mark_announced(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Job poller iteration failed")
                emit("job.poller_failed", "jobs", "error", exc_type=type(exc).__name__)
            await asyncio.sleep(interval_s)

    def cancel(self, job_id: str) -> Job | None:
        """Cancel a job locally without contacting its provider."""
        job = self.board.get(job_id)
        if job is None:
            return None
        job.status = JobStatus.cancelled
        self.board.update(job)
        return job

    @staticmethod
    def _announcement(job: Job) -> str:
        request = " ".join(job.request.split()[:6])
        prefix = f"Job {request}:"
        if job.status is JobStatus.finished:
            if job.pr_url:
                return f"{prefix} finished, PR is up at {job.pr_url}"
            return f"{prefix} finished"
        if job.status is JobStatus.blocked:
            return f"{prefix} needs your input: {job.question or 'Devin needs your input'}"
        if job.status is JobStatus.expired:
            return f"{prefix} expired"
        return f"{prefix} failed: {job.error or 'unknown error'}"
