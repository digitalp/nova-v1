"""Voice turn data-types, Protocol, adapters, and factory for realtime voice."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import WebSocket

from avatar_backend.services.conversation_service import PendingEventFollowupContext
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
ERROR = "error"

LLM_TIMEOUT_MSG = "I'm having trouble thinking right now. Please try again in a moment."
LLM_OFFLINE_MSG = "I can't reach my brain right now. Please check that Ollama is running."
_STREAMING_SENTENCE_RE = re.compile(r"^(.+?[.!?])(?:\s+|$)(.*)$", re.DOTALL)
_AUDIO_CACHE_TTL = 60


@dataclass
class VoiceTurnContext:
    ws: WebSocket
    ws_mgr: ConnectionManager
    session_id: str
    stt: STTService
    tts: TTSService
    speaker: SpeakerService | None
    app: Any
    container: Any = None


@dataclass
class VoiceSessionState:
    current_turn_id: int = 0
    current_task: asyncio.Task[None] | None = None
    pending_event_id: str | None = None
    pending_followup_prompt: str | None = None
    pending_speaker_name: str | None = None
    room_id: str | None = None
    input_stream_open: bool = False
    input_audio_chunks: list[bytes] = field(default_factory=list)
    input_audio_bytes: int = 0
    output_streaming_enabled: bool = False
    output_audio_format: str = "wav"
    state: str = IDLE
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Streaming STT: queue feeds audio chunks to transcribe_streaming
    stt_stream_queue: asyncio.Queue[bytes | None] | None = None
    stt_partial_text: str = ""


@dataclass
class VoiceTurnResult:
    text: str
    session_id: str
    tool_calls: list[Any]
    processing_time_ms: int | None = None


class RealtimeVoiceAdapter(Protocol):
    adapter_name: str
    provider_name: str
    supports_native_audio_input: bool
    supports_native_audio_output: bool
    supports_input_streaming: bool
    supports_output_streaming: bool
    supports_turn_context: bool
    supported_output_audio_formats: tuple[str, ...]

    async def transcribe(self, ctx: VoiceTurnContext, audio_bytes: bytes) -> str:
        ...

    async def run_turn(
        self,
        ctx: VoiceTurnContext,
        transcript: str,
        *,
        event_id: str | None = None,
        followup_prompt: str | None = None,
        speaker_name: str | None = None,
    ) -> VoiceTurnResult:
        ...

    async def synthesise_reply(
        self,
        ctx: VoiceTurnContext,
        reply_text: str,
    ) -> tuple[bytes, list[Any]]:
        ...


class DefaultRealtimeVoiceAdapter:
    adapter_name = "default_compat"
    provider_name = "local"
    supports_native_audio_input = False
    supports_native_audio_output = False
    supports_input_streaming = True
    supports_output_streaming = True
    supports_turn_context = True
    supported_output_audio_formats = ("wav", "pcm_s16le")

    async def transcribe(self, ctx: VoiceTurnContext, audio_bytes: bytes) -> str:
        return await ctx.stt.transcribe(audio_bytes)

    async def run_turn(
        self,
        ctx: VoiceTurnContext,
        transcript: str,
        *,
        event_id: str | None = None,
        followup_prompt: str | None = None,
        speaker_name: str | None = None,
        room_id: str | None = None,
    ) -> VoiceTurnResult:
        result = await _run_default_conversation_turn(
            ctx,
            transcript,
            event_id=event_id,
            followup_prompt=followup_prompt,
            speaker_name=speaker_name,
            room_id=room_id,
        )
        return VoiceTurnResult(
            text=result.text,
            session_id=result.session_id,
            tool_calls=list(result.tool_calls),
            processing_time_ms=result.processing_time_ms,
        )

    async def synthesise_reply(
        self,
        ctx: VoiceTurnContext,
        reply_text: str,
    ) -> tuple[bytes, list[Any]]:
        return await ctx.tts.synthesise_with_timing(reply_text)


async def _run_default_conversation_turn(
    ctx: VoiceTurnContext,
    transcript: str,
    *,
    event_id: str | None = None,
    followup_prompt: str | None = None,
    speaker_name: str | None = None,
    room_id: str | None = None,
) -> Any:
    if event_id:
        recent_events: dict[str, tuple[float, dict[str, Any]]] = getattr(
            ctx.container, "recent_event_contexts", {}
        )
        stored = recent_events.get(event_id)
        if stored:
            _, event_context = stored
            await ctx.container.conversation_service.set_event_followup_context(
                ctx.session_id,
                PendingEventFollowupContext(
                    event_type=str(event_context.get("event_type", "event")),
                    event_summary=str(event_context.get("event_summary", "")) or None,
                    event_context=dict(event_context.get("event_context", {})),
                    followup_prompt=followup_prompt,
                )
            )
    return await ctx.container.conversation_service.handle_voice_turn(
        session_id=ctx.session_id,
        user_text=transcript,
        speaker_name=speaker_name,
        room_id=room_id,
    )


class OpenAIChatRealtimeVoiceAdapter(DefaultRealtimeVoiceAdapter):
    adapter_name = "openai_chat_compat"
    provider_name = "openai"


class GoogleChatRealtimeVoiceAdapter(DefaultRealtimeVoiceAdapter):
    adapter_name = "google_chat_compat"
    provider_name = "google"


class AnthropicChatRealtimeVoiceAdapter(DefaultRealtimeVoiceAdapter):
    adapter_name = "anthropic_chat_compat"
    provider_name = "anthropic"


def create_realtime_voice_adapter(settings: Any) -> RealtimeVoiceAdapter:
    provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
    if provider == "openai" and str(getattr(settings, "openai_api_key", "") or "").strip():
        return OpenAIChatRealtimeVoiceAdapter()
    if provider == "google" and str(getattr(settings, "google_api_key", "") or "").strip():
        return GoogleChatRealtimeVoiceAdapter()
    if provider == "anthropic" and str(getattr(settings, "anthropic_api_key", "") or "").strip():
        return AnthropicChatRealtimeVoiceAdapter()
    return DefaultRealtimeVoiceAdapter()
