"""Mixin for ProactiveService: camera helpers, motion event handling, and phone notifications."""
from __future__ import annotations
import asyncio
import time

import structlog

_LOGGER = structlog.get_logger()

_GLOBAL_MOTION_COOLDOWN_S = 600  # 10 minutes


class ProactiveMotionMixin:
    """Camera label/room helpers, Coral/YOLO motion pipeline, clip archiving — mixed into ProactiveService."""
    def _cam_label(self, camera_id: str) -> str:
        """Return friendly camera label, falling back to entity ID."""
        return self._camera_labels.get(camera_id, camera_id.replace("camera.", "").replace("_", " ").title())


    def _cam_room(self, camera_id: str) -> str | None:
        """Return a room_id slug for this camera, used to route tablet announcements.
        Uses camera_room_map from home_runtime.json if configured,
        otherwise derives from camera label (e.g. "Living Room Camera" -> "living_room").
        """
        room_map = getattr(self, "_camera_room_map", {})
        if camera_id in room_map:
            return room_map[camera_id]
        label = self._cam_label(camera_id)
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        return slug or None

    def _motion_vision_llm_fields(self) -> dict[str, str]:
        """Return LLM fields reflecting the actual configured motion vision provider."""
        from avatar_backend.config import get_settings
        mvp = (get_settings().motion_vision_provider or "gemini").strip().lower()
        if mvp == "ollama":
            provider = "ollama"
            model = getattr(self._llm, "_backend", None)
            model = getattr(model, "_vision_model", None) if model else None
            if not model:
                model = get_settings().ollama_vision_model or "unknown"
            return {"llm_provider": provider, "llm_model": model, "llm_tag": f"{provider}:{model}"}
        if mvp == "ollama_remote":
            s = get_settings()
            model = s.ollama_vision_model or "moondream:1.8b"
            return {"llm_provider": "ollama_remote", "llm_model": model, "llm_tag": f"ollama_remote:{model}"}
        return self._gemini_llm_fields()


    async def _handle_motion_event(self, entity_id: str, friendly: str, camera_id: str) -> None:
        """Fetch a camera snapshot, describe it with vision, archive clip, and optionally announce."""
        bypass_global = camera_id in self._bypass_global_motion_cameras

        # Determine whether we should announce (voice) or just silently archive.
        # Clips are ALWAYS archived when Coral confirms a detection — only the
        # voice announcement is rate-limited by the global and per-camera cooldowns.
        _should_announce = True
        if not bypass_global:
            since_last = time.monotonic() - self._last_motion_announce_time
            if since_last < _GLOBAL_MOTION_COOLDOWN_S:
                _should_announce = False
                _LOGGER.debug("proactive.motion_announce_suppressed",
                              reason="global_cooldown",
                              seconds_remaining=int(_GLOBAL_MOTION_COOLDOWN_S - since_last))

        _LOGGER.info("proactive.motion_triggered", entity_id=entity_id, camera=self._cam_label(camera_id),
                     bypass_global=bypass_global, will_announce=_should_announce)
        if self._decision_log:
            self._decision_log.record(
                "motion_triggered",
                entity=entity_id,
                camera=self._cam_label(camera_id),
                **self._motion_vision_llm_fields(),
            )

        # ── Coral Edge TPU pre-filter ─────────────────────────────────────────
        # Fetch one frame and run fast on-device object detection.
        # If nothing of interest is found (no person/vehicle), drop the event —
        # we don't archive clips for background motion (wind, lighting, animals).
        # Only clips confirmed by Coral (person / plate-bearing vehicle) proceed
        # to the Ollama vision call and are saved to Find Anything.
        _coral_detections: list[str] = []
        _coral_has_plate: bool = False
        _coral_frame: bytes | None = None
        if self._coral.enabled:
            try:
                frame = await self._ha.fetch_camera_image(camera_id)
                if frame:
                    coral_result = await self._coral.check(frame, camera_id=camera_id)
                    if coral_result.skip:
                        if self._decision_log:
                            self._decision_log.record(
                                "motion_coral_filtered",
                                camera=self._cam_label(camera_id),
                                inference_ms=round(coral_result.inference_ms, 1),
                                reason=coral_result.reason,
                                **self._motion_vision_llm_fields(),
                            )
                        _LOGGER.info(
                            "coral.filtered_no_archive",
                            camera=self._cam_label(camera_id),
                            inference_ms=round(coral_result.inference_ms, 1),
                            detail="no person or vehicle — clip not archived",
                        )
                        return
                    _coral_detections = coral_result.detections
                    _coral_has_plate = coral_result.has_plate_bearing
                    _coral_frame = frame
                    # YOLOv5 verification — get proper labels from CodeProject.AI
                    _face_svc = getattr(self._camera_event_service, '_face_service', None)
                    if _face_svc and _face_svc.available and frame:
                        yolo_results = await _face_svc.detect_objects(frame)
                        if yolo_results:
                            _coral_detections = [f"{d['label']}({d['confidence']:.0%})" for d in yolo_results]
                            _coral_has_plate = any(d['label'] in ('car', 'truck', 'bus') for d in yolo_results)
                            _LOGGER.info("yolo.verified", camera=self._cam_label(camera_id), detections=_coral_detections)
                    if self._decision_log:
                        self._decision_log.record(
                            "coral_detection",
                            camera=self._cam_label(camera_id),
                            detections=_coral_detections,
                            has_plate_bearing=_coral_has_plate,
                            inference_ms=round(coral_result.inference_ms, 1),
                        )
                    _LOGGER.info(
                        "coral.passed_to_vision",
                        camera=self._cam_label(camera_id),
                        detections=_coral_detections,
                        has_plate_bearing=_coral_has_plate,
                        inference_ms=round(coral_result.inference_ms, 1),
                    )
            except Exception as exc:
                _LOGGER.warning("coral.check_failed", camera=self._cam_label(camera_id), exc=str(exc),
                                detail="falling through to Ollama vision")
        # ─────────────────────────────────────────────────────────────────────
        # ── CPAI fallback when Coral is disabled ──────────────────────────────
        if not self._coral.enabled and not _coral_detections:
            try:
                _face_svc = getattr(self._camera_event_service, "_face_service", None)
                if _face_svc and _face_svc.available:
                    frame = await self._ha.fetch_camera_image(camera_id)
                    if frame:
                        yolo_results = await _face_svc.detect_objects(frame)
                        if yolo_results:
                            _coral_detections = [f'{d["label"]}({d["confidence"]:.0%})' for d in yolo_results]
                            _coral_has_plate = any(d["label"] in ("car", "truck", "bus") for d in yolo_results)
                            _coral_frame = frame
                            _LOGGER.info("cpai.fallback_detect", camera=self._cam_label(camera_id), detections=_coral_detections)
            except Exception as exc:
                _LOGGER.warning("cpai.fallback_failed", camera=self._cam_label(camera_id), exc=str(exc)[:80])

        # Start clip capture IMMEDIATELY so the video captures the actual motion
        # event. Vision description runs in parallel — the clip gets a placeholder
        # description that's updated once Gemini finishes.
        clip_camera = self._clip_camera_map.get(entity_id, camera_id)
        clip_handle = self._motion_clip_service.schedule_capture(
            camera_entity_id=clip_camera,
            trigger_entity_id=entity_id,
            location=friendly,
            description=f"Motion detected by {friendly}.",
            extra={
                "coral_detections": _coral_detections,
                "coral_has_plate": _coral_has_plate,
            },
        )

        # Use the same frame Coral already fetched for vision analysis.
        # No delay needed — the frame was captured at the moment of motion detection.

        try:
            # Skip vision if camera is not in the enabled list
            # Exception: doorbell ring events (visitor) always get vision
            is_doorbell_ring = "visitor" in entity_id.lower()
            skip_vision = (
                self._vision_enabled_cameras
                and camera_id not in self._vision_enabled_cameras
                and not is_doorbell_ring
            )
            if skip_vision:
                # Use Coral detection labels as the description for archiving
                coral_desc = ", ".join(_coral_detections) if _coral_detections else "Motion detected"
                _LOGGER.info("proactive.vision_skipped", camera=self._cam_label(camera_id), reason="not in vision_enabled_cameras")
                result = {
                    "message": f"{coral_desc} on {friendly}.",
                    "description": f"{coral_desc} on {friendly}.",
                    "archive_description": f"{coral_desc} on {friendly}.",
                    "suppressed": False,
                    "is_delivery": False,
                    "delivery_company": "",
                    "plate_number": "",
                    "raw_description": "",
                    "canonical_event": None,
                    "delivery": False,
                }
            else:
                result = await self._camera_event_service.analyze_motion(
                    camera_entity_id=camera_id,
                    location=friendly,
                    trigger_entity_id=entity_id,
                    source="proactive_motion",
                    system_prompt=self._system_prompt or None,
                    vision_prompt=self._camera_vision_prompts.get(camera_id),
                    include_plate_ocr=_coral_has_plate,
                    prefetched_frame=_coral_frame,
                )
        except Exception as exc:
            _LOGGER.warning("proactive.motion_describe_failed", camera=self._cam_label(camera_id), exc=str(exc))
            result = {
                "message": f"Motion detected by {friendly}.",
                "description": "",
                "archive_description": f"Motion detected by {friendly}.",
                "suppressed": False,
                "is_delivery": False,
                "delivery_company": "",
                "plate_number": "",
                "raw_description": "",
                "canonical_event": None,
            }

        is_delivery = bool(result["is_delivery"])
        delivery_company = str(result["delivery_company"] or "")
        plate_number = str(result.get("plate_number") or "")
        message = str(result["message"] or f"Motion detected by {friendly}.")
        description = str(result["archive_description"] or result["description"] or message)

        if result["suppressed"]:
            # Gemini confirmed nothing worth alerting — cancel the in-flight clip.
            if clip_handle:
                self._motion_clip_service.cancel_pending(clip_handle)
            _LOGGER.info(
                "proactive.motion_suppressed_no_archive",
                camera=self._cam_label(camera_id),
                reason="gemini_no_motion",
                coral_detections=_coral_detections,
            )
            if self._decision_log:
                self._decision_log.record(
                    "motion_suppressed",
                    camera=self._cam_label(camera_id),
                    reason="NO_MOTION",
                    coral_detections=_coral_detections,
                    **self._motion_vision_llm_fields(),
                )
            return
        elif result["raw_description"]:
            _LOGGER.info("proactive.motion_described", camera=self._cam_label(camera_id),
                         chars=len(result["raw_description"]), delivery=is_delivery)
            if plate_number:
                _LOGGER.info("proactive.plate_read", camera=self._cam_label(camera_id), plate=plate_number)
            if is_delivery:
                _LOGGER.info("proactive.delivery_detected", camera=self._cam_label(camera_id),
                             company=delivery_company)
                if self._decision_log:
                    self._decision_log.record(
                        "delivery_detected",
                        camera=self._cam_label(camera_id),
                        company=delivery_company,
                        scene=description[:200],
                        **self._motion_vision_llm_fields(),
                    )

        extra = {
            "delivery": is_delivery,
            "delivery_company": delivery_company,
            "coral_detections": _coral_detections,
            "coral_has_plate": _coral_has_plate,
            "plate_number": plate_number,
        }
        if result.get("canonical_event") is not None:
            extra["canonical_event"] = result["canonical_event"]

        # Update the already-recording clip with the real description from Gemini
        if clip_handle is not None:
            self._motion_clip_service.update_pending_description(
                clip_handle, description=description, extra=extra,
            )
        else:
            # Fallback: schedule a new capture if the early one failed
            self._motion_clip_service.schedule_capture(
                camera_entity_id=clip_camera,
                trigger_entity_id=entity_id,
                location=friendly,
                description=description,
                extra=extra,
            )

        if self._decision_log:
            self._decision_log.record(
                "motion_clip_archived",
                camera=self._cam_label(camera_id),
                message=description[:300],
                delivery=is_delivery,
                announced=_should_announce,
                **self._motion_vision_llm_fields(),
            )

        # Only update the announce timestamp and notify if not cooldown-suppressed
        _motion_room_id = self._cam_room(camera_id)
        if _should_announce:
            self._last_motion_announce_time = time.monotonic()

        # For deliveries, always push to phones regardless of announce cooldown
        if is_delivery:
            title = f"Delivery – {delivery_company}" if delivery_company else "Delivery at driveway"
            await self._notify_phones(title, message)

    async def _notify_phones(self, title: str, message: str) -> None:
        """Push a notification to both registered phones via HA."""
        import httpx as _httpx
        headers = {
            "Authorization": f"Bearer {self._ha_token}",
            "Content-Type": "application/json",
        }
        for svc in self._phone_notify_services:
            url = f"{self._ha_url}/api/services/{svc}"
            try:
                async with _httpx.AsyncClient(timeout=10.0) as client:  # L3: removed verify=False
                    resp = await client.post(url, headers=headers,
                                             json={"title": title, "message": message})
                    resp.raise_for_status()
                _LOGGER.info("proactive.phone_notified", service=svc)
            except Exception as exc:
                _LOGGER.warning("proactive.phone_notify_failed", service=svc, exc=str(exc))

    # ── Weather monitoring ────────────────────────────────────────────────
