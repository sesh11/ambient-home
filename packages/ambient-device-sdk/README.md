# ambient-device-sdk

The interface every ambient-home device connector implements: Reachy Mini Lite, a Mac mic and
speaker, Meta Muse, or your own device. Pure Python standard library (3.11+), no dependencies.
Audio is plain PCM16 `bytes`, never numpy arrays.

Version `0.1.0`, device protocol version `1` (`ambient_device_sdk.PROTOCOL_VERSION`).

## Install

From GitHub (no PyPI). Pin a tag or commit for anything you ship:

```bash
pip install "ambient-device-sdk @ git+https://github.com/sesh11/ambient-home.git@<ref>#subdirectory=packages/ambient-device-sdk"
uv add "ambient-device-sdk @ git+https://github.com/sesh11/ambient-home.git@<ref>#subdirectory=packages/ambient-device-sdk"
```

In a `pyproject.toml`:

```toml
dependencies = [
    "ambient-device-sdk @ git+https://github.com/sesh11/ambient-home.git@<ref>#subdirectory=packages/ambient-device-sdk",
]
```

## Imports

Import from the top-level package only. `ambient_device_sdk.testing` is the one public
submodule; everything else (`types`, `device`, `plugin`, `_version`) is private layout.

```python
from ambient_device_sdk import (
    Device, DeviceContext, DevicePlugin, Capability, AudioFormat,
    DeviceHealth, HealthStatus, SessionPhase, SessionState, ToolSpec,
    EventSink, DescribeImage, PROTOCOL_VERSION, ENTRY_POINT_GROUP,
)
from ambient_device_sdk.testing import FakeDevice, check_device_contract, run_device_contract
```

## Types

| Type | Shape |
|---|---|
| `Capability` (StrEnum) | `LISTEN="listen"`, `SPEAK="speak"`, `MOVE="move"`, `SEE="see"`, `TEXT="text"` |
| `AudioFormat` | `sample_rate: int`, `channels: int = 1`. Always PCM16 signed little-endian, channels interleaved. Helpers: `bytes_per_frame`, `is_whole_frames(b)`, `duration(b)`, `byte_count(seconds)` |
| `HealthStatus` (StrEnum) | `OK="ok"`, `DEGRADED="degraded"`, `DOWN="down"` |
| `DeviceHealth` | `status`, `reason: str \| None` (short stable token, required unless OK), `detail: Mapping[str, str\|int\|float\|bool\|None]`. Builders `ok(**detail)`, `degraded(reason, **detail)`, `down(reason, **detail)` |
| `SessionPhase` (StrEnum) | `WAKE="wake"`, `LISTENING="listening"`, `SPEAKING="speaking"`, `IDLE="idle"`, `INTERRUPTED="interrupted"`, `SLEEP="sleep"` |
| `SessionState` | `phase`, `session_id: str`, `listening: bool = False`, `speaking: bool = False`, `reason: str \| None = None`. Builders `wake(id)`, `active(id, listening=, speaking=)`, `interrupted(id)`, `sleep(id, reason)` |
| `ToolSpec` | `name` (`^[A-Za-z0-9_-]{1,64}$`), `description` (non-empty), `parameters` (JSON Schema, root `"type": "object"`), `handler: async (dict[str, object]) -> dict[str, object]`. `schema()` gives the model-facing dict without the handler |
| `EventSink` (Protocol) | `emit(type, component, level="info", /, **data)`; `data` values are scalars |
| `DescribeImage` | `async (jpeg: bytes, question: str) -> str` |
| `DeviceContext` | `device_id: str`, `config: Mapping[str, object] = {}`, `emit: EventSink = null_event_sink`, `describe_image: DescribeImage \| None = None` |
| `DevicePlugin` | `kind: str` (`^[a-z0-9][a-z0-9_-]{0,63}$`), `create: (DeviceContext) -> Device`, `protocol_version: int = PROTOCOL_VERSION`, `description: str = ""` |
| `DeviceConfig` | `id`, `kind` (same pattern as kind), `enabled: bool = True`, `config: Mapping[str, object] = {}` |

## The `Device` protocol

