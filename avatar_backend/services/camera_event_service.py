from __future__ import annotations
import traceback

import time
import uuid
from typing import Any

from avatar_backend.services.event_service import EventService
from avatar_backend.services.llm_service import _DOORBELL_IMAGE_PROMPT, _MOTION_IMAGE_PROMPT

_STATIC_KEYWORDS = ("parked", "stationary", "no activity", "no movement", "empty driveway",
                     "no one", "nothing unusual", "no person", "no people", "quiet", "still image")

def _is_static_scene(description: str) -> bool:
    """Return True if the vision description indicates a static/parked scene with no motion."""
    lower = description.lower()
    if any(k in lower for k in _STATIC_KEYWORDS):
        # Only suppress if there's no person/delivery/activity mentioned
        if not any(w in lower for w in ("person", "walking", "approaching", "delivery", "carrying", "running")):
            return True
    return False



def _is_corrupted_frame(image_bytes: bytes) -> bool:
    """Detect corrupted camera frames (vertical bars, grey blanks, smeared pixels)."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        if w < 10 or h < 10:
            return True
        pixels = []
        for y in range(0, h, max(1, h // 8)):
            for x in range(0, w, max(1, w // 8)):
                pixels.append(img.getpixel((min(x, w-1), min(y, h-1))))
        r_vals = [p[0] for p in pixels]
        g_vals = [p[1] for p in pixels]
        b_vals = [p[2] for p in pixels]
        if max(r_vals)-min(r_vals) < 30 and max(g_vals)-min(g_vals) < 30 and max(b_vals)-min(b_vals) < 30:
            return True
        col_samples = [img.getpixel((x, h//2)) for x in range(0, w, max(1, w//20))]
        row_samples = [img.getpixel((w//2, y)) for y in range(0, h, max(1, h//20))]
        col_var = max(p[0] for p in col_samples) - min(p[0] for p in col_samples)
        row_var = max(p[0] for p in row_samples) - min(p[0] for p in row_samples)
        if col_var > 50 and row_var < 10:
            return True
        return False
    except Exception:
        return False

class CameraEventService:
    """Shared camera-event analysis for doorbell, motion, delivery, and driveway flows."""

    def __init__(self, *, ha_proxy, llm_service, event_service: EventService | None = None) -> None:
        self._ha = ha_proxy
        self._llm = llm_service
        self._event_service = event_service
        self._face_service = None  # Set externally if CodeProject.AI is configured

    def resolve_camera_entity(self, entity_id: str) -> str:
        return self._ha.resolve_camera_entity(entity_id)

    async def describe_doorbell(self, camera_entity_id: str) -> dict[str, Any]:
        camera_id = self.resolve_camera_entity(camera_entity_id)
        image_bytes = await self._ha.fetch_camera_image(camera_id)
        description = ""
        suppressed = False
        message = "Someone is at the door."

        if image_bytes:
            description = await self._llm.describe_image(image_bytes, prompt=_DOORBELL_IMAGE_PROMPT)
            if description.strip().startswith("NO_PERSON"):
                suppressed = True
                message = "no_person_visible"
            else:
                message = f"Someone is at the door. {description}"

        return {
            "camera_entity_id": camera_id,
            "image_available": bool(image_bytes),
            "description": description,
            "message": message,
            "suppressed": suppressed,
        }

    # Appended to the vision prompt when Coral detects a plate-bearing vehicle.
    _PLATE_HINT = (
        "\nIf you can see a vehicle with a clearly readable number plate (registration), "
        "append a new line with EXACTLY:\n"
        "PLATE: <registration>\n"
        "Use the exact characters you can read. Only include the PLATE line if you are "
        "confident the registration is legible — do not guess."
    )

    async def analyze_motion(
        self,
        *,
        camera_entity_id: str,
        location: str,
        trigger_entity_id: str = "",
        source: str,
        system_prompt: str | None = None,
        vision_prompt: str | None = None,
        include_plate_ocr: bool = False,
        prefetched_frame: bytes | None = None,
    ) -> dict[str, Any]:
        camera_id = self.resolve_camera_entity(camera_entity_id)
        image_bytes = prefetched_frame or await self._ha.fetch_camera_image(camera_id)

        is_delivery = False
        delivery_company = ""
        plate_number = ""
        raw_description = ""
        description = ""
        message = f"Motion detected {location}."
        suppressed = False

        if image_bytes and _is_corrupted_frame(image_bytes):
            suppressed = True
            description = "Corrupted camera frame — skipped vision analysis."
            image_bytes = None

        if image_bytes:
            prompt = vision_prompt or _MOTION_IMAGE_PROMPT
            if include_plate_ocr:
                prompt = prompt + self._PLATE_HINT

            # Use configured vision provider — "ollama" for free local inference,
            # "gemini" (default) for cloud vision.
            from avatar_backend.config import get_settings
            _vision_provider = (get_settings().motion_vision_provider or "gemini").strip().lower()
            if _vision_provider == "ollama_remote":
                from avatar_backend.services.llm_service import _ollama_describe_image, _vision_ollama_url
                _s = get_settings()
                raw_description = await _ollama_describe_image(
                    image_bytes, _vision_ollama_url(), _s.ollama_vision_model, prompt,
                )
            elif _vision_provider == "ollama":
                raw_description = await self._llm.describe_image(
                    image_bytes,
                    prompt=prompt,
                    system_instruction=system_prompt or None,
                )
            else:
                raw_description = await self._llm.describe_image_with_gemini(
                    image_bytes,
                    prompt=prompt,
                    system_instruction=system_prompt or None,
                )
            if raw_description.strip().startswith("NO_MOTION") or raw_description.strip().startswith("NO_PERSON"):
                suppressed = True
                description = "No meaningful motion visible."
            elif _is_static_scene(raw_description):
                suppressed = True
                description = "Static scene — no moving objects."
            else:
                scene_lines: list[str] = []
                delivery_line = ""
                plate_line = ""
                for line in raw_description.splitlines():
                    stripped = line.strip()
                    if stripped.upper().startswith("DELIVERY:"):
                        delivery_line = stripped
                    elif stripped.upper().startswith("PLATE:"):
                        plate_line = stripped
                    else:
                        scene_lines.append(line)
                scene = " ".join(scene_lines).strip()
                description = scene or raw_description.strip()
                if plate_line:
                    plate_number = plate_line.split(":", 1)[1].strip().upper()
                if delivery_line:
                    is_delivery = True
                    delivery_company = delivery_line.split(":", 1)[1].strip()
                    company_label = delivery_company if delivery_company.lower() != "unknown" else "a courier"
                    message = f"Delivery alert! There's {company_label} delivery at the driveway. {scene}"
                else:
                    message = f"Motion detected {location}. {description}"

        if not description:
            description = message

        canonical_event: dict[str, Any] | None = None
        archive_description = description
        if self._event_service is not None:
            event = self._event_service.build_event(
                event_id=f"{source}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                event_type="delivery_detected" if is_delivery else "motion_detected",
                title=f"Delivery at {location}" if is_delivery else f"Motion at {location}",
                message=description,
                camera_entity_id=camera_id,
                event_context={
                    "trigger_entity_id": trigger_entity_id,
                    "location": location,
                    "delivery": is_delivery,
                    "delivery_company": delivery_company,
                    "source": source,
                },
            )
            canonical_event = self._event_service.to_dict(event)
            archive_description = event.message or description

        # Face recognition — identify known people in the frame
        recognized_faces = []
        if image_bytes and not suppressed and self._face_service and self._face_service.available:
            try:
                # Use Blue Iris for higher quality face crop if available
                bi = getattr(self._ha, '_blueiris_service', None)
                face_frame = None
                if bi and bi.available:
                    face_frame = await bi.fetch_snapshot(camera_id)
                recognized_faces = await self._face_service.recognize(face_frame or image_bytes)
                if recognized_faces:
                    names = ", ".join(f["name"].title() for f in recognized_faces)
                    description = description.replace("A person", names).replace("a person", names)
                    description = description.replace("Someone", names).replace("someone", names)
                    if names.lower() not in description.lower():
                        description = f"{names} detected. {description}"
                    archive_description = description
                    message = description

                # ALPR — read license plate if vehicle detected and no plate from LLM
                if not plate_number and include_plate_ocr:
                    alpr_plate = await self._face_service.read_plate(face_frame or image_bytes)
                    if alpr_plate:
                        plate_number = alpr_plate
            except Exception as exc:
                import structlog
                structlog.get_logger().warning("face.recognition_failed", exc=str(exc)[:100])

        return {
            "camera_entity_id": camera_id,
            "image_available": bool(image_bytes),
            "raw_description": raw_description,
            "description": description,
            "archive_description": archive_description,
            "message": message,
            "suppressed": suppressed,
            "is_delivery": is_delivery,
            "delivery_company": delivery_company,
            "plate_number": plate_number,
            "canonical_event": canonical_event,
            "recognized_faces": recognized_faces,
        }

    def build_package_event(
        self,
        *,
        camera_entity_id: str,
        source: str,
        trigger_entity_id: str = "",
        location: str = "front door",
        title: str = "Package Delivery",
        message: str = "A package was delivered.",
    ) -> dict[str, Any]:
        camera_id = self.resolve_camera_entity(camera_entity_id)
        canonical_event: dict[str, Any] | None = None
        if self._event_service is not None:
            event = self._event_service.build_event(
                event_id=f"{source}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                event_type="package_delivery",
                title=title,
                message=message,
                camera_entity_id=camera_id,
                event_context={
                    "trigger_entity_id": trigger_entity_id,
                    "location": location,
                    "source": source,
                },
            )
            canonical_event = self._event_service.to_dict(event)
        return {
            "camera_entity_id": camera_id,
            "title": title,
            "message": message,
            "canonical_event": canonical_event,
        }
