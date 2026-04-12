from __future__ import annotations
from pydantic import BaseModel


class ToolResult(BaseModel):
    """
    The outcome of executing one tool call via ha_proxy.
    This is fed back into the conversation history so the LLM can
    respond naturally ("The kitchen light is now on." or
    "I'm not permitted to control door locks.").
    """
    success: bool
    message: str        # Human-readable — shown to the LLM as a tool role message
    entity_id: str = ""
    service_called: str = ""   # "domain.service", e.g. "light.turn_on"
    ha_status_code: int = 0    # HTTP status from HA API, 0 if ACL-blocked
