"""ambient-device-sdk: the interface every ambient-home device connector implements.

Pure standard library. Import everything from this top-level module; submodules are private
except ``ambient_device_sdk.testing``.
"""

from ambient_device_sdk.types import (
    SAMPLE_WIDTH_BYTES,
    ToolSpec,
    EventSink,
    Capability,
    EventLevel,
    EventValue,
    ToolResult,
    AudioFormat,
    ToolHandler,
    DeviceHealth,
    HealthStatus,
    SessionPhase,
    SessionState,
    DescribeImage,
    null_event_sink,
)
from ambient_device_sdk.device import Device, DeviceContext
from ambient_device_sdk.plugin import (
    ENTRY_POINT_GROUP,
    DeviceConfig,
    DevicePlugin,
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
from ambient_device_sdk._version import PROTOCOL_VERSION, __version__


__all__ = [
    "ENTRY_POINT_GROUP",
    "PROTOCOL_VERSION",
    "SAMPLE_WIDTH_BYTES",
    "AudioFormat",
    "Capability",
    "DescribeImage",
    "Device",
    "DeviceConfig",
    "DeviceContext",
    "DeviceCreateError",
    "DeviceHealth",
    "DevicePlugin",
    "DevicePluginError",
    "EventLevel",
    "EventSink",
    "EventValue",
    "HealthStatus",
    "IncompatiblePluginError",
    "PluginLoadError",
    "PluginNotFoundError",
    "SessionPhase",
    "SessionState",
    "ToolHandler",
    "ToolResult",
    "ToolSpec",
    "__version__",
    "available_kinds",
    "create_device",
    "load_plugin",
    "null_event_sink",
    "parse_device_configs",
]
