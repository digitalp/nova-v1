"""
PresenceContextService — auto-discovers presence/occupancy/motion sensors from
Home Assistant and builds a brief context note for each conversation turn.

Discovery is cached for 10 minutes so newly added sensors are picked up
automatically without restarting the server.  State reads are cached for 30
seconds so rapid-fire turns don't hammer HA.

Injected note example:
  "Penn home (4 min). Tangu away. Active: Kitchen Motion (on), Front Door (off 3 min ago)."
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import structlog

_LOGGER = structlog.get_logger()

# Binary sensor device_classes treated as presence signals
_PRESENCE_DEVICE_CLASSES = frozenset({"motion", "occupancy", "presence", "moving"})

_DISCOVERY_TTL_S  = 600    # 10 min — re-scan for new sensors
_CONTEXT_TTL_S    = 30     # 30 sec — re-read states
_RECENCY_WINDOW_S = 1200   # 20 min — surface binary sensors that changed within this window
_MAX_SENSORS      = 6      # cap active sensor list to avoid noise
_HA_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)


def _secs_ago(iso_ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def _fmt_duration(s: float) -> str:
    if s < 90:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s / 60)} min"
    return f"{int(s / 3600)}h {int((s % 3600) / 60)}m"


class PresenceContextService:
    """
    Builds a one-line presence context note from live HA state.

    Usage:
        svc = PresenceContextService(ha_url, ha_token)
        note = await svc.get_context()
        # → "Penn home (4 min). Tangu away. Active: Kitchen Motion, Front Door (off 2 min ago)."
    """

    def __init__(self, ha_url: str, ha_token: str) -> None:
        self._ha_url  = ha_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {ha_token}"}

        # Layer 1 — discovered entity IDs (10 min TTL)
        self._entity_ids: list[str] = []
        self._discovery_expires: float = 0.0

        # Layer 2 — formatted context string (30 sec TTL)
        self._context: str = ""
        self._context_expires: float = 0.0

        self._lock = asyncio.Lock()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _fetch_all_states(self) -> list[dict] | None:
        try:
            async with httpx.AsyncClient(timeout=_HA_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._ha_url}/api/states",
                    headers=self._headers,
                )
            return resp.json() if resp.status_code == 200 else None
        except Exception as exc:
            _LOGGER.warning("presence_context.fetch_failed", exc=str(exc))
            return None

    def _discover(self, states: list[dict]) -> list[str]:
        """Filter full state list to person + relevant binary_sensor entities."""
        ids: list[str] = []
        for s in states:
            eid: str = s.get("entity_id", "")
            if not eid:
                continue
            domain = eid.split(".")[0]
            if domain == "person":
                ids.append(eid)
            elif domain == "binary_sensor":
                dc = (s.get("attributes") or {}).get("device_class", "")
                if dc in _PRESENCE_DEVICE_CLASSES:
                    ids.append(eid)
        return ids

    def _build(self, states: list[dict], entity_ids: list[str]) -> str:
        """Format a presence context note from the given states."""
        by_id = {s["entity_id"]: s for s in states}

        person_parts: list[str] = []
        sensor_parts: list[tuple[str, float]] = []  # (text, age_s) — sorted by recency

        for eid in entity_ids:
            s = by_id.get(eid)
            if s is None:
                continue
            domain   = eid.split(".")[0]
            state    = s.get("state", "unknown")
            attrs    = s.get("attributes") or {}
            age_s    = _secs_ago(s.get("last_changed", ""))
            name     = (
                attrs.get("friendly_name")
                or eid.split(".", 1)[1].replace("_", " ").title()
            )

            if domain == "person":
                if state == "home":
                    note = (
                        f"{name} home ({_fmt_duration(age_s)} ago)"
                        if age_s is not None and age_s < 3600
                        else f"{name} home"
                    )
                else:
                    note = f"{name} away"
                person_parts.append(note)

            elif domain == "binary_sensor":
                if state == "on":
                    sensor_parts.append((f"{name} (on)", age_s or 99999))
                elif age_s is not None and age_s < _RECENCY_WINDOW_S:
                    sensor_parts.append((f"{name} (off {_fmt_duration(age_s)} ago)", age_s))

        if not person_parts and not sensor_parts:
            return ""

        sensor_parts.sort(key=lambda x: x[1])
        top_sensors = [t for t, _ in sensor_parts[:_MAX_SENSORS]]

        segments: list[str] = []
        if person_parts:
            segments.append(". ".join(person_parts))
        if top_sensors:
            segments.append("Active: " + ", ".join(top_sensors))

        return ". ".join(segments) + "."

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_context(self) -> str:
        """
        Return a cached presence context note.

        Layer 1 (entity discovery) refreshes every 10 min so new sensors
        are picked up automatically.  Layer 2 (state read) refreshes every
        30 sec.  Returns an empty string if HA is unreachable.
        """
        async with self._lock:
            now = time.monotonic()

            # Layer 1 — rediscover entities periodically
            if now >= self._discovery_expires:
                states = await self._fetch_all_states()
                if states is not None:
                    self._entity_ids = self._discover(states)
                    self._discovery_expires = now + _DISCOVERY_TTL_S
                    # pre-build context from the same fetch to save a round-trip
                    self._context = self._build(states, self._entity_ids)
                    self._context_expires = now + _CONTEXT_TTL_S
                    _LOGGER.info(
                        "presence_context.refreshed",
                        entities=len(self._entity_ids),
                        context_len=len(self._context),
                    )
                # if fetch failed, keep stale data and retry next call
                return self._context

            # Layer 2 — refresh state snapshot
            if now >= self._context_expires:
                states = await self._fetch_all_states()
                if states is not None:
                    self._context = self._build(states, self._entity_ids)
                    self._context_expires = now + _CONTEXT_TTL_S
                # if fetch failed, return stale context

            return self._context
