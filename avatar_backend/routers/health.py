"""
GET /health       — full component status (requires API key)
GET /health/public — liveness probe (no auth)
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Request
import httpx
import structlog

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


def _probe_whisper(request: Request) -> str:
    """Check if the Whisper model is loaded and ready."""
    try:
        stt = request.app.state.stt_service
        return "ready" if stt.is_ready else "loading"
    except Exception as exc:
        logger.warning("health.whisper_probe_error", exc=str(exc))
        return "unavailable"


def _probe_piper(request: Request) -> str:
    """Check if the Piper binary and voice model are present."""
    try:
        tts = request.app.state.tts_service
        return "ready" if tts.is_ready else "missing"
    except Exception as exc:
        logger.warning("health.piper_probe_error", exc=str(exc))
        return "unavailable"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/health")
async def health_check(request: Request) -> dict:
    settings = get_settings()

    ollama_status, ha_status = await asyncio.gather(
        _probe_ollama(settings.ollama_url),
        _probe_ha(settings.ha_url, settings.ha_token),
    )

    whisper_status = _probe_whisper(request)
    piper_status   = _probe_piper(request)

    components = {
        "ollama":         ollama_status,
        "whisper":        whisper_status,
        "piper":          piper_status,
        "home_assistant": ha_status,
    }

    healthy    = {"reachable", "ready", "loading"}
    all_ok     = all(v in healthy for v in components.values())
    overall    = "ok" if all_ok else "degraded"

    logger.info("health.checked", status=overall, components=components)
    return {"status": overall, "version": _VERSION, "components": components}


@router.get("/health/public")
async def health_public() -> dict:
    """Unauthenticated liveness probe — used by load balancers / systemd watchdog."""
    return {"status": "ok", "version": _VERSION}
