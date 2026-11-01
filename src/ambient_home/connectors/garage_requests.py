"""Optional request filing through a local Garage service."""

import httpx

from ambient_home.jobs.board import Job
from ambient_home.jobs.service import RequestRecord


class HttpRequestLog:
    """File jobs as Garage requests without logging their text."""

    def __init__(
        self,
        base_url: str,
        repositories: frozenset[str],
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        token: str | None = None,
    ) -> None:
        """Configure the Garage endpoint and repository allowlist."""
        self.base_url = base_url.rstrip("/")
        self.repositories = repositories
        self._client = client
        self.timeout_s = timeout_s
        self.token = token

    def wants(self, repository: str | None) -> bool:
        """Return whether Garage handles the selected repository."""
        if not repository:
            return False
        target = repository.strip().lower().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        return any(
            target in {configured.lower(), configured.lower().split("/")[-1]} for configured in self.repositories
        )

    async def file(self, job: Job) -> RequestRecord | None:
        """Create a Garage request for a completed job."""
        response = await self._request(
            "POST",
            "/api/requests",
            payload={
                "id": job.id,
                "request": job.request,
                "created_at": job.created_at.isoformat(),
                "repository": job.repository,
            },
        )
        response.raise_for_status()
        payload = response.json()
        issue = payload.get("issue")
        if issue is None:
            return None
        return RequestRecord(number=int(issue["number"]), url=str(issue["url"]))

    async def status(self, number: int) -> dict[str, object]:
        """Read the dispatch status for one Garage request."""
        response = await self._request("GET", f"/api/requests/{number}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Garage returned an invalid request status")
        return payload

    async def _request(self, method: str, path: str, *, payload: dict[str, object] | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token is not None else {}
        if self._client is not None:
            if payload is None:
                return await self._client.request(method, f"{self.base_url}{path}", headers=headers)
            return await self._client.request(method, f"{self.base_url}{path}", json=payload, headers=headers)
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            if payload is None:
                return await client.request(method, f"{self.base_url}{path}", headers=headers)
            return await client.request(method, f"{self.base_url}{path}", json=payload, headers=headers)
