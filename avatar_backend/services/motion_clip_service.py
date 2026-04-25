from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
from avatar_backend.services._shared_http import _http_client

from avatar_backend.runtime_paths import data_dir
from avatar_backend.services.perceptual_hash import compute_phash, hamming_distance

_LOGGER = structlog.get_logger()
_SAFE_PATH_CHARS = re.compile(r"[^a-z0-9_-]+")


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

from avatar_backend.services.clip_capture_mixin import ClipCaptureMixin
from avatar_backend.services.clip_manage_mixin import ClipManageMixin

class MotionClipService(ClipCaptureMixin, ClipManageMixin):
    def __init__(
        self,
        *,
        db,
        ha_proxy,
        llm_service,
        issue_autofix_service=None,
        clip_duration_s: int = 8,
        max_search_candidates: int = 120,
        max_search_results: int = 24,
        retention_days: int = 30,
    ) -> None:
        self._db = db
        self._ha = ha_proxy
        self._llm = llm_service
        self._issue_autofix_service = issue_autofix_service
        self._clip_duration_s = max(3, int(clip_duration_s))
        self._max_search_candidates = max(20, int(max_search_candidates))
        self._max_search_results = max(5, int(max_search_results))
        self._retention_days = max(0, int(retention_days))
        self._clips_dir = data_dir() / "motion_clips"
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._clips_dir_ready = self._ensure_clips_dir_ready()
        self._tasks: set[asyncio.Task] = set()
        self._pending_updates: dict[str, dict[str, Any]] = {}  # clip_handle → {description, extra}
        self._cancelled_handles: set[str] = set()
        from avatar_backend.services.home_runtime import load_home_runtime_config
        _rt = load_home_runtime_config()
        self._POLLING_ONLY_CAMERAS = set(getattr(_rt, 'polling_only_cameras', []))
        self._capture_semaphore: asyncio.Semaphore | None = None  # initialised lazily in async context
        self._phash_cache: dict[str, tuple[int, float]] = {}  # camera_entity_id → (hash, monotonic_ts)
        self._handle_to_clip_id: dict[str, int] = {}  # clip_handle → clip_id after DB insert

    @property
    def _local_ha_url(self) -> str:
        """Return a local URL for HA — avoids DNS/NAT hairpin for camera streams."""
        from avatar_backend.config import get_settings
        return get_settings().ha_local_url_resolved

    async def refresh_storage_status(self) -> bool:
        self._clips_dir_ready = self._ensure_clips_dir_ready()
        if self._clips_dir_ready and self._issue_autofix_service is not None:
            await self._issue_autofix_service.resolve_issue(
                "motion_clip_storage_unavailable",
                source="motion_clip_service",
            )
        return self._clips_dir_ready

    def _ensure_clips_dir_ready(self) -> bool:
        try:
            self._clips_dir.mkdir(parents=True, exist_ok=True)
            probe = self._clips_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except Exception as exc:
            _LOGGER.warning(
                "motion_clip.storage_unavailable",
                clips_dir=str(self._clips_dir),
                exc=_format_exc(exc),
            )
            return False

    def schedule_capture(
        self,
        *,
        camera_entity_id: str,
        trigger_entity_id: str = "",
        location: str = "",
        description: str = "",
        extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Schedule a clip capture. Returns a handle for updating the description later.

        Deduplicates per-camera: if a capture for this camera is already in-flight,
        the new request is dropped to prevent concurrent ffmpeg processes from
        exhausting memory.  A global semaphore (max 4) further caps concurrency.
        """
        task_name = f"motion_clip:{camera_entity_id}"
        for t in self._tasks:
            if t.get_name() == task_name and not t.done():
                _LOGGER.info(
                    "motion_clip.capture_skipped_already_running",
                    camera=camera_entity_id,
                )
                return None

        import uuid
        clip_handle = str(uuid.uuid4())[:12]
        task = asyncio.create_task(
            self.capture_and_store(
                camera_entity_id=camera_entity_id,
                trigger_entity_id=trigger_entity_id,
                location=location,
                description=description,
                extra=extra or {},
                clip_handle=clip_handle,
            ),
            name=task_name,
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return clip_handle

    def cancel_pending(self, clip_handle: str) -> None:
        """Mark a pending clip as cancelled — it will be deleted after capture completes."""
        if clip_handle:
            self._cancelled_handles.add(clip_handle)

    def update_pending_description(
        self,
        clip_handle: str,
        *,
        description: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Update the description/extra for a clip. Works whether clip is still
        being captured (pending) or already stored in DB."""
        self._pending_updates[clip_handle] = {
            "description": description,
            **(extra or {}),
        }
        # If clip is already in DB, update it directly
        clip_id = self._handle_to_clip_id.get(clip_handle)
        if clip_id and description:
            import json as _json
            self._db._write(
                "UPDATE motion_clips SET description = ? WHERE id = ?",
                (description, clip_id),
            )
            if extra:
                try:
                    row = self._db._conn().execute(
                        "SELECT extra_json FROM motion_clips WHERE id = ?", (clip_id,)
                    ).fetchone()
                    merged = _json.loads(row["extra_json"]) if row else {}
                    merged.update(extra)
                    self._db._write(
                        "UPDATE motion_clips SET extra_json = ? WHERE id = ?",
                        (_json.dumps(merged), clip_id),
                    )
                except Exception:
                    pass
            _LOGGER.info("motion_clip.description_updated_post_store",
                         clip_id=clip_id, chars=len(description))

    async def capture_and_store(
        self,
        *,
        camera_entity_id: str,
        trigger_entity_id: str = "",
        location: str = "",
        description: str = "",
        extra: dict[str, Any] | None = None,
        clip_handle: str = "",
    ) -> int | None:
        if not self._clips_dir_ready:
            _LOGGER.warning(
                "motion_clip.capture_skipped_storage_unavailable",
                camera=camera_entity_id,
                clips_dir=str(self._clips_dir),
            )
            if self._issue_autofix_service is not None:
                await self._issue_autofix_service.report_issue(
                    "motion_clip_storage_unavailable",
                    source="motion_clip.capture_and_store",
                    summary="Motion clip storage is unavailable",
                    details={"camera_entity_id": camera_entity_id, "clips_dir": str(self._clips_dir)},
                )
            return None

        # --- Perceptual hash dedup check ---
        try:
            import httpx as _httpx  # still needed for Timeout/Limits in polling loop
            frame_url = f"{self._ha.ha_url}/api/camera_proxy/{camera_entity_id}"
            resp = await _http_client().get(
                frame_url, headers=self._ha.auth_headers,
                timeout=_httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=5.0),
            )
            if resp.status_code == 200 and resp.content and len(resp.content) > 1000:
                new_hash = await compute_phash(resp.content)
                cached = self._phash_cache.get(camera_entity_id)
                if cached is not None:
                    old_hash, old_ts = cached
                    cache_age = time.monotonic() - old_ts
                    dist = hamming_distance(old_hash, new_hash)
                    if dist <= 5 and cache_age < 120:
                        _LOGGER.info(
                            "motion_clip.dedup_skipped",
                            camera=camera_entity_id,
                            hamming_distance=dist,
                            cache_age_s=round(cache_age, 1),
                        )
                        return None
                self._phash_cache[camera_entity_id] = (new_hash, time.monotonic())
        except Exception as exc:
            _LOGGER.debug(
                "motion_clip.phash_skipped",
                camera=camera_entity_id,
                exc=_format_exc(exc),
            )

        now = datetime.now(timezone.utc)
        relpath = self._build_relpath(camera_entity_id, now)
        fullpath = self._clips_dir / relpath
        try:
            fullpath.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            _LOGGER.warning(
                "motion_clip.capture_parent_mkdir_failed",
                camera=camera_entity_id,
                path=str(fullpath.parent),
                exc=_format_exc(exc),
            )
            if self._issue_autofix_service is not None:
                await self._issue_autofix_service.report_issue(
                    "motion_clip_storage_unavailable",
                    source="motion_clip.capture_parent_mkdir_failed",
                    summary="Motion clip storage parent directory could not be created",
                    details={"camera_entity_id": camera_entity_id, "path": str(fullpath.parent)},
                )
            return None
        if self._issue_autofix_service is not None:
            await self._issue_autofix_service.resolve_issue(
                "motion_clip_storage_unavailable",
                source="motion_clip.capture_and_store",
            )

        # Initialise semaphore lazily (must be created inside a running event loop)
        if self._capture_semaphore is None:
            self._capture_semaphore = asyncio.Semaphore(2)

        status = "ready"
        async with self._capture_semaphore:
            if not await self._capture_clip(camera_entity_id, fullpath):
                status = "capture_failed"

        # Check for updated description from vision (arrived while clip was recording)
        if clip_handle and clip_handle in self._pending_updates:
            update = self._pending_updates.pop(clip_handle)
            if update.get("description"):
                description = update["description"]
            # Merge extra fields from the vision result
            merged_extra = dict(extra or {})
            for k, v in update.items():
                if k != "description":
                    merged_extra[k] = v
            extra = merged_extra

        # Generate thumbnail from first frame — retry once if first attempt fails
        # (common when concurrent encodes are saturating CPU)
        thumb_relpath = ""
        if status == "ready":
            thumb_path = fullpath.with_suffix(".thumb.jpg")
            for _attempt in range(2):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "/usr/bin/ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                        "-i", str(fullpath), "-vframes", "1", "-q:v", "3",
                        "-vf", "scale=320:-1", str(thumb_path),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=15)
                    if proc.returncode == 0 and thumb_path.exists():
                        thumb_relpath = str(relpath.with_suffix(".thumb.jpg"))
                        break
                except Exception as exc:
                    _LOGGER.debug("motion_clip.thumb_failed", camera=camera_entity_id, exc=str(exc), attempt=_attempt)
                if _attempt == 0:
                    await asyncio.sleep(2)  # brief pause before retry

        # If vision determined NO_MOTION, discard the clip instead of archiving
        if clip_handle and clip_handle in self._cancelled_handles:
            self._cancelled_handles.discard(clip_handle)
            self._pending_updates.pop(clip_handle, None)
            if fullpath.exists():
                fullpath.unlink(missing_ok=True)
            if thumb_relpath:
                thumb_file = self._clips_dir / thumb_relpath
                if thumb_file.exists():
                    thumb_file.unlink(missing_ok=True)
            _LOGGER.info("motion_clip.cancelled", camera=camera_entity_id,
                         detail="vision returned NO_MOTION — clip discarded")
            return

        clip_id = self._db.insert_motion_clip({
            "ts": now.isoformat(),
            "camera_entity_id": camera_entity_id,
            "trigger_entity_id": trigger_entity_id,
            "location": location,
            "description": description,
            "video_relpath": str(relpath) if status == "ready" else "",
            "thumb_relpath": thumb_relpath,
            "status": status,
            "duration_s": self._clip_duration_s,
            "llm_provider": getattr(self._llm, "gemini_vision_provider_name", getattr(self._llm, "provider_name", "")),
            "llm_model": getattr(self._llm, "gemini_vision_effective_model_name", getattr(self._llm, "model_name", "")),
            "extra": extra or {},
        })
        if clip_handle:
            self._handle_to_clip_id[clip_handle] = clip_id
        _LOGGER.info(
            "motion_clip.stored",
            clip_id=clip_id,
            camera=camera_entity_id,
            status=status,
            relpath=str(relpath) if status == "ready" else "",
        )
        return clip_id
