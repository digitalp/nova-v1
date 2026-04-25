"""Teardown — stops all background services in reverse startup order."""
from __future__ import annotations

import asyncio

import structlog

from avatar_backend.bootstrap.container import AppContainer
from avatar_backend.bootstrap.lifecycle import Lifecycle
from avatar_backend.services._shared_http import close_shared_http_client

# Services stopped in this order (reverse of startup)
_LIFECYCLE_SERVICES = [
    "sys_metrics",
    "open_loop_automation_service",
    "ha_ws_manager",
    "update_monitor",
    "sensor_watch",
    "proactive_service",
]


async def teardown(container: AppContainer) -> None:
    """Stop all background tasks and services in reverse order."""
    logger = structlog.get_logger()

    # Cancel background asyncio tasks
    for task in container._background_tasks:
        task.cancel()
    for task in container._background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Stop lifecycle services in order
    for name in _LIFECYCLE_SERVICES:
        svc = getattr(container, name, None)
        if svc is not None and isinstance(svc, Lifecycle):
            try:
                await svc.stop()
            except Exception as exc:
                logger.warning("shutdown.service_stop_failed", service=name, exc=str(exc)[:100])

    # Close the shared httpx client
    if container.ha_proxy:
        await container.ha_proxy.close()
    await close_shared_http_client()

    logger.info("avatar_backend.stopped")
