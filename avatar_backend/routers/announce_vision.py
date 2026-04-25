"""
Vision-triggered announcement endpoints: visual events, doorbell, camera stream,
motion announce, and package announce.
"""
from __future__ import annotations
import asyncio
import time
from typing import Any, TYPE_CHECKING

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
from avatar_backend.routers.announce import (
    AnnounceRequest,
    announce_handler,
    _LEGACY_DEFAULT_DOORBELL_CAMERA,
)
from avatar_backend.services.event_service import publish_visual_event
from avatar_backend.services.home_runtime import load_home_runtime_config
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager

_LOGGER = structlog.get_logger()

router = APIRouter()


def _get_container(request: Request):
    """Avoid circular import: startup.py → announce.py → bootstrap."""
    return request.app.state._container


_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
_MOTION_ANNOUNCE_COOLDOWN_S = 600  # 10 minutes per camera for direct /announce/motion calls


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
    container: AppContainer,
    ws_mgr: ConnectionManager,
    *,
    app,
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
    event_service = getattr(container, "event_service", None)
    surface_state = getattr(container, "surface_state_service", None)
    await publish_visual_event(
        app=app,
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
async def visual_event_handler(body: VisualEventRequest, request: Request, container: AppContainer = Depends(_get_container)):
    ws_mgr: ConnectionManager = container.ws_manager
    event_id = await _broadcast_visual_event(
        container,
        ws_mgr,
        app=request.app,
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
async def doorbell_announce_handler(body: DoorbellAnnounceRequest, request: Request, container: AppContainer = Depends(_get_container)):
    """
    Called when the doorbell rings. Nova:
      1. Captures a snapshot from the doorbell camera
      2. Describes what it sees using vision AI
      3. Announces the result on all speakers with priority="alert"

    Falls back to a generic "Someone is at the door" if the camera is unavailable.
    """
    t0 = time.monotonic()
    ws_mgr: ConnectionManager = container.ws_manager
    camera_events = getattr(container, "camera_event_service", None)
    runtime = load_home_runtime_config()
    camera_entity_id = (
        body.camera_entity_id
        or runtime.default_doorbell_camera
        or _LEGACY_DEFAULT_DOORBELL_CAMERA
    )

    _LOGGER.info("doorbell.triggered", camera=camera_entity_id)
    await _broadcast_visual_event(
        container,
        ws_mgr,
        app=request.app,
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
    wav_bytes = announce_resp.wav_bytes if hasattr(announce_resp, "wav_bytes") else 0
    _LOGGER.info("doorbell.done", elapsed_ms=elapsed_ms, wav_bytes=wav_bytes)

    return DoorbellAnnounceResponse(
        status="ok",
        message=message,
        camera_used=result["camera_entity_id"],
        wav_bytes=wav_bytes,
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
    container: AppContainer = Depends(_get_container),
):
    ha = container.ha_proxy
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
    trigger_entity_id: str = Field(default="")
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
async def motion_announce_handler(body: MotionAnnounceRequest, request: Request, container: AppContainer = Depends(_get_container)):
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
    camera_events = getattr(container, "camera_event_service", None)

    camera_id = camera_events.resolve_camera_entity(body.camera_entity_id)
    location  = body.location.strip() or "outdoors"
    cooldowns: dict[str, float] = container.motion_announce_cooldowns

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

    system_prompt: str = getattr(container, "system_prompt", "")
    motion_clip_service = container.motion_clip_service
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
async def package_announce_handler(body: PackageAnnounceRequest, request: Request, container: AppContainer = Depends(_get_container)):
    ws_mgr: ConnectionManager = container.ws_manager
    camera_events = getattr(container, "camera_event_service", None)
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
        container,
        ws_mgr,
        app=request.app,
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
