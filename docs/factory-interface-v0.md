# Factory interface v0 (draft spec)

Status: **draft for review**. Nothing here is built yet. Today's code is referenced
where it already does part of the job.

## 1. What the factory is

Three kinds of tools already exist around a running app:

| Tool | Takes you from | Examples |
|---|---|---|
| Observability | running app → signals (logs, traces, errors) | OpenTelemetry, Sentry, Datadog |
| Coding agents | a task → a pull request | Devin, Codex, Copilot cloud agent |
| CI/CD | code → a release | GitHub Actions, release scripts |

Nothing standard owns the full way back:

```
signals → a good task → an agent → proof it works → release → confirmed fixed in the real world
```

Today a person does that by hand: reads the crash, writes the ticket, picks the agent,
tests the fix (sometimes on real hardware), ships it, and watches whether the problem stopped.

**The factory is that missing middle: a control loop between a running app and coding agents.**
It keeps comparing "what should be true" (no open incidents, requested features done) with
"what is happening" (the app's events), and sends out work until the two match.

```
observe → detect → report → fix → test → release → confirm
   ^                                                  |
   +-------------- reopen if it comes back -----------+
```

Some vendors cover parts of this (checked 2026-10, from their public docs):
- Sentry Seer Autofix can go from a Sentry issue to a drafted PR.
- Datadog Bits Code turns Datadog signals into fix PRs.
- GitHub lets you assign an issue to the Copilot cloud agent, which opens a PR.

Each is tied to its own vendor's signals or agent, and each stops at the PR. None of them
(as far as their docs say) picks between agents, requires a specific test kit such as a
hardware test, or checks after release that the problem actually stopped. The factory
aims to be the vendor-neutral loop that uses these tools as plug-ins rather than replacing them.

### Words used here

- **App**: any program the factory looks after (ambient-home is the first).
- **Install**: one copy of an app running on one machine.
- **Event**: one line the app writes about something that happened.
- **Incident**: a problem the factory found by grouping events (e.g. "mic silent 3 times in 6 h").
- **Request**: a feature or fix someone asked for (by voice, issue, or "report a problem").
- **Work order**: the task packet the factory hands to a coding agent.
- **Test kit**: the app's own check command that proves a change works (e.g. `ambient-hw-test`).
- **Outpost**: a machine with special hardware where a test kit must run (e.g. the Mac with the robot).
- **Connector**: a small plug-in that lets the factory talk to one outside system (GitHub, Devin, Sentry...).

## 2. The four contracts

An app plugs into the factory by meeting four contracts. The factory never imports app code.

| # | Contract | Direction | Who writes it |
|---|---|---|---|
| 1 | Signals | app → factory | the app, using `factory-sdk` |
| 2 | Manifest (`factory.toml`) | app → factory | the app's repo, once |
| 3 | Work order | factory → agent | the factory |
| 4 | Proof | agent / test kit / app → factory | test kit output, then new events |

### Contract 1: Signals (events)

Every event is one JSON object per line. The shape is a small subset of the
[OpenTelemetry logs data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/),
so apps that already use OpenTelemetry can send the same thing over OTLP instead of a file.

```json
{
  "ts": "2026-10-26T13:24:05.120Z",
  "type": "mic.silent_stream",
  "component": "audio",
  "level": "error",
  "resource": {"app": "ambient-home", "version": "0.2.0+8bc8ef5", "install": "a1b2c3d4"},
  "data": {"device": "reachy", "seconds": 12.5}
}
```

| Field | Required | Meaning | OpenTelemetry field |
|---|---|---|---|
| `ts` | yes | UTC time, timezone-aware | `Timestamp` |
| `type` | yes | dotted event name, stable over time | `EventName` |
| `component` | yes | part of the app that emitted it | `InstrumentationScope.name` |
| `level` | yes | `info` / `warning` / `error` | `SeverityText` |
| `resource.app` | yes | app id, same as in `factory.toml` | `service.name` |
| `resource.version` | yes | exact running version (see below) | `service.version` |
| `resource.install` | yes | random id per install, not tied to a person | `service.instance.id` |
| `data` | no | flat key → string/number/bool/null | `Attributes` |

Rules:
- **Scrubbed before writing.** Secrets, tokens, emails, home paths and free text from users are
  removed or masked by the SDK, not by the factory. Anything in the log may end up in a public issue.
- **Local by default.** Events go to a day-partitioned JSONL folder on the install's own machine,
  kept for a limited time (ambient-home: 14 days). Nothing is sent anywhere unless the user opts in.
- **Version on every event**, so the factory can tell "before the fix" from "after the fix".
  Format: the package version plus the short git commit, e.g. `0.2.0+8bc8ef5`.

Standard event types every app should emit (the factory relies on these):

| Type | When | Needed `data` |
|---|---|---|
| `app.started` | process start | — |
| `app.heartbeat` | every N minutes while healthy | `uptime_s` |
| `app.stopped` | clean exit | `exit_code` |
| `app.crashed` | uncaught exception | `exc_type`, `where` (module:function) |
| `request.made` | a user asked for a feature or fix | `summary` (scrubbed), `source` |

Heartbeats matter for **confirm**: "no incident since the release" only means something if
the new version was actually running.

Everything else (`mic.silent_stream`, `live.api_error`, `tool.completed`...) is app-specific
and is only given meaning by the app's detection rules in the manifest.

### Contract 2: Manifest (`factory.toml`)

One file at the root of the app's repo. It tells the factory everything app-specific, so the
factory code stays generic. Example for ambient-home:

```toml
[app]
id = "ambient-home"
repo = "github.com/sesh11/ambient-home"

[build]
setup = "uv sync"
check = ["uv run ruff check .", "uv run mypy", "uv run pytest tests/ -q"]

[signals]
source = "jsonl"                      # or "otlp", "sentry"
path = "~/.ambient-home/telemetry"    # per-install folder

[detect]
rules = "factory/rules.toml"          # today: src/ambient_home/incident_rules.toml

[[test_kit]]
name = "robot"
command = "uv run ambient-hw-test --json report.json"
runs_on = "outpost:my-mac"            # needs the real robot
when = "always"                       # or a list of paths, e.g. ["src/ambient_home/audio/**"]
max_minutes = 3
costs_money = false

[release]
command = "scripts/build-release.sh"  # issue #30
version_from = "pyproject.toml"

[confirm]
quiet_for = "24h"                     # incident must not recur on the new version for this long
min_heartbeats = 12                   # and the new version must really have run

[policy]
agents = ["devin", "codex"]           # in order of preference
max_concurrent = 1
needs_approval = ["release", "request"]   # steps a human must approve
```

### Contract 3: Work order

What the factory hands to a coding agent. It is agent-neutral JSON; each agent connector turns
it into that agent's prompt or task format (a Devin session, a Codex task, a GitHub issue
assigned to Copilot...).

