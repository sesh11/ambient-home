"""Provider-neutral realtime voice interfaces."""

from typing import Literal, Protocol
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from collections.abc import Sequence, AsyncIterator


ToolSchema = dict[str, object]
SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class VoiceSessionConfig:
    """Provider-neutral configuration for a realtime session."""

    instructions: str
    voice: str
    tools: Sequence[ToolSchema]


@dataclass(frozen=True)
class AudioOut:
    """One output-audio payload."""

    pcm16: bytes


@dataclass(frozen=True)
class TranscriptDelta:
    """One incremental transcript update."""

    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class ToolCall:
    """One provider-neutral tool invocation."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class MalformedToolCall:
    """A tool invocation that could not be decoded."""

    name: str | None


@dataclass(frozen=True)
class Usage:
    """Reported provider usage for one session."""

    seconds: float


@dataclass(frozen=True)
class VoiceError:
    """A provider-neutral error event."""

    code: str | int | float | bool | None
    error_type: str | None
    message: str | None


@dataclass(frozen=True)
class Closed:
    """Provider session closure and final usage."""

    reason: str
    usage_seconds: float | None


VoiceEvent = AudioOut | TranscriptDelta | ToolCall | MalformedToolCall | Usage | VoiceError | Closed


class VoiceConnectionLost(Exception):
    """The provider websocket closed unexpectedly."""


class VoiceStartupTimeout(Exception):
    """The provider did not start a session within its startup window."""


class VoiceSession(Protocol):
    """Realtime operations supported by a voice session."""

    async def send_audio(self, pcm16: bytes) -> None:
        """Send PCM16 audio to the provider."""
        ...

    def events(self) -> AsyncIterator[VoiceEvent]:
        """Yield provider-neutral session events."""
        ...

    async def send_tool_result(self, call_id: str, result: dict[str, object]) -> None:
        """Send a completed tool result."""
        ...

    async def request_response(self) -> None:
        """Request a provider response after tool results."""
        ...

    async def say(self, text: str) -> None:
        """Ask the provider to speak an announcement."""
        ...

    async def close(self) -> None:
        """Close the provider session."""
        ...


class VoiceModel(Protocol):
    """Factory for provider-neutral voice sessions."""

    name: str
    voices: tuple[str, ...]

    def available(self) -> bool:
        """Return whether credentials are configured."""
        ...

    def open(self, config: VoiceSessionConfig) -> AbstractAsyncContextManager[VoiceSession]:
        """Open a realtime session."""
        ...
