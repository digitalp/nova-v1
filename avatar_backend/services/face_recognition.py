"""FaceRecognitionService — face ID, object detection, and ALPR via CodeProject.AI."""
from __future__ import annotations
import traceback

import base64
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from avatar_backend.services._shared_http import _http_client
import structlog

_LOGGER = structlog.get_logger()
_MAX_UNKNOWN = 50


def _get_face_db_path() -> Path:
    from avatar_backend.runtime_paths import data_dir
    return data_dir() / "unknown_faces.db"


def _init_face_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS unknown_faces (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        crop_b64 TEXT NOT NULL,
        crop_bytes BLOB NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.0
    )""")
    conn.commit()
    return conn


class FaceRecognitionService:
    def __init__(self, cpai_url: str = "") -> None:
        self._url = cpai_url.rstrip("/")
        self._db: sqlite3.Connection | None = None
        self._deepface_svc = None  # injected at startup if deepface_enabled

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = _init_face_db(_get_face_db_path())
        return self._db

    @property
    def available(self) -> bool:
        return bool(self._url)

    # ── Face Recognition ──────────────────────────────────────────────────

    # Minimum confidence sent to the API — low enough that weak matches come back
    # with the actual name rather than "unknown". Application-level thresholds
    # below determine whether the match is announced or silently accepted.
    _API_MIN_CONF = 0.3
    # Minimum confidence to include a face in the returned list (used by callers
    # to decide whether to announce / act on the recognition).
    _ANNOUNCE_MIN_CONF = 0.55

    async def recognize(
        self,
        image_bytes: bytes,
        min_confidence: float = _ANNOUNCE_MIN_CONF,
        queue_full_frame_on_empty: bool = False,
    ) -> list[dict]:
        if not self._url or not image_bytes:
            return []
        try:
            resp = await _http_client().post(
                    f"{self._url}/v1/vision/face/recognize",
                    files={"image": ("frame.jpg", image_bytes, "image/jpeg")},
                    data={"min_confidence": str(self._API_MIN_CONF)},
                    timeout=10.0,
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("success"):
                return []
            predictions = data.get("predictions", [])
            faces = []
            has_any = False
            for p in predictions:
                has_any = True
                name = p.get("userid", "unknown")
                conf = round(p.get("confidence", 0), 2)

                if name and name != "unknown":
                    # Known person — never queue as unknown regardless of confidence.
                    # Only include in results if confidence meets the announce threshold.
                    if conf >= min_confidence:
                        faces.append({"name": name, "confidence": conf})
                        _LOGGER.info("face.recognized", name=name, confidence=conf)
                    else:
                        # Weak match for a known person: log silently, skip announcement.
                        _LOGGER.info("face.recognized_low_conf", name=name, confidence=conf)
                elif p.get("x_min") is not None:
                    # Genuinely unknown face — run local DeepFace check before queueing.
                    if self._deepface_svc is not None:
                        matched = await self._deepface_svc.find_match(
                            image_bytes, self._face_photo_dir()
                        )
                        if matched:
                            _LOGGER.info(
                                "face.deepface_suppressed_unknown",
                                matched=matched, cpai_conf=conf,
                            )
                        else:
                            self._queue_unknown(image_bytes, p)
                    else:
                        self._queue_unknown(image_bytes, p)

            # Only queue an empty frame when a caller explicitly asks for that behavior.
            if not has_any and queue_full_frame_on_empty:
                self._queue_full_frame(image_bytes)
            return faces
        except Exception as exc:
            _LOGGER.warning("face.recognize_error", exc=str(exc)[:100], traceback=traceback.format_exc()[-600:])
            return []

    def _queue_unknown(self, image_bytes: bytes, prediction: dict) -> None:
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(image_bytes))
            x1, y1 = max(0, int(prediction.get("x_min", 0))), max(0, int(prediction.get("y_min", 0)))
            x2, y2 = min(img.width, int(prediction.get("x_max", img.width))), min(img.height, int(prediction.get("y_max", img.height)))
            if x2 - x1 < 30 or y2 - y1 < 30:
                return
            pad = int((x2 - x1) * 0.3)
            crop = img.crop((max(0, x1-pad), max(0, y1-pad), min(img.width, x2+pad), min(img.height, y2+pad)))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=85)
            crop_bytes = buf.getvalue()
            face_id = str(uuid.uuid4())[:8]
            db = self._get_db()
            db.execute("INSERT OR REPLACE INTO unknown_faces (id, ts, crop_b64, crop_bytes, confidence) VALUES (?, ?, ?, ?, ?)",
                       (face_id, time.time(), base64.b64encode(crop_bytes).decode(), crop_bytes, round(prediction.get("confidence", 0), 2)))
            db.commit()
            # Trim to max
            db.execute("DELETE FROM unknown_faces WHERE id NOT IN (SELECT id FROM unknown_faces ORDER BY ts DESC LIMIT ?)", (_MAX_UNKNOWN,))
            db.commit()
            _LOGGER.info("face.unknown_queued", face_id=face_id)
        except Exception as exc:
            _LOGGER.debug("face.crop_failed", exc=str(exc)[:80])

    def _queue_full_frame(self, image_bytes: bytes) -> None:
        try:
            face_id = str(uuid.uuid4())[:8]
            db = self._get_db()
            db.execute("INSERT OR REPLACE INTO unknown_faces (id, ts, crop_b64, crop_bytes, confidence) VALUES (?, ?, ?, ?, ?)",
                       (face_id, time.time(), base64.b64encode(image_bytes).decode(), image_bytes, 0.0))
            db.commit()
            db.execute("DELETE FROM unknown_faces WHERE id NOT IN (SELECT id FROM unknown_faces ORDER BY ts DESC LIMIT ?)", (_MAX_UNKNOWN,))
            db.commit()
            _LOGGER.info("face.full_frame_queued", face_id=face_id)
        except Exception as exc:
            _LOGGER.debug("face.full_frame_queue_failed", exc=str(exc)[:80])

    def _face_photo_path(self, name: str) -> "Path":
        from avatar_backend.runtime_paths import data_dir
        d = data_dir() / "face_photos"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{name.strip().lower()}.jpg"

    def _face_photo_dir(self) -> str:
        from avatar_backend.runtime_paths import data_dir
        return str(data_dir() / "face_photos")

    def save_face_photo(self, name: str, image_bytes: bytes) -> None:
        try:
            self._face_photo_path(name).write_bytes(image_bytes)
        except Exception as exc:
            _LOGGER.debug("face.photo_save_failed", name=name, exc=str(exc)[:80])

    def get_face_photo(self, name: str) -> bytes | None:
        p = self._face_photo_path(name)
        return p.read_bytes() if p.exists() else None

    async def register_face(self, name: str, image_bytes: bytes) -> bool:
        if not self._url or not image_bytes or not name:
            return False
        try:
            resp = await _http_client().post(
                    f"{self._url}/v1/vision/face/register",
                    files={"image": ("face.jpg", image_bytes, "image/jpeg")},
                    data={"userid": name.strip().lower()},
                    timeout=10.0,
                )
            ok = resp.json().get("success", False)
            _LOGGER.info("face.registered", name=name, success=ok)
            if ok:
                self.save_face_photo(name, image_bytes)
            return ok
        except Exception as exc:
            _LOGGER.warning("face.register_error", name=name, exc=str(exc)[:100])
            return False

    # ── ALPR ──────────────────────────────────────────────────────────────

    async def read_plate(self, image_bytes: bytes) -> str | None:
        if not self._url or not image_bytes:
            return None
        try:
            resp = await _http_client().post(
                    f"{self._url}/v1/image/alpr",
                    files={"image": ("frame.jpg", image_bytes, "image/jpeg")},
                    timeout=10.0,
                )
            if resp.status_code != 200:
                return None
            data = resp.json()
            for p in data.get("predictions", []):
                plate = (p.get("plate") or "").strip().upper()
                if plate and len(plate) >= 4:
                    _LOGGER.info("alpr.plate_read", plate=plate, confidence=round(p.get("confidence", 0), 2))
                    return plate
        except Exception as exc:
            _LOGGER.warning("alpr.error", exc=str(exc)[:100], traceback=traceback.format_exc()[-600:])
        return None

    # ── YOLOv5 Object Detection ───────────────────────────────────────────

    async def detect_objects(self, image_bytes: bytes, min_confidence: float = 0.4) -> list[dict]:
        if not self._url or not image_bytes:
            return []
        try:
            resp = await _http_client().post(
                    f"{self._url}/v1/vision/detection",
                    files={"image": ("frame.jpg", image_bytes, "image/jpeg")},
                    data={"min_confidence": str(min_confidence)},
                    timeout=10.0,
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("success"):
                return []
            return [{"label": p["label"], "confidence": round(p.get("confidence", 0), 2)} for p in data.get("predictions", [])]
        except Exception as exc:
            _LOGGER.warning("yolo.detect_error", exc=str(exc)[:100], traceback=traceback.format_exc()[-600:])
            return []

    # ── Admin helpers ─────────────────────────────────────────────────────

    async def list_known_faces(self) -> list[str]:
        if not self._url:
            return []
        try:
            resp = await _http_client().post(f"{self._url}/v1/vision/face/list", timeout=5.0)
            if resp.status_code == 200:
                return resp.json().get("faces", [])
        except Exception:
            pass
        return []

    async def delete_face(self, name: str) -> bool:
        if not self._url or not name:
            return False
        try:
            resp = await _http_client().post(
                    f"{self._url}/v1/vision/face/delete",
                    data={"userid": name.strip().lower()},
                    timeout=5.0,
                )
            ok = resp.json().get("success", False)
            _LOGGER.info("face.deleted", name=name, success=ok)
            return ok
        except Exception as exc:
            _LOGGER.warning("face.delete_error", name=name, exc=str(exc)[:100])
            return False

    def get_unknown_faces(self) -> list[dict]:
        try:
            db = self._get_db()
            rows = db.execute("SELECT id, ts, crop_b64, confidence FROM unknown_faces ORDER BY ts DESC").fetchall()
            return [{"id": r["id"], "ts": r["ts"], "crop_b64": r["crop_b64"], "confidence": r["confidence"]} for r in rows]
        except Exception:
            return []

    def get_unknown_face_bytes(self, face_id: str) -> bytes | None:
        try:
            db = self._get_db()
            row = db.execute("SELECT crop_bytes FROM unknown_faces WHERE id = ?", (face_id,)).fetchone()
            return bytes(row["crop_bytes"]) if row else None
        except Exception:
            return None

    def remove_unknown(self, face_id: str) -> None:
        try:
            db = self._get_db()
            db.execute("DELETE FROM unknown_faces WHERE id = ?", (face_id,))
            db.commit()
        except Exception:
            pass
