# ambient-home

Ambient-home is a local voice-assistant hub. It connects device plugins through
`ambient-device-sdk`, routes audio through one wake-gated conversation, and
provides a local status UI, job board, and usage estimates. The OpenAI voice
connector and Devin coding worker are adapters; the hub can also start with no
device configured.

## Install and run

```bash
uv sync
cp .env.example .env
# Set OPENAI_API_KEY and any desired options in .env.
uv run ambient-home
```

Open <http://127.0.0.1:8765/> for live state, configured devices, jobs, and
costs. To configure devices, copy `devices.example.toml` to
`~/.ambient-home/devices.toml` (or set `AMBIENT_DEVICES_FILE`) and install the
corresponding device connector. Device connectors live in the
[ambient-devices](https://github.com/sesh11/ambient-devices) project and can be
developed independently against `ambient-device-sdk`; install `ambient-reachy`
using the pin in that project's README for the current devices branch. Device configuration
uses `[[devices]]` tables; `devices.example.toml` contains an optional
Reachy Mini Lite example and its config keys.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | unset | Required to open a Live conversation; clients are created lazily |
| `AMBIENT_LIVE_MODEL` | `gpt-live-1` | Realtime voice model |
| `AMBIENT_BACKEND_MODEL` | `gpt-5.6-terra` | Responses delegation model |
| `AMBIENT_VISION_MODEL` | backend model | Vision model |
| `AMBIENT_VOICE` | `ballad` | Default Live voice |
| `AMBIENT_PROFILE` | `alfred` | Persona profile under `profiles/` |
| `AMBIENT_PROFILES_DIR` | packaged profiles | Optional profile directory |
| `AMBIENT_WAKE_WORD` | `hey_jarvis` | Local openWakeWord model |
| `AMBIENT_WAKE_THRESHOLD` | `0.5` | Wake confidence threshold |
| `AMBIENT_PREROLL_SECONDS` | `2.0` | Audio retained before wake |
| `AMBIENT_IDLE_TIMEOUT_S` | `60` | Idle session limit |
| `AMBIENT_MAX_SESSION_S` | `600` | Maximum session length |
| `AMBIENT_DAILY_BUDGET_S` | `3600` | Daily Live audio budget |
| `AMBIENT_DATA_DIR` | `~/.ambient-home` | Local jobs, spend, and device configuration |
| `AMBIENT_MEMORY_FILE` | `$XDG_DATA_HOME/reachy_mini_conversation_app/memory.v1.json`, else `~/.local/share/reachy_mini_conversation_app/memory.v1.json` | Optional override for the persistent Alfred memory file |
| `AMBIENT_DEVICES_FILE` | `<data dir>/devices.toml` | TOML device plugin configuration |
| `AMBIENT_TRANSCRIPT_FINALIZE_S` | `1.0` | Transcript debounce interval |
| `DEVIN_API_KEY` | unset | Devin service-user API key |
| `DEVIN_ORG_ID` | unset | Devin organization ID |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai` | Devin API base URL |
| `AMBIENT_DEVIN_MAX_ACU` | `5` | Maximum ACUs per Devin session |
| `AMBIENT_GARAGE_URL` | unset | Required for Alfred to file software work with Garage; local Garage defaults to `http://127.0.0.1:8770` |
| `AMBIENT_GARAGE_TOKEN` | unset | Optional bearer token for the local Garage service |
| `AMBIENT_GARAGE_REPOS` | `sesh11/ambient-home` | Repositories Garage handles for Alfred |
| `AMBIENT_EVENT_RESOURCE` | `false` | Add local install/version identity to events |
| `AMBIENT_EVENTS_DIR` | `~/Library/Logs/ambient-home/events` | Local JSONL event directory |
| `AMBIENT_EVENT_RETENTION_DAYS` | `14` | Event retention |
| `AMBIENT_HEARTBEAT_S` | `300` | Application heartbeat interval |
| `AMBIENT_LIVE_USD_PER_MINUTE` | `0.3` | Local Live audio cost estimate |
| `AMBIENT_ACU_USD` | `2.25` | Local worker ACU cost estimate |
| `AMBIENT_COST_HISTORY_DAYS` | `7` | Cost history shown in the UI |
| `AMBIENT_JOB_POLL_S` | `20` | Job refresh interval |
| `AMBIENT_UI_HOST` | `127.0.0.1` | UI bind address |
| `AMBIENT_UI_PORT` | `8765` | UI port |
| `AMBIENT_LOG_LEVEL` | `INFO` | Application log level |

## Behavior and privacy

Wake-word detection, the device gateway, session limits, and the status UI run
locally. The core writes structured events to Garage SDK's local JSONL event
store. Events contain operational metadata; transcripts, audio, Live payloads,
and request text are not logged. With `AMBIENT_GARAGE_URL` configured, Alfred
files software-engineering work with Garage by default and Garage dispatches
the work. `dispatch_job` starts a direct coding session only when the user
explicitly asks for one. If Garage is not configured or cannot accept a request,
Alfred tells the user and asks before starting a direct session.

Jobs are PR-only: Devin must not merge, push directly to `main`/`master`, deploy,
or change branch protection or CI security settings. No secrets are shared with
Devin by default. Blocked jobs can be answered by voice or in the UI. The
dashboard reports local cost estimates for Live minutes and worker ACUs; the
configured budgets limit Live session use.

The `garage/` directory contains the app-specific Garage rules and draft product
brief. The architecture and package boundaries are described in
[`docs/architecture.md`](docs/architecture.md).

## Service and development

On macOS, `uv run ambient-install-service` installs the app LaunchAgent;
`uv run ambient-uninstall-service` removes only the ambient-home app agent. The
device project owns its daemon and device services.

Run the test suite with `uv run pytest -q`. Real devices and provider APIs are
not needed for tests; provider calls use fakes or HTTP mocks.

### Testing a feature branch

Use a separate worktree so the always-on checkout is never switched:

```bash
git -C ~/ambient-home fetch origin devin/1793294366-core-split
git -C ~/ambient-home worktree add ~/ambient-home-core-split origin/devin/1793294366-core-split
cd ~/ambient-home-core-split && uv sync
```

When finished, remove the feature worktree:

```bash
git -C ~/ambient-home worktree remove ~/ambient-home-core-split
```
