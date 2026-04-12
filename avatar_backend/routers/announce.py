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
from typing import Any
from typing import Literal

_AUDIO_CACHE_TTL = 60  # seconds before an unplayed cache entry expires
_MP3_CACHE_TTL = 120  # Alexa may take longer to fetch the MP3

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

import uuid

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.services.home_runtime import load_home_runtime_config
from avatar_backend.services.event_service import publish_visual_event
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager
from avatar_backend.runtime_paths import data_dir

_LOGGER = structlog.get_logger()

_ANNOUNCE_LOG: "Path | None" = None

def _announce_log_path() -> "Path":
    global _ANNOUNCE_LOG
    if _ANNOUNCE_LOG is None:
        from pathlib import Path as _Path
        p = data_dir() / "announcements.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        _ANNOUNCE_LOG = p
    return _ANNOUNCE_LOG

def _log_announcement(text: str, priority: str, target_areas: list, source: str = "announce", query: str | None = None) -> None:
    """Append a single JSON line to the announcement log (fire-and-forget)."""
    import json as _json
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "priority": priority,
        "target_areas": target_areas,
        "source": source,
    }
    if query:
        entry["query"] = query.strip()
    try:
        with open(_announce_log_path(), "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry) + "\n")
    except Exception as exc:
        _LOGGER.warning("announce.log_write_failed", exc=str(exc))

router = APIRouter()
_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
_MOTION_ANNOUNCE_COOLDOWN_S = 600  # 10 minutes per camera for direct /announce/motion calls


class AnnounceRequest(BaseModel):
    message:  str = Field(..., min_length=1, max_length=2000)
    priority: Literal["normal", "alert"] = "normal"
    target_areas: list[str] = Field(default_factory=list)
    source: str = "announce"  # caller tag recorded in announcement log


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
    surface_state = getattr(request.app.state, "surface_state_service", None)

    text = body.message.strip()
    target_areas = [str(area).strip() for area in body.target_areas if str(area).strip()]
    _LOGGER.info("announce.received", chars=len(text), priority=body.priority, target_areas=target_areas)
    _log_announcement(text, body.priority, target_areas, source=getattr(body, "source", "announce"))

    # 1. Show alert or speaking state on avatar card
    initial_state = "alert" if body.priority == "alert" else "speaking"
    if surface_state is not None:
        await surface_state.set_avatar_state(ws_mgr, initial_state)
    else:
        await ws_mgr.broadcast_json({"type": "avatar_state", "state": initial_state})

    # 2. Synthesise speech with word timings
    try:
        wav_bytes, word_timings = await tts.synthesise_with_timing(text)
    except Exception as exc:
        _LOGGER.error("announce.tts_error", exc=str(exc))
        if surface_state is not None:
            await surface_state.set_avatar_state(ws_mgr, "error")
        else:
            await ws_mgr.broadcast_json({"type": "avatar_state", "state": "error"})
        await asyncio.sleep(1)
        if surface_state is not None:
            await surface_state.set_avatar_state(ws_mgr, "idle")
        else:
            await ws_mgr.broadcast_json({"type": "avatar_state", "state": "idle"})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"TTS synthesis failed: {exc}",
        )

    # 3. Transition to speaking state (in case we were in alert)
    if body.priority == "alert":
        if surface_state is not None:
            await surface_state.set_avatar_state(ws_mgr, "speaking")
        else:
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

                # Convert WAV to Alexa-compatible MP3 for Echo devices
                # Only use SSML audio if PUBLIC_URL is HTTPS (reachable by Amazon's cloud).
                # Local HTTP URLs cause "trouble accessing skill" errors on Echo devices.
                mp3_url = None
                if public_url.startswith("https://"):
                    try:
                        mp3_bytes = await _wav_to_alexa_mp3(wav_bytes)
                        if mp3_bytes:
                            mp3_token = uuid.uuid4().hex
                            cache[f"mp3:{mp3_token}"] = (mp3_bytes, time.time() + _MP3_CACHE_TTL)
                            mp3_url = f"{public_url}/tts/audio_mp3/{mp3_token}"
                            _LOGGER.info("announce.mp3_ready", mp3_bytes=len(mp3_bytes), url=mp3_url)
                    except Exception as exc:
                        _LOGGER.warning("announce.mp3_convert_failed", exc=str(exc))
                else:
                    _LOGGER.debug("announce.mp3_skipped_no_https", public_url=public_url)

                speaker_task = asyncio.create_task(
                    speaker.speak_wav(text, audio_url, mp3_url=mp3_url, target_areas=target_areas, area_aware=True)
                )
            else:
                speaker_task = asyncio.create_task(
                    speaker.speak(text, target_areas=target_areas, area_aware=True)
                )
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
    if surface_state is not None:
        await surface_state.set_avatar_state(ws_mgr, "idle")
    else:
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




