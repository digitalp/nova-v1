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
    # ── Proactive cooldowns & timing ──
    "PROACTIVE_ENTITY_COOLDOWN_S":        ("Per-entity announce cooldown (seconds)",          False),
    "PROACTIVE_CAMERA_COOLDOWN_S":        ("Per-camera announce cooldown (seconds)",          False),
    "PROACTIVE_GLOBAL_MOTION_COOLDOWN_S": ("Global motion announce cooldown (seconds)",       False),
    "PROACTIVE_GLOBAL_ANNOUNCE_COOLDOWN_S": ("Global batch announce cooldown (seconds)",      False),
    "PROACTIVE_QUEUE_DEDUP_COOLDOWN_S":   ("Queue dedup cooldown (seconds)",                  False),
    "PROACTIVE_BATCH_WINDOW_S":           ("Batch triage window (seconds)",                   False),
    "PROACTIVE_MAX_BATCH_CHANGES":        ("Max changes per batch",                           False),
    "PROACTIVE_WEATHER_COOLDOWN_S":       ("Weather announce cooldown (seconds)",             False),
    "PROACTIVE_FORECAST_HOUR":            ("Daily forecast hour (0-23)",                      False),
    "HA_POWER_ALERT_COOLDOWN_S":          ("Power alert cooldown (seconds)",                  False),
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


def _set_session_cookie(response: JSONResponse | RedirectResponse, token: str, request: Request | None = None) -> None:
    # H4 security fix: set secure=True when served over HTTPS
    is_https = (
        request is not None
        and (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto") == "https"
        )
    )
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=is_https,
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
    _set_session_cookie(resp, token, request=request)
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
    # H1 security fix: strip newlines/carriage-returns/NUL to prevent env injection
    _UNSAFE_ENV_CHARS = str.maketrans("", "", "\n\r\x00")
    sanitized = {
        k: v.translate(_UNSAFE_ENV_CHARS)
        for k, v in body.values.items()
        if v != "" and k in _CONFIG_FIELDS
    }
    existing.update(sanitized)
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


# ── Prompt management ──────────────────────────────────────────────────────────

_PROMPTS_DIR = _CONFIG_DIR / "prompts"

_PROMPT_REGISTRY: dict[str, tuple[str, str, str]] = {
    "system":          ("System Prompt",           "Main personality and behaviour instructions for Nova",  "system_prompt.txt"),
    "heating_shadow":  ("Heating Controller",      "Prompt for the autonomous heating shadow controller",   "heating_shadow_prompt.txt"),
    "triage":          ("Batch Triage",            "Template for deciding if state changes warrant an announcement (use {home_context} and {changes} placeholders)", "prompts/triage.txt"),
    "vision_default":  ("Vision — Default",        "Default prompt for describing camera snapshots",        "prompts/vision_default.txt"),
    "vision_doorbell": ("Vision — Doorbell",       "Prompt when the doorbell is pressed",                   "prompts/vision_doorbell.txt"),
    "vision_motion":   ("Vision — Motion",         "Prompt for motion-triggered camera snapshots",          "prompts/vision_motion.txt"),
    "vision_driveway": ("Vision — Driveway",       "Prompt for driveway camera events",                    "prompts/vision_driveway.txt"),
    "vision_outdoor":  ("Vision — Outdoor",        "Prompt for rear/side outdoor camera events",            "prompts/vision_outdoor.txt"),
    "vision_entrance": ("Vision — Entrance",       "Prompt for front door / entrance camera events",        "prompts/vision_entrance.txt"),
}


@router.get("/prompts")
async def list_prompts(request: Request):
    _require_session(request, min_role="viewer")
    result = []
    for slug, (label, description, filename) in _PROMPT_REGISTRY.items():
        path = _CONFIG_DIR / filename
        text = ""
        exists = path.exists()
        if exists:
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                pass
        result.append({
            "slug": slug, "label": label, "description": description,
            "filename": filename, "text": text, "exists": exists, "chars": len(text),
        })
    return {"prompts": result}


class PromptUpdateBody(BaseModel):
    text: str


