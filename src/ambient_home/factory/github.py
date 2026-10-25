"""Minimal GitHub Issues client for the factory reporter."""

import re
from datetime import datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel


_MARKER = re.compile(r"<!--\s*factory-(?P<key>[a-z-]+):\s*(?P<value>[^\s>]+)\s*-->")
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class IssueRecord(BaseModel):
    """The parts of a GitHub issue the factory tracks."""

    number: int
    url: str
    state: str
    title: str
    body: str
    labels: list[str]
    created_at: datetime
    closed_at: datetime | None = None

    def marker(self, key: str) -> str | None:
        """Return the value of a hidden ``<!-- factory-key: value -->`` marker in the body."""
        for match in _MARKER.finditer(self.body):
            if match.group("key") == key:
                return match.group("value")
        return None


def _record(payload: dict[str, object]) -> IssueRecord:
    raw_labels = payload.get("labels") or []
    labels = (
        [str(item.get("name")) for item in raw_labels if isinstance(item, dict)]
        if isinstance(raw_labels, list)
        else []
    )
    return IssueRecord.model_validate(
        {
            "number": payload["number"],
            "url": payload["html_url"],
            "state": payload["state"],
            "title": payload.get("title") or "",
            "body": payload.get("body") or "",
            "labels": labels,
            "created_at": payload["created_at"],
            "closed_at": payload.get("closed_at"),
        }
    )


class GitHubIssues:
    """Create, update, and list issues in one repository through the REST API."""

    def __init__(
        self,
        repo: str,
        token: str | None,
        *,
        base_url: str = "https://api.github.com",
        http: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        """Configure the target repository and optional token (reads work without one on public repos)."""
        self.repo = repo
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._http = http
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request(self, method: str, url: str, json: object | None = None) -> httpx.Response:
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"
        if self._http is not None:
            response = await self._http.request(method, url, json=json, headers=self._headers())
        else:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.request(method, url, json=json, headers=self._headers())
        response.raise_for_status()
        return response

    @property
    def _repo_path(self) -> str:
        return f"/repos/{self.repo}"

    async def list_issues(self, label: str) -> list[IssueRecord]:
        """Return every open and closed issue (not PR) carrying ``label``."""
        url: str | None = f"{self._repo_path}/issues?state=all&labels={quote(label)}&per_page=100"
        found: list[IssueRecord] = []
        while url:
            response = await self._request("GET", url)
            payload = response.json()
            if isinstance(payload, list):
                found.extend(
                    _record(item) for item in payload if isinstance(item, dict) and "pull_request" not in item
                )
            match = _NEXT.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
        return found

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """Create any missing labels; ``labels`` maps name to (color, description)."""
        response = await self._request("GET", f"{self._repo_path}/labels?per_page=100")
        existing = {str(item.get("name")) for item in response.json() if isinstance(item, dict)}
        for name, (color, description) in labels.items():
            if name in existing:
                continue
            try:
                await self._request(
                    "POST", f"{self._repo_path}/labels", {"name": name, "color": color, "description": description}
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422:
                    raise

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        """Open a new issue."""
        response = await self._request(
            "POST", f"{self._repo_path}/issues", {"title": title, "body": body, "labels": labels}
        )
        return _record(response.json())

    async def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> IssueRecord:
        """Edit an issue's title, body, or state."""
        payload: dict[str, object] = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if state is not None:
            payload["state"] = state
        response = await self._request("PATCH", f"{self._repo_path}/issues/{number}", payload)
        return _record(response.json())

    async def add_labels(self, number: int, labels: list[str]) -> None:
        """Add labels without touching any others on the issue."""
        await self._request("POST", f"{self._repo_path}/issues/{number}/labels", {"labels": labels})

    async def remove_label(self, number: int, label: str) -> None:
        """Remove one label, ignoring it if it is already gone."""
        try:
            await self._request("DELETE", f"{self._repo_path}/issues/{number}/labels/{quote(label, safe='')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise

    async def comment(self, number: int, body: str) -> None:
        """Post a comment on an issue."""
        await self._request("POST", f"{self._repo_path}/issues/{number}/comments", {"body": body})
