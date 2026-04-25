"""HA Update Monitor — checks for Home Assistant updates and announces them."""
from __future__ import annotations

import asyncio
import time

import httpx
from avatar_backend.services._shared_http import _http_client
import structlog

_LOGGER = structlog.get_logger()
_CHECK_INTERVAL = 3600  # check every hour
_COOLDOWN = 86400  # don't re-announce same update for 24h


class HAUpdateMonitor:
    def __init__(self, ha_url: str, ha_token: str, announce_fn, ha_ws_manager=None) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._token = ha_token
        self._announce = announce_fn
        self._ha_ws_manager = ha_ws_manager
        self._announced: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="ha_update_monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self._check()
            except Exception as exc:
                _LOGGER.warning("ha_update_monitor.error", error=str(exc)[:100])
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _check(self) -> None:
        all_states = None
        ws_mgr = self._ha_ws_manager
        if ws_mgr and ws_mgr.is_connected:
            all_states = ws_mgr.get_all_states()

        if not all_states:
            r = await _http_client().get(
                f"{self._ha_url}/api/states",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10.0,
            )
            if r.status_code != 200:
                return
            all_states = r.json()

        now = time.monotonic()
        updates = []
        for s in all_states:
            eid = s.get("entity_id", "")
            if not eid.startswith("update.") or s.get("state") != "on":
                continue
            if now - self._announced.get(eid, 0) < _COOLDOWN:
                continue
            attrs = s.get("attributes", {})
            name = attrs.get("friendly_name", eid)
            ver = attrs.get("latest_version", "")
            updates.append(f"{name} ({ver})" if ver else name)
            self._announced[eid] = now

        if updates:
            msg = "Home Assistant updates available: " + ", ".join(updates) + "."
            _LOGGER.info("ha_update_monitor.announcing", count=len(updates))
            await self._announce(msg, priority="normal")
