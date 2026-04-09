from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventEnvelope(BaseModel):
    """Canonical V2 event schema for cross-service publication."""

    event_id: str
    event_type: str
    source: str = ""
    room: str = ""
    camera_entity_id: str = ""
    severity: Literal["info", "normal", "warn", "critical"] = "normal"
    summary: str = ""
    details: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "acknowledged", "dismissed", "resolved", "snoozed"] = "active"
    created_at: str = Field(default_factory=_utc_now_iso)
    expires_at: str = ""
    action_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    linked_session_id: str = ""
    linked_media: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