```python
class Device(Protocol):
    id: str                                   # read-only; == DeviceContext.device_id
    kind: str                                 # read-only; == DevicePlugin.kind
    capabilities: frozenset[Capability]       # fixed for the device's lifetime
    mic_format: AudioFormat | None            # set iff LISTEN
    speaker_format: AudioFormat | None        # set iff SPEAK

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> DeviceHealth: ...
    def mic(self) -> AsyncIterator[bytes]: ...           # LISTEN
    async def play(self, pcm16: bytes) -> None: ...      # SPEAK
    async def stop_playback(self) -> None: ...           # SPEAK
    def requests(self) -> AsyncIterator[str]: ...        # TEXT
    def tools(self) -> Sequence[ToolSpec]: ...
    async def on_session(self, state: SessionState) -> None: ...
```

The attributes may be plain attributes or read-only properties. `Device` is
`runtime_checkable`, so `isinstance(obj, Device)` checks that every member exists.

### Rules for each method

- **Construction** (`DevicePlugin.create(context)`): cheap and side-effect free. No hardware
  access, threads, subprocesses or network. Validate `context.config` here and raise on bad
  settings.
- **`start()`**: connect to the hardware (and any helper process it needs, e.g. the Reachy
  daemon, which the connector owns), then put the device in its rest pose (asleep). May raise;
  the hub logs it and retries later. May be called again after `stop()` (reconnect); each
  start gives fresh `mic()`/`requests()` streams.
- **`stop()`**: release everything, end the `mic()` and `requests()` iterators. Idempotent,
  never raises.
- **`health()`**: quick, never raises, works before `start()` and after `stop()`.
- **`mic()`** (LISTEN only): called once per `start()`. Yields PCM16 chunks in `mic_format`,
  whole frames only, any chunk size (20-100 ms is typical). Streams **continuously** from start
  to stop, also while no conversation is open: the hub runs the wake word and keeps the
  pre-wake (preroll) buffer on this stream. The iterator ending means the device dropped;
  the hub then treats it as disconnected. Mic fault recovery (e.g. the Reachy XMOS reboot) is
  the device's job; report it with events and `health()`.
- **`play(pcm16)`** (SPEAK only): queue audio in `speaker_format` and return without waiting
  for it to finish. The hub converts between the voice model's format and the device's.
- **`stop_playback()`** (SPEAK only): drop queued audio now. Idempotent.
- **`requests()`** (TEXT only): yields typed asks until `stop()`.
- **`tools()`**: called when a conversation opens on this device, right after
  `on_session(WAKE)`; the result is used for that conversation only. Return no tool that needs a
  missing dependency (e.g. no camera tool when `context.describe_image` is None).
- **`on_session(state)`**: only the device that owns the conversation gets these. See below.

The hub only calls `mic()`, `play()`/`stop_playback()` and `requests()` when the matching
capability is present; a device without it may raise `NotImplementedError` from them.

### Conversations (`SessionState`)

One conversation at a time across all devices. The first device whose audio wakes the hub
owns it; wakes from other devices are ignored and logged by the hub. For one `session_id`
the owner receives, in order and never concurrently:

1. exactly one `WAKE` (flags False): after the voice session has opened, before the
   preroll audio is sent; do the wake move (motors on, wake-up pose).
2. any number of `LISTENING` / `SPEAKING` / `IDLE` / `INTERRUPTED` updates, sent whenever a
   flag changes. `listening` and `speaking` are authoritative and map one-to-one to a body's
   listening/speaking animation; the phase is derived from them (SPEAKING if speaking, else
   LISTENING if listening, else IDLE). `INTERRUPTED` means the user talked over the assistant:
   `listening=True`, `speaking=False`, and `stop_playback()` has already been called.
3. exactly one `SLEEP` (flags False, `reason` = why it closed, e.g. `idle_timeout`): do the
   sleep move.

The hub awaits each call. Keep non-WAKE/SLEEP calls fast. Exceptions are caught and logged
by the hub, but the device should catch and `emit` its own failures (e.g. `robot.wake_failed`).
Phases a device doesn't care about can be ignored. New phases may be added within protocol 1;
ignore unknown ones.

### Tools

