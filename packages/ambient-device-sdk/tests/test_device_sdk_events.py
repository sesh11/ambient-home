"""Event callback typing and dispatch."""

from typing import cast
from collections.abc import Callable

from ambient_device_sdk import EventSink, EventLevel, EventValue, DeviceContext, null_event_sink
from ambient_device_sdk.testing import FakeDevice


def test_null_event_sink_returns_none() -> None:
    sink = cast(Callable[..., object], null_event_sink)
    assert sink("device.test", "test") is None


def test_positional_only_recording_sink_is_an_event_sink() -> None:
    events: list[tuple[str, str, EventLevel, dict[str, EventValue]]] = []

    def record(
        type: str,
        component: str,
        level: EventLevel = "info",
        /,
        **data: EventValue,
    ) -> None:
        events.append((type, component, level, data))

    sink: EventSink = record
    device = FakeDevice(DeviceContext("fake", emit=sink))
    device.context.emit("device.ready", "fake", "warning", attempt=3)
    assert events == [("device.ready", "fake", "warning", {"attempt": 3})]
