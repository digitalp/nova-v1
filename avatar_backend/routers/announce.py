"""
Announce endpoint — POST /announce

Called by the HA ai_avatar.announce service to make Nova speak
a message proactively (automation-triggered announcements, alerts).

Request body:
  {"message": "Someone is at the door", "priority": "normal"|"alert"}

Response:
  {"status": "ok", "message": "...", "wav_bytes": <int>, "elapsed_ms": <int>}

Flow:
  1. Broadcast avatar_state → "alert" (priority=alert) or "speaking" (normal)
  2. Synthesise speech via Piper TTS
  3. Play on configured HA speakers
  4. Broadcast avatar_state → "idle"
"""
from __future__ import annotations
import asyncio
import time
from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager

_LOGGER = structlog.get_logger()

router = APIRouter()


class AnnounceRequest(BaseModel):
    message:  str = Field(..., min_length=1, max_length=2000)
    priority: Literal["normal", "alert"] = "normal"


class AnnounceResponse(BaseModel):
    status:     str
    message:    str
    wav_bytes:  int = 0
    elapsed_ms: int = 0


@router.post(
    "/announce",
    response_model=AnnounceResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Trigger a proactive TTS announcement",
)
async def announce_handler(body: AnnounceRequest, request: Request):
    """
    Speak *body.message* on all configured speakers and update the avatar state.

    priority="alert"  → avatar pulses red before speaking (doorbell, motion, etc.)
    priority="normal" → avatar goes straight to speaking state
    """
    t0 = time.monotonic()

    tts:    TTSService        = request.app.state.tts_service
    speaker: SpeakerService   = getattr(request.app.state, "speaker_service", None)
    ws_mgr:  ConnectionManager = request.app.state.ws_manager

    text = body.message.strip()
    _LOGGER.info("announce.received", chars=len(text), priority=body.priority)

    # 1. Show alert or speaking state on avatar card
    initial_state = "alert" if body.priority == "alert" else "speaking"
    await ws_mgr.broadcast_json({"type": "avatar_state", "state": initial_state})

    # 2. Synthesise speech with word timings
    try:
        wav_bytes, word_timings = await tts.synthesise_with_timing(text)
    except Exception as exc:
        _LOGGER.error("announce.tts_error", exc=str(exc))
        await ws_mgr.broadcast_json({"type": "avatar_state", "state": "error"})
        await asyncio.sleep(1)
        await ws_mgr.broadcast_json({"type": "avatar_state", "state": "idle"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"TTS synthesis failed: {exc}",
        )

    # 3. Transition to speaking state (in case we were in alert)
    if body.priority == "alert":
        await ws_mgr.broadcast_json({"type": "avatar_state", "state": "speaking"})

    # 4. Push word timings then audio to connected browser voice clients
    await ws_mgr.broadcast_to_voice_json({
        "type":         "announce",
        "text":         text,
        "priority":     body.priority,
        "word_timings": word_timings,
    })
    await ws_mgr.broadcast_to_voice_bytes(wav_bytes)

    # 5. Play on HA speakers concurrently (non-blocking on failure)
    if speaker and speaker.is_configured:
        try:
            await speaker.speak(text)
        except Exception as exc:
            _LOGGER.warning("announce.speaker_error", exc=str(exc))

    # 6. Return to idle
    await ws_mgr.broadcast_json({"type": "avatar_state", "state": "idle"})

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("announce.done",
                 chars=len(text), wav_bytes=len(wav_bytes), elapsed_ms=elapsed_ms)

    return AnnounceResponse(
        status="ok",
        message=text,
        wav_bytes=len(wav_bytes),
        elapsed_ms=elapsed_ms,
    )
