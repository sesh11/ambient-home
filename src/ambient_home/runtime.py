"""Process-local runtime settings bridge for external tools."""

from ambient_home.costs import CostTracker
from ambient_home.config import AmbientSettings
from ambient_home.jobs.service import JobService


_settings: AmbientSettings | None = None
_job_service: JobService | None = None
_cost_tracker: CostTracker | None = None


def set_settings(settings: AmbientSettings) -> None:
    """Set settings used by external tools."""
    global _settings
    _settings = settings


def get_settings() -> AmbientSettings:
    """Return settings configured by the application entry point."""
    if _settings is None:
        raise RuntimeError("ambient-home settings have not been initialized")
    return _settings


def set_job_service(service: JobService) -> None:
    """Set the job service used by external tools and the local UI."""
    global _job_service
    _job_service = service


def get_job_service() -> JobService:
    """Return the configured background job service."""
    if _job_service is None:
        raise RuntimeError("ambient-home job service has not been initialized")
    return _job_service


def set_cost_tracker(tracker: CostTracker) -> None:
    """Set the cost tracker used by external tools and the local UI."""
    global _cost_tracker
    _cost_tracker = tracker


def get_cost_tracker() -> CostTracker:
    """Return the configured cost tracker."""
    if _cost_tracker is None:
        raise RuntimeError("ambient-home cost tracker has not been initialized")
    return _cost_tracker
