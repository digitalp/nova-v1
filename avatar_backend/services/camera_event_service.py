from __future__ import annotations

import time
import uuid
from typing import Any

from avatar_backend.services.event_service import EventService
from avatar_backend.services.llm_service import _DOORBELL_IMAGE_PROMPT, _MOTION_IMAGE_PROMPT


class CameraEventService:
    """Shared camera-event analysis for doorbell, motion, delivery, and driveway flows."""

    def __init__(self, *, ha_proxy, llm_service, event_service: EventService | None = None) -> None:
        self._ha = ha_proxy
        self._llm = llm_service
        self._event_service = event_service

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

    async def analyze_motion(
        self,
        *,
        camera_entity_id: str,
        location: str,
        trigger_entity_id: str = "",
        source: str,
        system_prompt: str | None = None,
        vision_prompt: str | None = None,
    ) -> dict[str, Any]:
        camera_id = self.resolve_camera_entity(camera_entity_id)
        image_bytes = await self._ha.fetch_camera_image(camera_id)

        is_delivery = False
        delivery_company = ""
        raw_description = ""
        description = ""
        message = f"Motion detected {location}."
        suppressed = False

        if image_bytes:
            raw_description = await self._llm.describe_image_with_gemini(
                image_bytes,
                prompt=vision_prompt or _MOTION_IMAGE_PROMPT,
                system_instruction=system_prompt or None,
            )
            if raw_description.strip().startswith("NO_MOTION"):
                suppressed = True
                description = "No meaningful motion visible."
            else:
                scene_lines: list[str] = []
                delivery_line = ""
                for line in raw_description.splitlines():
                    if line.strip().upper().startswith("DELIVERY:"):
                        delivery_line = line.strip()
                    else:
                        scene_lines.append(line)
                scene = " ".join(scene_lines).strip()
                description = scene or raw_description.strip()
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
            "canonical_event": canonical_event,
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
