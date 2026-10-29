"""Plugin registration and discovery through Python entry points."""

import re
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Iterable
from importlib.metadata import EntryPoint, entry_points

from ambient_device_sdk.types import EventSink, DescribeImage, null_event_sink
from ambient_device_sdk.device import Device, DeviceContext
from ambient_device_sdk._version import PROTOCOL_VERSION


ENTRY_POINT_GROUP = "ambient_device_sdk.devices"
"""Entry-point group. Entry name = plugin kind; value = a module-level :class:`DevicePlugin` object."""

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class DevicePluginError(Exception):
    """Base class for plugin lookup and construction errors."""


class PluginNotFoundError(DevicePluginError):
    """No installed distribution provides the requested kind."""


class PluginLoadError(DevicePluginError):
    """The entry point is ambiguous, fails to import, or is not a DevicePlugin of the right kind."""


class IncompatiblePluginError(DevicePluginError):
    """The plugin targets a different device protocol version."""


class DeviceCreateError(DevicePluginError):
    """The plugin's factory raised or returned something that breaks the contract."""


@dataclass(frozen=True, slots=True)
class DevicePlugin:
    """What a device package exports under :data:`ENTRY_POINT_GROUP`.

    ``create`` must be cheap and side-effect free: no hardware access, threads, subprocesses
    or network. All of that happens in ``Device.start()``.
    """

    kind: str
    create: Callable[[DeviceContext], Device]
    protocol_version: int = PROTOCOL_VERSION
    description: str = ""

    def __post_init__(self) -> None:
        """Validate the kind."""
        if not _ID.fullmatch(self.kind):
            raise ValueError("kind must match ^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """One configured device: which plugin to use, under which id, with which settings."""

    id: str
    kind: str
    enabled: bool = True
    config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the id and kind."""
        if not _ID.fullmatch(self.id):
            raise ValueError("device id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
        if not _ID.fullmatch(self.kind):
            raise ValueError("device kind must match ^[a-z0-9][a-z0-9_-]{0,63}$")

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "DeviceConfig":
        """Build from one parsed table: ``{"id": str, "kind": str, "enabled"?: bool, "config"?: table}``."""
        unknown = set(data) - {"id", "kind", "enabled", "config"}
        if unknown:
            raise ValueError(f"unknown device keys: {sorted(unknown)}")
        device_id, kind = data.get("id"), data.get("kind")
        enabled, config = data.get("enabled", True), data.get("config", {})
        if not isinstance(device_id, str) or not isinstance(kind, str):
            raise ValueError("device id and kind must be strings")
        if not isinstance(enabled, bool):
            raise ValueError("device enabled must be a boolean")
        if not isinstance(config, Mapping):
            raise ValueError("device config must be a table")
        return cls(device_id, kind, enabled, dict(config))


def parse_device_configs(entries: Iterable[Mapping[str, object]]) -> list[DeviceConfig]:
    """Parse a list of device tables in order, rejecting duplicate ids."""
    configs = [DeviceConfig.from_mapping(entry) for entry in entries]
    seen: set[str] = set()
    for config in configs:
        if config.id in seen:
            raise ValueError(f"duplicate device id: {config.id}")
        seen.add(config.id)
    return configs


def available_kinds(*, eps: Iterable[EntryPoint] | None = None) -> list[str]:
    """Return the sorted, de-duplicated kinds installed in this environment (nothing is imported)."""
    return sorted({ep.name for ep in _group(eps)})


def load_plugin(kind: str, *, eps: Iterable[EntryPoint] | None = None) -> DevicePlugin:
    """Import and validate the plugin registered for ``kind``.

    ``eps`` overrides the installed entry points (for tests).
    """
    matches = [ep for ep in _group(eps) if ep.name == kind]
    if not matches:
        raise PluginNotFoundError(f"no device plugin installed for kind {kind!r}")
    if len({ep.value for ep in matches}) > 1:
        raise PluginLoadError(f"more than one device plugin installed for kind {kind!r}")
    try:
        plugin = matches[0].load()
    except Exception as exc:
        raise PluginLoadError(f"device plugin {kind!r} failed to import: {type(exc).__name__}") from exc
    if not isinstance(plugin, DevicePlugin):
        raise PluginLoadError(f"entry point for {kind!r} is not a DevicePlugin")
    if plugin.kind != kind:
        raise PluginLoadError(f"entry point name {kind!r} does not match plugin kind {plugin.kind!r}")
    if plugin.protocol_version != PROTOCOL_VERSION:
        raise IncompatiblePluginError(
            f"device plugin {kind!r} targets protocol {plugin.protocol_version}, SDK is {PROTOCOL_VERSION}"
        )
    return plugin


def create_device(
    config: DeviceConfig,
    *,
    emit: EventSink = null_event_sink,
    describe_image: DescribeImage | None = None,
    eps: Iterable[EntryPoint] | None = None,
) -> Device:
    """Load the plugin for ``config.kind`` and build (not start) the device."""
    plugin = load_plugin(config.kind, eps=eps)
    context = DeviceContext(config.id, dict(config.config), emit, describe_image)
    try:
        device = plugin.create(context)
    except Exception as exc:
        raise DeviceCreateError(f"device {config.id!r} failed to build: {type(exc).__name__}: {exc}") from exc
    if not isinstance(device, Device):
        raise DeviceCreateError(f"device {config.id!r}: factory did not return a Device")
    if device.id != config.id or device.kind != config.kind:
        raise DeviceCreateError(f"device {config.id!r}: id/kind do not match the config")
    return device


def _group(eps: Iterable[EntryPoint] | None) -> list[EntryPoint]:
    if eps is None:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    return [ep for ep in eps if ep.group == ENTRY_POINT_GROUP]