@router.get("/prompts/{slug}")
async def get_prompt(slug: str, request: Request):
    _require_session(request, min_role="viewer")
    entry = _PROMPT_REGISTRY.get(slug)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {slug}")
    label, description, filename = entry
    path = _CONFIG_DIR / filename
    text = ""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            pass
    return {"slug": slug, "label": label, "description": description, "text": text}


@router.post("/prompts/{slug}")
async def save_prompt(slug: str, body: PromptUpdateBody, request: Request):
    _require_session(request, min_role="admin")
    entry = _PROMPT_REGISTRY.get(slug)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {slug}")
    label, description, filename = entry
    path = _CONFIG_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.text, encoding="utf-8")
    _LOGGER.info("admin.prompt_saved", slug=slug, chars=len(body.text))
    return {"saved": True, "slug": slug, "chars": len(body.text)}


# ── Speaker routing ────────────────────────────────────────────────────────────

class SpeakerPrefsBody(BaseModel):
    speakers: list[dict]


@router.get("/speakers")
async def get_speakers(request: Request):
    _require_session(request, min_role="viewer")
    svc = getattr(request.app.state, "speaker_service", None)
    if svc is None:
        return {"areas": [], "occupied_areas": []}
    catalog = await svc.get_speaker_catalog()
    occupied = await svc.get_occupied_areas()
    # Group by area_name for the admin UI
    area_map: dict[str, list[dict]] = {}
    for sp in catalog:
        area = sp.get("area_name") or "Unknown"
        area_map.setdefault(area, []).append(sp)
    areas = [
        {"area_name": name, "speakers": speakers}
        for name, speakers in sorted(area_map.items(), key=lambda x: x[0].lower())
    ]
    return {"areas": areas, "occupied_areas": occupied}


@router.post("/speakers")
async def save_speakers(body: SpeakerPrefsBody, request: Request):
    _require_session(request, min_role="admin")
    svc = getattr(request.app.state, "speaker_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Speaker service not available")
    svc.set_speaker_preferences(body.speakers)
    _LOGGER.info("admin.speakers_saved", count=len(body.speakers))
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


# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────

@router.post("/tunnel/refresh")
async def refresh_tunnel(request: Request):
    """Restart the Cloudflare quick tunnel and update PUBLIC_URL with the new URL."""
    _require_session(request, min_role="admin")
    _LOGGER.info("admin.tunnel_refresh_requested")

    # Restart the tunnel service
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo", "/usr/bin/systemctl", "restart", "cloudflared-nova",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)

    # Wait for the tunnel to come up and get the new URL
    await asyncio.sleep(6)
    new_url = await _read_tunnel_url()

    if not new_url:
        return {"ok": False, "error": "Tunnel restarted but URL not yet available. Check logs."}

    # Update .env
    _update_env_value("PUBLIC_URL", new_url)

    # Reload settings
    from avatar_backend.config import get_settings
    get_settings.cache_clear()

    _LOGGER.info("admin.tunnel_refreshed", url=new_url)
    return {"ok": True, "url": new_url}


@router.get("/tunnel/status")
async def tunnel_status(request: Request):
    """Check the current Cloudflare tunnel URL."""
    _require_session(request, min_role="viewer")
    url = await _read_tunnel_url()
    # Also check if the current PUBLIC_URL matches
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


def _update_env_value(key: str, value: str) -> None:
    """Update a single key in the .env file."""
    if not _ENV_FILE.exists():
        return
    lines = _ENV_FILE.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    _ENV_FILE.write_text("\n".join(lines) + "\n")


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(request: Request):
    _require_session(request)
    ws_mgr = getattr(request.app.state, "ws_manager", None)
    sessions = ws_mgr.list_voice_sessions() if ws_mgr else []
    return {"active_sessions": len(sessions), "sessions": sessions}


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


# Cache of clip_id -> bool so ffprobe only runs once per clip per process lifetime.
_playable_cache: dict[int, bool] = {}


