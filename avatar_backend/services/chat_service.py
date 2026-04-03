"""
Chat service — extracted from routers/chat.py for reuse by the voice pipeline.

Accepts a session_id + user text, runs the multi-round LLM/tool loop,
and returns the final text response plus all ToolCall results.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
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
            result: ToolResult = await ha.execute_tool_call(tc)
            tc.acl_status = "allowed" if result.success else "denied"
            tc.acl_reason = result.message if not result.success else ""
            all_tool_calls.append(tc)
            await sm.add_message(session_id, "tool", result.message)

        messages = await sm.get_messages(session_id)

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
