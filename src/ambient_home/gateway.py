"""Supervise device plugins and route audio to the active conversation."""

import time
import asyncio
import logging
import tomllib
from typing import Protocol, cast
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Iterable, Sequence, Awaitable
from importlib.metadata import EntryPoint

import numpy as np
from garage_sdk import emit
from numpy.typing import NDArray

from ambient_device_sdk import (
    Device,
    ToolSpec,
    Capability,
    AudioFormat,
    DeviceConfig,
    DeviceHealth,
    SessionState,
    DevicePluginError,
    create_device,
    parse_device_configs,
)
from ambient_home.audio import PrerollBuffer, PcmStreamConverter, to_bytes
from ambient_home.wake_word import WakeWordDetector
from ambient_device_sdk.types import EventSink
from ambient_home.connectors.voice.base import SAMPLE_RATE


logger = logging.getLogger(__name__)
DescribeImage = Callable[[bytes, str], Awaitable[str]]


class GatewayListener(Protocol):
    """Conversation callbacks used by the device gateway."""

    def request_wake(self, device_id: str) -> bool:
        """Request a conversation for one device."""
        ...

    def device_lost(self, device_id: str) -> None:
        """Report loss of an active owner device."""
        ...

    async def send_audio(self, mono: NDArray[np.int16]) -> None:
        """Forward microphone audio to the conversation."""
        ...

    def is_streaming(self) -> bool:
        """Return whether the conversation streams audio."""
        ...


@dataclass
class _DeviceEntry:
    config: DeviceConfig
    device: Device | None
    state: str = "starting"
    health: str = "unknown"
    converter: PcmStreamConverter | None = None
    preroll: PrerollBuffer | None = None
    detector: WakeWordDetector | None = None
    speaker_converter: PcmStreamConverter | None = None
    silence_seconds: float = 0.0
    saw_nonzero: bool = False
    silence_logged: bool = False
    heartbeat_at: float = 0.0
    peak_rms: float = 0.0


