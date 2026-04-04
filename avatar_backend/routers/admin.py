"""
Admin panel — /admin

Serves the admin UI and supporting REST endpoints for managing the
avatar backend without SSH access.

All API endpoints require the same X-API-Key as the main server.
The /admin/logs SSE endpoint accepts ?api_key= query param (browser
EventSource cannot set headers).
"""
from __future__ import annotations
import asyncio
import subprocess
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from avatar_backend.middleware.auth import verify_api_key

_LOGGER = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])

_INSTALL_DIR = Path("/opt/avatar-server")
_CONFIG_DIR  = _INSTALL_DIR / "config"
_ENV_FILE    = _INSTALL_DIR / ".env"
_PROMPT_FILE = _CONFIG_DIR / "system_prompt.txt"
_ACL_FILE    = _CONFIG_DIR / "acl.yaml"
_LOG_FILE    = Path("/tmp/avatar-backend.log")
_STATIC_DIR  = _INSTALL_DIR / "static"

# Fields shown in the config editor (display label, sensitive flag)
_CONFIG_FIELDS = {
    "API_KEY":              ("API Key",                                     True),
    "HA_URL":               ("Home Assistant URL",                          False),
    "HA_TOKEN":             ("HA Long-lived Token",                         True),
    "LLM_PROVIDER":         ("LLM Provider (ollama/openai/google/anthropic)",False),
    "OLLAMA_URL":           ("Ollama URL",                                  False),
    "OLLAMA_MODEL":         ("Ollama Model",                                False),
    "CLOUD_MODEL":          ("Cloud Model Name",                            False),
    "OPENAI_API_KEY":       ("OpenAI API Key",                              True),
    "GOOGLE_API_KEY":       ("Google API Key",                              True),
    "ANTHROPIC_API_KEY":    ("Anthropic API Key",                           True),
    "WHISPER_MODEL":        ("Whisper Model",                               False),
    "TTS_PROVIDER":         ("TTS Provider",                                False),
    "PIPER_VOICE":          ("Piper Voice",                                 False),
    "ELEVENLABS_API_KEY":   ("ElevenLabs API Key",                          True),
    "ELEVENLABS_VOICE_ID":  ("ElevenLabs Voice ID",                         False),
    "ELEVENLABS_MODEL":     ("ElevenLabs Model",                            False),
    "AFROTTS_VOICE":        ("AfroTTS Voice",                               False),
    "AFROTTS_SPEED":        ("AfroTTS Speed (0.5-2.0)",                      False),
    "PUBLIC_URL":           ("Server Public URL (for audio playback)",      False),
    "SPEAKERS":             ("Speakers",                                    False),
    "TTS_ENGINE":           ("TTS Engine (Sonos)",                          False),
    "LOG_LEVEL":            ("Log Level",                                   False),
    "HOST":                 ("Bind Host",                                   False),
    "PORT":                 ("Bind Port",                                   False),
}


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def admin_page():
    return FileResponse(str(_STATIC_DIR / "admin.html"))


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config", dependencies=[Depends(verify_api_key)])
async def get_config():
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


@router.post("/config", dependencies=[Depends(verify_api_key)])
async def save_config(body: ConfigUpdate, request: Request):
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
    existing.update({k: v for k, v in body.values.items() if v != ""})
    lines = header_lines + [f"{k}={v}" for k, v in existing.items()]
    _ENV_FILE.write_text("\n".join(lines) + "\n")
    _LOGGER.info("admin.config_saved")

    # Reload TTS service immediately so provider changes take effect without a restart
    from avatar_backend.config import get_settings
    from avatar_backend.services.tts_service import create_tts_service
    get_settings.cache_clear()
    new_settings = get_settings()
    new_tts = create_tts_service(new_settings)
    request.app.state.tts_service = new_tts
    _LOGGER.info("admin.tts_reloaded", provider=new_settings.tts_provider)

    # Pre-warm AfroTTS (Kokoro) model in background — first load downloads weights
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

@router.get("/prompt", dependencies=[Depends(verify_api_key)])
async def get_prompt():
    return {"text": _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""}


class TextBody(BaseModel):
    text: str


@router.post("/prompt", dependencies=[Depends(verify_api_key)])
async def save_prompt(body: TextBody):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PROMPT_FILE.write_text(body.text)
    _LOGGER.info("admin.prompt_saved", chars=len(body.text))
    return {"saved": True}


# ── ACL ───────────────────────────────────────────────────────────────────────

@router.get("/acl", dependencies=[Depends(verify_api_key)])
async def get_acl():
    return {"text": _ACL_FILE.read_text() if _ACL_FILE.exists() else ""}


@router.post("/acl", dependencies=[Depends(verify_api_key)])
async def save_acl(body: TextBody):
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _ACL_FILE.write_text(body.text)
    _LOGGER.info("admin.acl_saved")
    return {"saved": True}


# ── Server controls ───────────────────────────────────────────────────────────

@router.post("/restart", dependencies=[Depends(verify_api_key)])
async def restart_server():
    _LOGGER.info("admin.restart_requested")

    async def _do_restart():
        await asyncio.sleep(0.5)  # let the HTTP response reach the client first
        subprocess.Popen(
            ["/usr/bin/sudo", "/usr/bin/systemctl", "restart", "avatar-backend"],
        )

    asyncio.create_task(_do_restart())
    return {"restarting": True}


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.get("/sessions", dependencies=[Depends(verify_api_key)])
async def list_sessions(request: Request):
    sm = request.app.state.session_manager
    return {"active_sessions": sm.active_count()}


@router.delete("/sessions/{session_id}", dependencies=[Depends(verify_api_key)])
async def clear_session(session_id: str, request: Request):
    await request.app.state.session_manager.clear(session_id)
    return {"cleared": session_id}


# ── Test announce ─────────────────────────────────────────────────────────────

class AnnounceBody(BaseModel):
    message: str
    priority: str = "normal"


@router.post("/announce/test", dependencies=[Depends(verify_api_key)])
async def test_announce(body: AnnounceBody, request: Request):
    from avatar_backend.routers.announce import AnnounceRequest, announce_handler
    return await announce_handler(AnnounceRequest(
        message=body.message, priority=body.priority,  # type: ignore[arg-type]
    ), request)


# ── Live logs (SSE) ───────────────────────────────────────────────────────────

@router.get("/logs")
async def stream_logs(request: Request, api_key: str = ""):
    from avatar_backend.config import get_settings
    if api_key != get_settings().api_key:
        from fastapi.responses import Response
        return Response(status_code=401)

    async def generate():
        # Backfill last 100 lines
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


# ── Avatar settings ────────────────────────────────────────────────────────────

_AVATAR_SETTINGS_FILE = _CONFIG_DIR / "avatar_settings.json"


@router.get("/avatar-settings")
async def get_avatar_settings():
    import json as _json
    if _AVATAR_SETTINGS_FILE.exists():
        return _json.loads(_AVATAR_SETTINGS_FILE.read_text())
    return {"skin_tone": 0, "avatar_url": ""}


class AvatarSettings(BaseModel):
    skin_tone: int = 0
    avatar_url: str = ""


@router.post("/avatar-settings", dependencies=[Depends(verify_api_key)])
async def save_avatar_settings(body: AvatarSettings):
    import json as _json
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AVATAR_SETTINGS_FILE.write_text(_json.dumps(body.model_dump()))
    _LOGGER.info("admin.avatar_settings_saved", skin_tone=body.skin_tone)
    return {"saved": True}
