"""
Chat service — extracted from routers/chat.py for reuse by the voice pipeline.

Accepts a session_id + user text, runs the multi-round LLM/tool loop,
and returns the final text response plus all ToolCall results.
"""
from __future__ import annotations
import re
import time
import time as _time_module
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from avatar_backend.config import get_settings
from avatar_backend.models.messages import ChatResponse, ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.services.decision_log import DecisionLog
from avatar_backend.services.ha_proxy import HAProxy
from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.persistent_memory import PersistentMemoryService
from avatar_backend.services.presence_context import PresenceContextService
from avatar_backend.services.session_manager import SessionManager
from avatar_backend.services.metrics_db import MetricsDB

_LOGGER = structlog.get_logger()

_MAX_TOOL_ROUNDS = 3
_TIME_QUERY_RE = re.compile(
    r"\b(what(?:'s|\s+is)?\s+the\s+time|what\s+time\s+is\s+it|current\s+time|tell\s+me\s+the\s+time|time\s+now)\b",
    re.IGNORECASE,
)
_DATE_QUERY_RE = re.compile(
    r"\b(what(?:'s|\s+is)?\s+the\s+date|what\s+day\s+is\s+it|today'?s\s+date|current\s+date|date\s+today)\b",
    re.IGNORECASE,
)
_OPERATIONAL_SESSION_HINTS = {
    "ha_power_alert": (
        "This is an automated Home Assistant power alert session. "
        "Read sensor values with get_entity_state only. "
        "Do not call any sensor services. "
        "Never invent services like sensor.tts.say. "
        "If you need to speak, respond with plain text only and the system will announce it. "
        "Use call_ha_service only for actual controllable domains such as climate, light, switch, lock, fan, cover, media_player, button, or input_boolean."
    ),
    "ha_car_warning": (
        "This is an automated car warning session. "
        "Read warning sensors and lock state with get_entity_state only. "
        "Do not call binary_sensor.turn_on, binary_sensor.turn_off, binary_sensor.toggle, or binary_sensor.update_entity. "
        "If a refresh is truly needed, only homeassistant.update_entity is valid, but prefer reading current state first."
    ),
}
_AUTOMATED_SESSION_COOLDOWNS: dict[str, int] | None = None
_LAST_AUTOMATED_SESSION_RUN_AT: dict[str, float] = {}


def _get_cooldowns() -> dict[str, int]:
    global _AUTOMATED_SESSION_COOLDOWNS
    if _AUTOMATED_SESSION_COOLDOWNS is None:
        _AUTOMATED_SESSION_COOLDOWNS = {
            "ha_power_alert": get_settings().ha_power_alert_cooldown_s,
        }
    return _AUTOMATED_SESSION_COOLDOWNS


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    processing_time_ms: int = 0
    model: str = ""
    session_id: str = ""


def _is_automated_session_on_cooldown(session_id: str) -> bool:
    session_key = (session_id or "").strip().lower()
    cooldown_s = _get_cooldowns().get(session_key)
    if not cooldown_s or cooldown_s <= 0:
        return False
    now = time.monotonic()
    last = _LAST_AUTOMATED_SESSION_RUN_AT.get(session_key)
    if last is not None and (now - last) < cooldown_s:
        return True
    _LAST_AUTOMATED_SESSION_RUN_AT[session_key] = now
    return False


