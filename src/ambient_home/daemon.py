"""Reachy Mini daemon readiness and liveness checks."""

import os
import time
import asyncio
import logging
from typing import NoReturn
from collections.abc import Callable, Awaitable

import httpx


logger = logging.getLogger(__name__)

DAEMON_LOST_EXIT_CODE = 75
DAEMON_UNAVAILABLE_EXIT_CODE = 69

READY_STATE = "running"


async def fetch_daemon_state(client: httpx.AsyncClient, status_url: str) -> str | None:
    """Return the daemon state, or None when the daemon is unreachable or not ready."""
    try:
        response = await client.get(status_url, timeout=2.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Daemon status request failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    state = payload.get("state")
    return state if isinstance(state, str) else None


async def wait_for_daemon(
    status_url: str,
    timeout_s: float,
    poll_s: float = 1.0,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Poll the daemon status endpoint until it reports running or the timeout elapses."""
    deadline = time.monotonic() + timeout_s
    owned = client is None
    http = client or httpx.AsyncClient()
    last_state: str | None = None
    logged_waiting = False
    try:
        while True:
            state = await fetch_daemon_state(http, status_url)
            if state == READY_STATE:
                logger.info("Reachy daemon ready at %s", status_url)
                return True
            if state != last_state or not logged_waiting:
                logger.info("Waiting for Reachy daemon at %s (state=%s)", status_url, state or "unreachable")
                last_state = state
                logged_waiting = True
            if time.monotonic() >= deadline:
                logger.error("Reachy daemon not ready after %.0f s (state=%s)", timeout_s, state or "unreachable")
                return False
            await asyncio.sleep(poll_s)
    finally:
        if owned:
            await http.aclose()


async def watch_daemon(
    status_url: str,
    poll_s: float,
    max_failures: int,
    on_lost: Callable[[str], Awaitable[None]],
    client: httpx.AsyncClient | None = None,
) -> None:
    """Poll the daemon while running and call ``on_lost`` after consecutive failures."""
    owned = client is None
    http = client or httpx.AsyncClient()
    failures = 0
    try:
        while True:
            await asyncio.sleep(poll_s)
            state = await fetch_daemon_state(http, status_url)
            if state == READY_STATE:
                if failures:
                    logger.info("Reachy daemon reachable again (state=%s)", state)
                failures = 0
                continue
            failures += 1
            logger.warning(
                "Reachy daemon check failed (%d/%d, state=%s)", failures, max_failures, state or "unreachable"
            )
            if failures >= max_failures:
                await on_lost(state or "unreachable")
                return
    finally:
        if owned:
            await http.aclose()


def exit_process(code: int, reason: str) -> NoReturn:
    """Terminate the process immediately so the service supervisor restarts it."""
    logger.error("Exiting with status %d: %s", code, reason)
    logging.shutdown()
    os._exit(code)
