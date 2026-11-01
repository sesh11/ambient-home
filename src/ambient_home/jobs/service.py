"""Application service for dispatching and announcing background jobs."""

import asyncio
import logging
from typing import Literal, Protocol
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from garage_sdk import emit

from ambient_home.jobs.board import Job, JobBoard, JobStatus
from ambient_home.connectors.coding.worker import Worker


logger = logging.getLogger(__name__)
Announcer = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class RequestRecord:
    """Garage request number and URL."""

    number: int
    url: str


class RequestLog(Protocol):
    """Files requests with Garage and reads their dispatch status."""

    def wants(self, repository: str | None) -> bool:
        """Return whether requests for this repository should be recorded."""
        ...

    async def file(self, job: Job) -> RequestRecord | None:
        """Record the request and return the created issue, if any."""
        ...

    async def status(self, number: int) -> dict[str, object]:
        """Return dispatch state and URLs for a filed request."""
        ...


class RequestNotFiled(Exception):
    """A Garage request could not be filed for a known reason."""

    def __init__(self, reason: Literal["not_configured", "not_covered", "declined", "failed"]) -> None:
        """Store the reason that filing did not produce an issue."""
        self.reason = reason
        super().__init__(reason)


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

    async def file_request(self, request: str, repository: str | None) -> Job:
        """File a software request with Garage and track it until dispatch completes."""
        if self.request_log is None:
            raise RequestNotFiled("not_configured")
        if not self.request_log.wants(repository):
            raise RequestNotFiled("not_covered")
        job = Job(request=request, repository=repository, worker="garage", status=JobStatus.queued)
        try:
            issue = await self.request_log.file(job)
        except Exception as exc:
            logger.error("Could not file a Garage request for job %s (%s)", job.id, type(exc).__name__)
            emit(
                "factory.report_failed",
                "factory",
                "error",
                op="file",
                job_id=job.id,
                exc_type=type(exc).__name__,
            )
            raise RequestNotFiled("failed") from exc
        if issue is None:
            raise RequestNotFiled("declined")
        job.issue_number = issue.number
        job.issue_url = issue.url
        self.board.add(job)
        emit("garage.request_filed", "jobs", job_id=job.id, issue=issue.number)
        return job

    async def _start(self, job: Job) -> None:
        try:
            updated = await self.worker.start(job)
            self.board.update(updated)
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
        if job.worker == "garage":
            job.error = "This request is handled by Garage; answer in its Devin session or GitHub issue."
            self.board.update(job)
            return job
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
                "worker": job.worker,
                "worker_url": job.worker_url,
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
                    if job.worker == self.worker.name:
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
                    elif (
                        job.worker == "garage"
                        and job.status in {JobStatus.queued, JobStatus.working}
                        and job.issue_number is not None
                        and self.request_log is not None
                    ):
                        before = job.model_dump()
                        previous_garage_status = job.status
                        try:
                            state = await self.request_log.status(job.issue_number)
                            status_names = {
                                "queued": JobStatus.queued,
                                "working": JobStatus.working,
                                "finished": JobStatus.finished,
                                "failed": JobStatus.failed,
                                "expired": JobStatus.expired,
                            }
                            state_name = state.get("state")
                            if isinstance(state_name, str) and state_name in status_names:
                                job.status = status_names[state_name]
                            session_url = state.get("session_url")
                            job.worker_url = session_url if isinstance(session_url, str) else None
                            pr_url = state.get("pr_url")
                            job.pr_url = pr_url if isinstance(pr_url, str) else None
                            if job.model_dump() != before:
                                self.board.update(job)
                            if job.status is not previous_garage_status:
                                emit(
                                    "job.status_changed",
                                    "jobs",
                                    job_id=job.id,
                                    status=job.status.value,
                                    acus_consumed=job.acus_consumed,
                                )
                        except Exception as exc:
                            logger.error(
                                "Could not refresh Garage request #%s (%s)", job.issue_number, type(exc).__name__
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
