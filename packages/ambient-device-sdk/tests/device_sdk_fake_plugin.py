"""Module-level entry point targets used by plugin discovery tests."""

from ambient_device_sdk import PROTOCOL_VERSION, Device, DevicePlugin, DeviceContext
from ambient_device_sdk.testing import FakeDevice


def create_fake(context: DeviceContext) -> Device:
    return FakeDevice(context, kind="fake-plugin")


def create_wrong_id(context: DeviceContext) -> Device:
    return FakeDevice(DeviceContext(f"{context.device_id}-wrong"), kind="fake-plugin")


def create_wrong_kind(context: DeviceContext) -> Device:
    return FakeDevice(context, kind="wrong-kind")


def create_raising(context: DeviceContext) -> Device:
    raise RuntimeError("factory failed")


def create_not_device(context: DeviceContext) -> Device:
    return object()  # type: ignore[return-value]


PLUGIN = DevicePlugin("fake-plugin", create_fake)
PLUGIN_ALT = DevicePlugin("fake-plugin", create_fake, description="alternate")
PLUGIN_OLD = DevicePlugin("fake-plugin", create_fake, protocol_version=PROTOCOL_VERSION + 1)
PLUGIN_WRONG_ID = DevicePlugin("fake-plugin", create_wrong_id)
PLUGIN_WRONG_KIND = DevicePlugin("fake-plugin", create_wrong_kind)
PLUGIN_RAISING = DevicePlugin("fake-plugin", create_raising)
PLUGIN_NOT_DEVICE = DevicePlugin("fake-plugin", create_not_device)
NOT_A_PLUGIN = object()