class DeviceGateway:
    """Load and supervise device plugins while keeping failures isolated."""

    def __init__(
        self,
        configs: Sequence[DeviceConfig],
        *,
        wake_detector_factory: Callable[[], WakeWordDetector],
        preroll_seconds: float,
        describe_image: DescribeImage | None,
        eps: Iterable[EntryPoint] | None = None,
        reconnect_delays: Sequence[float] = (1, 2, 5, 10, 30),
        health_interval_s: float = 10.0,
    ) -> None:
        """Configure devices, supervision, wake detection, and vision."""
        self.configs = tuple(config for config in configs if config.enabled)
        self.wake_detector_factory = wake_detector_factory
        self.preroll_seconds = preroll_seconds
        self.describe_image = describe_image
        self.eps = eps
        self.reconnect_delays = tuple(reconnect_delays) or (1.0,)
        self.health_interval_s = health_interval_s
        self.listener: GatewayListener | None = None
        self.owner: str | None = None
        self._entries: dict[str, _DeviceEntry] = {}
        self._stop_event = asyncio.Event()
        self._supervisors: list[asyncio.Task[None]] = []
        self._speaker_owner: str | None = None

    def set_listener(self, listener: GatewayListener) -> None:
        """Set the conversation callback target."""
        self.listener = listener

    async def run(self) -> None:
        """Create and supervise configured devices."""
        self._stop_event.clear()
        self._entries.clear()
        for config in self.configs:
            try:
                device = create_device(
                    config,
                    emit=cast(EventSink, emit),
                    describe_image=self.describe_image,
                    eps=self.eps,
                )
            except DevicePluginError as exc:
                self._entries[config.id] = _DeviceEntry(config, None, state="failed")
                emit(
                    "device.create_failed",
                    "gateway",
                    "error",
                    device=config.id,
                    kind=config.kind,
                    exc_type=type(exc).__name__,
                )
                continue
            entry = _DeviceEntry(
                config,
                device,
                preroll=PrerollBuffer(self.preroll_seconds, SAMPLE_RATE),
            )
            self._entries[config.id] = entry
            self._supervisors.append(asyncio.create_task(self._supervise(entry), name=f"device-{config.id}"))
        try:
            await self._stop_event.wait()
        finally:
            for entry in self._entries.values():
                if entry.device is not None:
                    try:
                        await entry.device.stop()
                    except Exception:
                        pass
            for task in self._supervisors:
                task.cancel()
            if self._supervisors:
                await asyncio.gather(*self._supervisors, return_exceptions=True)
            self._supervisors.clear()

    async def stop(self) -> None:
        """Stop configured devices and cancel their supervisors."""
        self._stop_event.set()
        for entry in self._entries.values():
            if entry.device is not None:
                try:
                    await entry.device.stop()
                except Exception:
                    pass

    async def _supervise(self, entry: _DeviceEntry) -> None:
        assert entry.device is not None
        retry_index = 0
        while not self._stop_event.is_set():
            entry.state = "starting" if retry_index == 0 else "reconnecting"
            started_at = time.monotonic()
            try:
                await entry.device.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                entry.state = "reconnecting"
                emit(
                    "device.start_failed",
                    "gateway",
                    "warning",
                    device=entry.config.id,
                    exc_type=type(exc).__name__,
                )
                await self._retry(retry_index)
                retry_index = min(retry_index + 1, len(self.reconnect_delays) - 1)
                continue
            entry.state = "connected"
            entry.converter = (
                PcmStreamConverter(entry.device.mic_format, SAMPLE_RATE)
                if Capability.LISTEN in entry.device.capabilities and entry.device.mic_format is not None
                else None
            )
            if entry.detector is not None:
                entry.detector.reset()
            emit("device.connected", "gateway", device=entry.device.id, kind=entry.device.kind)
            reason: str | None = None
            try:
                reason = await self._run_connected(entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                reason = "pump_error"
            if self._stop_event.is_set():
                break
            emit("device.dropped", "gateway", "warning", device=entry.device.id, reason=reason or "pump_error")
            if self.owner == entry.device.id and self.listener is not None:
                self.listener.device_lost(entry.device.id)
            entry.state = "reconnecting"
            try:
                await entry.device.stop()
            except Exception:
                pass
            if time.monotonic() - started_at > 60:
                retry_index = 0
            await self._retry(retry_index)
            retry_index = min(retry_index + 1, len(self.reconnect_delays) - 1)

    async def _run_connected(self, entry: _DeviceEntry) -> str | None:
        assert entry.device is not None
        tasks: dict[asyncio.Task[str | None], str] = {}
        if Capability.LISTEN in entry.device.capabilities:
            tasks[asyncio.create_task(self._pump_mic(entry))] = "mic"
        if Capability.TEXT in entry.device.capabilities:
            tasks[asyncio.create_task(self._pump_requests(entry))] = "requests"
        tasks[asyncio.create_task(self._watch_health(entry))] = "health"
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        result = next(iter(done))
        try:
            reason = result.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "pump_error" if tasks[result] != "health" else "health_error"
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return reason or f"{tasks[result]}_ended"

    async def _pump_mic(self, entry: _DeviceEntry) -> str:
        assert entry.device is not None
        assert entry.device.mic_format is not None and entry.converter is not None
        try:
            async for pcm16 in entry.device.mic():
                mono = entry.converter.convert(pcm16).reshape(-1)
                if mono.size:
                    await self._route_audio(entry, mono)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "mic_error"
        return "mic_ended"

    async def _route_audio(self, entry: _DeviceEntry, mono: NDArray[np.int16]) -> None:
        if self.listener is None:
            return
        entry.silence_seconds += len(mono) / SAMPLE_RATE
        if np.any(mono):
            entry.saw_nonzero = True
        if entry.silence_seconds >= 5.0 and not entry.saw_nonzero and not entry.silence_logged:
            logger.error("Microphone on device %s has delivered only zeros for 5 s", entry.config.id)
            emit("mic.silent_stream", "live", "error", seconds=entry.silence_seconds)
            entry.silence_logged = True
        streaming = entry.device is not None and self.owner == entry.device.id and self.listener.is_streaming()
        if streaming:
            await self.listener.send_audio(mono)
            return
        if self.owner is None or (entry.device is not None and self.owner == entry.device.id):
            assert entry.preroll is not None
            entry.preroll.append(mono)
        now = time.monotonic()
        if now - entry.heartbeat_at >= 5.0:
            if entry.detector is None:
                entry.detector = self.wake_detector_factory()
            score = entry.detector.take_peak_score()
            logger.info(
                "Asleep: device=%s mic_rms=%.0f wake_score=%.2f",
                entry.config.id,
                entry.peak_rms,
                score,
            )
            entry.heartbeat_at = now
            entry.peak_rms = 0.0
        if entry.detector is None:
            entry.detector = self.wake_detector_factory()
        entry.peak_rms = max(entry.peak_rms, float(np.sqrt(np.mean(np.square(mono, dtype=np.float32)))))
        detected = entry.detector.feed(mono)
        if detected:
            if self.owner is None and self.listener.request_wake(entry.config.id):
                self.owner = entry.config.id
                self._speaker_owner = None
                logger.info("Wake word detected (score=%.2f)", entry.detector.last_score)
                emit("wake.detected", "live", score=entry.detector.last_score)
            elif self.owner is not None and self.owner != entry.config.id:
                emit("wake.ignored_busy", "gateway", "warning", device=entry.config.id)

    async def _pump_requests(self, entry: _DeviceEntry) -> str:
        assert entry.device is not None
        try:
            async for _request in entry.device.requests():
                logger.info("Ignoring unsupported text request from device %s", entry.config.id)
                emit("device.text_request_ignored", "gateway", "warning", device=entry.config.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "request_error"
        return "request_ended"

    async def _watch_health(self, entry: _DeviceEntry) -> str | None:
        assert entry.device is not None
        while not self._stop_event.is_set():
            await asyncio.sleep(self.health_interval_s)
            health: DeviceHealth = await entry.device.health()
            entry.health = health.status.value
            if health.status.value == "down":
                return "health_down"
        return "stopped"

    async def _retry(self, index: int) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self.reconnect_delays[index])
        except TimeoutError:
            pass

    @property
    def is_streaming(self) -> bool:
        """Return whether the conversation currently streams audio."""
        return self.listener is not None and self.listener.is_streaming()

    def preroll(self, device_id: str) -> NDArray[np.int16]:
        """Drain wake-word preroll for one device."""
        entry = self._entries.get(device_id)
        return (
            entry.preroll.drain() if entry is not None and entry.preroll is not None else np.empty(0, dtype=np.int16)
        )

    async def play(self, pcm16_16k_mono: bytes) -> None:
        """Convert and play provider audio through the owner device."""
        entry = self._entries.get(self.owner or "")
        if entry is None or entry.device is None or Capability.SPEAK not in entry.device.capabilities:
            return
        target = entry.device.speaker_format
        if target is None:
            return
        if self._speaker_owner != entry.device.id or entry.speaker_converter is None:
            entry.speaker_converter = PcmStreamConverter(AudioFormat(SAMPLE_RATE), target.sample_rate, target.channels)
            self._speaker_owner = entry.device.id
        converted = entry.speaker_converter.convert(pcm16_16k_mono)
        if converted.size:
            await entry.device.play(to_bytes(converted))

    async def stop_playback(self) -> None:
        """Stop playback on the owner device."""
        entry = self._entries.get(self.owner or "")
        if entry is not None and entry.device is not None and Capability.SPEAK in entry.device.capabilities:
            await entry.device.stop_playback()

    async def notify(self, state: SessionState) -> None:
        """Send a session-state notification to the owner device."""
        entry = self._entries.get(self.owner or "")
        if entry is None or entry.device is None:
            return
        try:
            await entry.device.on_session(state)
        except Exception as exc:
            emit(
                "device.session_failed",
                "gateway",
                "error",
                device=entry.config.id,
                phase=state.phase.value,
                exc_type=type(exc).__name__,
            )

    def owner_tools(self) -> list[ToolSpec]:
        """Return tools provided by the owner device."""
        entry = self._entries.get(self.owner or "")
        if entry is None or entry.device is None:
            return []
        try:
            return list(entry.device.tools())
        except Exception as exc:
            emit("device.tools_failed", "gateway", "error", device=entry.config.id, exc_type=type(exc).__name__)
            return []

    def release(self, device_id: str) -> None:
        """Release ownership when it matches the given device."""
        if self.owner == device_id:
            self.owner = None
            self._speaker_owner = None

    def status(self) -> list[dict[str, object]]:
        """Return the UI status for each configured device."""
        return [
            {
                "id": entry.config.id,
                "kind": entry.config.kind,
                "state": entry.state,
                "health": entry.health,
                "owner": entry.config.id == self.owner,
            }
            for entry in self._entries.values()
        ]


def load_device_configs(path: Path) -> list[DeviceConfig]:
    """Read configured device entries; a missing config is an empty device set."""
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError:
        logger.info("No devices configured at %s", path)
        return []
    devices = document.get("device", [])
    if not isinstance(devices, list):
        raise ValueError("devices TOML must contain [[device]] tables")
    return parse_device_configs(devices)
