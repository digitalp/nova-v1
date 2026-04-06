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
from pathlib import Path
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

_LOGGER = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])

_INSTALL_DIR  = Path("/opt/avatar-server")
_CONFIG_DIR   = _INSTALL_DIR / "config"
_ENV_FILE     = _INSTALL_DIR / ".env"
_PROMPT_FILE  = _CONFIG_DIR / "system_prompt.txt"
_ACL_FILE     = _CONFIG_DIR / "acl.yaml"
_LOG_FILE     = _INSTALL_DIR / "logs" / "avatar-backend.log"
_STATIC_DIR   = _INSTALL_DIR / "static"
_COOKIE_NAME  = "nova_session"

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
    await request.app.state.session_manager.clear(session_id)
    return {"cleared": session_id}


# ── Test announce ─────────────────────────────────────────────────────────────

class AnnounceBody(BaseModel):
    message:  str
    priority: str = "normal"


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


# ── Prompt sync ───────────────────────────────────────────────────────────────

_SYNC_DOMAINS = {
    "sensor", "binary_sensor", "light", "switch", "climate",
    "media_player", "lock", "cover", "input_boolean", "input_select",
    "input_number", "input_text", "person", "camera", "fan",
    "vacuum", "humidifier", "water_heater", "number",
}
_SKIP_PREFIXES = (
    "update.", "system.", "device_tracker.unifi_", "device_tracker.unknown_",
    "sensor.sun_", "sensor.moon_",
)
_SKIP_NAME_FRAGMENTS = ("rssi", "lqi", "linkquality", "uptime", "firmware", "version", "reboot")


def _extract_known_entity_ids(prompt_text: str) -> set[str]:
    import re
    return set(re.findall(r'\b\w+\.\w[\w_]*', prompt_text))


def _summarise_new_entities(states: list[dict], known: set[str]) -> str:
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)
    for s in states:
        eid    = s["entity_id"]
        domain = eid.split(".")[0]
        if domain not in _SYNC_DOMAINS:
            continue
        if eid in known:
            continue
        if any(eid.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if s["state"] in ("unavailable", "unknown", ""):
            continue
        name = s["attributes"].get("friendly_name", "")
        if any(frag in name.lower() for frag in _SKIP_NAME_FRAGMENTS):
            continue
        unit         = s["attributes"].get("unit_of_measurement", "")
        device_class = s["attributes"].get("device_class", "")
        line = f"  {eid}"
        if name and name != eid:
            line += f" | {name}"
        line += f" | {s['state']}"
        if unit:
            line += f" {unit}"
        if device_class:
            line += f" [{device_class}]"
        groups[domain].append(line)
    if not groups:
        return ""
    parts = []
    for domain in sorted(groups):
        parts.append(f"{domain} ({len(groups[domain])}):")
        parts.extend(groups[domain][:40])
    return "\n".join(parts)


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
    known          = _extract_known_entity_ids(current_prompt)
    new_summary    = _summarise_new_entities(all_states, known)

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



# ── AI Decision Log (SSE + snapshot) ─────────────────────────────────────────


# ── LLM Cost Log (SSE + snapshot) ────────────────────────────────────────────

@router.get("/costs")
async def get_costs(request: Request):
    """Return recent LLM cost entries + session totals as JSON."""
    if not _get_session(request):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    log = getattr(request.app.state, "cost_log", None)
    if not log:
        return {"entries": [], "totals": {}}
    return {"entries": log.recent(200), "totals": log.totals()}


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
