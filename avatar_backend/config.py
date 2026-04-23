import os
from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = os.environ.get(
    "NOVA_ENV_FILE",
    os.environ.get("NOVA_APP_ROOT", "/opt/avatar-server") + "/.env",
)
_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=_ENV_FILE,
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class Settings(BaseSettings):
    model_config = _SETTINGS_CONFIG

    # ── Server ────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    api_key: str
    public_url: str = ""
    cors_origins: str = ""
    speaker_audio_offset_ms: int = 0

    # ── Home Assistant ────────────────────────────────────────────────────
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""
    ha_local_url: str = ""  # Direct LAN URL — skips TLS/DNS for internal calls
    ha_power_alert_cooldown_s: int = 1800

    # ── LLM ───────────────────────────────────────────────────────────────
    llm_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b-instruct-q4_K_M"
    ollama_vision_model: str = "llama3.2-vision:11b-instruct-q4_K_M"
    ollama_vision_url: str = ""
    ollama_local_text_model: str = ""
    proactive_ollama_model: str = ""
    sensor_watch_ollama_model: str = ""
    sensor_watch_review_timeout_s: float = 120.0
    openai_api_key: str = ""
    google_api_key: str = ""
    google_api_key_enabled: bool = True
    gemini_api_keys: str = ""  # Comma-separated pool of Gemini API keys for vision rotation
    gemini_camera_pins: str = ""  # Comma-separated camera_id|raw_key pin mappings
    anthropic_api_key: str = ""
    cloud_model: str = ""

    # ── TTS ────────────────────────────────────────────────────────────────
    tts_provider: str = "piper"
    whisper_model: str = "small"
    piper_voice: str = "en_US-lessac-medium"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model: str = "eleven_monolingual_v1"
    afrotts_voice: str = "af_heart"
    afrotts_speed: float = 1.0
    intron_afro_tts_url: str = "http://127.0.0.1:8021"
    intron_afro_tts_timeout_s: float = 90.0
    intron_afro_tts_reference_wav: str = ""
    intron_afro_tts_language: str = "en"

    # ── Speakers ───────────────────────────────────────────────────────────
    speakers: str = ""
    tts_engine: str = "tts.google_translate_en_com"

    # ── Motion / Vision ────────────────────────────────────────────────────
    motion_clip_duration_s: int = 8
    motion_clip_search_candidates: int = 120
    motion_clip_search_results: int = 24
    motion_clip_retention_days: int = 30
    motion_vision_provider: str = "gemini"
    shared_memory_db_path: str = ""

    # ── Heating ────────────────────────────────────────────────────────────
    heating_llm_provider: str = "gemini"
    heating_shadow_enabled: bool = True

    # ── Proactive ──────────────────────────────────────────────────────────
    proactive_entity_cooldown_s: int = 600
    proactive_camera_cooldown_s: int = 600
    proactive_camera_capture_cooldown_s: int = 60
    proactive_global_motion_cooldown_s: int = 600
    proactive_global_announce_cooldown_s: int = 300
    proactive_queue_dedup_cooldown_s: int = 120
    proactive_batch_window_s: int = 60
    proactive_max_batch_changes: int = 20
    proactive_weather_cooldown_s: int = 3600
    proactive_forecast_hour: int = 7

    # ── Rate Limiting ──────────────────────────────────────────────────────
    session_rate_limit_max: int = 30
    session_rate_limit_window_s: int = 60

    # ── Music ──────────────────────────────────────────────────────────────
    music_assistant_url: str = "http://localhost:8095"

    # ── Blue Iris ─────────────────────────────────────────────────────────
    blueiris_url: str = ""  # e.g. http://192.168.0.33:81
    blueiris_user: str = ""
    blueiris_password: str = ""

    # ── Face Recognition ──────────────────────────────────────────────────
    codeproject_ai_url: str = ""  # e.g. http://192.168.0.33:32168
    scoreboard_enabled: bool = True

    # ── Cross-field validators ─────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_llm_provider(self):
        p = self.llm_provider.lower()
        if p not in ("ollama", "openai", "google", "anthropic"):
            raise ValueError(f"LLM_PROVIDER must be ollama/openai/google/anthropic, got '{p}'")
        key_map = {"openai": self.openai_api_key, "google": self.google_api_key, "anthropic": self.anthropic_api_key}
        if p != "ollama" and not key_map.get(p):
            raise ValueError(f"LLM_PROVIDER={p} requires {p.upper()}_API_KEY to be set")
        return self

    @model_validator(mode="after")
    def _validate_tts_provider(self):
        p = self.tts_provider.lower()
        valid = ("piper", "elevenlabs", "afrotts", "intron_afro_tts")
        if p not in valid:
            raise ValueError(f"TTS_PROVIDER must be one of {valid}, got '{p}'")
        if p == "elevenlabs" and not self.elevenlabs_api_key:
            raise ValueError("TTS_PROVIDER=elevenlabs requires ELEVENLABS_API_KEY")
        return self

    @model_validator(mode="after")
    def _validate_motion_vision(self):
        p = (self.motion_vision_provider or "").lower()
        if p and p not in ("gemini", "ollama", "ollama_remote"):
            raise ValueError(f"MOTION_VISION_PROVIDER must be gemini/ollama/ollama_remote, got '{p}'")
        return self

    @model_validator(mode="after")
    def _validate_proactive_timing(self):
        if self.proactive_forecast_hour < 0 or self.proactive_forecast_hour > 23:
            raise ValueError(f"PROACTIVE_FORECAST_HOUR must be 0-23, got {self.proactive_forecast_hour}")
        return self

    # ── Computed properties ─────────────────────────────────────────────────

    @property
    def ha_local_url_resolved(self) -> str:
        """Return HA_LOCAL_URL if set, otherwise fall back to HA_URL."""
        return self.ha_local_url.rstrip("/") if self.ha_local_url else self.ha_url.rstrip("/")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def speaker_list(self) -> list[str]:
        return [s.strip() for s in self.speakers.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
