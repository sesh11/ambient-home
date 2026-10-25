"""Factory step 3: incident and voice-request GitHub issues."""

import json
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ambient_home.ui import create_app
from ambient_home.costs import CostRates, CostTracker
from ambient_home.config import settings_from_env
from ambient_home.spend_log import SpendLog
from ambient_home.jobs.board import Job, JobBoard, JobStatus
from ambient_home.jobs.devin import NullWorker, build_prompt
from ambient_home.factory.cli import main as report_main
from ambient_home.factory.cli import build_reporters
from ambient_home.jobs.service import JobService
from ambient_home.live_handler import LiveStatus
from ambient_home.session_gate import GateState
from ambient_home.factory.github import IssueRecord, GitHubIssues
from ambient_home.factory.report import (
    READY,
    FIXING,
    REQUEST,
    INCIDENT,
    OBSERVED,
    RequestLogger,
    IncidentReporter,
    render_incident_body,
)
from ambient_home.incidents.base import Incident
from ambient_home.telemetry.events import Event, EventLog
from ambient_home.incidents.monitor import IncidentMonitor


NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class FakeGitHub:
    """In-memory stand-in for GitHubIssues."""

    def __init__(self, repo: str = "sesh11/ambient-home") -> None:
        self.repo = repo
        self.issues: dict[int, IssueRecord] = {}
        self.comments: list[tuple[int, str]] = []
        self.calls: list[str] = []
        self.labels_created = 0
        self.fail_create = False

    async def ensure_labels(self, labels) -> None:
        self.labels_created += 1

    async def list_issues(self, label: str) -> list[IssueRecord]:
        self.calls.append(f"list {label}")
        return [issue.model_copy(deep=True) for issue in self.issues.values() if label in issue.labels]

    async def create_issue(self, title: str, body: str, labels: list[str]) -> IssueRecord:
        if self.fail_create:
            raise RuntimeError("github down")
        number = len(self.issues) + 1
        issue = IssueRecord(
            number=number,
            url=f"https://github.com/{self.repo}/issues/{number}",
            state="open",
            title=title,
            body=body,
            labels=list(labels),
            created_at=NOW,
        )
        self.issues[number] = issue
        self.calls.append(f"create {number}")
        return issue.model_copy(deep=True)

    async def update_issue(self, number: int, *, title=None, body=None, state=None) -> IssueRecord:
        issue = self.issues[number]
        if body is not None:
            issue.body = body
        if state is not None:
            issue.state = state
            issue.closed_at = None if state == "open" else NOW
        self.calls.append(f"update {number}" + (f" {state}" if state else ""))
        return issue.model_copy(deep=True)

    async def add_labels(self, number: int, labels: list[str]) -> None:
        self.issues[number].labels.extend(label for label in labels if label not in self.issues[number].labels)

    async def remove_label(self, number: int, label: str) -> None:
        self.issues[number].labels = [item for item in self.issues[number].labels if item != label]

    async def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))


def event(minutes_ago: float, **data) -> Event:
    return Event(ts=NOW - timedelta(minutes=minutes_ago), type="mic.check", component="mic_check", data=data)


def incident(count: int, *, threshold: int = 3, fingerprint: str = "mic_all_zero:abc", samples=None) -> Incident:
    samples = samples or [event(60 - index, verdict="silent") for index in range(count)]
    return Incident(
        fingerprint=fingerprint,
        rule="mic_all_zero",
        title="Microphone delivers only zeros",
        severity="high",
        count=count,
        threshold=threshold,
        window_s=21600,
        first_seen=samples[0].ts,
        last_seen=samples[-1].ts,
        ready=count >= threshold,
        samples=samples,
        detector="rules",
    )


