"""Bridge external library errors into structured local telemetry."""

import logging

from ambient_home.telemetry.events import emit


class EventLoggingHandler(logging.Handler):
    """Record ERROR+ messages from third-party loggers as telemetry."""

    def emit(self, record: logging.LogRecord) -> None:
        """Forward one external error record to the event log."""
        if record.levelno < logging.ERROR or record.name.startswith("ambient_home"):
            return
        data: dict[str, object] = {"message": record.getMessage()}
        if record.exc_info and record.exc_info[0] is not None:
            data["exc_type"] = record.exc_info[0].__name__
        emit("log.error", record.name, "error", **data)
