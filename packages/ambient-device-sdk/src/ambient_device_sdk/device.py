"""The Device protocol and the context the hub hands to a device when building it."""

from typing import Protocol, runtime_checkable
from dataclasses import field, dataclass
from collections.abc import Mapping, Sequence, AsyncIterator

from ambient_device_sdk.types import (
    ToolSpec,
    EventSink,
    Capability,
    AudioFormat,
    DeviceHealth,
    SessionState,
    DescribeImage,
    null_event_sink,
)


@dataclass(frozen=True, slots=True)
class DeviceContext:
    """Everything a device factory receives. Built by the hub from one device config entry."""

    device_id: str
    """Install-unique id from config, e.g. ``"reachy-desk"``. The device's ``id`` must equal it."""
    config: Mapping[str, object] = field(default_factory=dict)
    """The entry's device-specific settings (TOML-style scalars, lists, tables). The device validates them."""
    emit: EventSink = null_event_sink
    """Local event callback; see :class:`~ambient_device_sdk.types.EventSink`."""
    describe_image: DescribeImage | None = None
    """Vision callback, or None when the hub has no vision model. If None, offer no tool that needs it."""


@runtime_checkable
class Device(Protocol):
    """One connected device ("front door"). See the package README for the full contract."""

    @property
    def id(self) -> str:
        """Install-unique id; equals ``DeviceContext.device_id``."""
        ...

    @property
    def kind(self) -> str:
        """Plugin kind, e.g. ``"reachy-mini-lite"``; equals the plugin's ``kind``."""
        ...

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What the device can do. Fixed for the device's lifetime."""
        ...

    @property
    def mic_format(self) -> AudioFormat | None:
        """Format of ``mic()`` chunks; set iff LISTEN is a capability."""
        ...

    @property
    def speaker_format(self) -> AudioFormat | None:
        """Format ``play()`` expects; set iff SPEAK is a capability."""
        ...

    async def start(self) -> None:
        """Connect to the hardware and put it in its rest pose. May raise; may be called again after ``stop()``."""
        ...

    async def stop(self) -> None:
        """Release the hardware and end the ``mic()``/``requests()`` iterators. Idempotent; never raises."""
        ...

    async def health(self) -> DeviceHealth:
        """Return current health quickly. Never raises."""
        ...

    def mic(self) -> AsyncIterator[bytes]:
        """LISTEN: stream PCM16 chunks in ``mic_format`` continuously from ``start()`` until ``stop()``."""
        ...

    async def play(self, pcm16: bytes) -> None:
        """SPEAK: queue PCM16 audio in ``speaker_format``; returns without waiting for playback to finish."""
        ...

    async def stop_playback(self) -> None:
        """SPEAK: drop all queued audio now. Idempotent."""
        ...

    def requests(self) -> AsyncIterator[str]:
        """TEXT: yield typed asks from the user until ``stop()``."""
        ...

    def tools(self) -> Sequence[ToolSpec]:
        """Return the device's tools for the conversation that is opening."""
        ...

    async def on_session(self, state: SessionState) -> None:
        """React to a conversation update (body moves). Only called on the device that owns the conversation."""
        ...
