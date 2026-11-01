"""Plain value types shared by devices and the hub."""

import re
from enum import StrEnum
from typing import Literal, Protocol
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Awaitable


SAMPLE_WIDTH_BYTES = 2
"""Every audio chunk is signed 16-bit little-endian PCM (PCM16), channels interleaved."""

EventLevel = Literal["info", "warning", "error"]
EventValue = str | int | float | bool | None


class Capability(StrEnum):
    """What a device can do. The hub only calls the methods a device's capabilities allow."""

    LISTEN = "listen"
    """Streams microphone audio to the hub with ``mic()``."""
    SPEAK = "speak"
    """Plays hub audio with ``play()`` / ``stop_playback()``."""
    MOVE = "move"
    """Has a body that reacts to ``on_session()`` and may offer body tools."""
    SEE = "see"
    """Has a camera and may offer camera tools (vision goes through ``DeviceContext.describe_image``)."""
    TEXT = "text"
    """Sends typed asks with ``requests()`` instead of audio."""


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Format of one audio stream: PCM16 little-endian, interleaved channels."""

    sample_rate: int
    channels: int = 1

    def __post_init__(self) -> None:
        """Reject impossible formats."""
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.channels < 1:
            raise ValueError("channels must be at least 1")

    @property
    def bytes_per_frame(self) -> int:
        """Bytes in one sample frame (all channels)."""
        return self.channels * SAMPLE_WIDTH_BYTES

    def is_whole_frames(self, pcm16: bytes) -> bool:
        """Return whether a chunk holds a whole number of frames."""
        return len(pcm16) % self.bytes_per_frame == 0

    def duration(self, pcm16: bytes) -> float:
        """Return the chunk's length in seconds."""
        return len(pcm16) / (self.bytes_per_frame * self.sample_rate)

    def byte_count(self, seconds: float) -> int:
        """Return the number of bytes in ``seconds`` of audio, rounded down to whole frames."""
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        return int(seconds * self.sample_rate) * self.bytes_per_frame


class HealthStatus(StrEnum):
    """Coarse device health."""

    OK = "ok"
    DEGRADED = "degraded"
    """Working, but something is off (e.g. camera unavailable, mic recovering)."""
    DOWN = "down"
    """Not usable (e.g. helper process unreachable, hardware unplugged)."""


