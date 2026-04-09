from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
import structlog

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.models.messages import ChatRequest, ChatResponse
from avatar_backend.services.conversation_service import (
    ConversationTurnRequest,
    EventFollowupRequest,
    PendingEventFollowupContext,
)
from avatar_backend.services.session_manager import SessionManager

router = APIRouter(tags=["chat"])
logger = structlog.get_logger()


class EventFollowupChatRequest(BaseModel):
    session_id: str = Field(..., max_length=128)
    text: str
    event_id: str = Field(..., min_length=1, max_length=64)


@router.post("/chat", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Full tool-execution chat endpoint."""
    llm = request.app.state.llm_service

    if not await llm.is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM not ready — model may still be loading.",
        )

    logger.info("chat.request", session_id=body.session_id, text_len=len(body.text))
    try:
        result = await request.app.state.conversation_service.handle_text_turn(
            ConversationTurnRequest(
                session_id=body.session_id,
                user_text=body.text,
                context=body.context,
            )
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


@router.post("/chat/followup-event", response_model=ChatResponse, dependencies=[Depends(verify_api_key)])
async def chat_followup_event(body: EventFollowupChatRequest, request: Request) -> ChatResponse:
    llm = request.app.state.llm_service

    if not await llm.is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM not ready — model may still be loading.",
        )

    recent_events: dict[str, tuple[float, dict[str, Any]]] = getattr(request.app.state, "recent_event_contexts", {})
    stored = recent_events.get(body.event_id)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown event_id '{body.event_id}'",
        )

    _, event_context = stored
    logger.info("chat.followup_event", session_id=body.session_id, event_id=body.event_id)
    try:
        await request.app.state.conversation_service.set_event_followup_context(
            body.session_id,
            PendingEventFollowupContext(
                event_type=str(event_context.get("event_type", "event")),
                event_summary=str(event_context.get("event_summary", "")) or None,
                event_context=dict(event_context.get("event_context", {})),
            )
        )
        result = await request.app.state.conversation_service.handle_text_turn(
            ConversationTurnRequest(
                session_id=body.session_id,
                user_text=body.text,
            )
        )
    except RuntimeError as exc:
        logger.error("chat.followup_event_error", session_id=body.session_id, event_id=body.event_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
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
