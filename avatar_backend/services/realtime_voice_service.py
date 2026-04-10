from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
import wave
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from fastapi import WebSocket

from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager
from avatar_backend.services.conversation_service import PendingEventFollowupContext

_LOGGER = structlog.get_logger()

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


@dataclass
class VoiceSessionState:
    current_turn_id: int = 0
    current_task: asyncio.Task[None] | None = None
    pending_event_id: str | None = None
    pending_followup_prompt: str | None = None
    input_stream_open: bool = False
    input_audio_chunks: list[bytes] = field(default_factory=list)
    input_audio_bytes: int = 0
    output_streaming_enabled: bool = False
    output_audio_format: str = "wav"
    state: str = IDLE
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    ) -> VoiceTurnResult:
        result = await _run_default_conversation_turn(
            ctx,
            transcript,
            event_id=event_id,
            followup_prompt=followup_prompt,
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
) -> Any:
    if event_id:
        recent_events: dict[str, tuple[float, dict[str, Any]]] = getattr(
            ctx.app.state, "recent_event_contexts", {}
        )
        stored = recent_events.get(event_id)
        if stored:
            _, event_context = stored
            await ctx.app.state.conversation_service.set_event_followup_context(
                ctx.session_id,
                PendingEventFollowupContext(
                    event_type=str(event_context.get("event_type", "event")),
                    event_summary=str(event_context.get("event_summary", "")) or None,
                    event_context=dict(event_context.get("event_context", {})),
                    followup_prompt=followup_prompt,
                )
            )
    return await ctx.app.state.conversation_service.handle_voice_turn(
        session_id=ctx.session_id,
        user_text=transcript,
    )


