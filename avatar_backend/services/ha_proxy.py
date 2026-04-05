from __future__ import annotations
from datetime import datetime
from typing import Any
import httpx
import structlog

from avatar_backend.models.acl import ACLManager
from avatar_backend.models.messages import ToolCall
from avatar_backend.models.tool_result import ToolResult

logger = structlog.get_logger()

_CALL_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)


class HAProxy:
    """
    The ONLY component that calls the Home Assistant REST API.

    Every service call passes through two gates:
      1. ACL check  — rejects anything not in config/acl.yaml
      2. HA API     — executes the approved call and returns a result

    The result (success or failure) is returned as a ToolResult whose
    .message field is inserted into the conversation history as a "tool"
    role message, so the LLM can respond naturally.
    """

    def __init__(self, ha_url: str, ha_token: str, acl: ACLManager | None) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        }
        self._acl = acl

    # ── Public interface ──────────────────────────────────────────────────

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        """Entry point called by the chat router for each tool call."""
        if tool_call.function_name == "get_entities":
            domain = tool_call.arguments.get("domain", "").strip()
            if not domain:
                return ToolResult(
                    success=False,
                    message="get_entities requires a 'domain' argument.",
                )
            return await self.get_entities(domain)

        if tool_call.function_name == "get_entity_state":
            entity_id = tool_call.arguments.get("entity_id", "").strip()
            if not entity_id:
                return ToolResult(success=False, message="get_entity_state requires an 'entity_id' argument.")
            # Intercept time/date lookups — return local server time directly
            # rather than hitting HA (sensor.system_time / sensor.time / etc. don't exist)
            _TIME_LIKE = {"sensor.system_time", "sensor.time", "sensor.date_time",
                          "sensor.current_time", "sensor.local_time"}
            if entity_id in _TIME_LIKE or entity_id.endswith("_time") and entity_id.startswith("sensor."):
                now = datetime.now().strftime("%A, %d %B %Y %H:%M")
                return ToolResult(success=True, message=f"Current time: {now}")
            return await self._get_single_entity_state(entity_id)

        if tool_call.function_name != "call_ha_service":
            logger.warning("ha_proxy.unknown_tool", name=tool_call.function_name)
            return ToolResult(
                success=False,
                message=f"Unknown tool '{tool_call.function_name}' — only call_ha_service, get_entities, and get_entity_state are supported.",
            )

        args = tool_call.arguments
        domain    = args.get("domain", "").strip()
        service   = args.get("service", "").strip()
        entity_id = args.get("entity_id", "").strip()
        service_data: dict[str, Any] = args.get("service_data") or {}

        # Validate required fields before hitting ACL or HA
        missing = [f for f in ("domain", "service", "entity_id") if not args.get(f)]
        if missing:
            return ToolResult(
                success=False,
                message=f"Tool call missing required fields: {', '.join(missing)}.",
            )

        return await self.call_service(domain, service, entity_id, service_data)

    async def get_entities(self, domain: str) -> ToolResult:
        """
        Return all HA entities for *domain* with their current state.
        Used by the LLM to discover real entity IDs before calling a service.
        """
        logger.info("ha_proxy.get_entities", domain=domain)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._ha_url}/api/states",
                    headers={"Authorization": self._headers["Authorization"]},
                )
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not reach Home Assistant: {exc}")

        if resp.status_code != 200:
            return ToolResult(success=False, message="Failed to fetch entity states from Home Assistant.")

        entities = [
            s for s in resp.json()
            if s["entity_id"].startswith(f"{domain}.")
            and s["state"] != "unavailable"
        ]

        if not entities:
            return ToolResult(
                success=True,
                message=f"No available entities found for domain '{domain}'.",
            )

        lines = []
        for s in entities:
            unit = s["attributes"].get("unit_of_measurement", "")
            state_str = f"{s['state']} {unit}".strip()
            lines.append(
                f"{s['entity_id']} | {s['attributes'].get('friendly_name', '')} | {state_str}"
            )
        return ToolResult(
            success=True,
            message=f"Available {domain} entities:\n" + "\n".join(lines),
        )

    async def _get_single_entity_state(self, entity_id: str) -> ToolResult:
        """Return the current state and key attributes of a single entity."""
        logger.info("ha_proxy.get_entity_state", entity_id=entity_id)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._ha_url}/api/states/{entity_id}",
                    headers={"Authorization": self._headers["Authorization"]},
                )
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not reach Home Assistant: {exc}")

        if resp.status_code == 404:
            return ToolResult(success=False, message=f"Entity '{entity_id}' not found.")
        if resp.status_code != 200:
            return ToolResult(success=False, message=f"Failed to fetch state for '{entity_id}'.")

        s = resp.json()
        unit = s["attributes"].get("unit_of_measurement", "")
        state_str = f"{s['state']} {unit}".strip()
        friendly = s["attributes"].get("friendly_name", entity_id)

        # Include useful extra attributes
        extras = []
        for key in ("device_class", "last_changed", "battery_level", "current_power_w", "today_energy_kwh"):
            if key in s["attributes"]:
                extras.append(f"{key}: {s['attributes'][key]}")

        msg = f"{friendly} ({entity_id}): {state_str}"
        if extras:
            msg += " | " + ", ".join(extras)
        return ToolResult(success=True, message=msg)

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
            payload.update(service_data)

        url = f"{self._ha_url}/api/services/{domain}/{service}"
        logger.info(
            "ha_proxy.calling",
            url=url, entity_id=entity_id,
            service_data=service_data or {},
        )

        try:
            async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
                resp = await client.post(url, headers=self._headers, json=payload)
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

    # Map common LLM-guessed camera aliases to real HA entity IDs
    _CAMERA_ALIASES: dict[str, str] = {
        "camera.doorbell":            "camera.reolink_video_doorbell_poe_fluent",
        "camera.front_door":          "camera.reolink_video_doorbell_poe_fluent",
        "camera.front_door_camera":   "camera.reolink_video_doorbell_poe_fluent",
        "camera.reolink_doorbell":    "camera.reolink_video_doorbell_poe_fluent",
        "camera.doorbell_camera":     "camera.reolink_video_doorbell_poe_fluent",
        "camera.outdoor":             "camera.rlc_410w_fluent",
        "camera.outdoor_1":           "camera.rlc_410w_fluent",
        "camera.outdoor1":            "camera.rlc_410w_fluent",
        "camera.outdoor_camera":      "camera.rlc_410w_fluent",
        "camera.outside":             "camera.rlc_410w_fluent",
        "camera.outdoor_2":           "camera.rlc_1224a_fluent",
        "camera.outdoor2":            "camera.rlc_1224a_fluent",
        "camera.floodlight":          "camera.rlc_1224a_fluent",
        "camera.floodlight_camera":   "camera.rlc_1224a_fluent",
        "camera.living_room":         "camera.reolink_living_room_profile000_mainstream",
        "camera.living_room_camera":  "camera.reolink_living_room_profile000_mainstream",
    }

    async def fetch_camera_image(self, entity_id: str) -> bytes | None:
        """Fetch a camera snapshot from HA. Returns raw image bytes or None on failure."""
        entity_id = self._CAMERA_ALIASES.get(entity_id, entity_id)
        url = f"{self._ha_url}/api/camera_proxy/{entity_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._headers)
                if resp.status_code == 200:
                    return resp.content
                logger.warning("ha_proxy.camera_fetch_failed", entity_id=entity_id, status=resp.status_code)
                return None
        except Exception as exc:
            logger.error("ha_proxy.camera_error", entity_id=entity_id, exc=str(exc))
            return None

        # ── Diagnostics ───────────────────────────────────────────────────────

    async def is_connected(self) -> bool:
        """True if HA is reachable AND the token is valid."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self._ha_url}/api/",
                    headers={"Authorization": self._headers["Authorization"]},
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def get_entity_state(self, entity_id: str) -> dict | None:
        """Fetch current state of an entity — used in tests and diagnostics."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._ha_url}/api/states/{entity_id}",
                    headers={"Authorization": self._headers["Authorization"]},
                )
                return resp.json() if resp.status_code == 200 else None
        except Exception:
            return None
