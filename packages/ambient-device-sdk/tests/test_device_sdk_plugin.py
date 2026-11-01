"""Configuration parsing, plugin discovery, and construction."""

from typing import cast
from importlib.metadata import EntryPoint

import pytest

from ambient_device_sdk import (
    ENTRY_POINT_GROUP,
    Device,
    EventLevel,
    EventValue,
    DeviceConfig,
    DevicePlugin,
    DeviceContext,
    PluginLoadError,
    DeviceCreateError,
    DevicePluginError,
    PluginNotFoundError,
    IncompatiblePluginError,
    load_plugin,
    create_device,
    available_kinds,
    parse_device_configs,
)
from ambient_device_sdk.testing import FakeDevice


_HELPER = "device_sdk_fake_plugin"


def _ep(name: str, attribute: str, *, group: str = ENTRY_POINT_GROUP) -> EntryPoint:
    return EntryPoint(name=name, value=f"{_HELPER}:{attribute}", group=group)


def _eps(*attributes: str) -> list[EntryPoint]:
    return [_ep("fake-plugin", attribute) for attribute in attributes]


def _not_a_device(context: DeviceContext) -> Device:
    return cast(Device, object())


def test_device_plugin_and_config_validation() -> None:
    with pytest.raises(ValueError):
        DevicePlugin("has space", _not_a_device)

    with pytest.raises(ValueError):
        DeviceConfig("Bad ID", "fake-plugin")
    with pytest.raises(ValueError):
        DeviceConfig("fake-id", "Bad Kind")
    with pytest.raises(ValueError):
        DeviceConfig.from_mapping({"id": 4, "kind": "fake-plugin"})
    with pytest.raises(ValueError):
        DeviceConfig.from_mapping({"id": "id", "kind": "fake-plugin", "enabled": 1})
    with pytest.raises(ValueError):
        DeviceConfig.from_mapping({"id": "id", "kind": "fake-plugin", "config": []})
    with pytest.raises(ValueError):
        DeviceConfig.from_mapping({"id": "id", "kind": "fake-plugin", "extra": True})

    config = DeviceConfig.from_mapping({"id": "desk", "kind": "fake-plugin"})
    assert config.enabled is True
    assert config.config == {}
    assert DeviceConfig.from_mapping(
        {"id": "desk", "kind": "fake-plugin", "enabled": False, "config": {"value": 3}}
    ).config == {"value": 3}


def test_parse_device_configs_preserves_order_and_rejects_duplicate_ids() -> None:
    parsed = parse_device_configs(
        [
            {"id": "second", "kind": "fake-plugin"},
            {"id": "first", "kind": "fake-plugin", "enabled": False},
        ]
    )
    assert [item.id for item in parsed] == ["second", "first"]
    with pytest.raises(ValueError, match="duplicate device id"):
        parse_device_configs([{"id": "same", "kind": "fake-plugin"}, {"id": "same", "kind": "fake-plugin"}])


def test_available_kinds_is_sorted_deduplicated_and_group_scoped() -> None:
    eps = [
        _ep("z-kind", "PLUGIN"),
        _ep("a-kind", "PLUGIN"),
        _ep("z-kind", "PLUGIN"),
        _ep("ignored", "PLUGIN", group="other.group"),
    ]
    assert available_kinds(eps=eps) == ["a-kind", "z-kind"]


def test_load_plugin_success_and_failures() -> None:
    assert load_plugin("fake-plugin", eps=_eps("PLUGIN")).kind == "fake-plugin"
    with pytest.raises(PluginNotFoundError):
        load_plugin("missing", eps=[])
    with pytest.raises(PluginLoadError, match="more than one"):
        load_plugin("fake-plugin", eps=_eps("PLUGIN", "PLUGIN_ALT"))
    assert load_plugin("fake-plugin", eps=_eps("PLUGIN", "PLUGIN")).kind == "fake-plugin"
    with pytest.raises(PluginLoadError, match="failed to import"):
        load_plugin("fake-plugin", eps=[_ep("fake-plugin", "missing_module:PLUGIN")])
    with pytest.raises(PluginLoadError, match="not a DevicePlugin"):
        load_plugin("fake-plugin", eps=_eps("NOT_A_PLUGIN"))
    with pytest.raises(PluginLoadError, match="does not match"):
        load_plugin("renamed-plugin", eps=[_ep("renamed-plugin", "PLUGIN")])
    with pytest.raises(IncompatiblePluginError):
        load_plugin("fake-plugin", eps=_eps("PLUGIN_OLD"))
    for error_type in (
        DevicePluginError,
        PluginNotFoundError,
        PluginLoadError,
        IncompatiblePluginError,
        DeviceCreateError,
    ):
        assert issubclass(error_type, DevicePluginError)


@pytest.mark.asyncio
async def test_create_device_passes_context_and_does_not_start() -> None:
    async def describe_image(image: bytes, question: str) -> str:
        return f"{len(image)}:{question}"

    config = DeviceConfig("desk", "fake-plugin", config={"volume": 3})

    def emit(
        type: str,
        component: str,
        level: EventLevel = "info",
        /,
        **data: EventValue,
    ) -> None:
        return None

    device = create_device(
        config,
        emit=emit,
        describe_image=describe_image,
        eps=_eps("PLUGIN"),
    )
    assert device.id == "desk"
    assert device.kind == "fake-plugin"
    assert isinstance(device, FakeDevice)
    assert not device.started
    assert device.context.device_id == "desk"
    assert device.context.config == {"volume": 3}
    assert device.context.emit is emit
    assert device.context.describe_image is describe_image


def test_create_device_wraps_factory_and_contract_errors() -> None:
    config = DeviceConfig("desk", "fake-plugin")
    with pytest.raises(DeviceCreateError, match="factory failed"):
        create_device(config, eps=_eps("PLUGIN_RAISING"))
    with pytest.raises(DeviceCreateError, match="did not return"):
        create_device(config, eps=_eps("PLUGIN_NOT_DEVICE"))
    with pytest.raises(DeviceCreateError, match="id/kind"):
        create_device(config, eps=_eps("PLUGIN_WRONG_ID"))
    with pytest.raises(DeviceCreateError, match="id/kind"):
        create_device(config, eps=_eps("PLUGIN_WRONG_KIND"))