`ToolSpec` is the same type the hub uses for its own tools, so all tools reach the voice model
the same way. Handlers return a JSON-serialisable dict; on failure return
`{"error": "<message>"}`. The hub also turns a raised exception into `{"error": str(exc)}`.
A device tool whose name clashes with a core tool or another device's tool is skipped and logged
by the hub.

### Events (`context.emit`)

Devices record events through the injected `EventSink`, so they need neither garage-sdk nor
ambient-home. Same shape as garage-sdk `emit`: dotted `type`, `component`, `level`
(`info`/`warning`/`error`), scalar `data`. The hub's sink scrubs and writes to the install's
local log, and never raises. Never put transcripts, audio, images or other personal content in
`data`. Keep existing event names and components unchanged when moving code (e.g.
`robot.wake_failed`/`live`, `vision.failed`/`vision`, `mic.silent_stream`/`live`) so detection
rules keep matching. The default `null_event_sink` drops everything.

### Vision (`context.describe_image`)

Camera devices capture a JPEG and call `await context.describe_image(jpeg, question)`. The hub
owns the vision provider, model and API key. It raises on failure; the device catches it, emits
`vision.failed` (`component="vision"`, `level="error"`, `exc_type=<class name>`) and returns
`{"error": str(exc)}` from its tool.

## Registering a device (entry points)

A connector package exports one module-level `DevicePlugin` per device kind under the entry
point group `ambient_device_sdk.devices`. The entry point **name must equal the plugin's
`kind`**.

```toml
# the connector's pyproject.toml
[project.entry-points."ambient_device_sdk.devices"]
reachy-mini-lite = "ambient_reachy.plugin:PLUGIN"
```

```python
# ambient_reachy/plugin.py
from ambient_device_sdk import DeviceContext, DevicePlugin

def _create(context: DeviceContext) -> "ReachyDevice":
    from ambient_reachy.device import ReachyDevice  # heavy imports stay out of discovery
    return ReachyDevice(context)

PLUGIN = DevicePlugin(kind="reachy-mini-lite", create=_create, description="Reachy Mini Lite (USB)")
```

The hub never imports a connector package by name. It looks plugins up by kind:

```python
from ambient_device_sdk import available_kinds, load_plugin, create_device, parse_device_configs

available_kinds()                  # sorted kinds installed here; imports nothing
load_plugin("reachy-mini-lite")    # -> DevicePlugin
create_device(config, emit=sink, describe_image=vision)  # -> Device, built but not started
```

Errors, all subclasses of `DevicePluginError`:

- `PluginNotFoundError`: nothing installed for that kind;
- `PluginLoadError`: two different entry points for one kind, import failure, not a `DevicePlugin`, or name/kind mismatch;
- `IncompatiblePluginError`: `protocol_version` differs from the SDK's;
- `DeviceCreateError`: the factory raised, returned a non-`Device`, or the id/kind don't match the config.

A missing or broken plugin never stops the hub; it logs the error and runs with no device.

## Configuration

Devices are listed explicitly; nothing is auto-attached just because a plugin is installed.
Each entry is a table with `id`, `kind`, optional `enabled` (default true) and optional
`config` (device-specific, passed as `DeviceContext.config`). `parse_device_configs` keeps
the order and rejects unknown keys and duplicate ids. Where the hub reads these tables from
(and its default when none are configured) is decided by ambient-home, e.g.:

```toml
[[devices]]
id = "reachy-desk"
kind = "reachy-mini-lite"

[devices.config]
camera = true
```

## Versioning

- The package uses semantic versions (`ambient_device_sdk.__version__`).
- `PROTOCOL_VERSION` changes only on a breaking change to this contract. Additive changes
  (new optional fields, new enum members) keep it. The hub refuses plugins built for another
  protocol version.

## Testing a connector

```python
from ambient_device_sdk import DeviceContext
from ambient_device_sdk.testing import check_device_contract, run_device_contract

async def test_contract():
    device = PLUGIN.create(DeviceContext("test", {"camera": False}))
    assert check_device_contract(device) == []        # static checks, no hardware
    assert await run_device_contract(device) == []    # start, one call each, sleep, stop twice
```

`FakeDevice` is an in-memory device for hub tests: `push_mic(pcm16)`, `push_request(text)`,
and it records `played`, `playback_stops` and `sessions`.
