from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A parsed tool call from the LLM response."""
    function_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Populated in Phase 3 when ha_proxy checks the ACL
    acl_status: str = "pending"   # "pending" | "allowed" | "denied"
    acl_reason: str = ""


class ChatRequest(BaseModel):
    session_id: str = Field(..., max_length=128)
    text: str
    # Optional context injected by HA automations (time, room, active devices, etc.)
    context: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    session_id: str
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    processing_time_ms: int = 0
    model: str = ""
