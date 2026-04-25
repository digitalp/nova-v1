"""Mixin for RealtimeVoiceService: session state management, turn lifecycle, adapter resolution."""
from __future__ import annotations
from contextlib import suppress
import json
import asyncio
from typing import Any

import structlog
from fastapi import WebSocket

from avatar_backend.services.voice_types import (
    IDLE,
    VoiceSessionState,
    VoiceTurnContext,
    RealtimeVoiceAdapter,
    DefaultRealtimeVoiceAdapter,
    create_realtime_voice_adapter,
)

_LOGGER = structlog.get_logger()


class VoiceSessionMixin:
    """Session state, turn tracking, adapter resolution — mixed into RealtimeVoiceService."""
    async def _get_or_create_session(self, session_key: str) -> VoiceSessionState:
        async with self._sessions_lock:
            session = self._sessions.get(session_key)
            if session is None:
                session = VoiceSessionState()
                self._sessions[session_key] = session
            return session

    async def _is_current_turn(self, session_key: str, turn_id: int | None) -> bool:
        if turn_id is None:
            return True
        session = await self._get_or_create_session(session_key)
        return session.current_turn_id == turn_id

    async def _clear_completed_task(self, session_key: str, turn_id: int | None) -> None:
        if turn_id is None:
            return
        session = await self._get_or_create_session(session_key)
        if session.current_turn_id == turn_id and session.current_task and session.current_task.done():
            session.current_task = None

    async def _finish_turn(
        self,
        ws: WebSocket,
        session_key: str | None,
        turn_id: int | None,
        reason: str,
    ) -> None:
        if turn_id is None:
            return
        await self._send_json(ws, {
            "type": "turn_finished",
            "reason": reason,
        }, turn_id=turn_id)
        if session_key:
            await self._clear_completed_task(session_key, turn_id)

    async def _cancel_turn_task(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _send_turn_interrupted(self, ws: WebSocket, interrupted_turn_id: int) -> None:
        await self._send_json(ws, {
            "type": "turn_interrupted",
            "interrupted_turn_id": interrupted_turn_id,
        })

    async def _send_json(
        self,
        ws: WebSocket,
        payload: dict[str, Any],
        *,
        turn_id: int | None = None,
    ) -> None:
        if turn_id is not None:
            payload = {**payload, "turn_id": turn_id}
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def _extract_turn_context(self, ws: WebSocket) -> VoiceTurnContext | None:
        app = getattr(ws, "app", None)
        if app is None:
            return None
        state = getattr(app, "state", None)
        if state is None:
            return None
        try:
            return VoiceTurnContext(
                ws=ws,
                ws_mgr=state.ws_manager,
                session_id=getattr(ws, "_nova_session_id"),
                stt=state.stt_service,
                tts=state.tts_service,
                speaker=getattr(state, "speaker_service", None),
                app=app,
                container=getattr(state, "_container", None),
            )
        except AttributeError:
            return None

    def _resolve_adapter(self, ctx: VoiceTurnContext) -> RealtimeVoiceAdapter:
        adapter = getattr(ctx.app.state, "realtime_voice_adapter", None)
        if adapter is None:
            return self._default_adapter
        return adapter

    def _resolve_adapter_for_ws(self, ws: WebSocket) -> RealtimeVoiceAdapter:
        app = getattr(ws, "app", None)
        state = getattr(app, "state", None) if app is not None else None
        adapter = getattr(state, "realtime_voice_adapter", None) if state is not None else None
        if adapter is None:
            return self._default_adapter
        return adapter
