from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import time
from typing import Any

from avatar_backend.models.events import EventEnvelope

_RECENT_EVENT_CONTEXT_TTL_S = 900


@dataclass
class EventRecord:
    event_id: str
    event_type: str
    title: str
    message: str = ""
    camera_entity_id: str | None = None
    image_urls: list[str] = field(default_factory=list)
    expires_in_ms: int = 30000
    open_loop_note: str = "Needs attention"
    event_context: dict[str, Any] = field(default_factory=dict)

    def to_surface_payload(self) -> dict[str, Any]:
        payload = {
            "event_id": self.event_id,
            "event": self.event_type,
            "title": self.title,
            "message": self.message,
            "expires_in_ms": self.expires_in_ms,
            "open_loop_note": self.open_loop_note,
        }
        if self.camera_entity_id:
            payload["camera_entity_id"] = self.camera_entity_id
        if self.image_urls:
            payload["image_urls"] = list(self.image_urls)
        return payload

    def to_context_payload(self) -> dict[str, Any]:
        payload = dict(self.event_context)
        if self.camera_entity_id:
            payload.setdefault("camera_entity_id", self.camera_entity_id)
        return payload

    def to_event_envelope(self) -> EventEnvelope:
        return EventEnvelope(
            event_id=self.event_id,
            event_type=self.event_type,
            source=str(self.event_context.get("source") or ""),
            camera_entity_id=self.camera_entity_id or "",
            summary=self.message or self.title,
            details=self.title if self.message else "",
            action_suggestions=[],
            data={
                "title": self.title,
                "image_urls": list(self.image_urls),
                "open_loop_note": self.open_loop_note,
                "event_context": self.to_context_payload(),
                "expires_in_ms": self.expires_in_ms,
            },
        )


class EventService:
    """Compatibility-first canonical event normalizer for V2.

    This does not replace the existing router flows yet. It provides one shared
    event shape that can be expanded later into a true event bus and persistent
    store without forcing routers and clients to keep inventing payloads.
    """

    def build_event(
        self,
        *,
        event_id: str,
        event_type: str,
        title: str | None = None,
        message: str | None = None,
        camera_entity_id: str | None = None,
        image_url: str | None = None,
        image_urls: list[str] | None = None,
        expires_in_ms: int = 30000,
        open_loop_note: str | None = None,
        event_context: dict[str, Any] | None = None,
    ) -> EventRecord:
        merged_urls = [url for url in ([image_url] + list(image_urls or [])) if url]
        return EventRecord(
            event_id=event_id,
            event_type=event_type,
            title=(title or event_type.replace("_", " ").title()).strip(),
            message=(message or "").strip(),
            camera_entity_id=(camera_entity_id or "").strip() or None,
            image_urls=merged_urls,
            expires_in_ms=expires_in_ms,
            open_loop_note=(open_loop_note or "Needs attention").strip(),
            event_context=dict(event_context or {}),
        )

    def to_dict(self, event: EventRecord) -> dict[str, Any]:
        return asdict(event)


def remember_recent_event_context(
    container,
    *,
    event_id: str,
    event_type: str,
    event_summary: str | None = None,
    event_context: dict[str, Any] | None = None,
) -> None:
    now = time.time()
    store: dict[str, tuple[float, dict[str, Any]]] = getattr(container, "recent_event_contexts", None)
    if store is None:
        store = {}
        container.recent_event_contexts = store
    expired = [key for key, (ts, _) in store.items() if now - ts > _RECENT_EVENT_CONTEXT_TTL_S]
    for key in expired:
        store.pop(key, None)
    store[event_id] = (
        now,
        {
            "event_type": event_type,
            "event_summary": event_summary or "",
            "event_context": dict(event_context or {}),
        },
    )


def persist_event_history(app, event_record: EventRecord) -> None:
    db = getattr(app.state, "metrics_db", None)
    if db is None:
        return
    try:
        db.insert_event_history(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event_id": event_record.event_id,
                "event_type": event_record.event_type,
                "title": event_record.title,
                "summary": event_record.message or event_record.title,
                "status": "active",
                "event_source": str(event_record.event_context.get("source") or ""),
                "camera_entity_id": event_record.camera_entity_id or "",
                "data": {
                    "image_urls": list(event_record.image_urls),
                    "open_loop_note": event_record.open_loop_note,
                    "event_context": event_record.to_context_payload(),
                },
            }
        )
    except Exception:
        pass


def persist_canonical_event(app, event_record: EventRecord) -> None:
    event_store = getattr(app.state, "event_store", None)
    if event_store is None:
        return
    try:
        event_store.create_event(event_record.to_event_envelope())
    except Exception:
        pass


async def publish_visual_event(
    *,
    app,
    ws_mgr,
    event_service: EventService | None,
    surface_state,
    event_id: str,
    event_type: str,
    title: str | None = None,
    message: str | None = None,
    camera_entity_id: str | None = None,
    image_url: str | None = None,
    image_urls: list[str] | None = None,
    event_context: dict[str, Any] | None = None,
    expires_in_ms: int = 30000,
    open_loop_note: str | None = None,
) -> EventRecord:
    service = event_service or EventService()
    event_record = service.build_event(
        event_id=event_id,
        event_type=event_type,
        title=title,
        message=message,
        camera_entity_id=camera_entity_id,
        image_url=image_url,
        image_urls=image_urls,
        event_context=event_context,
        expires_in_ms=expires_in_ms,
        open_loop_note=open_loop_note,
    )
    remember_recent_event_context(
        app.state._container,
        event_id=event_id,
        event_type=event_record.event_type,
        event_summary=event_record.message or event_record.title,
        event_context=event_record.to_context_payload(),
    )
    event_bus = getattr(app.state, "event_bus", None)
    if event_bus is not None:
        await event_bus.publish(event_record.to_event_envelope())
    persist_canonical_event(app, event_record)
    persist_event_history(app, event_record)
    payload = {"type": "visual_event", **event_record.to_surface_payload()}
    if surface_state is not None:
        await surface_state.record_visual_event(ws_mgr, payload)
    await ws_mgr.broadcast_to_voice_json(payload)
    return event_record
