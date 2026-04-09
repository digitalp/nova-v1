"""
ProactiveService — watches Home Assistant state changes via WebSocket and
asks the LLM if anything warrants a proactive announcement.

Flow:
  HA WS state_changed events
    → filter to important domains + non-trivial state changes

  Motion sensors (binary_sensor with camera mapping):
    → immediate camera fetch + vision describe + announce (bypasses batch)
    → per-camera cooldown (10 min) to avoid duplicate announces

  All other watched domains:
    → batch for BATCH_WINDOW_S seconds
    → LLM triage (generate_text, 30 s timeout)
    → if LLM says announce → call announce_fn(message, priority)
    → mark entities on cooldown (10 min) to avoid spam
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Awaitable, Callable

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from avatar_backend.services.home_runtime import load_home_runtime_config

_LOGGER = structlog.get_logger()

# Motion sensor → camera mapping.
# When a motion sensor fires, Nova fetches the associated camera and describes what it sees.
# Duplicate sensors for the same camera share the same camera cooldown.
_LEGACY_MOTION_CAMERA_MAP: dict[str, str] = {
    # Driveway camera — both general motion and AI-person triggers
    "binary_sensor.rlc_1224a_motion": "camera.rlc_1224a_fluent",
    "binary_sensor.rlc_1224a_person": "camera.rlc_1224a_fluent",
}

# binary_sensor device_classes that represent motion/presence.
# These are excluded from batch triage — they're either handled by the camera
# vision path (_MOTION_CAMERA_MAP) or by dedicated HA automations.
# Letting them reach the batch LLM produces unreliable "motion detected" blurts
# that contain no camera description.
_MOTION_DEVICE_CLASSES = {"motion", "occupancy", "presence", "moving"}

# Cameras that bypass the global motion-announce cooldown.
# Used for high-priority cameras (e.g. driveway delivery detection) that should
# always announce regardless of how recently another motion event fired.
_LEGACY_BYPASS_GLOBAL_MOTION_CAMERAS: set[str] = {"camera.rlc_1224a_fluent"}

# Per-camera vision prompts — override _DEFAULT_IMAGE_PROMPT for specific cameras.
_DRIVEWAY_IMAGE_PROMPT = (
    "This is a security camera snapshot of a residential driveway. "
    "The owner\'s car (a blue Mercedes B200 2017 AMG, plate KO66EWX) is normally parked here — "
    "ignore it as a motion cause unless it is moving or something unusual is happening with it. "
    "Only alert if you see: a person, an unfamiliar vehicle, an unexpected object, or unusual activity. "
    "If motion was caused solely by the owner\'s parked car (e.g. lighting change) or has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes. "
    "If you can see someone making a delivery (carrying a parcel, delivery uniform, or liveried van), "
    "append a new line with EXACTLY:\n"
    "DELIVERY: <company>\n"
    "where <company> is one of: DHL, Royal Mail, Amazon, or Unknown. "
    "Only include the DELIVERY line if you are confident a delivery is taking place."
)

_LEGACY_CAMERA_VISION_PROMPTS: dict[str, str] = {
    "camera.rlc_1224a_fluent": _DRIVEWAY_IMAGE_PROMPT,
}

# Entity IDs to completely ignore — handled by dedicated HA automations or
# too noisy to be useful in batch triage.
# Reolink AI detection sensors have no device_class so they bypass the
# _MOTION_DEVICE_CLASSES filter; list them explicitly here instead.
_LEGACY_EXCLUDE_ENTITIES: set[str] = {
    # Doorbell AI detections — handled by /announce/doorbell automation
    "binary_sensor.reolink_video_doorbell_poe_person",
    "binary_sensor.reolink_video_doorbell_poe_vehicle",
    "binary_sensor.reolink_video_doorbell_poe_visitor",
    "binary_sensor.reolink_video_doorbell_poe_face",
    "binary_sensor.reolink_video_doorbell_poe_package",
    # Outdoor cam 2 (driveway) — handled via _MOTION_CAMERA_MAP / vision path
    # binary_sensor.rlc_1224a_person — NOT excluded; wired to camera vision
    # Xbox Live / gaming sensors — not home relevant
    "binary_sensor.terminator5704",
    "binary_sensor.terminator5704_subscribed_to_xbox_game_pass",
    # Android kiosk device sensors
    "binary_sensor.rk3566_device_admin",
    "binary_sensor.rk3566_kiosk_mode",
}

# Domains monitored for batch triage announcements.
# 'sensor' excluded — numeric sensors emit constant updates and threshold alerts
# are already handled by dedicated HA automations.
# Motion binary_sensors are handled separately via _MOTION_CAMERA_MAP.
# 'device_tracker' and 'person' excluded — arrivals/departures are routine and
# already filtered by the LLM prompt, but excluding at ingestion prevents noise.
_WATCH_DOMAINS = {
    "binary_sensor",
    "lock",
    "cover",
    "alarm_control_panel",
    "input_boolean",
    "climate",  # heating mode changes (off↔heat) monitored proactively
}

# Climate states worth announcing — ignore attribute-only changes and idle fluctuations
_CLIMATE_ANNOUNCE_STATES = {"heat", "cool", "heat_cool", "auto", "dry", "fan_only", "off"}

# ── Weather monitoring ────────────────────────────────────────────────────────
_LEGACY_WEATHER_ENTITY = "weather.met_office_ince_in_makerfield"

# Weather conditions that are significant enough to warrant an announcement
# when they start or end.
_WEATHER_ALERT_CONDITIONS = {
    "rainy", "pouring", "snowy", "snowy-rainy", "hail",
    "lightning", "lightning-rainy", "exceptional", "fog", "windy-variant",
}

# Minimum seconds between weather condition-change announcements
_WEATHER_COOLDOWN_S = 3600  # 1 hour

# Hour of day (local time) to give the daily forecast (0-23)
_FORECAST_HOUR = 7

# States that are noise — skip silently
_NOISE_STATES = {"unavailable", "unknown", "none"}

# Minimum seconds before re-announcing the same entity (batch triage)
_COOLDOWN_S = 600  # 10 minutes

# Minimum seconds before re-queuing the same entity, regardless of whether the
# LLM announced or not. Prevents duplicate LLM calls for the same rapid event.
_QUEUE_SEEN_COOLDOWN_S = 120  # 2 minutes

# Minimum seconds before re-announcing from the same camera (motion events)
_CAMERA_COOLDOWN_S = 600  # 10 minutes

# Minimum seconds between ANY motion announcement (global cap)
_GLOBAL_MOTION_COOLDOWN_S = 600  # 10 minutes

# Minimum seconds between ANY batch-triage proactive announcements
_GLOBAL_ANNOUNCE_COOLDOWN_S = 300  # 5 minutes

# Collect changes for this many seconds before triaging
_BATCH_WINDOW_S = 60

# Max changes passed to LLM per batch
_MAX_CHANGES = 20

_HOUSE_NEEDS_ATTENTION_ENTITY = "binary_sensor.house_needs_attention"
_HOUSE_ATTENTION_SUMMARY_ENTITY = "sensor.house_attention_summary"
_HOUSE_ATTENTION_NORMAL_STATES = {"", "unknown", "unavailable", "home looks normal"}


class ProactiveService:
    """
    Subscribes to HA WebSocket state_changed events and makes proactive
    announcements when the LLM judges them worth saying.
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        ha_proxy,
        llm_service,
        motion_clip_service,
        announce_fn: Callable[[str, str], Awaitable[None]],
        system_prompt: str,
        event_service=None,
        camera_event_service=None,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._ha = ha_proxy
        self._llm = llm_service
        self._motion_clip_service = motion_clip_service
        self._announce = announce_fn
        self._system_prompt = system_prompt
        self._event_service = event_service
        self._camera_event_service = camera_event_service
        runtime = load_home_runtime_config()
        self._motion_camera_map = dict(_LEGACY_MOTION_CAMERA_MAP)
        self._motion_camera_map.update(runtime.motion_camera_map)
        self._bypass_global_motion_cameras = set(_LEGACY_BYPASS_GLOBAL_MOTION_CAMERAS)
        self._bypass_global_motion_cameras.update(runtime.bypass_global_motion_cameras)
        self._camera_vision_prompts = dict(_LEGACY_CAMERA_VISION_PROMPTS)
        self._camera_vision_prompts.update(runtime.camera_vision_prompts)
        self._exclude_entities = set(_LEGACY_EXCLUDE_ENTITIES)
        self._exclude_entities.update(runtime.exclude_entities)
        self._weather_entity = runtime.weather_entity or _LEGACY_WEATHER_ENTITY
        self._cooldowns: dict[str, float] = {}
        self._queue_seen: dict[str, float] = {}   # queue-time dedup cooldown
        self._camera_cooldowns: dict[str, float] = {}
        self._last_motion_announce_time: float = 0.0
        self._last_announce_time: float = 0.0
        self._queue: list[dict] = []
        self._task: asyncio.Task | None = None
        # Weather monitoring state
        self._last_weather_condition: str = ""
        self._last_weather_announce_time: float = 0.0
        self._last_forecast_date: str = ""
        self._decision_log = None

    def set_decision_log(self, log) -> None:
        self._decision_log = log

    def _active_llm_fields(self) -> dict[str, str]:
        provider = getattr(self._llm, "provider_name", "unknown")
        model = getattr(self._llm, "model_name", "unknown")
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_tag": f"{provider}:{model}",
        }

    def _gemini_llm_fields(self) -> dict[str, str]:
        provider = getattr(self._llm, "gemini_vision_provider_name", "google")
        model = getattr(self._llm, "gemini_vision_effective_model_name", "gemini")
        return {
            "llm_provider": provider,
            "llm_model": model,
            "llm_tag": f"{provider}:{model}",
        }

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

            # Start batch processor, daily forecast, and heating control loops
            batch_task   = asyncio.create_task(self._batch_loop(), name="proactive_batcher")
            forecast_task = asyncio.create_task(self._daily_forecast_loop(), name="proactive_forecast")
            heating_task  = asyncio.create_task(self._heating_control_loop(), name="proactive_heating")
            try:
                async for raw in ws:
                    self._on_message(json.loads(raw))
            finally:
                batch_task.cancel()
                forecast_task.cancel()
                heating_task.cancel()
                for t in (batch_task, forecast_task):
                    try:
                        await t
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

        new_state = data.get("new_state") or {}
        old_state = data.get("old_state") or {}
        new_val = new_state.get("state", "")
        old_val = old_state.get("state", "")

        if new_val == old_val:
            return
        if new_val in _NOISE_STATES or old_val in _NOISE_STATES:
            return

        # Explicitly excluded entities — handled by dedicated automations or irrelevant
        if entity_id in self._exclude_entities:
            _LOGGER.debug("proactive.entity_excluded", entity_id=entity_id)
            return

        # Motion/occupancy/presence binary_sensors — handle via camera vision path
        # or drop entirely.  Never let them reach the batch LLM triage.
        if domain == "binary_sensor":
            device_class = new_state.get("attributes", {}).get("device_class", "")
            if device_class in _MOTION_DEVICE_CLASSES:
                if entity_id in self._motion_camera_map and new_val == "on":
                    camera_id = self._motion_camera_map[entity_id]
                    if time.monotonic() - self._camera_cooldowns.get(camera_id, 0) >= _CAMERA_COOLDOWN_S:
                        self._camera_cooldowns[camera_id] = time.monotonic()
                        friendly = new_state.get("attributes", {}).get("friendly_name", entity_id)
                        asyncio.create_task(
                            self._handle_motion_event(entity_id, friendly, camera_id),
                            name=f"motion_{entity_id}",
                        )
                    else:
                        _LOGGER.debug("proactive.motion_camera_cooldown", entity_id=entity_id, camera=camera_id)
                        if self._decision_log:
                            self._decision_log.record("motion_cooldown", entity=entity_id, camera=camera_id)
                else:
                    _LOGGER.debug("proactive.motion_no_camera", entity_id=entity_id,
                                  hint="add to motion_camera_map in config/home_runtime.json to enable vision description")
                return  # always return — never queue motion sensors for batch triage

        # Weather entity — handled by dedicated weather monitor, not batch triage
        if entity_id == self._weather_entity:
            if new_val != old_val:
                asyncio.create_task(
                    self._handle_weather_change(old_val, new_val, new_state),
                    name="weather_change",
                )
            return

        if domain not in _WATCH_DOMAINS:
            return

        # For binary_sensor: only queue off→on transitions
        if domain == "binary_sensor" and new_val != "on":
            return

        # Door/window contacts: only queue during night hours (22:00–06:00 local time).
        # Routine daytime door use is never worth announcing; long-open anomalies are
        # handled by the system prompt's state drift detection rules, not batch triage.
        if domain == "binary_sensor":
            device_class = new_state.get("attributes", {}).get("device_class", "")
            if device_class in ("door", "window", "garage_door"):
                hour = datetime.datetime.now().hour
                if 6 <= hour < 22:
                    _LOGGER.debug("proactive.door_daytime_skip", entity_id=entity_id, hour=hour)
                    return

        # Climate: only queue meaningful HVAC mode transitions (e.g. off→heat, heat→off).
        # Skip attribute-only updates where the mode string hasn't changed.
        if domain == "climate":
            if old_val == new_val:
                return  # attribute-only update, mode unchanged
            if new_val not in _CLIMATE_ANNOUNCE_STATES:
                return  # unavailable / unknown / transient state

        # Per-entity announce cooldown (set after LLM announces)
        if time.monotonic() - self._cooldowns.get(entity_id, 0) < _COOLDOWN_S:
            return

        # Queue-time dedup: don't queue the same entity twice within 2 minutes,
        # even if the LLM said "no" last time. Prevents wasted LLM calls.
        now_m = time.monotonic()
        if now_m - self._queue_seen.get(entity_id, 0) < _QUEUE_SEEN_COOLDOWN_S:
            _LOGGER.debug("proactive.queue_seen_cooldown", entity_id=entity_id)
            return
        self._queue_seen[entity_id] = now_m

        friendly = new_state.get("attributes", {}).get("friendly_name", entity_id)
        self._queue.append({
            "entity_id": entity_id,
            "friendly": friendly,
            "old": old_val,
            "new": new_val,
            "queued_at": time.monotonic(),
        })
        _LOGGER.info("proactive.event_queued", entity_id=entity_id, old=old_val, new=new_val)

    # ── Motion + camera describe ──────────────────────────────────────────

    async def _handle_motion_event(self, entity_id: str, friendly: str, camera_id: str) -> None:
        """Fetch a camera snapshot, describe it with vision, and announce."""
        bypass_global = camera_id in self._bypass_global_motion_cameras

        # Global motion rate limit — skipped for bypass cameras (e.g. driveway)
        if not bypass_global:
            since_last = time.monotonic() - self._last_motion_announce_time
            if since_last < _GLOBAL_MOTION_COOLDOWN_S:
                _LOGGER.debug("proactive.motion_global_cooldown",
                              seconds_remaining=int(_GLOBAL_MOTION_COOLDOWN_S - since_last))
                return

        _LOGGER.info("proactive.motion_triggered", entity_id=entity_id, camera=camera_id,
                     bypass_global=bypass_global)
        if self._decision_log:
            self._decision_log.record(
                "motion_triggered",
                entity=entity_id,
                camera=camera_id,
                **self._gemini_llm_fields(),
            )

        try:
            result = await self._camera_event_service.analyze_motion(
                camera_entity_id=camera_id,
                location=friendly,
                trigger_entity_id=entity_id,
                source="proactive_motion",
                system_prompt=self._system_prompt or None,
                vision_prompt=self._camera_vision_prompts.get(camera_id),
            )
        except Exception as exc:
            _LOGGER.warning("proactive.motion_describe_failed", camera=camera_id, exc=str(exc))
            result = {
                "message": f"Motion detected by {friendly}.",
                "description": "",
                "archive_description": f"Motion detected by {friendly}.",
                "suppressed": False,
                "is_delivery": False,
                "delivery_company": "",
                "raw_description": "",
                "canonical_event": None,
            }

        is_delivery = bool(result["is_delivery"])
        delivery_company = str(result["delivery_company"] or "")
        message = str(result["message"] or f"Motion detected by {friendly}.")
        description = str(result["archive_description"] or result["description"] or message)

        if result["suppressed"]:
            _LOGGER.info("proactive.motion_suppressed", camera=camera_id, reason="owner_car_or_no_cause")
            if self._decision_log:
                self._decision_log.record(
                    "motion_suppressed",
                    camera=camera_id,
                    reason="NO_MOTION",
                    **self._gemini_llm_fields(),
                )
        elif result["raw_description"]:
            _LOGGER.info("proactive.motion_described", camera=camera_id,
                         chars=len(result["raw_description"]), delivery=is_delivery)
            if is_delivery:
                _LOGGER.info("proactive.delivery_detected", camera=camera_id,
                             company=delivery_company)
                if self._decision_log:
                    self._decision_log.record(
                        "delivery_detected",
                        camera=camera_id,
                        company=delivery_company,
                        scene=description[:200],
                        **self._gemini_llm_fields(),
                    )

        extra = {
            "delivery": is_delivery,
            "delivery_company": delivery_company,
        }
        if result.get("canonical_event") is not None:
            extra["canonical_event"] = result["canonical_event"]

        self._motion_clip_service.schedule_capture(
            camera_entity_id=camera_id,
            trigger_entity_id=entity_id,
            location=friendly,
            description=description,
            extra=extra,
        )

        self._last_motion_announce_time = time.monotonic()
        if self._decision_log:
            self._decision_log.record(
                "motion_clip_archived",
                camera=camera_id,
                message=description[:300],
                delivery=is_delivery,
                **self._gemini_llm_fields(),
            )

        # For deliveries, also push to phones (user may be away from home)
        if is_delivery:
            title = f"Delivery – {delivery_company}" if delivery_company else "Delivery at driveway"
            await self._notify_phones(title, message)

    async def _notify_phones(self, title: str, message: str) -> None:
        """Push a notification to both registered phones via HA."""
        import httpx as _httpx
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        for svc in ("notify/mobile_app_pixel_7", "notify/mobile_app_pixel_9_pro_xl"):
            url = f"{self._ha_url}/api/services/{svc}"
            try:
                async with _httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    resp = await client.post(url, headers=headers,
                                             json={"title": title, "message": message})
                    resp.raise_for_status()
                _LOGGER.info("proactive.phone_notified", service=svc)
            except Exception as exc:
                _LOGGER.warning("proactive.phone_notify_failed", service=svc, exc=str(exc))

    # ── Weather monitoring ────────────────────────────────────────────────

    async def _handle_weather_change(self, old_condition: str, new_condition: str, new_state: dict) -> None:
        """Announce significant weather condition changes (e.g. clear → rainy)."""
        going_to_alert = new_condition in _WEATHER_ALERT_CONDITIONS
        leaving_alert  = old_condition in _WEATHER_ALERT_CONDITIONS

        if not going_to_alert and not leaving_alert:
            _LOGGER.debug("proactive.weather_minor_change", old=old_condition, new=new_condition)
            self._last_weather_condition = new_condition
            return

        since_last = time.monotonic() - self._last_weather_announce_time
        if since_last < _WEATHER_COOLDOWN_S:
            _LOGGER.debug("proactive.weather_cooldown", seconds_remaining=int(_WEATHER_COOLDOWN_S - since_last))
            self._last_weather_condition = new_condition
            return

        attrs = new_state.get("attributes", {})
        temp  = attrs.get("temperature", "?")
        wind  = attrs.get("wind_speed", "")
        wind_str = f", wind {wind} kilometres per hour" if wind else ""

        prompt = (
            f"The weather at home has just changed from '{old_condition}' to '{new_condition}'. "
            f"Current temperature: {temp} degrees Celsius{wind_str}. "
            "As Nova, write a brief (1-2 sentence) natural spoken announcement about this weather change. "
            "Include a practical tip if relevant (e.g. umbrella for rain, stay indoors for lightning). "
            "Be conversational and warm, not robotic. "
            "When speaking, always say units as words, not symbols."
        )

        try:
            message = await self._llm.generate_text(prompt, timeout_s=20.0)
            message = message.strip()
            if message:
                self._last_weather_announce_time = time.monotonic()
                self._last_weather_condition = new_condition
                await self._announce(message, "normal")
                if self._decision_log:
                    self._decision_log.record(
                        "weather_announce",
                        old=old_condition,
                        new=new_condition,
                        message=message[:300],
                        **self._active_llm_fields(),
                    )
                _LOGGER.info("proactive.weather_announced", old=old_condition, new=new_condition)
        except Exception as exc:
            _LOGGER.warning("proactive.weather_announce_failed", exc=str(exc))

    async def _daily_forecast_loop(self) -> None:
        """Sleep until _FORECAST_HOUR each morning then announce the day's forecast."""
        while True:
            now    = datetime.datetime.now()
            target = now.replace(hour=_FORECAST_HOUR, minute=0, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            wait_s = (target - now).total_seconds()
            _LOGGER.debug("proactive.forecast_sleeping", wait_h=round(wait_s / 3600, 1))
            await asyncio.sleep(wait_s)

            today_str = datetime.date.today().isoformat()
            if self._last_forecast_date == today_str:
                continue  # already announced today (e.g. reconnect)

            try:
                await self._announce_daily_forecast()
                self._last_forecast_date = today_str
            except Exception as exc:
                _LOGGER.warning("proactive.forecast_failed", exc=str(exc))

    async def _announce_daily_forecast(self) -> None:
        """Fetch weather forecasts from HA and announce a spoken morning summary."""
        import httpx as _httpx
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        url = f"{self._ha_url}/api/services/weather/get_forecasts?return_response"
        payload = {"entity_id": self._weather_entity, "type": "daily"}

        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            _LOGGER.warning("proactive.forecast_fetch_failed", exc=str(exc))
            return

        forecasts = data.get("service_response", {}).get(self._weather_entity, {}).get("forecast", [])
        if not forecasts:
            _LOGGER.warning("proactive.forecast_empty")
            return

        def _fmt(f: dict) -> str:
            dt = f.get("datetime", "")
            try:
                day = datetime.datetime.fromisoformat(dt).strftime("%A")
            except Exception:
                day = "Unknown"
            cond  = f.get("condition", "?")
            hi    = f.get("temperature", "?")
            lo    = f.get("templow", "?")
            rain  = f.get("precipitation", 0)
            rain_str = f", {rain} millimetres of rain" if rain else ""
            return f"{day}: {cond}, high {hi} degrees Celsius, low {lo} degrees Celsius{rain_str}"

        today_line = _fmt(forecasts[0]) if forecasts else "No data"
        week_lines = "\n".join(_fmt(f) for f in forecasts[1:6]) if len(forecasts) > 1 else ""

        prompt = (
            f"Good morning. Here is today's weather and the week ahead:\n"
            f"Today: {today_line}\n"
            + (f"This week:\n{week_lines}\n" if week_lines else "")
            + "\nAs Nova, write a friendly 2-4 sentence morning weather briefing. "
            + "When speaking, always say units as words, not symbols. "
            "Highlight the most important weather for today, note anything noteworthy "
            "coming this week (rain, heat, cold), and give a practical tip. "
            "Be warm and natural — not a robotic read-out."
        )

        try:
            message = await self._llm.generate_text(prompt, timeout_s=30.0)
            message = message.strip()
            if message:
                await self._announce(message, "normal")
                if self._decision_log:
                    self._decision_log.record(
                        "forecast_announce",
                        message=message[:300],
                        **self._active_llm_fields(),
                    )
                _LOGGER.info("proactive.forecast_announced", chars=len(message))
        except Exception as exc:
            _LOGGER.warning("proactive.forecast_llm_failed", exc=str(exc))

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

        # Resolve current state for each queued entity.
        # If a binary_sensor that transitioned to "on" has already returned to "off",
        # the event is transient — drop it entirely to prevent stale announcements
        # (e.g. "back door opened" reported 90 seconds after the door was closed).
        resolved_changes = []
        for c in changes:
            current = await self._ha.get_entity_state(c["entity_id"])
            current_val = (current or {}).get("state", "")
            domain_c = c["entity_id"].split(".")[0]
            if domain_c == "binary_sensor" and c["new"] == "on" and current_val == "off":
                age_s = int(time.monotonic() - c.get("queued_at", time.monotonic()))
                _LOGGER.info(
                    "proactive.event_resolved",
                    entity_id=c["entity_id"],
                    age_s=age_s,
                    hint="binary_sensor returned to off before triage — skipping",
                )
                continue
            resolved_changes.append(c)

        if not resolved_changes:
            _LOGGER.debug("proactive.all_resolved", hint="all queued events already resolved")
            return
        changes = resolved_changes

        rendered_lines: list[str] = []
        for c in changes:
            rendered_lines.append(await self._render_change_for_triage(c))
        lines = "\n".join(rendered_lines)
        entity_ids = [c["entity_id"] for c in changes]
        _LOGGER.info("proactive.triaging", n_changes=len(changes), entities=entity_ids)

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
            raw = await self._llm.generate_text(prompt, timeout_s=60.0)
        except Exception as exc:
            _LOGGER.warning("proactive.llm_failed", exc=str(exc))
            return

        raw = raw.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        # Extract first JSON object — llama3.1 often appends prose after the JSON
        import re as _re
        m = _re.search(r'\{.*?\}', raw, _re.DOTALL)
        if not m:
            _LOGGER.warning("proactive.bad_json", raw=raw[:300])
            return
        try:
            result = json.loads(m.group())
        except json.JSONDecodeError:
            _LOGGER.warning("proactive.bad_json", raw=raw[:300])
            return

        if not result.get("announce"):
            _LOGGER.debug("proactive.no_action")
            if self._decision_log:
                self._decision_log.record(
                    "triage_silence",
                    entities=[c["entity_id"] for c in changes],
                    reason="LLM: no announcement needed",
                    **self._active_llm_fields(),
                )
            return

        message = (result.get("message") or "").strip()
        priority = result.get("priority", "normal")
        if priority not in ("normal", "alert"):
            priority = "normal"

        if not message:
            return

        message = await self._augment_announcement_message(changes, message)

        _LOGGER.info("proactive.announcing", chars=len(message), priority=priority)
        if self._decision_log:
            self._decision_log.record(
                "triage_announce",
                entities=[c["entity_id"] for c in changes],
                priority=priority,
                message=message,
                **self._active_llm_fields(),
            )

        now = time.monotonic()
        self._last_announce_time = now
        for c in changes:
            self._cooldowns[c["entity_id"]] = now

        await self._announce(message, priority)

    async def _render_change_for_triage(self, change: dict) -> str:
        entity_id = change["entity_id"]
        friendly = change["friendly"]
        old_val = change["old"]
        new_val = change["new"]

        if entity_id == _HOUSE_NEEDS_ATTENTION_ENTITY:
            summary = await self._get_house_attention_summary()
            if summary:
                return (
                    f"- {friendly} ({entity_id}): {old_val} → {new_val}"
                    f" | concrete issue: {summary}"
                )

        return f"- {friendly} ({entity_id}): {old_val} → {new_val}"

    async def _get_house_attention_summary(self) -> str | None:
        summary_state = await self._ha.get_entity_state(_HOUSE_ATTENTION_SUMMARY_ENTITY)
        summary = str((summary_state or {}).get("state", "")).strip()
        if summary.lower() in _HOUSE_ATTENTION_NORMAL_STATES:
            return None
        return summary

    async def _augment_announcement_message(self, changes: list[dict], message: str) -> str:
        if not any(change.get("entity_id") == _HOUSE_NEEDS_ATTENTION_ENTITY for change in changes):
            return message

        summary = await self._get_house_attention_summary()
        if not summary:
            return message
        return self._direct_house_attention_message(summary)

    @staticmethod
    def _direct_house_attention_message(summary: str) -> str:
        clean = " ".join(str(summary or "").split()).strip()
        if not clean:
            return "I've noticed something at home that needs attention."
        if clean[-1] not in ".!?":
            clean += "."
        return f"I've noticed {clean[0].lower() + clean[1:] if len(clean) > 1 else clean.lower()}"


    # ── Autonomous Heating Control ────────────────────────────────────────────

    _HEATING_INTERVAL_S = 1800  # evaluate every 30 minutes

    async def _heating_control_loop(self) -> None:
        """
        Runs every 30 minutes. Reads room/outdoor temperatures and presence,
        then lets the LLM (with full tool access) decide whether to adjust
        the Hive boiler and winter_mode. Nova is the sole heating controller
        — the schedule-based HA automations have been disabled.
        """
        # Stagger first run by 2 minutes so Nova finishes startup first
        await asyncio.sleep(120)
        while True:
            try:
                await self._evaluate_heating()
            except Exception as exc:
                _LOGGER.warning("heating.eval_error", exc=str(exc))
            await asyncio.sleep(self._HEATING_INTERVAL_S)

    async def _evaluate_heating(self) -> None:
        """
        Runs a full agentic loop (LLM + tool execution) to evaluate and
        adjust heating. The system prompt contains the decision rules.
        """
        import datetime as _dt
        now_str = _dt.datetime.now().strftime("%A, %d %B %Y %H:%M")
        month = _dt.datetime.now().month
        season = "spring/summer" if 4 <= month <= 9 else "autumn/winter"

        task_msg = (
            f"[Autonomous heating evaluation — {now_str}, {season}] "
            "Read all room temperature sensors, the outdoor temperature, and current presence. "
            "Then apply the heating decision rules from your system prompt and take action if needed. "
            "Be concise — one sentence announcement only if something changed, silent otherwise."
        )

        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": task_msg},
        ]

        _MAX_ROUNDS = 6
        _LOGGER.info("heating.eval_start")
        if self._decision_log:
            self._decision_log.record(
                "heating_eval_start",
                season=season,
                time=now_str,
                **self._active_llm_fields(),
            )

        all_tool_calls: list[str] = []

        for round_num in range(_MAX_ROUNDS):
            text, tool_calls = await self._llm.chat(messages, use_tools=True)

            if not tool_calls:
                # LLM gave a final text response
                if text and text.strip() and "nothing changed" not in text.lower() and "no change" not in text.lower():
                    _LOGGER.info("heating.eval_announce", message=text[:120])
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_action",
                            message=text.strip()[:300],
                            tool_calls=all_tool_calls,
                            **self._active_llm_fields(),
                        )
                    await self._announce(text.strip(), "normal")
                else:
                    _LOGGER.info("heating.eval_silent")
                    if self._decision_log:
                        self._decision_log.record(
                            "heating_eval_silent",
                            reason=text.strip()[:200] if text else "no change needed",
                            tool_calls=all_tool_calls,
                            **self._active_llm_fields(),
                        )
                break

            # Build assistant turn in OpenAI wire format
            raw_tcs = [
                {"id": f"htool_{i}", "type": "function",
                 "function": {"name": tc.function_name, "arguments": tc.arguments}}
                for i, tc in enumerate(tool_calls)
            ]
            messages.append({"role": "assistant", "content": text or "", "tool_calls": raw_tcs})

            # Execute each tool call
            for i, tc in enumerate(tool_calls):
                result = await self._ha.execute_tool_call(tc)
                summary = f"{tc.function_name}({tc.arguments}) → {(result.message or '')[:80]}"
                all_tool_calls.append(summary)
                _LOGGER.info(
                    "heating.tool_call",
                    tool=tc.function_name,
                    args=tc.arguments,
                    success=result.success,
                    result=(result.message or "")[:120],
                )
                if self._decision_log:
                    self._decision_log.record(
                        "heating_tool_call",
                        tool=tc.function_name,
                        args={k: str(v)[:80] for k, v in tc.arguments.items()},
                        success=result.success,
                        result=(result.message or "")[:200],
                        **self._active_llm_fields(),
                    )
                messages.append({
                    "role":         "tool",
                    "tool_call_id": f"htool_{i}",
                    "content":      result.message or "",
                })

            if round_num == _MAX_ROUNDS - 1:
                _LOGGER.warning("heating.eval_max_rounds")
                if self._decision_log:
                    self._decision_log.record("heating_eval_max_rounds", rounds=_MAX_ROUNDS)
                break

        _LOGGER.info("heating.eval_done")
