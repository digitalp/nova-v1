from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from avatar_backend.runtime_paths import data_dir

_LOGGER = structlog.get_logger()
_SAFE_PATH_CHARS = re.compile(r"[^a-z0-9_-]+")


def _format_exc(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class MotionClipService:
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
        self._capture_semaphore: asyncio.Semaphore | None = None  # initialised lazily in async context

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

    def update_pending_description(
        self,
        clip_handle: str,
        *,
        description: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Update the description/extra for a clip that's still being captured."""
        self._pending_updates[clip_handle] = {
            "description": description,
            **(extra or {}),
        }

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
        _LOGGER.info(
            "motion_clip.stored",
            clip_id=clip_id,
            camera=camera_entity_id,
            status=status,
            relpath=str(relpath) if status == "ready" else "",
        )
        return clip_id

    async def search(
        self,
        *,
        query: str,
        date: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        camera_entity_id: str | None = None,
        canonical_event_type: str | None = None,
    ) -> dict[str, Any]:
        candidates = self._db.recent_motion_clips(
            limit=self._max_search_candidates,
            date=date,
            start_time=start_time,
            end_time=end_time,
            camera_entity_id=camera_entity_id,
            canonical_event_type=canonical_event_type,
        )
        if not query.strip():
            return {"clips": candidates[: self._max_search_results], "mode": "recent"}
        if not candidates:
            return {"clips": [], "mode": "empty"}

        ranked_ids = await self._rank_candidates(query, candidates)
        if ranked_ids:
            order = {clip_id: idx for idx, clip_id in enumerate(ranked_ids)}
            clips = [clip for clip in candidates if clip["id"] in order]
            clips.sort(key=lambda clip: order[clip["id"]])
            return {"clips": clips[: self._max_search_results], "mode": "ai"}

        fallback = self._keyword_match(query, candidates)
        return {"clips": fallback[: self._max_search_results], "mode": "keyword"}

    async def _rank_candidates(self, query: str, candidates: list[dict[str, Any]]) -> list[int]:
        lines = []
        for clip in candidates[: self._max_search_candidates]:
            lines.append(
                f'{clip["id"]} | {clip["ts"]} | {clip["camera_entity_id"]} | '
                f'{clip.get("location", "")} | {clip.get("description", "")}'
            )
        prompt = (
            "You are ranking motion-video search results for a home security archive.\n"
            f'User query: "{query.strip()}"\n\n'
            "Return JSON only in the form {\"ids\": [..]} with the most relevant clip ids first.\n"
            "Use only ids from this candidate list. Prefer clips that directly answer the query.\n\n"
            "Candidates:\n" + "\n".join(lines)
        )
        try:
            raw = (await self._llm.generate_text_local(prompt, timeout_s=60.0)).strip()
        except Exception as exc:
            _LOGGER.warning("motion_clip.search_llm_failed", exc=_format_exc(exc))
            return []
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return []
        try:
            payload = json.loads(match.group())
        except json.JSONDecodeError:
            return []
        ids = []
        valid_ids = {int(clip["id"]) for clip in candidates}
        for value in payload.get("ids", []):
            try:
                clip_id = int(value)
            except Exception:
                continue
            if clip_id in valid_ids and clip_id not in ids:
                ids.append(clip_id)
        return ids

    def clip_path_for(self, clip: dict[str, Any]) -> Path | None:
        relpath = str(clip.get("video_relpath") or "").strip()
        if not relpath:
            return None
        fullpath = (self._clips_dir / relpath).resolve()
        try:
            fullpath.relative_to(self._clips_dir.resolve())
        except ValueError:
            return None
        return fullpath

    async def _capture_clip(self, camera_entity_id: str, output_path: Path) -> bool:
        # Mainstream/profile000 cameras don't support MJPEG streaming via HA proxy.
        # Go straight to polling to avoid a 40-second timeout waiting for a stream
        # that will never arrive.
        if "mainstream" in camera_entity_id or "profile000" in camera_entity_id:
            _LOGGER.info("motion_clip.mainstream_polling", camera=camera_entity_id,
                         detail="Mainstream camera — using polling for clip capture")
            return await self._capture_clip_polling(camera_entity_id, output_path)

        # Stream directly from HA's MJPEG proxy endpoint.
        token = self._ha.auth_headers.get("Authorization", "").removeprefix("Bearer ").strip()
        stream_url = f"{self._ha.ha_url}/api/camera_proxy_stream/{camera_entity_id}"

        cmd = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel", "error",
            # Short connection timeout — fail fast if stream doesn't start
            "-timeout", "5000000",  # 5 seconds in microseconds
            "-headers", f"Authorization: Bearer {token}\r\n",
            "-i", stream_url,
            "-t", str(self._clip_duration_s),
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-r", "25",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        _LOGGER.info("motion_clip.capture_start", camera=camera_entity_id, duration_s=self._clip_duration_s)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._clip_duration_s + 30,
                )
            except asyncio.TimeoutError:
                proc.kill()
                _, stderr = await proc.communicate()
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass
                _LOGGER.warning("motion_clip.capture_timeout", camera=camera_entity_id,
                                detail="MJPEG stream timed out — falling back to polling")
                return await self._capture_clip_polling(camera_entity_id, output_path)
        except Exception as exc:
            _LOGGER.warning("motion_clip.capture_spawn_failed", camera=camera_entity_id, exc=_format_exc(exc))
            return await self._capture_clip_polling(camera_entity_id, output_path)

        if proc.returncode != 0 or not await self._is_valid_clip(output_path):
            stderr_text = (stderr or b"").decode("utf-8", "ignore")[:400]
            _LOGGER.warning(
                "motion_clip.capture_failed",
                camera=camera_entity_id,
                returncode=proc.returncode,
                stderr=stderr_text,
            )
            # If MJPEG stream failed (camera offline / unsupported), fall back to
            # the snapshot-polling method so we still get a clip.
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            _LOGGER.info("motion_clip.capture_fallback_to_polling", camera=camera_entity_id)
            return await self._capture_clip_polling(camera_entity_id, output_path)
        return True

    async def _capture_clip_polling(self, camera_entity_id: str, output_path: Path) -> bool:
        """Poll /api/camera_proxy with concurrent requests for higher frame rates.
        Uses multiple in-flight requests to keep the pipeline full — each request
        takes ~0.5-1s for mainstream cameras, so 4 concurrent requests yield ~4-8 fps
        instead of the ~1 fps from sequential polling."""
        import httpx as _httpx

        frame_dir = Path(tempfile.mkdtemp(prefix="motion_frames_", dir=str(self._clips_dir)))
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self._clip_duration_s
        url = f"{self._ha.ha_url}/api/camera_proxy/{camera_entity_id}"
        headers = self._ha.auth_headers

        # Sequential polling with keep-alive — one request at a time to avoid
        # corrupted frames from overloading the HA camera proxy. Keep-alive
        # reuses the TCP connection so each request is faster than cold starts.
        write_index = 0
        try:
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=5.0),
                limits=_httpx.Limits(max_keepalive_connections=1, max_connections=1),
            ) as client:
                while loop.time() < deadline:
                    try:
                        resp = await client.get(url, headers=headers)
                        if resp.status_code == 200 and resp.content and len(resp.content) > 1000:
                            # Validate JPEG: must start with FFD8 and end with FFD9
                            if resp.content[:2] == b'\xff\xd8' and resp.content[-2:] == b'\xff\xd9':
                                (frame_dir / f"frame_{write_index:04d}.jpg").write_bytes(resp.content)
                                write_index += 1
                    except Exception:
                        await asyncio.sleep(0.1)
        except Exception as exc:
            _LOGGER.warning("motion_clip.poll_sample_failed", camera=camera_entity_id, exc=_format_exc(exc))
            shutil.rmtree(frame_dir, ignore_errors=True)
            return False

        captured = write_index
        elapsed = max(0.1, loop.time() - started)

        if captured < 3:
            shutil.rmtree(frame_dir, ignore_errors=True)
            _LOGGER.warning("motion_clip.poll_insufficient_frames", camera=camera_entity_id, frames=captured)
            _LOGGER.debug("motion_clip.poll_debug_info", camera=camera_entity_id, elapsed=elapsed)
            return False

        actual_fps = captured / elapsed
        _LOGGER.info(
            "motion_clip.poll_complete",
            camera=camera_entity_id,
            frames=captured,
            elapsed_s=round(elapsed, 2),
            actual_fps=round(actual_fps, 2),
        )
        # Use minterpolate for any capture rate ≥1.5 fps — produces smooth 25fps
        # output by synthesizing intermediate frames via motion compensation.
        # Below 1.5 fps the gaps are too large for useful interpolation.
        if actual_fps >= 1.5:
            vf_filters = "minterpolate=fps=25:mi_mode=mci:mc_mode=obmc"
        else:
            vf_filters = "fps=fps=25"
        cmd = [
            "/usr/bin/ffmpeg",
            "-nostdin", "-y", "-loglevel", "error",
            "-framerate", f"{actual_fps:.4f}",
            "-i", str(frame_dir / "frame_%04d.jpg"),
            "-an",
            "-vf", vf_filters,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._clip_duration_s + 60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                output_path.unlink(missing_ok=True)
                _LOGGER.warning("motion_clip.poll_encode_timeout", camera=camera_entity_id)
                return False
        except Exception as exc:
            _LOGGER.warning("motion_clip.poll_encode_failed", camera=camera_entity_id, exc=_format_exc(exc))
            return False
        finally:
            shutil.rmtree(frame_dir, ignore_errors=True)
        if proc.returncode != 0 or not await self._is_valid_clip(output_path):
            output_path.unlink(missing_ok=True)
            return False
        return True

    async def _is_valid_clip(self, output_path: Path) -> bool:
        if not output_path.exists() or output_path.stat().st_size < 1024:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        try:
            return float((stdout or b"0").decode("utf-8", "ignore").strip() or "0") > 0.5
        except ValueError:
            return False

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except Exception as exc:
            _LOGGER.warning("motion_clip.task_failed", exc=_format_exc(exc))

    def _build_relpath(self, camera_entity_id: str, now: datetime) -> Path:
        camera_slug = _SAFE_PATH_CHARS.sub("-", camera_entity_id.lower()).strip("-") or "camera"
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        return Path(now.strftime("%Y/%m/%d")) / f"{stamp}_{camera_slug}.mp4"

    def _keyword_match(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        terms = [term for term in re.split(r"[^a-z0-9]+", query.lower()) if term]
        if not terms:
            return candidates
        scored: list[tuple[int, dict[str, Any]]] = []
        for clip in candidates:
            haystack = " ".join([
                str(clip.get("description", "")),
                str(clip.get("location", "")),
                str(clip.get("camera_entity_id", "")),
                str(clip.get("trigger_entity_id", "")),
            ]).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, clip))
        scored.sort(key=lambda item: (-item[0], item[1].get("ts", "")), reverse=False)
        return [clip for _, clip in scored] or candidates

    async def run_cleanup(self) -> dict[str, int]:
        """Delete clips older than retention_days. Flagged clips are preserved.
        Returns stats about what was cleaned up."""
        if self._retention_days <= 0:
            return {"skipped": True, "reason": "retention disabled"}
        relpaths = self._db.delete_old_motion_clips(self._retention_days)
        deleted_files = 0
        for relpath in relpaths:
            fullpath = (self._clips_dir / relpath).resolve()
            if fullpath.exists():
                try:
                    fullpath.unlink()
                    deleted_files += 1
                except Exception:
                    pass
        # Clean up empty date directories
        try:
            for d in sorted(self._clips_dir.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        except Exception:
            pass
        if relpaths:
            _LOGGER.info("motion_clip.cleanup_complete",
                         db_rows=len(relpaths), files_removed=deleted_files,
                         retention_days=self._retention_days)
        return {"db_rows_deleted": len(relpaths), "files_removed": deleted_files}

    async def backfill_thumbnails(self) -> dict[str, int]:
        """Generate thumbnails for existing clips that don't have one."""
        clips = self._db.recent_motion_clips(limit=500)
        generated = 0
        skipped = 0
        for clip in clips:
            if clip.get("thumb_relpath"):
                skipped += 1
                continue
            video_relpath = clip.get("video_relpath", "")
            if not video_relpath:
                skipped += 1
                continue
            video_path = self._clips_dir / video_relpath
            if not video_path.exists():
                skipped += 1
                continue
            thumb_path = video_path.with_suffix(".thumb.jpg")
            if thumb_path.exists():
                # Thumb file exists but DB not updated — fix the DB
                thumb_relpath = str(Path(video_relpath).with_suffix(".thumb.jpg"))
                try:
                    with self._db._conn() as conn:
                        conn.execute(
                            "UPDATE motion_clips SET thumb_relpath = ? WHERE id = ?",
                            (thumb_relpath, clip["id"]),
                        )
                    generated += 1
                except Exception:
                    pass
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    "/usr/bin/ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-i", str(video_path), "-vframes", "1", "-q:v", "3",
                    "-vf", "scale=320:-1", str(thumb_path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=10)
                if proc.returncode == 0 and thumb_path.exists():
                    thumb_relpath = str(Path(video_relpath).with_suffix(".thumb.jpg"))
                    with self._db._conn() as conn:
                        conn.execute(
                            "UPDATE motion_clips SET thumb_relpath = ? WHERE id = ?",
                            (thumb_relpath, clip["id"]),
                        )
                    generated += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
        _LOGGER.info("motion_clip.backfill_complete", generated=generated, skipped=skipped)
        return {"generated": generated, "skipped": skipped}
