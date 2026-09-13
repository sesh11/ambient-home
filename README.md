# ambient-home

Ambient voice assistant for Reachy Mini Lite, using Pollen's conversation app as
the local robot/media/tools substrate and OpenAI GPT-Live as its brain.

## Architecture

1. Reachy Mini daemon provides robot motion and media.
2. Pollen's `LocalStream` captures and plays audio.
3. `GPTLiveHandler` owns wake-gated sessions.
4. OpenAI Live handles speech-to-speech conversation.
5. Responses delegation runs robot tools through Pollen's tool manager.
6. The local “hey jarvis” detector gates paid sessions.
7. A preroll buffer preserves speech immediately after wake.
8. Idle, session, and daily audio caps limit spend.
9. Robot tools remain Pollen-compatible external tools.
10. Devin dispatch and the loopback status UI run as background services.

## Install

Run `reachy-mini-daemon` first, then:

```bash
uv sync
cp .env.example .env
# edit OPENAI_API_KEY and any desired settings
uv run ambient-home
```

The Reachy Mini dependency currently requires Python 3.11 on Linux because its
TensorFlow Lite runtime has no Python 3.12 wheel; `.python-version` pins the
supported environment.

Simulator mode:

```bash
reachy-mini-daemon --sim
uv run ambient-home
```

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | required to wake | OpenAI API credential |
| `AMBIENT_LIVE_MODEL` | `gpt-live-1` | Live model |
| `AMBIENT_BACKEND_MODEL` | `gpt-5.6-terra` | Responses delegation model |
| `AMBIENT_VISION_MODEL` | backend model | Vision tool model |
| `AMBIENT_VOICE` | `ballad` | Live voice |
| `AMBIENT_WAKE_WORD` | `hey_jarvis` | openWakeWord model |
| `AMBIENT_WAKE_THRESHOLD` | `0.5` | Wake confidence threshold |
| `AMBIENT_PREROLL_SECONDS` | `2.0` | Audio retained before wake |
| `AMBIENT_IDLE_TIMEOUT_S` | `60` | Idle close cap |
| `AMBIENT_MAX_SESSION_S` | `600` | Per-session cap |
| `AMBIENT_DAILY_BUDGET_S` | `3600` | Daily audio budget |
| `AMBIENT_DATA_DIR` | `~/.ambient-home` | Spend log directory |
| `AMBIENT_TRANSCRIPT_FINALIZE_S` | `1.0` | Transcript debounce |
| `DEVIN_API_KEY` | unset | Devin API credential; jobs fail clearly when unset |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai` | Devin API base URL |
| `AMBIENT_DEVIN_MAX_ACU` | `5` | Maximum ACU per Devin session |
| `AMBIENT_JOB_POLL_S` | `20` | Job refresh interval |
| `AMBIENT_UI_HOST` | `127.0.0.1` | Local status UI bind address |
| `AMBIENT_UI_PORT` | `8765` | Local status UI port |

Set `AMBIENT_WAKE_WORD=hey_alfred` to use the bundled custom wake-word model.

## Jobs and status UI

Say “dispatch a coding task” to hand work to Devin. Jobs are PR-only: Devin
must not merge, push directly to `main`/`master`, deploy, or change branch
protection or CI security settings. No secrets are shared with Devin by
default. Blocked jobs can be answered by voice with `answer_job` or in the UI.

With the app running, open <http://127.0.0.1:8765/> for live state, daily
usage, job status, pull-request links, and a stop-session button. Cancelling a
job through an API is not implemented in this MVP.

## Not yet

TTS announcements while asleep, provider workers other than Devin, and remote
job cancellation are not yet implemented. Barge-in audio queue flushing is
also pending hardware verification.

Hardware verification still needed: microphone/speaker behavior, AEC,
barge-in flushing, direction of arrival, a real OpenAI Live session, and a
real Devin dispatch.
