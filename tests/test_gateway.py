"""Device gateway startup and idle behavior."""

import asyncio

import numpy as np

from ambient_device_sdk import AudioFormat, DeviceContext, parse_device_configs
from ambient_home.audio import PrerollBuffer, PcmStreamConverter
from ambient_home.gateway import DeviceGateway, _DeviceEntry, load_device_configs
from ambient_device_sdk.testing import FakeDevice


def test_missing_device_file_is_optional(tmp_path) -> None:
    assert load_device_configs(tmp_path / "missing.toml") == []


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
        await gateway.stop()
        await task

    asyncio.run(exercise())


def test_owner_audio_routes_to_conversation_without_wake_detection() -> None:
    class Listener:
        streaming = True
        sent = []

        def is_streaming(self) -> bool:
            return self.streaming

        async def send_audio(self, mono) -> None:
            self.sent.append(mono.copy())

        def request_wake(self, device_id: str) -> bool:
            return False

        def device_lost(self, device_id: str) -> None:
            return

    class Detector:
        def feed(self, samples):
            raise AssertionError("owner audio must not reach the wake detector")

    config = parse_device_configs([{"id": "desk", "kind": "fake"}])[0]
    device = FakeDevice(DeviceContext("desk"))
    gateway = DeviceGateway(
        [config],
        wake_detector_factory=lambda: Detector(),
        preroll_seconds=1.0,
        describe_image=None,
    )
    gateway._entries["desk"] = _DeviceEntry(
        config,
        device,
        converter=PcmStreamConverter(AudioFormat(16000), 16000),
        preroll=PrerollBuffer(1.0, 16000),
        detector=Detector(),
    )
    gateway.owner = "desk"
    listener = Listener()
    gateway.set_listener(listener)

    async def exercise() -> None:
        await gateway._route_audio(gateway._entries["desk"], np.array([10, 20], dtype=np.int16))

    asyncio.run(exercise())
    np.testing.assert_array_equal(listener.sent[0], np.array([10, 20], dtype=np.int16))
