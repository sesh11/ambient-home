"""Factory wiring and the ``ambient-report`` command."""

import sys
import asyncio
import logging
import argparse
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from collections.abc import Sequence

from dotenv import load_dotenv

from ambient_home.config import AmbientSettings, settings_from_env
from ambient_home.telemetry import EventLog
from ambient_home.jobs.devin import DevinWorker
from ambient_home.factory.github import GitHubIssues
from ambient_home.factory.report import READY, RequestLogger, IncidentReporter
from ambient_home.incidents.base import Detector, Incident, parse_duration
from ambient_home.factory.dispatch import Dispatcher, build_fix_prompt
from ambient_home.telemetry.events import Event
from ambient_home.incidents.registry import build_detector


logger = logging.getLogger(__name__)


def build_reporters(settings: AmbientSettings) -> tuple[IncidentReporter | None, RequestLogger | None]:
    """Return the incident reporter and voice-request logger, or Nones when reporting is off."""
    if not settings.factory_report:
        return None, None
    if not settings.github_token:
        logger.warning("AMBIENT_FACTORY_REPORT is on but AMBIENT_GITHUB_TOKEN is not set; not filing GitHub issues.")
        return None, None
    issues = GitHubIssues(settings.factory_repo, settings.github_token)
    cap = settings.factory_max_new_issues_per_day
    return IncidentReporter(issues, max_new_per_day=cap), RequestLogger(issues, max_per_day=cap)


def build_dispatcher(settings: AmbientSettings, *, dry_run: bool = False) -> Dispatcher | None:
    """Return the fix-session dispatcher, or None when dispatch is off or not configured."""
    if not settings.factory_dispatch and not dry_run:
        return None
    missing = [
        name
        for name, value in (
            ("AMBIENT_GITHUB_TOKEN", settings.github_token),
            ("DEVIN_API_KEY", settings.devin_api_key),
            ("DEVIN_ORG_ID", settings.devin_org_id),
        )
        if not value
    ]
    if missing and not dry_run:
        logger.warning("AMBIENT_FACTORY_DISPATCH is on but %s is not set; not dispatching.", ", ".join(missing))
        return None
    return Dispatcher(
        GitHubIssues(settings.factory_repo, settings.github_token),
        DevinWorker(
            settings.devin_api_key or "",
            settings.devin_org_id or "",
            base_url=settings.devin_api_base_url,
            max_acu=settings.devin_max_acu,
        ),
        settings.data_dir / "factory_dispatch.json",
        outpost=settings.factory_outpost,
        interval_s=settings.factory_dispatch_interval_s,
        dry_run=dry_run,
    )


async def detect_over(detector: Detector, events: Sequence[Event], start: datetime, now: datetime) -> list[Incident]:
    """Replay detection across the lookback so incidents whose rule window has passed are still found.

    Each fingerprint keeps its worst moment: ready beats observed, then the higher count.
    """
    relevant = sorted(
        (event for event in events if event.component != "factory" and event.type != "app.heartbeat"),
        key=lambda event: event.ts,
    )
    points = {now}
    for event in relevant:
        # The extra point an hour later lets rules with a clear-within delay settle.
        points.update(t for t in (event.ts, event.ts + timedelta(hours=1)) if start <= t <= now)
    best: dict[str, Incident] = {}
    for point in sorted(points):
        for incident in await detector.detect([event for event in relevant if event.ts <= point], point):
            current = best.get(incident.fingerprint)
            if current is None or (incident.ready, incident.count) > (current.ready, current.count):
                best[incident.fingerprint] = incident
    return sorted(best.values(), key=lambda incident: incident.first_seen)


async def _report(settings: AmbientSettings, since: str, dry_run: bool) -> list[str]:
    now = datetime.now(timezone.utc)
    event_log = EventLog(settings.events_dir, retention_days=settings.event_retention_days)
    detector = build_detector(settings)
    start = now - parse_duration(since)
    incidents = await detect_over(detector, event_log.read(start, now), start, now)
    issues = GitHubIssues(settings.factory_repo, settings.github_token)
    reporter = IncidentReporter(issues, max_new_per_day=settings.factory_max_new_issues_per_day, dry_run=dry_run)
    print(f"{len(incidents)} incident(s) in the last {since} from {settings.events_dir}")
    return await reporter.sync(incidents)


def main(argv: list[str] | None = None) -> int:
    """Run detection once and file or preview GitHub issues for the result."""
    parser = argparse.ArgumentParser(description="Turn current incidents into GitHub issues (factory step 3).")
    parser.add_argument("--dry-run", action="store_true", help="print what would be posted; change nothing")
    parser.add_argument(
        "--since", default="24h", help="report incidents that happened in this lookback, e.g. 6h or 7d (default 24h)"
    )
    args = parser.parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = settings_from_env()
    if not args.dry_run and not settings.github_token:
        print("AMBIENT_GITHUB_TOKEN is not set; use --dry-run or add the token to .env", file=sys.stderr)
        return 2
    try:
        actions = asyncio.run(_report(settings, args.since, args.dry_run))
    except Exception as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        return 1
    prefix = "[dry run] " if args.dry_run else ""
    for action in actions:
        print(f"{prefix}{action}")
    if not actions:
        print(f"{prefix}nothing to post for {settings.factory_repo}")
    return 0


async def _dispatch_once(dispatcher: Dispatcher, show_prompt: bool) -> list[str]:
    if show_prompt:
        for issue in await dispatcher.issues.list_issues(READY):
            if issue.state == "open":
                print(f"----- prompt for #{issue.number} -----")
                print(build_fix_prompt(issue, dispatcher.issues.repo, dispatcher.outpost))
    return await dispatcher.poll_once()


def dispatch_main(argv: list[str] | None = None) -> int:
    """Run one dispatch pass: start a cloud Devin fix session for each ready issue (factory step 4)."""
    parser = argparse.ArgumentParser(description="Start Devin fix sessions for ready GitHub issues (factory step 4).")
    parser.add_argument("--dry-run", action="store_true", help="print what would be started; change nothing")
    parser.add_argument("--show-prompt", action="store_true", help="also print the prompt each session would get")
    args = parser.parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = settings_from_env()
    dispatcher = build_dispatcher(settings, dry_run=True) if args.dry_run else _forced_dispatcher(settings)
    if dispatcher is None:
        print("needs AMBIENT_GITHUB_TOKEN, DEVIN_API_KEY and DEVIN_ORG_ID in .env (or use --dry-run)", file=sys.stderr)
        return 2
    try:
        actions = asyncio.run(_dispatch_once(dispatcher, args.show_prompt))
    except Exception as exc:
        print(f"dispatch failed: {exc}", file=sys.stderr)
        return 1
    prefix = "[dry run] " if args.dry_run else ""
    for action in actions:
        print(f"{prefix}{action}")
    if not actions:
        print(f"{prefix}nothing to dispatch for {settings.factory_repo}")
    return 0


def _forced_dispatcher(settings: AmbientSettings) -> Dispatcher | None:
    return build_dispatcher(replace(settings, factory_dispatch=True))


if __name__ == "__main__":
    raise SystemExit(main())
