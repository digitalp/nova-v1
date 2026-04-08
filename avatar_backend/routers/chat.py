from __future__ import annotations
import re
from fastapi import APIRouter, Depends, HTTPException, Request, status
import structlog

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.models.messages import ChatRequest, ChatResponse
from avatar_backend.services.chat_service import run_chat

router = APIRouter(tags=["chat"])
logger = structlog.get_logger()


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Full tool-execution chat endpoint."""
    llm = request.app.state.llm_service

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
    try:
        result = await run_chat(
            session_id=body.session_id,
            user_text=user_text,
            llm=request.app.state.llm_service,
            sm=request.app.state.session_manager,
            ha=request.app.state.ha_proxy,
            decision_log=getattr(request.app.state, "decision_log", None),
            memory_service=getattr(request.app.state, "memory_service", None),
        )
    except RuntimeError as exc:
        logger.error("chat.llm_error", session_id=body.session_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    logger.info(
        "chat.response",
        session_id=body.session_id,
        elapsed_ms=result.processing_time_ms,
        tool_calls_executed=len(result.tool_calls),
    )

    return ChatResponse(
        session_id=result.session_id,
        text=result.text,
        tool_calls=result.tool_calls,
        processing_time_ms=result.processing_time_ms,
        model=result.model,
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
