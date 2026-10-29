"""Device gateway lifecycle and routing behavior."""

import sys
import asyncio
from types import ModuleType
from importlib.metadata import EntryPoint

import numpy as np
import pytest

import ambient_home.gateway as gateway_module
from ambient_device_sdk import (
    Capability,
    AudioFormat,
    DeviceConfig,
    DeviceHealth,
    DevicePlugin,
    SessionState,
    DeviceContext,
    parse_device_configs,
)
from ambient_home.audio import PrerollBuffer
from ambient_home.config import settings_from_env
from ambient_home.gateway import DeviceGateway, _DeviceEntry, load_device_configs
from ambient_home.persona import load_profile
from ambient_home.spend_log import SpendLog
from ambient_device_sdk.plugin import ENTRY_POINT_GROUP
from ambient_home.conversation import Conversation
from ambient_home.session_gate import SessionGate
from ambient_device_sdk.testing import FakeDevice


class Listener:
    def __init__(self, *, accept_wake: bool = True, streaming: bool = False) -> None:
        self.accept_wake = accept_wake
        self.streaming = streaming
        self.wakes: list[str] = []
        self.lost: list[str] = []
        self.audio: list[np.ndarray] = []

    def request_wake(self, device_id: str) -> bool:
        self.wakes.append(device_id)
        return self.accept_wake

    def device_lost(self, device_id: str) -> None:
        self.lost.append(device_id)

    async def send_audio(self, mono: np.ndarray) -> None:
        self.audio.append(mono.copy())

    def is_streaming(self) -> bool:
        return self.streaming


class ScriptedDetector:
    last_score = 0.91

    def __init__(self, detections: list[bool] | None = None) -> None:
        self.detections = list(detections or [])

    def feed(self, _samples: np.ndarray) -> bool:
        return self.detections.pop(0) if self.detections else False

    def take_peak_score(self) -> float:
        return self.last_score

    def reset(self) -> None:
        return


def _entry_points(monkeypatch, plugins: dict[str, DevicePlugin]) -> list[EntryPoint]:
    module = ModuleType(f"_gateway_test_plugins_{id(monkeypatch)}")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    points = []
    for index, (kind, plugin) in enumerate(plugins.items()):
        attribute = f"plugin_{index}"
        setattr(module, attribute, plugin)
        points.append(
            EntryPoint(
                name=kind,
                value=f"{module.__name__}:{attribute}",
                group=ENTRY_POINT_GROUP,
            )
        )
    return points


def _configs(*kinds: str) -> list[DeviceConfig]:
    return parse_device_configs([{"id": f"device-{index}", "kind": kind} for index, kind in enumerate(kinds)])


def _record_events(monkeypatch) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    records: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(gateway_module, "emit", lambda *args, **data: records.append((args, data)))
    return records


def _events(records, name: str) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    return [record for record in records if record[0][0] == name]


