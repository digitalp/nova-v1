"""
Shared constants, helpers, Pydantic models, and singletons for the admin sub-routers.
"""
from __future__ import annotations
import structlog
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from avatar_backend.services.open_loop_service import OpenLoopService
from avatar_backend.runtime_paths import config_dir, env_file, install_dir, logs_dir, static_dir

_LOGGER = structlog.get_logger()

# ── Path constants ────────────────────────────────────────────────────────────

_INSTALL_DIR  = install_dir()
_CONFIG_DIR   = config_dir()
_ENV_FILE     = env_file()
_PROMPT_FILE  = _CONFIG_DIR / "system_prompt.txt"
_ACL_FILE     = _CONFIG_DIR / "acl.yaml"
_LOG_FILE     = logs_dir() / "avatar-backend.log"
_STATIC_DIR   = static_dir()
_COOKIE_NAME  = "nova_session"

# ── Singleton ─────────────────────────────────────────────────────────────────

_OPEN_LOOP_SERVICE = OpenLoopService()

# ── Config fields ─────────────────────────────────────────────────────────────

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
    # ── Motion vision ──
    "MOTION_VISION_PROVIDER":             ("Motion Vision Provider (gemini/ollama/ollama_remote)",  False),

    "OLLAMA_VISION_MODEL":                ("Ollama Vision Model",                              False),
    # ── Heating ──
    "HEATING_LLM_PROVIDER":               ("Heating Tool Call Provider (gemini/ollama)",       False),
    "HEATING_SHADOW_ENABLED":             ("Enable Heating Shadow Evaluation (true/false)",    False),
    # ── Integrations ──
    "BLUEIRIS_URL":                       ("Blue Iris URL (e.g. http://192.168.0.33:81)",       False),
    "CODEPROJECT_AI_URL":                 ("CodeProject.AI URL for face recognition",           False),
    "MUSIC_ASSISTANT_URL":                ("Music Assistant URL",                                False),
}

# ── Prompt registry ───────────────────────────────────────────────────────────

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


# ── Session helpers ───────────────────────────────────────────────────────────

def _get_session(request: Request) -> dict | None:
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return None
    users: "UserService" = request.app.state._container.user_service
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


# ── Pydantic models ──────────────────────────────────────────────────────────

class TextBody(BaseModel):
    text: str


class LoginBody(BaseModel):
    username: str
    password: str


class CreateUserBody(BaseModel):
    username: str
    password: str
    role:     Literal["admin", "viewer"] = "viewer"


class ChangePasswordBody(BaseModel):
    new_password: str


class ChangeRoleBody(BaseModel):
    role: Literal["admin", "viewer"]


class ConfigUpdate(BaseModel):
    values: dict[str, str]


class PromptUpdateBody(BaseModel):
    text: str


class SpeakerPrefsBody(BaseModel):
    speakers: list[dict]


class MemoryBody(BaseModel):
    summary: str
    category: str = "general"
    confidence: float = 0.9
    pinned: bool = False


class MotionClipSearchBody(BaseModel):
    query: str = ""
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    camera_entity_id: str | None = None
    canonical_event_type: str | None = None


class BulkDeleteBody(BaseModel):
    ids: list[int] | None = None
    delete_all: bool = False


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


class AvatarSettings(BaseModel):
    skin_tone: int = -1     # -1 = use GLB default, 0-9 = preset index
    hair_color: int = -1    # -1 = use GLB default, 0-9 = preset index
    avatar_url: str = ""
    bg_type: str = ""        # "color" | "image" | ""
    bg_color: str = ""       # hex color e.g. "#1a1a2e"
    bg_image_url: str = ""   # URL for background image


class SyncPromptResponse(BaseModel):
    status:             str
    new_entities_found: int
    prompt_updated:     bool
    summary:            str


class MusicControlBody(BaseModel):
    entity_id: str
    action: str  # play, pause, stop, next, previous, volume, mute, unmute
    value: float | str | None = None
