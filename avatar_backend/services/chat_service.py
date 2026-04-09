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
from avatar_backend.services.decision_log import DecisionLog
from avatar_backend.services.ha_proxy import HAProxy
from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.persistent_memory import PersistentMemoryService
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
    decision_log: DecisionLog | None = None,
    memory_service: PersistentMemoryService | None = None,
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

    memory_ids: list[int] = []
    if memory_service is not None:
        memory_context, memory_ids = memory_service.build_context(user_text)
        if memory_context:
            messages = _inject_persistent_memory(messages, memory_context)

    # Inject current datetime + timezone into the system message so the LLM can answer
    # time/date questions without needing a tool call.
    tz_name = _time_module.strftime("%Z")  # "BST" in summer, "GMT" in winter
    now_str = datetime.now().strftime(f"%A, %d %B %Y %H:%M {tz_name}")
    messages = _inject_datetime(messages, now_str)

    final_text = ""
    model_name = ""

    # Sanitize history: remove any trailing assistant tool-call turns that have no
    # corresponding tool-response. These cause Gemini HTTP 400 ("function call turn
    # comes immediately after a user turn").
    messages = _sanitize_history(messages)

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
                "function": {
                    "name": tc.function_name,
                    "arguments": tc.arguments,
                },
            }
            for tc in tool_calls
        ]
        await sm.add_message(session_id, "assistant", text or "", tool_calls=raw_tcs)
        messages = list(messages) + [{
            "role": "assistant",
            "content": text or "",
            "tool_calls": raw_tcs,
        }]

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
            if decision_log:
                decision_log.record(
                    "tool_call",
                    tool=tc.function_name,
                    args={k: str(v)[:80] for k, v in tc.arguments.items()},
                    success=result.success,
                    result=result.message[:120] if result.message else "",
                    session=session_id,
                )
            tc.acl_reason = result.message if not result.success else ""
            all_tool_calls.append(tc)
            await sm.add_message(session_id, "tool", result.message)
            messages = list(messages) + [{"role": "tool", "content": result.message}]

        if memory_service is not None:
            memory_context, memory_ids = memory_service.build_context(user_text)
            if memory_context:
                messages = _inject_persistent_memory(messages, memory_context)
        messages = _inject_datetime(messages, now_str)  # now_str already has tz
        messages = _sanitize_history(messages)

        if round_num == _MAX_TOOL_ROUNDS:
            _LOGGER.warning("chat.max_tool_rounds_reached",
                            session_id=session_id, rounds=round_num + 1)
            # Ask the LLM for a final summary without triggering more tools
            text2, _ = await llm.chat(messages)
            await sm.add_message(session_id, "assistant", text2)
            final_text = text2

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    if memory_service is not None and memory_ids:
        memory_service.mark_referenced(memory_ids)

    if memory_service is not None and final_text:
        memory_service.learn_from_exchange_async(
            session_id=session_id,
            user_text=user_text,
            assistant_text=final_text,
            llm=llm,
        )

    if decision_log and (final_text or all_tool_calls):
        decision_log.record(
            "chat_response",
            session=session_id,
            query=user_text[:100],
            tool_count=len(all_tool_calls),
            response=final_text[:150] if final_text else "",
            ms=elapsed_ms,
        )
    return ChatResult(
        text=final_text,
        tool_calls=all_tool_calls,
        processing_time_ms=elapsed_ms,
        model=llm.model_name,
        session_id=session_id,
    )


def _inject_persistent_memory(messages: list[dict], memory_context: str) -> list[dict]:
    """Append long-term household memory to the system message for this turn."""
    if not messages or not memory_context:
        return messages
    result = list(messages)
    sys_msg = dict(result[0])
    original = sys_msg.get("content", "")
    marker = "Long-term household memory."
    if marker not in original:
        sys_msg["content"] = f"{original}\n\n{memory_context}".strip()
    result[0] = sys_msg
    return result


def _drop_dangling_tool_calls(messages: list[dict]) -> list[dict]:
    """Remove trailing assistant tool-call turns not followed by a tool response.

    Gemini rejects a conversation where an assistant message containing function
    calls is immediately followed by a user turn (or is the last message).
    This happens when an exception interrupts the tool-execution loop, leaving
    the session history in an invalid state.
    """
    if len(messages) < 2:
        return messages
    result = list(messages)
    # Walk backwards from the end; drop orphaned assistant+tool_calls entries
    while len(result) >= 2:
        last = result[-1]
        prev = result[-2]
        # An assistant message with tool_calls that is NOT followed by a tool response
        if (prev.get("role") == "assistant"
                and prev.get("tool_calls")
                and last.get("role") != "tool"):
            result.pop(-2)  # remove the dangling assistant turn
        else:
            break
    return result


def _drop_orphan_tool_messages(messages: list[dict]) -> list[dict]:
    """Remove tool messages that no longer have a preceding assistant tool-call turn.

    Session trimming can evict the assistant tool-call message while leaving later
    tool responses behind. Gemini and Ollama both reject that conversation shape.
    """
    if len(messages) < 2:
        return messages
    result: list[dict] = []
    pending_tool_results = 0
    for index, msg in enumerate(messages):
        role = msg.get("role")
        if index == 0 and role == "system":
            result.append(msg)
            continue
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            pending_tool_results = len(tool_calls)
            result.append(msg)
            continue
        if role == "tool":
            if pending_tool_results > 0:
                result.append(msg)
                pending_tool_results -= 1
            continue
        pending_tool_results = 0
        result.append(msg)
    return result


def _sanitize_history(messages: list[dict]) -> list[dict]:
    return _drop_dangling_tool_calls(_drop_orphan_tool_messages(messages))


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