def reporter(github: FakeGitHub, **kwargs) -> IncidentReporter:
    clock = kwargs.pop("clock", lambda: NOW)
    return IncidentReporter(github, clock=clock, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_new_incident_opens_one_observed_issue_and_dedupes(tmp_path) -> None:
    github = FakeGitHub()
    rep = reporter(github)
    await rep.sync([incident(1)])
    await rep.sync([incident(1)])
    assert [call for call in github.calls if call.startswith("create")] == ["create 1"]
    issue = github.issues[1]
    assert issue.labels == [INCIDENT, OBSERVED]
    assert issue.marker("fingerprint") == "mic_all_zero:abc"
    assert rep.issue_url("mic_all_zero:abc") == "https://github.com/sesh11/ambient-home/issues/1"


@pytest.mark.asyncio
async def test_restart_finds_existing_issue_by_marker() -> None:
    github = FakeGitHub()
    await reporter(github).sync([incident(1)])
    await reporter(github).sync([incident(2)])
    assert len(github.issues) == 1


@pytest.mark.asyncio
async def test_threshold_flips_label_and_comments_once() -> None:
    github = FakeGitHub()
    rep = reporter(github)
    await rep.sync([incident(1)])
    await rep.sync([incident(3)])
    await rep.sync([incident(4)])
    assert READY in github.issues[1].labels and OBSERVED not in github.issues[1].labels
    assert len(github.comments) == 1 and "Reached the threshold" in github.comments[0][1]


@pytest.mark.asyncio
async def test_body_edits_are_rate_limited() -> None:
    github = FakeGitHub()
    now = [NOW]
    rep = reporter(github, clock=lambda: now[0], min_update_interval=timedelta(minutes=10))
    await rep.sync([incident(1, threshold=10)])
    await rep.sync([incident(2, threshold=10)])
    assert "update 1" not in github.calls
    now[0] = NOW + timedelta(minutes=11)
    await rep.sync([incident(2, threshold=10)])
    assert github.calls.count("update 1") == 1
    await rep.sync([incident(2, threshold=10)])
    assert github.calls.count("update 1") == 1


@pytest.mark.asyncio
async def test_closed_issue_reopens_only_for_events_after_close() -> None:
    github = FakeGitHub()
    await reporter(github).sync([incident(3)])
    github.issues[1].state = "closed"
    github.issues[1].closed_at = NOW - timedelta(minutes=30)
    await reporter(github).sync([incident(3)])
    assert github.issues[1].state == "closed"

    later = [event(60, verdict="silent"), event(10, verdict="silent")]
    await reporter(github).sync([incident(2, samples=later)])
    issue = github.issues[1]
    assert issue.state == "open"
    assert OBSERVED in issue.labels and READY not in issue.labels
    assert issue.marker("since") is not None
    assert "1 / 3" in issue.body
    assert "Seen again" in github.comments[-1][1]


@pytest.mark.asyncio
async def test_daily_cap_limits_new_issues() -> None:
    github = FakeGitHub()
    rep = reporter(github, max_new_per_day=2)
    done = await rep.sync([incident(1, fingerprint=f"rule:{index}") for index in range(4)])
    assert len(github.issues) == 2
    assert sum("daily cap" in action for action in done) == 2


@pytest.mark.asyncio
async def test_dry_run_changes_nothing() -> None:
    github = FakeGitHub()
    done = await reporter(github, dry_run=True).sync([incident(3)])
    assert github.issues == {} and github.labels_created == 0
    assert done == [f"open [{INCIDENT}, {READY}]: Microphone delivers only zeros"]


def test_body_is_scrubbed_again_and_drops_sensitive_keys() -> None:
    samples = [
        event(5, error="token sk-abcdefghijklmnop at /Users/nithin/dev from me@example.com", transcript="hello"),
    ]
    body = render_incident_body(incident(1, samples=samples), 1, None)
    assert "sk-abc" not in body and "nithin" not in body and "me@example.com" not in body
    assert "transcript" not in body and "hello" not in body
    assert "<secret>" in body and "<email>" in body


def test_body_is_stable_for_unchanged_incident() -> None:
    assert render_incident_body(incident(2), 2, None) == render_incident_body(incident(2), 2, None)


@pytest.mark.asyncio
async def test_github_client_paginates_and_skips_pull_requests() -> None:
    seen: list[httpx.Request] = []

    def issue(number: int, **extra) -> dict:
        return {
            "number": number,
            "html_url": f"https://github.com/o/r/issues/{number}",
            "state": "open",
            "title": "t",
            "body": f"<!-- factory-fingerprint: rule:{number} -->",
            "labels": [{"name": INCIDENT}],
            "created_at": "2026-10-25T00:00:00Z",
            "closed_at": None,
            **extra,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[issue(3)])
        return httpx.Response(
            200,
            json=[issue(1), issue(2, pull_request={})],
            headers={"link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"'},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GitHubIssues("o/r", "tok", http=http)
        found = await client.list_issues(INCIDENT)
    assert [item.number for item in found] == [1, 3]
    assert found[0].marker("fingerprint") == "rule:1"
    assert "labels=factory%3Aincident" in str(seen[0].url)
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_github_client_label_calls() -> None:
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.raw_path.decode(), body))
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": INCIDENT}])
        if request.method == "DELETE":
            return httpx.Response(404)
        return httpx.Response(201, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GitHubIssues("o/r", "tok", http=http)
        await client.ensure_labels({INCIDENT: ("000000", "x"), READY: ("111111", "y")})
        await client.remove_label(7, READY)
    posted = [item for item in seen if item[0] == "POST"]
    assert posted == [("POST", "/repos/o/r/labels", {"name": READY, "color": "111111", "description": "y"})]
    assert ("DELETE", "/repos/o/r/issues/7/labels/factory%3Aready-for-fix", None) in seen


class StartingWorker:
    name = "fake"

    def __init__(self, status: JobStatus = JobStatus.working) -> None:
        self.status = status
        self.prompts: list[str] = []

    async def start(self, job: Job) -> Job:
        self.prompts.append(build_prompt(job))
        job.status = self.status
        return job

    async def refresh(self, job: Job) -> Job:
        return job

    async def answer(self, job: Job, answer: str) -> None:
        return None


async def quiet(text: str) -> None:
    return None


@pytest.mark.asyncio
async def test_voice_request_for_factory_repo_files_ready_issue_then_fixing(tmp_path) -> None:
    github = FakeGitHub()
    worker = StartingWorker()
    service = JobService(
        JobBoard(tmp_path / "jobs.json"),
        worker,
        quiet,
        request_log=RequestLogger(github, clock=lambda: NOW),  # type: ignore[arg-type]
    )
    job = await service.dispatch("Make Alfred nod 10 degrees, email me@example.com", "sesh11/ambient-home")
    for _ in range(5):
        await asyncio.sleep(0)
    issue = github.issues[1]
    assert issue.labels == [REQUEST, FIXING]
    assert issue.title.startswith("Voice request: Make Alfred nod")
    assert "me@example.com" not in issue.body and issue.marker("request") == job.id
    stored = service.board.get(job.id)
    assert stored.issue_number == 1 and stored.issue_url == issue.url
    assert "Closes #1" in worker.prompts[0]


@pytest.mark.asyncio
async def test_voice_request_stays_ready_when_session_fails_and_other_repos_skip(tmp_path) -> None:
    github = FakeGitHub()
    service = JobService(
        JobBoard(tmp_path / "jobs.json"),
        StartingWorker(JobStatus.failed),
        quiet,
        request_log=RequestLogger(github, clock=lambda: NOW),  # type: ignore[arg-type]
    )
    await service.dispatch("Fix the wake word", "ambient-home")
    await service.dispatch("Fix the readme", "sesh11/experiments")
    await service.dispatch("Something unnamed")
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(github.issues) == 1
    assert github.issues[1].labels == [REQUEST, READY]


@pytest.mark.asyncio
async def test_issue_failure_does_not_block_the_job(tmp_path) -> None:
    github = FakeGitHub()
    github.fail_create = True
    worker = StartingWorker()
    service = JobService(
        JobBoard(tmp_path / "jobs.json"),
        worker,
        quiet,
        request_log=RequestLogger(github, clock=lambda: NOW),  # type: ignore[arg-type]
    )
    job = await service.dispatch("Fix it", "sesh11/ambient-home")
    for _ in range(5):
        await asyncio.sleep(0)
    assert service.board.get(job.id).status is JobStatus.working
    assert "GitHub issue" not in worker.prompts[0]


@pytest.mark.asyncio
async def test_request_daily_cap() -> None:
    github = FakeGitHub()
    log = RequestLogger(github, max_per_day=1, clock=lambda: NOW)  # type: ignore[arg-type]
    assert await log.file(Job(request="one")) is not None
    assert await log.file(Job(request="two")) is None


@pytest.mark.asyncio
async def test_monitor_feeds_reporter(tmp_path) -> None:
    received: list[list[Incident]] = []

    class OneShot:
        name = "one"

        async def detect(self, events, now):
            return [incident(1)] if events else []

    event_log = EventLog(tmp_path / "events")
    monitor = IncidentMonitor(OneShot(), event_log, lookback=timedelta(hours=1), debounce_s=0)
    monitor.add_listener(received.append)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.01)
    event_log.emit("trigger", "test")
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert received[-1][0].fingerprint == "mic_all_zero:abc"


@pytest.mark.asyncio
async def test_reporter_run_loop_posts_notified_incidents() -> None:
    github = FakeGitHub()
    rep = reporter(github)
    task = asyncio.create_task(rep.run(retry_s=60))
    await asyncio.sleep(0)
    rep.notify([incident(1)])
    for _ in range(10):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(github.issues) == 1


def test_incidents_api_includes_issue_url(tmp_path) -> None:
    class Detector:
        name = "fixed"

        async def detect(self, events, now):
            return [incident(1)]

    github = FakeGitHub()
    rep = reporter(github)
    asyncio.run(rep.sync([incident(1)]))
    board = JobBoard(tmp_path / "jobs.json")
    app = create_app(
        lambda: LiveStatus(GateState.ASLEEP, 0.0, 0.0, 0.0, None),
        JobService(board, NullWorker(), quiet),
        quiet,  # type: ignore[arg-type]
        CostTracker(SpendLog(tmp_path / "spend.jsonl"), board, rates=CostRates(0.3, 2.0)),
        event_log=EventLog(tmp_path / "events"),
        detector=Detector(),
        reporter=rep,
    )
    with TestClient(app) as client:
        found = client.get("/api/incidents").json()["incidents"]
    assert found[0]["issue_url"] == "https://github.com/sesh11/ambient-home/issues/1"


def test_reporting_is_off_by_default_and_needs_token(monkeypatch) -> None:
    monkeypatch.delenv("AMBIENT_FACTORY_REPORT", raising=False)
    assert build_reporters(settings_from_env()) == (None, None)
    monkeypatch.setenv("AMBIENT_FACTORY_REPORT", "true")
    monkeypatch.delenv("AMBIENT_GITHUB_TOKEN", raising=False)
    assert build_reporters(settings_from_env()) == (None, None)
    monkeypatch.setenv("AMBIENT_GITHUB_TOKEN", "x")
    rep, log = build_reporters(settings_from_env())
    assert rep is not None and log is not None


def test_cli_requires_token_unless_dry_run(monkeypatch, capsys) -> None:
    monkeypatch.delenv("AMBIENT_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("ambient_home.factory.cli.load_dotenv", lambda: None)
    assert report_main([]) == 2
    assert "AMBIENT_GITHUB_TOKEN" in capsys.readouterr().err
