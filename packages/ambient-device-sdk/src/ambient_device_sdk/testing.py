"""Contract checks and a scriptable fake device, for connector and hub tests."""

import asyncio
from collections.abc import Sequence, AsyncIterator

from ambient_device_sdk.types import (
    ToolSpec,
    Capability,
    AudioFormat,
    DeviceHealth,
    SessionState,
)
from ambient_device_sdk.device import Device, DeviceContext


def check_device_contract(device: object) -> list[str]:
    """Return contract violations visible without starting the device (empty list = OK)."""
    if not isinstance(device, Device):
        return ["object does not implement the Device protocol"]
    problems: list[str] = []
    if not isinstance(device.id, str) or not device.id:
        problems.append("id must be a non-empty string")
    if not isinstance(device.kind, str) or not device.kind:
        problems.append("kind must be a non-empty string")
    caps = device.capabilities
    if not isinstance(caps, frozenset) or not all(isinstance(c, Capability) for c in caps):
        problems.append("capabilities must be a frozenset of Capability")
        caps = frozenset()
    for cap, fmt, name in (
        (Capability.LISTEN, device.mic_format, "mic_format"),
        (Capability.SPEAK, device.speaker_format, "speaker_format"),
    ):
        if (cap in caps) != (fmt is not None):
            problems.append(f"{name} must be set if and only if {cap.value} is a capability")
        if fmt is not None and not isinstance(fmt, AudioFormat):
            problems.append(f"{name} must be an AudioFormat")
    tools = device.tools()
    names = [tool.name for tool in tools if isinstance(tool, ToolSpec)]
    if len(names) != len(tools):
        problems.append("tools() must return ToolSpec objects")
    if len(set(names)) != len(names):
        problems.append("tool names must be unique")
    return problems


async def run_device_contract(device: Device, *, timeout: float = 5.0) -> list[str]:
    """Start the device, exercise each allowed method once, stop it twice; return violations."""
    problems = check_device_contract(device)
    if problems:
        return problems
    async with asyncio.timeout(timeout):
        await device.start()
        try:
            if not isinstance(await device.health(), DeviceHealth):
                problems.append("health() must return DeviceHealth")
            if Capability.LISTEN in device.capabilities and device.mic_format is not None:
                chunk = await anext(aiter(device.mic()))
                if not isinstance(chunk, bytes) or not device.mic_format.is_whole_frames(chunk):
                    problems.append("mic() must yield whole PCM16 frames as bytes")
            if Capability.SPEAK in device.capabilities and device.speaker_format is not None:
                await device.play(b"\x00" * device.speaker_format.bytes_per_frame)
                await device.stop_playback()
                await device.stop_playback()
            for state in (
                SessionState.wake("contract"),
                SessionState.active("contract", listening=True, speaking=False),
                SessionState.interrupted("contract"),
                SessionState.active("contract", listening=False, speaking=True),
                SessionState.active("contract", listening=False, speaking=False),
                SessionState.sleep("contract", "contract_test"),
            ):
                await device.on_session(state)
        finally:
            await device.stop()
            await device.stop()
        if not isinstance(await device.health(), DeviceHealth):
            problems.append("health() must return DeviceHealth after stop()")
    return problems


class FakeDevice:
    """In-memory Device for tests. Feeds scripted mic chunks / text asks and records hub calls."""

    def __init__(
        self,
        context: DeviceContext,
        *,
        kind: str = "fake",
        capabilities: frozenset[Capability] = frozenset({Capability.LISTEN, Capability.SPEAK}),
        mic_format: AudioFormat | None = AudioFormat(16_000),
        speaker_format: AudioFormat | None = AudioFormat(24_000),
        tools: Sequence[ToolSpec] = (),
    ) -> None:
        """Build a stopped fake; nothing runs until ``start()``."""
        self._id = context.device_id
        self._kind = kind
        self._capabilities = capabilities
        self._mic_format = mic_format if Capability.LISTEN in capabilities else None
        self._speaker_format = speaker_format if Capability.SPEAK in capabilities else None
        self._tools = tuple(tools)
        self.context = context
        self.started = False
        self.start_count = 0
        self.played: list[bytes] = []
        self.playback_stops = 0
        self.sessions: list[SessionState] = []
        self.health_result = DeviceHealth.ok()
        self._mic: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._requests: asyncio.Queue[str | None] = asyncio.Queue()

    @property
    def id(self) -> str:
        """Device id from the context."""
        return self._id

    @property
    def kind(self) -> str:
        """Plugin kind."""
        return self._kind

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Configured capabilities."""
        return self._capabilities

    @property
    def mic_format(self) -> AudioFormat | None:
        """Mic format, if LISTEN."""
        return self._mic_format

    @property
    def speaker_format(self) -> AudioFormat | None:
        """Speaker format, if SPEAK."""
        return self._speaker_format

    def push_mic(self, pcm16: bytes) -> None:
        """Queue one mic chunk."""
        self._mic.put_nowait(pcm16)

    def push_request(self, text: str) -> None:
        """Queue one text ask."""
        self._requests.put_nowait(text)

    async def start(self) -> None:
        """Mark started; a restarted fake gets fresh streams."""
        if not self.started:
            self._mic = asyncio.Queue()
            self._requests = asyncio.Queue()
        self.started = True
        self.start_count += 1

    async def stop(self) -> None:
        """End both streams."""
        if self.started:
            self.started = False
            self._mic.put_nowait(None)
            self._requests.put_nowait(None)

    async def health(self) -> DeviceHealth:
        """Return ``health_result`` (DOWN when stopped)."""
        return self.health_result if self.started else DeviceHealth.down("stopped")

    async def mic(self) -> AsyncIterator[bytes]:
        """Yield pushed chunks until stopped; yields one silent frame first so the stream is never empty."""
        if self._mic_format is None:
            raise NotImplementedError("device has no LISTEN capability")
        queue = self._mic
        yield b"\x00" * self._mic_format.bytes_per_frame
        while (chunk := await queue.get()) is not None:
            yield chunk

    async def play(self, pcm16: bytes) -> None:
        """Record played audio."""
        self.played.append(pcm16)

    async def stop_playback(self) -> None:
        """Count playback stops."""
        self.playback_stops += 1

    async def requests(self) -> AsyncIterator[str]:
        """Yield pushed asks until stopped."""
        queue = self._requests
        while (text := await queue.get()) is not None:
            yield text

    def tools(self) -> Sequence[ToolSpec]:
        """Return configured tools."""
        return self._tools

    async def on_session(self, state: SessionState) -> None:
        """Record session updates."""
        self.sessions.append(state)
