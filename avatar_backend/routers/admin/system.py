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


def _sync_gemini_pool_env(pool, primary_key: str) -> None:
    primary_enabled, pool_value = serialize_pool_for_env(pool, primary_key)
    if primary_enabled is not None:
        _update_env_value("GOOGLE_API_KEY_ENABLED", primary_enabled)
    _update_env_value("GEMINI_API_KEYS", pool_value)
    _update_env_value("GEMINI_CAMERA_PINS", serialize_pins_for_env(pool))

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


# ── Music ─────────────────────────────────────────────────────────────────────

@router.get("/music/players")
async def music_players(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"players": []}
    return {"players": await svc.get_players()}


@router.get("/music/now-playing")
async def music_now_playing(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"players": []}
    return {"players": await svc.get_now_playing()}


@router.post("/music/control")
async def music_control(body: MusicControlBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = getattr(container, "music_service", None)
    if not svc:
        raise HTTPException(status_code=503, detail="Music service not available")
    action_map = {
        "play": lambda: svc.play_media(body.entity_id, str(body.value), "music") if body.value else svc.play(body.entity_id),
        "pause": lambda: svc.pause(body.entity_id),
        "stop": lambda: svc.stop(body.entity_id),
        "next": lambda: svc.next_track(body.entity_id),
        "previous": lambda: svc.previous_track(body.entity_id),
        "volume": lambda: svc.set_volume(body.entity_id, float(body.value or 0.5)),
        "mute": lambda: svc.mute(body.entity_id, True),
        "unmute": lambda: svc.mute(body.entity_id, False),
    }
    fn = action_map.get(body.action)
    if not fn:
        raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")
    return await fn()


@router.get("/music/search")
async def music_search(request: Request, q: str = "", media_type: str = "track", limit: int = 10, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"results": []}
    return {"results": await svc.search(q, media_type, limit)}


@router.get("/music/status")
async def music_assistant_status(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "music_service", None)
    if not svc:
        return {"available": False, "configured": False}
    return {
        "configured": svc.music_assistant_available,
        "available": await svc.check_music_assistant() if svc.music_assistant_available else False,
    }


# ── Music Assistant UI proxy ─────────────────────────────────────────────────

@router.api_route("/music-ui/{path:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def music_assistant_proxy(request: Request, path: str = ""):
    """Reverse proxy to Music Assistant, stripping X-Frame-Options for iframe embedding."""
    sess = _get_session(request)
    if not sess:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    url = f"{_MUSIC_ASSISTANT_BASE}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    try:
        resp = await _http_client().request(
            method=request.method,
            url=url,
            content=await request.body(),
            follow_redirects=True,
            timeout=15.0,
        )
        headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("x-frame-options", "content-security-policy", "transfer-encoding")}
        return RawResponse(content=resp.content, status_code=resp.status_code, headers=headers)
    except httpx.ConnectError:
        return JSONResponse({"error": "Music Assistant is not running"}, status_code=503)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=502)


# ── nova-selfheal proxy ────────────────────────────────────────────────────────

