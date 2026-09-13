"""Application service for dispatching and announcing background jobs."""

import asyncio
import logging
from collections.abc import Callable, Awaitable

from ambient_home.jobs.board import Job, JobBoard, JobStatus
from ambient_home.jobs.worker import Worker


logger = logging.getLogger(__name__)
Announcer = Callable[[str], Awaitable[None]]
DispatchGuard = Callable[[], str | None]


class JobService:
    """Coordinate a persistent job board with a remote worker."""

    def __init__(
        self,
        board: JobBoard,
        worker: Worker,
        announcer: Announcer,
        dispatch_guard: DispatchGuard = lambda: None,
    ) -> None:
        """Configure job persistence, execution, spoken announcements, and the spend guard."""
        self.board = board
        self.worker = worker
        self.announcer = announcer
        self.dispatch_guard = dispatch_guard

    async def dispatch(self, request: str, repository: str | None = None) -> Job:
        """Persist a queued job and start it unless a spend cap refuses new work."""
        job = Job(request=request, repository=repository, worker=self.worker.name)
        refusal = self.dispatch_guard()
        if refusal is not None:
            job.status = JobStatus.failed
            job.error = refusal
            job.announced_status = JobStatus.failed
            self.board.add(job)
            return job
        self.board.add(job)
        asyncio.create_task(self._start(job), name=f"job-start-{job.id}")
        return job

    async def _start(self, job: Job) -> None:
        try:
            updated = await self.worker.start(job)
            self.board.update(updated)
        except Exception as exc:
            logger.exception("Failed to start job %s", job.id)
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
                    refreshed = await self.worker.refresh(job)
                    if refreshed.model_dump() != before:
                        self.board.update(refreshed)
                for job in self.board.unannounced():
                    await self.announcer(self._announcement(job))
                    self.board.mark_announced(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Job poller iteration failed")
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
