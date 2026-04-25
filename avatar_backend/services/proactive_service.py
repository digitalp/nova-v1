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
import traceback
from avatar_backend.services._shared_http import _http_client
from avatar_backend.services.heating_controller import (
    HeatingController,
    _HEATING_SHADOW_SYSTEM_PROMPT,
    _is_heating_action_tool,
)

import asyncio
import datetime
import json
import time
from typing import Awaitable, Callable

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from avatar_backend.services.home_runtime import load_home_runtime_config
from avatar_backend.services.coral_detector import CoralMotionDetector

_LOGGER = structlog.get_logger()


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


# Motion sensor → camera mapping.
# When a motion sensor fires, Nova fetches the associated camera and describes what it sees.
# Duplicate sensors for the same camera share the same camera cooldown.
_LEGACY_MOTION_CAMERA_MAP: dict[str, str] = {}
# Populated from config/home_runtime.json — run install.sh to configure.

# binary_sensor device_classes that represent motion/presence.
# These are excluded from batch triage — they're either handled by the camera
# vision path (_MOTION_CAMERA_MAP) or by dedicated HA automations.
# Letting them reach the batch LLM produces unreliable "motion detected" blurts
# that contain no camera description.
_MOTION_DEVICE_CLASSES = {"motion", "occupancy", "presence", "moving"}

# Cameras that bypass the global motion-announce cooldown.
# Used for high-priority cameras (e.g. driveway delivery detection) that should
# always announce regardless of how recently another motion event fired.
_LEGACY_BYPASS_GLOBAL_MOTION_CAMERAS: set[str] = set()

# Per-camera vision prompts — override _DEFAULT_IMAGE_PROMPT for specific cameras.
_DRIVEWAY_IMAGE_PROMPT = (
    "This is a security camera snapshot of a residential driveway. "
    "Only alert if you see: a person, an unfamiliar vehicle, an unexpected object, or unusual activity. "
    "If motion was caused solely by a parked car (e.g. lighting change) or has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes. "
    "If you can see someone making a delivery (carrying a parcel, delivery uniform, or liveried van), "
    "append a new line with EXACTLY:\n"
    "DELIVERY: <company>\n"
    "where <company> is one of: DHL, Royal Mail, Amazon, or Unknown. "
    "Only include the DELIVERY line if you are confident a delivery is taking place."
)

_OUTDOOR1_IMAGE_PROMPT = (
    "This is a security camera snapshot of the rear garden / outdoor area. "
    "Only alert if you see a person, an unfamiliar vehicle, an animal, or unusual activity. "
    "If motion was caused solely by plants moving in the wind, lighting changes, or has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes."
)

_DOORBELL_IMAGE_PROMPT = (
    "This is a security camera snapshot of the front door. "
    "Describe who or what triggered the doorbell or motion sensor. "
    "If nothing meaningful is visible or motion has no obvious cause, "
    "reply with exactly: NO_MOTION\n"
    "Otherwise describe what you see in 1-2 sentences. "
    "Do NOT mention age, race, gender or personal attributes. "
    "If you can see someone making a delivery (carrying a parcel, delivery uniform, or liveried van), "
    "append a new line with EXACTLY:\n"
    "DELIVERY: <company>\n"
    "where <company> is one of: DHL, Royal Mail, Amazon, or Unknown. "
    "Only include the DELIVERY line if you are confident a delivery is taking place."
)

_LEGACY_CAMERA_VISION_PROMPTS: dict[str, str] = {}
# Populated from config/home_runtime.json — map camera entity IDs to vision prompts.

# Entity IDs to completely ignore — handled by dedicated HA automations or
# too noisy to be useful in batch triage.
# Reolink AI detection sensors have no device_class so they bypass the
# _MOTION_DEVICE_CLASSES filter; list them explicitly here instead.
_LEGACY_EXCLUDE_ENTITIES: set[str] = set()
# Populated from config/home_runtime.json — entities to ignore in batch triage.

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

_LEGACY_WEATHER_ENTITY = ""


# Minimum seconds between weather condition-change announcements

# Hour of day (local time) to give the daily forecast (0-23)

# States that are noise — skip silently
_NOISE_STATES = {"unavailable", "unknown", "none"}

