"""
Admin panel — /admin

Session-based authentication (username + password) replaces the API key gate.
All browser sessions are tracked via an HTTP-only cookie (nova_session).

Roles
-----
admin  — full access: config, prompt, ACL, restart, user management
viewer — read-only: dashboard, logs, sessions
"""
from __future__ import annotations
import asyncio
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
import httpx
import structlog
import re as _re
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from avatar_backend.services.action_service import ActionService
from avatar_backend.services.open_loop_service import OpenLoopService
from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService
from avatar_backend.services.prompt_bootstrap import (
    extract_known_entity_ids,
    summarise_new_entities,
)
from avatar_backend.runtime_paths import config_dir, env_file, install_dir, logs_dir, static_dir

_LOGGER = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])

_INSTALL_DIR  = install_dir()
_CONFIG_DIR   = config_dir()
_ENV_FILE     = env_file()
_PROMPT_FILE  = _CONFIG_DIR / "system_prompt.txt"
_ACL_FILE     = _CONFIG_DIR / "acl.yaml"
_LOG_FILE     = logs_dir() / "avatar-backend.log"
_STATIC_DIR   = static_dir()
_COOKIE_NAME  = "nova_session"
_OPEN_LOOP_SERVICE = OpenLoopService()
_RESTART_KIOSK_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# Fields shown in the config editor (display label, sensitive flag)
_CONFIG_FIELDS = {
    "API_KEY":              ("API Key",                                      True),
    "HA_URL":               ("Home Assistant URL",                           False),
    "HA_TOKEN":             ("HA Long-lived Token",                          True),
    "LLM_PROVIDER":         ("LLM Provider (ollama/openai/google/anthropic)", False),
    "OLLAMA_URL":           ("Ollama URL",                                   False),
    "OLLAMA_MODEL":         ("Ollama Model",                                 False),
    "CLOUD_MODEL":          ("Cloud Model Name",                             False),
    "OPENAI_API_KEY":       ("OpenAI API Key",                               True),
    "GOOGLE_API_KEY":       ("Google API Key",                               True),
    "ANTHROPIC_API_KEY":    ("Anthropic API Key",                            True),
    "WHISPER_MODEL":        ("Whisper Model",                                False),
    "TTS_PROVIDER":         ("TTS Provider",                                 False),
    "PIPER_VOICE":          ("Piper Voice",                                  False),
    "ELEVENLABS_API_KEY":   ("ElevenLabs API Key",                           True),
    "ELEVENLABS_VOICE_ID":  ("ElevenLabs Voice ID",                          False),
    "ELEVENLABS_MODEL":     ("ElevenLabs Model",                             False),
    "AFROTTS_VOICE":        ("AfroTTS Voice",                                False),
    "AFROTTS_SPEED":        ("AfroTTS Speed (0.5-2.0)",                       False),
    "PUBLIC_URL":           ("Server Public URL (for audio playback)",       False),
    "CORS_ORIGINS":         ("Allowed CORS Origins (comma-separated URLs)",  False),
    "SPEAKERS":             ("Speakers",                                     False),
    "TTS_ENGINE":           ("TTS Engine (Sonos)",                           False),
    "SPEAKER_AUDIO_OFFSET_MS": ("Speaker Audio Delay ms (delay browser audio to sync with room speakers, 0 = off)", False),
    "MOTION_CLIP_DURATION_S": ("Motion Clip Duration Seconds",               False),
    "MOTION_CLIP_SEARCH_CANDIDATES": ("Motion Search Candidate Window",      False),
    "MOTION_CLIP_SEARCH_RESULTS": ("Motion Search Max Results",              False),
    "LOG_LEVEL":            ("Log Level",                                    False),
    "HOST":                 ("Bind Host",                                    False),
    "PORT":                 ("Bind Port",                                    False),
}


# ── Session helpers ───────────────────────────────────────────────────────────

def _get_session(request: Request) -> dict | None:
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return None
    users: "UserService" = request.app.state.user_service
    return users.validate_session(token)


