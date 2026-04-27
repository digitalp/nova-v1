"""
Periodic LLM-based snapshot review of all HA sensor states.
Fully self-contained mixin — no imports from sensor_watch_service.py.
"""
from __future__ import annotations
import asyncio
import datetime
import json
import time

import httpx
import structlog

from avatar_backend.config import get_settings
from avatar_backend.services._shared_http import _http_client

_LOGGER = structlog.get_logger()


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


# Constants owned by snapshot (not used elsewhere in sensor_watch_service)
_NOISE_STATES: frozenset[str] = frozenset({"unavailable", "unknown", "none", ""})

_REVIEW_INTERVAL_S          = 1800   # 30 minutes
_REVIEW_ANNOUNCE_COOLDOWN_S = 3600   # 1 hour
_MAX_REVIEW_SNAPSHOT_ITEMS  = 24

_SNAPSHOT_DEVICE_CLASSES: set[str] = {
    "temperature", "humidity", "power", "battery", "energy", "monetary",
}

# Used by _review_priority to bump priority of sensors near known thresholds.
# Kept as empty dict here; actual rules are on self._threshold_rules set in __init__.
_LEGACY_THRESHOLD_RULES: dict[str, dict] = {}


def _review_priority(sensor: dict) -> tuple[int, str]:
    entity_id    = str(sensor.get("entity_id") or "")
    device_class = str(sensor.get("device_class") or "")
    state        = str(sensor.get("state") or "")
    score = 0
    if entity_id in _LEGACY_THRESHOLD_RULES:
        score += 100
    if device_class == "battery":
        try:
            score += max(0, int(30 - float(state)))
        except (TypeError, ValueError):
            score += 10
    elif device_class == "temperature":
        try:
            v = float(state)
            score += 80 if (v < 14 or v > 30) else (30 if (v < 16 or v > 27) else 0)
        except (TypeError, ValueError):
            pass
    elif device_class == "humidity":
        try:
            v = float(state)
            score += 70 if (v < 15 or v > 80) else (25 if (v < 20 or v > 75) else 0)
        except (TypeError, ValueError):
            pass
    elif device_class == "monetary":
        try:
            v = float(state)
            score += 60 if v > 8 else (20 if v > 5 else 0)
        except (TypeError, ValueError):
            pass
    return (-score, entity_id)


def _compress_snapshot_for_review(snapshot: list[dict], limit: int = _MAX_REVIEW_SNAPSHOT_ITEMS) -> list[dict]:
    if len(snapshot) <= limit:
        return snapshot
    return sorted(snapshot, key=_review_priority)[:limit]


async def _ollama_generate(prompt: str, ollama_url: str, model: str, timeout_s: float = 120.0) -> str:
    """Single-shot text generation via local Ollama. No tools, low temperature."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 120},
    }
    resp = await _http_client().post(
        f"{ollama_url.rstrip('/')}/api/chat", json=payload, timeout=httpx.Timeout(timeout_s)
    )
    resp.raise_for_status()
    return (resp.json().get("message", {}).get("content") or "").strip()


class SensorSnapshotMixin:
    """Periodic sensor snapshot review via local LLM — mixed into SensorWatchService."""
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
        """Fetch current states of all actionable sensors, preferring WS mirror."""
        all_states = None
        ws_mgr = getattr(self, "_ha_ws_manager", None)
        if ws_mgr and ws_mgr.is_connected:
            all_states = ws_mgr.get_all_states()

        if not all_states:
            headers = {
                "Authorization": f"Bearer {self._ha_token}",
                "Content-Type": "application/json",
            }
            try:
                resp = await _http_client().get(f"{self._ha_url}/api/states", headers=headers, timeout=15.0)
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
            # Skip thermostat/TRV internal temperature sensors
            _thermo_skip = ("_thermo_local_temperature", "_thermostat_temperature",
                           "_thermostat_temp", "_trv_local_temperature", "_thermo_temperature", "_thermostat_local_temperature")
            if any(s in entity_id for s in _thermo_skip):
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
        raw_count = len(snapshot)
        snapshot = _compress_snapshot_for_review(snapshot)

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
            raw = await self._generate_review_text(prompt)
        except Exception as exc:
            _LOGGER.warning(
                "sensor_watch.review_ollama_failed",
                exc=_format_exc(exc),
                model=self._ollama_model,
                timeout_s=self._review_timeout_s,
                sensor_count=len(snapshot),
                raw_sensor_count=raw_count,
            )
            if self._decision_log:
                self._decision_log.record(
                    "sensor_review_error",
                    reason=_format_exc(exc)[:200],
                    timeout_s=self._review_timeout_s,
                    sensor_count=len(snapshot),
                    raw_sensor_count=raw_count,
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
                    raw_sensor_count=raw_count,
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
                raw_sensor_count=raw_count,
                priority=priority,
                message=message[:300],
                **self._llm_fields(),
            )

        try:
            await self._announce(message, priority)
        except Exception as exc:
            _LOGGER.warning("sensor_watch.review_announce_failed", exc=_format_exc(exc))

    async def _generate_review_text(self, prompt: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return await _ollama_generate(
                    prompt,
                    self._ollama_url,
                    model=self._ollama_model,
                    timeout_s=self._review_timeout_s,
                )
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt == 0:
                    _LOGGER.warning(
                        "sensor_watch.review_retry_scheduled",
                        model=self._ollama_model,
                        retry_delay_s=2.0,
                        exc=_format_exc(exc),
                    )
                    await asyncio.sleep(2.0)
                    continue
                break

        if self._llm_service is not None and getattr(self._llm_service, "provider_name", "ollama") != "ollama":
            _LOGGER.warning(
                "sensor_watch.review_local_failed_using_cloud",
                local_model=self._ollama_model,
                provider=getattr(self._llm_service, "provider_name", "unknown"),
                cloud_model=getattr(self._llm_service, "model_name", "unknown"),
                exc=_format_exc(last_exc)[:200] if last_exc else "local_failed",
            )
            return await self._llm_service.generate_text(
                prompt,
                timeout_s=min(self._review_timeout_s, 25.0),
            )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("sensor review generation failed")
