"""
ProactiveService — watches Home Assistant state changes via WebSocket and
asks the LLM if anything warrants a proactive announcement.

Flow:
  HA WS state_changed events
    → filter to important domains + non-trivial state changes
    → batch for BATCH_WINDOW_S seconds
    → LLM triage (generate_text, 30 s timeout)
    → if LLM says announce → call announce_fn(message, priority)
    → mark entities on cooldown (10 min) to avoid spam
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

_LOGGER = structlog.get_logger()

# Domains monitored for proactive announcements.
# NOTE: 'sensor' is intentionally excluded — numeric sensors emit constant updates
# and threshold-based alerts are already handled by dedicated HA automations.
_WATCH_DOMAINS = {
    "binary_sensor",
    "lock",
    "cover",
    "alarm_control_panel",
    "person",
    "device_tracker",
    "input_boolean",
}

# States that are noise — skip silently
_NOISE_STATES = {"unavailable", "unknown", "none"}

# Minimum seconds before re-announcing the same entity
_COOLDOWN_S = 600  # 10 minutes

# Minimum seconds between ANY proactive announcements (global rate limit)
_GLOBAL_ANNOUNCE_COOLDOWN_S = 300  # 5 minutes

# Collect changes for this many seconds before triaging
_BATCH_WINDOW_S = 60  # increased from 30s to reduce triage frequency

# Max changes passed to LLM per batch
_MAX_CHANGES = 20


class ProactiveService:
    """
    Subscribes to HA WebSocket state_changed events and makes proactive
    announcements when the LLM judges them worth saying.
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        llm_service,
        announce_fn: Callable[[str, str], Awaitable[None]],
        system_prompt: str,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._llm = llm_service
        self._announce = announce_fn
        self._system_prompt = system_prompt
        self._cooldowns: dict[str, float] = {}
        self._last_announce_time: float = 0.0
        self._queue: list[dict] = []
        self._task: asyncio.Task | None = None

    def update_system_prompt(self, prompt: str) -> None:
        """Called by sync-prompt to keep the proactive context current."""
        self._system_prompt = prompt
        _LOGGER.info("proactive.prompt_updated", chars=len(prompt))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="proactive_monitor")
        _LOGGER.info("proactive.started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.info("proactive.stopped")

    # ── Main reconnect loop ───────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._ws_loop()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning(
                    "proactive.ws_disconnected",
                    exc=str(exc),
                    retry_in_s=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    # ── WebSocket connection ──────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        ws_url = (
            self._ha_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/api/websocket"
        )
        _LOGGER.info("proactive.ws_connecting", url=ws_url)

        async with websockets.connect(
            ws_url, ping_interval=30, ping_timeout=10, open_timeout=10
        ) as ws:
            # Handshake
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got {msg.get('type')}")

            await ws.send(json.dumps({"type": "auth", "access_token": self._ha_token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"HA WebSocket auth failed: {msg}")

            _LOGGER.info("proactive.ws_authenticated")

            # Subscribe to state_changed
            await ws.send(json.dumps({
                "id": 1,
                "type": "subscribe_events",
                "event_type": "state_changed",
            }))

            # Confirm subscription
            msg = json.loads(await ws.recv())
            if msg.get("type") != "result" or not msg.get("success"):
                raise RuntimeError(f"subscribe_events failed: {msg}")

            _LOGGER.info("proactive.subscribed_to_state_changed")

            # Start batch processor alongside event loop
            batch_task = asyncio.create_task(self._batch_loop(), name="proactive_batcher")
            try:
                async for raw in ws:
                    self._on_message(json.loads(raw))
            finally:
                batch_task.cancel()
                try:
                    await batch_task
                except asyncio.CancelledError:
                    pass

    # ── Event ingestion ───────────────────────────────────────────────────

    def _on_message(self, msg: dict) -> None:
        if msg.get("type") != "event":
            return
        event = msg.get("event", {})
        if event.get("event_type") != "state_changed":
            return

        data = event.get("data", {})
        entity_id: str = data.get("entity_id", "")
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        if domain not in _WATCH_DOMAINS:
            return

        new_state = data.get("new_state") or {}
        old_state = data.get("old_state") or {}
        new_val = new_state.get("state", "")
        old_val = old_state.get("state", "")

        if new_val == old_val:
            return
        if new_val in _NOISE_STATES or old_val in _NOISE_STATES:
            return

        # For binary_sensor: only queue transitions TO "on" — "off" transitions
        # are rarely actionable and generate most of the noise.
        if domain == "binary_sensor" and new_val != "on":
            return

        # Cooldown check
        if time.monotonic() - self._cooldowns.get(entity_id, 0) < _COOLDOWN_S:
            return

        friendly = new_state.get("attributes", {}).get("friendly_name", entity_id)

        self._queue.append({
            "entity_id": entity_id,
            "friendly": friendly,
            "old": old_val,
            "new": new_val,
        })
        _LOGGER.debug(
            "proactive.event_queued",
            entity_id=entity_id, old=old_val, new=new_val,
        )

    # ── Batch triage ──────────────────────────────────────────────────────

    async def _batch_loop(self) -> None:
        while True:
            await asyncio.sleep(_BATCH_WINDOW_S)
            if not self._queue:
                continue
            batch = self._queue[:_MAX_CHANGES]
            self._queue.clear()
            try:
                await self._triage(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("proactive.triage_error", exc=str(exc))

    async def _triage(self, changes: list[dict]) -> None:
        """Ask the LLM if any of these state changes warrant a spoken announcement."""

        # Global rate limit — don't announce if we just announced recently
        since_last = time.monotonic() - self._last_announce_time
        if since_last < _GLOBAL_ANNOUNCE_COOLDOWN_S:
            _LOGGER.debug(
                "proactive.global_cooldown_active",
                seconds_remaining=int(_GLOBAL_ANNOUNCE_COOLDOWN_S - since_last),
            )
            return

        lines = "\n".join(
            f"- {c['friendly']} ({c['entity_id']}): {c['old']} → {c['new']}"
            for c in changes
        )
        _LOGGER.info("proactive.triaging", n_changes=len(changes))

        # Trim system prompt to keep token cost reasonable
        prompt_ctx = self._system_prompt[:3000]

        prompt = (
            "You are Nova's proactive home monitor. Review these Home Assistant state "
            "changes and decide if any warrant a spoken announcement.\n\n"
            f"Home context:\n{prompt_ctx}\n\n"
            f"State changes:\n{lines}\n\n"
            "STRICT RULES — the default answer is NO. Only announce if the event:\n"
            "  • Is a genuine safety or security concern (alarm triggered, unexpected "
            "door/lock change, smoke/CO detector, flood)\n"
            "  • Requires immediate human action (door left open, critical alert)\n"
            "  • Is a clear exception or anomaly that the household would want to know "
            "RIGHT NOW and could not figure out themselves\n\n"
            "DO NOT ANNOUNCE any of the following — return {\"announce\": false}:\n"
            "  • Motion sensors, presence sensors, occupancy sensors detecting movement\n"
            "  • Routine door open/close during normal waking hours\n"
            "  • Any light, switch, or media player change\n"
            "  • Person/device_tracker arriving or leaving home\n"
            "  • Climate mode or setpoint changes\n"
            "  • Any binary_sensor that is merely reporting a normal condition\n"
            "  • Anything already handled by a dedicated HA automation\n\n"
            "Speak as Nova, naturally in first person. Keep it brief.\n\n"
            "Reply with JSON only (no markdown fences):\n"
            '{"announce": true, "message": "...", "priority": "normal"}\n'
            "or\n"
            '{"announce": false}'
        )

        try:
            raw = await self._llm.generate_text(prompt, timeout_s=30.0)
        except Exception as exc:
            _LOGGER.warning("proactive.llm_failed", exc=str(exc))
            return

        # Strip markdown fences if model wrapped the response
        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.warning("proactive.bad_json", raw=raw[:300])
            return

        if not result.get("announce"):
            _LOGGER.debug("proactive.no_action")
            return

        message = (result.get("message") or "").strip()
        priority = result.get("priority", "normal")
        if priority not in ("normal", "alert"):
            priority = "normal"

        if not message:
            return

        _LOGGER.info("proactive.announcing", chars=len(message), priority=priority)

        # Apply cooldowns
        now = time.monotonic()
        self._last_announce_time = now
        for c in changes:
            self._cooldowns[c["entity_id"]] = now

        await self._announce(message, priority)
