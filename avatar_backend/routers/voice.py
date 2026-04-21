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
import json

import structlog
from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from avatar_backend.middleware.auth import verify_api_key, verify_api_key_ws, issue_ws_token
from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.services.coral_wake_detector import CoralWakeDetector, WakeResult
from avatar_backend.services.realtime_voice_service import (
    RealtimeVoiceService,
    VoiceTurnContext,
)
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


@router.websocket("/ws/voice")
async def voice_websocket(
    ws: WebSocket,
    session_id: str = Query(default="voice_default", max_length=128),
    room: str = Query(default="", max_length=64),
    _: None = Depends(verify_api_key_ws),
    container: AppContainer = Depends(get_container),
):
    """Full-duplex voice pipeline: audio in → transcript → LLM → TTS → audio out."""
    stt: STTService            = container.stt_service
    tts: TTSService            = container.tts_service
    ws_mgr: ConnectionManager  = container.ws_manager
    speaker: SpeakerService    = getattr(container, "speaker_service", None)
    voice_service: RealtimeVoiceService = getattr(container, "realtime_voice_service", None) or RealtimeVoiceService()

    await ws.accept()
    setattr(ws, "_nova_session_id", session_id)
    await ws_mgr.connect_voice(ws)
    _room = room.strip().lower() if room else None
    if _room:
        ws_mgr.set_room(ws, _room)
        setattr(ws, "_nova_room_id", _room)
    session_key = f"{session_id}:{id(ws)}"
    await voice_service.connect_session(session_key)
    await voice_service.send_initial_state(ws, ws_mgr)

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.receive" and msg.get("text"):
                handled = await voice_service.handle_text_frame(ws, session_key, msg["text"])
                if handled:
                    continue
                continue

            if msg["type"] == "websocket.receive" and msg.get("bytes"):
                await voice_service.handle_binary_frame(
                    session_key,
                    VoiceTurnContext(
                        ws=ws,
                        ws_mgr=ws_mgr,
                        session_id=session_id,
                        stt=stt,
                        tts=tts,
                        speaker=speaker,
                        app=app,
                    ),
                    msg["bytes"],
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
        await voice_service.disconnect_session(session_key)
        await ws_mgr.disconnect_voice(ws)
        await ws_mgr.broadcast_json({"type": "avatar_state", "state": "idle"})


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
async def check_wake_word(request: Request, container: AppContainer = Depends(get_container)):
    """
    3-stage wake word detection:
      1. Coral Edge TPU TFLite model (if nova_wakeword_edgetpu.tflite present, ~1ms)
      2. Silero VAD gate — silent chunks are dropped without calling Whisper (~14ms)
      3. Whisper transcribe_wake fallback — only fires on speech-containing audio

    Used by Fully Kiosk Browser and other WebView clients that lack the
    Web Speech Recognition API.
    """
    body = await request.body()
    stt: STTService = container.stt_service
    detector: CoralWakeDetector = getattr(
        container, "coral_wake_detector", None
    )
    if detector is None:
        # Fallback: direct Whisper (original behaviour if detector not wired up)
        try:
            transcript = await stt.transcribe_wake(body)
        except Exception as exc:
            _LOGGER.warning("stt.wake_check_error", exc=str(exc))
            return JSONResponse({"wake": False, "transcript": ""})
        wake = _is_wake_word(transcript)
        _LOGGER.info("stt.wake_check", transcript=transcript[:60], wake=wake)
        return JSONResponse({"wake": wake, "transcript": transcript, "method": "whisper_direct"})

    result: WakeResult = await detector.detect(body)
    return JSONResponse({
        "wake":       result.wake,
        "transcript": result.transcript,
        "method":     result.method,
        "elapsed_ms": round(result.elapsed_ms, 1),
    })


# ── Face recognition from browser webcam ──────────────────────────────────────


@router.post("/face/greet", dependencies=[Depends(verify_api_key)])
async def greet_face(request: Request, container: AppContainer = Depends(get_container)):
    """Browser sends recognized name; returns a WAV greeting to play on that device only."""
    from datetime import datetime as _dt
    from fastapi.responses import Response as _Resp
    body = await request.json()
    name = str(body.get("name") or "").strip().title()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    hour = _dt.now().hour
    if hour < 12:
        time_phrase = "Good morning"
    elif hour < 18:
        time_phrase = "Good afternoon"
    else:
        time_phrase = "Good evening"
    msg = time_phrase + ", " + name + "! Great to see you. Is there anything I can help you with?"
    tts = getattr(container, "tts_service", None)
    if tts is None:
        return JSONResponse({"error": "TTS not available"}, status_code=503)
    import base64 as _b64
    wav_bytes, word_timings = await tts.synthesise_with_timing(msg)
    return JSONResponse({
        "wav_b64": _b64.b64encode(wav_bytes).decode(),
        "word_timings": word_timings,
        "message": msg,
    })

@router.post("/face/recognize", dependencies=[Depends(verify_api_key)])
async def recognize_face(request: Request, container: AppContainer = Depends(get_container)):
    """Accept a JPEG frame from the browser webcam and return recognized faces."""
    face_service = getattr(container, "face_service", None)
    if not face_service or not face_service.available:
        return JSONResponse({"faces": [], "error": "Face recognition not configured"})
    body = await request.body()
    if not body:
        return JSONResponse({"faces": []})
    faces = await face_service.recognize(body)
    return JSONResponse({"faces": faces})
