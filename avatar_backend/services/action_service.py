from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from avatar_backend.services.event_service import EventService, publish_visual_event
from avatar_backend.services.conversation_service import EventFollowupRequest
from avatar_backend.services.open_loop_service import OpenLoopService


class ActionService:
    """Compatibility-first action catalog and executor for V2 surfaces."""

    def __init__(self, *, open_loop_service: OpenLoopService | None = None) -> None:
        self._open_loop_service = open_loop_service or OpenLoopService()

    def build_suggested_actions(self, event_record: dict[str, Any], *, is_active: bool) -> list[dict[str, Any]]:
        status = str(event_record.get("status") or "active")
        has_event_id = bool(event_record.get("event_id"))
        actions: list[dict[str, Any]] = []
        if not has_event_id:
            return actions
        if is_active:
            actions.extend(self._followup_actions(event_record))
            if status not in {"acknowledged", "resolved"}:
                actions.append(self._action(
                    "acknowledge_active_event",
                    "Acknowledge",
                    tone="warn",
                    requires_confirmation=True,
                    confirm_text="Acknowledge this event?",
                ))
            if status != "snoozed":
                actions.append(self._action(
                    "snooze_active_event",
                    "Snooze 30m",
                    tone="quiet",
                    requires_confirmation=True,
                    confirm_text="Snooze this event for 30 minutes?",
                ))
            if status != "dismissed":
                actions.append(self._action(
                    "dismiss_active_event",
                    "Dismiss",
                    tone="quiet",
                    requires_confirmation=True,
                    confirm_text="Hide this event for now?",
                ))
            if status != "resolved":
                actions.append(self._action(
                    "resolve_active_event",
                    "Resolve",
                    tone="success",
                    requires_confirmation=True,
                    confirm_text="Mark this event as resolved?",
                ))
            return actions

        if status in {"dismissed", "resolved", "snoozed"}:
            actions.append(self._action(
                "activate_recent_event",
                "Unsnooze" if status == "snoozed" else "Reopen",
                tone="info",
                requires_confirmation=False,
            ))
        else:
            actions.extend(self._followup_actions(event_record))
            if status != "acknowledged":
                actions.append(self._action(
                    "acknowledge_recent_event",
                    "Acknowledge",
                    tone="warn",
                    requires_confirmation=True,
                    confirm_text="Acknowledge this event?",
                ))
            if status != "snoozed":
                actions.append(self._action(
                    "snooze_recent_event",
                    "Snooze 30m",
                    tone="quiet",
                    requires_confirmation=True,
                    confirm_text="Snooze this event for 30 minutes?",
                ))
            if status != "dismissed":
                actions.append(self._action(
                    "dismiss_recent_event",
                    "Dismiss",
                    tone="quiet",
                    requires_confirmation=True,
                    confirm_text="Hide this event for now?",
                ))
            if status != "resolved":
                actions.append(self._action(
                    "resolve_recent_event",
                    "Resolve",
                    tone="success",
                    requires_confirmation=True,
                    confirm_text="Mark this event as resolved?",
                ))
        return actions

    async def handle_surface_action(
        self,
        *,
        app,
        ws_mgr,
        action: str,
        event_id: str = "",
        action_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = dict(action_payload or {})
        surface_state = getattr(app.state, "surface_state_service", None)
        if surface_state is None:
            return self._ack(action, event_id=event_id, ok=False)

        active_actions = {
            "dismiss_active_event": surface_state.dismiss_active_event,
            "acknowledge_active_event": surface_state.acknowledge_active_event,
            "resolve_active_event": surface_state.resolve_active_event,
            "snooze_active_event": surface_state.snooze_active_event,
        }
        if action in active_actions:
            await active_actions[action](ws_mgr)
            return self._ack(action)

        recent_actions = {
            "dismiss_recent_event": surface_state.dismiss_recent_event,
            "acknowledge_recent_event": surface_state.acknowledge_recent_event,
            "resolve_recent_event": surface_state.resolve_recent_event,
            "snooze_recent_event": surface_state.snooze_recent_event,
            "activate_recent_event": surface_state.activate_recent_event,
        }
        if action in recent_actions:
            ok = await recent_actions[action](ws_mgr, event_id)
            return self._ack(action, event_id=event_id, ok=ok)

        if action == "show_related_camera":
            return await self._handle_related_camera_action(
                app=app,
                ws_mgr=ws_mgr,
                source_event_id=event_id,
                action_payload=payload,
            )

        return self._ack(action, event_id=event_id, ok=False)

    async def handle_event_history_action(
        self,
        *,
        app,
        ws_mgr,
        event_id: str,
        status: str,
        workflow_action: str | None = None,
        title: str = "",
        summary: str = "",
        event_type: str = "",
        event_source: str = "",
        camera_entity_id: str = "",
        open_loop_note: str | None = None,
        admin_note: str | None = None,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
    ) -> dict[str, Any]:
        db = getattr(app.state, "metrics_db", None)
        event_store = getattr(app.state, "event_store", None)
        surface_state = getattr(app.state, "surface_state_service", None)
        normalized_workflow = str(workflow_action or "").strip()
        reminder_requested = reminder_sent
        escalation_requested = escalation_level
        if normalized_workflow == "send_reminder":
            reminder_requested = True
        elif normalized_workflow.startswith("escalate_"):
            escalation_requested = normalized_workflow.split("_", 1)[1] or escalation_level

        history_persisted = False
        if db is not None and event_id:
            history_persisted = db.update_event_history_status(event_id, status, open_loop_note, admin_note)
            if not history_persisted:
                db.insert_event_history(
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "title": title,
                        "summary": summary,
                        "status": status,
                        "event_source": event_source,
                        "camera_entity_id": camera_entity_id,
                        "data": {
                            "open_loop_note": open_loop_note or "",
                            "admin_note": admin_note or "",
                            "admin_note_ts": datetime.now(timezone.utc).isoformat() if admin_note else "",
                        },
                    }
                )
                history_persisted = True
            if history_persisted and (reminder_requested or escalation_requested):
                db.update_event_history_policy(
                    event_id,
                    reminder_sent=reminder_requested,
                    escalation_level=escalation_requested,
                )

        canonical_persisted = False
        if event_store is not None and event_id:
            updated_event = event_store.update_status(
                event_id,
                status=status,
                open_loop_note=open_loop_note,
                admin_note=admin_note,
                reminder_sent=reminder_requested,
                escalation_level=escalation_requested,
            )
            if updated_event is None:
                created_event = event_store.create_event(
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "source": event_source,
                        "camera_entity_id": camera_entity_id,
                        "summary": summary or title,
                        "details": title if summary and title and summary != title else "",
                        "status": status,
                        "data": {
                            "open_loop_note": open_loop_note or "",
                            "admin_note": admin_note or "",
                            "admin_note_ts": datetime.now(timezone.utc).isoformat() if admin_note else "",
                        },
                    }
                )
                canonical_persisted = bool(created_event)
                updated_event = event_store.update_status(
                    event_id,
                    status=status,
                    open_loop_note=open_loop_note,
                    admin_note=admin_note,
                    reminder_sent=reminder_requested,
                    escalation_level=escalation_requested,
                )
                canonical_persisted = canonical_persisted or updated_event is not None
            else:
                canonical_persisted = True
            if canonical_persisted:
                event_store.record_action(
                    event_id=event_id,
                    action_id=uuid.uuid4().hex,
                    action_type=normalized_workflow or f"set_status:{status}",
                    status="completed",
                    result={
                        "status": status,
                        "open_loop_note": open_loop_note or "",
                        "admin_note": admin_note or "",
                        "reminder_sent": reminder_requested,
                        "escalation_level": escalation_requested,
                    },
                )

        surface_updated = False
        if surface_state is not None and ws_mgr is not None and event_id:
            if status == "acknowledged":
                surface_updated = await surface_state.acknowledge_recent_event(ws_mgr, event_id)
            elif status == "resolved":
                surface_updated = await surface_state.resolve_recent_event(ws_mgr, event_id)
            elif status == "active":
                surface_updated = await surface_state.activate_recent_event(ws_mgr, event_id)
            if reminder_requested or escalation_requested or open_loop_note:
                workflow_updated = await surface_state.apply_open_loop_workflow(
                    ws_mgr,
                    event_id,
                    open_loop_note=open_loop_note,
                    reminder_sent=reminder_requested,
                    escalation_level=escalation_requested,
                )
                surface_updated = bool(surface_updated or workflow_updated)

        return {
            "ok": bool(history_persisted or canonical_persisted or surface_updated),
            "event_id": event_id,
            "status": status,
            "workflow_action": normalized_workflow or None,
            "reminder_sent": reminder_requested,
            "escalation_level": escalation_requested,
            "persisted": bool(history_persisted or canonical_persisted),
            "surface_updated": surface_updated,
        }

    def build_event_history_actions(self, event_record: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = list(self._followup_actions(event_record))
        event_id = str(event_record.get("event_id") or "").strip()
        status = str(event_record.get("status") or "")
        if not event_id:
            return actions

        if status != "acknowledged":
            actions.append({"action": "acknowledge", "label": "Acknowledge"})
        if status != "resolved":
            actions.append({"action": "resolve", "label": "Resolve"})
        if status != "active":
            actions.append({"action": "reopen", "label": "Reopen"})

        actions.extend(
            self._open_loop_service.build_workflow_actions(
                event_record.get("data") or {},
                status=status,
                fallback_ts=str(event_record.get("ts") or ""),
            )
        )
        return actions

    async def handle_event_history_domain_action(
        self,
        *,
        app,
        ws_mgr,
        session_id: str,
        event_id: str,
        action: str,
        title: str = "",
        summary: str = "",
        event_type: str = "",
        event_source: str = "",
        camera_entity_id: str = "",
        followup_prompt: str | None = None,
        target_camera_entity_id: str | None = None,
        target_event: str | None = None,
        target_title: str | None = None,
        target_message: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(action or "").strip()
        if normalized == "ask_about_event":
            conversation = getattr(app.state, "conversation_service", None)
            if conversation is None:
                return {"ok": False, "action": normalized, "event_id": event_id, "text": ""}
            result = await conversation.handle_event_followup(
                EventFollowupRequest(
                    session_id=session_id,
                    user_text="Tell me about this event.",
                    event_type=event_type or "event",
                    event_summary=summary or title or None,
                    event_context={
                        "source": event_source or "admin_event_history",
                        "event_id": event_id,
                        "camera_entity_id": camera_entity_id,
                    },
                    followup_prompt=followup_prompt,
                )
            )
            return {
                "ok": True,
                "action": normalized,
                "event_id": event_id,
                "text": result.text,
                "session_id": result.session_id,
                "processing_time_ms": result.processing_time_ms,
            }

        if normalized == "show_related_camera":
            return await self._handle_related_camera_action(
                app=app,
                ws_mgr=ws_mgr,
                source_event_id=event_id,
                action_payload={
                    "target_camera_entity_id": target_camera_entity_id or camera_entity_id,
                    "target_event": target_event or "related_camera",
                    "target_title": target_title or title or "Related camera",
                    "target_message": target_message or summary or "Related live view",
                },
            )

        return {"ok": False, "action": normalized, "event_id": event_id, "text": ""}

    async def _handle_related_camera_action(
        self,
        *,
        app,
        ws_mgr,
        source_event_id: str,
        action_payload: dict[str, Any],
    ) -> dict[str, Any]:
        target_camera_entity_id = str(action_payload.get("target_camera_entity_id") or "").strip()
        target_event = str(action_payload.get("target_event") or "related_camera").strip() or "related_camera"
        target_title = str(action_payload.get("target_title") or "Related camera").strip() or "Related camera"
        target_message = str(action_payload.get("target_message") or "Related live view").strip() or "Related live view"
        if not target_camera_entity_id:
            return self._ack("show_related_camera", event_id=source_event_id, ok=False)

        ha = getattr(app.state, "ha_proxy", None)
        if ha is None:
            return self._ack("show_related_camera", event_id=source_event_id, ok=False)

        event_service = getattr(app.state, "event_service", None) or EventService()
        surface_state = getattr(app.state, "surface_state_service", None)
        resolved_camera = ha.resolve_camera_entity(target_camera_entity_id)
        opened_event_id = uuid.uuid4().hex
        await publish_visual_event(
            app=app,
            ws_mgr=ws_mgr,
            event_service=event_service,
            surface_state=surface_state,
            event_id=opened_event_id,
            event_type=target_event,
            title=target_title,
            message=target_message,
            camera_entity_id=resolved_camera,
            event_context={
                "source": "surface_action",
                "related_to_event_id": source_event_id,
            },
            expires_in_ms=45000,
        )
        return self._ack(
            "show_related_camera",
            event_id=source_event_id,
            ok=True,
            opened_event_id=opened_event_id,
        )

    @staticmethod
    def _ack(action: str, *, event_id: str | None = None, ok: bool | None = None, opened_event_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "surface_action_ack", "action": action}
        if event_id:
            payload["event_id"] = event_id
        if ok is not None:
            payload["ok"] = ok
        if opened_event_id:
            payload["opened_event_id"] = opened_event_id
        return payload

    @staticmethod
    def _action(
        action: str,
        label: str,
        *,
        tone: str,
        requires_confirmation: bool,
        confirm_text: str | None = None,
        followup_prompt: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "action": action,
            "label": label,
            "tone": tone,
            "requires_confirmation": requires_confirmation,
        }
        if confirm_text:
            payload["confirm_text"] = confirm_text
        if followup_prompt:
            payload["followup_prompt"] = followup_prompt
        if extra:
            payload.update(extra)
        return payload

    def _followup_actions(self, event_record: dict[str, Any]) -> list[dict[str, Any]]:
        text = " ".join(
            str(event_record.get(key) or "")
            for key in ("event", "title", "message", "open_loop_note")
        ).lower()
        label = "Ask about this"
        prompt = "Focus on the most relevant detail in this event before answering the user's question."
        actions: list[dict[str, Any]] = []
        if "doorbell" in text or "visitor" in text:
            label = "Ask who is there"
            prompt = "Focus on who is at the door, whether they appear familiar, and whether this looks like a delivery or visit."
            actions.append(self._action(
                "show_related_camera",
                "Show driveway too",
                tone="info",
                requires_confirmation=False,
                extra={
                    "target_camera_entity_id": "camera.outdoor_2",
                    "target_event": "related_camera",
                    "target_title": "Driveway",
                    "target_message": "Driveway live view",
                },
            ))
            actions.append(self._action(
                "ask_about_event",
                "Ask if it is a delivery",
                tone="info",
                requires_confirmation=False,
                followup_prompt="Focus on whether this event appears to be a package delivery, courier stop, or personal visitor.",
            ))
        elif "package" in text or "parcel" in text:
            label = "Ask about the delivery"
            prompt = "Focus on what was delivered, where the package was left, and whether it appears exposed or still outside."
            actions.append(self._action(
                "ask_about_event",
                "Ask where the package is",
                tone="info",
                requires_confirmation=False,
                followup_prompt="Focus on where the package or parcel was placed and whether it looks reachable, hidden, or exposed.",
            ))
        elif "driveway" in text or "vehicle" in text or "car" in text:
            label = "Ask about the vehicle"
            prompt = "Focus on the vehicle, what it is doing, and whether the arrival looks expected or unusual."
            actions.append(self._action(
                "show_related_camera",
                "Show doorbell too",
                tone="info",
                requires_confirmation=False,
                extra={
                    "target_camera_entity_id": "camera.doorbell",
                    "target_event": "related_camera",
                    "target_title": "Doorbell",
                    "target_message": "Front door live view",
                },
            ))
            actions.append(self._action(
                "ask_about_event",
                "Ask if it seems expected",
                tone="info",
                requires_confirmation=False,
                followup_prompt="Focus on whether the vehicle activity looks routine, expected, or worth attention.",
            ))
        elif "motion" in text or "outside" in text or "garden" in text:
            label = "Ask what moved"
            prompt = "Focus on what caused the motion, whether a person, animal, or vehicle is visible, and whether it needs attention."
            actions.append(self._action(
                "ask_about_event",
                "Ask if it matters",
                tone="info",
                requires_confirmation=False,
                followup_prompt="Focus on whether this motion looks meaningful, unusual, or worth following up on.",
            ))
        actions.insert(0, self._action(
            "ask_about_event",
            label,
            tone="info",
            requires_confirmation=False,
            followup_prompt=prompt,
        ))
        return actions