_LEGACY_DEFAULT_DOORBELL_CAMERA = "camera.reolink_video_doorbell_poe_fluent"


class DoorbellAnnounceRequest(BaseModel):
    camera_entity_id: str | None = None


class DoorbellAnnounceResponse(BaseModel):
    status:      str
    message:     str
    camera_used: str
    wav_bytes:   int = 0
    elapsed_ms:  int = 0


class VisualEventRequest(BaseModel):
    event: str = Field(..., min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    message: str | None = Field(default=None, max_length=300)
    camera_entity_id: str | None = None
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    image_urls_csv: str | None = None
    event_context: dict[str, Any] | None = None
    expires_in_ms: int = Field(default=30000, ge=1000, le=120000)


class VisualEventResponse(BaseModel):
    status: str
    event: str
    event_id: str
    delivered: bool = True


async def _broadcast_visual_event(
    request: Request,
    ws_mgr: ConnectionManager,
    *,
    event: str,
    title: str | None = None,
    message: str | None = None,
    camera_entity_id: str | None = None,
    image_url: str | None = None,
    image_urls: list[str] | None = None,
    event_context: dict[str, Any] | None = None,
    expires_in_ms: int = 30000,
) -> str:
    event_id = uuid.uuid4().hex
    event_service = getattr(request.app.state, "event_service", None)
    surface_state = getattr(request.app.state, "surface_state_service", None)
    await publish_visual_event(
        app=request.app,
        ws_mgr=ws_mgr,
        event_service=event_service,
        surface_state=surface_state,
        event_id=event_id,
        event_type=event,
        title=title,
        message=message,
        camera_entity_id=camera_entity_id,
        image_url=image_url,
        image_urls=image_urls,
        event_context=event_context,
        expires_in_ms=expires_in_ms,
    )
    return event_id


def _parse_image_urls_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@router.post(
    "/announce/visual",
    response_model=VisualEventResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Send a visual-only event card to connected avatar clients",
)
async def visual_event_handler(body: VisualEventRequest, request: Request):
    ws_mgr: ConnectionManager = request.app.state.ws_manager
    event_id = await _broadcast_visual_event(
        request,
        ws_mgr,
        event=body.event,
        title=body.title,
        message=body.message,
        camera_entity_id=body.camera_entity_id,
        image_url=body.image_url,
        image_urls=body.image_urls + _parse_image_urls_csv(body.image_urls_csv),
        event_context=body.event_context,
        expires_in_ms=body.expires_in_ms,
    )
    return VisualEventResponse(status="ok", event=body.event, event_id=event_id)


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
    ws_mgr: ConnectionManager = request.app.state.ws_manager
    camera_events = getattr(request.app.state, "camera_event_service", None)
    runtime = load_home_runtime_config()
    camera_entity_id = (
        body.camera_entity_id
        or runtime.default_doorbell_camera
        or _LEGACY_DEFAULT_DOORBELL_CAMERA
    )

    _LOGGER.info("doorbell.triggered", camera=camera_entity_id)
    await _broadcast_visual_event(
        request,
        ws_mgr,
        event="doorbell",
        title="Doorbell",
        message="Front door live view",
        camera_entity_id=camera_entity_id,
        event_context={"camera_entity_id": camera_entity_id, "source": "doorbell"},
        expires_in_ms=45000,
    )

    try:
        result = await camera_events.describe_doorbell(camera_entity_id)
    except Exception as exc:
        _LOGGER.warning("doorbell.describe_failed", exc=str(exc))
        result = {
            "camera_entity_id": camera_entity_id,
            "image_available": False,
            "description": "",
            "message": "Someone is at the door.",
            "suppressed": False,
        }

    if result["suppressed"]:
        _LOGGER.info("doorbell.no_person_visible", camera=result["camera_entity_id"])
        return DoorbellAnnounceResponse(
            status="ok",
            message="no_person_visible",
            camera_used=result["camera_entity_id"],
            wav_bytes=0,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    if result["description"]:
        _LOGGER.info("doorbell.described", chars=len(result["description"]))
    elif not result["image_available"]:
        _LOGGER.warning("doorbell.camera_unavailable", camera=result["camera_entity_id"])
    message = result["message"]

    # 2. Announce via the standard announce flow
    announce_resp = await announce_handler(
        AnnounceRequest(message=message, priority="alert", source="doorbell"),
        request,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("doorbell.done", elapsed_ms=elapsed_ms, wav_bytes=announce_resp.wav_bytes)

    return DoorbellAnnounceResponse(
        status="ok",
        message=message,
        camera_used=result["camera_entity_id"],
        wav_bytes=announce_resp.wav_bytes,
        elapsed_ms=elapsed_ms,
    )


async def _close_stream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


@router.get(
    "/camera/stream",
    dependencies=[Depends(verify_api_key)],
    include_in_schema=False,
    summary="Proxy a Home Assistant camera stream for authenticated avatar clients",
)
async def camera_stream_proxy(
    request: Request,
    entity_id: str = Query(..., min_length=1, description="HA camera entity ID"),
):
    ha = request.app.state.ha_proxy
    resolved_entity_id = ha.resolve_camera_entity(entity_id)
    stream_url = f"{ha.ha_url}/api/camera_proxy_stream/{resolved_entity_id}"

    client = httpx.AsyncClient(timeout=_STREAM_TIMEOUT)
    upstream_request = client.build_request("GET", stream_url, headers=ha.auth_headers)
    upstream_response = await client.send(upstream_request, stream=True)

    if upstream_response.status_code != 200:
        await _close_stream(upstream_response, client)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Camera stream unavailable for '{resolved_entity_id}'",
        )

    media_type = upstream_response.headers.get("content-type", "multipart/x-mixed-replace")
    return StreamingResponse(
        upstream_response.aiter_raw(),
        media_type=media_type,
        background=BackgroundTask(_close_stream, upstream_response, client),
    )


class MotionAnnounceRequest(BaseModel):
    camera_entity_id: str = Field(..., description="HA camera entity ID for the triggered camera")
    location:         str = Field("outdoors", max_length=64, description="Human-readable label used in the spoken message")


class MotionAnnounceResponse(BaseModel):
    status:      str
    message:     str
    camera_used: str
    archived:    bool = False
    wav_bytes:   int = 0
    elapsed_ms:  int = 0


class PackageAnnounceRequest(BaseModel):
    camera_entity_id: str | None = None
    trigger_entity_id: str = Field(default="binary_sensor.reolink_video_doorbell_poe_package")
    location: str = Field(default="front door", max_length=64)
    title: str = Field(default="Package Delivery", max_length=120)
    message: str = Field(default="A package was delivered.", max_length=300)


class PackageAnnounceResponse(BaseModel):
    status: str
    event_id: str
    event: str
    camera_used: str
    delivered: bool = True


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
      3. Archives a short clip and description for later AI search in admin

    camera_entity_id: HA camera entity ID (or use _OUTDOOR_CAMERAS aliases)
    location: human-readable label used in the fallback message (e.g. "the garden")

    Falls back to a generic "Motion detected" message if the camera is unavailable.
    """
    t0 = time.monotonic()
    camera_events = getattr(request.app.state, "camera_event_service", None)

    camera_id = camera_events.resolve_camera_entity(body.camera_entity_id)
    location  = body.location.strip() or "outdoors"
    cooldowns: dict[str, float] = getattr(request.app.state, "motion_announce_cooldowns", None)
    if cooldowns is None:
        cooldowns = {}
        request.app.state.motion_announce_cooldowns = cooldowns

    now_m = time.monotonic()
    since_last = now_m - cooldowns.get(camera_id, 0.0)
    if since_last < _MOTION_ANNOUNCE_COOLDOWN_S:
        _LOGGER.info(
            "motion.cooldown",
            camera=camera_id,
            location=location,
            seconds_remaining=int(_MOTION_ANNOUNCE_COOLDOWN_S - since_last),
        )
        return MotionAnnounceResponse(
            status="ok",
            message="motion_cooldown",
            camera_used=camera_id,
            archived=False,
            wav_bytes=0,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    cooldowns[camera_id] = now_m

    _LOGGER.info("motion.triggered", camera=camera_id, location=location)

    system_prompt: str = getattr(request.app.state, "system_prompt", "")
    motion_clip_service = request.app.state.motion_clip_service
    try:
        result = await camera_events.analyze_motion(
            camera_entity_id=camera_id,
            location=location,
            trigger_entity_id=body.camera_entity_id,
            source="announce_motion",
            system_prompt=system_prompt or None,
        )
        if result["suppressed"]:
            _LOGGER.info("motion.suppressed", camera=camera_id, reason="no_concern")
        elif result["image_available"]:
            _LOGGER.info("motion.described", chars=len(result["description"]))
        else:
            _LOGGER.warning("motion.camera_unavailable", camera=camera_id)
        message = result["message"]
    except Exception as exc:
        _LOGGER.warning("motion.describe_failed", exc=str(exc))
        result = {"canonical_event": None}
        message = f"Motion detected {location}."

    extra = {"source": "announce_motion"}
    if result.get("canonical_event") is not None:
        extra["canonical_event"] = result["canonical_event"]

    motion_clip_service.schedule_capture(
        camera_entity_id=camera_id,
        trigger_entity_id=body.camera_entity_id,
        location=location,
        description=message,
        extra=extra,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("motion.archived", elapsed_ms=elapsed_ms, camera=camera_id)

    return MotionAnnounceResponse(
        status="ok",
        message=message,
        camera_used=camera_id,
        archived=True,
        wav_bytes=0,
        elapsed_ms=elapsed_ms,
    )


@router.post(
    "/announce/package",
    response_model=PackageAnnounceResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Package alert — send a shared package camera event to avatar clients",
)
async def package_announce_handler(body: PackageAnnounceRequest, request: Request):
    ws_mgr: ConnectionManager = request.app.state.ws_manager
    camera_events = getattr(request.app.state, "camera_event_service", None)
    runtime = load_home_runtime_config()
    camera_entity_id = (
        body.camera_entity_id
        or runtime.default_doorbell_camera
        or _LEGACY_DEFAULT_DOORBELL_CAMERA
    )

    package_event = camera_events.build_package_event(
        camera_entity_id=camera_entity_id,
        source="package_announce",
        trigger_entity_id=body.trigger_entity_id,
        location=body.location.strip() or "front door",
        title=body.title.strip() or "Package Delivery",
        message=body.message.strip() or "A package was delivered.",
    )
    event_context = {
        "camera_entity_id": package_event["camera_entity_id"],
        "source": "package_announce",
        "trigger_entity_id": body.trigger_entity_id,
        "location": body.location.strip() or "front door",
    }
    if package_event.get("canonical_event") is not None:
        event_context["canonical_event"] = package_event["canonical_event"]

    event_id = await _broadcast_visual_event(
        request,
        ws_mgr,
        event="package_delivery",
        title=package_event["title"],
        message=package_event["message"],
        camera_entity_id=package_event["camera_entity_id"],
        event_context=event_context,
        expires_in_ms=45000,
    )
    return PackageAnnounceResponse(
        status="ok",
        event_id=event_id,
        event="package_delivery",
        camera_used=package_event["camera_entity_id"],
        delivered=True,
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


@router.get(
    "/tts/audio_mp3/{token}",
    include_in_schema=False,
    summary="Serve Alexa-compatible MP3 for SSML <audio> playback on Echo devices",
)
async def serve_tts_audio_mp3(token: str, request: Request):
    """Serve an Alexa-compatible MP3.

    Unlike the WAV endpoint, this does NOT pop the entry on first read because
    Amazon's servers may retry the fetch.  The entry expires naturally via TTL.
    """
    cache: dict = getattr(request.app.state, "audio_cache", {})
    entry = cache.get(f"mp3:{token}")
    if entry is None:
        raise HTTPException(status_code=404, detail="Audio not found or expired")
    data, expiry = entry
    if time.time() > expiry:
        cache.pop(f"mp3:{token}", None)
        raise HTTPException(status_code=404, detail="Audio not found or expired")
    return Response(content=data, media_type="audio/mpeg")


# ── WAV → Alexa-compatible MP3 conversion ─────────────────────────────────────

async def _wav_to_alexa_mp3(wav_bytes: bytes) -> bytes | None:
    """Convert WAV to Alexa-compatible MP3 using ffmpeg.

    Output format: stereo, libmp3lame, 48 kbps, 24 kHz, no Xing header.
    This matches the format the Alexa Media Player integration uses.
    """
    import tempfile
    import os

    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as inf:
            inf.write(wav_bytes)
            in_path = inf.name
        out_path = in_path.replace(".wav", "_alexa.mp3")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", in_path,
            "-ac", "2",
            "-codec:a", "libmp3lame",
            "-b:a", "48k",
            "-ar", "24000",
            "-write_xing", "0",
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)

        if proc.returncode != 0:
            _LOGGER.error("wav_to_mp3.ffmpeg_failed", returncode=proc.returncode,
                          stderr=stderr.decode()[:200] if stderr else "")
            return None

        with open(out_path, "rb") as f:
            mp3_bytes = f.read()

        _LOGGER.debug("wav_to_mp3.ok", wav_size=len(wav_bytes), mp3_size=len(mp3_bytes))
        return mp3_bytes

    except asyncio.TimeoutError:
        _LOGGER.error("wav_to_mp3.timeout")
        return None
    except FileNotFoundError:
        _LOGGER.error("wav_to_mp3.ffmpeg_not_found",
                      detail="ffmpeg is not installed — run: sudo apt install ffmpeg")
        return None
    except Exception as exc:
        _LOGGER.error("wav_to_mp3.error", exc=str(exc))
        return None
    finally:
        for path in (in_path, out_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# ── Media Fun Fact ────────────────────────────────────────────────────────────

class MediaFunFactRequest(BaseModel):
    media_title: str = Field(..., min_length=1, max_length=300)
    app_name: str = Field(default="", max_length=100)
    target_areas: list[str] = Field(default_factory=lambda: ["LIVING ROOM"])


class MediaFunFactResponse(BaseModel):
    status: str
    fun_fact: str
    wav_bytes: int = 0
    elapsed_ms: int = 0


@router.post(
    "/announce/media_fun_fact",
    response_model=MediaFunFactResponse,
    dependencies=[Depends(verify_api_key)],
    summary="Generate and announce a fun fact about currently playing media",
)
async def media_fun_fact_handler(body: MediaFunFactRequest, request: Request):
    """
    Called when Plex or Channels DVR starts playing on the living room TV.
    Nova generates an interesting fun fact about the media using the cloud LLM
    and announces it on living room speakers only.
    """
    t0 = time.monotonic()
    llm = getattr(request.app.state, "llm_service", None)
    if llm is None:
        raise HTTPException(status_code=503, detail="LLM service unavailable")

    title = body.media_title.strip()
    app = body.app_name.strip() or "your media player"
    target_areas = [str(a).strip() for a in body.target_areas if str(a).strip()] or ["LIVING ROOM"]

    _LOGGER.info("media_fun_fact.requested", title=title, app=app)

    prompt = (
        f'Someone just started watching "{title}" on {app}.\n'
        "Share one short, genuinely interesting fun fact about this movie or TV show. "
        "Keep it to 2 sentences maximum. "
        'Start with "Fun fact about" followed by the title, then the fact. '
        "Be conversational and friendly, as if speaking to someone in the living room. "
        "Do not give spoilers. Do not mention streaming platforms."
    )

    try:
        fun_fact = (await llm.generate_text_grounded(prompt, timeout_s=30.0)).strip()
    except Exception as exc:
        _LOGGER.warning("media_fun_fact.llm_failed", title=title, exc=str(exc))
        raise HTTPException(status_code=503, detail=f"LLM failed: {exc}")

    if not fun_fact:
        raise HTTPException(status_code=503, detail="LLM returned empty response")

    _LOGGER.info("media_fun_fact.generated", title=title, chars=len(fun_fact))

    announce_resp = await announce_handler(
        AnnounceRequest(message=fun_fact, priority="normal", target_areas=target_areas, source="media_fun_fact"),
        request,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("media_fun_fact.done", title=title, elapsed_ms=elapsed_ms)

    return MediaFunFactResponse(
        status="ok",
        fun_fact=fun_fact,
        wav_bytes=announce_resp.wav_bytes,
        elapsed_ms=elapsed_ms,
    )
    
