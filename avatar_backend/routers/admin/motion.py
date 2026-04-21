"""Motion sub-router: motion clips, announcements, all clip helpers."""
from __future__ import annotations

import asyncio
import structlog

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from avatar_backend.bootstrap.container import AppContainer, get_container

from .common import (
    _get_session,
    _require_session,
    MotionClipSearchBody,
    BulkDeleteBody,
)

_LOGGER = structlog.get_logger()
router = APIRouter()


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _serialize_motion_clip(clip: dict) -> dict:
    data = dict(clip)
    data["video_url"] = f"/admin/motion-clips/{clip['id']}/video" if clip.get("video_relpath") else ""
    data["thumb_url"] = f"/admin/motion-clips/{clip['id']}/thumb" if clip.get("thumb_relpath") else ""
    data["flagged"] = bool(clip.get("flagged"))
    extra = data.get("extra") or {}
    canonical_event = data.get("canonical_event") or extra.get("canonical_event") or {}
    data["canonical_event"] = canonical_event
    data["canonical_event_id"] = data.get("canonical_event_id") or canonical_event.get("event_id") or ""
    data["canonical_event_type"] = data.get("canonical_event_type") or canonical_event.get("event_type") or ""
    data["event_source"] = (
        canonical_event.get("event_context", {}).get("source")
        or extra.get("source")
        or ""
    )
    return data


# Cache of clip_id -> bool so ffprobe only runs once per clip per process lifetime.
_playable_cache: dict[int, bool] = {}


# L4 security fix: async subprocess to avoid blocking the event loop
async def _motion_clip_is_playable(request: Request, clip: dict, container: AppContainer | None = None) -> bool:
    clip_id = clip.get("id")
    if clip_id is not None and clip_id in _playable_cache:
        return _playable_cache[clip_id]
    svc = (container if container is not None else request.app.state._container).motion_clip_service
    path = svc.clip_path_for(clip)
    if not path or not path.exists():
        if clip_id is not None:
            _playable_cache[clip_id] = False
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            if clip_id is not None:
                _playable_cache[clip_id] = False
            return False
        result = float((stdout or b"0").decode("utf-8", "ignore").strip() or "0") > 0.5
        if clip_id is not None:
            _playable_cache[clip_id] = result
        return result
    except Exception:
        return False


async def _filter_playable(request: Request, clips: list[dict], container: AppContainer | None = None) -> list[dict]:
    """Run all playability checks concurrently instead of sequentially."""
    flags = await asyncio.gather(*[_motion_clip_is_playable(request, c, container) for c in clips])
    return [c for c, ok in zip(clips, flags) if ok]


# ── Announcements ─────────────────────────────────────────────────────────────

@router.get("/announcements")
async def list_announcements(request: Request, limit: int = 200):
    """Return recent announcements from the JSONL log file, newest first."""
    _require_session(request, min_role="viewer")
    import json as _json
    from avatar_backend.runtime_paths import data_dir
    log_path = data_dir() / "announcements.jsonl"
    entries: list[dict] = []
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(_json.loads(line))
                except Exception:
                    pass
                if len(entries) >= limit:
                    break
        except Exception as exc:
            _LOGGER.warning("admin.announcements_read_failed", exc=str(exc))
    return {"announcements": entries, "total": len(entries)}


@router.delete("/announcements")
async def clear_announcements(request: Request):
    """Truncate the announcement log."""
    _require_session(request, min_role="admin")
    from avatar_backend.runtime_paths import data_dir
    log_path = data_dir() / "announcements.jsonl"
    try:
        log_path.write_text("", encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"cleared": True}


# ── Motion clips ─────────────────────────────────────────────────────────────

@router.get("/motion-clips")
async def list_motion_clips(
    request: Request,
    limit: int = 60,
    date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    camera_entity_id: str | None = None,
    canonical_event_type: str | None = None,
    container: AppContainer = Depends(get_container),
):
    _require_session(request, min_role="viewer")
    db = container.metrics_db
    clips = db.recent_motion_clips(
        limit=max(1, min(limit, 200)),
        date=date,
        start_time=start_time,
        end_time=end_time,
        camera_entity_id=camera_entity_id,
        canonical_event_type=canonical_event_type,
    )
    clips = await _filter_playable(request, clips)
    return {"clips": [_serialize_motion_clip(clip) for clip in clips]}


