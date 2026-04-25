"""Mixin for RealtimeVoiceService: audio output, streaming STT, and WAV utilities."""
from __future__ import annotations
import json
import asyncio
import io
import re
import wave
from contextlib import suppress
from typing import Any

import structlog
from fastapi import WebSocket
from avatar_backend.services.ws_manager import ConnectionManager

from avatar_backend.services.stt_service import STTService
from avatar_backend.services.voice_types import (
    IDLE, LISTENING, THINKING, SPEAKING, ERROR,
    _STREAMING_SENTENCE_RE, _AUDIO_CACHE_TTL,
    VoiceTurnResult,
    VoiceTurnContext,
    RealtimeVoiceAdapter,
    VoiceSessionState,
)

_LOGGER = structlog.get_logger()


class VoiceAudioMixin:
    """Audio output helpers, streaming STT, WAV processing — mixed into RealtimeVoiceService."""
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

        # Queue synthesis of segments 2..N concurrently while segment 1 synthesises
        remainder_tasks = [
            asyncio.create_task(adapter.synthesise_reply(ctx, seg_text))
            for seg_text in segment_texts[1:]
        ]

        try:
            first_wav, first_word_timings = await adapter.synthesise_reply(ctx, segment_texts[0])
        except Exception as exc:
            _LOGGER.warning("tts.segment_failed", segment_index=0, text=segment_texts[0][:60], exc=repr(exc))
            # Cancel remaining tasks and bail
            for t in remainder_tasks:
                t.cancel()
            for t in remainder_tasks:
                with suppress(asyncio.CancelledError):
                    await t
            return False

        if session_key and not await self._is_current_turn(session_key, turn_id):
            for t in remainder_tasks:
                t.cancel()
            for t in remainder_tasks:
                with suppress(asyncio.CancelledError):
                    await t
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

        # Stream remaining segments sequentially, with cumulative offset
        cumulative_offset_ms = self._wav_duration_ms(first_wav)

        for seg_idx, task in enumerate(remainder_tasks, start=1):
            try:
                seg_wav, seg_word_timings = await task
            except Exception as exc:
                _LOGGER.warning(
                    "tts.segment_failed",
                    segment_index=seg_idx,
                    text=segment_texts[seg_idx][:60],
                    exc=repr(exc),
                )
                continue

            if session_key and not await self._is_current_turn(session_key, turn_id):
                # Cancel any remaining tasks
                for remaining_task in remainder_tasks[seg_idx:]:
                    remaining_task.cancel()
                for remaining_task in remainder_tasks[seg_idx:]:
                    with suppress(asyncio.CancelledError):
                        await remaining_task
                return True

            seg_pcm, _, _, _ = self._extract_pcm_stream(seg_wav)
            seg_chunks = [seg_pcm[i:i + chunk_size] for i in range(0, len(seg_pcm), chunk_size)] or [b""]
            adjusted_word_timings = [
                {
                    "word": str(item.get("word") or ""),
                    "start_ms": int(item.get("start_ms", 0) + cumulative_offset_ms),
                    "end_ms": int(item.get("end_ms", 0) + cumulative_offset_ms),
                }
                for item in (seg_word_timings or [])
            ]
            if adjusted_word_timings:
                await self._send_json(ctx.ws, {
                    "type": "word_timings",
                    "word_timings": adjusted_word_timings,
                    "append": True,
                }, turn_id=turn_id)
            for chunk in seg_chunks:
                await self._send_wav(ctx.ws, chunk)

            cumulative_offset_ms += self._wav_duration_ms(seg_wav)

        await self._send_json(ctx.ws, {"type": "output_audio_end"}, turn_id=turn_id)
        return True

    # ── Streaming STT helpers ─────────────────────────────────────────────────

    @staticmethod
    async def _queue_to_async_iter(queue: asyncio.Queue[bytes | None]):
        """Yield bytes from an asyncio.Queue until None (sentinel) is received."""
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            yield chunk

    async def _run_streaming_stt(
        self,
        ws: WebSocket,
        session_key: str,
        session: VoiceSessionState,
        stt: STTService,
    ) -> None:
        """Background task: consume the STT stream queue and emit partial_transcript messages."""
        queue = session.stt_stream_queue
        if queue is None:
            return
        try:
            async for partial_text in stt.transcribe_streaming(
                self._queue_to_async_iter(queue)
            ):
                if partial_text:
                    session.stt_partial_text = partial_text
                    await self._send_json(ws, {
                        "type": "partial_transcript",
                        "text": partial_text,
                    })
        except Exception as exc:
            _LOGGER.debug("voice_ws.streaming_stt_error", exc=repr(exc))

    def _split_reply_for_progressive_audio(self, reply_text: str) -> list[str] | None:
        text = (reply_text or "").strip()
        if len(text) < 80:
            return None
        # Split at all sentence boundaries (.!? followed by whitespace or end)
        parts = re.split(r"(?<=[.!?])(?:\s+|$)", text)
        segments = [p.strip() for p in parts if p.strip()]
        if len(segments) < 2:
            return None
        # Merge trailing short segments into the previous one
        merged: list[str] = [segments[0]]
        for seg in segments[1:]:
            if len(seg) < 24:
                merged[-1] = merged[-1] + " " + seg
            else:
                merged.append(seg)
        if len(merged) < 2 or len(merged[0]) < 24:
            return None
        return merged

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
