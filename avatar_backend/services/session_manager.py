from __future__ import annotations
import asyncio
import time
from typing import Any
import structlog

logger = structlog.get_logger()

_MAX_HISTORY  = 20
_SESSION_TTL  = 3600


class Session:
    def __init__(self, session_id: str, system_prompt: str) -> None:
        self.id = session_id
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self.created_at = time.time()
        self.last_used  = time.time()

    def add_message(
        self,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """
        Append a message to the conversation history.
        tool_calls should be in Ollama wire format:
            [{"function": {"name": "...", "arguments": {...}}}]
        This is used for assistant messages that include tool invocations.
        """
        msg: dict[str, Any] = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)
        self.last_used = time.time()

        # Rolling window — always preserve system prompt at index 0
        if len(self.messages) > _MAX_HISTORY:
            self.messages = [self.messages[0]] + self.messages[-(_MAX_HISTORY - 1):]

    def get_messages(self) -> list[dict[str, Any]]:
        self.last_used = time.time()
        return list(self.messages)

    def is_expired(self) -> bool:
        return (time.time() - self.last_used) > _SESSION_TTL

    def message_count(self) -> int:
        return len(self.messages) - 1   # exclude system prompt


class SessionManager:
    def __init__(self, system_prompt: str) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._system_prompt = system_prompt
        logger.info("session_manager.initialized")

    async def get_or_create(self, session_id: str) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.is_expired():
                if session is not None:
                    logger.info("session.expired", session_id=session_id)
                session = Session(session_id, self._system_prompt)
                self._sessions[session_id] = session
                logger.info("session.created", session_id=session_id)
            return session

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
    ) -> None:
        session = await self.get_or_create(session_id)
        async with self._lock:
            session.add_message(role, content, tool_calls=tool_calls)
            logger.debug(
                "session.message_added",
                session_id=session_id, role=role,
                has_tool_calls=bool(tool_calls),
                total=session.message_count(),
            )

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        session = await self.get_or_create(session_id)
        return session.get_messages()

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.info("session.cleared", session_id=session_id)

    async def cleanup_expired(self) -> int:
        async with self._lock:
            expired = [sid for sid, s in self._sessions.items() if s.is_expired()]
            for sid in expired:
                del self._sessions[sid]
        if expired:
            logger.info("session.cleanup_done", removed=len(expired))
        return len(expired)

    def active_count(self) -> int:
        return len(self._sessions)
