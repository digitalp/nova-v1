"""
Dashboard sub-router: conversations, energy summary, test announce, face recognition.
"""
from __future__ import annotations
import asyncio

import io
import os
import time
import tempfile
from typing import Any

import httpx
import structlog

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.services.deepface_service import DeepFaceService
from avatar_backend.config import get_settings

from .common import _require_session, AnnounceBody

_LOGGER = structlog.get_logger()
router = APIRouter()

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