async def run_chat(
    *,
    session_id: str,
    user_text: str,
    llm: LLMService,
    sm: SessionManager,
    ha: HAProxy,
    decision_log: DecisionLog | None = None,
    memory_service: PersistentMemoryService | None = None,
    presence_service: PresenceContextService | None = None,
    metrics_db: MetricsDB | None = None,
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

    if _is_automated_session_on_cooldown(session_id):
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        if decision_log:
            decision_log.record(
                "automated_session_cooldown",
                session=session_id,
                query=user_text[:100],
                cooldown_s=_get_cooldowns().get((session_id or "").strip().lower(), 0),
            )
        return ChatResult(
            text="",
            tool_calls=[],
            processing_time_ms=elapsed_ms,
            model="cooldown_skip",
            session_id=session_id,
        )

    await sm.add_message(session_id, "user", user_text)
    messages = await sm.get_messages(session_id)
    operational_prompt = _operational_prompt_for_session(session_id, user_text)
    if operational_prompt:
        messages = _inject_operational_prompt(messages, operational_prompt)

    memory_ids: list[int] = []
    enforced_memory_ids: list[int] = []
    if memory_service is not None:
        enforced_context, enforced_memory_ids = memory_service.build_enforced_preferences_context()
        if enforced_context:
            messages = _inject_enforced_preferences(messages, enforced_context)
            if decision_log:
                decision_log.record(
                    "memory_context_used",
                    session=session_id,
                    query=user_text[:100],
                    memory_count=len(enforced_memory_ids),
                    memory_ids=enforced_memory_ids,
                    memory_preview=enforced_context[:240],
                    phase="enforced",
                )
        try:
            memory_context, memory_ids = await memory_service.build_context_async(user_text, session_id=session_id)
        except Exception:
            memory_context, memory_ids = memory_service.build_context(user_text)
        if memory_context:
            if decision_log:
                decision_log.record(
                    "memory_context_used",
                    session=session_id,
                    query=user_text[:100],
                    memory_count=len(memory_ids),
                    memory_ids=memory_ids,
                    memory_preview=memory_context[:240],
                    phase="initial",
                )
            messages = _inject_persistent_memory(messages, memory_context)

    # Inject current datetime + timezone into the system message so the LLM can answer
    # time/date questions without needing a tool call.
    tz_name = _time_module.strftime("%Z")  # "BST" in summer, "GMT" in winter
    now_str = datetime.now().strftime(f"%A, %d %B %Y %H:%M {tz_name}")
    direct_response = _maybe_direct_time_or_date_response(user_text, tz_name=tz_name)
    if direct_response:
        await sm.add_message(session_id, "assistant", direct_response)
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        if decision_log:
            decision_log.record(
                "chat_response",
                session=session_id,
                query=user_text[:100],
                tool_count=0,
                response=direct_response[:150],
                ms=elapsed_ms,
            )
        return ChatResult(
            text=direct_response,
            tool_calls=[],
            processing_time_ms=elapsed_ms,
            model="deterministic_time",
            session_id=session_id,
        )
    messages = _inject_datetime(messages, now_str)

    if presence_service is not None:
        try:
            presence_context = await presence_service.get_context()
            if presence_context:
                messages = _inject_presence_context(messages, presence_context)
        except Exception:
            pass  # never block a conversation turn on presence fetch failure

    final_text = ""
    model_name = ""

    # Sanitize history: remove any trailing assistant tool-call turns that have no
    # corresponding tool-response. These cause Gemini HTTP 400 ("function call turn
    # comes immediately after a user turn").
    messages = _sanitize_history(messages)

    for round_num in range(_MAX_TOOL_ROUNDS + 1):
        text, tool_calls = await _chat_for_session(llm, session_id, messages, use_tools=True)
        model_name = _model_name_for_session(llm, session_id)

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
            enforced_context, enforced_memory_ids = memory_service.build_enforced_preferences_context()
            if enforced_context:
                messages = _inject_enforced_preferences(messages, enforced_context)
            try:
                memory_context, memory_ids = await memory_service.build_context_async(user_text)
            except Exception:
                memory_context, memory_ids = memory_service.build_context(user_text)
            if memory_context:
                if decision_log:
                    decision_log.record(
                        "memory_context_used",
                        session=session_id,
                        query=user_text[:100],
                        memory_count=len(memory_ids),
                        memory_ids=memory_ids,
                        memory_preview=memory_context[:240],
                        phase=f"tool_round_{round_num + 1}",
                    )
                messages = _inject_persistent_memory(messages, memory_context)
        messages = _inject_datetime(messages, now_str)  # now_str already has tz
        if operational_prompt:
            messages = _inject_operational_prompt(messages, operational_prompt)
        messages = _sanitize_history(messages)

        if round_num == _MAX_TOOL_ROUNDS:
            _LOGGER.warning("chat.max_tool_rounds_reached",
                            session_id=session_id, rounds=round_num + 1)
            # Ask the LLM for a final summary without triggering more tools
            text2, _ = await _chat_for_session(llm, session_id, messages, use_tools=False)
            await sm.add_message(session_id, "assistant", text2)
            final_text = text2

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    if memory_service is not None and (memory_ids or enforced_memory_ids):
        memory_service.mark_referenced(sorted(set(memory_ids + enforced_memory_ids)))
        if decision_log:
            decision_log.record(
                "memory_context_referenced",
                session=session_id,
                query=user_text[:100],
                memory_count=len(set(memory_ids + enforced_memory_ids)),
                memory_ids=sorted(set(memory_ids + enforced_memory_ids)),
            )

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

    # ── Conversation audit trail ──────────────────────────────────────────
    if metrics_db is not None:
        try:
            metrics_db.insert_conversation_audit({
                "session_id": session_id,
                "user_text": user_text,
                "context_summary": "",
                "llm_response": "",
                "tool_calls": [
                    {"name": tc.function_name, "args": tc.arguments, "status": tc.acl_status}
                    for tc in all_tool_calls
                ],
                "final_reply": final_text or "",
                "processing_ms": elapsed_ms,
                "model": model_name or llm.model_name,
            })
        except Exception:
            _LOGGER.warning("conversation_audit.insert_failed", session_id=session_id)

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


def _operational_prompt_for_session(session_id: str, user_text: str) -> str:
    session_key = (session_id or "").strip().lower()
    if session_key in _OPERATIONAL_SESSION_HINTS:
        return _OPERATIONAL_SESSION_HINTS[session_key]
    text = (user_text or "").lower()
    if "weather" in text:
        from avatar_backend.services.home_runtime import load_home_runtime_config
        _rt = load_home_runtime_config()
        _we = _rt.weather_entity or "weather.forecast_home"
        return (
            f"For weather reads, use get_entity_state('{_we}'). "
            "Do not call weather.get_state or any other weather service unless you are explicitly requesting forecasts through the dedicated backend path."
        )
    return ""


def _inject_operational_prompt(messages: list[dict], operational_prompt: str) -> list[dict]:
    if not messages or not operational_prompt:
        return messages
    result = list(messages)
    for idx, message in enumerate(result):
        if message.get("role") == "system":
            updated = dict(message)
            content = str(updated.get("content") or "").strip()
            updated["content"] = f"{operational_prompt}\n\n{content}".strip() if content else operational_prompt
            result[idx] = updated
            return result
    return [{"role": "system", "content": operational_prompt}, *result]


async def _chat_for_session(
    llm: LLMService,
    session_id: str,
    messages: list[dict],
    *,
    use_tools: bool,
) -> tuple[str, list[ToolCall]]:
    if (session_id or "").strip().lower().startswith("ha_") and hasattr(llm, "chat_operational"):
        return await llm.chat_operational(messages, use_tools=use_tools, purpose=session_id)
    return await llm.chat(messages, use_tools=use_tools)


def _model_name_for_session(llm: LLMService, session_id: str) -> str:
    if (session_id or "").strip().lower().startswith("ha_") and hasattr(llm, "operational_model_name"):
        return llm.operational_model_name
    return llm.model_name


def _inject_enforced_preferences(messages: list[dict], memory_context: str) -> list[dict]:
    if not messages or not memory_context:
        return messages
    result = list(messages)
    sys_msg = dict(result[0])
    original = sys_msg.get("content", "")
    marker = "Enforced household preferences and policies."
    if marker not in original:
        sys_msg["content"] = f"{memory_context}\n\n{original}".strip()
    result[0] = sys_msg
    return result


def _maybe_direct_time_or_date_response(user_text: str, *, tz_name: str) -> str:
    text = " ".join((user_text or "").split()).strip().lower()
    if not text:
        return ""
    now = datetime.now()
    if _TIME_QUERY_RE.search(text):
        return f"The current time is {now.strftime('%H:%M')} {tz_name}."
    if _DATE_QUERY_RE.search(text):
        return f"Today is {now.strftime('%A, %d %B %Y')}."
    return ""


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


def _inject_presence_context(messages: list[dict], presence_context: str) -> list[dict]:
    """Append live presence context to the system message for this turn."""
    if not messages or not presence_context:
        return messages
    result = list(messages)
    sys_msg = dict(result[0])
    original = sys_msg.get("content", "")
    marker = "Presence context:"
    if marker not in original:
        sys_msg["content"] = f"{original}\n\nPresence context: {presence_context}".strip()
    result[0] = sys_msg
    return result


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
