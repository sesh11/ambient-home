"""Debounced background incident evaluation."""

import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta
from collections.abc import Callable

from ambient_home.incidents.base import Detector, Incident
from ambient_home.telemetry.events import Event, EventLog


logger = logging.getLogger(__name__)


class IncidentMonitor:
    """Re-evaluate a detector after relevant events arrive."""

    def __init__(
        self,
        detector: Detector,
        event_log: EventLog,
        *,
        lookback: timedelta,
        debounce_s: float = 30.0,
    ) -> None:
        """Register event notifications and configure evaluation timing."""
        self.detector = detector
        self.event_log = event_log
        self.lookback = lookback
        self.debounce_s = debounce_s
        self.latest: list[Incident] = []
        self._dirty: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen: set[str] = set()
        self._ready: set[str] = set()
        self._listener_lock = threading.Lock()
        self._incident_listeners: list[Callable[[list[Incident]], None]] = []
        event_log.add_listener(self._on_event)

    def add_listener(self, callback: Callable[[list[Incident]], None]) -> None:
        """Register a callback that receives every completed detection result."""
        self._incident_listeners.append(callback)

    def _on_event(self, event: Event) -> None:
        if event.component == "factory" or event.type == "app.heartbeat":
            return
        with self._listener_lock:
            loop = self._loop
            dirty = self._dirty
        if loop is not None and dirty is not None and not loop.is_closed():
            loop.call_soon_threadsafe(dirty.set)

    async def run(self) -> None:
        """Wait for changes and evaluate incidents until cancelled."""
        self._loop = asyncio.get_running_loop()
        self._dirty = asyncio.Event()
        self._dirty.set()
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            if self.debounce_s > 0:
                await asyncio.sleep(self.debounce_s)
            now = datetime.now(timezone.utc)
            try:
                incidents = await self.detector.detect(self.event_log.read(now - self.lookback, now), now)
                self.latest = incidents
                for listener in self._incident_listeners:
                    try:
                        listener(incidents)
                    except Exception:
                        logger.exception("Incident listener failed")
                for incident in incidents:
                    if incident.fingerprint not in self._seen:
                        self._seen.add(incident.fingerprint)
                        self.event_log.emit(
                            "incident.observed",
                            "factory",
                            fingerprint=incident.fingerprint,
                            rule=incident.rule,
                            count=incident.count,
                            threshold=incident.threshold,
                            severity=incident.severity,
                        )
                    if incident.ready and incident.fingerprint not in self._ready:
                        self._ready.add(incident.fingerprint)
                        logger.warning(
                            "Incident ready: %s (%s, %d/%d)",
                            incident.title,
                            incident.severity,
                            incident.count,
                            incident.threshold,
                        )
                        self.event_log.emit(
                            "incident.ready",
                            "factory",
                            fingerprint=incident.fingerprint,
                            rule=incident.rule,
                            count=incident.count,
                            threshold=incident.threshold,
                            severity=incident.severity,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Incident detection failed")
