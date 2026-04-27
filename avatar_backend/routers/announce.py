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
from typing import Any, TYPE_CHECKING
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

if TYPE_CHECKING:
    from avatar_backend.bootstrap.container import AppContainer

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.services.home_runtime import load_home_runtime_config
from avatar_backend.services.event_service import publish_visual_event
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager
from avatar_backend.runtime_paths import data_dir

_LOGGER = structlog.get_logger()

# Global guard against runaway HA automations flooding TTS.
from avatar_backend.middleware.session_ratelimit import SessionRateLimiter as _SRL
_ANNOUNCE_LIMITER = _SRL(max_requests=20, window_s=60)

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


def _get_container(request: Request):
    """Avoid circular import: startup.py → announce.py → bootstrap."""
    return request.app.state._container


class AnnounceRequest(BaseModel):
    message:  str = Field(..., min_length=1, max_length=2000)
    priority: Literal["normal", "alert"] = "normal"
    target_areas: list[str] = Field(default_factory=list)
    room_id: str | None = None  # route to this room's avatar tablets only
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
async def announce_handler(body: AnnounceRequest, request: Request, container: AppContainer = Depends(_get_container)):
    """
    Speak *body.message* on all configured speakers and update the avatar state.

    priority="alert"  → avatar pulses red before speaking (doorbell, motion, etc.)
    priority="normal" → avatar goes straight to speaking state
    """
    t0 = time.monotonic()

    _ok, _ra = _ANNOUNCE_LIMITER.check("__global__")
    if not _ok:
        raise HTTPException(
            status_code=429,
            detail=f"Announce rate limit — retry in {_ra}s",
            headers={"Retry-After": str(_ra)},
        )

    tts:    TTSService        = container.tts_service
    speaker: SpeakerService   = getattr(container, "speaker_service", None)
    ws_mgr:  ConnectionManager = container.ws_manager
    surface_state = getattr(container, "surface_state_service", None)

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
        # Detect silent/failed TTS — check WAV duration vs text length
        # Real speech: ~150 bytes per char at 22050Hz mono 16-bit. Silent fallback: ~100ms.
        if wav_bytes and len(text) > 10:
            expected_min = len(text) * 50  # very conservative minimum
            if len(wav_bytes) < expected_min:
                raise RuntimeError("TTS returned suspiciously short audio")
    except Exception as exc:
        _LOGGER.error("announce.tts_error", exc=str(exc))
        # Fallback: send text-only to speakers via native Alexa/HA TTS
        if speaker and speaker.is_configured:
            try:
                await speaker.speak(text, target_areas=target_areas, area_aware=bool(target_areas))
                _LOGGER.info("announce.tts_fallback_to_speakers", chars=len(text))
            except Exception as fb_exc:
                _LOGGER.warning("announce.tts_fallback_failed", exc=str(fb_exc))
        if surface_state is not None:
            await surface_state.set_avatar_state(ws_mgr, "idle")
        else:
            await ws_mgr.broadcast_json({"type": "avatar_state", "state": "idle"})
        return AnnounceResponse(status="fallback", message=text, wav_bytes=0)

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
                cache = container.audio_cache
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

                # Use area-aware routing: prefer occupied rooms, fall back to all speakers
                _area_aware = True
                speaker_task = asyncio.create_task(
                    speaker.speak_wav(text, audio_url, mp3_url=mp3_url, target_areas=target_areas, area_aware=_area_aware)
                )
            else:
                _area_aware = True
                speaker_task = asyncio.create_task(
                    speaker.speak(text, target_areas=target_areas, area_aware=_area_aware)
                )
        except Exception as exc:
            _LOGGER.warning("announce.speaker_error", exc=str(exc))

    # Wait so the speaker has a head start before the browser plays
    if offset_s > 0 and speaker_task is not None:
        await asyncio.sleep(offset_s)

    # 5. Push word timings then audio to connected browser voice clients
    _room_id = getattr(body, "room_id", None)
    await ws_mgr.send_to_room_json(_room_id, {
        "type":         "announce",
        "text":         text,
        "priority":     body.priority,
        "word_timings": word_timings,
    }, fallback_to_all=True)
    await ws_mgr.send_to_room_bytes(_room_id, wav_bytes, fallback_to_all=True)

    # 6. Await speaker task
    if speaker_task is not None:
        try:
            await speaker_task
        except Exception as exc:
            _LOGGER.warning("announce.speaker_error", exc=str(exc))
    elif speaker and speaker.is_configured:
        # No speakers resolved (nobody home) — send mobile notification
        try:
            from avatar_backend.services.home_runtime import load_home_runtime_config
            _rt = load_home_runtime_config()
            if _rt.phone_notify_services:
                ha = container.ha_proxy
                for svc in _rt.phone_notify_services:
                    parts = svc.split("/")
                    if len(parts) == 2:
                        await ha.call_service(parts[0], parts[1], "", service_data={"title": "Nova", "message": text})
                _LOGGER.info("announce.mobile_fallback", chars=len(text))
        except Exception as exc:
            _LOGGER.warning("announce.mobile_fallback_failed", exc=str(exc))

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


_LEGACY_DEFAULT_DOORBELL_CAMERA = ""


@router.get(
    "/tts/audio/{token}",
    include_in_schema=False,
    summary="Serve a one-shot synthesised audio file",
)
async def serve_tts_audio(token: str, request: Request, container: AppContainer = Depends(_get_container)):
    """Serve a pre-synthesised WAV to HA media players then delete it from cache."""
    cache: dict = getattr(container, "audio_cache", {})
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
async def serve_tts_audio_mp3(token: str, request: Request, container: AppContainer = Depends(_get_container)):
    """Serve an Alexa-compatible MP3.

    Unlike the WAV endpoint, this does NOT pop the entry on first read because
    Amazon's servers may retry the fetch.  The entry expires naturally via TTL.
    """
    cache: dict = getattr(container, "audio_cache", {})
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
async def media_fun_fact_handler(body: MediaFunFactRequest, request: Request, container: AppContainer = Depends(_get_container)):
    """
    Called when Plex or Channels DVR starts playing on the living room TV.
    Nova generates an interesting fun fact about the media using the cloud LLM
    and announces it on living room speakers only.
    """
    t0 = time.monotonic()
    llm = getattr(container, "llm_service", None)
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
        request, container,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _LOGGER.info("media_fun_fact.done", title=title, elapsed_ms=elapsed_ms)

    return MediaFunFactResponse(
        status="ok",
        fun_fact=fun_fact,
        wav_bytes=announce_resp.wav_bytes if hasattr(announce_resp, "wav_bytes") else 0,
        elapsed_ms=elapsed_ms,
    )
    
