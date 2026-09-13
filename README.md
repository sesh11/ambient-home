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
10. Devin dispatch and UI are intentionally deferred.

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
| `AMBIENT_VOICE` | `marin` | Live voice |
| `AMBIENT_WAKE_WORD` | `hey_jarvis` | openWakeWord model |
| `AMBIENT_WAKE_THRESHOLD` | `0.5` | Wake confidence threshold |
| `AMBIENT_PREROLL_SECONDS` | `2.0` | Audio retained before wake |
| `AMBIENT_IDLE_TIMEOUT_S` | `60` | Idle close cap |
| `AMBIENT_MAX_SESSION_S` | `600` | Per-session cap |
| `AMBIENT_DAILY_BUDGET_S` | `3600` | Daily audio budget |
| `AMBIENT_DATA_DIR` | `~/.ambient-home` | Spend log directory |
| `AMBIENT_TRANSCRIPT_FINALIZE_S` | `1.0` | Transcript debounce |

## Not yet

Devin job dispatch and the companion UI are coming later. Barge-in audio queue
flushing is also pending hardware verification.
