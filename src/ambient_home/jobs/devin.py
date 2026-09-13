"""Devin v3 API worker."""

import logging

import httpx
from pydantic import Field, BaseModel, ConfigDict

from ambient_home.jobs.board import Job, JobStatus


logger = logging.getLogger(__name__)


class _CreateSessionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    url: str
    status: str


class _PullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pr_url: str
    pr_state: str | None = None


class _SessionMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: str
    source: str


class _MessagesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_SessionMessage] = Field(default_factory=list)
    has_next_page: bool = False
    end_cursor: str | None = None


class _SessionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    status: str
    status_detail: str | None = None
    pull_requests: list[_PullRequest] = Field(default_factory=list)
    structured_output: dict[str, object] | None = None


def build_prompt(job: Job) -> str:
    """Build a policy-constrained prompt for Devin."""
    repository = f"\nRepository: {job.repository}" if job.repository else ""
    return (
        f"Request: {job.request}{repository}\n\n"
        "Rules: Work only in the repository named in the request (or the most plausible repository owned by the user if unnamed; ask if unsure). "
        "Open a pull request with your changes. Never merge, never push directly to main/master, never deploy, never modify branch protection or CI security settings. "
        "If you need a decision from the user, ask and wait. When done, fill the structured output (summary, risk, pr_url, ci_state)."
    )


class DevinWorker:
    """Execute jobs through Devin's v3 sessions API."""

    name = "devin"

    def __init__(
        self,
        api_key: str,
        org_id: str,
        *,
        base_url: str = "https://api.devin.ai",
        max_acu: int = 5,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure a Devin API client."""
        self.api_key = api_key
        self.org_id = org_id
        self.base_url = base_url.rstrip("/")
        self.max_acu = max_acu
        self._http = http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        if self._http is not None:
            return await self._http.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )
        async with httpx.AsyncClient() as client:
            return await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )

    def _sessions_path(self) -> str:
        return f"/v3/organizations/{self.org_id}/sessions"

    async def start(self, job: Job) -> Job:
        """Create a Devin session for a queued job."""
        payload: dict[str, object] = {
            "prompt": build_prompt(job),
            "title": job.request[:80],
            "tags": ["voice", "office"],
            "max_acu_limit": self.max_acu,
            "structured_output_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "pr_url": {"type": ["string", "null"]},
                    "ci_state": {"type": "string"},
                },
                "required": ["summary", "risk", "pr_url", "ci_state"],
            },
        }
        if job.repository:
            payload["repos"] = [job.repository]
        try:
            response = await self._request("POST", self._sessions_path(), payload)
            response.raise_for_status()
            created = _CreateSessionResponse.model_validate(response.json())
        except Exception as exc:
            detail = self._error_detail(exc)
            logger.error("Could not start Devin job %s: %s", job.id, detail)
            job.status = JobStatus.failed
            job.error = detail
            return job
        job.worker_session_id = created.session_id
        job.worker_url = created.url
        job.status = JobStatus.working
        job.error = None
        return job

    async def refresh(self, job: Job) -> Job:
        """Refresh a Devin job and map its provider status."""
        if not job.worker_session_id:
            return job
        try:
            response = await self._request("GET", f"{self._sessions_path()}/{job.worker_session_id}")
            response.raise_for_status()
            session = _SessionResponse.model_validate(response.json())
        except Exception as exc:
            logger.warning("Could not refresh Devin job %s: %s", job.id, exc)
            return job

        status = session.status.lower()
        status_detail = (session.status_detail or "").lower()
        mapped_status = self._map_status(status, status_detail)
        if mapped_status is not None:
            job.status = mapped_status
        if status == "error":
            job.error = f"Devin session error ({session.status_detail})"
        elif status == "suspended" and mapped_status is JobStatus.failed:
            job.error = f"Devin session suspended: {session.status_detail}"

        if session.structured_output is not None:
            summary = session.structured_output.get("summary")
            if isinstance(summary, str):
                job.summary = summary
            pr_url = session.structured_output.get("pr_url")
            if isinstance(pr_url, str):
                job.pr_url = pr_url
            elif session.pull_requests:
                job.pr_url = session.pull_requests[-1].pr_url
        elif session.pull_requests:
            job.pr_url = session.pull_requests[-1].pr_url
        if job.status is JobStatus.blocked:
            job.question = await self._blocked_question(job.worker_session_id)
        return job

    async def _blocked_question(self, session_id: str) -> str:
        try:
            response = await self._request("GET", f"{self._sessions_path()}/{session_id}/messages?first=100")
            response.raise_for_status()
            messages = _MessagesResponse.model_validate(response.json())
        except Exception as exc:
            logger.warning("Could not fetch Devin messages for %s: %s", session_id, exc)
            return "Devin needs your input"
        for message in reversed(messages.items):
            if message.source == "devin":
                return message.message
        return "Devin needs your input"

    async def answer(self, job: Job, answer: str) -> None:
        """Send an answer to a blocked Devin session."""
        if not job.worker_session_id:
            raise ValueError("job has no Devin session")
        response = await self._request(
            "POST",
            f"{self._sessions_path()}/{job.worker_session_id}/messages",
            {"message": answer},
        )
        response.raise_for_status()
        job.status = JobStatus.working
        job.question = None

    @staticmethod
    def _map_status(status: str, status_detail: str) -> JobStatus | None:
        if status in {"new", "claimed", "running", "resuming"}:
            if status_detail in {"waiting_for_user", "waiting_for_approval"}:
                return JobStatus.blocked
            if status_detail == "finished":
                return JobStatus.finished
            return JobStatus.working
        if status == "exit":
            return JobStatus.finished
        if status == "error":
            return JobStatus.failed
        if status == "suspended":
            if status_detail == "finished":
                return JobStatus.finished
            if status_detail in {"inactivity", "user_request"}:
                return JobStatus.expired
            return JobStatus.failed
        return None

    @staticmethod
    def _error_detail(error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            return f"HTTP {response.status_code}: {response.text[:500]}"
        return str(error)


class NullWorker:
    """Worker used when Devin credentials are not configured."""

    name = "devin"

    def __init__(self, error: str = "DEVIN_API_KEY is not configured") -> None:
        """Set the unavailable-worker explanation."""
        self.error = error

    async def start(self, job: Job) -> Job:
        """Fail a job clearly without contacting a provider."""
        job.status = JobStatus.failed
        job.error = self.error
        return job

    async def refresh(self, job: Job) -> Job:
        """Leave unavailable-provider jobs unchanged."""
        return job

    async def answer(self, job: Job, answer: str) -> None:
        """Reject answers when no provider is configured."""
        raise RuntimeError(self.error)
