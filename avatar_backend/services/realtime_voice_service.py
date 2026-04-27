from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
import wave
from contextlib import suppress
from typing import Any

import structlog
from fastapi import WebSocket

from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager

_LOGGER = structlog.get_logger()
from avatar_backend.services.voice_types import (
    IDLE,
    LISTENING,
    THINKING,
    SPEAKING,
    ERROR,
    LLM_TIMEOUT_MSG,
    LLM_OFFLINE_MSG,
    _STREAMING_SENTENCE_RE,
    _AUDIO_CACHE_TTL,
    VoiceTurnContext,
    VoiceSessionState,
    VoiceTurnResult,
    RealtimeVoiceAdapter,
    DefaultRealtimeVoiceAdapter,
    create_realtime_voice_adapter,  # re-exported for startup.py
)

from avatar_backend.services.voice_audio import VoiceAudioMixin
from avatar_backend.services.voice_session import VoiceSessionMixin

class RealtimeVoiceService(VoiceAudioMixin, VoiceSessionMixin):
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


    async def _try_media_shortcut(self, ctx, transcript: str) -> str | None:
        """Handle media control commands with a focused prompt instead of the full system prompt."""
        import re as _re
        _lower = transcript.lower()
        _media_keywords = ("switch to", "put on", "tune to", "play channel", "change to",
                           "watch channel", "put the match on", "switch channel",
                           "turn on the tv", "turn on the living", "turn on the bedroom", "switch the channel", "change the channel", "switch it to", "to cnn", "to bbc", "to sky", "to itv")
        _channel_names = ("cnn", "bbc", "sky", "itv", "channel 4", "channel 5",
                          "discovery", "cartoon", "nick", "tnt sport", "premier league",
                          "eurosport", "sky news", "sky cinema", "sky sports")
        if not any(kw in _lower for kw in _media_keywords) and not any(ch in _lower for ch in _channel_names):
            return None

        from avatar_backend.services.chat_service import _match_channel
        _match = await _match_channel(_lower, ctx.container.ha_proxy)
        if not _match:
            return None
        channel, channel_number = _match

        bedroom = any(w in _lower for w in ("bedroom", "bed room"))
        remote = "remote.bed_room_shield_tv" if bedroom else "remote.shield_android_tv"
        player = "media_player.shield_bedroom" if bedroom else "media_player.shield_living_room"
        room = "bedroom" if bedroom else "living room"

        _LOGGER.info("voice_ws.media_shortcut", transcript=transcript[:80], channel=channel, room=room)

        from avatar_backend.models.messages import ToolCall
        import asyncio
        if not bedroom:
            try:
                await ctx.container.ha_proxy.execute_tool_call(ToolCall(
                    function_name="call_ha_service",
                    arguments={"domain": "wake_on_lan", "service": "send_magic_packet", "entity_id": "all", "service_data": {"mac": "3C:6D:66:24:F8:AE"}}
                ))
            except Exception as e:
                _LOGGER.warning("voice_ws.media_shortcut_wol_error", exc=str(e)[:80])
        else:
            try:
                await ctx.container.ha_proxy.execute_tool_call(ToolCall(
                    function_name="call_ha_service",
                    arguments={"domain": "remote", "service": "turn_on", "entity_id": remote}
                ))
            except Exception as e:
                _LOGGER.warning("voice_ws.media_shortcut_wake_error", exc=str(e)[:80])

        await asyncio.sleep(8)

        try:
            await ctx.container.ha_proxy.execute_tool_call(ToolCall(
                function_name="call_ha_service",
                arguments={"domain": "remote", "service": "turn_on", "entity_id": remote, "service_data": {"activity": "com.getchannels.dvr.app"}}
            ))
        except Exception as e:
            _LOGGER.warning("voice_ws.media_shortcut_launch_error", exc=str(e)[:80])

        await asyncio.sleep(5)

        try:
            await ctx.container.ha_proxy.execute_tool_call(ToolCall(
                function_name="call_ha_service",
                arguments={"domain": "media_player", "service": "select_source", "entity_id": player, "service_data": {"source": channel}}
            ))
        except Exception as e:
            _LOGGER.warning("voice_ws.media_shortcut_channel_error", exc=str(e)[:80])
            return f"Launched Channels DVR but couldn't tune to {channel}."

        # Tune directly via Channels DVR client API
        _shield_ip = "192.168.0.129" if not bedroom else "192.168.0.139"
        if channel_number:
            try:
                import httpx as _hx
                async with _hx.AsyncClient(timeout=5.0) as _client:
                    await _client.post(f"http://{_shield_ip}:57000/api/play/channel/{channel_number}")
            except Exception:
                pass

        return f"Switching {room} TV to {channel}."

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
            speaker_name = session.pending_speaker_name
            room_id = session.room_id
            session.pending_event_id = None
            session.pending_followup_prompt = None
            session.pending_speaker_name = None
            await self._send_json(ctx.ws, {"type": "turn_started"}, turn_id=turn_id)
            session.current_task = asyncio.create_task(
                self.process_audio(
                    ctx,
                    audio_bytes,
                    session_key=session_key,
                    turn_id=turn_id,
                    event_id=event_id,
                    followup_prompt=followup_prompt,
                    speaker_name=speaker_name,
                    room_id=room_id,
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
            speaker_name = str(data.get("speaker_name") or "").strip()
            session = await self._get_or_create_session(session_key)
            session.pending_event_id = event_id or None
            session.pending_followup_prompt = followup_prompt or None
            session.pending_speaker_name = speaker_name or None
            await self._send_json(ws, {
                "type": "turn_context_ack",
                "event_id": session.pending_event_id,
                "followup_prompt": session.pending_followup_prompt,
                "speaker_name": session.pending_speaker_name,
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
            room_id_cap = str(capabilities.get("room_id") or "").strip().lower() or None
            if room_id_cap:
                session.room_id = room_id_cap
                if app is not None:
                    _ws_mgr = getattr(getattr(app.state, "_container", None), "ws_manager", None)
                    if _ws_mgr is not None:
                        _ws_mgr.set_room(ws, room_id_cap)
            await self._send_json(ws, {
                "type": "client_capabilities_ack",
                "output_streaming": session.output_streaming_enabled,
                "output_audio_format": session.output_audio_format,
                "room_id": session.room_id,
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
                session.stt_stream_queue = asyncio.Queue()
                session.stt_partial_text = ""
            # Start background streaming STT task
            ctx = self._extract_turn_context(ws)
            if ctx is not None and hasattr(ctx.stt, "transcribe_streaming"):
                asyncio.create_task(
                    self._run_streaming_stt(ws, session_key, session, ctx.stt)
                )
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
                stt_queue = session.stt_stream_queue
                session.input_stream_open = False
                session.input_audio_chunks = []
                session.input_audio_bytes = 0
                session.stt_stream_queue = None
            # Signal end-of-stream to the streaming STT queue
            if stt_queue is not None:
                await stt_queue.put(None)
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
                stt_queue = session.stt_stream_queue
                session.input_stream_open = False
                session.input_audio_chunks = []
                session.input_audio_bytes = 0
                session.stt_stream_queue = None
            # Signal cancellation to the streaming STT queue
            if stt_queue is not None:
                await stt_queue.put(None)
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
                # Feed chunk to streaming STT if active
                if session.stt_stream_queue is not None:
                    await session.stt_stream_queue.put(audio_bytes)
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
        speaker_name: str | None = None,
        room_id: str | None = None,
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

            # Try media shortcut first
            media_reply = await self._try_media_shortcut(ctx, transcript)
            if media_reply:
                reply_text = media_reply
                await self._send_state(ctx.ws, ctx.ws_mgr, SPEAKING, session_key=session_key, turn_id=turn_id)
                await self._speak_and_stream(ctx, reply_text, session_key, turn_id)
                await self._finish_turn(ctx.ws, session_key, turn_id, "stop")
                return

            await self._send_state(ctx.ws, ctx.ws_mgr, THINKING, session_key=session_key, turn_id=turn_id)
            fallback_text = None
            result = None

            try:
                result = await adapter.run_turn(
                    ctx,
                    transcript,
                    event_id=event_id,
                    followup_prompt=followup_prompt,
                    speaker_name=speaker_name,
                    room_id=room_id,
                )
            except RuntimeError as exc:
                err = str(exc)
                _LOGGER.error("voice_ws.llm_error", exc=err)
                if "timed out" in err.lower():
                    fallback_text = LLM_TIMEOUT_MSG
                elif "400" in err and "bad request" in err.lower():
                    _LOGGER.warning("voice_ws.clearing_corrupt_session", session_id=ctx.session_id)
                    await ctx.container.conversation_service.clear_session_state(ctx.session_id)
                    try:
                        result = await adapter.run_turn(
                            ctx,
                            transcript,
                            event_id=event_id,
                            followup_prompt=followup_prompt,
                            speaker_name=speaker_name,
                            room_id=room_id,
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

                    # Try progressive sentence-level streaming first
                    progressive_done = False
                    try:
                        progressive_done = await self._send_sentence_first_audio(
                            ctx, adapter,
                            session_key=session_key,
                            turn_id=turn_id,
                            reply_text=reply_text,
                            offset_s=offset_s,
                        )
                    except Exception as exc:
                        _LOGGER.debug("voice_ws.progressive_audio_failed", exc=repr(exc))

                    if progressive_done:
                        # Progressive streaming handled everything — log and skip single-pass
                        try:
                            from avatar_backend.routers.announce import _log_announcement
                            _log_announcement(reply_text, "normal", [], source="voice", query=transcript)
                            ctx.ws_mgr.increment_message_count(ctx.ws)
                        except Exception:
                            pass
                        # Speaker broadcast for progressive path
                        if ctx.speaker and ctx.speaker.is_configured:
                            try:
                                await ctx.speaker.speak(reply_text, area_aware=True)
                            except Exception:
                                pass
                    else:
                        # Single-pass TTS fallback
                        wav_bytes, word_timings = await adapter.synthesise_reply(ctx, reply_text)
                        try:
                            from avatar_backend.routers.announce import _log_announcement
                            _log_announcement(reply_text, "normal", [], source="voice", query=transcript)
                            ctx.ws_mgr.increment_message_count(ctx.ws)
                        except Exception:
                            pass
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
                                cache = ctx.container.audio_cache
                                expired = [k for k, (_, exp) in cache.items() if time.time() > exp]
                                for k in expired:
                                    cache.pop(k, None)
                                cache[token] = (wav_bytes, expiry)
                                audio_url = f"{public_url}/tts/audio/{token}"

                                # Convert WAV to Alexa-compatible MP3 for Echo SSML playback
                                mp3_url = None
                                try:
                                    from avatar_backend.routers.announce import _wav_to_alexa_mp3
                                    mp3_bytes = await _wav_to_alexa_mp3(wav_bytes)
                                    if mp3_bytes:
                                        mp3_token = uuid.uuid4().hex
                                        cache[f"mp3:{mp3_token}"] = (mp3_bytes, time.time() + 120)
                                        mp3_url = f"{public_url}/tts/audio_mp3/{mp3_token}"
                                except Exception:
                                    pass

                                speaker_task = asyncio.create_task(ctx.speaker.speak_wav(reply_text, audio_url, mp3_url=mp3_url, area_aware=True))
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
