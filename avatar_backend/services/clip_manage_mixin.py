"""ClipManageMixin: search, cleanup, and maintenance for MotionClipService."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

import structlog

from avatar_backend.services._shared_http import _http_client
from avatar_backend.runtime_paths import data_dir
from avatar_backend.services.perceptual_hash import compute_phash, hamming_distance

_LOGGER = structlog.get_logger()


def _format_exc(exc: BaseException) -> str:
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


class ClipManageMixin:
    """Clip search, ranking, cleanup, and backfill — mixed into MotionClipService."""
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

    # Cameras whose MJPEG proxy stream always times out — skip straight to polling.
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
