"""Mixin for ProactiveService: weather condition monitoring and daily forecast announcement."""
from __future__ import annotations
import asyncio
import datetime
import time

import structlog

from avatar_backend.services._shared_http import _http_client

_LOGGER = structlog.get_logger()

_WEATHER_ALERT_CONDITIONS = {
    "rainy", "pouring", "snowy", "snowy-rainy", "hail",
    "lightning", "lightning-rainy", "exceptional", "fog", "windy-variant",
}
_WEATHER_COOLDOWN_S = 3600  # 1 hour
_FORECAST_HOUR = 7


class ProactiveWeatherMixin:
    """Weather condition change alerts and daily spoken forecast — mixed into ProactiveService."""
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
            message = await self._llm.generate_text_local_fast_resilient(
                prompt,
                timeout_s=20.0,
                retry_delay_s=2.0,
                fallback_timeout_s=20.0,
                purpose="weather_announce",
            )
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
                        **self._fast_local_llm_fields(),
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
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        url = f"{self._ha_url}/api/services/weather/get_forecasts?return_response"
        payload = {"entity_id": self._weather_entity, "type": "daily"}

        try:
            resp = await _http_client().post(url, headers=headers, json=payload, timeout=15.0)
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
            message = await self._llm.generate_text_local_fast_resilient(
                prompt,
                timeout_s=30.0,
                retry_delay_s=2.0,
                fallback_timeout_s=25.0,
                purpose="forecast_announce",
            )
            message = message.strip()
            if message:
                await self._announce(message, "normal")
                if self._decision_log:
                    self._decision_log.record(
                        "forecast_announce",
                        message=message[:300],
                        **self._fast_local_llm_fields(),
                    )
                _LOGGER.info("proactive.forecast_announced", chars=len(message))
        except Exception as exc:
            _LOGGER.warning("proactive.forecast_llm_failed", exc=str(exc))

    # ── Batch triage ──────────────────────────────────────────────────────
