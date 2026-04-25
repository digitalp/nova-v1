"""ClipCaptureMixin: low-level clip recording helpers for MotionClipService."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import structlog

from avatar_backend.services._shared_http import _http_client
from avatar_backend.runtime_paths import data_dir

_LOGGER = structlog.get_logger()


class ClipCaptureMixin:
    """Low-level clip capture methods — mixed into MotionClipService."""
    _POLLING_ONLY_CAMERAS: set[str] = set()  # Loaded from home_runtime.json

    async def _capture_clip(self, camera_entity_id: str, output_path: Path) -> bool:
        # Use polling for all cameras — MJPEG proxy consistently times out
        # on HTTPS with self-signed certificates. Polling via httpx with
        # verify=False works reliably at 4-5 FPS.
        return await self._capture_clip_polling(camera_entity_id, output_path)
        # Stream directly from HA's MJPEG proxy endpoint.
        # Use local HTTP URL for camera streams — avoids TLS overhead and timeouts.
        token = self._ha.auth_headers.get("Authorization", "").removeprefix("Bearer ").strip()
        stream_url = f"{self._local_ha_url}/api/camera_proxy_stream/{camera_entity_id}"

        cmd = [
            "/usr/bin/ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel", "error",
            "-tls_verify", "0",
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
        url = f"{self._local_ha_url}/api/camera_proxy/{camera_entity_id}"
        headers = self._ha.auth_headers

        # Prefer Blue Iris direct URL — plain HTTP, no TLS overhead, faster polling
        bi = getattr(self._ha, '_blueiris_service', None)
        bi_url = bi.mjpeg_url(camera_entity_id) if bi and bi.available else None
        if bi_url:
            # Use snapshot endpoint for polling (mjpeg is for streaming)
            bi_name = bi.resolve_camera(camera_entity_id)
            if bi_name:
                url = f"{bi._bi_url}/image/{bi_name}?q=60"
                headers = {}  # Blue Iris doesn't need auth for images

        # Sequential polling with keep-alive — one request at a time to avoid
        # corrupted frames from overloading the HA camera proxy. Keep-alive
        # reuses the TCP connection so each request is faster than cold starts.
        write_index = 0
        try:
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=5.0),
                limits=_httpx.Limits(max_keepalive_connections=1, max_connections=1),
                verify=False,
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
        # Frame duplication is the correct approach for surveillance footage —
        # minterpolate creates morphing artefacts at the low fps typical of HA proxy polling.
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
