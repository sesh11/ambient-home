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

At startup the assistant probes the microphone and, if it is returning only
zeros, reboots Reachy's XMOS audio processor once before listening (disable
with `AMBIENT_MIC_AUTORECOVER=false`). `uv run ambient-mic-check` runs the
same probe standalone.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | required to wake | OpenAI API credential |
| `AMBIENT_LIVE_MODEL` | `gpt-live-1` | Live model |
| `AMBIENT_BACKEND_MODEL` | `gpt-5.6-terra` | Responses delegation model |
| `AMBIENT_VISION_MODEL` | backend model | Vision tool model |
| `AMBIENT_VOICE` | `ballad` | Live voice |
| `AMBIENT_LOG_LEVEL` | `INFO` | Application log level |
| `AMBIENT_WAKE_WORD` | `hey_jarvis` | openWakeWord model |
| `AMBIENT_WAKE_THRESHOLD` | `0.5` | Wake confidence threshold |
| `AMBIENT_PREROLL_SECONDS` | `2.0` | Audio retained before wake |
| `AMBIENT_IDLE_TIMEOUT_S` | `60` | Idle close cap |
| `AMBIENT_MAX_SESSION_S` | `600` | Per-session cap |
| `AMBIENT_DAILY_BUDGET_S` | `3600` | Daily audio budget |
| `AMBIENT_DATA_DIR` | `~/.ambient-home` | Spend log directory |
| `AMBIENT_TRANSCRIPT_FINALIZE_S` | `1.0` | Transcript debounce |
| `DEVIN_API_KEY` | unset | Devin service-user API key (`cog_…`) |
| `DEVIN_ORG_ID` | unset | Devin organization ID (`org-…`, from app.devin.ai → Settings → Organization); required together with a `cog_` service-user API key |
| `DEVIN_API_BASE_URL` | `https://api.devin.ai` | Devin API base URL |
| `AMBIENT_DEVIN_MAX_ACU` | `5` | Maximum ACU per Devin session |
| `AMBIENT_LIVE_USD_PER_MINUTE` | `0.3` | Dollar estimate per Live audio minute |
| `AMBIENT_ACU_USD` | `2.25` | Dollar estimate per worker ACU |
| `AMBIENT_COST_HISTORY_DAYS` | `7` | Days of usage history charted in the UI |
| `AMBIENT_JOB_POLL_S` | `20` | Job refresh interval |
| `AMBIENT_UI_HOST` | `127.0.0.1` | Local status UI bind address |
| `AMBIENT_UI_PORT` | `8765` | Local status UI port |
| `AMBIENT_MIC_AUTORECOVER` | `true` | Reboot the XMOS audio processor at startup if the mic is silent |

Set `AMBIENT_WAKE_WORD=hey_alfred` to use the bundled custom wake-word model.

## Jobs and status UI

Say “dispatch a coding task” to hand work to Devin. Jobs are PR-only: Devin
must not merge, push directly to `main`/`master`, deploy, or change branch
protection or CI security settings. No secrets are shared with Devin by
default. Blocked jobs can be answered by voice with `answer_job` or in the UI.

With the app running, open <http://127.0.0.1:8765/> for live state, daily
usage, job status, pull-request links, and a stop-session button. Cancelling a
job through an API is not implemented in this MVP.

## Costs

The dashboard charts daily usage over the last `AMBIENT_COST_HISTORY_DAYS`
days: Live audio minutes and worker ACUs per day, with dollar estimates from
`AMBIENT_LIVE_USD_PER_MINUTE` and `AMBIENT_ACU_USD`, plus today's totals and
the window total. Ask “what have we spent today?” for the same numbers by
voice. Rates are local estimates only — no usage data leaves the machine.

## Not yet

TTS announcements while asleep, provider workers other than Devin, and remote
job cancellation are not yet implemented. Barge-in audio queue flushing is
also pending hardware verification.

Hardware verification still needed: microphone/speaker behavior, AEC,
barge-in flushing, direction of arrival, a real OpenAI Live session, and a
real Devin dispatch.

### Microphone returns silence

Reachy Mini Lite's XMOS audio processor can stream all-zero microphone frames
after a USB connect or power cycle until it is rebooted — the known
[Reachy Mini XMOS startup issue](https://github.com/pollen-robotics/reachy_mini/issues/770).
The assistant recovers from this automatically at startup and logs
`Microphone recovered after audio processor restart`. To reproduce or recover
by hand:

```bash
uv run ambient-mic-check                # expect verdict=silent when faulted
uv run ambient-mic-check --reboot-xmos  # reboots the chip, expect verdict=ok
```

If the microphone is still silent after the reboot, check the microphone FPC
cable per Pollen's troubleshooting guide rather than tuning the wake word.
On macOS, the daemon may need an explicit serial port:

```bash
ls /dev/cu.usbmodem*
reachy-mini-daemon --serialport /dev/cu.usbmodem…
```
