"""
SensorWatchService — watches Home Assistant sensor.* state changes and
periodically reviews them with a local Ollama LLM (gemma2:9b).

Runs independently from ProactiveService and always uses local Ollama,
regardless of which cloud LLM is active for conversations.

Two announcement paths:
  1. Immediate threshold breach — hard limits on specific sensors
     (battery < 10 %, fridge power drops to 0 W, extreme temperature, low fuel,
      bin collection tomorrow) fire an announcement right away.
  2. Periodic snapshot review — every REVIEW_INTERVAL_S seconds, Ollama receives
     a snapshot of all watched sensor current values and decides if anything
     warrants a spoken announcement that the immediate path wouldn't catch.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Awaitable, Callable

import httpx
import structlog
import websockets
from avatar_backend.config import get_settings
from avatar_backend.services.llm_service import _select_local_text_model
from websockets.exceptions import ConnectionClosed, WebSocketException
from avatar_backend.services.home_runtime import load_home_runtime_config

_LOGGER = structlog.get_logger()


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


_SENSOR_WATCH_MODEL_PREFERENCES: tuple[str, ...] = (
    "qwen2.5:7b",
    "llama3.1:8b-instruct-q4_K_M",
    "llama3.1:8b",
    "mistral-nemo:12b",
    "gemma2:9b",
)


def _select_sensor_watch_model(settings) -> str:
    configured = (getattr(settings, "sensor_watch_ollama_model", "") or "").strip()
    if configured:
        return configured
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{settings.ollama_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
        installed = {str(model.get("name") or "").strip() for model in resp.json().get("models", [])}
    except Exception:
        installed = set()
    for candidate in _SENSOR_WATCH_MODEL_PREFERENCES:
        if candidate in installed:
            return candidate
    return _select_local_text_model(settings)

# ── Timing ────────────────────────────────────────────────────────────────────
# How often to run the periodic snapshot review
_REVIEW_INTERVAL_S = 1800          # 30 minutes

# Per-entity cooldown — don't re-announce the same sensor within this window
_ENTITY_COOLDOWN_S = 7200          # 2 hours

# Global cooldown between any sensor watch announcements
_GLOBAL_COOLDOWN_S = 900           # 15 minutes

# Min seconds between snapshot-review announcements (separate from threshold ones)
_REVIEW_ANNOUNCE_COOLDOWN_S = 3600 # 1 hour

# ── Device classes to include in periodic snapshot review ─────────────────────
# These are read from HA REST at review time (no WebSocket needed for snapshot).
_SNAPSHOT_DEVICE_CLASSES: set[str] = {
    "temperature",
    "humidity",
    "power",
    "battery",
    "energy",
    "monetary",
}

# ── Entity-level exclusions from snapshot (too noisy / irrelevant) ────────────
_LEGACY_SNAPSHOT_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "sensor.ble_",              # BLE beacon noise
    "sensor.cpu_",              # system metrics
    "sensor.awtrix_b21ffc_",   # matrix clock device
    "sensor.tangu_home_",         # Nova server hardware sensors (CPU/RAM/disk)
    "sensor.192_168_",            # network IP sensors
    "sensor.calex_",           # single smart plug — already low value
    "sensor.octopus_energy_a_ffd8c137_",  # loyalty points, spins — not actionable
    "sensor.octopus_energy_electricity_16p0478729_",  # rate/standing charge info
    "sensor.octopus_energy_gas_g4p04330231600_",
    "sensor.monthly_",         # monthly aggregates — too slow-moving to be actionable
    "sensor.daddy_s_pixel",    # phone battery — person manages their own phone
    "sensor.bedroom_1_thermo_closing",   # TRV internals
    "sensor.bedroom_1_thermo_idle",
    "sensor.bedroom_1_thermo_valve",
    "sensor.back_door_device_temperature",
    "sensor.back_entrance_device_temperature",
    "sensor.met_office_",
    "sensor.openweathermap_dew_point",
    "sensor.openweathermap_feels_like_temperature",
)

# ── Hard threshold rules — immediate announcement on WebSocket event ──────────
# Format: entity_id → {"min": float|None, "max": float|None, "label": str}
# An announcement fires when value crosses a bound (and cooldown allows).
_LEGACY_THRESHOLD_RULES: dict[str, dict] = {
    # Car fuel
    "sensor.ko66ewx_fuel_level": {
        "min": 15.0,
        "max": None,
        "label": "Car fuel level",
        "unit": "%",
        "min_msg": "Heads up — the car fuel level is low at {value}%. Consider filling up soon.",
    },
    # Bin collection — remind 1 day before
    "sensor.19_patterdale_place_black_bin_days_until_collection": {
        "min": None, "max": None,
        "equals": 1,
        "label": "Black bin collection",
        "unit": "days",
        "equals_msg": "Reminder: the black bin needs to go out tomorrow.",
    },
    "sensor.19_patterdale_place_brown_bin_days_until_collection": {
        "min": None, "max": None,
        "equals": 1,
        "label": "Brown bin collection",
        "unit": "days",
        "equals_msg": "Reminder: the brown bin needs to go out tomorrow.",
    },
    "sensor.19_patterdale_place_green_bin_days_until_collection": {
        "min": None, "max": None,
        "equals": 1,
        "label": "Green bin collection",
        "unit": "days",
        "equals_msg": "Reminder: the green bin needs to go out tomorrow.",
    },
    "sensor.19_patterdale_place_blue_bin_days_until_collection": {
        "min": None, "max": None,
        "equals": 1,
        "label": "Blue bin collection",
        "unit": "days",
        "equals_msg": "Reminder: the blue bin needs to go out tomorrow.",
    },
}

# ── Temperature sensor entity prefixes to SKIP in threshold check ─────────────
# (server hardware, TRV internals, door sensors — not room ambient sensors)
_LEGACY_TEMP_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "sensor.tangu_home_",
    "sensor.192_168_",
    "sensor.awtrix_",
    "sensor.back_door_device_",
    "sensor.back_entrance_device_",
    "sensor.bedroom_1_thermo_",
    "sensor.met_office_",
    "sensor.openweathermap_dew_point",
    "sensor.openweathermap_feels_like_temperature",
    "sensor.openweathermap_temperature",
)

_LEGACY_TEMP_EXCLUDE_SUBSTRINGS: tuple[str, ...] = (
    "_thermo_local_temperature",
    "_trv_local_temperature",
)

# ── Temperature thresholds applied to room temperature sensors ─────────────────
_TEMP_MAX_C = 32.0   # room too hot
_TEMP_MIN_C = 10.0   # room too cold

# ── Battery threshold applied to ALL battery sensors ──────────────────────────
_BATTERY_LOW_PCT = 10.0

# ── Noise states ──────────────────────────────────────────────────────────────
_NOISE_STATES = {"unavailable", "unknown", "none", ""}


def _spoken_unit(unit: str) -> str:
    normalized = str(unit or "").strip()
    return {
        "%": " percent",
        "W": " watts",
        "kW": " kilowatts",
        "kWh": " kilowatt hours",
        "°C": " degrees Celsius",
        "°F": " degrees Fahrenheit",
        "km/h": " kilometres per hour",
        "mph": " miles per hour",
    }.get(normalized, f" {normalized}" if normalized else "")


# ── Minimal inline Ollama client ──────────────────────────────────────────────

async def _ollama_generate(
    prompt: str,
    ollama_url: str,
    model: str,
    timeout_s: float = 120.0,
) -> str:
    """Single-shot text generation via local Ollama. No tools, low temperature."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 120},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        resp = await client.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload)
        resp.raise_for_status()
    return (resp.json().get("message", {}).get("content") or "").strip()


