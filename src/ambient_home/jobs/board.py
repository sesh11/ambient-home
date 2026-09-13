"""Persistent provider-neutral job tracking."""

import os
import json
import logging
import tempfile
from enum import Enum
from uuid import uuid4
from pathlib import Path
from datetime import datetime, timezone

from pydantic import Field, BaseModel


logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Lifecycle states shared by all job workers."""

    queued = "queued"
    working = "working"
    blocked = "blocked"
    finished = "finished"
    failed = "failed"
    expired = "expired"
    cancelled = "cancelled"
    QUEUED = queued
    WORKING = working
    BLOCKED = blocked
    FINISHED = finished
    FAILED = failed
    EXPIRED = expired
    CANCELLED = cancelled


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """A persisted remote engineering job."""

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    request: str
    repository: str | None = None
    worker: str = "devin"
    status: JobStatus = JobStatus.queued
    worker_session_id: str | None = None
    worker_url: str | None = None
    pr_url: str | None = None
    summary: str | None = None
    question: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    announced_status: JobStatus | None = None


class JobBoard:
    """Store jobs in an atomically replaced JSON file."""

    def __init__(self, path: Path) -> None:
        """Load jobs from disk, tolerating a missing or corrupt file."""
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
            if not isinstance(payload, list):
                raise ValueError("job file must contain a list")
            self._jobs = {job.id: job for job in (Job.model_validate(item) for item in payload)}
        except Exception:
            logger.exception("Could not load job board from %s", self.path)
            self._jobs = {}

    def _write(self) -> None:
        payload = [job.model_dump(mode="json") for job in self._jobs.values()]
        fd, temp_name = tempfile.mkstemp(prefix=f"{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as temporary:
                json.dump(payload, temporary, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                logger.debug("Could not remove failed job-board temp file %s", temp_name, exc_info=True)
            raise

    def add(self, job: Job) -> None:
        """Persist a new job."""
        self._jobs[job.id] = job
        self._write()

    def get(self, job_id: str) -> Job | None:
        """Return a job by identifier."""
        return self._jobs.get(job_id)

    def update(self, job: Job) -> None:
        """Replace and persist an existing job."""
        job.updated_at = _utc_now()
        self._jobs[job.id] = job
        self._write()

    def open_jobs(self) -> list[Job]:
        """Return jobs that still need attention."""
        return [
            job
            for job in self._jobs.values()
            if job.status in {JobStatus.queued, JobStatus.working, JobStatus.blocked}
        ]

    def recent(self, limit: int = 10) -> list[Job]:
        """Return the newest jobs first."""
        return sorted(self._jobs.values(), key=lambda job: job.updated_at, reverse=True)[:limit]

    def unannounced(self) -> list[Job]:
        """Return terminal jobs whose status has not been announced."""
        terminal = {JobStatus.finished, JobStatus.failed, JobStatus.blocked, JobStatus.expired}
        return [job for job in self._jobs.values() if job.status in terminal and job.status != job.announced_status]

    def mark_announced(self, job: Job) -> None:
        """Record that a terminal status was announced."""
        job.announced_status = job.status
        self.update(job)
