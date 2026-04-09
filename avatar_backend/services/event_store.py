from __future__ import annotations

from typing import Any

from avatar_backend.models.events import EventEnvelope
from avatar_backend.services.metrics_db import MetricsDB


class EventStoreService:
    """Persistent canonical event store for V2 event records and lifecycle data."""

    def __init__(self, db: MetricsDB) -> None:
        self._db = db

    def create_event(self, event: EventEnvelope | dict[str, Any]) -> dict[str, Any]:
        envelope = event if isinstance(event, EventEnvelope) else EventEnvelope.model_validate(event)
        self._db.insert_event_record(envelope.model_dump())
        return self.get_event(envelope.event_id) or envelope.model_dump()

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return self._db.get_event_record(event_id)

    def list_events(
        self,
        *,
        limit: int = 100,
        event_type: str | None = None,
        status: str | None = None,
        source: str | None = None,
        camera_entity_id: str | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._db.list_event_records(
            limit=limit,
            event_type=event_type,
            status=status,
            source=source,
            camera_entity_id=camera_entity_id,
            created_after=created_after,
            created_before=created_before,
        )

    def update_status(
        self,
        event_id: str,
        *,
        status: str,
        open_loop_note: str | None = None,
        admin_note: str | None = None,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
    ) -> dict[str, Any] | None:
        ok = self._db.update_event_record_status(
            event_id,
            status=status,
            open_loop_note=open_loop_note,
            admin_note=admin_note,
            reminder_sent=reminder_sent,
            escalation_level=escalation_level,
        )
        if not ok:
            return None
        return self.get_event(event_id)

    def record_action(
        self,
        *,
        event_id: str,
        action_id: str,
        action_type: str,
        status: str = "completed",
        result: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._db.insert_event_action(
            event_id=event_id,
            action_id=action_id,
            action_type=action_type,
            status=status,
            result=result,
        )
        return self._db.list_event_actions(event_id)

    def add_media(
        self,
        *,
        event_id: str,
        media_type: str,
        url: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._db.insert_event_media(
            event_id=event_id,
            media_type=media_type,
            url=url,
            metadata=metadata,
        )
        return self._db.list_event_media(event_id)

    def touch_conversation_session(
        self,
        *,
        session_id: str,
        surface: str = "",
        linked_event_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._db.upsert_conversation_session(
            session_id=session_id,
            surface=surface,
            linked_event_id=linked_event_id,
            metadata=metadata,
        )

    def add_turn_summary(
        self,
        *,
        session_id: str,
        role: str,
        summary: str,
        event_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._db.insert_conversation_turn_summary(
            session_id=session_id,
            role=role,
            summary=summary,
            event_id=event_id,
            metadata=metadata,
        )
