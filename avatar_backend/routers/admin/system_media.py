"""System sub-router: music, Music Assistant UI, selfheal, Gemini key pool, vision cameras, rooms."""
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

_MUSIC_ASSISTANT_BASE = "http://localhost:8095"
_SELFHEAL_BASE = "http://localhost:7779"

def _sync_gemini_pool_env(pool, primary_key: str) -> None:
    primary_enabled, pool_value = serialize_pool_for_env(pool, primary_key)
    if primary_enabled is not None:
        _update_env_value("GOOGLE_API_KEY_ENABLED", primary_enabled)
    _update_env_value("GEMINI_API_KEYS", pool_value)
    _update_env_value("GEMINI_CAMERA_PINS", serialize_pins_for_env(pool))


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
