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
    ) -> None:
        self._db = db
        self._ha = ha_proxy
        self._llm = llm_service
        self._issue_autofix_service = issue_autofix_service
        self._clip_duration_s = max(3, int(clip_duration_s))
        self._max_search_candidates = max(20, int(max_search_candidates))
        self._max_search_results = max(5, int(max_search_results))
        self._clips_dir = data_dir() / "motion_clips"
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._clips_dir_ready = self._ensure_clips_dir_ready()
        self._tasks: set[asyncio.Task] = set()

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
    ) -> None:
        task = asyncio.create_task(
            self.capture_and_store(
                camera_entity_id=camera_entity_id,
                trigger_entity_id=trigger_entity_id,
                location=location,
                description=description,
                extra=extra or {},
            ),
            name=f"motion_clip:{camera_entity_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def capture_and_store(
        self,
        *,
        camera_entity_id: str,
        trigger_entity_id: str = "",
        location: str = "",
        description: str = "",
        extra: dict[str, Any] | None = None,
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

        status = "ready"
        if not await self._capture_clip(camera_entity_id, fullpath):
            status = "capture_failed"

        clip_id = self._db.insert_motion_clip({
            "ts": now.isoformat(),
            "camera_entity_id": camera_entity_id,
            "trigger_entity_id": trigger_entity_id,
            "location": location,
            "description": description,
            "video_relpath": str(relpath) if status == "ready" else "",
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
        # Stream directly from HA's MJPEG proxy endpoint using a single persistent
        # connection.  This delivers frames at the camera's native rate (~5–15 fps)
        # versus the ~1 fps we get when polling /api/camera_proxy one request at a time.
        token = self._ha.auth_headers.get("Authorization", "").removeprefix("Bearer ").strip()
        stream_url = f"{self._ha.ha_url}/api/camera_proxy_stream/{camera_entity_id}"

        cmd = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel", "error",
            # Pass the HA Bearer token as an HTTP header for the MJPEG input
            "-headers", f"Authorization: Bearer {token}\r\n",
            # Read from the live MJPEG stream
            "-i", stream_url,
            # Capture exactly clip_duration_s seconds of wall-clock time
            "-t", str(self._clip_duration_s),
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            # Output at smooth 25 fps — ffmpeg duplicates/drops frames as needed
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
                    timeout=self._clip_duration_s + 20,
                )
            except asyncio.TimeoutError:
                proc.kill()
                _, stderr = await proc.communicate()
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass
                _LOGGER.warning("motion_clip.capture_timeout", camera=camera_entity_id)
                return False
        except Exception as exc:
            _LOGGER.warning("motion_clip.capture_spawn_failed", camera=camera_entity_id, exc=_format_exc(exc))
            return False

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
        """Fallback: poll /api/camera_proxy one frame at a time.  Used when the MJPEG
        stream endpoint is unavailable.  Computes the actual capture fps from elapsed
        wall-clock time so the video plays back at real-time speed."""
        target_fps = 5
        frame_interval_s = 1.0 / target_fps
        frame_dir = Path(tempfile.mkdtemp(prefix="motion_frames_", dir=str(self._clips_dir)))
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self._clip_duration_s
        write_index = 0
        fetch_index = 0
        try:
            while loop.time() < deadline:
                image_bytes = await self._ha.fetch_camera_image(camera_entity_id)
                if image_bytes:
                    (frame_dir / f"frame_{write_index:04d}.jpg").write_bytes(image_bytes)
                    write_index += 1
                fetch_index += 1
                next_tick = started + (fetch_index * frame_interval_s)
                sleep_for = next_tick - loop.time()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
        except Exception as exc:
            _LOGGER.warning("motion_clip.poll_sample_failed", camera=camera_entity_id, exc=_format_exc(exc))
            shutil.rmtree(frame_dir, ignore_errors=True)
            return False

        captured = write_index
        elapsed = max(0.1, loop.time() - started)

        if captured < 3:
            shutil.rmtree(frame_dir, ignore_errors=True)
            _LOGGER.warning("motion_clip.poll_insufficient_frames", camera=camera_entity_id, frames=captured)
            return False

        actual_fps = captured / elapsed
        _LOGGER.info(
            "motion_clip.poll_complete",
            camera=camera_entity_id,
            frames=captured,
            elapsed_s=round(elapsed, 2),
            actual_fps=round(actual_fps, 2),
        )
        cmd = [
            "/usr/bin/ffmpeg",
            "-nostdin", "-y", "-loglevel", "error",
            "-framerate", f"{actual_fps:.4f}",
            "-i", str(frame_dir / "frame_%04d.jpg"),
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-r", "25",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._clip_duration_s + 12)
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