# L4 security fix: async subprocess to avoid blocking the event loop
async def _motion_clip_is_playable(request: Request, clip: dict) -> bool:
    clip_id = clip.get("id")
    if clip_id is not None and clip_id in _playable_cache:
        return _playable_cache[clip_id]
    svc = request.app.state.motion_clip_service
    path = svc.clip_path_for(clip)
    if not path or not path.exists():
        if clip_id is not None:
            _playable_cache[clip_id] = False
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            if clip_id is not None:
                _playable_cache[clip_id] = False
            return False
        result = float((stdout or b"0").decode("utf-8", "ignore").strip() or "0") > 0.5
        if clip_id is not None:
            _playable_cache[clip_id] = result
        return result
    except Exception:
        return False


async def _filter_playable(request: Request, clips: list[dict]) -> list[dict]:
    """Run all playability checks concurrently instead of sequentially."""
    flags = await asyncio.gather(*[_motion_clip_is_playable(request, c) for c in clips])
    return [c for c, ok in zip(clips, flags) if ok]


@router.get("/announcements")
async def list_announcements(request: Request, limit: int = 200):
    """Return recent announcements from the JSONL log file, newest first."""
    _require_session(request, min_role="viewer")
    import json as _json
    from avatar_backend.runtime_paths import data_dir
    log_path = data_dir() / "announcements.jsonl"
    entries: list[dict] = []
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(_json.loads(line))
                except Exception:
                    pass
                if len(entries) >= limit:
                    break
        except Exception as exc:
            _LOGGER.warning("admin.announcements_read_failed", exc=str(exc))
    return {"announcements": entries, "total": len(entries)}


@router.delete("/announcements")
async def clear_announcements(request: Request):
    """Truncate the announcement log."""
    _require_session(request, min_role="admin")
    from avatar_backend.runtime_paths import data_dir
    log_path = data_dir() / "announcements.jsonl"
    try:
        log_path.write_text("", encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"cleared": True}


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
    clips = await _filter_playable(request, clips)
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
    clips = await _filter_playable(request, result.get("clips", []))
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
    if not await _motion_clip_is_playable(request, clip):
        raise HTTPException(status_code=404, detail="Motion clip is not playable")
    path = svc.clip_path_for(clip)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Motion clip file unavailable")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@router.delete("/motion-clips/{clip_id}")
async def delete_motion_clip(clip_id: int, request: Request):
    """Delete a single motion clip (DB row + video file)."""
    _require_session(request, min_role="admin")
    db = request.app.state.metrics_db
    svc = request.app.state.motion_clip_service
    clip = db.get_motion_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Motion clip not found")
    relpath = db.delete_motion_clip(clip_id)
    if relpath:
        path = svc.clip_path_for({"video_relpath": relpath})
        if path and path.exists():
            try:
                path.unlink()
            except Exception:
                pass
    return {"deleted": clip_id}


class BulkDeleteBody(BaseModel):
    ids: list[int] | None = None
    delete_all: bool = False


@router.post("/motion-clips/delete")
async def delete_motion_clips_bulk(body: BulkDeleteBody, request: Request):
    """Delete multiple clips by ID, or all clips if delete_all=true."""
    _require_session(request, min_role="admin")
    db = request.app.state.metrics_db
    svc = request.app.state.motion_clip_service

    if body.delete_all:
        relpaths = db.delete_all_motion_clips()
    elif body.ids:
        relpaths = db.delete_motion_clips_bulk(body.ids)
    else:
        return {"deleted": 0}

    deleted_files = 0
    for relpath in relpaths:
        path = svc.clip_path_for({"video_relpath": relpath})
        if path and path.exists():
            try:
                path.unlink()
                deleted_files += 1
            except Exception:
                pass

    return {"deleted": len(relpaths), "files_removed": deleted_files}


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
        motion_clips = await _filter_playable(request, motion_clips)
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
    bg_type: str = ""        # "color" | "image" | ""
    bg_color: str = ""       # hex color e.g. "#1a1a2e"
    bg_image_url: str = ""   # URL for background image


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


@router.get("/sync-prompt/preview")
async def sync_prompt_preview(request: Request):
    """
    Discover new HA entities not yet in the system prompt.
    Returns structured entity list enriched with area + state — no LLM call, no prompt changes.
    """
    _require_session(request, min_role="admin")
    import httpx as _httpx
    from avatar_backend.services.prompt_bootstrap import fetch_area_mapping, discover_new_entities

    ha = request.app.state.ha_proxy
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


