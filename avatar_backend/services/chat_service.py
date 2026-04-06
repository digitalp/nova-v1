"""
Chat service — extracted from routers/chat.py for reuse by the voice pipeline.

Accepts a session_id + user text, runs the multi-round LLM/tool loop,
and returns the final text response plus all ToolCall results.
"""
from __future__ import annotations
import time
import time as _time_module
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from avatar_backend.models.messages import ChatResponse, ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.services.ha_proxy import HAProxy
from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.session_manager import SessionManager

_LOGGER = structlog.get_logger()

_MAX_TOOL_ROUNDS = 3


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    processing_time_ms: int = 0
    model: str = ""
    session_id: str = ""


async def run_chat(
    *,
    session_id: str,
    user_text: str,
    llm: LLMService,
    sm: SessionManager,
    ha: HAProxy,
) -> ChatResult:
    """
    Full multi-round chat → tool execution loop.

    1. Add user message to session.
    2. LLM generates response (may include tool calls).
    3. Execute any tool calls via HA proxy.
    4. Feed results back to LLM for a follow-up text.
    5. Repeat up to _MAX_TOOL_ROUNDS times.
    6. Return final text + annotated ToolCall list.
    """
    t_start = time.monotonic()
    all_tool_calls: list[ToolCall] = []

    await sm.add_message(session_id, "user", user_text)
    messages = await sm.get_messages(session_id)

    # Inject current datetime + timezone into the system message so the LLM can answer
    # time/date questions without needing a tool call.
    tz_name = _time_module.strftime("%Z")  # "BST" in summer, "GMT" in winter
    now_str = datetime.now().strftime(f"%A, %d %B %Y %H:%M {tz_name}")
    messages = _inject_datetime(messages, now_str)

    final_text = ""
    model_name = ""

    for round_num in range(_MAX_TOOL_ROUNDS + 1):
        text, tool_calls = await llm.chat(messages)
        model_name = llm.model_name

        if not tool_calls:
            await sm.add_message(session_id, "assistant", text)
            final_text = text
            break

        # Store assistant turn with tool calls in Ollama wire format
        raw_tcs: list[dict] = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.function_name,
                    "arguments": tc.arguments,
                },
            }
            for i, tc in enumerate(tool_calls)
        ]
        await sm.add_message(session_id, "assistant", text or "", tool_calls=raw_tcs)

        for tc in tool_calls:
            if tc.function_name == "describe_camera":
                entity_id = tc.arguments.get("entity_id", "")
                image_bytes = await ha.fetch_camera_image(entity_id)
                if image_bytes:
                    description = await llm.describe_image(image_bytes)
                    result = ToolResult(success=True, message=description)
                else:
                    result = ToolResult(success=False, message=f"Could not capture image from {entity_id}. The camera may be offline.")
            else:
                result = await ha.execute_tool_call(tc)
            tc.acl_status = "allowed" if result.success else "denied"
            tc.acl_reason = result.message if not result.success else ""
            all_tool_calls.append(tc)
            await sm.add_message(session_id, "tool", result.message)

        messages = _inject_datetime(await sm.get_messages(session_id), now_str)  # now_str already has tz

        if round_num == _MAX_TOOL_ROUNDS:
            _LOGGER.warning("chat.max_tool_rounds_reached",
                            session_id=session_id, rounds=round_num + 1)
            # Ask the LLM for a final summary without triggering more tools
            text2, _ = await llm.chat(messages)
            await sm.add_message(session_id, "assistant", text2)
            final_text = text2

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    return ChatResult(
        text=final_text,
        tool_calls=all_tool_calls,
        processing_time_ms=elapsed_ms,
        model=llm.model_name,
        session_id=session_id,
    )


def _inject_datetime(messages: list[dict], now_str: str) -> list[dict]:
    """Prepend current datetime to the system message so the LLM can answer
    time/date questions. Does not mutate the original list."""
    if not messages:
        return messages
    result = list(messages)
    sys_msg = dict(result[0])
    original = sys_msg.get("content", "")
    if not original.startswith(f"Current date/time: {now_str}"):
        sys_msg["content"] = f"Current date/time: {now_str}\n\n{original}"
    result[0] = sys_msg
    return result
