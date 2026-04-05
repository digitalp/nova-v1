"""
Voice WebSocket endpoint — /ws/voice

Protocol
--------
Client → Server:
  Binary frames: raw PCM16 mono audio at 16 kHz  OR  WAV file bytes
  Text frame:    {"type": "ping"}  (keepalive)

Server → Client (all text, JSON):
  {"type": "state", "state": "<idle|listening|thinking|speaking>"}
  {"type": "transcript", "text": "<user speech>"}
  {"type": "response", "text": "<assistant reply>", "session_id": "..."}
  {"type": "error", "detail": "<message>"}

  Binary frames: WAV audio of the assistant's spoken reply

Authentication:
  POST /ws/token  (X-API-Key header) → returns {"token": "<short-lived>"}
  ?token=<token>  use returned token in WebSocket URL (single-use, 30s TTL)

Usage notes:
  - The client sends ONE audio chunk per turn (full utterance, not streaming).
  - Empty/silent audio is accepted; the server returns an empty response.
  - If speaker_service is configured, the response is also spoken on HA media
    players (Echo, Sonos, etc.) concurrently with sending WAV to the WS client.
"""
from __future__ import annotations
import asyncio
import json

import structlog
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from avatar_backend.middleware.auth import verify_api_key, verify_api_key_ws, issue_ws_token
from avatar_backend.services.chat_service import run_chat
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.stt_service import STTService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager

_LOGGER = structlog.get_logger()

router = APIRouter()


@router.post("/ws/token", dependencies=[Depends(verify_api_key)])
async def get_ws_token() -> dict:
    """
    Exchange a permanent API key for a short-lived single-use WebSocket token.
    The token must be used within 30 seconds and is consumed on first connection.
    Use: POST /ws/token with X-API-Key header → connect WS with ?token=<token>
    """
    return {"token": issue_ws_token(), "ttl_seconds": 30}


_IDLE      = "idle"
_LISTENING = "listening"
_THINKING  = "thinking"
_SPEAKING  = "speaking"
_ERROR     = "error"

# Spoken when the LLM times out — synthesised and returned as audio
_LLM_TIMEOUT_MSG  = "I'm having trouble thinking right now. Please try again in a moment."
_LLM_OFFLINE_MSG  = "I can't reach my brain right now. Please check that Ollama is running."


@router.websocket("/ws/voice")
async def voice_websocket(
    ws: WebSocket,
    session_id: str = "voice_default",
    _: None = Depends(verify_api_key_ws),
):
    """Full-duplex voice pipeline: audio in → transcript → LLM → TTS → audio out."""
    app = ws.app
    stt: STTService            = app.state.stt_service
    tts: TTSService            = app.state.tts_service
    ws_mgr: ConnectionManager  = app.state.ws_manager
    speaker: SpeakerService    = getattr(app.state, "speaker_service", None)

    await ws.accept()
    await ws_mgr.connect_voice(ws)
    await _send_state(ws, ws_mgr, _IDLE)

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.receive" and msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                    if data.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    pass
                continue

            if msg["type"] == "websocket.receive" and msg.get("bytes"):
                await _process_audio(
                    ws=ws,
                    ws_mgr=ws_mgr,
                    audio_bytes=msg["bytes"],
                    session_id=session_id,
                    stt=stt,
                    tts=tts,
                    speaker=speaker,
                    app=app,
                )
                continue

            if msg["type"] == "websocket.disconnect":
                break

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _LOGGER.exception("voice_ws.unhandled_error", exc=str(exc))
        try:
            await ws.send_text(json.dumps({"type": "error", "detail": "An internal error occurred."}))
        except Exception:
            pass
    finally:
        await ws_mgr.disconnect_voice(ws)
        await _broadcast_state(ws_mgr, _IDLE)


