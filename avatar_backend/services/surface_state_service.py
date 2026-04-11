from __future__ import annotations

import asyncio
import time
from typing import Any

from avatar_backend.services.action_service import ActionService
from avatar_backend.services.ws_manager import ConnectionManager


class SurfaceStateService:
    """Compatibility-first surface state registry for avatar and voice clients."""
    _SNOOZE_SECONDS = 30 * 60

    def __init__(self, *, max_recent_events: int = 8, action_service: ActionService | None = None) -> None:
        self._max_recent_events = max_recent_events
        self._action_service = action_service or ActionService()
        self._avatar_state = "idle"
        self._active_event: dict[str, Any] | None = None
        self._recent_events: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def set_avatar_state(self, ws_mgr: ConnectionManager, state: str) -> None:
        async with self._lock:
            self._avatar_state = state
            snapshot = self._snapshot_unlocked()
        await ws_mgr.broadcast_json({"type": "avatar_state", "state": state})
        await self._broadcast_snapshot(ws_mgr, snapshot)

    async def record_visual_event(self, ws_mgr: ConnectionManager, event_payload: dict[str, Any]) -> None:
        now_ts = time.time()
        event_record = {
            "event_id": event_payload.get("event_id"),
            "event": event_payload.get("event"),
            "title": event_payload.get("title"),
            "message": event_payload.get("message"),
            "camera_entity_id": event_payload.get("camera_entity_id"),
            "image_urls": list(event_payload.get("image_urls") or []),
            "expires_in_ms": event_payload.get("expires_in_ms"),
            "status": "active",
            "open_loop_note": str(event_payload.get("open_loop_note") or "Needs attention"),
            "open_loop_state": "active",
            "open_loop_active": True,
            "open_loop_started_ts": now_ts,
            "open_loop_updated_ts": now_ts,
            "ts": now_ts,
        }
        async with self._lock:
            self._active_event = event_record
            self._recent_events = [event_record] + [
                item for item in self._recent_events
                if item.get("event_id") != event_record.get("event_id")
            ]
            self._recent_events = self._recent_events[: self._max_recent_events]
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)

    async def get_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot_unlocked()

    async def dismiss_active_event(self, ws_mgr: ConnectionManager) -> None:
        async with self._lock:
            if self._active_event and self._active_event.get("event_id"):
                active_id = self._active_event["event_id"]
                for item in self._recent_events:
                    if item.get("event_id") == active_id:
                        item["status"] = "dismissed"
                        item["open_loop_note"] = "Hidden for now"
                        item["open_loop_state"] = "dismissed"
                        item["open_loop_active"] = True
                        item["open_loop_updated_ts"] = time.time()
            self._active_event = None
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)

    async def acknowledge_active_event(self, ws_mgr: ConnectionManager) -> None:
        async with self._lock:
            if self._active_event and self._active_event.get("event_id"):
                active_id = self._active_event["event_id"]
                for item in self._recent_events:
                    if item.get("event_id") == active_id:
                        item["status"] = "acknowledged"
                        item["open_loop_note"] = "Seen by user"
                        item["open_loop_state"] = "acknowledged"
                        item["open_loop_active"] = True
                        item["open_loop_updated_ts"] = time.time()
                        self._active_event = dict(item)
                        break
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)

    async def resolve_active_event(self, ws_mgr: ConnectionManager) -> None:
        async with self._lock:
            if self._active_event and self._active_event.get("event_id"):
                active_id = self._active_event["event_id"]
                for item in self._recent_events:
                    if item.get("event_id") == active_id:
                        item["status"] = "resolved"
                        item["open_loop_note"] = "Closed out"
                        item["open_loop_state"] = "resolved"
                        item["open_loop_active"] = False
                        item["open_loop_updated_ts"] = time.time()
                        item["open_loop_resolved_ts"] = item["open_loop_updated_ts"]
                        break
            self._active_event = None
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)

    async def snooze_active_event(self, ws_mgr: ConnectionManager) -> None:
        async with self._lock:
            if self._active_event and self._active_event.get("event_id"):
                active_id = self._active_event["event_id"]
                snoozed_until = time.time() + self._SNOOZE_SECONDS
                for item in self._recent_events:
                    if item.get("event_id") == active_id:
                        item["status"] = "snoozed"
                        item["snoozed_until_ts"] = snoozed_until
                        item["open_loop_note"] = "Snoozed for 30 minutes"
                        item["open_loop_state"] = "snoozed"
                        item["open_loop_active"] = True
                        item["open_loop_updated_ts"] = time.time()
                        break
            self._active_event = None
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)

    async def dismiss_recent_event(self, ws_mgr: ConnectionManager, event_id: str) -> bool:
        async with self._lock:
            match = next((item for item in self._recent_events if item.get("event_id") == event_id), None)
            if not match:
                return False
            match["status"] = "dismissed"
            match["open_loop_note"] = "Hidden for now"
            match["open_loop_state"] = "dismissed"
            match["open_loop_active"] = True
            match["open_loop_updated_ts"] = time.time()
            if self._active_event and self._active_event.get("event_id") == event_id:
                self._active_event = None
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)
        return True

    async def acknowledge_recent_event(self, ws_mgr: ConnectionManager, event_id: str) -> bool:
        async with self._lock:
            match = next((item for item in self._recent_events if item.get("event_id") == event_id), None)
            if not match:
                return False
            match["status"] = "acknowledged"
            match["open_loop_note"] = "Seen by user"
            match["open_loop_state"] = "acknowledged"
            match["open_loop_active"] = True
            match["open_loop_updated_ts"] = time.time()
            if self._active_event and self._active_event.get("event_id") == event_id:
                self._active_event = dict(match)
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)
        return True

    async def resolve_recent_event(self, ws_mgr: ConnectionManager, event_id: str) -> bool:
        async with self._lock:
            match = next((item for item in self._recent_events if item.get("event_id") == event_id), None)
            if not match:
                return False
            match["status"] = "resolved"
            match["open_loop_note"] = "Closed out"
            match["open_loop_state"] = "resolved"
            match["open_loop_active"] = False
            match["open_loop_updated_ts"] = time.time()
            match["open_loop_resolved_ts"] = match["open_loop_updated_ts"]
            if self._active_event and self._active_event.get("event_id") == event_id:
                self._active_event = None
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)
        return True

    async def snooze_recent_event(self, ws_mgr: ConnectionManager, event_id: str) -> bool:
        async with self._lock:
            match = next((item for item in self._recent_events if item.get("event_id") == event_id), None)
            if not match:
                return False
            match["status"] = "snoozed"
            match["snoozed_until_ts"] = time.time() + self._SNOOZE_SECONDS
            match["open_loop_note"] = "Snoozed for 30 minutes"
            match["open_loop_state"] = "snoozed"
            match["open_loop_active"] = True
            match["open_loop_updated_ts"] = time.time()
            if self._active_event and self._active_event.get("event_id") == event_id:
                self._active_event = None
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)
        return True

    async def activate_recent_event(self, ws_mgr: ConnectionManager, event_id: str) -> bool:
        async with self._lock:
            match = next((item for item in self._recent_events if item.get("event_id") == event_id), None)
            if not match:
                return False
            match["status"] = "active"
            match["open_loop_note"] = "Needs attention"
            match["open_loop_state"] = "active"
            match["open_loop_active"] = True
            match["open_loop_updated_ts"] = time.time()
            match.pop("snoozed_until_ts", None)
            match.pop("open_loop_resolved_ts", None)
            self._active_event = dict(match)
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)
        return True

    async def apply_open_loop_workflow(
        self,
        ws_mgr: ConnectionManager,
        event_id: str,
        *,
        open_loop_note: str | None = None,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
    ) -> bool:
        async with self._lock:
            match = next((item for item in self._recent_events if item.get("event_id") == event_id), None)
            if not match:
                return False
            now_ts = time.time()
            match["open_loop_updated_ts"] = now_ts
            if open_loop_note:
                match["open_loop_note"] = open_loop_note
            if reminder_sent:
                match["open_loop_last_reminder_ts"] = now_ts
                match["open_loop_reminder_count"] = int(match.get("open_loop_reminder_count") or 0) + 1
            if escalation_level:
                match["open_loop_escalation_level"] = escalation_level
                match["open_loop_last_escalation_ts"] = now_ts
            if self._active_event and self._active_event.get("event_id") == event_id:
                self._active_event = dict(match)
            snapshot = self._snapshot_unlocked()
        await self._broadcast_snapshot(ws_mgr, snapshot)
        return True

    async def _broadcast_snapshot(self, ws_mgr: ConnectionManager, snapshot: dict[str, Any]) -> None:
        payload = {"type": "surface_state", **snapshot}
        await ws_mgr.broadcast_json(payload)
        await ws_mgr.broadcast_to_voice_json(payload)

    def _snapshot_unlocked(self) -> dict[str, Any]:
        return {
            "avatar_state": self._avatar_state,
            "active_event": self._serialize_event(self._active_event, is_active=True) if self._active_event else None,
            "recent_events": [self._serialize_event(item, is_active=False) for item in self._recent_events],
        }

    def _serialize_event(self, event_record: dict[str, Any], *, is_active: bool) -> dict[str, Any]:
        payload = dict(event_record)
        payload["suggested_actions"] = self._action_service.build_suggested_actions(payload, is_active=is_active)
        return payload
