"""Process-local runtime settings bridge for external tools."""

from ambient_home.config import AmbientSettings


_settings: AmbientSettings | None = None


def set_settings(settings: AmbientSettings) -> None:
    """Set settings used by external tools."""
    global _settings
    _settings = settings


def get_settings() -> AmbientSettings:
    """Return settings configured by the application entry point."""
    if _settings is None:
        raise RuntimeError("ambient-home settings have not been initialized")
    return _settings