@dataclass(frozen=True, slots=True)
class DeviceHealth:
    """Result of ``Device.health()``.

    ``reason`` is a short stable token such as ``"daemon_unreachable"`` or ``"mic_silent"``;
    it is required unless the status is OK. ``detail`` holds small scalar values only.
    """

    status: HealthStatus
    reason: str | None = None
    detail: Mapping[str, EventValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Require a reason for any non-OK status."""
        if self.status is not HealthStatus.OK and not self.reason:
            raise ValueError("reason is required when status is not ok")

    @classmethod
    def ok(cls, **detail: EventValue) -> "DeviceHealth":
        """Build an OK result."""
        return cls(HealthStatus.OK, None, detail)

    @classmethod
    def degraded(cls, reason: str, **detail: EventValue) -> "DeviceHealth":
        """Build a DEGRADED result."""
        return cls(HealthStatus.DEGRADED, reason, detail)

    @classmethod
    def down(cls, reason: str, **detail: EventValue) -> "DeviceHealth":
        """Build a DOWN result."""
        return cls(HealthStatus.DOWN, reason, detail)


class SessionPhase(StrEnum):
    """Which conversation transition an ``on_session()`` call reports."""

    WAKE = "wake"
    """The conversation opened on this device (wake pose, motors on)."""
    LISTENING = "listening"
    """The user is talking."""
    SPEAKING = "speaking"
    """The assistant is talking."""
    IDLE = "idle"
    """Conversation open, nobody talking."""
    INTERRUPTED = "interrupted"
    """The user talked over the assistant; the hub has already called ``stop_playback()``."""
    SLEEP = "sleep"
    """The conversation closed (rest pose). ``reason`` says why."""


_ACTIVE_PHASES = frozenset({SessionPhase.LISTENING, SessionPhase.SPEAKING, SessionPhase.IDLE})


@dataclass(frozen=True, slots=True)
class SessionState:
    """One conversation update sent to the owning device.

    ``listening`` and ``speaking`` are the authoritative flags (they map one-to-one to a body's
    "listening" and "speaking" animations). ``phase`` names the transition. For the active phases
    the phase is derived from the flags: SPEAKING if ``speaking``, else LISTENING if
    ``listening``, else IDLE. Build those with :meth:`active`.
    """

    phase: SessionPhase
    session_id: str
    listening: bool = False
    speaking: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce the phase/flag invariants."""
        if not self.session_id:
            raise ValueError("session_id must be non-empty")
        if self.phase in (SessionPhase.WAKE, SessionPhase.SLEEP):
            if self.listening or self.speaking:
                raise ValueError(f"{self.phase} must have listening=False and speaking=False")
        elif self.phase is SessionPhase.INTERRUPTED:
            if self.speaking:
                raise ValueError("interrupted must have speaking=False")
        elif self.phase is not _derive_phase(self.listening, self.speaking):
            raise ValueError("active phase must match the listening/speaking flags")
        if self.reason is not None and self.phase is not SessionPhase.SLEEP:
            raise ValueError("reason is only allowed on sleep")

    @classmethod
    def wake(cls, session_id: str) -> "SessionState":
        """Build the first update of a conversation."""
        return cls(SessionPhase.WAKE, session_id)

    @classmethod
    def active(cls, session_id: str, *, listening: bool, speaking: bool) -> "SessionState":
        """Build a LISTENING / SPEAKING / IDLE update from the two flags."""
        return cls(_derive_phase(listening, speaking), session_id, listening, speaking)

    @classmethod
    def interrupted(cls, session_id: str) -> "SessionState":
        """Build an INTERRUPTED update (user is talking, playback stopped)."""
        return cls(SessionPhase.INTERRUPTED, session_id, listening=True)

    @classmethod
    def sleep(cls, session_id: str, reason: str) -> "SessionState":
        """Build the last update of a conversation."""
        return cls(SessionPhase.SLEEP, session_id, reason=reason)


def _derive_phase(listening: bool, speaking: bool) -> SessionPhase:
    if speaking:
        return SessionPhase.SPEAKING
    if listening:
        return SessionPhase.LISTENING
    return SessionPhase.IDLE


ToolResult = dict[str, object]
ToolHandler = Callable[[dict[str, object]], Awaitable[ToolResult]]
"""Async tool handler: JSON arguments in, JSON-serialisable dict out.

On failure return ``{"error": "<message>"}``. The hub also turns a raised exception into
``{"error": str(exc)}``, but handlers should not rely on that.
"""

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the voice model can call. Used for both device tools and core tools."""

    name: str
    description: str
    parameters: Mapping[str, object]
    """JSON Schema for the arguments; top level must be ``{"type": "object", ...}``."""
    handler: ToolHandler

    def __post_init__(self) -> None:
        """Validate the name, description and schema root."""
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError("tool name must match ^[A-Za-z0-9_-]{1,64}$")
        if not self.description.strip():
            raise ValueError("tool description must be non-empty")
        if self.parameters.get("type") != "object":
            raise ValueError('tool parameters must be a JSON Schema with "type": "object"')

    def schema(self) -> dict[str, object]:
        """Return the model-facing declaration (no handler)."""
        return {"name": self.name, "description": self.description, "parameters": dict(self.parameters)}


class EventSink(Protocol):
    """Callback a device uses to record a local event, without depending on garage-sdk.

    Same shape as garage-sdk's ``emit``: dotted ``type`` (e.g. ``"robot.wake_failed"``),
    ``component`` (e.g. ``"live"``), ``level`` and small scalar ``data``. The hub's sink scrubs
    and writes the event to the install's local log; it never raises. Devices must not put
    transcripts, audio, images or other personal content in ``data``.
    """

    def __call__(self, type: str, component: str, level: EventLevel = "info", /, **data: EventValue) -> None:
        """Record one event."""


def null_event_sink(type: str, component: str, level: EventLevel = "info", /, **data: EventValue) -> None:
    """Drop the event. Default when the hub injects no sink."""


DescribeImage = Callable[[bytes, str], Awaitable[str]]
"""Vision callback injected by the hub: ``await describe_image(jpeg_bytes, question) -> answer``.

The hub owns the provider, model and API key. It raises on failure; the device catches it,
emits ``vision.failed`` and returns ``{"error": str(exc)}`` from its tool.
"""