class RealtimeVoiceService:
    """Compatibility-first voice turn orchestrator for the websocket voice path.

    This service preserves the existing request/response websocket contract while
    extracting orchestration out of the router so future interruption-aware and
    streaming behavior can be introduced behind a stable interface.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, VoiceSessionState] = {}
        self._sessions_lock = asyncio.Lock()
        self._default_adapter = DefaultRealtimeVoiceAdapter()

    async def connect_session(self, session_key: str) -> None:
        await self._get_or_create_session(session_key)

    async def disconnect_session(self, session_key: str) -> None:
        async with self._sessions_lock:
            session = self._sessions.pop(session_key, None)
        if session and session.current_task:
            await self._cancel_turn_task(session.current_task)

    async def start_audio_turn(
        self,
        session_key: str,
        ctx: VoiceTurnContext,
        audio_bytes: bytes,
    ) -> None:
        session = await self._get_or_create_session(session_key)
        async with session.lock:
            if session.current_task:
                await self._send_turn_interrupted(ctx.ws, session.current_turn_id)
                await self._cancel_turn_task(session.current_task)
            session.current_turn_id += 1
            turn_id = session.current_turn_id
            event_id = session.pending_event_id
            followup_prompt = session.pending_followup_prompt
            session.pending_event_id = None
            session.pending_followup_prompt = None
            await self._send_json(ctx.ws, {"type": "turn_started"}, turn_id=turn_id)
            session.current_task = asyncio.create_task(
                self.process_audio(
                    ctx,
                    audio_bytes,
                    session_key=session_key,
                    turn_id=turn_id,
                    event_id=event_id,
                    followup_prompt=followup_prompt,
                )
            )

    async def handle_text_frame(self, ws: WebSocket, session_key: str, raw_text: str) -> bool:
        try:
            data = json.loads(raw_text)
        except Exception:
            return False

        if data.get("type") == "ping":
            await ws.send_text(json.dumps({"type": "pong"}))
            return True

        if data.get("type") == "turn_context":
            adapter = self._resolve_adapter_for_ws(ws)
            if not getattr(adapter, "supports_turn_context", True):
                await self._send_json(ws, {
                    "type": "error",
                    "detail": "This voice adapter does not support turn context.",
                })
                return True
            event_id = str(data.get("event_id") or "").strip()
            followup_prompt = str(data.get("followup_prompt") or "").strip()
            session = await self._get_or_create_session(session_key)
            session.pending_event_id = event_id or None
            session.pending_followup_prompt = followup_prompt or None
            await self._send_json(ws, {
                "type": "turn_context_ack",
                "event_id": session.pending_event_id,
                "followup_prompt": session.pending_followup_prompt,
            })
            return True

        if data.get("type") == "client_capabilities":
            adapter = self._resolve_adapter_for_ws(ws)
            session = await self._get_or_create_session(session_key)
            capabilities = data if isinstance(data, dict) else {}
            metadata = capabilities.get("client_metadata") or {}
            session.output_streaming_enabled = bool(capabilities.get("output_streaming")) and bool(
                getattr(adapter, "supports_output_streaming", True)
            )
            supported_formats = tuple(getattr(adapter, "supported_output_audio_formats", ("wav", "pcm_s16le")))
            output_audio_format = str(capabilities.get("output_audio_format") or "wav").strip().lower()
            default_output_audio_format = supported_formats[0] if supported_formats else "wav"
            session.output_audio_format = (
                output_audio_format if output_audio_format in supported_formats else default_output_audio_format
            )
            app = getattr(ws, "app", None)
            session_id = getattr(ws, "_nova_session_id", "")
            if app is not None and session_id and isinstance(metadata, dict):
                session_manager = getattr(app.state, "session_manager", None)
                if session_manager is not None:
                    await session_manager.set_metadata(session_id, metadata)
            await self._send_json(ws, {
                "type": "client_capabilities_ack",
                "output_streaming": session.output_streaming_enabled,
                "output_audio_format": session.output_audio_format,
            })
            return True

        if data.get("type") == "input_audio_start":
            adapter = self._resolve_adapter_for_ws(ws)
            if not getattr(adapter, "supports_input_streaming", True):
                await self._send_json(ws, {
                    "type": "error",
                    "detail": "This voice adapter does not support streamed input.",
                })
                return True
            session = await self._get_or_create_session(session_key)
            async with session.lock:
                session.input_stream_open = True
                session.input_audio_chunks = []
                session.input_audio_bytes = 0
            await self._send_json(ws, {"type": "input_audio_start_ack"})
            return True

        if data.get("type") == "input_audio_commit":
            session = await self._get_or_create_session(session_key)
            async with session.lock:
                if not session.input_stream_open:
                    await self._send_json(ws, {
                        "type": "error",
                        "detail": "No input audio stream is active.",
                    })
                    return True
                audio_bytes = b"".join(session.input_audio_chunks)
                buffered_bytes = session.input_audio_bytes
                session.input_stream_open = False
                session.input_audio_chunks = []
                session.input_audio_bytes = 0
            await self._send_json(ws, {
                "type": "input_audio_commit_ack",
                "byte_length": buffered_bytes,
            })
            ctx = self._extract_turn_context(ws)
            if ctx is None:
                await self._send_json(ws, {
                    "type": "error",
                    "detail": "Voice session context is unavailable.",
                })
                return True
            await self.start_audio_turn(session_key, ctx, audio_bytes)
            return True

        if data.get("type") == "input_audio_cancel":
            session = await self._get_or_create_session(session_key)
            async with session.lock:
                had_stream = session.input_stream_open
                session.input_stream_open = False
                session.input_audio_chunks = []
                session.input_audio_bytes = 0
            await self._send_json(ws, {
                "type": "input_audio_cancel_ack",
                "active": had_stream,
            })
            return True

        return False

    async def send_pong_if_needed(self, ws: WebSocket, raw_text: str) -> bool:
        """Backward-compatible keepalive helper for older websocket routers."""
        return await self.handle_text_frame(ws, "legacy_voice_socket", raw_text)

    async def handle_binary_frame(
        self,
        session_key: str,
        ctx: VoiceTurnContext,
        audio_bytes: bytes,
    ) -> None:
        session = await self._get_or_create_session(session_key)
        async with session.lock:
            if session.input_stream_open:
                session.input_audio_chunks.append(audio_bytes)
                session.input_audio_bytes += len(audio_bytes)
                return
        await self.start_audio_turn(session_key, ctx, audio_bytes)

    async def process_audio(
        self,
        ctx: VoiceTurnContext,
        audio_bytes: bytes,
        *,
        session_key: str | None = None,
        turn_id: int | None = None,
        event_id: str | None = None,
        followup_prompt: str | None = None,
    ) -> None:
        speaker_task: asyncio.Task[None] | None = None
        finish_reason = "completed"
        finish_sent = False
        adapter = self._resolve_adapter(ctx)
        try:
            try:
                await self._send_state(ctx.ws, ctx.ws_mgr, LISTENING, session_key=session_key)
                transcript = await adapter.transcribe(ctx, audio_bytes)
            except Exception as exc:
                _LOGGER.error("voice_ws.stt_error", exc=str(exc))
                await self._send_json(ctx.ws, {"type": "error", "detail": f"STT failed: {exc}"}, turn_id=turn_id)
                await self._send_state(ctx.ws, ctx.ws_mgr, ERROR, session_key=session_key)
                await asyncio.sleep(1)
                await self._send_state(ctx.ws, ctx.ws_mgr, IDLE, session_key=session_key, turn_id=turn_id)
                finish_reason = "stt_error"
                await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)
                finish_sent = True
                return

            if not transcript:
                _LOGGER.info("voice_ws.empty_transcript")
                await self._send_state(ctx.ws, ctx.ws_mgr, IDLE, session_key=session_key, turn_id=turn_id)
                finish_reason = "empty_transcript"
                await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)
                finish_sent = True
                return

            if session_key and not await self._is_current_turn(session_key, turn_id):
                finish_reason = "superseded"
                await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)
                finish_sent = True
                return

            await self._send_json(ctx.ws, {"type": "transcript", "text": transcript}, turn_id=turn_id)
            _LOGGER.info("voice_ws.transcript", chars=len(transcript), text=transcript[:80])

            await self._send_state(ctx.ws, ctx.ws_mgr, THINKING, session_key=session_key, turn_id=turn_id)
            fallback_text = None
            result = None

            try:
                result = await adapter.run_turn(
                    ctx,
                    transcript,
                    event_id=event_id,
                    followup_prompt=followup_prompt,
                )
            except RuntimeError as exc:
                err = str(exc)
                _LOGGER.error("voice_ws.llm_error", exc=err)
                if "timed out" in err.lower():
                    fallback_text = LLM_TIMEOUT_MSG
                elif "400" in err and "bad request" in err.lower():
                    _LOGGER.warning("voice_ws.clearing_corrupt_session", session_id=ctx.session_id)
                    await ctx.app.state.conversation_service.clear_session_state(ctx.session_id)
                    try:
                        result = await adapter.run_turn(
                            ctx,
                            transcript,
                            event_id=event_id,
                            followup_prompt=followup_prompt,
                        )
                    except Exception as retry_exc:
                        _LOGGER.error("voice_ws.llm_retry_failed", exc=str(retry_exc))
                        fallback_text = LLM_OFFLINE_MSG
                else:
                    fallback_text = LLM_OFFLINE_MSG
            except Exception as exc:
                _LOGGER.error("voice_ws.llm_error", exc=str(exc))
                fallback_text = LLM_OFFLINE_MSG

            reply_text = fallback_text if fallback_text else (result.text if result else "")
            if session_key and not await self._is_current_turn(session_key, turn_id):
                finish_reason = "superseded"
                await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)
                finish_sent = True
                return

            if result and not fallback_text:
                await self._send_json(ctx.ws, {
                    "type": "response",
                    "text": reply_text,
                    "session_id": result.session_id,
                    "tool_calls": [tc.model_dump() for tc in result.tool_calls],
                    "processing_ms": result.processing_time_ms,
                }, turn_id=turn_id)

            if reply_text:
                await self._send_state(ctx.ws, ctx.ws_mgr, SPEAKING, session_key=session_key, turn_id=turn_id)
                try:
                    from avatar_backend.config import get_settings as _get_settings
                    _settings = _get_settings()
                    offset_s = _settings.speaker_audio_offset_ms / 1000.0
                    wav_bytes, word_timings = await adapter.synthesise_reply(ctx, reply_text)
                    if session_key and not await self._is_current_turn(session_key, turn_id):
                        finish_reason = "superseded"
                        await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)
                        finish_sent = True
                        return
                    if ctx.speaker and ctx.speaker.is_configured:
                        public_url = (_settings.public_url or "").rstrip("/")
                        if public_url:
                            token = uuid.uuid4().hex
                            expiry = time.time() + _AUDIO_CACHE_TTL
                            cache = ctx.app.state.audio_cache
                            expired = [k for k, (_, exp) in cache.items() if time.time() > exp]
                            for k in expired:
                                cache.pop(k, None)
                            cache[token] = (wav_bytes, expiry)
                            audio_url = f"{public_url}/tts/audio/{token}"
                            speaker_task = asyncio.create_task(ctx.speaker.speak_wav(reply_text, audio_url, area_aware=True))
                        else:
                            speaker_task = asyncio.create_task(ctx.speaker.speak(reply_text, area_aware=True))

                    if offset_s > 0 and speaker_task is not None:
                        await asyncio.sleep(offset_s)

                    await self._send_json(ctx.ws, {
                        "type": "audio_start",
                        "byte_length": len(wav_bytes),
                    }, turn_id=turn_id)
                    await self._send_json(ctx.ws, {
                        "type": "word_timings",
                        "word_timings": word_timings,
                    }, turn_id=turn_id)
                    await self._send_audio_output(ctx.ws, session_key, wav_bytes, turn_id=turn_id)

                    if speaker_task is not None:
                        await speaker_task
                except Exception as exc:
                    _LOGGER.error("voice_ws.tts_error", exc=str(exc))
                    await self._send_json(ctx.ws, {"type": "error", "detail": f"TTS failed: {exc}"}, turn_id=turn_id)
                    finish_reason = "tts_error"

            await self._send_state(ctx.ws, ctx.ws_mgr, IDLE, session_key=session_key, turn_id=turn_id)
            await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)
            finish_sent = True
            return
        except asyncio.CancelledError:
            if speaker_task is not None:
                speaker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await speaker_task
            finish_reason = "interrupted"
            raise
        finally:
            if not finish_sent and finish_reason == "interrupted":
                await self._finish_turn(ctx.ws, session_key, turn_id, finish_reason)

    async def send_initial_state(self, ws: WebSocket, ws_mgr: ConnectionManager) -> None:
        await self._send_state(ws, ws_mgr, IDLE)
        adapter = self._resolve_adapter_for_ws(ws)
        await self._send_json(ws, {
            "type": "voice_capabilities",
            "input_streaming": bool(getattr(adapter, "supports_input_streaming", True)),
            "output_streaming": bool(getattr(adapter, "supports_output_streaming", True)),
            "output_audio_formats": list(getattr(adapter, "supported_output_audio_formats", ("wav", "pcm_s16le"))),
            "turn_context": bool(getattr(adapter, "supports_turn_context", True)),
            "realtime_adapter": getattr(adapter, "adapter_name", "default_compat"),
            "realtime_provider": getattr(adapter, "provider_name", "local"),
            "native_audio_input": bool(getattr(adapter, "supports_native_audio_input", False)),
            "native_audio_output": bool(getattr(adapter, "supports_native_audio_output", False)),
        })

    async def _send_wav(self, ws: WebSocket, wav_bytes: bytes) -> None:
        try:
            await ws.send_bytes(wav_bytes)
        except Exception as exc:
            _LOGGER.warning("voice_ws.send_wav_error", exc=str(exc))

    async def _send_state(
        self,
        ws: WebSocket,
        ws_mgr: ConnectionManager,
        state: str,
        *,
        session_key: str | None = None,
        turn_id: int | None = None,
    ) -> None:
        if session_key and turn_id is not None and not await self._is_current_turn(session_key, turn_id):
            return
        if session_key:
            session = await self._get_or_create_session(session_key)
            session.state = state
        payload = {"type": "state", "state": state}
        if turn_id is not None:
            payload["turn_id"] = turn_id
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass
        surface_state = getattr(ws.app.state, "surface_state_service", None)
        if surface_state is not None:
            await surface_state.set_avatar_state(ws_mgr, state)
        else:
            await ws_mgr.broadcast_json({"type": "avatar_state", "state": state})

    async def _send_audio_output(
        self,
        ws: WebSocket,
        session_key: str | None,
        wav_bytes: bytes,
        *,
        turn_id: int | None,
    ) -> None:
        if session_key:
            session = await self._get_or_create_session(session_key)
            if session.output_streaming_enabled:
                audio_format = session.output_audio_format
                if audio_format == "pcm_s16le":
                    pcm_bytes, sample_rate, channels, sample_width_bytes = self._extract_pcm_stream(wav_bytes)
                    payload_bytes = pcm_bytes
                else:
                    payload_bytes = wav_bytes
                    sample_rate = None
                    channels = None
                    sample_width_bytes = None
                chunk_size = 32 * 1024
                chunks = [
                    payload_bytes[i:i + chunk_size]
                    for i in range(0, len(payload_bytes), chunk_size)
                ] or [b""]
                await self._send_json(ws, {
                    "type": "output_audio_start",
                    "audio_format": audio_format,
                    "byte_length": len(payload_bytes),
                    "chunk_count": len(chunks),
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "sample_width_bytes": sample_width_bytes,
                }, turn_id=turn_id)
                for chunk in chunks:
                    await self._send_wav(ws, chunk)
                await self._send_json(ws, {
                    "type": "output_audio_end",
                }, turn_id=turn_id)
                return
        await self._send_wav(ws, wav_bytes)

    async def _send_sentence_first_audio(
        self,
        ctx: VoiceTurnContext,
        adapter: RealtimeVoiceAdapter,
        *,
        session_key: str | None,
        turn_id: int | None,
        reply_text: str,
        offset_s: float,
    ) -> bool:
        segment_texts = self._split_reply_for_progressive_audio(reply_text)
        if not segment_texts or not session_key:
            return False
        session = await self._get_or_create_session(session_key)
        if not session.output_streaming_enabled or session.output_audio_format != "pcm_s16le":
            return False

        first_text, remaining_text = segment_texts
        remainder_task = asyncio.create_task(adapter.synthesise_reply(ctx, remaining_text))
        first_wav, first_word_timings = await adapter.synthesise_reply(ctx, first_text)

        if session_key and not await self._is_current_turn(session_key, turn_id):
            remainder_task.cancel()
            with suppress(asyncio.CancelledError):
                await remainder_task
            return True

        pcm_bytes, sample_rate, channels, sample_width_bytes = self._extract_pcm_stream(first_wav)
        chunk_size = 32 * 1024
        first_chunks = [pcm_bytes[i:i + chunk_size] for i in range(0, len(pcm_bytes), chunk_size)] or [b""]
        await self._send_json(ctx.ws, {
            "type": "audio_start",
            "byte_length": len(first_wav),
        }, turn_id=turn_id)
        await self._send_json(ctx.ws, {
            "type": "output_audio_start",
            "audio_format": "pcm_s16le",
            "byte_length": len(pcm_bytes),
            "chunk_count": len(first_chunks),
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width_bytes": sample_width_bytes,
        }, turn_id=turn_id)
        await self._send_json(ctx.ws, {
            "type": "word_timings",
            "word_timings": first_word_timings,
            "append": False,
        }, turn_id=turn_id)

        if offset_s > 0 and ctx.speaker and ctx.speaker.is_configured:
            await asyncio.sleep(offset_s)

        for chunk in first_chunks:
            await self._send_wav(ctx.ws, chunk)

        remaining_wav, remaining_word_timings = await remainder_task
        if session_key and not await self._is_current_turn(session_key, turn_id):
            return True

        remaining_pcm, _, _, _ = self._extract_pcm_stream(remaining_wav)
        remaining_chunks = [remaining_pcm[i:i + chunk_size] for i in range(0, len(remaining_pcm), chunk_size)] or [b""]
        offset_ms = self._wav_duration_ms(first_wav)
        adjusted_word_timings = [
            {
                "word": str(item.get("word") or ""),
                "start_ms": int(item.get("start_ms", 0) + offset_ms),
                "end_ms": int(item.get("end_ms", 0) + offset_ms),
            }
            for item in (remaining_word_timings or [])
        ]
        if adjusted_word_timings:
            await self._send_json(ctx.ws, {
                "type": "word_timings",
                "word_timings": adjusted_word_timings,
                "append": True,
            }, turn_id=turn_id)
        for chunk in remaining_chunks:
            await self._send_wav(ctx.ws, chunk)
        await self._send_json(ctx.ws, {"type": "output_audio_end"}, turn_id=turn_id)
        return True

    def _split_reply_for_progressive_audio(self, reply_text: str) -> tuple[str, str] | None:
        text = (reply_text or "").strip()
        if len(text) < 80:
            return None
        match = _STREAMING_SENTENCE_RE.match(text)
        if not match:
            return None
        first = match.group(1).strip()
        rest = match.group(2).strip()
        if len(first) < 24 or len(rest) < 24:
            return None
        return first, rest

    def _wav_duration_ms(self, wav_bytes: bytes) -> int:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return round((wf.getnframes() / wf.getframerate()) * 1000)

    def _extract_pcm_stream(self, wav_bytes: bytes) -> tuple[bytes, int, int, int]:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width_bytes = wf.getsampwidth()
            pcm_bytes = wf.readframes(wf.getnframes())
        return pcm_bytes, sample_rate, channels, sample_width_bytes

    async def _get_or_create_session(self, session_key: str) -> VoiceSessionState:
        async with self._sessions_lock:
            session = self._sessions.get(session_key)
            if session is None:
                session = VoiceSessionState()
                self._sessions[session_key] = session
            return session

    async def _is_current_turn(self, session_key: str, turn_id: int | None) -> bool:
        if turn_id is None:
            return True
        session = await self._get_or_create_session(session_key)
        return session.current_turn_id == turn_id

    async def _clear_completed_task(self, session_key: str, turn_id: int | None) -> None:
        if turn_id is None:
            return
        session = await self._get_or_create_session(session_key)
        if session.current_turn_id == turn_id and session.current_task and session.current_task.done():
            session.current_task = None

    async def _finish_turn(
        self,
        ws: WebSocket,
        session_key: str | None,
        turn_id: int | None,
        reason: str,
    ) -> None:
        if turn_id is None:
            return
        await self._send_json(ws, {
            "type": "turn_finished",
            "reason": reason,
        }, turn_id=turn_id)
        if session_key:
            await self._clear_completed_task(session_key, turn_id)

    async def _cancel_turn_task(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _send_turn_interrupted(self, ws: WebSocket, interrupted_turn_id: int) -> None:
        await self._send_json(ws, {
            "type": "turn_interrupted",
            "interrupted_turn_id": interrupted_turn_id,
        })

    async def _send_json(
        self,
        ws: WebSocket,
        payload: dict[str, Any],
        *,
        turn_id: int | None = None,
    ) -> None:
        if turn_id is not None:
            payload = {**payload, "turn_id": turn_id}
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def _extract_turn_context(self, ws: WebSocket) -> VoiceTurnContext | None:
        app = getattr(ws, "app", None)
        if app is None:
            return None
        state = getattr(app, "state", None)
        if state is None:
            return None
        try:
            return VoiceTurnContext(
                ws=ws,
                ws_mgr=state.ws_manager,
                session_id=getattr(ws, "_nova_session_id"),
                stt=state.stt_service,
                tts=state.tts_service,
                speaker=getattr(state, "speaker_service", None),
                app=app,
            )
        except AttributeError:
            return None

    def _resolve_adapter(self, ctx: VoiceTurnContext) -> RealtimeVoiceAdapter:
        adapter = getattr(ctx.app.state, "realtime_voice_adapter", None)
        if adapter is None:
            return self._default_adapter
        return adapter

    def _resolve_adapter_for_ws(self, ws: WebSocket) -> RealtimeVoiceAdapter:
        app = getattr(ws, "app", None)
        state = getattr(app, "state", None) if app is not None else None
        adapter = getattr(state, "realtime_voice_adapter", None) if state is not None else None
        if adapter is None:
            return self._default_adapter
        return adapter


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