def _require_session(request: Request, min_role: Literal["admin", "viewer"] = "viewer") -> dict:
    """Return the session or raise 401/403. Used inline (not as Depends)."""
    sess = _get_session(request)
    if not sess:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if min_role == "admin" and sess["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return sess


def _set_session_cookie(response: JSONResponse | RedirectResponse, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        max_age=86400,
        path="/admin",
    )


# ── Login / logout / setup ────────────────────────────────────────────────────

@router.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse(str(_STATIC_DIR / "login.html"))


@router.get("/setup-required", include_in_schema=False)
async def setup_required(request: Request):
    return {"required": not request.app.state.user_service.has_users()}


@router.post("/setup", include_in_schema=False)
async def first_run_setup(request: Request):
    """Create the very first admin account. Only works when no users exist."""
    from avatar_backend.middleware.ratelimit import is_rate_limited, record_failure
    client_ip = request.client.host if request.client else "unknown"
    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    users = request.app.state.user_service
    if users.has_users():
        raise HTTPException(status_code=409, detail="Setup already complete")
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username:
        record_failure(client_ip)
        raise HTTPException(status_code=400, detail="Username is required")
    try:
        users.create_user(username, password, "admin")
    except ValueError as exc:
        record_failure(client_ip)
        raise HTTPException(status_code=400, detail=str(exc))
    _LOGGER.info("admin.setup_complete", username=username)
    return {"created": True}


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def do_login(body: LoginBody, request: Request):
    from avatar_backend.middleware.ratelimit import is_rate_limited, record_failure, clear_failures
    client_ip = request.client.host if request.client else "unknown"
    if is_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    users = request.app.state.user_service
    user  = users.authenticate(body.username, body.password)
    if not user:
        record_failure(client_ip)
        _LOGGER.warning("admin.login_failed", username=body.username, client=client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    clear_failures(client_ip)
    token = users.create_session(user["username"], user["role"])
    _LOGGER.info("admin.login_ok", username=user["username"], role=user["role"])
    resp  = JSONResponse({"ok": True, "role": user["role"]})
    _set_session_cookie(resp, token)
    return resp


@router.post("/logout")
async def do_logout(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if token:
        request.app.state.user_service.invalidate_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_NAME, path="/admin")
    return resp


@router.get("/me")
async def get_me(request: Request):
    sess = _get_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": sess["username"], "role": sess["role"]}


# ── Admin page ────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def admin_page(request: Request):
    if not _get_session(request):
        return RedirectResponse("/admin/login")
    return FileResponse(str(_STATIC_DIR / "admin.html"))


# ── User management (admin only) ──────────────────────────────────────────────

class CreateUserBody(BaseModel):
    username: str
    password: str
    role:     Literal["admin", "viewer"] = "viewer"


class ChangePasswordBody(BaseModel):
    new_password: str


class ChangeRoleBody(BaseModel):
    role: Literal["admin", "viewer"]


@router.get("/users")
async def list_users(request: Request):
    _require_session(request, min_role="admin")
    return {"users": request.app.state.user_service.list_users()}


@router.post("/users", status_code=201)
async def create_user(body: CreateUserBody, request: Request):
    _require_session(request, min_role="admin")
    try:
        request.app.state.user_service.create_user(body.username, body.password, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"created": body.username}


@router.delete("/users/{username}")
async def delete_user(username: str, request: Request):
    sess = _require_session(request, min_role="admin")
    if username == sess["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    try:
        request.app.state.user_service.delete_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": username}


@router.post("/users/{username}/password")
async def change_user_password(username: str, body: ChangePasswordBody, request: Request):
    _require_session(request, min_role="admin")
    try:
        request.app.state.user_service.change_password(username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"updated": username}


@router.post("/users/{username}/role")
async def change_user_role(username: str, body: ChangeRoleBody, request: Request):
    sess = _require_session(request, min_role="admin")
    if username == sess["username"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    try:
        request.app.state.user_service.change_role(username, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"updated": username, "role": body.role}


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(request: Request):
    _require_session(request)
    pairs: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                pairs[k.strip()] = v.strip()
    return {"values": pairs, "fields": _CONFIG_FIELDS}


class ConfigUpdate(BaseModel):
    values: dict[str, str]


@router.post("/config")
async def save_config(body: ConfigUpdate, request: Request):
    _require_session(request, min_role="admin")
    existing: dict[str, str] = {}
    header_lines: list[str] = []
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                header_lines.append(line)
            elif "=" in stripped:
                k, _, v = stripped.partition("=")
                existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in body.values.items() if v != "" and k in _CONFIG_FIELDS})
    lines = header_lines + [f"{k}={v}" for k, v in existing.items()]
    _ENV_FILE.write_text("\n".join(lines) + "\n")
    _LOGGER.info("admin.config_saved")

    from avatar_backend.config import get_settings
    from avatar_backend.services.tts_service import create_tts_service
    get_settings.cache_clear()
    new_settings = get_settings()
    new_tts = create_tts_service(new_settings)
    request.app.state.tts_service = new_tts
    _LOGGER.info("admin.tts_reloaded", provider=new_settings.tts_provider)

    if new_settings.tts_provider.lower() == "afrotts":
        async def _warm():
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, new_tts._get_pipeline)
                _LOGGER.info("admin.afrotts_warmed")
            except Exception as exc:
                _LOGGER.warning("admin.afrotts_warm_failed", exc=str(exc))
        asyncio.create_task(_warm())

    return {"saved": True}


# ── System prompt ─────────────────────────────────────────────────────────────

class TextBody(BaseModel):
    text: str


@router.get("/prompt")
async def get_prompt(request: Request):
    _require_session(request)
    return {"text": _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""}


@router.post("/prompt")
async def save_prompt(body: TextBody, request: Request):
    _require_session(request, min_role="admin")
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PROMPT_FILE.write_text(body.text)
    _LOGGER.info("admin.prompt_saved", chars=len(body.text))
    return {"saved": True}


# ── ACL ───────────────────────────────────────────────────────────────────────

@router.get("/acl")
async def get_acl(request: Request):
    _require_session(request)
    return {"text": _ACL_FILE.read_text() if _ACL_FILE.exists() else ""}


@router.post("/acl")
async def save_acl(body: TextBody, request: Request):
    _require_session(request, min_role="admin")
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _ACL_FILE.write_text(body.text)
    _LOGGER.info("admin.acl_saved")
    return {"saved": True}


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


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(request: Request):
    _require_session(request)
    return {"active_sessions": request.app.state.session_manager.active_count()}


@router.delete("/sessions/{session_id}")
async def clear_session(session_id: str, request: Request):
    _require_session(request, min_role="admin")
    await request.app.state.conversation_service.clear_session_state(session_id)
    return {"cleared": session_id}


# ── Persistent memory ────────────────────────────────────────────────────────

class MemoryBody(BaseModel):
    summary: str
    category: str = "general"
    confidence: float = 0.9
    pinned: bool = False


@router.get("/memory")
async def list_memory(request: Request, n: int = 200):
    _require_session(request, min_role="viewer")
    svc = request.app.state.memory_service
    return {"memories": svc.list_memories(limit=max(1, min(n, 500)))}


@router.post("/memory")
async def create_memory(body: MemoryBody, request: Request):
    _require_session(request, min_role="admin")
    svc = request.app.state.memory_service
    memory = svc.add_memory(
        summary=body.summary,
        category=body.category,
        confidence=body.confidence,
        pinned=body.pinned,
    )
    return {"memory": memory}


@router.delete("/memory")
async def clear_memory(request: Request):
    _require_session(request, min_role="admin")
    svc = request.app.state.memory_service
    removed = svc.clear_memories()
    return {"cleared": removed}


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: int, request: Request):
    _require_session(request, min_role="admin")
    svc = request.app.state.memory_service
    deleted = svc.delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": memory_id}


