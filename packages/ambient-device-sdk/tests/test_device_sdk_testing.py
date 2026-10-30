"""Contract checker and fake device behavior."""

import pytest

from ambient_device_sdk import (
    ToolSpec,
    Capability,
    AudioFormat,
    SessionState,
    DeviceContext,
)
from ambient_device_sdk.testing import FakeDevice, run_device_contract, check_device_contract


async def _tool_handler(arguments: dict[str, object]) -> dict[str, object]:
    return arguments


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name, "test tool", {"type": "object"}, _tool_handler)


@pytest.mark.asyncio
async def test_fake_device_passes_contract_checks() -> None:
    device = FakeDevice(
        DeviceContext("fake"),
        capabilities=frozenset(Capability),
    )
    assert check_device_contract(device) == []
    assert await run_device_contract(device) == []


def test_contract_checker_reports_bad_objects_formats_and_duplicate_tools() -> None:
    assert check_device_contract(object()) == ["object does not implement the Device protocol"]

    no_mic_format = FakeDevice(
        DeviceContext("fake"),
        capabilities=frozenset({Capability.LISTEN}),
        mic_format=None,
    )
    assert "mic_format must be set if and only if listen is a capability" in check_device_contract(no_mic_format)

    duplicate_tools = FakeDevice(
        DeviceContext("fake"),
        capabilities=frozenset(),
        mic_format=None,
        speaker_format=None,
        tools=(_tool("same"), _tool("same")),
    )
    assert "tool names must be unique" in check_device_contract(duplicate_tools)


@pytest.mark.asyncio
async def test_fake_device_streams_stop_and_restart_and_records_sessions() -> None:
    device = FakeDevice(DeviceContext("fake"))
    await device.start()
    mic_iterator = device.mic()
    request_iterator = device.requests()
    assert await anext(mic_iterator) == b"\x00" * AudioFormat(16_000).bytes_per_frame

    mic_chunk = b"\x01\x02"
    device.push_mic(mic_chunk)
    assert await anext(mic_iterator) == mic_chunk
    device.push_request("hello")
    assert await anext(request_iterator) == "hello"

    first = SessionState.wake("one")
    second = SessionState.sleep("one", "done")
    await device.on_session(first)
    await device.on_session(second)
    assert device.sessions == [first, second]

    await device.stop()
    with pytest.raises(StopAsyncIteration):
        await anext(mic_iterator)
    with pytest.raises(StopAsyncIteration):
        await anext(request_iterator)

    await device.start()
    assert device.start_count == 2
    restarted_requests = device.requests()
    device.push_request("again")
    assert await anext(restarted_requests) == "again"
    await device.stop()
    with pytest.raises(StopAsyncIteration):
        await anext(restarted_requests)