async def _process_audio(
    *,
    ws: WebSocket,
    ws_mgr: ConnectionManager,
    audio_bytes: bytes,
    session_id: str,
    stt: STTService,
    tts: TTSService,
    speaker: SpeakerService | None,
    app,
) -> None:
    # 1. STT
    await _send_state(ws, ws_mgr, _LISTENING)
    try:
        transcript = await stt.transcribe(audio_bytes)
    except Exception as exc:
        _LOGGER.error("voice_ws.stt_error", exc=str(exc))
        await ws.send_text(json.dumps({"type": "error", "detail": f"STT failed: {exc}"}))
        await _send_state(ws, ws_mgr, _ERROR)
        await asyncio.sleep(1)
        await _send_state(ws, ws_mgr, _IDLE)
        return

    if not transcript:
        _LOGGER.info("voice_ws.empty_transcript")
        await _send_state(ws, ws_mgr, _IDLE)
        return

    await ws.send_text(json.dumps({"type": "transcript", "text": transcript}))
    _LOGGER.info("voice_ws.transcript", chars=len(transcript), text=transcript[:80])

    # 2. LLM chat — with graceful timeout/offline fallback
    await _send_state(ws, ws_mgr, _THINKING)
    fallback_text = None
    result = None

    try:
        result = await run_chat(
            session_id=session_id,
            user_text=transcript,
            llm=app.state.llm_service,
            sm=app.state.session_manager,
            ha=app.state.ha_proxy,
        )
    except RuntimeError as exc:
        err = str(exc)
        _LOGGER.error("voice_ws.llm_error", exc=err)
        if "timed out" in err.lower():
            fallback_text = _LLM_TIMEOUT_MSG
        elif "HTTP 400" in err:
            # Corrupt conversation history (e.g. orphaned tool call after session
            # reconnect). Clear the session and retry once with a clean slate.
            _LOGGER.warning("voice_ws.clearing_corrupt_session", session_id=session_id)
            await app.state.session_manager.clear(session_id)
            try:
                result = await run_chat(
                    session_id=session_id,
                    user_text=transcript,
                    llm=app.state.llm_service,
                    sm=app.state.session_manager,
                    ha=app.state.ha_proxy,
                )
            except Exception as retry_exc:
                _LOGGER.error("voice_ws.llm_retry_failed", exc=str(retry_exc))
                fallback_text = _LLM_OFFLINE_MSG
        else:
            fallback_text = _LLM_OFFLINE_MSG
    except Exception as exc:
        _LOGGER.error("voice_ws.llm_error", exc=str(exc))
        fallback_text = _LLM_OFFLINE_MSG

    reply_text = fallback_text if fallback_text else (result.text if result else "")

    if result and not fallback_text:
        await ws.send_text(json.dumps({
            "type":          "response",
            "text":          reply_text,
            "session_id":    result.session_id,
            "tool_calls":    [tc.model_dump() for tc in result.tool_calls],
            "processing_ms": result.processing_time_ms,
        }))

    # 3. TTS + speaker broadcast
    if reply_text:
        await _send_state(ws, ws_mgr, _SPEAKING)
        try:
            wav_bytes, word_timings = await tts.synthesise_with_timing(reply_text)
            # Send word timings before the audio so the client can attach them
            await ws.send_text(json.dumps({
                "type":         "word_timings",
                "word_timings": word_timings,
            }))
            tasks = [_send_wav(ws, wav_bytes)]
            if speaker and speaker.is_configured:
                tasks.append(speaker.speak(reply_text))
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            _LOGGER.error("voice_ws.tts_error", exc=str(exc))
            await ws.send_text(json.dumps({"type": "error", "detail": f"TTS failed: {exc}"}))

    await _send_state(ws, ws_mgr, _IDLE)


async def _send_wav(ws: WebSocket, wav_bytes: bytes) -> None:
    try:
        await ws.send_bytes(wav_bytes)
    except Exception as exc:
        _LOGGER.warning("voice_ws.send_wav_error", exc=str(exc))


async def _send_state(ws: WebSocket, ws_mgr: ConnectionManager, state: str) -> None:
    payload = {"type": "state", "state": state}
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass
    await _broadcast_state(ws_mgr, state)


async def _broadcast_state(ws_mgr: ConnectionManager, state: str) -> None:
    await ws_mgr.broadcast_json({"type": "avatar_state", "state": state})


# ── Wake word check (fallback for browsers without SpeechRecognition) ─────────

# Whisper sometimes mishears "Nova" as phonetically similar words.
# This set covers observed misrecognitions so we don't miss a wake event.
_WAKE_VARIANTS = {
    "nova",
    "noba",
    "nobba",
    "no va",
    "nover",
    "novah",
    "novia",
    "noah",   # Whisper base mishears "Nova" as "Noah"
}


def _is_wake_word(transcript: str) -> bool:
    t = transcript.lower().strip().rstrip(".,!?")
    # Reject long phrases — a wake word utterance is never more than 3 words.
    # This prevents ambient speech like "I'm so sick of this" from ever matching
    # even if a variant substring happened to appear in it.
    if len(t.split()) > 3:
        return False
    return any(v in t for v in _WAKE_VARIANTS)


@router.post("/stt/wake", dependencies=[Depends(verify_api_key)])
async def check_wake_word(request: Request):
    """
    Accepts raw audio bytes, runs Whisper STT, returns whether 'nova' was said.
    Used by Fully Kiosk Browser and other WebView clients that lack the
    Web Speech Recognition API.
    """
    body = await request.body()
    stt: STTService = request.app.state.stt_service
    try:
        transcript = await stt.transcribe_wake(body)
    except Exception as exc:
        _LOGGER.warning("stt.wake_check_error", exc=str(exc))
        return JSONResponse({"wake": False, "transcript": ""})
    wake = _is_wake_word(transcript)
    _LOGGER.info("stt.wake_check", transcript=transcript[:60], wake=wake)
    return JSONResponse({"wake": wake, "transcript": transcript})
