"""Mixin for HAProxy: entity reads, call_service, camera fetch, diagnostics, state cache."""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any

import httpx
import structlog

from avatar_backend.models.messages import ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.services._shared_http import _http_client
from avatar_backend.services.home_runtime import load_home_runtime_config

logger = structlog.get_logger()

_CALL_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


_DENIED_DOMAINS: frozenset[str] = frozenset({"shell_command", "script"})
_DENIED_SERVICES: frozenset[tuple[str, str]] = frozenset({
    ("homeassistant", "restart"),
    ("homeassistant", "stop"),
    ("system_log", "write"),
})


def _validate_service_data(data: dict) -> dict:
    """Strip keys that are code-injection vectors."""
    blocked = {"template", "event_data_template", "message_template"}
    return {k: v for k, v in data.items() if k not in blocked}


class HAStateMixin:
    """HA entity/state operations, call_service, camera fetch, diagnostics — mixed into HAProxy."""
    _LARGE_DOMAINS: frozenset = frozenset({"sensor", "binary_sensor", "automation", "input_boolean"})
    _LARGE_DOMAIN_CAP: int = 15

    async def get_entities(self, domain: str) -> ToolResult:
        """
        Return all HA entities for *domain* with their current state.
        Used by the LLM to discover real entity IDs before calling a service.
        For large domains (sensor, binary_sensor) results are capped to avoid
        the LLM summarising hundreds of unrelated readings.
        """
        logger.info("ha_proxy.get_entities", domain=domain)

        if domain in self._LARGE_DOMAINS:
            logger.warning("ha_proxy.get_entities_large_domain", domain=domain)

        # Try WebSocket state mirror first (zero API calls)
        all_states = None
        ws_mgr = getattr(self, "_ws_manager", None)
        if ws_mgr and ws_mgr.is_connected:
            all_states = ws_mgr.get_all_states()

        if not all_states:
            try:
                client = await self._get_client()
                resp = await client.get(f"{self._ha_url}/api/states")
            except Exception as exc:
                return ToolResult(success=False, message=f"Could not reach Home Assistant: {exc}")
            if resp.status_code != 200:
                return ToolResult(success=False, message="Failed to fetch entity states from Home Assistant.")
            all_states = resp.json()

        entities = [
            s for s in all_states
            if s.get("entity_id", "").startswith(f"{domain}.")
            and s.get("state") != "unavailable"
        ]

        if not entities:
            return ToolResult(
                success=True,
                message=f"No available entities found for domain '{domain}'.",
            )

        total = len(entities)
        truncated = False
        if domain in self._LARGE_DOMAINS and total > self._LARGE_DOMAIN_CAP:
            entities = entities[:self._LARGE_DOMAIN_CAP]
            truncated = True

        lines = []
        for s in entities:
            unit = s["attributes"].get("unit_of_measurement", "")
            state_str = f"{s['state']} {unit}".strip()
            lines.append(
                f"{s['entity_id']} | {s['attributes'].get('friendly_name', '')} | {state_str}"
            )

        header = f"Available {domain} entities"
        if truncated:
            header += (
                f" (showing {self._LARGE_DOMAIN_CAP} of {total} — "
                f"DO NOT summarize this list. Use get_entity_state with the EXACT entity_id "
                f"from the system prompt to answer the user's question. "
                f"For weather/outdoor temperature use {self._weather_entity})"
            )
        return ToolResult(
            success=True,
            message=f"{header}:\n" + "\n".join(lines),
        )

    async def _get_single_entity_state(self, entity_id: str) -> ToolResult:
        """Return the current state and key attributes of a single entity."""
        logger.info("ha_proxy.get_entity_state", entity_id=entity_id)

        # Try WebSocket state mirror first (zero API calls)
        ws_mgr = getattr(self, "_ws_manager", None)
        s = ws_mgr.get_state(entity_id) if ws_mgr and ws_mgr.is_connected else None
        if s is not None:
            return await self._format_entity_state(entity_id, s)

        # Fallback to REST
        try:
            client = await self._get_client()
            resp = await client.get(f"{self._ha_url}/api/states/{entity_id}")
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not reach Home Assistant: {exc}")

        if resp.status_code == 404:
            # Auto-discover: fetch all entities in the same domain to guide the LLM
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            hint = ""
            if domain:
                try:
                    disc = await self.get_entities(domain)
                    if disc.success:
                        hint = f"\nAvailable {domain} entities:\n{disc.message}"
                except Exception:
                    pass
            logger.warning("ha_proxy.entity_not_found", entity_id=entity_id)
            return ToolResult(
                success=False,
                message=(
                    f"Entity '{entity_id}' not found. "
                    f"Call get_entities('{domain}') to find the correct entity_id "
                    f"-- do NOT guess another ID.{hint}"
                ),
            )
        if resp.status_code != 200:
            return ToolResult(success=False, message=f"Failed to fetch state for '{entity_id}'.")

        s = resp.json()
        return await self._format_entity_state(entity_id, s)

    async def _format_entity_state(self, entity_id: str, s: dict) -> ToolResult:
        """Format an entity state dict into a ToolResult for the LLM."""
        unit = s.get("attributes", {}).get("unit_of_measurement", "")
        state_str = f"{s['state']} {unit}".strip()
        friendly = s.get("attributes", {}).get("friendly_name", entity_id)

        # Return ALL attributes so the LLM has full context (e.g. car lock
        # doorStatusOverall, sensor sub-readings). Skip internal HA display keys.
        _SKIP_ATTRS = {
            "friendly_name", "icon", "unit_of_measurement", "attribution",
            "restored", "supported_features", "supported_color_modes",
            "assumed_state", "editable",
        }
        extras = []
        for key, val in s["attributes"].items():
            if key in _SKIP_ATTRS:
                continue
            extras.append(f"{key}: {val}")

        msg = f"{friendly} ({entity_id}): {state_str}"
        if extras:
            msg += "\nAttributes:\n  " + "\n  ".join(extras)

        # Enrich weather entities with forecast data (HA doesn't include it in state)
        if entity_id.startswith("weather."):
            forecast_text = await self._fetch_weather_forecast(entity_id)
            if forecast_text:
                msg += f"\n{forecast_text}"

        return ToolResult(success=True, message=msg)


    async def _play_music(self, args: dict) -> ToolResult:
        """Search Music Assistant and play the first result on a speaker."""
        query = (args.get("query") or "").strip()
        entity_id = (args.get("entity_id") or "").strip()
        if not query:
            return ToolResult(success=False, message="play_music requires a 'query' argument.")
        if not entity_id:
            return ToolResult(success=False, message="play_music requires an 'entity_id' argument.")

        music_svc = getattr(self, "_music_service", None)
        if music_svc is None:
            return ToolResult(success=False, message="Music service is not configured.")

        logger.info("ha_proxy.play_music", query=query, entity_id=entity_id)
        results = await music_svc.search(query, media_type="track", limit=5)
        if not results:
            results = await music_svc.search(query, media_type="artist", limit=5)
        if not results:
            return ToolResult(success=False, message=f"No results found for '{query}'. Try a different search term.")

        track = results[0]
        uri = track.get("uri") or track.get("url") or ""
        name = track.get("name") or track.get("title") or query
        artist = track.get("artist") or track.get("artists") or ""
        if isinstance(artist, list):
            artist = artist[0].get("name", "") if artist else ""

        if not uri:
            return ToolResult(success=False, message=f"Found '{name}' but no playable URI available.")

        # Use music_assistant.play_media for MA URIs — it handles routing to the correct player
        logger.info("ha_proxy.play_music_uri", entity_id=entity_id, uri=uri[:80], name=name)
        r = await self.call_service(
            "music_assistant", "play_media", entity_id,
            service_data={"media_id": uri, "media_type": "track"},
        )
        if r.success:
            desc = f"Now playing: {name}"
            if artist:
                desc += f" by {artist}"
            desc += f" on {entity_id.split('.', 1)[-1].replace('_', ' ').title()}"
            return ToolResult(success=True, message=desc)
        return ToolResult(success=False, message=f"Failed to play '{name}': {r.message}")

    _forecast_cache: dict[str, tuple[float, str]] = {}  # entity_id → (expires_at, text)
    _FORECAST_TTL = 1800  # 30 minutes

    async def _fetch_weather_forecast(self, entity_id: str) -> str:
        """Fetch daily forecast from HA weather.get_forecasts service. Cached 30 min."""
        import time as _time
        now = _time.monotonic()
        cached = self._forecast_cache.get(entity_id)
        if cached and now < cached[0]:
            return cached[1]
        try:
            url = f"{self._ha_url}/api/services/weather/get_forecasts?return_response"
            client = await self._get_client()
            resp = await client.post(url, headers={"Content-Type": "application/json"},
                                     json={"entity_id": entity_id, "type": "daily"})
            if resp.status_code != 200:
                return ""
            data = resp.json()
            forecasts = data.get("service_response", {}).get(entity_id, {}).get("forecast", [])
            if not forecasts:
                return ""
            lines = ["Daily forecast:"]
            for fc in forecasts[:3]:  # next 3 days
                date = fc.get("datetime", "")[:10]
                cond = fc.get("condition", "")
                temp_high = fc.get("temperature", "")
                temp_low = fc.get("templow", "")
                precip = fc.get("precipitation_probability", "")
                wind = fc.get("wind_speed", "")
                line = f"  {date}: {cond}, high {temp_high}°C"
                if temp_low:
                    line += f", low {temp_low}°C"
                if precip:
                    line += f", {precip}% rain chance"
                if wind:
                    line += f", wind {wind} km/h"
                lines.append(line)
            result = "\n".join(lines)
            self._forecast_cache[entity_id] = (now + self._FORECAST_TTL, result)
            return result
        except Exception:
            return ""

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
    ) -> ToolResult:
        """
        ACL-gate then call POST /api/services/{domain}/{service} on HA.
        Returns a ToolResult regardless of outcome.
        """
        svc_label = f"{domain}.{service}"

        # H2 security fix: hardcoded denylist — checked before ACL
        if domain in _DENIED_DOMAINS or (domain, service) in _DENIED_SERVICES:
            logger.warning("ha_proxy.denied_service_direct", domain=domain, service=service, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=f"Service '{svc_label}' is permanently blocked for safety.",
                entity_id=entity_id,
                service_called=svc_label,
            )

        # ── Gate 1: ACL ───────────────────────────────────────────────────
        if self._acl is not None:
            if not self._acl.is_allowed(domain, service, entity_id):
                reason = self._acl.deny_reason(domain, service, entity_id)
                logger.warning(
                    "ha_proxy.acl_denied",
                    domain=domain, service=service, entity_id=entity_id,
                    reason=reason,
                )
                return ToolResult(
                    success=False,
                    message=f"Permission denied: {reason}. I cannot perform this action.",
                    entity_id=entity_id,
                    service_called=svc_label,
                )
        else:
            logger.warning("ha_proxy.no_acl", detail="ACL not loaded — all calls permitted")

        # ── Gate 2: HA API ────────────────────────────────────────────────
        payload: dict[str, Any] = {"entity_id": entity_id}
        if service_data:
            try:
                payload.update(_validate_service_data(service_data))
            except ValueError as exc:
                logger.warning("ha_proxy.bad_service_data", error=str(exc),
                               entity_id=entity_id, service=svc_label)
                return ToolResult(
                    success=False,
                    message=f"Invalid service_data: {exc}",
                    entity_id=entity_id,
                    service_called=svc_label,
                )

        url = f"{self._ha_url}/api/services/{domain}/{service}"
        logger.info(
            "ha_proxy.calling",
            url=url, entity_id=entity_id,
            service_data=service_data or {},
        )

        try:
            client = await self._get_client()
            resp = await client.post(url, headers={"Content-Type": "application/json"}, json=payload)
        except httpx.ConnectError as exc:
            logger.error("ha_proxy.connect_error", error=str(exc))
            return ToolResult(
                success=False,
                message="Could not reach Home Assistant. Please check the connection.",
                entity_id=entity_id,
                service_called=svc_label,
            )
        except httpx.TimeoutException:
            logger.error("ha_proxy.timeout", entity_id=entity_id)
            return ToolResult(
                success=False,
                message="Home Assistant did not respond in time.",
                entity_id=entity_id,
                service_called=svc_label,
            )

        # ── Interpret response ────────────────────────────────────────────
        if resp.status_code == 200:
            logger.info(
                "ha_proxy.success",
                entity_id=entity_id, service=svc_label,
                states_changed=len(resp.json()) if resp.content else 0,
            )
            return ToolResult(
                success=True,
                message=f"Done — {svc_label} called on {entity_id} successfully.",
                entity_id=entity_id,
                service_called=svc_label,
                ha_status_code=200,
            )

        if resp.status_code == 401:
            logger.error("ha_proxy.bad_token")
            return ToolResult(
                success=False,
                message="Home Assistant rejected the request — authentication error.",
                entity_id=entity_id,
                service_called=svc_label,
                ha_status_code=401,
            )

        if resp.status_code == 404:
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = ""
            logger.warning(
                "ha_proxy.not_found",
                entity_id=entity_id, service=svc_label, detail=detail,
            )
            return ToolResult(
                success=False,
                message=(
                    f"Entity '{entity_id}' was not found in Home Assistant. "
                    "Use get_entities to find the correct entity_id."
                ),
                entity_id=entity_id,
                service_called=svc_label,
                ha_status_code=404,
            )

        logger.error(
            "ha_proxy.unexpected_status",
            status=resp.status_code, body=resp.text[:200],
        )
        return ToolResult(
            success=False,
            message=f"Home Assistant returned an unexpected error (HTTP {resp.status_code}).",
            entity_id=entity_id,
            service_called=svc_label,
            ha_status_code=resp.status_code,
        )

    async def fetch_camera_image(self, entity_id: str) -> bytes | None:
        """Fetch a camera snapshot from HA. Returns raw image bytes or None on failure.
        Retries once after a short delay for transient failures (502/503/timeout)."""
        entity_id = self.resolve_camera_entity(entity_id)
        # ACL gate — treat camera reads as domain=camera, service=get_image
        if self._acl is not None and not self._acl.is_allowed("camera", "get_image", entity_id):
            reason = self._acl.deny_reason("camera", "get_image", entity_id)
            logger.warning("ha_proxy.camera_acl_denied", entity_id=entity_id, reason=reason)
            return None
        from avatar_backend.config import get_settings
        _base = get_settings().ha_local_url_resolved
        url = f"{_base}/api/camera_proxy/{entity_id}"
        for attempt in range(2):
            try:
                client = await self._get_client()
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content
                if attempt == 0 and resp.status_code in (502, 503, 504):
                    logger.debug("ha_proxy.camera_fetch_retry", entity_id=entity_id, status=resp.status_code)
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("ha_proxy.camera_fetch_failed", entity_id=entity_id, status=resp.status_code)
                return None
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt == 0:
                    logger.debug("ha_proxy.camera_fetch_retry", entity_id=entity_id, exc_type=type(exc).__name__)
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("ha_proxy.camera_fetch_failed", entity_id=entity_id, exc_type=type(exc).__name__)
                return None
            except Exception as exc:
                logger.error("ha_proxy.camera_error", entity_id=entity_id, exc=repr(exc), exc_type=type(exc).__name__)
                return None
        # HA failed — try Blue Iris direct fallback
        bi = getattr(self, "_blueiris_service", None)
        if bi and bi.available:
            logger.info("ha_proxy.camera_blueiris_fallback", entity_id=entity_id)
            return await bi.fetch_snapshot(entity_id)
        return None

        # ── Diagnostics ───────────────────────────────────────────────────────

    async def is_connected(self) -> bool:
        """True if HA is reachable AND the token is valid."""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self._ha_url}/api/")
            return resp.status_code == 200
        except Exception:
            return False

    async def get_entity_state(self, entity_id: str) -> dict | None:
        """Fetch current state of an entity — used in tests and diagnostics."""
        ws_mgr = getattr(self, "_ws_manager", None)
        if ws_mgr and ws_mgr.is_connected:
            s = ws_mgr.get_state(entity_id)
            if s is not None:
                return s
        try:
            client = await self._get_client()
            resp = await client.get(f"{self._ha_url}/api/states/{entity_id}")
            return resp.json() if resp.status_code == 200 else None
        except Exception:
            return None

    async def get_states_by_domain(self, domain: str) -> list[dict]:
        """Fetch all entity states for a given domain (e.g. 'media_player'). Uses cache."""
        states = await self._get_all_states_cached()
        return [s for s in states if s.get("entity_id", "").startswith(domain + ".")]

    async def _get_all_states_cached(self) -> list[dict]:
        """Return all entity states — prefers WebSocket mirror, falls back to REST with cache."""
        # Try WebSocket state mirror first (zero API calls)
        ws_mgr = getattr(self, "_ws_manager", None)
        if ws_mgr and ws_mgr.is_connected:
            states = ws_mgr.get_all_states()
            if states:
                return states
        # Fallback to REST with 10-second cache
        import time as _time
        now = _time.monotonic()
        if hasattr(self, "_states_cache") and now - self._states_cache_ts < 10:
            return self._states_cache
        try:
            client = await self._get_client()
            resp = await client.get(f"{self._ha_url}/api/states")
            if resp.status_code == 200:
                    self._states_cache = resp.json()
                    self._states_cache_ts = now
                    return self._states_cache
        except Exception:
            pass
        return getattr(self, "_states_cache", [])

    def set_ws_manager(self, ws_manager) -> None:
        """Link the shared HA WebSocket manager for state mirror access."""
        self._ws_manager = ws_manager
