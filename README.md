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
| `AMBIENT_JOB_POLL_S` | `20` | Job refresh interval |
| `AMBIENT_UI_HOST` | `127.0.0.1` | Local status UI bind address |
| `AMBIENT_UI_PORT` | `8765` | Local status UI port |
| `AMBIENT_MIC_AUTORECOVER` | `true` | Reboot the XMOS audio processor at startup if the mic is silent |
| `AMBIENT_DAEMON_STATUS_URL` | `http://127.0.0.1:8000/api/daemon/status` | Reachy daemon status endpoint |
| `AMBIENT_DAEMON_WAIT_S` | `60` | How long to wait for the daemon at startup before exiting non-zero |
| `AMBIENT_DAEMON_WATCHDOG_S` | `5` | Daemon liveness poll interval while running |
| `AMBIENT_DAEMON_WATCHDOG_FAILURES` | `3` | Consecutive failed polls before the app exits for restart |

Set `AMBIENT_WAKE_WORD=hey_alfred` to use the bundled custom wake-word model.

## Jobs and status UI

Say “dispatch a coding task” to hand work to Devin. Jobs are PR-only: Devin
must not merge, push directly to `main`/`master`, deploy, or change branch
protection or CI security settings. No secrets are shared with Devin by
default. Blocked jobs can be answered by voice with `answer_job` or in the UI.

With the app running, open <http://127.0.0.1:8765/> for live state, daily
usage, job status, pull-request links, and a stop-session button. Cancelling a
job through an API is not implemented in this MVP.

## Run as a service (macOS)

Install both processes as per-user LaunchAgents so Reachy comes back on its
own after a reboot or a USB replug, with no terminal open:

```bash
cd ~/path/to/ambient-home      # the checkout with your .env
uv run ambient-install-service
```

This renders two plists from `src/ambient_home/launchd/` into
`~/Library/LaunchAgents/` and bootstraps them into `gui/$UID`:

| Agent | Runs | Log |
| --- | --- | --- |
| `ai.ambient-home.daemon` | `scripts/run-daemon.sh` → `reachy-mini-daemon --serialport /dev/cu.usbmodem…` | `~/Library/Logs/ambient-home/daemon.log` |
| `ai.ambient-home.app` | `uv run ambient-home` | `~/Library/Logs/ambient-home/app.log` |

Both use `RunAtLoad`, `KeepAlive` (`SuccessfulExit=false`) and a 10 s
`ThrottleInterval`. Secrets are not placed in the plists; the app reads `.env`
from the repo working directory as before.

How the pieces recover:

- `run-daemon.sh` resolves the serial port at start and exits `69` when no
  `/dev/cu.usbmodem*` exists, then exits `75` if the port vanishes mid-run.
  launchd restarts it after the throttle interval.
- `ambient-home` runs the mic check, then polls the daemon status endpoint
  until it reports `running` (up to `AMBIENT_DAEMON_WAIT_S`) before connecting
  to Reachy; it exits `69` on timeout. While running, a watchdog polls the
  daemon every `AMBIENT_DAEMON_WATCHDOG_S` and exits `75` after
  `AMBIENT_DAEMON_WATCHDOG_FAILURES` consecutive failures so launchd restarts
  it, which re-runs the XMOS mic recovery.

Useful commands:

```bash
tail -f ~/Library/Logs/ambient-home/app.log ~/Library/Logs/ambient-home/daemon.log
launchctl print gui/$UID/ai.ambient-home.app | head -20      # state, pid, last exit
launchctl kickstart -k gui/$UID/ai.ambient-home.app          # restart the app now
launchctl bootout gui/$UID/ai.ambient-home.app               # stop temporarily
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.ambient-home.app.plist  # start again
uv run ambient-uninstall-service                             # remove both agents
uv run ambient-install-service --dry-run                     # preview the plists
```

Stop the app agent (`bootout`) before running `uv run ambient-home` by hand,
otherwise two copies compete for the microphone and the status UI port.

Replug acceptance test: with both agents loaded, unplug Reachy and plug it back
in. Expect in `daemon.log`: `serial port ... vanished` followed within ~10–20 s
by `starting reachy-mini-daemon on /dev/cu.usbmodem…`. Expect in `app.log`:
`Reachy daemon check failed (3/3 ...)`, `Exiting with status 75`, then on the
restart `Microphone check: ok` (or `Microphone recovered after audio processor
restart`), `Reachy daemon ready`, and the asleep heartbeat. Reachy should be
listening for the wake word within about 30 s of the replug.

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