@router.api_route("/selfheal/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def selfheal_proxy(path: str, request: Request):
    _require_session(request, min_role="viewer")
    url = f"{_SELFHEAL_BASE}/{path}"
    try:
        resp = await _http_client().request(
            method=request.method,
            url=url,
            content=await request.body(),
            headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
            timeout=10.0,
        )
        try:
            content = resp.json()
        except Exception:
            content = {"error": resp.text[:300]}
        return JSONResponse(content=content, status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse({"error": "nova-selfheal is not running"}, status_code=503)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@router.post("/selfheal-restart")
async def selfheal_restart(request: Request):
    _require_session(request, min_role="admin")
    import subprocess as _sp
    result = _sp.run(
        ["sudo", "systemctl", "restart", "nova-selfheal"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0:
        return {"ok": True}
    return JSONResponse({"error": result.stderr[:200]}, status_code=500)


@router.post("/selfheal-test")
async def selfheal_test(request: Request):
    """Inject a fake ERROR log entry to test nova-selfheal pipeline."""
    _require_session(request, min_role="admin")
    import structlog as _sl
    _log = _sl.get_logger("avatar_backend.services.ha_proxy")
    _log.error(
        "test.error",
        exc_type="TestError",
        exc="selfheal test injection",
        logger="avatar_backend.services.ha_proxy",
    )
    return {"ok": True, "message": "Test error logged — check Telegram and Self-Heal tab."}


# ── Gemini Key Pool ───────────────────────────────────────────────────────────

@router.get("/gemini-pool")
async def get_gemini_pool(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    pool = container.gemini_key_pool
    if not pool:
        return {"keys": [], "stats": {"pool_size": 0}}
    return {"keys": pool.get_status(), "stats": pool.get_stats()}


@router.post("/gemini-pool/add")
async def add_gemini_key(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    key = (body.get("key") or "").strip()
    label = (body.get("label") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key is required")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    pool.add_key(key, label)
    # Sync to .env
    from avatar_backend.config import get_settings
    primary = get_settings().google_api_key
    _sync_gemini_pool_env(pool, primary)
    return {"ok": True, "pool_size": pool.size}


@router.delete("/gemini-pool/{index}")
async def remove_gemini_key(index: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    ok = pool.remove_key(index)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    # Sync to .env
    from avatar_backend.config import get_settings
    primary = get_settings().google_api_key
    _sync_gemini_pool_env(pool, primary)
    return {"ok": True, "pool_size": pool.size}


@router.post("/gemini-pool/toggle")
async def toggle_gemini_key(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    index = body.get("index")
    enabled = body.get("enabled", True)
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    
    if index is None: raise HTTPException(status_code=400, detail="Missing index")
    
    ok = pool.toggle_key(index, enabled)
    if not ok: raise HTTPException(status_code=404, detail="Key not found")
    
    # Sync to .env
    from avatar_backend.config import get_settings
    primary = get_settings().google_api_key
    _sync_gemini_pool_env(pool, primary)
    return {"ok": True, "enabled": enabled}

@router.post("/gemini-pool/pin")
async def pin_camera_to_key(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    key_index = body.get("key_index")
    camera_id = (body.get("camera_id") or "").strip()
    if key_index is None or not camera_id:
        raise HTTPException(status_code=400, detail="key_index and camera_id required")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    pool.pin_camera(int(key_index), camera_id)
    from avatar_backend.config import get_settings
    primary = get_settings().google_api_key
    _sync_gemini_pool_env(pool, primary)
    return {"ok": True}


@router.post("/gemini-pool/unpin")
async def unpin_camera(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    camera_id = (body.get("camera_id") or "").strip()
    if not camera_id:
        raise HTTPException(status_code=400, detail="camera_id required")
    pool = container.gemini_key_pool
    if not pool:
        raise HTTPException(status_code=503, detail="Key pool not initialized")
    pool.unpin_camera(camera_id)
    from avatar_backend.config import get_settings
    primary = get_settings().google_api_key
    _sync_gemini_pool_env(pool, primary)
    return {"ok": True}


# ── Vision Camera Selection ───────────────────────────────────────────────────

@router.get("/vision-cameras")
async def get_vision_cameras(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config
    runtime = load_home_runtime_config()
    all_cameras = dict(runtime.camera_labels)
    # Also include cameras from motion map
    for cam in set(runtime.motion_camera_map.values()):
        if cam not in all_cameras:
            all_cameras[cam] = cam.replace("camera.", "").replace("_", " ").title()
    enabled = set(runtime.vision_enabled_cameras)
    cameras = [{"entity_id": k, "label": v, "vision_enabled": k in enabled or not enabled} for k, v in sorted(all_cameras.items())]
    return {"cameras": cameras}


@router.get("/rooms")
async def get_rooms(request: Request, container: AppContainer = Depends(get_container)):
    """Return configured avatar rooms and which are currently connected."""
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config, _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    # Attach live connection status from ws_manager
    ws_mgr = getattr(container, "ws_manager", None)
    connected_rooms: set[str] = set()
    if ws_mgr is not None:
        for sess in ws_mgr.list_voice_sessions():
            if sess.get("room_id"):
                connected_rooms.add(sess["room_id"])
    from avatar_backend.config import get_settings as _gs
    _s = _gs()
    public_url = (_s.public_url or "").rstrip("/")
    ak = _s.api_key
    return {
        "rooms": [
            {**r, "connected": r.get("id", "") in connected_rooms,
             "avatar_url": f"{public_url}/avatar?room={r['id']}&api_key={ak}" if public_url else f"/avatar?room={r['id']}&api_key={ak}",
             "local_url": f"http://192.168.0.249:8001/avatar?room={r['id']}&api_key={ak}",
             "glb_url": (f"{public_url}/static/avatars/{r['glb']}" if r.get("glb") else None)}
            for r in rooms
        ]
    }


@router.post("/rooms")
async def add_room(request: Request, container: AppContainer = Depends(get_container)):
    """Add a new room to the avatar_rooms list."""
    _require_session(request, min_role="admin")
    body = await request.json()
    label = str(body.get("label") or "").strip()
    room_id = str(body.get("id") or "").strip().lower().replace(" ", "_")
    import re as _re
    room_id = _re.sub(r"[^a-z0-9_]", "", room_id)
    if not label or not room_id:
        from fastapi import HTTPException
        raise HTTPException(400, "label and id are required")
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    if any(r.get("id") == room_id for r in rooms):
        from fastapi import HTTPException
        raise HTTPException(409, f"Room '{room_id}' already exists")
    glb = str(body.get("glb") or "").strip() or None
    entry: dict = {"id": room_id, "label": label}
    if glb:
        entry["glb"] = glb
    rooms.append(entry)
    raw["avatar_rooms"] = rooms
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "room": entry}


@router.patch("/rooms/{room_id}")
async def update_room(room_id: str, request: Request, container: AppContainer = Depends(get_container)):
    """Update a room's label or glb assignment."""
    _require_session(request, min_role="admin")
    body = await request.json()
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    updated = False
    for r in rooms:
        if r.get("id") == room_id:
            if "label" in body:
                r["label"] = str(body["label"]).strip()
            if "glb" in body:
                r["glb"] = str(body["glb"]).strip() or None
                if not r["glb"]:
                    r.pop("glb", None)
            updated = True
            break
    if not updated:
        from fastapi import HTTPException
        raise HTTPException(404, f"Room '{room_id}' not found")
    raw["avatar_rooms"] = rooms
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True}


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: str, request: Request, container: AppContainer = Depends(get_container)):
    """Remove a room from the avatar_rooms list."""
    _require_session(request, min_role="admin")
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    rooms = raw.get("avatar_rooms", [])
    rooms = [r for r in rooms if r.get("id") != room_id]
    raw["avatar_rooms"] = rooms
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True}


@router.post("/vision-cameras")
async def save_vision_cameras(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    enabled = body.get("enabled", [])
    from avatar_backend.services.home_runtime import load_home_runtime_config, _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    raw["vision_enabled_cameras"] = enabled
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "enabled": len(enabled)}


@router.get("/gemini-operational-tasks")
async def get_gemini_operational_tasks(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config
    rt = load_home_runtime_config()
    all_tasks = ["ha_power_alert", "ha_door_check", "ha_bedtime_house_check"]
    enabled = set(rt.gemini_operational_tasks)
    return {"tasks": [{"id": t, "label": t.replace("ha_", "").replace("_", " ").title(), "use_gemini": t in enabled} for t in all_tasks]}


@router.post("/gemini-operational-tasks")
async def save_gemini_operational_tasks(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    enabled = body.get("enabled", [])
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    raw["gemini_operational_tasks"] = enabled
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "enabled": enabled}


@router.get("/gemini-chat-toggle")
async def get_gemini_chat_toggle(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    from avatar_backend.services.home_runtime import load_home_runtime_config
    return {"use_gemini_chat": load_home_runtime_config().use_gemini_chat}


@router.post("/gemini-chat-toggle")
async def set_gemini_chat_toggle(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    body = await request.json()
    from avatar_backend.services.home_runtime import _RUNTIME_FILE
    import json as _json
    raw = _json.loads(_RUNTIME_FILE.read_text()) if _RUNTIME_FILE.exists() else {}
    raw["use_gemini_chat"] = bool(body.get("enabled", False))
    _RUNTIME_FILE.write_text(_json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "use_gemini_chat": raw["use_gemini_chat"]}
