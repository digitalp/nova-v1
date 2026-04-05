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

_AUDIO_CACHE_TTL = 60  # seconds before an unplayed cache entry expires

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

import uuid

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

    # 4. Start HA speaker first, then delay browser audio so lip-sync aligns
    from avatar_backend.config import get_settings as _get_settings
    _settings = _get_settings()
    offset_s = _settings.speaker_audio_offset_ms / 1000.0

    speaker_task = None
    if speaker and speaker.is_configured:
        try:
            public_url = (_settings.public_url or "").rstrip("/")
            if public_url:
                token = uuid.uuid4().hex
                expiry = time.time() + _AUDIO_CACHE_TTL
                cache = request.app.state.audio_cache
                # Prune expired entries before inserting
                expired = [k for k, (_, exp) in cache.items() if time.time() > exp]
                for k in expired:
                    cache.pop(k, None)
                cache[token] = (wav_bytes, expiry)
                audio_url = f"{public_url}/tts/audio/{token}"
                speaker_task = asyncio.create_task(speaker.speak_wav(text, audio_url))
            else:
                speaker_task = asyncio.create_task(speaker.speak(text))
        except Exception as exc:
            _LOGGER.warning("announce.speaker_error", exc=str(exc))

    # Wait so the speaker has a head start before the browser plays
    if offset_s > 0 and speaker_task is not None:
        await asyncio.sleep(offset_s)

    # 5. Push word timings then audio to connected browser voice clients
    await ws_mgr.broadcast_to_voice_json({
        "type":         "announce",
        "text":         text,
        "priority":     body.priority,
        "word_timings": word_timings,
    })
    await ws_mgr.broadcast_to_voice_bytes(wav_bytes)

    # 6. Await speaker task
    if speaker_task is not None:
        try:
            await speaker_task
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




_DEFAULT_DOORBELL_CAMERA = "camera.reolink_video_doorbell_poe_fluent"


class DoorbellAnnounceRequest(BaseModel):
    camera_entity_id: str = _DEFAULT_DOORBELL_CAMERA


class DoorbellAnnounceResponse(BaseModel):
    status:      str
    message:     str
    camera_used: str
    wav_bytes:   int = 0
    elapsed_ms:  int = 0


@router.post(
    "/announce/doorbell",
    response_model=DoorbellAnnounceResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Doorbell alert — capture camera image and announce what Nova sees",
)
async def doorbell_announce_handler(body: DoorbellAnnounceRequest, request: Request):
    """
    Called when the doorbell rings. Nova:
      1. Captures a snapshot from the doorbell camera
      2. Describes what it sees using vision AI
      3. Announces the result on all speakers with priority="alert"

    Falls back to a generic "Someone is at the door" if the camera is unavailable.
    """
    t0 = time.monotonic()
    ha  = request.app.state.ha_proxy
    llm = request.app.state.llm_service

    _LOGGER.info("doorbell.triggered", camera=body.camera_entity_id)

    # 1. Fetch camera snapshot
    image_bytes = await ha.fetch_camera_image(body.camera_entity_id)

    if image_bytes:
        try:
            description = await llm.describe_image(image_bytes)
            message = f"Someone is at the door. {description}"
            _LOGGER.info("doorbell.described", chars=len(description))
        except Exception as exc:
            _LOGGER.warning("doorbell.describe_failed", exc=str(exc))
            message = "Someone is at the door."
    else:
        _LOGGER.warning("doorbell.camera_unavailable", camera=body.camera_entity_id)
        message = "Someone is at the door."

    # 2. Announce via the standard announce flow
    announce_resp = await announce_handler(
        AnnounceRequest(message=message, priority="alert"),
        request,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("doorbell.done", elapsed_ms=elapsed_ms, wav_bytes=announce_resp.wav_bytes)

    return DoorbellAnnounceResponse(
        status="ok",
        message=message,
        camera_used=body.camera_entity_id,
        wav_bytes=announce_resp.wav_bytes,
        elapsed_ms=elapsed_ms,
    )


class MotionAnnounceRequest(BaseModel):
    camera_entity_id: str = Field(..., description="HA camera entity ID for the triggered camera")
    location:         str = Field("outdoors", max_length=64, description="Human-readable label used in the spoken message")


class MotionAnnounceResponse(BaseModel):
    status:      str
    message:     str
    camera_used: str
    wav_bytes:   int = 0
    elapsed_ms:  int = 0


@router.post(
    "/announce/motion",
    response_model=MotionAnnounceResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Motion alert — capture outdoor camera image and announce what Nova sees",
)
async def motion_announce_handler(body: MotionAnnounceRequest, request: Request):
    """
    Called when motion is detected on an outdoor camera. Nova:
      1. Captures a snapshot from the specified camera
      2. Describes what it sees using vision AI
      3. Announces the result on all speakers with priority="alert"

    camera_entity_id: HA camera entity ID (or use _OUTDOOR_CAMERAS aliases)
    location: human-readable label used in the fallback message (e.g. "the garden")

    Falls back to a generic "Motion detected" message if the camera is unavailable.
    """
    t0 = time.monotonic()
    ha  = request.app.state.ha_proxy
    llm = request.app.state.llm_service

    camera_id = body.camera_entity_id
    location  = body.location.strip() or "outdoors"

    _LOGGER.info("motion.triggered", camera=camera_id, location=location)

    # 1. Fetch camera snapshot
    image_bytes = await ha.fetch_camera_image(camera_id)

    if image_bytes:
        try:
            description = await llm.describe_image(image_bytes)
            message = f"Motion detected {location}. {description}"
            _LOGGER.info("motion.described", chars=len(description))
        except Exception as exc:
            _LOGGER.warning("motion.describe_failed", exc=str(exc))
            message = f"Motion detected {location}."
    else:
        _LOGGER.warning("motion.camera_unavailable", camera=camera_id)
        message = f"Motion detected {location}."

    # 2. Announce via the standard announce flow
    announce_resp = await announce_handler(
        AnnounceRequest(message=message, priority="alert"),
        request,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("motion.done", elapsed_ms=elapsed_ms, wav_bytes=announce_resp.wav_bytes)

    return MotionAnnounceResponse(
        status="ok",
        message=message,
        camera_used=camera_id,
        wav_bytes=announce_resp.wav_bytes,
        elapsed_ms=elapsed_ms,
    )


@router.get(
    "/tts/audio/{token}",
    include_in_schema=False,
    summary="Serve a one-shot synthesised audio file",
)
async def serve_tts_audio(token: str, request: Request):
    """Serve a pre-synthesised WAV to HA media players then delete it from cache."""
    cache: dict = getattr(request.app.state, "audio_cache", {})
    entry = cache.pop(token, None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Audio not found or already played")
    data, expiry = entry
    if time.time() > expiry:
        raise HTTPException(status_code=404, detail="Audio not found or already played")
    return Response(content=data, media_type="audio/wav")