async def _wait_for(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not reached")


async def _stop_gateway(gateway: DeviceGateway, task: asyncio.Task[None]) -> None:
    await gateway.stop()
    await asyncio.wait_for(task, timeout=1)


def test_missing_device_file_is_optional(tmp_path) -> None:
    assert load_device_configs(tmp_path / "missing.toml") == []


def test_plural_device_tables_load_config(tmp_path) -> None:
    path = tmp_path / "devices.toml"
    path.write_text(
        '[[devices]]\nid = "reachy"\nkind = "reachy-mini-lite"\n[devices.config]\ncamera = true\n',
        encoding="utf-8",
    )

    assert load_device_configs(path) == [DeviceConfig("reachy", "reachy-mini-lite", config={"camera": True})]


def test_singular_device_table_is_rejected_with_migration_message(tmp_path) -> None:
    path = tmp_path / "devices.toml"
    path.write_text('[[device]]\nid = "reachy"\nkind = "reachy-mini-lite"\n', encoding="utf-8")

    with pytest.raises(ValueError, match=r"^use \[\[devices\]\] tables, not \[\[device\]\]$"):
        load_device_configs(path)


def test_gateway_runs_without_devices_or_loading_wake_model() -> None:
    def forbidden_detector():
        raise AssertionError("wake detector should remain lazy with no devices")

    async def exercise() -> None:
        gateway = DeviceGateway(
            [],
            wake_detector_factory=forbidden_detector,
            preroll_seconds=2.0,
            describe_image=None,
        )
        task = asyncio.create_task(gateway.run())
        await asyncio.sleep(0)
        assert not task.done()
        assert gateway.status() == []
        await _stop_gateway(gateway, task)

    asyncio.run(exercise())


@pytest.mark.asyncio
async def test_creation_failure_isolated_from_healthy_device(monkeypatch) -> None:
    records = _record_events(monkeypatch)

    def fail_creation(_context):
        raise RuntimeError("synthetic create failure")

    plugins = {
        "broken": DevicePlugin("broken", fail_creation),
        "healthy": DevicePlugin("healthy", lambda context: FakeDevice(context, kind="healthy")),
    }
    gateway = DeviceGateway(
        _configs("broken", "healthy"),
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(monkeypatch, plugins),
        health_interval_s=3600,
    )
    task = asyncio.create_task(gateway.run())
    await _wait_for(
        lambda: any(item["id"] == "device-1" and item["state"] == "connected" for item in gateway.status())
    )
    assert gateway.status()[0]["state"] == "failed"
    assert _events(records, "device.create_failed")
    assert _events(records, "device.connected")
    await _stop_gateway(gateway, task)


@pytest.mark.asyncio
async def test_start_failure_retries_then_connects(monkeypatch) -> None:
    records = _record_events(monkeypatch)

    class FlakyDevice(FakeDevice):
        def __init__(self, context: DeviceContext) -> None:
            super().__init__(context, kind="flaky")
            self.attempts = 0

        async def start(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("synthetic start failure")
            await super().start()

    devices: list[FlakyDevice] = []

    def create(context: DeviceContext) -> FlakyDevice:
        device = FlakyDevice(context)
        devices.append(device)
        return device

    gateway = DeviceGateway(
        _configs("flaky"),
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(monkeypatch, {"flaky": DevicePlugin("flaky", create)}),
        reconnect_delays=(0,),
        health_interval_s=3600,
    )
    task = asyncio.create_task(gateway.run())
    await _wait_for(lambda: bool(_events(records, "device.connected")))
    assert devices[0].attempts >= 2
    assert len(_events(records, "device.start_failed")) == 1
    assert len(_events(records, "device.connected")) == 1
    await _stop_gateway(gateway, task)


@pytest.mark.asyncio
async def test_simultaneous_wake_detection_has_one_owner(monkeypatch) -> None:
    records = _record_events(monkeypatch)
    listener = Listener()
    gateway = DeviceGateway(
        _configs("fake", "fake"),
        wake_detector_factory=lambda: ScriptedDetector([True]),
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(
            monkeypatch,
            {"fake": DevicePlugin("fake", lambda context: FakeDevice(context))},
        ),
        reconnect_delays=(0,),
        health_interval_s=3600,
    )
    gateway.set_listener(listener)
    task = asyncio.create_task(gateway.run())
    await _wait_for(lambda: bool(_events(records, "wake.ignored_busy")))
    assert gateway.owner == "device-0"
    assert listener.wakes == ["device-0"]
    assert _events(records, "wake.ignored_busy")[0][1] == {"device": "device-1"}
    await _stop_gateway(gateway, task)


def test_preroll_only_receives_non_streaming_owner_audio() -> None:
    config_owner, config_other = _configs("fake", "fake")
    owner = FakeDevice(DeviceContext(config_owner.id))
    other = FakeDevice(DeviceContext(config_other.id))
    gateway = DeviceGateway(
        [config_owner, config_other],
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
    )
    listener = Listener(streaming=False)
    gateway.set_listener(listener)
    owner_entry = _DeviceEntry(config_owner, owner, preroll=PrerollBuffer(1, 16_000))
    other_entry = _DeviceEntry(config_other, other, preroll=PrerollBuffer(1, 16_000))
    gateway._entries = {config_owner.id: owner_entry, config_other.id: other_entry}
    gateway.owner = config_owner.id

    async def exercise() -> None:
        await gateway._route_audio(owner_entry, np.array([1, 2], dtype=np.int16))
        await gateway._route_audio(other_entry, np.array([3, 4], dtype=np.int16))

    asyncio.run(exercise())
    np.testing.assert_array_equal(gateway.preroll(config_owner.id), [1, 2])
    assert gateway.preroll(config_other.id).size == 0
    assert listener.audio == []


@pytest.mark.asyncio
async def test_owner_mic_end_reports_loss_stops_and_reconnects(monkeypatch) -> None:
    records = _record_events(monkeypatch)

    class EndsOnceDevice(FakeDevice):
        def __init__(self, context: DeviceContext) -> None:
            super().__init__(context, kind="ends-once")
            self.mic_runs = 0
            self.stop_count = 0

        async def mic(self):
            self.mic_runs += 1
            yield b"\x00\x00"
            if self.mic_runs > 1:
                async for chunk in super().mic():
                    yield chunk

        async def stop(self) -> None:
            self.stop_count += 1
            await super().stop()

    devices: list[EndsOnceDevice] = []

    def create(context: DeviceContext) -> EndsOnceDevice:
        device = EndsOnceDevice(context)
        devices.append(device)
        return device

    listener = Listener()
    gateway = DeviceGateway(
        _configs("ends-once"),
        wake_detector_factory=lambda: ScriptedDetector([True]),
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(monkeypatch, {"ends-once": DevicePlugin("ends-once", create)}),
        reconnect_delays=(0,),
        health_interval_s=3600,
    )
    gateway.set_listener(listener)
    task = asyncio.create_task(gateway.run())
    await _wait_for(lambda: len(_events(records, "device.connected")) >= 2)
    dropped = _events(records, "device.dropped")
    assert dropped[0][1] == {"device": "device-0", "reason": "mic_ended"}
    assert listener.lost == ["device-0"]
    assert devices[0].stop_count >= 1
    assert devices[0].start_count >= 2
    await _stop_gateway(gateway, task)


@pytest.mark.asyncio
async def test_down_health_drops_device(monkeypatch) -> None:
    records = _record_events(monkeypatch)
    devices: list[FakeDevice] = []

    def create(context: DeviceContext) -> FakeDevice:
        device = FakeDevice(context, kind="down")
        device.health_result = DeviceHealth.down("synthetic")
        devices.append(device)
        return device

    gateway = DeviceGateway(
        _configs("down"),
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(monkeypatch, {"down": DevicePlugin("down", create)}),
        reconnect_delays=(0,),
        health_interval_s=0.001,
    )
    task = asyncio.create_task(gateway.run())
    await _wait_for(
        lambda: any(event[1].get("reason") == "health_down" for event in _events(records, "device.dropped"))
    )
    assert devices[0].start_count >= 1
    await _stop_gateway(gateway, task)


@pytest.mark.asyncio
async def test_device_drop_clears_pending_wake_before_conversation_starts(monkeypatch, tmp_path) -> None:
    records = _record_events(monkeypatch)

    class EndsImmediatelyDevice(FakeDevice):
        def __init__(self, context: DeviceContext) -> None:
            super().__init__(context, kind="instant")

        async def mic(self):
            yield b"\x00\x00"

    def create(context: DeviceContext) -> EndsImmediatelyDevice:
        return EndsImmediatelyDevice(context)

    gateway = DeviceGateway(
        _configs("instant"),
        wake_detector_factory=lambda: ScriptedDetector([True]),
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(monkeypatch, {"instant": DevicePlugin("instant", create)}),
        reconnect_delays=(30,),
        health_interval_s=3600,
    )

    class UnusedVoice:
        name = "unused"
        voices = ("ballad",)
        opens = 0

        def available(self) -> bool:
            return True

        def open(self, _config):
            self.opens += 1
            raise AssertionError("a wake from a lost device must not open a session")

    voice = UnusedVoice()
    settings = settings_from_env()
    conversation = Conversation(
        settings,
        voice_model=voice,
        gate=SessionGate(idle_timeout_s=60, max_session_s=600, daily_budget_s=3600, seconds_used_today=0),
        spend_log=SpendLog(tmp_path / "spend.jsonl"),
        profile=load_profile(),
        core_tools=[],
        hub=gateway,
    )
    gateway.set_listener(conversation)
    task = asyncio.create_task(gateway.run())
    await _wait_for(lambda: bool(_events(records, "device.dropped")) and gateway.owner is None)
    assert not conversation._wake_event.is_set()
    assert conversation._pending_device is None
    await _stop_gateway(gateway, task)

    conversation_task = asyncio.create_task(conversation.run())
    await asyncio.sleep(0.02)
    assert voice.opens == 0
    assert not conversation_task.done()
    await conversation.shutdown()
    await conversation_task


@pytest.mark.asyncio
async def test_play_converts_to_owner_speaker_format_and_stops_playback() -> None:
    config = _configs("fake")[0]
    device = FakeDevice(
        DeviceContext(config.id),
        speaker_format=AudioFormat(24_000, 2),
    )
    gateway = DeviceGateway([config], wake_detector_factory=ScriptedDetector, preroll_seconds=1, describe_image=None)
    gateway._entries[config.id] = _DeviceEntry(config, device)
    gateway.owner = config.id

    await gateway.play(np.arange(160, dtype=np.int16).tobytes())
    await gateway.stop_playback()

    output = np.frombuffer(device.played[0], dtype="<i2").reshape(-1, 2)
    assert output.shape[1] == 2
    assert output.shape[0] > 0
    np.testing.assert_array_equal(output[:, 0], output[:, 1])
    assert device.playback_stops == 1


def test_release_allows_a_later_device_to_claim_ownership() -> None:
    config_one, config_two = _configs("fake", "fake")
    first = FakeDevice(DeviceContext(config_one.id))
    second = FakeDevice(DeviceContext(config_two.id))
    gateway = DeviceGateway(
        [config_one, config_two],
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
    )
    gateway.set_listener(Listener())
    one = _DeviceEntry(config_one, first, preroll=PrerollBuffer(1, 16_000), detector=ScriptedDetector([True]))
    two = _DeviceEntry(config_two, second, preroll=PrerollBuffer(1, 16_000), detector=ScriptedDetector([True]))
    gateway._entries = {config_one.id: one, config_two.id: two}

    async def exercise() -> None:
        await gateway._route_audio(one, np.array([1], dtype=np.int16))
        assert gateway.owner == config_one.id
        gateway.release(config_one.id)
        await gateway._route_audio(two, np.array([1], dtype=np.int16))

    asyncio.run(exercise())
    assert gateway.owner == config_two.id


@pytest.mark.asyncio
async def test_text_requests_are_ignored_without_logging_or_emitting_text(monkeypatch, caplog) -> None:
    records = _record_events(monkeypatch)
    sentinel = "private request sentinel"
    devices: list[FakeDevice] = []

    def create(context: DeviceContext) -> FakeDevice:
        device = FakeDevice(
            context,
            kind="text",
            capabilities=frozenset({Capability.LISTEN, Capability.SPEAK, Capability.TEXT}),
        )
        devices.append(device)
        return device

    gateway = DeviceGateway(
        _configs("text"),
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
        eps=_entry_points(monkeypatch, {"text": DevicePlugin("text", create)}),
        health_interval_s=3600,
    )
    caplog.set_level("INFO", logger="ambient_home.gateway")
    task = asyncio.create_task(gateway.run())
    await _wait_for(lambda: bool(devices) and devices[0].started)
    devices[0].push_request(sentinel)
    await _wait_for(lambda: bool(_events(records, "device.text_request_ignored")))
    assert sentinel not in caplog.text
    assert sentinel not in repr(_events(records, "device.text_request_ignored"))
    await _stop_gateway(gateway, task)


def test_notify_and_tools_failures_are_reported_and_tools_fall_back(monkeypatch) -> None:
    records = _record_events(monkeypatch)
    config = _configs("fake")[0]

    class BrokenCalls(FakeDevice):
        async def on_session(self, _state: SessionState) -> None:
            raise RuntimeError("notification failed")

        def tools(self):
            raise RuntimeError("tools failed")

    device = BrokenCalls(DeviceContext(config.id))
    gateway = DeviceGateway([config], wake_detector_factory=ScriptedDetector, preroll_seconds=1, describe_image=None)
    gateway._entries[config.id] = _DeviceEntry(config, device)
    gateway.owner = config.id

    async def exercise() -> None:
        await gateway.notify(SessionState.wake("session"))

    asyncio.run(exercise())
    assert gateway.owner_tools() == []
    assert [event[0][0] for event in records] == ["device.session_failed", "device.tools_failed"]


def test_status_reports_owner_and_device_state() -> None:
    config_one, config_two = _configs("fake", "fake")
    gateway = DeviceGateway(
        [config_one, config_two],
        wake_detector_factory=ScriptedDetector,
        preroll_seconds=1,
        describe_image=None,
    )
    first = _DeviceEntry(config_one, FakeDevice(DeviceContext(config_one.id)), state="connected", health="ok")
    second = _DeviceEntry(
        config_two, FakeDevice(DeviceContext(config_two.id)), state="reconnecting", health="degraded"
    )
    gateway._entries = {config_one.id: first, config_two.id: second}
    gateway.owner = config_two.id

    assert gateway.status() == [
        {"id": "device-0", "kind": "fake", "state": "connected", "health": "ok", "owner": False},
        {"id": "device-1", "kind": "fake", "state": "reconnecting", "health": "degraded", "owner": True},
    ]