# Minimum seconds before re-announcing the same entity (batch triage)
_COOLDOWN_S = 600  # 10 minutes

# Minimum seconds before re-queuing the same entity, regardless of whether the
# LLM announced or not. Prevents duplicate LLM calls for the same rapid event.
_QUEUE_SEEN_COOLDOWN_S = 120  # 2 minutes

# Minimum seconds before re-announcing from the same camera (motion events)

# Minimum seconds between ANY motion announcement (global cap)

# Minimum seconds between ANY batch-triage proactive announcements

# Collect changes for this many seconds before triaging

# Max changes passed to LLM per batch


from avatar_backend.services.proactive_batch import ProactiveBatchMixin
from avatar_backend.services.proactive_motion import ProactiveMotionMixin
from avatar_backend.services.proactive_weather import ProactiveWeatherMixin

class ProactiveService(ProactiveMotionMixin, ProactiveBatchMixin, ProactiveWeatherMixin):
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
        announce_fn: Callable[..., Awaitable[None]],
        system_prompt: str,
        event_service=None,
        camera_event_service=None,
        issue_autofix_service=None,
        coral_detector: CoralMotionDetector | None = None,
        ha_ws_manager=None,
    ) -> None:
        self._ha_url = ha_url.rstrip("/")
        self._ha_token = ha_token
        self._ha = ha_proxy
        self._llm = llm_service
        self._motion_clip_service = motion_clip_service
        self._announce = announce_fn
        self._system_prompt = system_prompt
        self._heating = HeatingController(
            ha=ha_proxy,
            llm=llm_service,
            system_prompt=system_prompt,
            announce_fn=announce_fn,
        )
        self._event_service = event_service
        self._camera_event_service = camera_event_service
        self._issue_autofix_service = issue_autofix_service
        self._ha_ws_manager = ha_ws_manager
        self._coral = coral_detector or CoralMotionDetector.build()
        if self._coral.enabled:
            _LOGGER.info("coral.enabled", detail="Edge TPU pre-filter active for camera motion events")
        else:
            _LOGGER.info("coral.disabled", detail="No Coral TPU — all motion events go straight to Ollama vision")
        runtime = load_home_runtime_config()
        self._clip_camera_map: dict[str, str] = {}  # sensor → preferred clip camera override
        self._motion_camera_map = dict(_LEGACY_MOTION_CAMERA_MAP)
        self._motion_camera_map.update(runtime.motion_camera_map)
        self._bypass_global_motion_cameras = set(_LEGACY_BYPASS_GLOBAL_MOTION_CAMERAS)
        self._bypass_global_motion_cameras.update(runtime.bypass_global_motion_cameras)
        self._camera_vision_prompts = dict(_LEGACY_CAMERA_VISION_PROMPTS)
        self._camera_vision_prompts.update(runtime.camera_vision_prompts)
        self._vision_enabled_cameras = set(getattr(runtime, 'vision_enabled_cameras', []))
        self._camera_room_map: dict[str, str] = dict(getattr(runtime, 'camera_room_map', {}))
        self._camera_labels = dict(getattr(runtime, 'camera_labels', {}))
        self._exclude_entities = set(_LEGACY_EXCLUDE_ENTITIES)
        self._exclude_entities.update(runtime.exclude_entities)
        self._weather_entity = runtime.weather_entity or _LEGACY_WEATHER_ENTITY
        self._phone_notify_services = runtime.phone_notify_services
        self._cooldowns: dict[str, float] = {}
        self._queue_seen: dict[str, float] = {}   # queue-time dedup cooldown
        self._camera_cooldowns: dict[str, float] = {}
        from avatar_backend.config import get_settings as _gs
        self._camera_capture_cooldown_s = _gs().proactive_camera_capture_cooldown_s
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
        self._heating.set_decision_log(log)

    def apply_discovery(self, discovery_result) -> None:
        """Merge auto-discovered camera/motion mappings into the live maps.

        Discovery results are layered on top of legacy + runtime config:
          legacy → runtime config → auto-discovery
        This means discovered mappings take lowest priority — explicit
        config in home_runtime.json always wins.
        """
        if not getattr(discovery_result, "discovered", False):
            return
        # Only add discovered mappings for sensors NOT already mapped
        for sensor, camera in discovery_result.motion_camera_map.items():
            if sensor not in self._motion_camera_map:
                self._motion_camera_map[sensor] = camera
        for cam in discovery_result.bypass_global_motion_cameras:
            self._bypass_global_motion_cameras.add(cam)
        for cam, prompt in discovery_result.camera_vision_prompts.items():
            if cam not in self._camera_vision_prompts:
                self._camera_vision_prompts[cam] = prompt
        _LOGGER.info(
            "proactive.discovery_applied",
            total_motion_mappings=len(self._motion_camera_map),
            total_bypass_cameras=len(self._bypass_global_motion_cameras),
            total_vision_prompts=len(self._camera_vision_prompts),
        )

    def update_system_prompt(self, prompt: str) -> None:
        """Called by sync-prompt to keep the proactive context current."""
        self._system_prompt = prompt
        _LOGGER.info("proactive.prompt_updated", chars=len(prompt))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._ha_ws_manager is not None:
            # Use shared WS manager — register callback and start background tasks
            self._ha_ws_manager.register("proactive", self._on_message)
            self._batch_task = asyncio.create_task(self._batch_loop(), name="proactive_batcher")
            self._forecast_task = asyncio.create_task(self._daily_forecast_loop(), name="proactive_forecast")
            self._heating_task = asyncio.create_task(self._heating_control_loop(), name="proactive_heating")
            _LOGGER.info("proactive.started", mode="shared_ws")
        else:
            self._task = asyncio.create_task(self._run(), name="proactive_monitor")
            _LOGGER.info("proactive.started", mode="own_ws")

    async def stop(self) -> None:
        if self._ha_ws_manager is not None:
            self._ha_ws_manager.unregister("proactive")
            for attr in ("_batch_task", "_forecast_task", "_heating_task"):
                task = getattr(self, attr, None)
                if task:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
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
                if self._issue_autofix_service is not None:
                    await self._issue_autofix_service.report_issue(
                        "proactive_ws_disconnected",
                        source="proactive._run",
                        summary="Proactive websocket disconnected",
                        details={"exc": str(exc), "retry_in_s": backoff},
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
            if self._issue_autofix_service is not None:
                await self._issue_autofix_service.resolve_issue(
                    "proactive_ws_disconnected",
                    source="proactive.ws_ready",
                )

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
        # Also catches camera AI sensors (e.g. Reolink person detectors) that have
        # no standard device_class but are explicitly mapped to a camera.
        if domain == "binary_sensor":
            device_class = new_state.get("attributes", {}).get("device_class", "")
            is_motion_sensor = device_class in _MOTION_DEVICE_CLASSES
            is_camera_mapped = entity_id in self._motion_camera_map
            if is_motion_sensor or is_camera_mapped:
                if is_camera_mapped and new_val == "on":
                    camera_id = self._motion_camera_map[entity_id]
                    # Per-camera capture cooldown — prevents hammering vision APIs
                    # (Gemini 429s, Ollama GPU OOM) when a camera fires repeatedly.
                    # Set to 60s which is long enough for API rate limits to recover.
                    _CAMERA_CAPTURE_COOLDOWN_S = self._camera_capture_cooldown_s
                    if time.monotonic() - self._camera_cooldowns.get(camera_id, 0) < _CAMERA_CAPTURE_COOLDOWN_S:
                        _LOGGER.debug("proactive.motion_capture_interval",
                                      entity_id=entity_id, camera=self._cam_label(camera_id),
                                      interval_s=_CAMERA_CAPTURE_COOLDOWN_S)
                        return
                    self._camera_cooldowns[camera_id] = time.monotonic()
                    friendly = new_state.get("attributes", {}).get("friendly_name", entity_id)
                    asyncio.create_task(
                        self._handle_motion_event(entity_id, friendly, camera_id),
                        name=f"motion_{entity_id}",
                    )
                else:
                    _LOGGER.debug("proactive.motion_no_camera", entity_id=entity_id,
                                  hint="add to motion_camera_map in config/home_runtime.json to enable vision description")
                return  # always return — never queue motion/camera-mapped sensors for batch triage

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