# ── Service ───────────────────────────────────────────────────────────────────

class SensorWatchService:
    """
    Subscribes to HA WebSocket state_changed events for sensor.* entities and
    periodically reviews sensor snapshots with a local Ollama LLM.

    Always uses Ollama (gemma2:9b) — never the active cloud LLM.
    """

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        ollama_url: str,
        announce_fn: Callable[[str, str], Awaitable[None]],
        issue_autofix_service=None,
    ) -> None:
        self._ha_url       = ha_url.rstrip("/")
        self._ha_token     = ha_token
        self._ollama_url   = ollama_url
        self._announce     = announce_fn
        self._issue_autofix_service = issue_autofix_service
        settings = get_settings()
        self._ollama_model = _select_sensor_watch_model(settings)
        self._review_timeout_s = max(30.0, float(settings.sensor_watch_review_timeout_s))
        runtime = load_home_runtime_config()
        self._snapshot_exclude_prefixes = tuple(
            dict.fromkeys(_LEGACY_SNAPSHOT_EXCLUDE_PREFIXES + tuple(runtime.sensor_snapshot_exclude_prefixes))
        )
        self._temp_exclude_prefixes = tuple(
            dict.fromkeys(_LEGACY_TEMP_EXCLUDE_PREFIXES + tuple(runtime.sensor_temp_exclude_prefixes))
        )
        self._threshold_rules = dict(_LEGACY_THRESHOLD_RULES)
        self._threshold_rules.update(runtime.sensor_threshold_rules)
        self._cooldowns: dict[str, float] = {}   # entity_id → last announce time
        self._last_global_announce: float  = 0.0
        self._last_review_announce: float  = 0.0
        self._task: asyncio.Task | None    = None
        self._decision_log = None

    def set_decision_log(self, log) -> None:
        self._decision_log = log

    def _llm_fields(self) -> dict[str, str]:
        return {
            "llm_provider": "ollama",
            "llm_model": self._ollama_model,
            "llm_tag": f"ollama:{self._ollama_model}",
        }

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="sensor_watch")
        _LOGGER.info(
            "sensor_watch.started",
            model=self._ollama_model,
            review_timeout_s=self._review_timeout_s,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.info("sensor_watch.stopped")

    # ── Main reconnect loop ────────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = 5
        while True:
            try:
                await self._ws_loop()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("sensor_watch.ws_disconnected", exc=_format_exc(exc), retry_in_s=backoff)
                if self._issue_autofix_service is not None:
                    await self._issue_autofix_service.report_issue(
                        "sensor_watch_ws_disconnected",
                        source="sensor_watch._run",
                        summary="Sensor watch websocket disconnected",
                        details={"exc": _format_exc(exc), "retry_in_s": backoff},
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    # ── WebSocket connection ───────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        ws_url = (
            self._ha_url
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            + "/api/websocket"
        )
        _LOGGER.info("sensor_watch.ws_connecting", url=ws_url)

        async with websockets.connect(
            ws_url, ping_interval=30, ping_timeout=10, open_timeout=10
        ) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"Expected auth_required, got {msg.get('type')}")

            await ws.send(json.dumps({"type": "auth", "access_token": self._ha_token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise RuntimeError(f"HA WebSocket auth failed: {msg}")

            await ws.send(json.dumps({
                "id": 2,
                "type": "subscribe_events",
                "event_type": "state_changed",
            }))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "result" or not msg.get("success"):
                raise RuntimeError(f"subscribe_events failed: {msg}")

            _LOGGER.info("sensor_watch.ws_ready")
            if self._issue_autofix_service is not None:
                await self._issue_autofix_service.resolve_issue(
                    "sensor_watch_ws_disconnected",
                    source="sensor_watch.ws_ready",
                )

            review_task = asyncio.create_task(self._review_loop(), name="sensor_review")
            try:
                async for raw in ws:
                    self._on_message(json.loads(raw))
            finally:
                review_task.cancel()
                try:
                    await review_task
                except asyncio.CancelledError:
                    pass

    # ── WebSocket event ingestion ──────────────────────────────────────────

    def _on_message(self, msg: dict) -> None:
        if msg.get("type") != "event":
            return
        event = msg.get("event", {})
        if event.get("event_type") != "state_changed":
            return

        data       = event.get("data", {})
        entity_id  = data.get("entity_id", "")

        if not entity_id.startswith("sensor."):
            return

        new_state  = data.get("new_state") or {}
        old_state  = data.get("old_state") or {}
        new_val    = new_state.get("state", "")
        old_val    = old_state.get("state", "")

        if new_val in _NOISE_STATES or old_val in _NOISE_STATES:
            return
        if new_val == old_val:
            return

        attrs        = new_state.get("attributes", {})
        device_class = attrs.get("device_class", "")
        friendly     = attrs.get("friendly_name", entity_id)

        # ── Check hard threshold rules ────────────────────────────────────
        if entity_id in self._threshold_rules:
            asyncio.create_task(
                self._check_threshold(entity_id, friendly, new_val, self._threshold_rules[entity_id]),
                name=f"threshold_{entity_id}",
            )
            return

        # ── Temperature sensors — check extreme values ────────────────────
        if device_class == "temperature":
            asyncio.create_task(
                self._check_temperature(entity_id, friendly, new_val),
                name=f"temp_{entity_id}",
            )
            return

        # ── Battery sensors — check low battery ───────────────────────────
        if device_class == "battery":
            asyncio.create_task(
                self._check_battery(entity_id, friendly, new_val),
                name=f"bat_{entity_id}",
            )
            return

    # ── Immediate threshold checks ─────────────────────────────────────────

    def _entity_on_cooldown(self, entity_id: str) -> bool:
        return time.monotonic() - self._cooldowns.get(entity_id, 0) < _ENTITY_COOLDOWN_S

    def _global_on_cooldown(self) -> bool:
        return time.monotonic() - self._last_global_announce < _GLOBAL_COOLDOWN_S

    async def _announce_now(self, entity_id: str, message: str, priority: str = "normal") -> None:
        now = time.monotonic()
        self._cooldowns[entity_id] = now
        self._last_global_announce  = now
        _LOGGER.info("sensor_watch.announcing", entity_id=entity_id, priority=priority)
        try:
            await self._announce(message, priority)
        except Exception as exc:
            _LOGGER.warning("sensor_watch.announce_failed", entity_id=entity_id, exc=_format_exc(exc))

    async def _check_threshold(
        self, entity_id: str, friendly: str, raw_val: str, rule: dict
    ) -> None:
        if self._entity_on_cooldown(entity_id) or self._global_on_cooldown():
            return
        try:
            value = float(raw_val)
        except (ValueError, TypeError):
            return

        unit = _spoken_unit(rule.get("unit", ""))
        msg  = None
        priority = "normal"

        # Equals check (e.g. bin days == 1)
        if rule.get("equals") is not None and value == rule["equals"]:
            msg = rule.get("equals_msg", f"{friendly} is {value}{unit}.")

        # Min breach
        elif rule.get("min") is not None and value < rule["min"]:
            template = rule.get("min_msg", f"{friendly} is below threshold: {value}{unit}.")
            msg = template.replace("{value}", str(round(value, 1)))
            priority = "alert"

        # Max breach
        elif rule.get("max") is not None and value > rule["max"]:
            template = rule.get("max_msg", f"{friendly} is above threshold: {value}{unit}.")
            msg = template.replace("{value}", str(round(value, 1)))
            priority = "alert"

        if msg:
            _LOGGER.info("sensor_watch.threshold_breach",
                         entity_id=entity_id, value=value, rule=rule.get("label"))
            if self._decision_log:
                self._decision_log.record(
                    "sensor_threshold_announce",
                    entity=entity_id,
                    friendly=friendly,
                    priority=priority,
                    rule=rule.get("label"),
                    message=msg[:300],
                    **self._llm_fields(),
                )
            await self._announce_now(entity_id, msg, priority)

    async def _check_temperature(self, entity_id: str, friendly: str, raw_val: str) -> None:
        # Skip non-room temperature sensors
        if any(entity_id.startswith(p) for p in self._temp_exclude_prefixes):
            return
        if any(part in entity_id for part in _LEGACY_TEMP_EXCLUDE_SUBSTRINGS):
            return
        if self._entity_on_cooldown(entity_id) or self._global_on_cooldown():
            return
        try:
            value = float(raw_val)
        except (ValueError, TypeError):
            return

        if value > _TEMP_MAX_C:
            msg = f"It's getting quite warm — {friendly} is reading {round(value, 1)} degrees Celsius. You may want to open a window."
            await self._announce_now(entity_id, msg)
        elif value < _TEMP_MIN_C:
            msg = f"The temperature near {friendly} has dropped to {round(value, 1)} degrees Celsius. It's quite cold — you may want to check the heating."
            await self._announce_now(entity_id, msg)

    async def _check_battery(self, entity_id: str, friendly: str, raw_val: str) -> None:
        if self._entity_on_cooldown(entity_id) or self._global_on_cooldown():
            return
        try:
            value = float(raw_val)
        except (ValueError, TypeError):
            return

        if value < _BATTERY_LOW_PCT:
            msg = f"Low battery alert — {friendly} is at {round(value, 0):.0f} percent. It may need replacing soon."
            await self._announce_now(entity_id, msg)

    # ── Periodic snapshot review ───────────────────────────────────────────

    async def _review_loop(self) -> None:
        """Every REVIEW_INTERVAL_S, fetch a sensor snapshot and ask Ollama to review it."""
        # Stagger first review by 5 minutes so server is fully warmed up
        await asyncio.sleep(300)
        while True:
            try:
                await self._run_snapshot_review()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("sensor_watch.review_error", exc=_format_exc(exc))
            await asyncio.sleep(_REVIEW_INTERVAL_S)

    async def _fetch_sensor_snapshot(self) -> list[dict]:
        """Fetch current states of all actionable sensors from HA REST API."""
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                resp = await client.get(f"{self._ha_url}/api/states", headers=headers)
                resp.raise_for_status()
                all_states = resp.json()
        except Exception as exc:
            _LOGGER.warning("sensor_watch.snapshot_fetch_failed", exc=_format_exc(exc))
            return []

        results = []
        for entity in all_states:
            entity_id = entity.get("entity_id", "")
            if not entity_id.startswith("sensor."):
                continue
            state = entity.get("state", "")
            if state in _NOISE_STATES:
                continue
            # Skip excluded prefixes
            if any(entity_id.startswith(p) for p in self._snapshot_exclude_prefixes):
                continue

            attrs        = entity.get("attributes", {})
            device_class = attrs.get("device_class", "")
            unit         = attrs.get("unit_of_measurement", "")
            friendly     = attrs.get("friendly_name", entity_id)

            # Include if device class matches OR it's one of our threshold entities
            if device_class not in _SNAPSHOT_DEVICE_CLASSES and entity_id not in self._threshold_rules:
                continue

            # Skip battery sensors that are fine (> 20%) to keep prompt short
            if device_class == "battery":
                try:
                    if float(state) > 20:
                        continue
                except (ValueError, TypeError):
                    pass

            results.append({
                "entity_id": entity_id,
                "friendly": friendly,
                "state": state,
                "unit": unit,
                "device_class": device_class,
            })

        return results

    async def _run_snapshot_review(self) -> None:
        """Fetch sensor snapshot and ask Ollama if anything warrants an announcement."""
        # Don't run if a review announcement was very recent
        if time.monotonic() - self._last_review_announce < _REVIEW_ANNOUNCE_COOLDOWN_S:
            _LOGGER.debug("sensor_watch.review_cooldown")
            return

        snapshot = await self._fetch_sensor_snapshot()
        if not snapshot:
            _LOGGER.debug("sensor_watch.review_empty_snapshot")
            return

        now_str  = datetime.datetime.now().strftime("%A %H:%M")
        lines    = "\n".join(
            f"  {s['friendly']} ({s['entity_id']}): {s['state']} {s['unit']}".rstrip()
            for s in snapshot
        )

        prompt = (
            f"You are Nova's background sensor monitor. Current time: {now_str}.\n\n"
            "Review these Home Assistant sensor readings and decide if anything warrants "
            "a spoken announcement to the household RIGHT NOW.\n\n"
            f"Current sensor values:\n{lines}\n\n"
            "ANNOUNCE only if you see a genuinely actionable or safety-relevant condition:\n"
            "  • A battery under 15% that has not already been announced recently\n"
            "  • A room temperature outside normal comfort range (below 14°C or above 30°C)\n"
            "  • Humidity above 80% (mold risk) or below 15% (very dry)\n"
            "  • A bin collection due tomorrow (days_until_collection = 1)\n"
            "  • Unusually high daily energy cost (electricity > £8 or gas > £5 for the day)\n"
            "  • Any other clear anomaly a household would want to know about immediately\n\n"
            "DO NOT announce:\n"
            "  • Normal sensor readings within expected ranges\n"
            "  • Minor fluctuations or gradual changes\n"
            "  • Anything already obvious or handled by scheduled automations\n\n"
            "Speak as Nova in first person, naturally. Keep it to 1-2 sentences.\n\n"
            "Reply with JSON only (no markdown):\n"
            '{"announce": true, "message": "...", "priority": "normal"}\n'
            "or\n"
            '{"announce": false}'
        )

        try:
            raw = await _ollama_generate(
                prompt,
                self._ollama_url,
                model=self._ollama_model,
                timeout_s=self._review_timeout_s,
            )
        except Exception as exc:
            _LOGGER.warning(
                "sensor_watch.review_ollama_failed",
                exc=_format_exc(exc),
                model=self._ollama_model,
                timeout_s=self._review_timeout_s,
            )
            if self._decision_log:
                self._decision_log.record(
                    "sensor_review_error",
                    reason=_format_exc(exc)[:200],
                    timeout_s=self._review_timeout_s,
                    **self._llm_fields(),
                )
            return

        raw = raw.strip()
        # Strip markdown fences if model adds them
        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        import re as _re
        m = _re.search(r'\{.*?\}', raw, _re.DOTALL)
        if not m:
            _LOGGER.warning("sensor_watch.review_bad_json", raw=raw[:300])
            return

        try:
            result = json.loads(m.group())
        except json.JSONDecodeError:
            _LOGGER.warning("sensor_watch.review_bad_json", raw=raw[:300])
            return

        if not result.get("announce"):
            _LOGGER.debug("sensor_watch.review_no_action")
            if self._decision_log:
                self._decision_log.record(
                    "sensor_review_silence",
                    sensor_count=len(snapshot),
                    **self._llm_fields(),
                )
            return

        message  = (result.get("message") or "").strip()
        priority = result.get("priority", "normal")
        if priority not in ("normal", "alert"):
            priority = "normal"

        if not message:
            return

        _LOGGER.info("sensor_watch.review_announcing", chars=len(message), priority=priority)
        now = time.monotonic()
        self._last_review_announce = now
        self._last_global_announce = now
        if self._decision_log:
            self._decision_log.record(
                "sensor_review_announce",
                sensor_count=len(snapshot),
                priority=priority,
                message=message[:300],
                **self._llm_fields(),
            )

        try:
            await self._announce(message, priority)
        except Exception as exc:
            _LOGGER.warning("sensor_watch.review_announce_failed", exc=_format_exc(exc))