@router.post("/motion-clips/search")
async def search_motion_clips(body: MotionClipSearchBody, request: Request, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    svc = container.motion_clip_service
    result = await svc.search(
        query=body.query or "",
        date=body.date,
        start_time=body.start_time,
        end_time=body.end_time,
        camera_entity_id=body.camera_entity_id,
        canonical_event_type=body.canonical_event_type,
    )
    clips = await _filter_playable(request, result.get("clips", []))
    return {
        "mode": result.get("mode", "recent"),
        "clips": [_serialize_motion_clip(clip) for clip in clips],
    }


@router.get("/motion-clips/{clip_id}/video", include_in_schema=False)
async def serve_motion_clip_video(clip_id: int, request: Request, download: int = 0, container: AppContainer = Depends(get_container)):
    _require_session(request, min_role="viewer")
    db = container.metrics_db
    svc = container.motion_clip_service
    clip = db.get_motion_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Motion clip not found")
    if not await _motion_clip_is_playable(request, clip, container):
        raise HTTPException(status_code=404, detail="Motion clip is not playable")
    path = svc.clip_path_for(clip)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Motion clip file unavailable")
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(str(path), media_type="video/mp4", filename=path.name, headers=headers)


@router.delete("/motion-clips/{clip_id}")
async def delete_motion_clip(clip_id: int, request: Request, container: AppContainer = Depends(get_container)):
    """Delete a single motion clip (DB row + video file)."""
    _require_session(request, min_role="admin")
    db = container.metrics_db
    svc = container.motion_clip_service
    clip = db.get_motion_clip(clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Motion clip not found")
    relpath = db.delete_motion_clip(clip_id)
    if relpath:
        path = svc.clip_path_for({"video_relpath": relpath})
        if path and path.exists():
            try:
                path.unlink()
            except Exception:
                pass
    return {"deleted": clip_id}


@router.post("/motion-clips/delete")
async def delete_motion_clips_bulk(body: BulkDeleteBody, request: Request, container: AppContainer = Depends(get_container)):
    """Delete multiple clips by ID, or all clips if delete_all=true."""
    _require_session(request, min_role="admin")
    db = container.metrics_db
    svc = container.motion_clip_service

    if body.delete_all:
        relpaths = db.delete_all_motion_clips()
    elif body.ids:
        relpaths = db.delete_motion_clips_bulk(body.ids)
    else:
        return {"deleted": 0}

    deleted_files = 0
    for relpath in relpaths:
        path = svc.clip_path_for({"video_relpath": relpath})
        if path and path.exists():
            try:
                path.unlink()
                deleted_files += 1
            except Exception:
                pass

    return {"deleted": len(relpaths), "files_removed": deleted_files}


@router.get("/motion-clips/{clip_id}/thumb", include_in_schema=False)
async def serve_motion_clip_thumb(clip_id: int, request: Request, container: AppContainer = Depends(get_container)):
    """Serve the thumbnail JPEG for a clip."""
    _require_session(request, min_role="viewer")
    db = container.metrics_db
    clip = db.get_motion_clip(clip_id)
    if not clip or not clip.get("thumb_relpath"):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    svc = container.motion_clip_service
    path = svc.clip_path_for({"video_relpath": clip["thumb_relpath"]})
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail file missing")
    return FileResponse(str(path), media_type="image/jpeg")


@router.post("/motion-clips/{clip_id}/flag")
async def toggle_motion_clip_flag(clip_id: int, request: Request, container: AppContainer = Depends(get_container)):
    """Toggle the flagged/starred state of a clip."""
    _require_session(request, min_role="admin")
    db = container.metrics_db
    new_state = db.toggle_motion_clip_flag(clip_id)
    return {"clip_id": clip_id, "flagged": new_state}


@router.get("/motion-clips/stats")
async def get_motion_clip_stats(request: Request, container: AppContainer = Depends(get_container)):
    """Return aggregate stats for the motion clip archive."""
    _require_session(request, min_role="viewer")
    db = container.metrics_db
    stats = db.motion_clip_stats()
    # Calculate disk usage
    svc = container.motion_clip_service
    clips_dir = svc._clips_dir
    total_bytes = 0
    try:
        for f in clips_dir.rglob("*"):
            if f.is_file():
                total_bytes += f.stat().st_size
    except Exception:
        pass
    stats["disk_usage_mb"] = round(total_bytes / (1024 * 1024), 1)
    from avatar_backend.services.home_runtime import load_home_runtime_config
    stats["camera_labels"] = load_home_runtime_config().camera_labels
    return stats


@router.post("/motion-clips/backfill-thumbnails")
async def backfill_thumbnails(request: Request, container: AppContainer = Depends(get_container)):
    """Generate thumbnails for all existing clips that don't have one."""
    _require_session(request, min_role="admin")
    svc = container.motion_clip_service
    result = await svc.backfill_thumbnails()
    return result
