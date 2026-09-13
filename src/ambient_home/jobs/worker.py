"""Worker abstraction for remote engineering jobs."""

from typing import Protocol

from ambient_home.jobs.board import Job


class Worker(Protocol):
    """Provider-neutral remote job worker."""

    name: str

    async def start(self, job: Job) -> Job:
        """Start work for a queued job."""
        ...

    async def refresh(self, job: Job) -> Job:
        """Refresh a working job from its provider."""
        ...

    async def answer(self, job: Job, answer: str) -> None:
        """Send a user answer to a blocked job."""
        ...
