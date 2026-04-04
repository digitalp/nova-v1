from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="/opt/avatar-server/.env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Security
    api_key: str

    # Home Assistant
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""

    # LLM provider: ollama | openai | google | anthropic
    llm_provider: str = "ollama"

    # Ollama (used when llm_provider=ollama)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b-instruct-q4_K_M"

    # Cloud LLM — API keys and model name (used when llm_provider != ollama)
    openai_api_key: str = ""
    google_api_key: str = ""
    anthropic_api_key: str = ""
    cloud_model: str = ""

    # STT / TTS
    whisper_model: str = "small"
    piper_voice: str = "en_US-lessac-medium"

    # TTS provider: piper | elevenlabs
    tts_provider: str = "piper"

    # ElevenLabs TTS
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # Rachel (default)
    elevenlabs_model: str = "eleven_monolingual_v1"

    # AfroTTS (local Kokoro engine)
    afrotts_voice: str = "af_heart"   # af_heart af_nicole af_sarah af_sky am_adam am_michael bf_emma bf_isabella bm_george bm_lewis
    afrotts_speed: float = 1.0

    # Public URL of this server (used to serve TTS audio to non-Alexa media players)
    # e.g. http://192.168.0.249:8001  — leave blank to use HA TTS engine instead
    public_url: str = "http://192.168.0.249:8001"

    # Speakers — comma-separated HA media_player entity IDs
    speakers: str = ""

    # TTS engine used for non-Alexa speakers (must be a tts.* entity in HA)
    tts_engine: str = "tts.google_translate_en_com"

    @property
    def speaker_list(self) -> list[str]:
        return [s.strip() for s in self.speakers.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
