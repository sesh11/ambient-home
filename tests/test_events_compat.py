"""Garage SDK event sink compatibility."""

import json
from datetime import datetime, timezone

from garage_sdk import EventLog, emit, configure

from ambient_device_sdk import DeviceContext


def test_device_context_emits_legacy_jsonl_shape_without_resource(tmp_path) -> None:
    log = EventLog(tmp_path, clock=lambda: datetime(2025, 1, 2, tzinfo=timezone.utc))
    configure(log)
    try:
        context = DeviceContext("desk", emit=emit)
        context.emit("device.ready", "device", "warning", sample_rate=16000)
    finally:
        configure(None)

    record = json.loads((tmp_path / "2025-01-02.jsonl").read_text())
    assert set(record) == {"ts", "type", "component", "level", "data"}
    assert record["type"] == "device.ready"
    assert record["component"] == "device"
    assert record["level"] == "warning"
    assert record["data"] == {"sample_rate": 16000}
