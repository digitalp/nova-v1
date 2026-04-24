"""Config sub-router: GET/POST /config, /reload-config, /ollama-models, /prompts/*, /prompt, /acl, /speakers."""
from __future__ import annotations

import asyncio
import structlog

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.services.gemini_key_pool import load_pool_from_settings

from .common import (
    _CONFIG_DIR,
    _CONFIG_FIELDS,
    _ENV_FILE,
    _PROMPT_FILE,
    _ACL_FILE,
    _PROMPT_REGISTRY,
    _get_session,
    _require_session,
    TextBody,
    ConfigUpdate,
    PromptUpdateBody,
    SpeakerPrefsBody,
)

_LOGGER = structlog.get_logger()
router = APIRouter()


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


@router.post("/config")
async def save_config(body: ConfigUpdate, request: Request, container: AppContainer = Depends(get_container)):
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
        if k in _CONFIG_FIELDS and v.strip() != ""
    }
    existing.update(sanitized)
    # Ensure we preserve the order of fields from _CONFIG_FIELDS or existing
    lines = header_lines
    for k, v in existing.items():
        lines.append(f"{k}={v}")
    _ENV_FILE.write_text("\n".join(lines) + "\n")
    _LOGGER.info("admin.config_saved")

    from avatar_backend.config import get_settings
    from avatar_backend.services.tts_service import create_tts_service
    get_settings.cache_clear()
    new_settings = get_settings()
    new_tts = create_tts_service(new_settings)
    container.tts_service = new_tts
    _LOGGER.info("admin.tts_reloaded", provider=new_settings.tts_provider)

    # Reload Gemini Key Pool
    if hasattr(container, "gemini_key_pool"):
        load_pool_from_settings(container.gemini_key_pool, new_settings)
        _LOGGER.info("admin.gemini_pool_reloaded", size=container.gemini_key_pool.size)

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


@router.post("/reload-config")
async def reload_config(request: Request, container: AppContainer = Depends(get_container)):
    """Hot-reload .env config without restarting the server."""
    _require_session(request, min_role="admin")
    from avatar_backend.config import get_settings, Settings
    from avatar_backend.services.tts_service import create_tts_service
    from avatar_backend.services.tts_fallback import FallbackTTSService
    from avatar_backend.services.tts_service import PiperTTSService
    from pydantic import ValidationError

    old = get_settings()
    old_dict = old.model_dump()

    get_settings.cache_clear()
    try:
        new = get_settings()
    except (ValidationError, Exception) as exc:
        get_settings.cache_clear()
        get_settings()  # force re-read; if .env is broken this will also fail
        return JSONResponse(
            {"reloaded": False, "error": str(exc)},
            status_code=422,
        )

    new_dict = new.model_dump()
    changed = [k for k in new_dict if new_dict[k] != old_dict.get(k)]

    if "tts_provider" in changed or "piper_voice" in changed:
        primary = create_tts_service(new)
        container.tts_service = FallbackTTSService(
            primary=primary, fallbacks=[PiperTTSService(new.piper_voice)]
        )

    if "session_rate_limit_max" in changed or "session_rate_limit_window_s" in changed:
        limiter = getattr(container, "session_limiter", None)
        if limiter:
            limiter.update_config(new.session_rate_limit_max, new.session_rate_limit_window_s)

    if hasattr(container, "gemini_key_pool") and any(
        key in changed for key in ("google_api_key", "google_api_key_enabled", "gemini_api_keys")
    ):
        load_pool_from_settings(container.gemini_key_pool, new)
        _LOGGER.info("admin.gemini_pool_reloaded", size=container.gemini_key_pool.size)

    return {"reloaded": True, "changed_keys": changed}


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


# ── Prompt management ──────────────────────────────────────────────────────────

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


@router.get("/prompts/{slug}")
async def get_prompt_by_slug(slug: str, request: Request):
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
async def save_prompt_by_slug(slug: str, body: PromptUpdateBody, request: Request):
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

@router.get("/speakers")
async def get_speakers(request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = getattr(container, "speaker_service", None)
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
async def save_speakers(body: SpeakerPrefsBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="admin")
    svc = getattr(container, "speaker_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Speaker service not available")
    svc.set_speaker_preferences(body.speakers)
    _LOGGER.info("admin.speakers_saved", count=len(body.speakers))
    return {"saved": True}


# ── System prompt (legacy) ────────────────────────────────────────────────────

@router.get("/prompt")
async def get_legacy_prompt(request: Request):
    _require_session(request)
    return {"text": _PROMPT_FILE.read_text() if _PROMPT_FILE.exists() else ""}


@router.post("/prompt")
async def save_legacy_prompt(body: TextBody, request: Request):
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