```json
{
  "id": "wo-ambient-home-42-1",
  "app": "ambient-home",
  "repo": "github.com/sesh11/ambient-home",
  "kind": "incident",
  "issue": "https://github.com/sesh11/ambient-home/issues/42",
  "title": "Microphone stream silent while running",
  "fingerprint": "mic_silent_stream:3f9a1c20aa",
  "seen_in_version": "0.2.0+8bc8ef5",
  "evidence": [ {"ts": "...", "type": "mic.silent_stream", "data": {"seconds": 12.5}} ],
  "goal": "The mic keeps delivering audio; no mic.silent_stream events on the new version.",
  "must_pass": ["build.check", "test_kit:robot"],
  "constraints": {"base_branch": "main", "never_merge": true, "max_cost": "policy default"},
  "report_back": "contract 4 agent result"
}
```

- `kind` is `incident` or `request`. A request has a `request` text instead of `evidence`,
  and the agent posts a plan and waits for approval before building.
- `evidence` is copied from scrubbed events only.
- `must_pass` comes from the manifest: the factory, not the agent, decides which test kits count.

### Contract 4: Proof

Proof comes back in three stages. An issue closes only after all three.

**4a. Agent result** (structured output from the agent; today's `OUTPUT_SCHEMA` in
`src/ambient_home/factory/dispatch.py`, generalized):

```json
{"work_order": "wo-ambient-home-42-1", "summary": "...", "pr_url": "https://...", "risk": "low"}
```

