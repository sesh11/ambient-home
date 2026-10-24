"""Detector registry and the single detector swap point."""

from pathlib import Path
from collections.abc import Callable

from ambient_home.config import AmbientSettings
from ambient_home.incidents.base import Detector
from ambient_home.incidents.rules import RuleDetector


DETECTORS: dict[str, Callable[[AmbientSettings], Detector]] = {
    "rules": lambda settings: RuleDetector.from_file(
        settings.incident_rules_path or Path(__file__).parent.parent / "incident_rules.toml"
    ),
}


def build_detector(settings: AmbientSettings) -> Detector:
    """Build the configured detector, reporting registered choices on failure."""
    factory = DETECTORS.get(settings.detector)
    if factory is None:
        raise ValueError(f"unknown detector {settings.detector!r}; valid names: {', '.join(sorted(DETECTORS))}")
    return factory(settings)
