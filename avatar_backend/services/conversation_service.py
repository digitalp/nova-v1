from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from avatar_backend.services.chat_service import ChatResult, run_chat
from avatar_backend.services.context_builder import ContextBuilder


@dataclass
class ConversationTurnRequest:
    session_id: str
    user_text: str
    context: dict[str, Any] | None = None


@dataclass
class EventFollowupRequest:
    session_id: str
    user_text: str
    event_type: str
    event_summary: str | None = None
    event_context: dict[str, Any] | None = None
    followup_prompt: str | None = None


@dataclass
class PendingEventFollowupContext:
    event_type: str
    event_summary: str | None = None
    event_context: dict[str, Any] | None = None
    followup_prompt: str | None = None


@dataclass
class ConversationSessionState:
    home_context: dict[str, str] | None = None
    pending_event_context: PendingEventFollowupContext | None = None
    active_event_context: PendingEventFollowupContext | None = None


class ConversationService:
    """Compatibility-first coordinator for text and voice conversation turns.

    This wraps the existing run_chat orchestration behind a higher-level service
    so voice and chat can converge on one coordinator before deeper V2 refactors.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._context_builder = ContextBuilder()
        self._session_states: dict[str, ConversationSessionState] = {}
        self._state_lock = asyncio.Lock()

    async def handle_text_turn(self, turn: ConversationTurnRequest) -> ChatResult:
        user_text = await self._build_user_text(
            session_id=turn.session_id,
            user_text=turn.user_text,
            context=turn.context,
        )
        return await self._run_turn(
            session_id=turn.session_id,
            user_text=user_text,
        )

    async def handle_voice_turn(self, *, session_id: str, user_text: str) -> ChatResult:
        return await self._run_turn(
            session_id=session_id,
            user_text=await self._build_user_text(session_id=session_id, user_text=user_text),
        )

    async def handle_event_followup(self, turn: EventFollowupRequest) -> ChatResult:
        await self.set_event_followup_context(
            turn.session_id,
            PendingEventFollowupContext(
                event_type=turn.event_type,
                event_summary=turn.event_summary,
                event_context=turn.event_context,
                followup_prompt=turn.followup_prompt,
            ),
        )
        return await self.handle_voice_turn(session_id=turn.session_id, user_text=turn.user_text)

    async def set_event_followup_context(self, session_id: str, context: PendingEventFollowupContext) -> None:
        async with self._state_lock:
            session_state = self._session_states.get(session_id)
            if session_state is None:
                session_state = ConversationSessionState()
                self._session_states[session_id] = session_state
            session_state.pending_event_context = context

    async def clear_event_followup_context(self, session_id: str) -> None:
        async with self._state_lock:
            session_state = self._session_states.get(session_id)
            if session_state is None:
                return
            session_state.pending_event_context = None
            session_state.active_event_context = None
            if session_state.home_context is None:
                self._session_states.pop(session_id, None)

    async def clear_session_state(self, session_id: str) -> None:
        async with self._state_lock:
            self._session_states.pop(session_id, None)
        await self._app.state.session_manager.clear(session_id)

    async def _build_user_text(
        self,
        *,
        session_id: str,
        user_text: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        pending: PendingEventFollowupContext | None = None
        active_event: PendingEventFollowupContext | None = None
        sanitized_context = self._context_builder.sanitize_context(context)
        context_was_provided = context is not None
        async with self._state_lock:
            session_state = self._session_states.get(session_id)
            if session_state is None and (sanitized_context or context_was_provided):
                session_state = ConversationSessionState()
                self._session_states[session_id] = session_state
            if session_state is not None:
                if context_was_provided:
                    if sanitized_context:
                        session_state.home_context = {
                            **(session_state.home_context or {}),
                            **sanitized_context,
                        }
                    else:
                        session_state.home_context = None
                effective_context = session_state.home_context
                pending = session_state.pending_event_context
                active_event = session_state.active_event_context
                session_state.pending_event_context = None
                if pending is not None:
                    session_state.active_event_context = pending
                elif active_event is not None:
                    session_state.active_event_context = None
                if (
                    session_state.home_context is None
                    and session_state.pending_event_context is None
                    and session_state.active_event_context is None
                ):
                    self._session_states.pop(session_id, None)
            else:
                effective_context = sanitized_context or None
        shaped = self._context_builder.build_text_context(user_text, effective_context)
        event_context = pending or active_event
        if not event_context:
            return shaped
        return self._context_builder.build_event_followup_context(
            user_text=shaped,
            event_type=event_context.event_type,
            event_summary=event_context.event_summary,
            event_context=event_context.event_context,
            followup_prompt=event_context.followup_prompt,
        )

    async def _run_turn(self, *, session_id: str, user_text: str) -> ChatResult:
        return await run_chat(
            session_id=session_id,
            user_text=user_text,
            llm=self._app.state.llm_service,
            sm=self._app.state.session_manager,
            ha=self._app.state.ha_proxy,
            decision_log=getattr(self._app.state, "decision_log", None),
            memory_service=getattr(self._app.state, "memory_service", None),
            presence_service=getattr(self._app.state, "presence_service", None),
        )