# ── Motion clips ─────────────────────────────────────────────────────────────

class MotionClipSearchBody(BaseModel):
    query: str = ""
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    camera_entity_id: str | None = None
    canonical_event_type: str | None = None


def _serialize_motion_clip(clip: dict) -> dict:
    data = dict(clip)
    data["video_url"] = f"/admin/motion-clips/{clip['id']}/video" if clip.get("video_relpath") else ""
    extra = data.get("extra") or {}
    canonical_event = data.get("canonical_event") or extra.get("canonical_event") or {}
    data["canonical_event"] = canonical_event
    data["canonical_event_id"] = data.get("canonical_event_id") or canonical_event.get("event_id") or ""
    data["canonical_event_type"] = data.get("canonical_event_type") or canonical_event.get("event_type") or ""
    data["event_source"] = (
        canonical_event.get("event_context", {}).get("source")
        or extra.get("source")
        or ""
    )
    return data


def _surface_event_iso_ts(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _serialize_event_history_item(item: dict) -> dict:
    open_loop = _OPEN_LOOP_SERVICE.extract_summary_fields(
        item.get("data") or {},
        status=str(item.get("status") or ""),
        fallback_ts=str(item.get("ts") or ""),
    )
    action_service = ActionService(open_loop_service=_OPEN_LOOP_SERVICE)
    payload = {
        "id": item.get("id", ""),
        "kind": item.get("kind", "event"),
        "ts": item.get("ts", ""),
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "status": item.get("status", ""),
        "event_id": item.get("event_id", ""),
        "event_type": item.get("event_type", ""),
        "event_source": item.get("event_source", ""),
        "camera_entity_id": item.get("camera_entity_id", ""),
        "clip_id": item.get("clip_id"),
        "video_url": item.get("video_url", ""),
        "open_loop_note": open_loop["open_loop_note"] or item.get("open_loop_note", ""),
        "open_loop_state": open_loop["open_loop_state"],
        "open_loop_active": open_loop["open_loop_active"],
        "open_loop_started_ts": open_loop["open_loop_started_ts"],
        "open_loop_updated_ts": open_loop["open_loop_updated_ts"],
        "open_loop_resolved_ts": open_loop["open_loop_resolved_ts"],
        "open_loop_age_s": open_loop["open_loop_age_s"],
        "open_loop_stale": open_loop["open_loop_stale"],
        "open_loop_last_reminder_ts": open_loop["open_loop_last_reminder_ts"],
        "open_loop_reminder_count": open_loop["open_loop_reminder_count"],
        "open_loop_reminder_due": open_loop["open_loop_reminder_due"],
        "open_loop_reminder_state": open_loop["open_loop_reminder_state"],
        "open_loop_last_escalation_ts": open_loop["open_loop_last_escalation_ts"],
        "open_loop_escalation_level": open_loop["open_loop_escalation_level"],
        "open_loop_escalation_due": open_loop["open_loop_escalation_due"],
        "open_loop_priority": open_loop["open_loop_priority"],
        "data": item.get("data") or {},
    }
    payload["available_actions"] = action_service.build_event_history_actions(payload)
    return payload


def _motion_clip_is_playable(request: Request, clip: dict) -> bool:
    svc = request.app.state.motion_clip_service
    path = svc.clip_path_for(clip)
    if not path or not path.exists():
        return False
    try:
        result = subprocess.run(
            [
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return False
        return float((result.stdout or "0").strip() or "0") > 0.5
    except Exception:
        return False


@router.get("/motion-clips")
async def list_motion_clips(
    request: Request,
    limit: int = 60,
    date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    camera_entity_id: str | None = None,
    canonical_event_type: str | None = None,
):
    _require_session(request, min_role="viewer")
    db = request.app.state.metrics_db
    clips = db.recent_motion_clips(
        limit=max(1, min(limit, 200)),
        date=date,
        start_time=start_time,
        end_time=end_time,
        camera_entity_id=camera_entity_id,
        canonical_event_type=canonical_event_type,
    )
    clips = [clip for clip in clips if _motion_clip_is_playable(request, clip)]
    return {"clips": [_serialize_motion_clip(clip) for clip in clips]}


@router.post("/motion-clips/search")
async def search_motion_clips(body: MotionClipSearchBody, request: Request):
    _require_session(request, min_role="viewer")
    svc = request.app.state.motion_clip_service
    result = await svc.search(
        query=body.query or "",
        date=body.date,
        start_time=body.start_time,
        end_time=body.end_time,
        camera_entity_id=body.camera_entity_id,
        canonical_event_type=body.canonical_event_type,
    )
    clips = [clip for clip in result.get("clips", []) if _motion_clip_is_playable(request, clip)]
    return {
        "mode": result.get("mode", "recent"),
        "clips": [_serialize_motion_clip(clip) for clip in clips],
    }


@router.get("/motion-clips/{clip_id}/video", include_in_schema=False)
async def serve_motion_clip_video(clip_id: int, request: Request):
    _require_session(request, min_role="viewer")
    db = request.app.state.metrics_db
    svc = request.app.state.motion_clip_service
    clip = db.get_motion_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Motion clip not found")
    if not _motion_clip_is_playable(request, clip):
        raise HTTPException(status_code=404, detail="Motion clip is not playable")
    path = svc.clip_path_for(clip)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Motion clip file unavailable")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@router.get("/event-history")
async def get_event_history(
    request: Request,
    limit: int = 20,
    query: str | None = None,
    kind: str | None = None,
    event_type: str | None = None,
    event_source: str | None = None,
    status: str | None = None,
    open_loop_state: str | None = None,
    open_loop_only: bool = False,
    open_loop_stale_only: bool = False,
    open_loop_priority: str | None = None,
    open_loop_reminder_due_only: bool = False,
    open_loop_escalation_due_only: bool = False,
    window: str | None = None,
    before_ts: str | None = None,
):
    _require_session(request, min_role="viewer")
    db = request.app.state.metrics_db
    surface_state = getattr(request.app.state, "surface_state_service", None)

    rows: list[dict] = []

    if db is not None:
        canonical_events = []
        if hasattr(db, "list_event_records"):
            canonical_events = db.list_event_records(limit=max(1, min(limit * 3, 120)))
        for event in canonical_events:
            rows.append(
                _serialize_event_history_item(
                    {
                        "id": f"canonical:{event.get('event_id') or event.get('created_at')}",
                        "kind": "canonical_event",
                        "ts": event.get("created_at", ""),
                        "title": event.get("details") or event.get("summary") or event.get("event_type", ""),
                        "summary": event.get("summary", ""),
                        "status": event.get("status", ""),
                        "event_id": event.get("event_id", ""),
                        "event_type": event.get("event_type", ""),
                        "event_source": event.get("source", ""),
                        "camera_entity_id": event.get("camera_entity_id", ""),
                        "clip_id": None,
                        "video_url": "",
                        "open_loop_note": str((event.get("data") or {}).get("open_loop_note", "")),
                        "data": event.get("data") or {},
                    }
                )
            )

    if db is not None:
        persisted_events = db.recent_event_history(max(1, min(limit * 3, 120)))
        for event in persisted_events:
            rows.append(
                _serialize_event_history_item(
                    {
                        "id": f"persisted:{event.get('event_id') or event.get('ts')}",
                        "kind": "persisted_event",
                        "ts": event.get("ts", ""),
                        "title": event.get("title", ""),
                        "summary": event.get("summary", ""),
                        "status": event.get("status", ""),
                        "event_id": event.get("event_id", ""),
                        "event_type": event.get("event_type", ""),
                        "event_source": event.get("event_source", ""),
                        "camera_entity_id": event.get("camera_entity_id", ""),
                        "clip_id": None,
                        "video_url": "",
                        "open_loop_note": str((event.get("data") or {}).get("open_loop_note", "")),
                        "data": event.get("data") or {},
                    }
                )
            )

    if db is not None:
        motion_clips = db.recent_motion_clips(limit=max(1, min(limit * 3, 120)))
        motion_clips = [clip for clip in motion_clips if _motion_clip_is_playable(request, clip)]
        for clip in motion_clips:
            payload = _serialize_motion_clip(clip)
            rows.append(
                _serialize_event_history_item(
                    {
                        "id": f"motion:{payload.get('id')}",
                        "kind": "motion_clip",
                        "ts": payload.get("ts", ""),
                        "title": payload.get("location") or payload.get("canonical_event_type") or "Motion event",
                        "summary": payload.get("description", ""),
                        "status": payload.get("status", ""),
                        "event_id": payload.get("canonical_event_id", ""),
                        "event_type": payload.get("canonical_event_type", ""),
                        "event_source": payload.get("event_source", ""),
                        "camera_entity_id": payload.get("camera_entity_id", ""),
                        "clip_id": payload.get("id"),
                        "video_url": payload.get("video_url", ""),
                        "open_loop_note": str(payload.get("extra", {}).get("open_loop_note", "")),
                        "data": {
                            "location": payload.get("location", ""),
                            "trigger_entity_id": payload.get("trigger_entity_id", ""),
                            "duration_s": payload.get("duration_s", 0),
                            "canonical_event": payload.get("canonical_event") or {},
                            "extra": payload.get("extra") or {},
                        },
                    }
                )
            )

    if surface_state is not None:
        snapshot = await surface_state.get_snapshot()
        for event in snapshot.get("recent_events", []):
            rows.append(
                _serialize_event_history_item(
                    {
                        "id": f"surface:{event.get('event_id', '')}",
                        "kind": "surface_event",
                        "ts": _surface_event_iso_ts(event.get("ts")),
                        "title": event.get("title") or event.get("event") or "Event",
                        "summary": event.get("message", ""),
                        "status": event.get("status", ""),
                        "event_id": event.get("event_id", ""),
                        "event_type": event.get("event", ""),
                        "event_source": "surface_state",
                        "camera_entity_id": event.get("camera_entity_id", ""),
                        "clip_id": None,
                        "video_url": "",
                        "open_loop_note": event.get("open_loop_note", ""),
                        "data": dict(event),
                    }
                )
            )

    rows.sort(key=lambda item: item.get("ts", ""), reverse=True)
    query_norm = (query or "").strip().lower()
    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        dedupe_key = str(row.get("event_id") or row.get("id") or "")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if kind and str(row.get("kind") or "") != kind:
            continue
        if event_type and str(row.get("event_type") or "") != event_type:
            continue
        if event_source and str(row.get("event_source") or "") != event_source:
            continue
        if status and str(row.get("status") or "") != status:
            continue
        if open_loop_state and str(row.get("open_loop_state") or "") != open_loop_state:
            continue
        if open_loop_only and not bool(row.get("open_loop_active")):
            continue
        if open_loop_stale_only and not bool(row.get("open_loop_stale")):
            continue
        if open_loop_priority and str(row.get("open_loop_priority") or "") != open_loop_priority:
            continue
        if open_loop_reminder_due_only and not bool(row.get("open_loop_reminder_due")):
            continue
        if open_loop_escalation_due_only and not bool(row.get("open_loop_escalation_due")):
            continue
        if query_norm:
            haystack = " ".join(
                [
                    str(row.get("title") or ""),
                    str(row.get("summary") or ""),
                    str(row.get("event_type") or ""),
                    str(row.get("event_source") or ""),
                    str(row.get("open_loop_note") or ""),
                    str((row.get("data") or {}).get("admin_note") or ""),
                ]
            ).lower()
            if query_norm not in haystack:
                continue
        deduped.append(row)
        if len(deduped) >= max(1, min(limit, 100)):
            break

    if before_ts:
        deduped = [row for row in deduped if str(row.get("ts") or "") < before_ts]

    if window:
        now = datetime.now(timezone.utc)
        hours = {
            "24h": 24,
            "3d": 72,
            "7d": 168,
            "30d": 720,
        }.get(window)
        if hours:
            cutoff = (now - timedelta(hours=hours)).isoformat()
            deduped = [row for row in deduped if str(row.get("ts") or "") >= cutoff]

    deduped = deduped[: max(1, min(limit, 100))]
    next_before = deduped[-1]["ts"] if deduped else None
    return {"events": deduped, "next_before_ts": next_before}


@router.get("/event-history/workflow-summary")
async def get_event_history_workflow_summary(request: Request, limit: int = 10):
    _require_session(request, min_role="viewer")
    workflow_service = getattr(request.app.state, "open_loop_workflow_service", None)
    if workflow_service is None:
        workflow_service = OpenLoopWorkflowService(open_loop_service=_OPEN_LOOP_SERVICE)

    history = await get_event_history(
        request,
        limit=max(20, min(limit * 6, 120)),
        open_loop_only=True,
        window="30d",
    )
    persisted_rows = [row for row in history.get("events", []) if row.get("kind") == "persisted_event"]
    summary = workflow_service.summarize_due_work(persisted_rows, limit=max(1, min(limit, 20)))
    summary["generated_from"] = {"kind": "persisted_event", "count": len(persisted_rows)}
    return summary


@router.get("/event-history/workflow-status")
async def get_event_history_workflow_status(request: Request):
    _require_session(request, min_role="viewer")
    automation_service = getattr(request.app.state, "open_loop_automation_service", None)
    if automation_service is None:
        return {"running": False, "last_run_ts": "", "last_run_summary": {"planned": 0, "applied": 0, "applied_actions": []}}
    return automation_service.get_status()


# ── Test announce ─────────────────────────────────────────────────────────────

class AnnounceBody(BaseModel):
    message:  str
    priority: str = "normal"


class EventHistoryActionBody(BaseModel):
    event_id: str = ""
    status: Literal["active", "acknowledged", "resolved"] = "active"
    workflow_action: Literal["send_reminder", "escalate_medium", "escalate_high"] | None = None
    title: str = ""
    summary: str = ""
    event_type: str = ""
    event_source: str = ""
    camera_entity_id: str = ""
    open_loop_note: str | None = None
    admin_note: str | None = None
    reminder_sent: bool = False
    escalation_level: Literal["medium", "high"] | None = None


class EventHistoryWorkflowRunBody(BaseModel):
    include_reminders: bool = True
    include_escalations: bool = True
    limit: int = 10
    dry_run: bool = False


class EventHistoryDomainActionBody(BaseModel):
    session_id: str = "admin_event_history"
    event_id: str = ""
    action: Literal["ask_about_event", "show_related_camera"]
    title: str = ""
    summary: str = ""
    event_type: str = ""
    event_source: str = ""
    camera_entity_id: str = ""
    followup_prompt: str | None = None
    target_camera_entity_id: str | None = None
    target_event: str | None = None
    target_title: str | None = None
    target_message: str | None = None


def _default_open_loop_note(status: str, workflow_action: str | None = None) -> str:
    if workflow_action:
        return _OPEN_LOOP_SERVICE.default_note_for_workflow_action(workflow_action)
    return {
        "active": "Needs attention",
        "acknowledged": "Seen by admin",
        "resolved": "Closed out",
    }.get(status, "")


@router.post("/event-history/action")
async def update_event_history_action(body: EventHistoryActionBody, request: Request):
    _require_session(request, min_role="viewer")
    ws_mgr = getattr(request.app.state, "ws_manager", None)
    action_service = getattr(request.app.state, "action_service", None) or ActionService()

    event_id = (body.event_id or "").strip()
    open_loop_note = body.open_loop_note if body.open_loop_note is not None else _default_open_loop_note(body.status, body.workflow_action)
    return await action_service.handle_event_history_action(
        app=request.app,
        ws_mgr=ws_mgr,
        event_id=event_id,
        status=body.status,
        workflow_action=body.workflow_action,
        title=body.title,
        summary=body.summary,
        event_type=body.event_type,
        event_source=body.event_source,
        camera_entity_id=body.camera_entity_id,
        open_loop_note=open_loop_note,
        admin_note=body.admin_note,
        reminder_sent=body.reminder_sent,
        escalation_level=body.escalation_level,
    )


@router.post("/event-history/workflow-run")
async def run_event_history_workflow(body: EventHistoryWorkflowRunBody, request: Request):
    _require_session(request, min_role="viewer")
    workflow_service = getattr(request.app.state, "open_loop_workflow_service", None)
    if workflow_service is None:
        workflow_service = OpenLoopWorkflowService(open_loop_service=_OPEN_LOOP_SERVICE)
    action_service = getattr(request.app.state, "action_service", None) or ActionService()
    ws_mgr = getattr(request.app.state, "ws_manager", None)

    history = await get_event_history(
        request,
        limit=max(20, min(body.limit * 8, 160)),
        open_loop_only=True,
        window="30d",
    )
    persisted_rows = [row for row in history.get("events", []) if row.get("kind") == "persisted_event"]
    planned = workflow_service.plan_due_actions(
        persisted_rows,
        include_reminders=body.include_reminders,
        include_escalations=body.include_escalations,
        limit=max(1, min(body.limit, 25)),
    )
    if body.dry_run:
        return {"planned": planned, "applied": [], "dry_run": True}

    applied: list[dict] = []
    for item in planned:
        applied.append(
            await action_service.handle_event_history_action(
                app=request.app,
                ws_mgr=ws_mgr,
                event_id=str(item.get("event_id") or ""),
                status=str(item.get("status") or "active"),
                workflow_action=str(item.get("workflow_action") or ""),
                title=str(item.get("title") or ""),
                summary=str(item.get("summary") or ""),
                event_type=str(item.get("event_type") or ""),
                event_source=str(item.get("event_source") or ""),
                open_loop_note=str(item.get("open_loop_note") or ""),
            )
        )
    return {"planned": planned, "applied": applied, "dry_run": False}


@router.post("/event-history/domain-action")
async def run_event_history_domain_action(body: EventHistoryDomainActionBody, request: Request):
    _require_session(request, min_role="viewer")
    ws_mgr = getattr(request.app.state, "ws_manager", None)
    action_service = getattr(request.app.state, "action_service", None) or ActionService()
    return await action_service.handle_event_history_domain_action(
        app=request.app,
        ws_mgr=ws_mgr,
        session_id=(body.session_id or "admin_event_history").strip() or "admin_event_history",
        event_id=(body.event_id or "").strip(),
        action=body.action,
        title=body.title,
        summary=body.summary,
        event_type=body.event_type,
        event_source=body.event_source,
        camera_entity_id=body.camera_entity_id,
        followup_prompt=body.followup_prompt,
        target_camera_entity_id=body.target_camera_entity_id,
        target_event=body.target_event,
        target_title=body.target_title,
        target_message=body.target_message,
    )


@router.post("/announce/test")
async def test_announce(body: AnnounceBody, request: Request):
    _require_session(request, min_role="admin")
    from avatar_backend.routers.announce import AnnounceRequest, announce_handler
    return await announce_handler(
        AnnounceRequest(message=body.message, priority=body.priority),  # type: ignore[arg-type]
        request,
    )


# ── Live logs (SSE) ───────────────────────────────────────────────────────────
# EventSource cannot set custom headers, but cookies are sent automatically
# for same-origin requests — session cookie checked directly here.

@router.get("/logs")
async def stream_logs(request: Request):
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    async def generate():
        if _LOG_FILE.exists():
            for line in _LOG_FILE.read_text().splitlines()[-100:]:
                yield f"data: {line}\n\n"

        pos = _LOG_FILE.stat().st_size if _LOG_FILE.exists() else 0
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.5)
            if not _LOG_FILE.exists():
                continue
            new_size = _LOG_FILE.stat().st_size
            if new_size > pos:
                with open(_LOG_FILE) as f:
                    f.seek(pos)
                    chunk = f.read()
                pos = new_size
                for line in chunk.splitlines():
                    if line.strip():
                        yield f"data: {line}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


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
    return {"skin_tone": 0, "avatar_url": ""}


class AvatarSettings(BaseModel):
    skin_tone: int = 0
    avatar_url: str = ""


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
_AVATAR_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

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
        raise HTTPException(status_code=413, detail="File too large (max 50 MB).")
    _AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    (_AVATARS_DIR / safe).write_bytes(content)
    _LOGGER.info("admin.avatar_uploaded", filename=safe, bytes=len(content))
    return {"uploaded": safe}


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


class SyncPromptResponse(BaseModel):
    status:             str
    new_entities_found: int
    prompt_updated:     bool
    summary:            str


@router.post("/sync-prompt", response_model=SyncPromptResponse)
async def sync_prompt(request: Request):
    _require_session(request, min_role="admin")
    import httpx as _httpx

    ha  = request.app.state.ha_proxy
    llm = request.app.state.llm_service

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
    new_summary    = summarise_new_entities(all_states, known)

    if not new_summary:
        return SyncPromptResponse(status="ok", new_entities_found=0,
                                  prompt_updated=False,
                                  summary="No new entities found — system prompt is up to date.")

    new_count = new_summary.count("\n  ")
    integration_request = (
        "You are updating the system prompt for Nova, an AI home automation controller.\n\n"
        "Here is the current system prompt:\n```\n" + current_prompt + "\n```\n\n"
        "The following new Home Assistant entities have been discovered:\n\n"
        + new_summary + "\n\n"
        "Instructions:\n"
        "- Add these entities to appropriate existing sections.\n"
        "- Skip clear infrastructure noise.\n"
        "- Preserve the exact structure, tone, and formatting of the original prompt.\n"
        "- Return ONLY the complete updated system prompt — no explanation, no markdown fences."
    )

    try:
        updated_prompt = await llm.generate_text(integration_request, timeout_s=180.0)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"LLM call failed: {exc}")

    if not updated_prompt or len(updated_prompt) < len(current_prompt) // 2:
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly short response.")
    if len(updated_prompt) > len(current_prompt) * 3:
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly long response — possible prompt injection. Prompt not saved.")

    # Strip NUL bytes and non-printable control characters before persisting
    updated_prompt = "".join(c for c in updated_prompt if c >= " " or c in "\n\r\t")

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PROMPT_FILE.write_text(updated_prompt)

    from avatar_backend.services.session_manager import SessionManager
    request.app.state.session_manager = SessionManager(updated_prompt)

    proactive = getattr(request.app.state, "proactive_service", None)
    if proactive is not None:
        proactive.update_system_prompt(updated_prompt)

    return SyncPromptResponse(status="ok", new_entities_found=new_count,
                               prompt_updated=True,
                               summary=f"Integrated {new_count} new entities into the system prompt.")




# ── Python Logger (SSE + snapshot) ────────────────────────────────────────────────────────────

@router.get("/pylog")
async def get_pylog(request: Request, n: int = 500, level: str = ""):
    """Return recent server log entries as JSON (optionally filtered by level)."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    store = getattr(request.app.state, "log_store", None)
    entries = store.recent(n, level or None) if store else []
    return {"entries": entries}


@router.get("/pylog/stream")
async def stream_pylog(request: Request):
    """SSE stream — pushes each new log entry as JSON."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    store = getattr(request.app.state, "log_store", None)
    if not store:
        return JSONResponse({"detail": "Log store not available"}, status_code=503)

    import json as _json

    async def generate():
        q = store.subscribe()
        try:
            for entry in store.recent(200):
                yield f"data: {_json.dumps(entry)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {_json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            store.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── AI Decision Log (SSE + snapshot) ─────────────────────────────────────────


# ── LLM Cost Log (SSE + snapshot) ────────────────────────────────────────────

@router.get("/costs")
async def get_costs(request: Request):
    """Return recent LLM cost entries + session totals as JSON."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    log = getattr(request.app.state, "cost_log", None)
    db = getattr(request.app.state, "metrics_db", None)

    entries = log.recent(200) if log else []
    totals = log.totals() if log else {}

    if not entries and db:
        entries = db.recent_invocations(200)
        if entries:
            totals = _totals_from_entries(entries)

    return {"entries": entries, "totals": totals}


def _totals_from_entries(entries: list[dict]) -> dict:
    by_model: dict[str, dict] = {}
    total_input = 0
    total_output = 0
    total_cost = 0.0

    for e in entries:
        input_tokens = int(e.get("input_tokens", 0) or 0)
        output_tokens = int(e.get("output_tokens", 0) or 0)
        cost_usd = float(e.get("cost_usd", 0.0) or 0.0)
        total_input += input_tokens
        total_output += output_tokens
        total_cost += cost_usd
        key = f"{e.get('provider', '')}/{e.get('model', '')}"
        bucket = by_model.setdefault(key, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "price_in": 0.0,
            "price_out": 0.0,
        })
        bucket["calls"] += 1
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["cost_usd"] += cost_usd

    for bucket in by_model.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)

    return {
        "session_calls": len(entries),
        "session_input_tokens": total_input,
        "session_output_tokens": total_output,
        "session_cost_usd": round(total_cost, 6),
        "by_model": by_model,
    }


@router.get("/costs/stream")
async def stream_costs(request: Request):
    """SSE stream — pushes each new LLM cost event as it happens."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    log = getattr(request.app.state, "cost_log", None)

    async def generate():
        import json as _json
        if not log:
            yield "data: {}\n\n"
            return
        q = log.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {_json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            log.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")

@router.get("/decisions")
async def get_decisions(request: Request):
    """Return the last 200 AI decision events as JSON."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    log = getattr(request.app.state, "decision_log", None)
    return {"decisions": log.recent(200) if log else []}


@router.get("/decisions/stream")
async def stream_decisions(request: Request):
    """SSE stream — pushes each new decision event as it happens."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    log = getattr(request.app.state, "decision_log", None)

    async def generate():
        import json as _json
        # Send backlog of recent decisions first
        if log:
            for entry in log.recent(50):
                yield f"data: {_json.dumps(entry)}\n\n"
        if not log:
            yield "data: {}\n\n"
            return
        q = log.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {_json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            log.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")

@router.get("/ollama-models")
async def list_ollama_models(request: Request):
    """Return list of locally available Ollama model names."""
    _require_session(request, min_role="viewer")
    import httpx as _httpx
    from avatar_backend.config import get_settings as _gs
    settings = _gs()
    ollama_url = getattr(settings, "OLLAMA_URL", "http://localhost:11434").rstrip("/")
    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        models = [m["name"] for m in data.get("models", [])]
    except Exception as exc:
        _LOGGER.warning("ollama_models.fetch_failed", error=str(exc))
        models = []
    return {"models": models}


# ── Cost history (persistent DB) ──────────────────────────────────────────────

@router.get("/costs/history")
async def get_cost_history(request: Request, period: str = "month"):
    """Return cost chart data filtered by period (day/week/month/year)."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    db = getattr(request.app.state, "metrics_db", None)
    if not db:
        return {"summary": {}, "by_day": [], "by_model": [], "monthly": []}

    period = period if period in ("day", "week", "month", "year") else "month"
    days_map = {"day": 1, "week": 7, "month": 30, "year": 365}

    summary  = db.cost_summary(period)
    by_day   = db.cost_by_day(days=days_map[period])
    by_model = db.cost_by_model(period)
    monthly  = db.monthly_totals(12)

    return {
        "summary":  summary,
        "by_day":   by_day,
        "by_model": by_model,
        "monthly":  monthly,
    }


# ── System metrics ────────────────────────────────────────────────────────────

@router.get("/metrics")
async def get_metrics(request: Request):
    """Return latest system sample + recent history."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    db       = getattr(request.app.state, "metrics_db", None)
    sys_svc  = getattr(request.app.state, "sys_metrics", None)
    latest   = sys_svc.latest() if sys_svc else (db.latest_sample() if db else None)
    history  = db.hourly_averages(24) if db else []
    return {"latest": latest, "history": history}


@router.get("/metrics/stream")
async def stream_metrics(request: Request):
    """SSE stream — pushes a new system sample every 5 s."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    sys_svc = getattr(request.app.state, "sys_metrics", None)

    async def generate():
        import json as _json
        if not sys_svc:
            yield "data: {}\n\n"
            return
        latest = sys_svc.latest()
        if latest:
            yield f"data: {_json.dumps(latest)}\n\n"
        q = sys_svc.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    sample = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {_json.dumps(sample)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            sys_svc.unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/metrics/history")
async def get_metrics_history(request: Request, hours: int = 24):
    """Return hourly averages for system metrics."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    db = getattr(request.app.state, "metrics_db", None)
    if not db:
        return {"averages": []}
    hours = min(max(hours, 1), 168)  # cap at 1 week
    return {"averages": db.hourly_averages(hours)}