**4b. Test-kit report** (JSON written by the test kit; today's `ambient-hw-test --json` payload):

```json
{"ok": true, "run_id": "...", "version": "0.2.0+1a2b3c4", "seconds": 118.0,
 "checks": [{"name": "mic", "status": "pass", "seconds": 70.2, "detail": "..."}]}
```

Exit codes: `0` pass, `1` fail, `2` could not run (e.g. robot unplugged). "Could not run" is
never treated as a pass.

**4c. Confirmation** (computed by the factory from contract 1 events, after release):
- events with the new `resource.version` exist, with at least `min_heartbeats` heartbeats, and
- the incident's rule did not fire on the new version for `quiet_for`.

If both hold, the issue closes as confirmed. If the incident fires again on the new version,
the issue reopens with the new evidence.

## 3. The issue lifecycle

The tracker (GitHub issues today) is where people see the loop. Labels already used by the
factory are kept; v0 adds the last three.

```
factory:observed → factory:ready-for-fix → factory:dispatched / factory:fixing
    → factory:testing → factory:released → closed (confirmed)
                                         ↘ reopened (came back on new version)
```

## 4. What goes where

### `factory-sdk` (small library every app installs)

Kept tiny and low-dependency, because every app and every end-user install pulls it in.

- `Event` model and `EventLog` writer (from `src/ambient_home/telemetry/events.py`)
- scrubbing (from `src/ambient_home/telemetry/scrub.py`)
- version stamp and install id
- standard event helpers (`app.started`, heartbeat, crash hook, `request.made`)
- test-kit report helper (write contract 4b JSON + exit codes)
- schemas for `factory.toml`, work orders and proof, so apps and the factory agree on one copy
- later: opt-in "report a problem", which turns local events into a scrubbed issue on the app's repo

The SDK never talks to an agent or a tracker by itself.

### factory service (the loop)

Runs wherever the app's maintainer runs it (for ambient-home: Nithin's Mac, reading his
install's log). Not installed by end users.

- loop: observe → detect → report → fix → test → release → confirm
- detectors (from `src/ambient_home/incidents/`)
- connectors, each behind a small interface:
  - **sources**: local JSONL, OTLP, Sentry
  - **trackers**: GitHub (from `src/ambient_home/factory/github.py`), later Linear
  - **agents**: Devin (from `src/ambient_home/factory/dispatch.py`), later Codex
  - **test runners**: cloud VM, outpost
- reads each app's `factory.toml`; no app-specific code inside

### The app (ambient-home)

- uses `factory-sdk` to write events and its test-kit report
- ships `factory.toml` and its detection rules
- keeps its own voice-job Devin client for "Alfred, do X" jobs; that is a product feature,
  separate from factory fix sessions

## 5. End-user installs

End users run the app and the SDK only. Their events stay on their machine. If something
breaks, they report it: today by opening an issue by hand, later with the SDK's opt-in
"report a problem" that files a scrubbed issue in the same shape as an incident. The
maintainer's factory then treats it like any other incident. There is no hidden telemetry.

## 6. Gaps in today's code

| Contract | Today | Gap |
|---|---|---|
| 1 Signals | scrubbed day-partitioned JSONL (`telemetry/events.py`) | no `resource` block (app, version, install id); no standard `app.*` types |
| 2 Manifest | settings spread over `.env`, `incident_rules.toml`, and hard-coded outpost steps in `factory/dispatch.py` | no `factory.toml` |
| 3 Work order | Devin prompt text built in `build_fix_prompt` | not agent-neutral; no `must_pass` list |
| 4 Proof | `OUTPUT_SCHEMA` and `ambient-hw-test --json` | no version in the test report; no release step; no confirm step |

## 7. Open questions

1. Event transport: keep JSONL as the default and add OTLP as a source later (proposed), or
   adopt the OpenTelemetry SDK from day one?
2. Should `factory-sdk` live in the factory repo (proposed, published as its own package) or
   in its own repo?
3. Who approves a release in v0: always Nithin (proposed), or auto-release when all `must_pass`
   kits pass and risk is `low`?
4. Name for the factory repo and package.

## 8. Next steps (after this spec is agreed)

1. Add the `resource` block and standard `app.*` events to ambient-home (no behavior change).
2. Write ambient-home's `factory.toml` and move outpost steps out of `dispatch.py` into it.
3. Create the factory repo; move `factory/`, `incidents/` and `telemetry/` there as the
   service and `factory-sdk`, keeping git history.
4. Add `factory:testing`, `factory:released` and the confirm step.
