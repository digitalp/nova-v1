"""System sub-router: restart, tunnel, coral/*, heating-shadow/*, camera-discovery/*, music/*, music-ui/*, selfheal/*."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
from avatar_backend.services._shared_http import _http_client
import structlog

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response as RawResponse

from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.services.gemini_key_pool import serialize_pins_for_env, serialize_pool_for_env

from avatar_backend.runtime_paths import install_dir

from .common import (
    _ENV_FILE,
    _INSTALL_DIR,
    _get_session,
    _require_session,
    _update_env_value,
    MusicControlBody,
)

_LOGGER = structlog.get_logger()
router = APIRouter()

_RESTART_KIOSK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_SELFHEAL_BASE = "http://localhost:7779"
# ── Intron Afro TTS sidecar toggle ────────────────────────────────────────────

@router.get("/intron-tts/status")
async def intron_tts_status(request: Request):
    _require_session(request, min_role="viewer")
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "avatar_intron_afro_tts"],
            capture_output=True, text=True, timeout=5,
        )
        running = result.stdout.strip() == "true"
        return {"running": running}
    except Exception:
        return {"running": False}


@router.post("/intron-tts/toggle")
async def intron_tts_toggle(request: Request):
    _require_session(request, min_role="admin")
    body = await request.json()
    enable = body.get("enable", False)
    try:
        cmd = ["docker", "start" if enable else "stop", "avatar_intron_afro_tts"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        ok = result.returncode == 0
        return {"ok": ok, "running": enable if ok else not enable}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:100]}, status_code=500)
_MUSIC_ASSISTANT_BASE = "http://localhost:8095"


# ── Server controls ───────────────────────────────────────────────────────────

@router.post("/restart")
async def restart_server(request: Request):
    _require_session(request, min_role="admin")
    _LOGGER.info("admin.restart_requested")

    async def _do_restart():
        await asyncio.sleep(0.5)
        subprocess.Popen(["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "avatar-backend"])

    asyncio.create_task(_do_restart())
    return {"restarting": True}


# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────

@router.post("/tunnel/refresh")
async def refresh_tunnel(request: Request):
    """Restart the Cloudflare quick tunnel and update PUBLIC_URL with the new URL."""
    _require_session(request, min_role="admin")
    _LOGGER.info("admin.tunnel_refresh_requested")

    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo", "/usr/bin/systemctl", "restart", "cloudflared-nova",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)

    await asyncio.sleep(6)
    new_url = await _read_tunnel_url()

    if not new_url:
        return {"ok": False, "error": "Tunnel restarted but URL not yet available. Check logs."}

    _update_env_value("PUBLIC_URL", new_url)

    from avatar_backend.config import get_settings
    get_settings.cache_clear()

    _LOGGER.info("admin.tunnel_refreshed", url=new_url)
    return {"ok": True, "url": new_url}


@router.get("/tunnel/status")
async def tunnel_status(request: Request):
    """Check the current Cloudflare tunnel URL."""
    _require_session(request, min_role="viewer")
    url = await _read_tunnel_url()
    current_public = ""
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            if line.strip().startswith("PUBLIC_URL="):
                current_public = line.strip().split("=", 1)[1].strip()
                break
    return {
        "tunnel_url": url or "",
        "public_url": current_public,
        "match": bool(url and current_public == url),
        "tunnel_active": bool(url),
    }


async def _read_tunnel_url() -> str | None:
    """Read the current tunnel URL from journalctl."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/journalctl", "-u", "cloudflared-nova", "--no-pager", "-n", "30",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        output = stdout.decode("utf-8", "ignore")
        import re
        matches = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', output)
        return matches[-1] if matches else None
    except Exception:
        return None


# ── Heating Shadow ────────────────────────────────────────────────────────────

@router.get("/heating-shadow/history")
async def get_heating_shadow_history(request: Request, limit: int = 40, container: AppContainer = Depends(get_container)):
    """Return recent heating shadow decision log entries for the admin panel."""
    _require_session(request)
    log = getattr(container, "decision_log", None)
    if log is None:
        return {"entries": []}
    kinds = {
        "heating_shadow_eval_start",
        "heating_shadow_tool_call",
        "heating_shadow_round_silent",
        "heating_shadow_max_rounds",
        "heating_shadow_eval_error",
        "heating_shadow_comparison",
    }
    all_entries = log.recent(500)
    filtered = [e for e in all_entries if e.get("kind") in kinds][-limit:]
    return {"entries": filtered}


@router.post("/heating-shadow/force")
async def force_heating_shadow(request: Request, scenario: str = "winter", container: AppContainer = Depends(get_container)):
    """Trigger a shadow-only heating evaluation with an injected scenario."""
    _require_session(request, min_role="admin")
    proactive = getattr(container, "proactive_service", None)
    if proactive is None:
        return {"ok": False, "message": "Proactive service not available"}
    if not hasattr(proactive, "run_heating_shadow_force"):
        return {"ok": False, "message": "Shadow force not supported by this proactive version"}
    try:
        records = await proactive.run_heating_shadow_force(scenario=scenario)
        writes = [r for r in records if r["is_write"]]
        reads = [r for r in records if not r["is_write"]]
        return {
            "ok": True,
            "scenario": scenario,
            "total_tool_calls": len(records),
            "write_calls_intercepted": len(writes),
            "read_calls_executed": len(reads),
            "writes": writes,
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# ── Camera Discovery ──────────────────────────────────────────────────────────

@router.get("/camera-discovery")
async def get_camera_discovery(request: Request, container: AppContainer = Depends(get_container)):
    """Return the auto-discovered camera/motion sensor mappings from HA areas."""
    _require_session(request, min_role="admin")
    discovery = getattr(container, "camera_discovery", None)
    if discovery is None:
        return {"discovered": False, "message": "Camera discovery not available or not yet run"}
    proactive = getattr(container, "proactive_service", None)
    return {
        "discovered": discovery.discovered,
        "outdoor_cameras": discovery.outdoor_cameras,
        "camera_areas": discovery.camera_areas,
        "motion_camera_map_discovered": discovery.motion_camera_map,
        "bypass_cameras_discovered": list(discovery.bypass_global_motion_cameras),
        "vision_prompts_discovered": list(discovery.camera_vision_prompts.keys()),
        "active_motion_camera_map": dict(getattr(proactive, "_motion_camera_map", {})) if proactive else {},
        "active_bypass_cameras": list(getattr(proactive, "_bypass_global_motion_cameras", set())) if proactive else [],
    }


@router.post("/camera-discovery/refresh")
async def refresh_camera_discovery(request: Request, container: AppContainer = Depends(get_container)):
    """Re-run camera discovery from HA area registry."""
    _require_session(request, min_role="admin")
    from avatar_backend.services.camera_discovery import CameraDiscoveryService
    from avatar_backend.config import get_settings
    settings = get_settings()
    discovery = CameraDiscoveryService(settings.ha_url, settings.ha_token)
    result = await discovery.discover(timeout_s=15.0)
    if result.discovered:
        container.camera_discovery = result
        proactive = getattr(container, "proactive_service", None)
        if proactive and hasattr(proactive, "apply_discovery"):
            proactive.apply_discovery(result)
    return {
        "discovered": result.discovered,
        "outdoor_cameras": result.outdoor_cameras,
        "motion_camera_map": result.motion_camera_map,
        "bypass_cameras": list(result.bypass_global_motion_cameras),
    }
