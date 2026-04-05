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

import secrets

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
    existing.update({k: v for k, v in body.values.items() if v != "" and k in _CONFIG_FIELDS})
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
    if not api_key or not secrets.compare_digest(api_key.encode(), get_settings().api_key.encode()):
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


@router.get("/avatar-settings", dependencies=[Depends(verify_api_key)])
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


# ── Prompt sync ───────────────────────────────────────────────────────────────

# Domains worth tracking for prompt enrichment
_SYNC_DOMAINS = {
    "sensor", "binary_sensor", "light", "switch", "climate",
    "media_player", "lock", "cover", "input_boolean", "input_select",
    "input_number", "input_text", "person", "camera", "fan",
    "vacuum", "humidifier", "water_heater", "number",
}

# Prefixes that indicate noise/infrastructure entities to skip
_SKIP_PREFIXES = (
    "update.", "system.", "device_tracker.unifi_", "device_tracker.unknown_",
    "sensor.sun_", "sensor.moon_",
)

# These friendly-name substrings usually indicate internal/debug entities
_SKIP_NAME_FRAGMENTS = ("rssi", "lqi", "linkquality", "uptime", "firmware", "version", "reboot")


def _extract_known_entity_ids(prompt_text: str) -> set[str]:
    """Return all entity_id-like strings found anywhere in the prompt."""
    import re
    return set(re.findall(r'\b\w+\.\w[\w_]*', prompt_text))


def _summarise_new_entities(states: list[dict], known: set[str]) -> str:
    """
    Filter states down to meaningful unknown entities and return a compact
    grouped summary string to feed the LLM.
    """
    from collections import defaultdict
    groups: dict[str, list[str]] = defaultdict(list)

    for s in states:
        eid = s["entity_id"]
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
        unit = s["attributes"].get("unit_of_measurement", "")
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
        parts.extend(groups[domain][:40])  # cap per domain to avoid token explosion
    return "\n".join(parts)


class SyncPromptResponse(BaseModel):
    status: str
    new_entities_found: int
    prompt_updated: bool
    summary: str


@router.post(
    "/sync-prompt",
    response_model=SyncPromptResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Discover new HA entities and integrate them into the system prompt",
)
async def sync_prompt(request: Request):
    """
    1. Fetches all current HA entity states
    2. Diffs against entity IDs already referenced in the system prompt
    3. If new meaningful entities are found, calls the LLM to integrate them
    4. Saves the updated prompt and reloads the session manager
    """
    import httpx as _httpx
    import json as _json

    ha = request.app.state.ha_proxy
    llm = request.app.state.llm_service
    sm = request.app.state.session_manager

    _LOGGER.info("sync_prompt.started")

    # 1. Fetch all states from HA
    try:
        async with _httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{ha._ha_url}/api/states",
                headers={"Authorization": ha._headers["Authorization"]},
            )
            resp.raise_for_status()
            all_states: list[dict] = resp.json()
    except Exception as exc:
        _LOGGER.error("sync_prompt.ha_fetch_failed", exc=str(exc))
        raise HTTPException(status_code=503, detail=f"Could not fetch HA states: {exc}")

    # 2. Diff against current prompt
    current_prompt = _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""
    known = _extract_known_entity_ids(current_prompt)
    new_summary = _summarise_new_entities(all_states, known)

    if not new_summary:
        _LOGGER.info("sync_prompt.no_new_entities")
        return SyncPromptResponse(
            status="ok",
            new_entities_found=0,
            prompt_updated=False,
            summary="No new entities found — system prompt is up to date.",
        )

    new_count = new_summary.count("\n  ")  # rough count of entity lines
    _LOGGER.info("sync_prompt.new_entities_found", count=new_count)

    # 3. Ask LLM to integrate new entities into the prompt
    integration_request = (
        "You are updating the system prompt for Nova, an AI home automation controller.\n\n"
        "Here is the current system prompt:\n"
        "```\n" + current_prompt + "\n```\n\n"
        "The following new Home Assistant entities have been discovered that are not yet "
        "referenced in the system prompt:\n\n"
        + new_summary + "\n\n"
        "Instructions:\n"
        "- Add these entities to the appropriate existing sections of the system prompt.\n"
        "- If an entity clearly belongs in an existing section (e.g. a new sensor in the "
        "Car section, a new climate entity in Heating), add it there.\n"
        "- If a group of new entities represent a genuinely new capability or room, "
        "create a minimal new section.\n"
        "- Skip entities that are clearly noise (network infrastructure, debug sensors, "
        "duplicate trackers, internal HA helpers with no user value).\n"
        "- Preserve the exact structure, tone, and formatting of the original prompt.\n"
        "- Return ONLY the complete updated system prompt — no explanation, no markdown "
        "fences, no preamble."
    )

    try:
        updated_prompt = await llm.generate_text(integration_request, timeout_s=180.0)
    except Exception as exc:
        _LOGGER.error('sync_prompt.llm_failed', exc=str(exc))
        raise HTTPException(status_code=503, detail=f'LLM call failed: {exc}')

    if not updated_prompt or len(updated_prompt) < len(current_prompt) // 2:
        _LOGGER.warning("sync_prompt.llm_response_too_short", chars=len(updated_prompt))
        raise HTTPException(status_code=500, detail="LLM returned an unexpectedly short response — prompt not saved.")

    # 4. Save and reload
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _PROMPT_FILE.write_text(updated_prompt)
    _LOGGER.info("sync_prompt.saved", chars=len(updated_prompt))

    # Reload session manager so new sessions pick up the updated prompt
    from avatar_backend.services.session_manager import SessionManager
    request.app.state.session_manager = SessionManager(updated_prompt)
    _LOGGER.info("sync_prompt.session_manager_reloaded")

    # Keep proactive monitor in sync with the new prompt
    proactive = getattr(request.app.state, "proactive_service", None)
    if proactive is not None:
        proactive.update_system_prompt(updated_prompt)
        _LOGGER.info("sync_prompt.proactive_updated")

    return SyncPromptResponse(
        status="ok",
        new_entities_found=new_count,
        prompt_updated=True,
        summary=f"Integrated {new_count} new entities into the system prompt.",
    )
