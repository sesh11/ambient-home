"""Factory step 4: start one cloud Devin fix session per ready GitHub issue.

An issue is picked up when it is open, labeled ``factory:ready-for-fix``, and has neither
``factory:dispatched`` nor ``factory:request`` (voice requests already start their own session).
A local ledger is written before the issue is labeled, so a crash in between never starts a
second session. Removing ``factory:dispatched`` by hand allows one more attempt.
"""

import os
import json
import asyncio
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from collections.abc import Callable

from pydantic import Field, BaseModel

from ambient_home.telemetry import emit, scrub_text
from ambient_home.jobs.board import JobStatus
from ambient_home.jobs.devin import DevinWorker
from ambient_home.factory.github import IssueRecord, GitHubIssues
from ambient_home.factory.report import READY, FIXING, LABELS, REQUEST, DISPATCHED


logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 6000
OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "pr_url": {"type": ["string", "null"]},
        "robot_test": {"type": "string", "enum": ["passed", "failed", "skipped"]},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["summary", "pr_url", "robot_test", "risk"],
}
_DONE = {JobStatus.finished, JobStatus.failed, JobStatus.expired}


class DispatchRecord(BaseModel):
    """One fix session started for one issue."""

    issue: int
    session_id: str
    session_url: str
    started_at: datetime
    labeled: bool = False
    done: bool = False
    status: str | None = None
    pr_url: str | None = None


