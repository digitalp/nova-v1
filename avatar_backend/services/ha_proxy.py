from __future__ import annotations
import asyncio
import re
from datetime import datetime
from typing import Any
import httpx
import structlog

from avatar_backend.models.acl import ACLManager
from avatar_backend.models.messages import ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.services.home_runtime import load_home_runtime_config
from avatar_backend.services import mdm_client as _mdm
from avatar_backend.services.ha_parental_tools import ParentalToolsMixin

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
    # Populated from config/home_runtime.json at startup.
    # Run install.sh or edit home_runtime.json to configure camera mappings.
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

from avatar_backend.services.ha_state_mixin import HAStateMixin

class HAProxy(HAStateMixin, ParentalToolsMixin):
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
        self._client: httpx.AsyncClient | None = None
        runtime = load_home_runtime_config()
        self._camera_aliases = dict(_LEGACY_CAMERA_ALIASES)
        self._camera_aliases.update(runtime.camera_aliases)
        # Weather entity — configurable via home_runtime.json, falls back to legacy
        self._weather_entity = runtime.weather_entity or ""
        self._sensor_shortcuts = runtime.sensor_shortcuts

    async def _get_client(self) -> httpx.AsyncClient:
        """Return a shared httpx client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=_CALL_TIMEOUT,
                headers={"Authorization": self._headers["Authorization"]},
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                verify=False,
            )
        return self._client

    async def close(self) -> None:
        """Close the shared httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

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
            # Intercept weather domain — always redirect to the preferred entity
            if domain == "weather":
                return ToolResult(
                    success=True,
                    message=(
                        f"Use get_entity_state('{self._weather_entity}') for all weather questions. "
                        "It has current conditions, temperature, humidity, wind, and forecast data. "
                        "Do NOT list multiple weather sources — just call get_entity_state with this entity."
                    ),
                )
            # Intercept sensor/binary_sensor for common questions
            if domain in ("sensor", "binary_sensor"):
                lines = [f"  Outdoor temp: {self._weather_entity} (attribute: temperature)"]
                for label, eid in self._sensor_shortcuts.items():
                    lines.append(f"  {label}: {eid}")
                if lines:
                    shortcuts = "\n".join(lines)
                    return ToolResult(
                        success=True,
                        message=(
                            "Do NOT browse sensors. Use get_entity_state with the EXACT entity_id:\n"
                            f"{shortcuts}\n"
                            "Call get_entity_state with the specific entity_id that answers the user's question."
                        ),
                    )
                return await self.get_entities(domain)
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

        if tool_call.function_name == "play_music":
            return await self._play_music(tool_call.arguments)

        if tool_call.function_name == "log_chore":
            return await self._log_chore(tool_call.arguments)

        if tool_call.function_name == "get_scoreboard":
            return await self._get_scoreboard(tool_call.arguments)

        if tool_call.function_name == "deduct_points":
            return await self._deduct_points(tool_call.arguments)

        if tool_call.function_name == "get_enrolled_devices":
            return await self._get_enrolled_devices()

        if tool_call.function_name == "get_parental_status":
            return await self._get_parental_status()

        if tool_call.function_name == "simulate_household_at":
            return await self._simulate_household_at(tool_call.arguments)

        if tool_call.function_name == "get_household_forecast":
            return await self._get_household_forecast(tool_call.arguments)

        if tool_call.function_name == "get_bedtime_status":
            return await self._get_bedtime_status(tool_call.arguments)

        if tool_call.function_name == "get_device_location":
            return await self._get_device_location(tool_call.arguments)

        if tool_call.function_name == "search_apps":
            return await self._search_apps(tool_call.arguments)

        if tool_call.function_name == "block_app":
            _result = await self._mdm_set_app(tool_call.arguments, action=0)
            self._log_parental_action("block_app", tool_call.arguments, _result)
            return _result

        if tool_call.function_name == "unblock_app":
            _result = await self._mdm_set_app(tool_call.arguments, action=1)
            self._log_parental_action("unblock_app", tool_call.arguments, _result)
            return _result

        if tool_call.function_name == "deploy_app":
            return await self._deploy_app(tool_call.arguments)

        if tool_call.function_name == "check_homework_gate":
            return self._check_homework_gate(tool_call.arguments)

        if tool_call.function_name == "request_exception":
            args = tool_call.arguments
            subject = str(args.get("subject") or "").strip()
            resource = str(args.get("resource") or "").strip()
            reason = str(args.get("reason") or "").strip()
            duration_m = int(args.get("duration_minutes") or 30)
            db = getattr(self._container, "metrics_db", None)
            if not db:
                return "Override queue unavailable — please ask a parent directly."
            row = db.add_override_request(
                subject=subject, resource=resource, reason=reason,
                duration_m=duration_m, requested_by="nova"
            )
            _LOGGER.info("parental.override_requested", subject=subject, resource=resource, id=row.get("id"))
            return (
                f"I've submitted a request for {subject} to have {duration_m} minutes of "
                f"{resource or 'the requested activity'}. Reason: {reason}. "
                "A parent can approve or deny this in the admin panel under Parental → Override Queue."
            )

        if tool_call.function_name == "send_device_message":
            _result = await self._send_device_message(tool_call.arguments)
            self._log_parental_action("send_device_message", tool_call.arguments, _result)
            return _result

        if tool_call.function_name == "get_parental_configurations":
            return await self._get_parental_configurations()

        if tool_call.function_name == "get_enrollment_link":
            return await self._get_enrollment_link(tool_call.arguments)

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
                message=f"Invalid Home Assistant service name: '{service}'. Service names use underscores, not dots.",
            )

        # Reject get_state — LLM should use the get_entity_state tool instead
        if service == "get_state":
            logger.warning("ha_proxy.get_state_rejected", domain=domain, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=f"Use get_entity_state('{entity_id}') instead of calling {domain}.get_state as a service.",
            )

        # Read-only domains — cannot be switched, toggled, or turned on/off
        _READ_ONLY_DOMAINS = frozenset({"sensor", "binary_sensor", "weather", "sun", "zone", "person", "device_tracker"})
        _WRITE_SERVICES = frozenset({"turn_on", "turn_off", "toggle"})
        if domain in _READ_ONLY_DOMAINS and service in _WRITE_SERVICES:
            logger.warning("ha_proxy.read_only_domain", domain=domain, service=service, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=f"Domain '{domain}' is not switchable — it is read-only. Use get_entity_state to read its value.",
            )

        # update_entity must go through homeassistant domain
        if service == "update_entity" and domain != "homeassistant":
            logger.warning("ha_proxy.update_entity_wrong_domain", domain=domain, entity_id=entity_id)
            return ToolResult(
                success=False,
                message=f"update_entity must be called via the homeassistant domain: call_ha_service(domain='homeassistant', service='update_entity', entity_id='{entity_id}').",
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
