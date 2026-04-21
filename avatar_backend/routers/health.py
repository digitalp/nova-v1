"""
GET /health         — full component status (requires API key)
GET /health/public  — unauthenticated legacy liveness probe
GET /health/live    — liveness probe (no auth)
GET /health/ready   — readiness probe (depends on Ollama + HA)
GET /health/history — rolling health-check history
"""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query
import httpx
import structlog

from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.config import get_settings

router = APIRouter(tags=["health"])
logger = structlog.get_logger()

_VERSION = "0.7.0"


# ── Component probes ──────────────────────────────────────────────────────────

async def _probe_ollama(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{url}/api/tags")
            return "reachable" if resp.status_code == 200 else f"http_{resp.status_code}"
    except httpx.ConnectError:
        return "unreachable"
    except httpx.TimeoutException:
        return "timeout"


async def _probe_ha(url: str, token: str) -> str:
    try:
        timeout = httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{url}/api/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                return "reachable"
            if resp.status_code == 401:
                return "bad_token"
            return f"http_{resp.status_code}"
    except httpx.ConnectError:
        return "unreachable"
    except httpx.TimeoutException:
        return "timeout"


def _probe_whisper(container: AppContainer) -> str:
    """Check if the Whisper model is loaded and ready."""
    try:
        stt = container.stt_service
        return "ready" if stt.is_ready else "loading"
    except Exception as exc:
        logger.warning("health.whisper_probe_error", exc=str(exc))
        return "unavailable"


def _probe_piper(container: AppContainer) -> str:
    """Check if the Piper binary and voice model are present."""
    try:
        tts = container.tts_service
        return "ready" if tts.is_ready else "missing"
    except Exception as exc:
        logger.warning("health.piper_probe_error", exc=str(exc))
        return "unavailable"


async def _probe_intron_afro_tts(url: str) -> str:
    """Check if the Intron Afro TTS sidecar is reachable and loaded."""
    if not url:
        return "not_configured"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{url}/health")
            if resp.status_code == 200:
                data = resp.json()
                return "ready" if data.get("loaded") else "loading"
            return f"http_{resp.status_code}"
    except httpx.ConnectError:
        return "unreachable"
    except httpx.TimeoutException:
        return "timeout"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health")
async def health_check(container: AppContainer = Depends(get_container)) -> dict:
    settings = get_settings()

    ollama_status, ha_status, intron_status = await asyncio.gather(
        _probe_ollama(settings.ollama_url),
        _probe_ha(settings.ha_url, settings.ha_token),
        _probe_intron_afro_tts(settings.intron_afro_tts_url),
    )

    whisper_status = _probe_whisper(container)
    piper_status   = _probe_piper(container)

    components = {
        "ollama":           ollama_status,
        "whisper":          whisper_status,
        "piper":            piper_status,
        "home_assistant":   ha_status,
        "intron_afro_tts":  intron_status,
    }

    healthy    = {"reachable", "ready", "loading"}
    # intron_afro_tts is optional — don't degrade overall status if it's off
    core_components = {k: v for k, v in components.items() if k != "intron_afro_tts"}
    all_ok     = all(v in healthy for v in core_components.values())
    overall    = "ok" if all_ok else "degraded"

    issue_autofix = getattr(container, "issue_autofix_service", None)
    if issue_autofix is not None:
        if ha_status == "timeout":
            await issue_autofix.report_issue(
                "home_assistant_timeout",
                source="health_check",
                summary="Home Assistant health probe timed out",
                details={"components": components},
            )
        elif ha_status == "reachable":
            await issue_autofix.resolve_issue("home_assistant_timeout", source="health_check")

    logger.info("health.checked", status=overall, components=components)

    # Persist each component probe result for the history endpoint
    health_history = getattr(container, "health_history_service", None)
    if health_history is not None:
        for comp, comp_status in components.items():
            try:
                health_history.record_check(comp, comp_status)
            except Exception as exc:
                logger.warning("health.persist_failed", component=comp, exc=str(exc))

    return {"status": overall, "version": _VERSION, "components": components}


@router.get("/health/public")
async def health_public() -> dict:
    """Unauthenticated liveness probe — used by load balancers / systemd watchdog."""
    return {"status": "ok", "version": _VERSION}


@router.get("/health/live")
async def health_live() -> dict:
    """Liveness probe — process is alive. Always returns 200."""
    return {"status": "alive", "version": _VERSION}


@router.get("/health/ready")
async def health_ready(container: AppContainer = Depends(get_container)) -> dict:
    """Readiness probe — core dependencies connected."""
    settings = get_settings()
    ollama_status, ha_status = await asyncio.gather(
        _probe_ollama(settings.ollama_url),
        _probe_ha(settings.ha_url, settings.ha_token),
    )
    ws_mgr = getattr(container, "ha_ws_manager", None)
    ws_connected = ws_mgr.is_connected if ws_mgr else False

    ready = ollama_status == "reachable" and ha_status == "reachable"
    return {
        "ready": ready,
        "ollama": ollama_status,
        "home_assistant": ha_status,
        "websocket_mirror": "connected" if ws_connected else "disconnected",
    }


@router.get("/health/history")
async def health_history(
    container: AppContainer = Depends(get_container),
    component: Optional[str] = Query(None, description="Filter by component name"),
    since: Optional[str] = Query(None, description="ISO-8601 start timestamp"),
    until: Optional[str] = Query(None, description="ISO-8601 end timestamp"),
) -> dict:
    """Return rolling health-check history, optionally filtered by component and time range.

    Defaults to the last 24 hours when no time range is specified.
    """
    svc = getattr(container, "health_history_service", None)
    if svc is None:
        return {"rows": [], "count": 0}

    # Default to last 24 hours if no time range given
    if since is None and until is None:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    rows = svc.get_history(component=component, since=since, until=until)
    return {"rows": rows, "count": len(rows)}


# ── Ambient data (clock + weather for avatar page) ────────────────────────────

import time as _time_mod
from datetime import datetime as _dt

_ambient_cache: dict = {"ts": 0.0, "data": {}}
_AMBIENT_TTL = 120  # cache weather for 2 minutes


@router.get("/ambient")
async def ambient_data(container: AppContainer = Depends(get_container)) -> dict:
    """Return current time + weather + hourly forecast for the avatar ambient display.
    Lightweight, no auth — polled every 60s by the avatar page."""
    now = _dt.now()
    result = {
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%A, %d %B"),
        "date_short": now.strftime("%a %d %b"),
        "year": now.strftime("%Y"),
    }

    # Return cached weather if fresh
    mono = _time_mod.monotonic()
    if mono - _ambient_cache["ts"] < _AMBIENT_TTL and _ambient_cache["data"]:
        result.update(_ambient_cache["data"])
        return result

    # Fetch weather from HA
    settings = get_settings()
    ha = getattr(container, "ha_proxy", None)
    if ha is None:
        return result

    from avatar_backend.services.home_runtime import load_home_runtime_config
    _rt = load_home_runtime_config()
    weather_entity = _rt.weather_entity or "weather.forecast_home"
    headers = {"Authorization": f"Bearer {settings.ha_token}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # Current state
            resp = await client.get(
                f"{settings.ha_url}/api/states/{weather_entity}",
                headers=headers,
            )
            weather = {}
            if resp.status_code == 200:
                s = resp.json()
                attrs = s.get("attributes", {})
                weather = {
                    "condition": s.get("state", ""),
                    "temperature": attrs.get("temperature"),
                    "humidity": attrs.get("humidity"),
                    "wind_speed": attrs.get("wind_speed"),
                    "wind_bearing": attrs.get("wind_bearing"),
                }

            # Hourly forecast
            try:
                fc_resp = await client.post(
                    f"{settings.ha_url}/api/services/weather/get_forecasts?return_response",
                    headers=headers,
                    json={"entity_id": weather_entity, "type": "hourly"},
                )
                if fc_resp.status_code == 200:
                    fc_data = fc_resp.json()
                    hourly = fc_data.get("service_response", {}).get(weather_entity, {}).get("forecast", [])
                    # Return next 4 hours
                    weather["hourly"] = [
                        {
                            "time": h.get("datetime", ""),
                            "temperature": h.get("temperature"),
                            "condition": h.get("condition", ""),
                        }
                        for h in hourly[:4]
                    ]
            except Exception:
                weather["hourly"] = []

            # Tomorrow forecast
            try:
                daily_resp = await client.post(
                    f"{settings.ha_url}/api/services/weather/get_forecasts?return_response",
                    headers=headers,
                    json={"entity_id": weather_entity, "type": "daily"},
                )
                if daily_resp.status_code == 200:
                    daily_data = daily_resp.json()
                    daily = daily_data.get("service_response", {}).get(weather_entity, {}).get("forecast", [])
                    if len(daily) >= 2:
                        tmrw = daily[1]
                        weather["tomorrow"] = {
                            "condition": tmrw.get("condition", ""),
                            "temperature_high": tmrw.get("temperature"),
                            "temperature_low": tmrw.get("templow"),
                        }
            except Exception:
                pass

            if weather:
                _ambient_cache["ts"] = mono
                _ambient_cache["data"] = weather
                result.update(weather)
    except Exception as exc:
        logger.warning("ambient.weather_fetch_failed", exc=str(exc))

    return result