class DispatchLedger(BaseModel):
    """Every dispatch this machine has made, newest last."""

    records: list[DispatchRecord] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "DispatchLedger":
        """Read the ledger, or start an empty one."""
        if not path.exists():
            return cls()
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        """Write the ledger atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self.model_dump_json(indent=2))
        os.replace(tmp, path)

    def latest(self, issue: int) -> DispatchRecord | None:
        """Return the newest record for an issue."""
        return next((record for record in reversed(self.records) if record.issue == issue), None)


def _robot_steps(repo: str, issue: int, outpost: str) -> str:
    worktree = f"~/dev/ambient-home/ambient-home-fix-{issue}"
    main = "~/dev/ambient-home/ambient-home"
    models = ".venv/lib/python3.11/site-packages/openwakeword/resources/models"
    return (
        "Robot test. You are on a cloud VM and cannot reach the robot. If your change touches code that runs on "
        "the robot or the Mac (mic/audio, wake word, Live sessions, the Reachy daemon, head motion, LaunchAgents, "
        "hw_test) or the issue is an incident the robot reported, push your branch and start ONE child Devin "
        f'session on Nithin\'s Mac outpost with platform "{outpost}" (if that name is rejected, the error lists the '
        "valid pool names; use the Mac one). Otherwise skip the robot test and say why in the PR. "
        "Give the child exactly these steps:\n"
        f"  1. `cd {main}` is the always-on app's checkout: never switch its branch or edit files in it.\n"
        f"  2. `git -C {main} fetch origin <branch> && git -C {main} worktree add {worktree} FETCH_HEAD`\n"
        f"  3. `cd {worktree} && cp {main}/.env .env && uv sync && mkdir -p {models} "
        f"&& cp -R {main}/{models}/. {models}/`\n"
        f"  4. `uv run ambient-hw-test --markdown /tmp/ambient-hw-test-{issue}.md` (never `--live`; about 2 minutes; "
        "it pauses only ai.ambient-home.app and restores it).\n"
        "  5. Check ai.ambient-home.app is running again (`launchctl print gui/$(id -u)/ai.ambient-home.app`); if "
        "not, `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.ambient-home.app.plist`.\n"
        "  6. Report any `tool.completed` `dispatch_job` events written during the test under "
        "~/Library/Logs/ambient-home/events (room audio can wake Alfred and start a real job).\n"
        f"  7. `git -C {main} worktree remove --force {worktree}`, then send back the exit code and the full "
        "Markdown report.\n"
        "Paste that Markdown report into the PR under a 'Robot test' heading."
    )


def build_fix_prompt(issue: IssueRecord, repo: str, outpost: str) -> str:
    """Build the prompt for a cloud fix session; only the scrubbed title and body are included."""
    body = scrub_text(issue.body).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[... truncated]"
    return (
        f"Fix GitHub issue #{issue.number} in {repo}: {scrub_text(issue.title)}\n"
        f"Issue: {issue.url}\n"
        "Started automatically by the ambient-home factory (step 4, Dispatch).\n\n"
        "Issue body (comments are not included):\n"
        f"<<<\n{body}\n>>>\n\n"
        "Rules:\n"
        f"- Work only in {repo}. Branch off the latest main and open ONE pull request with "
        f"'Closes #{issue.number}' in its body. Never merge, never push to main, never deploy.\n"
        f"- If pushing to origin fails with 403, push with `env -u GH_TOKEN git push https://github.com/{repo}.git "
        "<branch>`.\n"
        "- The repository is public: PRs, comments and reports must contain no secrets, tokens, personal data, "
        "or transcripts.\n"
        "- Before opening the PR, pass `uv run ruff check .`, `uv run mypy` and `uv run pytest tests/ -q`.\n"
        f"- {_robot_steps(repo, issue.number, outpost)}\n"
        "- If you can't find a fix you are confident in, don't open a PR; explain what you found in the summary.\n"
        "- When done, fill the structured output: summary (plain language, no secrets), pr_url, robot_test, risk."
    )


class Dispatcher:
    """Poll for ready issues and start one cloud Devin fix session for each."""

    def __init__(
        self,
        issues: GitHubIssues,
        devin: DevinWorker,
        ledger_path: Path,
        *,
        outpost: str = "my-mac",
        interval_s: float = 3600.0,
        dry_run: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        """Configure the GitHub and Devin clients, the ledger file, and the poll interval."""
        self.issues = issues
        self.devin = devin
        self.ledger_path = ledger_path
        self.outpost = outpost
        self.interval_s = interval_s
        self.dry_run = dry_run
        self.clock = clock
        self._labels_ready = False

    async def run(self) -> None:
        """Poll forever, starting with one pass right away."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Factory dispatch failed")
                emit("factory.dispatch_failed", "factory", "error", exc_type=type(exc).__name__, error=str(exc))
            await asyncio.sleep(self.interval_s)

    async def poll_once(self) -> list[str]:
        """Follow up on running sessions, then dispatch new ready issues; return what was done."""
        ledger = DispatchLedger.load(self.ledger_path)
        done = await self._follow_up(ledger)
        if not self._labels_ready and not self.dry_run:
            await self.issues.ensure_labels(LABELS)
            self._labels_ready = True
        for issue in sorted(await self.issues.list_issues(READY), key=lambda item: item.number):
            action = await self._consider(ledger, issue)
            if action:
                done.append(action)
        return done

    async def _consider(self, ledger: DispatchLedger, issue: IssueRecord) -> str | None:
        if issue.state != "open" or DISPATCHED in issue.labels or REQUEST in issue.labels:
            return None
        record = ledger.latest(issue.number)
        if record is not None and not record.labeled:
            if self.dry_run:
                return f"label #{issue.number} (session already started: {record.session_url})"
            await self._label(ledger, record)
            return f"label #{issue.number} (session already started: {record.session_url})"
        if self.dry_run:
            return f"start session for #{issue.number}: {issue.title}"
        return await self._dispatch(ledger, issue)

    async def _dispatch(self, ledger: DispatchLedger, issue: IssueRecord) -> str | None:
        repo = self.issues.repo
        try:
            created = await self.devin.create_session(
                build_fix_prompt(issue, repo, self.outpost),
                title=f"Fix #{issue.number}: {scrub_text(issue.title)}"[:80],
                tags=["factory", "dispatch"],
                structured_output_schema=OUTPUT_SCHEMA,
                repos=[repo],
            )
        except Exception as exc:
            detail = DevinWorker.error_detail(exc)
            logger.error("Could not start a fix session for #%d: %s", issue.number, detail)
            emit("factory.dispatch_failed", "factory", "error", issue=issue.number, error=detail)
            return f"failed to start session for #{issue.number}: {detail}"
        record = DispatchRecord(
            issue=issue.number, session_id=created.session_id, session_url=created.url, started_at=self.clock()
        )
        ledger.records.append(record)
        ledger.save(self.ledger_path)
        emit("factory.dispatched", "factory", issue=issue.number, session_id=created.session_id)
        await self._label(ledger, record)
        return f"started {created.url} for #{issue.number}: {issue.title}"

    async def _label(self, ledger: DispatchLedger, record: DispatchRecord) -> None:
        await self.issues.add_labels(record.issue, [DISPATCHED, FIXING])
        await self.issues.comment(
            record.issue,
            f"Started a Devin fix session: {record.session_url}\n\n"
            f"Remove `{DISPATCHED}` to allow another attempt.\n\n"
            f"<!-- factory-session: {record.session_id} -->",
        )
        record.labeled = True
        ledger.save(self.ledger_path)

    async def _follow_up(self, ledger: DispatchLedger) -> list[str]:
        done: list[str] = []
        for record in ledger.records:
            if record.done or not record.labeled:
                continue
            try:
                session = await self.devin.get_session(record.session_id)
            except Exception as exc:
                logger.warning("Could not refresh fix session %s: %s", record.session_id, exc)
                continue
            status = DevinWorker.map_status(session.status.lower(), (session.status_detail or "").lower())
            if status not in _DONE:
                continue
            output = session.structured_output or {}
            pr_url = output.get("pr_url")
            if not isinstance(pr_url, str) or not pr_url:
                pr_url = session.pull_requests[-1].pr_url if session.pull_requests else None
            summary = output.get("summary")
            if self.dry_run:
                done.append(f"report outcome on #{record.issue}")
                continue
            lines = [f"Devin fix session ended ({status.value}): {record.session_url}"]
            lines.append(f"Pull request: {pr_url}" if pr_url else "No pull request was opened.")
            robot = output.get("robot_test")
            if isinstance(robot, str):
                lines.append(f"Robot test: {scrub_text(robot)}")
            if isinstance(summary, str) and summary.strip():
                lines += ["", scrub_text(summary).strip()[:2000]]
            await self.issues.comment(record.issue, "\n".join(lines))
            await self.issues.remove_label(record.issue, FIXING)
            record.done = True
            record.status = status.value
            record.pr_url = pr_url
            ledger.save(self.ledger_path)
            emit("factory.dispatch_ended", "factory", issue=record.issue, status=status.value, has_pr=bool(pr_url))
            done.append(f"session for #{record.issue} ended ({status.value})")
        return done
