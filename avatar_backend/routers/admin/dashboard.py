"""Dashboard sub-router: sessions, memory, avatar settings/library, sync-prompt, conversations, energy, announce/test."""
from __future__ import annotations

import asyncio as _asyncio
import structlog
import re as _re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from avatar_backend.bootstrap.container import AppContainer, get_container

from avatar_backend.services.prompt_bootstrap import (
    extract_known_entity_ids,
    summarise_new_entities,
)
from avatar_backend.runtime_paths import install_dir

from .common import (
    _CONFIG_DIR,
    _INSTALL_DIR,
    _PROMPT_FILE,
    _get_session,
    _require_session,
    MemoryBody,
    AvatarSettings,
    SyncPromptResponse,
    AnnounceBody,
)

_LOGGER = structlog.get_logger()
router = APIRouter()


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request)
    ws_mgr = getattr(container, "ws_manager", None)
    sessions = ws_mgr.list_voice_sessions() if ws_mgr else []
    return {"active_sessions": len(sessions), "sessions": sessions}


@router.delete("/sessions/{session_id}")
async def clear_session(session_id: str, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    await container.conversation_service.clear_session_state(session_id)
    return {"cleared": session_id}


# ── Persistent memory ────────────────────────────────────────────────────────

@router.get("/memory")
async def list_memory(request: Request, n: int = 200, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = container.memory_service
    return {"memories": svc.list_memories(limit=max(1, min(n, 500)))}


@router.post("/memory")
async def create_memory(body: MemoryBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = container.memory_service
    memory = svc.add_memory(
        summary=body.summary,
        category=body.category,
        confidence=body.confidence,
        pinned=body.pinned,
    )
    return {"memory": memory}


@router.delete("/memory")
async def clear_memory(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = container.memory_service
    removed = svc.clear_memories()
    return {"cleared": removed}


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = container.memory_service
    deleted = svc.delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": memory_id}


@router.get("/memory/{memory_id}/usage")
async def get_memory_usage(memory_id: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    db = getattr(container, "metrics_db", None)
    if db is None:
        return {"usage": []}
    return {"usage": db.list_memory_usage(memory_id)}


@router.get("/memory/stale")
async def list_stale_memory(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = container.memory_service
    return {"memories": svc.list_stale_memories(limit=200)}


@router.post("/memory/{memory_id}/mark-stale")
async def mark_memory_stale(memory_id: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = container.memory_service
    ok = svc.mark_stale(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"ok": True, "memory_id": memory_id}


@router.post("/memory/{memory_id}/restore")
async def restore_memory(memory_id: int, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = container.memory_service
    ok = svc.restore_memory(memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"ok": True, "memory_id": memory_id}


# ── Avatar settings ───────────────────────────────────────────────────────────

_AVATAR_SETTINGS_FILE = _CONFIG_DIR / "avatar_settings.json"


@router.get("/avatar-settings")
async def get_avatar_settings(request: Request):
    # Accept session cookie (admin panel) OR API key header (avatar/kiosk page)
    if not _get_session(request):
        import secrets as _sec
        from avatar_backend.config import get_settings as _gs
        key = request.headers.get("X-API-Key") or request.query_params.get("api_key", "")
        if not key or not _sec.compare_digest(key.encode(), _gs().api_key.encode()):
            raise HTTPException(status_code=401, detail="Not authenticated")
    import json as _json
    if _AVATAR_SETTINGS_FILE.exists():
        return _json.loads(_AVATAR_SETTINGS_FILE.read_text())
    return {"skin_tone": -1, "hair_color": -1, "avatar_url": ""}


@router.post("/avatar-settings")
async def save_avatar_settings(body: AvatarSettings, request: Request):
    _require_session(request, min_role="admin")
    import json as _json
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AVATAR_SETTINGS_FILE.write_text(_json.dumps(body.model_dump()))
    _LOGGER.info("admin.avatar_settings_saved", skin_tone=body.skin_tone)
    return {"saved": True}


# ── Avatar library ───────────────────────────────────────────────────────────

_AVATARS_DIR = _INSTALL_DIR / "static" / "avatars"
_AVATAR_MAX_BYTES = 100 * 1024 * 1024  # 100 MB

@router.get("/avatars")
async def list_avatars(request: Request):
    """Return all GLB filenames available in static/avatars/."""
    _require_session(request, min_role="viewer")
    files = sorted(p.name for p in _AVATARS_DIR.glob("*.glb")) if _AVATARS_DIR.exists() else []
    return {"avatars": files}


@router.post("/avatars/upload")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    """Upload a new GLB avatar (admin only, 50 MB max)."""
    _require_session(request, min_role="admin")
    if not (file.filename or "").lower().endswith(".glb"):
        raise HTTPException(status_code=400, detail="Only .glb files are accepted.")
    safe = _re.sub(r"[^a-zA-Z0-9._-]", "_", file.filename or "avatar.glb")
    if not safe.lower().endswith(".glb"):
        safe += ".glb"
    content = await file.read()
    if len(content) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 100 MB).")
    _AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    dest = _AVATARS_DIR / safe
    dest.write_bytes(content)

    # Auto-fix: add missing ARKit blendshapes, fix skeleton for TalkingHead
    fix_result = {"fixed": False, "actions": [], "error": None}
    try:
        from avatar_backend.services.avatar_fixer import fix_avatar
        fix_result = await _asyncio.get_event_loop().run_in_executor(
            None, fix_avatar, str(dest), str(dest),
        )
        if fix_result.get("error"):
            _LOGGER.warning("admin.avatar_fix_error", error=fix_result["error"])
        elif fix_result.get("fixed"):
            _LOGGER.info("admin.avatar_fixed", actions=fix_result["actions"])
    except Exception as exc:
        _LOGGER.warning("admin.avatar_fix_failed", exc=str(exc)[:100])
        fix_result["error"] = str(exc)

    _LOGGER.info("admin.avatar_uploaded", filename=safe, bytes=len(content))
    return {"uploaded": safe, "fix": fix_result}


@router.delete("/avatars/{filename}")
async def delete_avatar(filename: str, request: Request):
    """Delete a GLB avatar file (admin only). Cannot delete brunette.glb."""
    _require_session(request, min_role="admin")
    if not _re.match(r"^[a-zA-Z0-9._-]+\.glb$", filename):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if filename == "brunette.glb":
        raise HTTPException(status_code=403, detail="Cannot delete the default avatar.")
    dest = _AVATARS_DIR / filename
    if not dest.exists():
        raise HTTPException(status_code=404, detail="Avatar not found.")
    dest.unlink()
    _LOGGER.info("admin.avatar_deleted", filename=filename)
    return {"deleted": filename}


# ── Prompt sync ───────────────────────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel


class ApplySyncBody(_BaseModel):
    entity_ids: list[str]
    area_overrides: dict[str, str] = {}  # entity_id → area name override


@router.get("/sync-prompt/preview")
async def sync_prompt_preview(request: Request, container: AppContainer = Depends(get_container)):
    """
    Discover new HA entities not yet in the system prompt.
    Returns structured entity list enriched with area + state — no LLM call, no prompt changes.
    """
    _require_session(request, min_role="admin")
    import httpx as _httpx
    from avatar_backend.services.prompt_bootstrap import fetch_area_mapping, discover_new_entities

    ha = container.ha_proxy
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{ha._ha_url}/api/states",
                headers={"Authorization": ha._headers["Authorization"]},
            )
            resp.raise_for_status()
            all_states: list[dict] = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not fetch HA states: {exc}")

    current_prompt = _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""
    known = extract_known_entity_ids(current_prompt)
    area_by_entity = await fetch_area_mapping(ha._ha_url, ha._headers["Authorization"].split(" ", 1)[1])
    entities = discover_new_entities(all_states, known, area_by_entity)

    # Collect unique area names for dropdown
    all_areas = sorted({e.area for e in entities if e.area})

    return {
        "total": len(entities),
        "available_areas": all_areas,
        "entities": [
            {
                "entity_id":    e.entity_id,
                "friendly_name": e.friendly_name,
                "domain":       e.domain,
                "state":        e.state,
                "device_class": e.device_class,
                "unit":         e.unit,
                "area":         e.area,
                "group":        e.group,
            }
            for e in entities
        ],
    }


@router.post("/sync-prompt/apply")
async def sync_prompt_apply(body: ApplySyncBody, request: Request, container: AppContainer = Depends(get_container)):
    """
    Integrate a user-selected subset of new entities into the system prompt via LLM.
    """
    _require_session(request, min_role="admin")
    import httpx as _httpx
    from avatar_backend.services.prompt_bootstrap import fetch_area_mapping, discover_new_entities

    if not body.entity_ids:
        raise HTTPException(status_code=400, detail="No entity IDs provided.")

    ha  = container.ha_proxy
    llm = container.llm_service

    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{ha._ha_url}/api/states",
                headers={"Authorization": ha._headers["Authorization"]},
            )
            resp.raise_for_status()
            all_states: list[dict] = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not fetch HA states: {exc}")

    # Filter states to only selected entity IDs
    selected_ids = set(body.entity_ids)
    selected_states = [s for s in all_states if s.get("entity_id") in selected_ids]

    current_prompt = _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""
    known = set()  # treat all selected as new regardless of known state
    area_by_entity = await fetch_area_mapping(ha._ha_url, ha._headers["Authorization"].split(" ", 1)[1])
    area_by_entity.update(body.area_overrides)  # user overrides take precedence
    new_summary = summarise_new_entities(selected_states, known, area_by_entity=area_by_entity)

    if not new_summary:
        return SyncPromptResponse(status="ok", new_entities_found=0,
                                  prompt_updated=False,
                                  summary="No valid entities to integrate.")

    new_count = len(body.entity_ids)
    integration_request = (
        "You are updating the system prompt for Nova, an AI home automation controller.\n\n"
        "Here is the current system prompt:\n```\n" + current_prompt + "\n```\n\n"
        "The following Home Assistant entities should be added. Each line shows:\n"
        "  entity_id | friendly name | current state [device_class] — Area\n\n"
        + new_summary + "\n\n"
        "Instructions:\n"
        "- Add each entity to the most appropriate existing section, guided by its Area and group.\n"
        "- If an Area section exists in the prompt, place the entity there.\n"
        "- Skip adding if the entity is clearly infrastructure noise (connectivity, cloud connection).\n"
        "- Preserve the exact structure, tone, and formatting of the original prompt.\n"
        "- Return ONLY the complete updated system prompt — no explanation, no markdown fences."
    )

    try:
        # Prefer Gemini for large prompt rewriting tasks
        _op = getattr(llm, "_operational_backend", None)
        if _op and hasattr(_op, "generate_text"):
            updated_prompt = await _op.generate_text(integration_request, timeout_s=240.0)
        else:
            updated_prompt = await llm.generate_text(integration_request, timeout_s=240.0)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"LLM call failed: {exc}")

    if not updated_prompt or len(updated_prompt) < len(current_prompt) // 2:
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly short response.")
    if len(updated_prompt) > len(current_prompt) * 3:
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly long response.")

    updated_prompt = "".join(c for c in updated_prompt if c >= " " or c in "\n\r\t")

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if _PROMPT_FILE.exists():
        _backup = _CONFIG_DIR / "system_prompt.txt.bak"
        _backup.write_text(_PROMPT_FILE.read_text())
    _PROMPT_FILE.write_text(updated_prompt)

    from avatar_backend.services.session_manager import SessionManager
    container.session_manager = SessionManager(updated_prompt)
    proactive = getattr(container, "proactive_service", None)
    if proactive is not None:
        proactive.update_system_prompt(updated_prompt)

    _LOGGER.info("sync_prompt.apply_done", selected=new_count)
    return SyncPromptResponse(status="ok", new_entities_found=new_count,
                               prompt_updated=True,
                               summary=f"Integrated {new_count} selected entities into the system prompt.")


@router.post("/sync-prompt", response_model=SyncPromptResponse)
async def sync_prompt_legacy(request: Request, container: AppContainer = Depends(get_container)):
    """Legacy full-auto sync — now area-aware. Prefer /preview + /apply for manual syncs."""
    _require_session(request, min_role="admin")
    import httpx as _httpx

    ha  = container.ha_proxy
    llm = container.llm_service

    _LOGGER.info("sync_prompt.started")
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{ha._ha_url}/api/states",
                headers={"Authorization": ha._headers["Authorization"]},
            )
            resp.raise_for_status()
            all_states: list[dict] = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not fetch HA states: {exc}")

    current_prompt = _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""
    known          = extract_known_entity_ids(current_prompt)
    from avatar_backend.services.prompt_bootstrap import fetch_area_mapping
    area_by_entity = await fetch_area_mapping(ha._ha_url, ha._headers["Authorization"].split(" ", 1)[1])
    new_summary    = summarise_new_entities(all_states, known, area_by_entity=area_by_entity)

    if not new_summary:
        return SyncPromptResponse(status="ok", new_entities_found=0,
                                  prompt_updated=False,
                                  summary="No new entities found — system prompt is up to date.")

    new_count = new_summary.count("\n  ")
    integration_request = (
        "You are updating the system prompt for Nova, an AI home automation controller.\n\n"
        "Here is the current system prompt:\n```\n" + current_prompt + "\n```\n\n"
        "The following new Home Assistant entities have been discovered. Each line shows:\n"
        "  entity_id | friendly name | current state [device_class] — Area\n\n"
        + new_summary + "\n\n"
        "Instructions:\n"
        "- Add each entity to the most appropriate existing section, guided by its Area.\n"
        "- Skip clear infrastructure noise (connectivity, cloud connection sensors).\n"
        "- Preserve the exact structure, tone, and formatting of the original prompt.\n"
        "- Return ONLY the complete updated system prompt — no explanation, no markdown fences."
    )

    try:
        # Prefer Gemini for large prompt rewriting tasks
        _op = getattr(llm, "_operational_backend", None)
        if _op and hasattr(_op, "generate_text"):
            updated_prompt = await _op.generate_text(integration_request, timeout_s=240.0)
        else:
            updated_prompt = await llm.generate_text(integration_request, timeout_s=240.0)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"LLM call failed: {exc}")

    if not updated_prompt or len(updated_prompt) < len(current_prompt) // 2:
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly short response.")
    if len(updated_prompt) > len(current_prompt) * 3:
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly long response — possible prompt injection. Prompt not saved.")

    updated_prompt = "".join(c for c in updated_prompt if c >= " " or c in "\n\r\t")

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if _PROMPT_FILE.exists():
        _backup = _CONFIG_DIR / "system_prompt.txt.bak"
        _backup.write_text(_PROMPT_FILE.read_text())
        _LOGGER.info("sync_prompt.backup_saved", path=str(_backup))
    _PROMPT_FILE.write_text(updated_prompt)

    from avatar_backend.services.session_manager import SessionManager
    container.session_manager = SessionManager(updated_prompt)

    proactive = getattr(container, "proactive_service", None)
    if proactive is not None:
        proactive.update_system_prompt(updated_prompt)

    return SyncPromptResponse(status="ok", new_entities_found=new_count,
                               prompt_updated=True,
                               summary=f"Integrated {new_count} new entities into the system prompt.")


# ── Conversations ─────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(request: Request, limit: int = 100, session_id: str | None = None, container: AppContainer = Depends(get_container)):
    """Return recent conversation audit records."""
    _require_session(request, min_role="viewer")
    db = getattr(container, "metrics_db", None)
    if not db:
        return {"conversations": []}
    return {"conversations": db.list_conversation_audits(limit=limit, session_id=session_id)}


@router.get("/conversations/{session_id}")
async def get_conversation_by_session(request: Request, session_id: str, container: AppContainer = Depends(get_container)):
    """Return all audit records for a specific session."""
    _require_session(request, min_role="viewer")
    db = getattr(container, "metrics_db", None)
    if not db:
        return {"conversations": []}
    return {"conversations": db.list_conversation_audits(limit=500, session_id=session_id)}


# ── Energy ────────────────────────────────────────────────────────────────────

@router.get("/energy/summary")
async def energy_summary(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "energy_service", None)
    if not svc:
        return {"summary": {}}
    return {"summary": await svc.get_summary()}


@router.get("/energy/devices")
async def energy_devices(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "energy_service", None)
    if not svc:
        return {"devices": []}
    return {"devices": await svc.get_device_breakdown()}


# ── Test announce ─────────────────────────────────────────────────────────────

@router.post("/announce/test")
async def test_announce(body: AnnounceBody, request: Request):
    _require_session(request, min_role="admin")
    from avatar_backend.routers.announce import AnnounceRequest, announce_handler
    return await announce_handler(
        AnnounceRequest(message=body.message, priority=body.priority),  # type: ignore[arg-type]
        request,
        container=request.app.state._container,
    )

# ── Face Recognition ──────────────────────────────────────────────────────────

@router.get("/faces/unknown")
async def get_unknown_faces(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "face_service", None)
    if not svc or not svc.available:
        return {"faces": [], "available": False}
    return {"faces": svc.get_unknown_faces(), "available": True}


@router.get("/faces/known")
async def get_known_faces(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "face_service", None)
    if not svc or not svc.available:
        return {"faces": [], "available": False}
    return {"faces": await svc.list_known_faces(), "available": True}


@router.post("/faces/register")
async def register_face(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = getattr(container, "face_service", None)
    if not svc or not svc.available:
        return JSONResponse({"ok": False, "error": "Face recognition not configured"}, status_code=503)
    body = await request.json()
    face_id = body.get("face_id", "")
    name = body.get("name", "").strip()
    if not face_id or not name:
        return JSONResponse({"ok": False, "error": "face_id and name required"}, status_code=400)
    image_bytes = svc.get_unknown_face_bytes(face_id)
    if not image_bytes:
        return JSONResponse({"ok": False, "error": "Face not found in queue"}, status_code=404)
    # DeepFace preprocessing on queue crops too
    df_svc = getattr(container, "deepface_service", None)
    if df_svc and getattr(df_svc, "_preprocess_training", False):
        preprocessed = await asyncio.get_event_loop().run_in_executor(
            None, df_svc.preprocess_for_training, image_bytes
        )
        if preprocessed:
            image_bytes = preprocessed
    ok = await svc.register_face(name, image_bytes)
    if ok:
        svc.remove_unknown(face_id)
    return {"ok": ok, "name": name}



@router.get("/faces/photo/{name}")
async def get_face_photo(name: str, request: Request, container: AppContainer = Depends(get_container)):
    """Serve a cached face thumbnail for the scoreboard widget."""
    svc = getattr(container, "face_service", None)
    if not svc:
        return Response(status_code=404)
    photo = svc.get_face_photo(name)
    if not photo:
        return Response(status_code=404)
    return Response(content=photo, media_type="image/jpeg")


@router.delete("/faces/unknown/{face_id}")
async def dismiss_unknown_face(face_id: str, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = getattr(container, "face_service", None)
    if svc:
        svc.remove_unknown(face_id)
    return {"ok": True}


@router.delete("/faces/known/{name}")
async def delete_known_face(name: str, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = getattr(container, "face_service", None)
    if not svc or not svc.available:
        return JSONResponse({"ok": False, "error": "Not configured"}, status_code=503)
    ok = await svc.delete_face(name)
    return {"ok": ok}


@router.post("/faces/train")
async def train_face(request: Request, container: AppContainer = Depends(get_container),
                     name: str = Form(...), image: UploadFile = File(...)):
    """Register a new face directly from an uploaded image — no detection queue needed."""
    _require_session(request, min_role="admin")
    svc = getattr(container, "face_service", None)
    if not svc or not svc.available:
        return JSONResponse({"ok": False, "error": "Face recognition not configured"}, status_code=503)
    clean_name = name.strip().lower()
    if not clean_name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    image_bytes = await image.read()
    if not image_bytes:
        return JSONResponse({"ok": False, "error": "image is empty"}, status_code=400)
    # DeepFace preprocessing: align + crop before CPAI registration
    df_svc = getattr(container, "deepface_service", None)
    if df_svc and getattr(df_svc, "_preprocess_training", False):
        preprocessed = await asyncio.get_event_loop().run_in_executor(
            None, df_svc.preprocess_for_training, image_bytes
        )
        if preprocessed:
            image_bytes = preprocessed
        else:
            return JSONResponse({"ok": False, "error": "DeepFace could not detect a face in the image. Try a clearer photo or disable DeepFace preprocessing."}, status_code=422)
    ok = await svc.register_face(clean_name, image_bytes)
    return {"ok": ok, "name": clean_name}
