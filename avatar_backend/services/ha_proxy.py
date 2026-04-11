from __future__ import annotations
import re
from datetime import datetime
from typing import Any
import httpx
import structlog

from avatar_backend.models.acl import ACLManager
from avatar_backend.models.messages import ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.services.home_runtime import load_home_runtime_config

logger = structlog.get_logger()

_CALL_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# ── service_data validation ───────────────────────────────────────────────────

_KEY_RE          = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_ALLOWED_TYPES   = (str, int, float, bool)
_MAX_SD_KEYS     = 10
_MAX_STR_LEN     = 512
# entity_id is always set explicitly by call_service — block LLM from overriding it
_FORBIDDEN_KEYS  = frozenset({"entity_id"})

# H2 security fix: hardcoded denylist — blocked regardless of ACL config.
# Prevents LLM prompt injection from shutting down HA or running shell commands.
_DENIED_DOMAINS = frozenset({"shell_command", "script"})
_DENIED_SERVICES = frozenset({
    ("homeassistant", "stop"),
    ("homeassistant", "restart"),
})
_DOMAIN_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LEGACY_CAMERA_ALIASES: dict[str, str] = {
    "camera.doorbell": "camera.reolink_video_doorbell_poe_fluent",
    "camera.front_door": "camera.reolink_video_doorbell_poe_fluent",
    "camera.front_door_camera": "camera.reolink_video_doorbell_poe_fluent",
    "camera.reolink_doorbell": "camera.reolink_video_doorbell_poe_fluent",
    "camera.doorbell_camera": "camera.reolink_video_doorbell_poe_fluent",
    "camera.outdoor": "camera.rlc_410w_fluent",
    "camera.outdoor_1": "camera.rlc_410w_fluent",
    "camera.outdoor1": "camera.rlc_410w_fluent",
    "camera.outdoor_camera": "camera.rlc_410w_fluent",
    "camera.outside": "camera.rlc_410w_fluent",
    "camera.outdoor_2": "camera.rlc_1224a_fluent",
    "camera.outdoor2": "camera.rlc_1224a_fluent",
    "camera.floodlight": "camera.rlc_1224a_fluent",
    "camera.floodlight_camera": "camera.rlc_1224a_fluent",
    "camera.living_room": "camera.reolink_living_room_profile000_mainstream",
    "camera.living_room_camera": "camera.reolink_living_room_profile000_mainstream",
}


def _validate_service_data(data: dict) -> dict[str, Any]:
    """
    Sanitise LLM-supplied service_data before merging into the HA payload.
    Raises ValueError if the dict contains anything unsafe.
    """
    if not isinstance(data, dict):
        raise ValueError("service_data must be a mapping")
    if len(data) > _MAX_SD_KEYS:
        raise ValueError(f"service_data exceeds maximum key count ({_MAX_SD_KEYS})")
    clean: dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not _KEY_RE.match(k):
            raise ValueError(f"Invalid service_data key: {k!r}")
        if k in _FORBIDDEN_KEYS:
            continue  # silently drop — we set entity_id ourselves
        if not isinstance(v, _ALLOWED_TYPES):
            raise ValueError(
                f"service_data[{k!r}] has unsupported type {type(v).__name__!r}"
            )
        if isinstance(v, str) and len(v) > _MAX_STR_LEN:
            raise ValueError(f"service_data[{k!r}] string value too long")
        clean[k] = v
    return clean


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
        runtime = load_home_runtime_config()
        self._camera_aliases = dict(_LEGACY_CAMERA_ALIASES)
        self._camera_aliases.update(runtime.camera_aliases)

    # ── Public interface ──────────────────────────────────────────────────

    def resolve_camera_entity(self, entity_id: str) -> str:
        """Return the runtime/legacy-resolved Home Assistant camera entity ID."""
        return self._camera_aliases.get(entity_id, entity_id)

    @property
    def ha_url(self) -> str:
        return self._ha_url

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": self._headers["Authorization"]}

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

        # H3 security fix: reject malformed domain/service strings from LLM
        if not _DOMAIN_SERVICE_RE.match(domain):
            logger.warning("ha_proxy.bad_domain_format", domain=domain)
            return ToolResult(
                success=False,
                message=f"Invalid domain format: '{domain}'. Use get_entities to find valid domains.",
            )
        if not _DOMAIN_SERVICE_RE.match(service):
            logger.warning("ha_proxy.bad_service_format", service=service)
            return ToolResult(
                success=False,
                message=f"Invalid service format: '{service}'.",
            )

        # H2 security fix: hardcoded denylist — blocked even with wildcard ACL
        if domain in _DENIED_DOMAINS:
            logger.warning("ha_proxy.denied_domain", domain=domain, service=service, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=f"Domain '{domain}' is permanently blocked for safety. This action is not allowed.",
            )
        if (domain, service) in _DENIED_SERVICES:
            logger.warning("ha_proxy.denied_service", domain=domain, service=service, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=f"Service '{domain}.{service}' is permanently blocked for safety. This action is not allowed.",
            )

        # Never allow TTS service calls — Nova's text responses are automatically
        # spoken by the announce pipeline. Calling tts.speak directly is always wrong.
        if domain == "tts":
            logger.warning("ha_proxy.tts_blocked", service=service, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=(
                    "Do not call tts services directly. "
                    "Your text response will be automatically spoken by the system. "
                    "Simply respond with the information as plain text."
                ),
            )

        return await self.call_service(domain, service, entity_id, service_data)

    # Domains that have too many entities to dump wholesale — the LLM must
    # use get_entity_state for specific value reads, not browse the whole domain.
    _LARGE_DOMAINS: frozenset = frozenset({"sensor", "binary_sensor", "automation", "input_boolean"})
    _LARGE_DOMAIN_CAP: int = 30

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
                f"use get_entity_state with a specific entity_id for value lookups, "
                f"not get_entities)"
            )
        return ToolResult(
            success=True,
            message=f"{header}:\n" + "\n".join(lines),
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
        unit = s["attributes"].get("unit_of_measurement", "")
        state_str = f"{s['state']} {unit}".strip()
        friendly = s["attributes"].get("friendly_name", entity_id)

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

    async def fetch_camera_image(self, entity_id: str) -> bytes | None:
        """Fetch a camera snapshot from HA. Returns raw image bytes or None on failure."""
        entity_id = self.resolve_camera_entity(entity_id)
        # ACL gate — treat camera reads as domain=camera, service=get_image
        if self._acl is not None and not self._acl.is_allowed("camera", "get_image", entity_id):
            reason = self._acl.deny_reason("camera", "get_image", entity_id)
            logger.warning("ha_proxy.camera_acl_denied", entity_id=entity_id, reason=reason)
            return None
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
