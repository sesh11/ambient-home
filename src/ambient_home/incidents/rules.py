"""Pure TOML-configured incident rules."""

import logging
import tomllib
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from ambient_home.incidents.base import (
    Incident,
    Severity,
    parse_duration,
    make_fingerprint,
    normalize_message,
)
from ambient_home.telemetry.events import Event


logger = logging.getLogger(__name__)
Scalar = str | int | float | bool | None


class Rule(BaseModel):
    """Validated rule configuration."""

    name: str
    title: str
    severity: Severity
    types: list[str]
    where: dict[str, Scalar] = {}
    group_by: list[str] = []
    threshold: int
    window: str
    cleared_by: str | None = None
    clear_within: str | None = None

    @field_validator("window")
    @classmethod
    def _valid_window(cls, value: str) -> str:
        parse_duration(value)
        return value

    @field_validator("clear_within")
    @classmethod
    def _valid_clear_within(cls, value: str | None) -> str | None:
        if value is not None:
            parse_duration(value)
        return value

    @model_validator(mode="after")
    def _require_clear_window(self) -> "Rule":
        if self.cleared_by and not self.clear_within:
            raise ValueError("clear_within is required when cleared_by is configured")
        if self.threshold < 1:
            raise ValueError("threshold must be at least 1")
        if not self.types:
            raise ValueError("types must not be empty")
        return self


class RuleDetector:
    """Detect incidents from event types, filters, and rolling windows."""

    name = "rules"

    def __init__(self, rules: list[Rule]) -> None:
        """Create a detector from validated rules."""
        self.rules = rules

    @classmethod
    def from_file(cls, path: Path) -> "RuleDetector":
        """Load and validate rules from a TOML file."""
        try:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
            raw_rules = payload.get("rule")
            if not isinstance(raw_rules, list):
                raise ValueError("expected one or more [[rule]] tables")
            rules = [Rule.model_validate(item) for item in raw_rules]
            return cls(rules)
        except (OSError, tomllib.TOMLDecodeError, ValidationError, TypeError) as exc:
            raise ValueError(f"invalid incident rules file {path}: {exc}") from exc

    async def detect(self, events: Sequence[Event], now: datetime) -> list[Incident]:
        """Evaluate rules over an immutable event snapshot."""
        results: list[Incident] = []
        end = now.astimezone()
        severity_rank: dict[Severity, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        for rule in self.rules:
            start = end - parse_duration(rule.window)
            rule_events = [event for event in events if start <= event.ts.astimezone() <= end]
            matching = [
                event
                for event in rule_events
                if event.type in rule.types and all(event.data.get(key) == value for key, value in rule.where.items())
            ]
            clear_events = [event for event in events if event.type == rule.cleared_by] if rule.cleared_by else []
            groups: dict[tuple[tuple[str, str], ...], list[Event]] = defaultdict(list)
            group_values: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}
            for event in matching:
                group: dict[str, str] = {}
                for key in rule.group_by:
                    if key == "message":
                        raw = event.data.get("error") or event.data.get("message") or ""
                        group[key] = normalize_message(str(raw))
                    else:
                        value = event.data.get(key)
                        group[key] = "" if value is None else str(value)
                group_key = tuple(sorted(group.items()))
                groups[group_key].append(event)
                group_values[group_key] = group
            clear_window = parse_duration(rule.clear_within) if rule.clear_within else timedelta()
            for group_key, group_matches in groups.items():
                settled: list[Event] = []
                for match in group_matches:
                    if rule.cleared_by:
                        if any(match.ts < clear.ts <= match.ts + clear_window for clear in clear_events):
                            continue
                        if end < match.ts + clear_window:
                            continue
                    settled.append(match)
                if not settled:
                    continue
                settled.sort(key=lambda event: event.ts)
                group = group_values[group_key]
                try:
                    title = rule.title.format(**group)
                except (KeyError, ValueError) as exc:
                    logger.debug("Could not format title for incident rule %s: %s", rule.name, exc)
                    title = rule.title
                count = len(settled)
                results.append(
                    Incident(
                        fingerprint=make_fingerprint(rule.name, group),
                        rule=rule.name,
                        title=title,
                        severity=rule.severity,
                        count=count,
                        threshold=rule.threshold,
                        window_s=parse_duration(rule.window).total_seconds(),
                        first_seen=settled[0].ts,
                        last_seen=settled[-1].ts,
                        ready=count >= rule.threshold,
                        group=group,
                        samples=settled[-20:],
                        detector=self.name,
                    )
                )
        results.sort(
            key=lambda incident: (incident.ready, severity_rank[incident.severity], incident.last_seen),
            reverse=True,
        )
        return results