class ApplySyncBody(BaseModel):
    entity_ids: list[str]
    area_overrides: dict[str, str] = {}  # entity_id → area name override


@router.post("/sync-prompt/apply")
async def sync_prompt_apply(body: ApplySyncBody, request: Request):
    """
    Integrate a user-selected subset of new entities into the system prompt via LLM.
    """
    _require_session(request, min_role="admin")
    import httpx as _httpx
    from avatar_backend.services.prompt_bootstrap import fetch_area_mapping, discover_new_entities

    if not body.entity_ids:
        raise HTTPException(status_code=400, detail="No entity IDs provided.")

    ha  = request.app.state.ha_proxy
    llm = request.app.state.llm_service

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
    request.app.state.session_manager = SessionManager(updated_prompt)
    proactive = getattr(request.app.state, "proactive_service", None)
    if proactive is not None:
        proactive.update_system_prompt(updated_prompt)

    _LOGGER.info("sync_prompt.apply_done", selected=new_count)
    return SyncPromptResponse(status="ok", new_entities_found=new_count,
                               prompt_updated=True,
                               summary=f"Integrated {new_count} selected entities into the system prompt.")


@router.post("/sync-prompt", response_model=SyncPromptResponse)
async def sync_prompt(request: Request):
    """Legacy full-auto sync — now area-aware. Prefer /preview + /apply for manual syncs."""
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

# ── Coral Wake Word ───────────────────────────────────────────────────────────

