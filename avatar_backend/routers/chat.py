from __future__ import annotations
import re
import time
from fastapi import APIRouter, Depends, HTTPException, Request, status
import structlog

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.models.messages import ChatRequest, ChatResponse, ToolCall
from avatar_backend.services.ha_proxy import HAProxy
from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.session_manager import SessionManager

router = APIRouter(tags=["chat"])
logger = structlog.get_logger()

# Guard against pathological tool-call loops
_MAX_TOOL_ROUNDS = 3


def _to_ollama_tool_calls(tool_calls: list[ToolCall]) -> list[dict]:
    """Convert ToolCall models → Ollama wire format for session history."""
    return [
        {"function": {"name": tc.function_name, "arguments": tc.arguments}}
        for tc in tool_calls
    ]


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """
    Full tool-execution chat endpoint.

    Flow per user message:
      1. Add user message to session history.
      2. Call LLM → get text and/or tool calls.
      3. If tool calls: execute via ha_proxy (ACL enforced), feed results
         back into history, call LLM again for a natural language response.
      4. Repeat up to _MAX_TOOL_ROUNDS times, then return.
    """
    t0 = time.monotonic()

    llm: LLMService     = request.app.state.llm_service
    sm:  SessionManager = request.app.state.session_manager
    ha:  HAProxy        = request.app.state.ha_proxy

    if not await llm.is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM not ready — model may still be loading.",
        )

    # Optionally inject HA context (time, room, active devices, etc.)
    user_text = body.text
    if body.context:
        _CTX_KEY_RE  = re.compile(r'^[a-zA-Z0-9_\-\.]{1,64}$')
        _CTX_MAX_VAL = 256
        sanitized: dict = {}
        for k, v in body.context.items():
            if not isinstance(k, str) or not _CTX_KEY_RE.match(k):
                continue
            safe_v = str(v).replace("\n", " ").replace("\r", " ")[:_CTX_MAX_VAL]
            sanitized[k] = safe_v
        if sanitized:
            ctx_lines = "\n".join(f"  {k}: {v}" for k, v in sanitized.items())
            user_text = f"{body.text}\n\n[Home context]\n{ctx_lines}"

    logger.info("chat.request", session_id=body.session_id, text_len=len(user_text))
    await sm.add_message(body.session_id, "user", user_text)

    executed_calls: list[ToolCall] = []
    final_text = ""

    for round_num in range(_MAX_TOOL_ROUNDS + 1):
        messages = await sm.get_messages(body.session_id)

        try:
            text, tool_calls = await llm.chat(messages)
        except RuntimeError as exc:
            logger.error("chat.llm_error", session_id=body.session_id, error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            )

        # ── Pure text response — conversation turn complete ────────────────
        if not tool_calls:
            await sm.add_message(body.session_id, "assistant", text)
            final_text = text
            break

        # ── Tool round limit ───────────────────────────────────────────────
        if round_num >= _MAX_TOOL_ROUNDS:
            logger.warning(
                "chat.tool_round_limit_reached",
                session_id=body.session_id, rounds=round_num,
            )
            await sm.add_message(body.session_id, "assistant", text or "[tool limit]")
            final_text = text or "I've reached the limit of actions I can take at once."
            break

        # ── Store assistant turn with its tool calls ───────────────────────
        await sm.add_message(
            body.session_id,
            "assistant",
            text or "",
            tool_calls=_to_ollama_tool_calls(tool_calls),
        )

        # ── Execute each tool call, collect results ────────────────────────
        for tc in tool_calls:
            result = await ha.execute_tool_call(tc)

            tc.acl_status = "allowed" if result.success else "denied"
            if not result.success:
                tc.acl_reason = result.message

            # Feed the result back as a tool role message
            await sm.add_message(body.session_id, "tool", result.message)

            logger.info(
                "chat.tool_executed",
                session_id=body.session_id,
                tool=tc.function_name,
                entity=tc.arguments.get("entity_id", ""),
                success=result.success,
            )

            executed_calls.append(tc)

        # Loop back — LLM will now see the tool results and produce text

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "chat.response",
        session_id=body.session_id,
        elapsed_ms=elapsed_ms,
        tool_calls_executed=len(executed_calls),
    )

    return ChatResponse(
        session_id=body.session_id,
        text=final_text,
        tool_calls=executed_calls,
        processing_time_ms=elapsed_ms,
        model=llm.model_name,
    )


@router.delete("/chat/{session_id}", dependencies=[Depends(verify_api_key)])
async def clear_session(session_id: str, request: Request) -> dict:
    sm: SessionManager = request.app.state.session_manager
    await sm.clear(session_id)
    return {"cleared": session_id}


@router.get("/chat/sessions/stats", dependencies=[Depends(verify_api_key)])
async def session_stats(request: Request) -> dict:
    sm: SessionManager = request.app.state.session_manager
    return {"active_sessions": sm.active_count()}
