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


    def _check_homework_gate(self, args: dict) -> str:
        from datetime import datetime as _dt
        person_id = str(args.get("person_id") or "").strip().lower()
        fs = getattr(self, "_family_service", None)
        sb = getattr(self, "_scoreboard_service", None)
        if not fs or not person_id:
            return "Homework gate is not configured for this household."
        now = _dt.now()
        midnight_ts = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        person = fs.get_person(person_id)
        if not person:
            return f"I don't have {person_id} in the household roster."
        state = fs.get_child_state(person_id)
        policies = fs.get_policies_for(person_id)
        hw_policies = [p for p in policies if p.rule_type == "requires_task_before_entertainment"]
        if not hw_policies:
            return f"{person.display_name} has no homework gate policy active."
        lines = [f"{person.display_name}'s homework gate status:"]
        lines.append(f"  Current state: {state.get('state', 'allowed')} — {state.get('reason', '')}")
        if sb:
            logs = sb.all_logs(days=1)
            done_today = {l["task_id"] for l in logs
                         if l["ts"] >= midnight_ts and l["person"] == person_id and l["points"] > 0}
            for pol in hw_policies:
                required = pol.required_task_ids or []
                if required:
                    done = [t for t in required if t in done_today]
                    pending = [t for t in required if t not in done_today]
                    lines.append(f"  Tasks done: {', '.join(done) or 'none'}")
                    lines.append(f"  Tasks pending: {', '.join(pending) or 'none'}")
                else:
                    lines.append(f"  Chores logged today: {len(done_today)}")
        return chr(10).join(lines)

    def _log_parental_action(self, tool: str, args: dict, result) -> None:
        """Fire-and-forget audit log for parental LLM tool calls."""
        try:
            db = getattr(self._container, "metrics_db", None)
            if db is None:
                return
            success = getattr(result, "success", True)
            msg = getattr(result, "message", str(result))
            db.log_parental_tool(tool=tool, args=args, success=bool(success), message=msg)
        except Exception:
            pass

    async def _get_enrolled_devices(self) -> "ToolResult":
        try:
            devices = await _mdm.get_devices()
            if not devices:
                return ToolResult(success=True, message="No devices enrolled.")
            lines = []
            for d in devices:
                number = d.get("number", "?")
                name = d.get("description") or d.get("name") or d.get("model") or "Unknown"
                is_online = d.get("online")
                last_upd_raw = d.get("lastUpdate")
                if not is_online and last_upd_raw:
                    # If flag is missing or false, check if update was in last 10 mins
                    if abs((time.time() * 1000) - last_upd_raw) < 600000:
                        is_online = True
                online = "online" if is_online else "offline"
                raw_upd = d.get("lastUpdate")
                if raw_upd and isinstance(raw_upd, int):
                    try: last_seen = datetime.fromtimestamp(raw_upd / 1000).strftime("%Y-%m-%d")
                    except Exception: last_seen = str(raw_upd)[:10]
                else:
                    last_seen = str(raw_upd or "never")[:10]
                lines.append(f"{number}: {name} — {online} (last seen {last_seen})")
            return ToolResult(success=True, message=chr(10).join(lines))
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _get_parental_status(self) -> "ToolResult":
        try:
            status = await _mdm.get_parental_status()
            reachable = "reachable" if status.get("hmdm_reachable") else "unreachable"
            url = status.get("url") or ""
            msg = f"Headwind parental backend is {reachable}."
            if url:
                msg += f" URL: {url}"
            return ToolResult(success=True, message=msg)
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _simulate_household_at(self, args: dict) -> "ToolResult":
        from datetime import datetime as _dt
        time_str = str(args.get("time") or "").strip()
        day_str = str(args.get("day") or "").strip().lower()
        if not time_str:
            return ToolResult(success=False, message="time is required (HH:MM).")
        try:
            sim_h, sim_m = (int(x) for x in time_str.split(":"))
        except Exception:
            return ToolResult(success=False, message=f"Invalid time {time_str!r}.")
        if not day_str:
            day_str = _dt.now().strftime("%A").lower()
        fs = getattr(self, "_family_service", None)
        if not fs:
            return ToolResult(success=False, message="Family service not configured.")
        hm = (sim_h, sim_m)
        lines = []
        for person in fs.get_children():
            school_nights = [s.lower() for s in (person.school_nights or [])]
            is_school = day_str in school_nights
            bedtime_str = person.bedtime_weekday if is_school else person.bedtime_weekend
            night_type = "school night" if is_school else "weekend"
            parts = []
            if bedtime_str:
                bt_h, bt_m = (int(x) for x in bedtime_str.split(":"))
                if hm >= (bt_h, bt_m):
                    parts.append(f"past bedtime ({bedtime_str}, {night_type}) — device LOCKED")
                else:
                    mins = (bt_h * 60 + bt_m) - (sim_h * 60 + sim_m)
                    h, m = divmod(mins, 60)
                    t = f"{h}h {m}m" if h else f"{m}m"
                    parts.append(f"bedtime {bedtime_str} in {t} ({night_type})")
            else:
                parts.append(f"no bedtime for {night_type}")
            for pol in fs.get_homework_gate_policies():
                if pol.subject_id != person.id or not pol.active:
                    continue
                if pol.enforce_from and pol.enforce_until:
                    fh, fm = (int(x) for x in pol.enforce_from.split(":"))
                    uh, um = (int(x) for x in pol.enforce_until.split(":"))
                    in_w = (fh, fm) <= hm < (uh, um)
                    tasks = ", ".join(pol.required_task_ids) or "assigned tasks"
                    if in_w:
                        parts.append(f"homework gate ACTIVE until {pol.enforce_until} (needs: {tasks})")
                    elif hm < (fh, fm):
                        mins2 = (fh * 60 + fm) - (sim_h * 60 + sim_m)
                        h2, m2 = divmod(mins2, 60)
                        t2 = f"{h2}h {m2}m" if h2 else f"{m2}m"
                        parts.append(f"homework gate opens in {t2} (at {pol.enforce_from})")
                    else:
                        parts.append("homework gate closed")
            lines.append(f"{person.display_name}: " + "; ".join(parts))
        if not lines:
            return ToolResult(success=True, message="No children in family model.")
        return ToolResult(success=True, message=
            f"Simulation for {day_str.title()} at {time_str}:\n" +
            "\n".join(f"  {chr(8226)} {l}" for l in lines))

    async def _get_household_forecast(self, args: dict) -> "ToolResult":
        from datetime import datetime as _dt, timedelta as _td
        fs = getattr(self, "_family_service", None)
        if not fs:
            return ToolResult(success=False, message="Family service not configured.")
        now = _dt.now()
        day = now.strftime("%A").lower()
        hm = (now.hour, now.minute)
        lines = []

        for person in fs.get_children():
            school_nights = [s.lower() for s in (person.school_nights or [])]
            is_school = day in school_nights
            bedtime_str = person.bedtime_weekday if is_school else person.bedtime_weekend
            night_type = "school night" if is_school else "weekend"
            state = fs.get_child_state(person.id)
            state_name = state.get("state", "allowed")
            state_reason = state.get("reason", "")

            entry = f"{person.display_name} [{state_name}]"
            if state_reason:
                entry += f" ({state_reason})"

            if bedtime_str:
                bt_h, bt_m = (int(x) for x in bedtime_str.split(":"))
                if hm < (bt_h, bt_m):
                    mins_left = (bt_h * 60 + bt_m) - (now.hour * 60 + now.minute)
                    h, m = divmod(mins_left, 60)
                    time_str = f"{h}h {m}m" if h else f"{m}m"
                    entry += f" — bedtime {bedtime_str} in {time_str} ({night_type})"
                else:
                    entry += f" — past bedtime ({bedtime_str}, {night_type})"

            for pol in fs.get_homework_gate_policies():
                if pol.subject_id != person.id or not pol.active:
                    continue
                if pol.enforce_from and pol.enforce_until:
                    from_h, from_m = (int(x) for x in pol.enforce_from.split(":"))
                    until_h, until_m = (int(x) for x in pol.enforce_until.split(":"))
                    if hm < (from_h, from_m):
                        mins = (from_h * 60 + from_m) - (now.hour * 60 + now.minute)
                        h, m = divmod(mins, 60)
                        t = f"{h}h {m}m" if h else f"{m}m"
                        entry += f" — homework gate opens in {t}"
                    elif hm < (until_h, until_m):
                        entry += f" — homework gate active until {pol.enforce_until}"

            lines.append(entry)

        if not lines:
            return ToolResult(success=True, message="No children found in family model.")
        now_str = now.strftime("%H:%M")
        return ToolResult(success=True, message=f"Household forecast at {now_str}:\n" + "\n".join(f"• {l}" for l in lines))

    async def _get_bedtime_status(self, args: dict) -> "ToolResult":
        from datetime import datetime as _dt
        person_id = str(args.get("person_id") or "").strip().lower()
        if not person_id:
            return ToolResult(success=False, message="person_id is required.")
        fs = getattr(self, "_family_service", None)
        if not fs:
            return ToolResult(success=False, message="Family service not configured.")
        person = fs.get_person(person_id)
        if not person:
            return ToolResult(success=False, message=f"Unknown person '{person_id}'.")
        if person.role != "child":
            return ToolResult(success=True, message=f"{person.display_name} is a guardian — no bedtime enforced.")
        now = _dt.now()
        day = now.strftime("%A").lower()
        school_nights = [s.lower() for s in (person.school_nights or [])]
        is_school = day in school_nights
        bedtime = person.bedtime_weekday if is_school else person.bedtime_weekend
        night_type = "school night" if is_school else "weekend"
        state = fs.get_child_state(person_id)
        state_str = state.get("state", "allowed")
        state_reason = state.get("reason", "")
        if not bedtime:
            return ToolResult(success=True, message=f"{person.display_name} has no bedtime set for tonight ({night_type}).")
        msg = (f"{person.display_name}'s bedtime tonight is {bedtime} ({night_type}). "
               f"Current device state: {state_str}")
        if state_reason:
            msg += f" — {state_reason}"
        return ToolResult(success=True, message=msg + ".")

    async def _get_device_location(self, args: dict) -> "ToolResult":
        import httpx as _httpx
        person_id = str(args.get("person_id") or "").strip().lower()
        device_number = str(args.get("device_number") or "").strip()
        display_name = person_id or device_number

        # Resolve person_id → device_number via family_service
        if person_id and not device_number:
            fs = getattr(self, "_family_service", None)
            if fs:
                for res in fs.get_resources_for(person_id):
                    if res.kind == "mdm_device" and res.device_number:
                        device_number = res.device_number
                        person = fs.get_person(person_id)
                        if person:
                            display_name = person.display_name
                        break
        if not device_number:
            return ToolResult(success=False, message=f"No MDM device found for '{person_id or 'unknown'}'. Check family_state.json or provide device_number directly.")

        try:
            device = await _mdm.get_device(device_number)
            location = await _mdm.get_device_location(device_number)
            dev_name = device.get("description") or device.get("name") or device.get("model") or device_number
            if not location:
                return ToolResult(success=True, message=f"No recent location is available for {display_name} — their device ({device_number}) may have location disabled or has not checked in recently.")
            lat = location.get("lat")
            lon = location.get("lon")
            ts = str(location.get("ts") or "unknown time")[:16]

            # Reverse geocode via Nominatim
            address = None
            try:
                async with _httpx.AsyncClient(timeout=5.0) as _hc:
                    r = await _hc.get(
                        "https://nominatim.openstreetmap.org/reverse",
                        params={"lat": lat, "lon": lon, "format": "json", "zoom": 16},
                        headers={"User-Agent": "Nova-HomeAssistant/1.0"},
                    )
                    if r.status_code == 200:
                        geo = r.json()
                        addr = geo.get("address", {})
                        parts = []
                        for key in ("road", "suburb", "city", "town", "village", "county"):
                            v = addr.get(key)
                            if v and v not in parts:
                                parts.append(v)
                        if parts:
                            address = ", ".join(parts[:3])
            except Exception:
                pass

            loc_str = address if address else f"{lat}, {lon}"
            return ToolResult(
                success=True,
                message=f"{display_name} was last seen near {loc_str} at {ts} (device: {dev_name}).",
            )
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM location error: {exc}")

    async def _search_apps(self, args: dict) -> "ToolResult":
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, message="query is required.")
        try:
            apps = await _mdm.search_apps(query, limit=12)
            if not apps:
                return ToolResult(success=True, message=f"No apps found for '{query}'.")
            lines = []
            for app in apps:
                mode = "installable" if app.get("installable") else "allow only"
                lines.append(f"{app.get('name') or app.get('pkg')} — {app.get('pkg')} ({mode})")
            return ToolResult(success=True, message="\n".join(lines))
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _mdm_set_app(self, args: dict, action: int) -> "ToolResult":
        device_number = str(args.get("device_number") or "").strip()
        package = str(args.get("package") or "").strip()
        if not device_number or not package:
            return ToolResult(success=False, message="device_number and package are required.")
        try:
            await _mdm.set_app_action(device_number, package, action)
            verb = "blocked" if action == 0 else "unblocked"
            return ToolResult(success=True, message=f"{package} {verb} on device {device_number}.")
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _deploy_app(self, args: dict) -> "ToolResult":
        device_number = str(args.get("device_number") or "").strip()
        package = str(args.get("package") or "").strip()
        name = str(args.get("name") or "").strip()
        if not device_number or not package:
            return ToolResult(success=False, message="device_number and package are required.")
        try:
            result = await _mdm.deploy_app(device_number, package, preferred_name=name)
            app = result.get("application") or {}
            mode = result.get("result_mode") or "allow"
            app_name = app.get("name") or name or package
            if mode == "install":
                msg = f"{app_name} is marked for install on device {device_number}."
            else:
                msg = (
                    f"{app_name} is allow-only in Headwind, so Nova allowed it on device {device_number} "
                    f"but Headwind cannot silently install it."
                )
            return ToolResult(success=True, message=msg)
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _send_device_message(self, args: dict) -> "ToolResult":
        device_number = str(args.get("device_number") or "").strip()
        message = str(args.get("message") or "").strip()
        if not device_number or not message:
            return ToolResult(success=False, message="device_number and message are required.")
        try:
            await _mdm.send_message(device_number, message)
            return ToolResult(success=True, message=f"Message sent to device {device_number}.")
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _get_parental_configurations(self) -> "ToolResult":
        try:
            configs = await _mdm.get_configurations()
            if not configs:
                return ToolResult(success=True, message="No parental configurations were found.")
            lines = []
            for cfg in configs:
                cfg_id = cfg.get("id", "?")
                name = cfg.get("name") or f"Configuration {cfg_id}"
                desc = str(cfg.get("description") or "").strip()
                lines.append(f"{cfg_id}: {name}" + (f" — {desc}" if desc else ""))
            return ToolResult(success=True, message="\n".join(lines))
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _get_enrollment_link(self, args: dict) -> "ToolResult":
        raw_config_id = args.get("config_id")
        if raw_config_id is None:
            return ToolResult(success=False, message="config_id is required.")
        try:
            config_id = int(raw_config_id)
            info = await _mdm.get_enrollment_info(config_id)
            return ToolResult(
                success=True,
                message=(
                    f"Enrollment link for {info.get('config_name') or config_id}: "
                    f"{info.get('enroll_url')} (QR key: {info.get('qr_key')})"
                ),
            )
        except Exception as exc:
            return ToolResult(success=False, message=f"MDM error: {exc}")

    async def _log_chore(self, args: dict) -> "ToolResult":
        svc = getattr(self, "_scoreboard_service", None)
        llm = getattr(self, "_llm_service", None)
        if svc is None:
            from avatar_backend.models.tool_result import ToolResult
            return ToolResult(success=False, message="Scoreboard service not available.")
        result = await svc.handle_log_chore(args, ha_proxy=self, llm_service=llm)
        from avatar_backend.models.tool_result import ToolResult
        return ToolResult(success=True, message=result)

    async def _get_scoreboard(self, args: dict) -> "ToolResult":
        from avatar_backend.models.tool_result import ToolResult
        svc = getattr(self, "_scoreboard_service", None)
        if svc is None:
            return ToolResult(success=False, message="Scoreboard service not available.")
        from datetime import datetime as _dt
        from collections import defaultdict
        period = str(args.get("period") or "week").strip().lower()
        lines = []
        try:
            if period == "today":
                midnight = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                logs = [l for l in svc.all_logs(days=1) if l["ts"] >= midnight]
                if not logs:
                    return ToolResult(success=True, message="No chores have been logged today yet.")
                by_person: dict = defaultdict(lambda: {"count": 0, "points": 0, "tasks": []})
                for log in logs:
                    p = log["person"].title()
                    by_person[p]["count"] += 1
                    by_person[p]["points"] += log["points"]
                    by_person[p]["tasks"].append(log["task_label"])
                for p, d in sorted(by_person.items(), key=lambda x: -x[1]["points"]):
                    lines.append(p + ": " + str(d["count"]) + " chore(s), " + str(d["points"]) + " pts - " + ", ".join(d["tasks"]))
                return ToolResult(success=True, message="Today's chores: " + "; ".join(lines))
            elif period == "week":
                scores = svc.weekly_scores()
                if not scores:
                    return ToolResult(success=True, message="No chores logged this week yet.")
                for i, s in enumerate(scores):
                    rank = ["1st", "2nd", "3rd"][i] if i < 3 else str(i+1) + "th"
                    lines.append(rank + ": " + s["person"].title() + " - " + str(s["points"]) + " pts (" + str(s["tasks"]) + " tasks)")
                return ToolResult(success=True, message="Weekly scoreboard: " + "; ".join(lines))
            else:
                logs = svc.recent_logs(10)
                if not logs:
                    return ToolResult(success=True, message="No chores logged recently.")
                for log in logs:
                    when = _dt.fromtimestamp(log["ts"]).strftime("%a %H:%M")
                    lines.append(log["person"].title() + ": " + log["task_label"] + " (+" + str(log["points"]) + "pts) at " + when)
                return ToolResult(success=True, message="Recent chores: " + "; ".join(lines))
        except Exception as exc:
            return ToolResult(success=False, message="Error fetching scoreboard: " + str(exc))

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
