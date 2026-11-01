"""Value type validation for the public device contract."""

import pytest

from ambient_device_sdk import (
    ToolSpec,
    Capability,
    AudioFormat,
    DeviceHealth,
    HealthStatus,
    SessionPhase,
    SessionState,
)


async def _handler(arguments: dict[str, object]) -> dict[str, object]:
    return arguments


def test_audio_format_validation_and_calculations() -> None:
    with pytest.raises(ValueError):
        AudioFormat(0)
    with pytest.raises(ValueError):
        AudioFormat(16_000, channels=0)

    mono = AudioFormat(16_000)
    stereo = AudioFormat(16_000, channels=2)
    assert mono.bytes_per_frame == 2
    assert stereo.bytes_per_frame == 4
    assert mono.is_whole_frames(b"\x00\x00")
    assert not mono.is_whole_frames(b"\x00")
    assert mono.duration(b"\x00" * 32_000) == 1.0
    assert mono.byte_count(0.5) == 16_000
    with pytest.raises(ValueError):
        mono.byte_count(-0.1)


def test_device_health_validation_and_details() -> None:
    ok = DeviceHealth.ok(temperature=21.5)
    assert ok.reason is None
    assert ok.detail == {"temperature": 21.5}
    assert DeviceHealth.degraded("mic_silent", retries=2).detail == {"retries": 2}
    assert DeviceHealth.down("unreachable").status is HealthStatus.DOWN
    with pytest.raises(ValueError):
        DeviceHealth(HealthStatus.DEGRADED)


def test_enum_values_are_frozen() -> None:
    assert {item.value for item in Capability} == {"listen", "speak", "move", "see", "text"}
    assert {item.value for item in HealthStatus} == {"ok", "degraded", "down"}
    assert {item.value for item in SessionPhase} == {
        "wake",
        "listening",
        "speaking",
        "idle",
        "interrupted",
        "sleep",
    }


def test_session_state_active_phases_and_invariants() -> None:
    assert SessionState.active("s", listening=False, speaking=True).phase is SessionPhase.SPEAKING
    assert SessionState.active("s", listening=True, speaking=False).phase is SessionPhase.LISTENING
    assert SessionState.active("s", listening=False, speaking=False).phase is SessionPhase.IDLE
    assert SessionState.active("s", listening=True, speaking=True).phase is SessionPhase.SPEAKING
    assert SessionState.interrupted("s").listening
    assert not SessionState.interrupted("s").speaking

    for phase in (SessionPhase.WAKE, SessionPhase.SLEEP):
        with pytest.raises(ValueError):
            SessionState(phase, "s", listening=True)
    with pytest.raises(ValueError):
        SessionState(SessionPhase.INTERRUPTED, "s", listening=True, speaking=True)
    with pytest.raises(ValueError):
        SessionState(SessionPhase.IDLE, "s", listening=True)
    with pytest.raises(ValueError):
        SessionState(SessionPhase.WAKE, "s", reason="not sleep")
    with pytest.raises(ValueError):
        SessionState.active("", listening=False, speaking=False)
    assert SessionState.sleep("s", "finished").reason == "finished"


@pytest.mark.asyncio
async def test_tool_spec_validation_schema_copy_and_async_handler() -> None:
    parameters: dict[str, object] = {"type": "object", "properties": {}}
    tool = ToolSpec("valid_tool-1", "A valid tool", parameters, _handler)
    schema = tool.schema()
    assert schema == {"name": "valid_tool-1", "description": "A valid tool", "parameters": parameters}
    assert schema["parameters"] is not parameters
    assert "handler" not in schema

    for name in ("", "has space", "x" * 65):
        with pytest.raises(ValueError):
            ToolSpec(name, "description", parameters, _handler)
    with pytest.raises(ValueError):
        ToolSpec("valid", " ", parameters, _handler)
    with pytest.raises(ValueError):
        ToolSpec("valid", "description", {}, _handler)

    assert await tool.handler({"answer": 42}) == {"answer": 42}
