"""Factory step 4: dispatch ready issues to cloud Devin fix sessions."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from ambient_home.config import settings_from_env
from ambient_home.jobs.devin import DevinWorker, SessionState, CreatedSession
from ambient_home.factory.cli import build_dispatcher
from ambient_home.factory.github import IssueRecord
from ambient_home.factory.report import READY, FIXING, REQUEST, INCIDENT, DISPATCHED
from ambient_home.factory.dispatch import Dispatcher, DispatchLedger, build_fix_prompt


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class FakeGitHub:
    def __init__(self) -> None:
        self.repo = "sesh11/ambient-home"
        self.issues: dict[int, IssueRecord] = {}
        self.comments: list[tuple[int, str]] = []
        self.fail_labels = False

    def add(self, number: int, labels: list[str], *, state: str = "open", body: str = "body") -> None:
        self.issues[number] = IssueRecord(
            number=number,
            url=f"https://github.com/{self.repo}/issues/{number}",
            state=state,
            title=f"Problem {number}",
            body=body,
            labels=labels,
            created_at=NOW,
        )

    async def ensure_labels(self, labels) -> None:
        pass

    async def list_issues(self, label: str) -> list[IssueRecord]:
        return [issue.model_copy(deep=True) for issue in self.issues.values() if label in issue.labels]

    async def add_labels(self, number: int, labels: list[str]) -> None:
        if self.fail_labels:
            raise RuntimeError("github down")
        self.issues[number].labels.extend(label for label in labels if label not in self.issues[number].labels)

    async def remove_label(self, number: int, label: str) -> None:
        self.issues[number].labels = [item for item in self.issues[number].labels if item != label]

    async def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))


class FakeDevin:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.states: dict[str, SessionState] = {}
        self.fail = False

    async def create_session(self, prompt: str, **kwargs) -> CreatedSession:
        if self.fail:
            raise RuntimeError("HTTP 400: bad platform")
        session_id = f"s{len(self.created) + 1}"
        self.created.append({"prompt": prompt, **kwargs})
        self.states[session_id] = SessionState(session_id=session_id, status="running")
        return CreatedSession(session_id=session_id, url=f"https://app.devin.ai/sessions/{session_id}", status="new")

    async def get_session(self, session_id: str) -> SessionState:
        return self.states[session_id]


def dispatcher(github: FakeGitHub, devin: FakeDevin, tmp_path, **kwargs) -> Dispatcher:
    return Dispatcher(github, devin, tmp_path / "ledger.json", clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


async def test_ready_issue_gets_one_cloud_session_and_is_marked(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(28, [INCIDENT, READY])
    first = dispatcher(github, devin, tmp_path)

    actions = await first.poll_once()
    await first.poll_once()
    await dispatcher(github, devin, tmp_path).poll_once()

    assert len(devin.created) == 1
    assert "platform" not in devin.created[0]
    assert devin.created[0]["repos"] == ["sesh11/ambient-home"]
    assert {DISPATCHED, FIXING, READY} <= set(github.issues[28].labels)
    assert len(github.comments) == 1
    assert "https://app.devin.ai/sessions/s1" in github.comments[0][1]
    assert "<!-- factory-session: s1 -->" in github.comments[0][1]
    assert actions == ["started https://app.devin.ai/sessions/s1 for #28: Problem 28"]


async def test_skips_closed_requests_and_already_dispatched(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(1, [INCIDENT, READY], state="closed")
    github.add(2, [REQUEST, READY])
    github.add(3, [INCIDENT, READY, DISPATCHED])
    github.add(4, [READY])

    await dispatcher(github, devin, tmp_path).poll_once()

    assert [created["title"] for created in devin.created] == ["Fix #4: Problem 4"]


async def test_crash_before_labeling_does_not_start_a_second_session(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(5, [INCIDENT, READY])
    github.fail_labels = True
    with pytest.raises(RuntimeError):
        await dispatcher(github, devin, tmp_path).poll_once()
    github.fail_labels = False

    actions = await dispatcher(github, devin, tmp_path).poll_once()

    assert len(devin.created) == 1
    assert DISPATCHED in github.issues[5].labels
    assert actions == ["label #5 (session already started: https://app.devin.ai/sessions/s1)"]


async def test_removing_dispatched_label_allows_another_attempt(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(6, [INCIDENT, READY])
    await dispatcher(github, devin, tmp_path).poll_once()
    await github.remove_label(6, DISPATCHED)

    await dispatcher(github, devin, tmp_path).poll_once()

    assert len(devin.created) == 2


async def test_failed_start_leaves_issue_unmarked(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(7, [INCIDENT, READY])
    devin.fail = True

    actions = await dispatcher(github, devin, tmp_path).poll_once()

    assert DISPATCHED not in github.issues[7].labels
    assert github.comments == []
    assert actions[0].startswith("failed to start session for #7")


async def test_finished_session_reports_pr_and_drops_fixing(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(8, [INCIDENT, READY])
    run = dispatcher(github, devin, tmp_path)
    await run.poll_once()
    devin.states["s1"] = SessionState(
        session_id="s1",
        status="exit",
        structured_output={
            "summary": "Restarted the mic stream after silence.",
            "pr_url": "https://github.com/sesh11/ambient-home/pull/40",
            "robot_test": "passed",
            "risk": "low",
        },
    )

    actions = await run.poll_once()
    await run.poll_once()

    assert actions == ["session for #8 ended (finished)"]
    assert FIXING not in github.issues[8].labels
    assert DISPATCHED in github.issues[8].labels
    outcome = github.comments[-1][1]
    assert "pull/40" in outcome and "Robot test: passed" in outcome
    assert len(github.comments) == 2
    assert DispatchLedger.load(tmp_path / "ledger.json").records[0].done


async def test_session_waiting_after_reporting_counts_as_finished(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(10, [INCIDENT, READY])
    run = dispatcher(github, devin, tmp_path)
    await run.poll_once()
    devin.states["s1"] = SessionState(session_id="s1", status="running", status_detail="waiting_for_user")
    assert await run.poll_once() == []
    devin.states["s1"] = SessionState(
        session_id="s1",
        status="running",
        status_detail="waiting_for_user",
        structured_output={"summary": "Done.", "pr_url": "https://github.com/sesh11/ambient-home/pull/41"},
    )

    actions = await run.poll_once()

    assert actions == ["session for #10 ended (finished)"]
    assert FIXING not in github.issues[10].labels
    assert "pull/41" in github.comments[-1][1]


async def test_dry_run_changes_nothing(tmp_path) -> None:
    github, devin = FakeGitHub(), FakeDevin()
    github.add(9, [INCIDENT, READY])

    actions = await dispatcher(github, devin, tmp_path, dry_run=True).poll_once()

    assert actions == ["start session for #9: Problem 9"]
    assert devin.created == [] and github.comments == []
    assert not (tmp_path / "ledger.json").exists()


def test_fix_prompt_covers_rules_and_robot_test_on_outpost() -> None:
    issue = IssueRecord(
        number=28,
        url="https://github.com/sesh11/ambient-home/issues/28",
        state="open",
        title="Microphone delivers only zeros",
        body="token sk-abcdefghijklmnopqrstuvwxyz123456 seen",
        labels=[INCIDENT, READY],
        created_at=NOW,
    )

    prompt = build_fix_prompt(issue, "sesh11/ambient-home", "user:my-mac")

    assert "Closes #28" in prompt
    assert "Never merge" in prompt
    assert 'platform "user:my-mac"' in prompt
    assert "uv run ambient-hw-test --markdown" in prompt and "never `--live`" in prompt
    assert "worktree add" in prompt
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in prompt


async def test_devin_worker_create_session_sends_cloud_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"session_id": "s1", "url": "https://app.devin.ai/sessions/s1", "status": "new"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = DevinWorker("secret", "org-test", max_acu=9, http=client)
        created = await worker.create_session(
            "Fix it", title="Fix #1", tags=["factory"], structured_output_schema={"type": "object"}, repos=["o/r"]
        )

    payload = json.loads(requests[0].content)
    assert created.session_id == "s1"
    assert payload["max_acu_limit"] == 9 and payload["repos"] == ["o/r"]
    assert "platform" not in payload


def test_dispatch_settings(monkeypatch) -> None:
    monkeypatch.setenv("AMBIENT_FACTORY_DISPATCH", "true")
    monkeypatch.setenv("AMBIENT_FACTORY_DISPATCH_INTERVAL", "30m")
    monkeypatch.setenv("AMBIENT_FACTORY_OUTPOST", "user:my-mac")
    monkeypatch.setenv("AMBIENT_GITHUB_TOKEN", "gh")
    monkeypatch.setenv("DEVIN_API_KEY", "key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org")
    settings = settings_from_env()

    built = build_dispatcher(settings)

    assert settings.factory_dispatch_interval_s == 1800
    assert built is not None and built.outpost == "user:my-mac" and built.interval_s == 1800


def test_dispatch_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AMBIENT_FACTORY_DISPATCH", raising=False)
    monkeypatch.delenv("AMBIENT_FACTORY_DISPATCH_INTERVAL", raising=False)
    settings = settings_from_env()
    assert settings.factory_dispatch_interval_s == 3600
    assert build_dispatcher(settings) is None