@router.get("/coral/wake-status")
async def coral_wake_status(request: Request):
    """Return current Coral wake detector status."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    detector = getattr(request.app.state, "coral_wake_detector", None)
    model_dir = Path(install_dir()) / "models" / "coral"
    edgetpu_path = model_dir / "nova_wakeword_edgetpu.tflite"
    cpu_path = model_dir / "nova_wakeword.tflite"
    verifier_path = model_dir / "nova_verifier.pkl"
    return {
        "coral_available": detector.coral_available if detector else False,
        "cpu_tflite_available": detector.cpu_tflite_available if detector else False,
        "numpy_model_available": detector.numpy_model_available if detector else False,
        "vad_available": detector.vad_available if detector else False,
        "verifier_available": detector.verifier_available if detector else False,
        "coral_model_exists": edgetpu_path.exists(),
        "cpu_model_exists": cpu_path.exists(),
        "verifier_model_exists": verifier_path.exists(),
        "pipeline_stages": detector.describe_pipeline() if detector else [],
        "edgetpu_compiler_available": _check_edgetpu_compiler(),
    }


def _check_edgetpu_compiler() -> bool:
    """Check if edgetpu_compiler is installed."""
    import shutil
    return shutil.which("edgetpu_compiler") is not None


@router.post("/coral/train-wakeword")
async def train_wakeword(request: Request):
    """Stream-train wake word models: verifier + TFLite (+ Edge TPU if compiler available).

    Pipeline:
    1. Generate synthetic audio via Piper TTS
    2. Generate negative (non-wake) samples
    3. Extract spectral features
    4. Train cosine-similarity verifier (~2ms)
    5. Build & quantize TFLite classification model (~3-8ms CPU)
    6. If edgetpu_compiler available, compile for Edge TPU (~1-3ms)
    7. Reload detector with new models
    """
    _require_session(request, min_role="admin")
    import json as _json_ww

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    wake_word = str(body.get("wake_word", "Nova")).strip() or "Nova"

    async def _generate():
        def _emit(stage: str, progress: int, message: str):
            payload = {"stage": stage, "progress": progress, "message": message}
            return f"data: {_json_ww.dumps(payload)}\n\n"

        yield _emit("init", 0, f"Training wake word models for '{wake_word}'...")

        model_dir = Path(install_dir()) / "models" / "coral"
        model_dir.mkdir(parents=True, exist_ok=True)
        verifier_path = model_dir / "nova_verifier.pkl"
        tflite_path = model_dir / "nova_wakeword.tflite"
        edgetpu_path = model_dir / "nova_wakeword_edgetpu.tflite"

        # ── Stage 1: Generate positive audio samples ──────────────────────────
        yield _emit("generate", 5, f"Generating synthetic '{wake_word}' audio samples via Piper TTS...")

        tts = getattr(request.app.state, "tts_service", None)
        if tts is None:
            yield _emit("error", 0, "TTS service not available — check Piper configuration")
            return

        import numpy as np
        import wave as _wave
        import io as _io

        positive_phrases = [
            wake_word,
            wake_word.lower(),
            f"Hey {wake_word}",
            f"hey {wake_word}",
            f"{wake_word}!",
            f"{wake_word}?",
            f"{wake_word}.",
            f"Hey {wake_word}!",
            f"OK {wake_word}",
            f"ok {wake_word}",
            f"Excuse me {wake_word}",
            f"{wake_word}, hello",
        ]
        negative_phrases = [
            "Hello there",
            "What time is it",
            "Turn on the lights",
            "Good morning",
            "Play some music",
            "Set a timer",
            "How is the weather",
            "Open the door",
            "Thank you very much",
            "I need help",
            "What is happening",
            "Stop playing",
        ]

        def _wav_to_pcm16k(wav_data: bytes) -> np.ndarray:
            with _io.BytesIO(wav_data) as buf:
                with _wave.open(buf, "rb") as wf:
                    sr = wf.getframerate()
                    frames = wf.readframes(wf.getnframes())
                    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if sr != 16000 and sr > 0:
                ratio = 16000 / sr
                new_len = int(len(audio) * ratio)
                indices = np.linspace(0, len(audio) - 1, new_len).astype(int)
                audio = audio[indices]
            target_len = 16000  # 1 second at 16kHz
            if len(audio) < target_len:
                audio = np.pad(audio, (0, target_len - len(audio)))
            else:
                audio = audio[:target_len]
            return audio

        def _extract_features(audio: np.ndarray) -> np.ndarray:
            fft = np.abs(np.fft.rfft(audio))
            bin_size = max(1, len(fft) // 128)
            features = np.array([fft[i * bin_size:(i + 1) * bin_size].mean() for i in range(128)])
            norm = np.linalg.norm(features)
            if norm > 0:
                features = features / norm
            return features

        positive_audio = []
        positive_features = []
        total_phrases = len(positive_phrases) + len(negative_phrases)

        for i, phrase in enumerate(positive_phrases):
            try:
                wav_bytes, _ = await tts.synthesise_with_timing(phrase)
                audio = _wav_to_pcm16k(wav_bytes)
                positive_audio.append(audio)
                positive_features.append(_extract_features(audio))
                pct = 5 + int((i / total_phrases) * 30)
                yield _emit("generate", pct, f"Positive {i+1}/{len(positive_phrases)}: '{phrase}'")
            except Exception as exc:
                yield _emit("warn", 5, f"Skipped '{phrase}': {exc}")

        if len(positive_audio) < 3:
            yield _emit("error", 0, f"Only {len(positive_audio)} positive samples — need at least 3")
            return

        # ── Stage 2: Generate negative audio samples ──────────────────────────
        yield _emit("generate", 35, "Generating negative (non-wake) samples...")

        negative_audio = []
        negative_features = []
        for i, phrase in enumerate(negative_phrases):
            try:
                wav_bytes, _ = await tts.synthesise_with_timing(phrase)
                audio = _wav_to_pcm16k(wav_bytes)
                negative_audio.append(audio)
                negative_features.append(_extract_features(audio))
                pct = 35 + int((i / len(negative_phrases)) * 10)
                yield _emit("generate", pct, f"Negative {i+1}/{len(negative_phrases)}: '{phrase}'")
            except Exception as exc:
                yield _emit("warn", 35, f"Skipped negative '{phrase}': {exc}")

        # Add silence as negative
        silence = np.zeros(16000, dtype=np.float32)
        negative_audio.append(silence)
        negative_features.append(_extract_features(silence))
        # Add noise as negative
        noise = np.random.randn(16000).astype(np.float32) * 0.01
        negative_audio.append(noise)
        negative_features.append(_extract_features(noise))

        yield _emit("generate", 45, f"Generated {len(positive_audio)} positive + {len(negative_audio)} negative samples")

        positive_features = np.array(positive_features, dtype=np.float32)
        negative_features = np.array(negative_features, dtype=np.float32)

        # ── Stage 3: Train verifier (centroid + threshold) ────────────────────
        yield _emit("train_verifier", 48, "Building cosine-similarity verifier...")

        try:
            import pickle

            centroid = positive_features.mean(axis=0)
            centroid_norm = np.linalg.norm(centroid)
            if centroid_norm > 0:
                centroid = centroid / centroid_norm

            pos_sims = np.array([np.dot(f, centroid) for f in positive_features])
            neg_sims = np.array([np.dot(f, centroid) for f in negative_features])
            mean_pos = float(pos_sims.mean())
            min_pos = float(pos_sims.min())
            max_neg = float(neg_sims.max())

            # Threshold: midpoint between worst positive and best negative
            threshold = max(0.3, (min_pos + max_neg) / 2.0)

            verifier_data = {
                "wake_word": wake_word,
                "centroid": centroid.tolist(),
                "threshold": threshold,
                "mean_similarity": mean_pos,
                "min_similarity": min_pos,
                "max_negative_similarity": max_neg,
                "num_positive": len(positive_features),
                "num_negative": len(negative_features),
                "feature_dim": 128,
                "trained_at": __import__("datetime").datetime.now().isoformat(),
            }
            with open(verifier_path, "wb") as f:
                pickle.dump(verifier_data, f)

            yield _emit("train_verifier", 55, f"Verifier saved — threshold={threshold:.3f}, pos_mean={mean_pos:.3f}, neg_max={max_neg:.3f}")
        except Exception as exc:
            yield _emit("error", 0, f"Verifier training failed: {exc}")
            return

        # ── Stage 4: Build TFLite classification model ────────────────────────
        yield _emit("train_tflite", 58, "Building TFLite keyword classification model...")

        tflite_built = False
        try:
            # Build a simple 2-layer dense classifier using raw numpy weights
            # Input: 128 features → Dense(64, relu) → Dense(2, softmax)
            # Then convert to TFLite with full int8 quantization

            X = np.vstack([positive_features, negative_features])
            y = np.array([1] * len(positive_features) + [0] * len(negative_features), dtype=np.float32)

            # Simple logistic regression via gradient descent (no TF dependency)
            # Architecture: input(128) → hidden(64, relu) → output(2, softmax)
            np.random.seed(42)
            W1 = np.random.randn(128, 64).astype(np.float32) * 0.1
            b1 = np.zeros(64, dtype=np.float32)
            W2 = np.random.randn(64, 2).astype(np.float32) * 0.1
            b2 = np.zeros(2, dtype=np.float32)

            def _relu(x):
                return np.maximum(0, x)

            def _softmax(x):
                e = np.exp(x - x.max(axis=-1, keepdims=True))
                return e / e.sum(axis=-1, keepdims=True)

            lr = 0.05
            n_epochs = 200
            n_samples = len(X)

            yield _emit("train_tflite", 60, f"Training classifier: {n_samples} samples, {n_epochs} epochs...")

            for epoch in range(n_epochs):
                # Forward pass
                h = _relu(X @ W1 + b1)
                logits = h @ W2 + b2
                probs = _softmax(logits)

                # One-hot encode labels
                y_onehot = np.zeros((n_samples, 2), dtype=np.float32)
                y_onehot[np.arange(n_samples), y.astype(int)] = 1.0

                # Backward pass (cross-entropy gradient)
                d_logits = (probs - y_onehot) / n_samples
                dW2 = h.T @ d_logits
                db2 = d_logits.sum(axis=0)
                d_h = d_logits @ W2.T
                d_h[X @ W1 + b1 <= 0] = 0  # relu gradient
                dW1 = X.T @ d_h
                db1 = d_h.sum(axis=0)

                W1 -= lr * dW1
                b1 -= lr * db1
                W2 -= lr * dW2
                b2 -= lr * db2

                if epoch % 50 == 0:
                    preds = probs.argmax(axis=1)
                    acc = (preds == y.astype(int)).mean()
                    pct = 60 + int((epoch / n_epochs) * 15)
                    yield _emit("train_tflite", pct, f"Epoch {epoch}/{n_epochs} — accuracy: {acc:.1%}")

            # Final accuracy
            h_final = _relu(X @ W1 + b1)
            probs_final = _softmax(h_final @ W2 + b2)
            preds_final = probs_final.argmax(axis=1)
            final_acc = (preds_final == y.astype(int)).mean()
            yield _emit("train_tflite", 76, f"Training complete — accuracy: {final_acc:.1%}")

            # ── Convert to TFLite with int8 quantization ──────────────────────
            yield _emit("quantize", 78, "Quantizing model to int8 TFLite...")

            tflite_bytes = _build_quantized_tflite(W1, b1, W2, b2, X)
            if tflite_bytes is not None:
                tflite_path.write_bytes(tflite_bytes)
                tflite_built = True
                yield _emit("quantize", 82, f"TFLite model saved ({len(tflite_bytes)} bytes)")
            else:
                # No TensorFlow — save numpy weights for the numpy model path
                if _save_numpy_model(model_dir, W1, b1, W2, b2):
                    yield _emit("quantize", 82, "Saved numpy model (TensorFlow not available for TFLite conversion)")
                else:
                    yield _emit("warn", 78, "Could not save model — verifier will be used instead")

        except Exception as exc:
            yield _emit("warn", 58, f"TFLite training failed (verifier still works): {exc}")

        # ── Stage 5: Edge TPU compilation (if compiler available) ─────────────
        edgetpu_compiled = False
        if tflite_built and _check_edgetpu_compiler():
            yield _emit("edgetpu", 84, "Compiling for Edge TPU...")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "edgetpu_compiler",
                    "-s",  # show operations
                    "-o", str(model_dir),
                    str(tflite_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                output = (stdout or b"").decode("utf-8", "ignore")
                # edgetpu_compiler outputs to <name>_edgetpu.tflite
                compiled_path = model_dir / "nova_wakeword_edgetpu.tflite"
                if compiled_path.exists():
                    edgetpu_compiled = True
                    yield _emit("edgetpu", 90, "Edge TPU model compiled successfully!")
                else:
                    yield _emit("warn", 84, f"Edge TPU compilation produced no output: {output[:200]}")
            except Exception as exc:
                yield _emit("warn", 84, f"Edge TPU compilation failed: {exc}")
        elif tflite_built:
            yield _emit("info", 84, "edgetpu_compiler not installed — CPU TFLite model will be used (~3-8ms)")

        # Always save numpy model as guaranteed fallback
        _save_numpy_model(model_dir, W1, b1, W2, b2)

        # ── Stage 6: Reload detector ──────────────────────────────────────────
        yield _emit("reload", 92, "Reloading wake word detector...")
        detector = getattr(request.app.state, "coral_wake_detector", None)
        if detector:
            detector.reload_verifier()
            detector.reload_tflite()
            yield _emit("reload", 98, "Detector reloaded with new models")

        # Summary
        models_built = ["verifier", "numpy_classifier"]
        if tflite_built:
            models_built.append("cpu_tflite")
        if edgetpu_compiled:
            models_built.append("edgetpu_tflite")
        summary = (
            f"Wake word '{wake_word}' trained! "
            f"Models: {', '.join(models_built)}. "
            f"{len(positive_audio)} positive + {len(negative_audio)} negative samples. "
            f"Verifier threshold={threshold:.3f}, classifier accuracy={final_acc:.1%}"
        )
        yield _emit("done", 100, summary)

    return StreamingResponse(_generate(), media_type="text/event-stream")


def _build_quantized_tflite(W1, b1, W2, b2, calibration_data) -> bytes | None:
    """Build a TFLite model from numpy weights.

    Tries TensorFlow first for proper TFLite conversion.
    Falls back to saving a numpy model archive that the detector can load directly.
    """
    import numpy as np

    # Try TensorFlow first (best TFLite support, enables Edge TPU compilation)
    try:
        import tensorflow as tf

        model = tf.keras.Sequential([
            tf.keras.layers.InputLayer(input_shape=(128,)),
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.Dense(2, activation="softmax"),
        ])
        model.layers[0].set_weights([W1, b1])
        model.layers[1].set_weights([W2, b2])

        converter = tf.lite.TFLiteConverter.from_keras_model(model)
        converter.optimizations = [tf.lite.Optimize.DEFAULT]

        def _representative_dataset():
            for sample in calibration_data:
                yield [sample.reshape(1, 128).astype(np.float32)]

        converter.representative_dataset = _representative_dataset
        try:
            converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
            converter.inference_input_type = tf.int8
            converter.inference_output_type = tf.int8
            return converter.convert()
        except Exception:
            # Fall back to default quantization
            converter2 = tf.lite.TFLiteConverter.from_keras_model(model)
            converter2.optimizations = [tf.lite.Optimize.DEFAULT]
            return converter2.convert()
    except ImportError:
        pass
    except Exception:
        pass

    # No TensorFlow — save as numpy model archive instead.
    # The detector will load this via _try_load_numpy_model().
    return None


def _save_numpy_model(model_dir, W1, b1, W2, b2) -> bool:
    """Save trained weights as a numpy archive for the detector to load."""
    import numpy as np
    try:
        path = model_dir / "nova_wakeword_weights.npz"
        np.savez(path, W1=W1, b1=b1, W2=W2, b2=b2)
        return True
    except Exception:
        return False


@router.post("/coral/install-edgetpu-compiler")
async def install_edgetpu_compiler(request: Request):
    """Install the Edge TPU compiler on the system."""
    _require_session(request, min_role="admin")

    if _check_edgetpu_compiler():
        return {"ok": True, "message": "edgetpu_compiler is already installed"}

    try:
        # Add Google Coral apt repo and install
        commands = [
            'curl -s https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -',
            'echo "deb https://packages.cloud.google.com/apt coral-edgetpu-stable main" | sudo tee /etc/apt/sources.list.d/coral-edgetpu.list',
            'sudo apt-get update -qq',
            'sudo apt-get install -y -qq edgetpu-compiler',
        ]
        for cmd in commands:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)

        installed = _check_edgetpu_compiler()
        return {
            "ok": installed,
            "message": "edgetpu_compiler installed successfully" if installed else "Installation completed but compiler not found",
        }
    except Exception as exc:
        return {"ok": False, "message": f"Installation failed: {exc}"}


# ── Heating Shadow ────────────────────────────────────────────────────────────

@router.get("/heating-shadow/history")
async def get_heating_shadow_history(request: Request, limit: int = 40):
    """Return recent heating shadow decision log entries for the admin panel."""
    _require_session(request)
    log = getattr(request.app.state, "decision_log", None)
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
async def force_heating_shadow(request: Request, scenario: str = "winter"):
    """
    Trigger a shadow-only heating evaluation with an injected scenario.
    scenario: 'winter' (default) or 'spring'.
    Writes are intercepted — nothing is applied to HA.
    """
    _require_session(request, min_role="admin")
    proactive = getattr(request.app.state, "proactive_service", None)
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



@router.get("/camera-discovery")
async def get_camera_discovery(request: Request):
    """Return the auto-discovered camera/motion sensor mappings from HA areas."""
    _require_session(request, min_role="admin")
    discovery = getattr(request.app.state, "camera_discovery", None)
    if discovery is None:
        return {"discovered": False, "message": "Camera discovery not available or not yet run"}
    proactive = getattr(request.app.state, "proactive_service", None)
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
async def refresh_camera_discovery(request: Request):
    """Re-run camera discovery from HA area registry."""
    _require_session(request, min_role="admin")
    from avatar_backend.services.camera_discovery import CameraDiscoveryService
    from avatar_backend.config import get_settings
    settings = get_settings()
    discovery = CameraDiscoveryService(settings.ha_url, settings.ha_token)
    result = await discovery.discover(timeout_s=15.0)
    if result.discovered:
        request.app.state.camera_discovery = result
        proactive = getattr(request.app.state, "proactive_service", None)
        if proactive and hasattr(proactive, "apply_discovery"):
            proactive.apply_discovery(result)
    return {
        "discovered": result.discovered,
        "outdoor_cameras": result.outdoor_cameras,
        "motion_camera_map": result.motion_camera_map,
        "bypass_cameras": list(result.bypass_global_motion_cameras),
    }
