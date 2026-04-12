"""
WebSocket connection manager.

Tracks two pools of WebSocket connections:
  - _connections:       /ws/avatar  — state-only clients (Lovelace card, etc.)
  - _voice_connections: /ws/voice   — full-duplex browser voice clients

The announce endpoint uses the voice pool to push audio + subtitles
directly to the browser without a user initiating a conversation.
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any

import structlog
from fastapi import WebSocket

_LOGGER = structlog.get_logger()


class ConnectionManager:
    """Thread-safe registry of active WebSocket connections with broadcast support."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()        # /ws/avatar
        self._voice_connections: set[WebSocket] = set()  # /ws/voice
        self._voice_meta: dict[int, dict] = {}           # id(ws) → session metadata
        self._lock = asyncio.Lock()

    # ── /ws/avatar clients ────────────────────────────────────────────────────

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        _LOGGER.info("ws.client_connected", total=len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)
        _LOGGER.info("ws.client_disconnected", total=len(self._connections))

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        """Send a JSON message to every /ws/avatar client."""
        await self._send_json_to(self._connections, payload)

    # ── /ws/voice clients ─────────────────────────────────────────────────────

    async def connect_voice(self, ws: WebSocket) -> None:
        session_id = getattr(ws, "_nova_session_id", "unknown")
        client_host = (ws.client.host if ws.client else None) or "unknown"
        user_agent = ws.headers.get("user-agent", "")
        meta = {
            "session_id":   session_id,
            "remote_addr":  client_host,
            "user_agent":   user_agent,
            "connected_at": time.time(),
            "message_count": 0,
        }
        async with self._lock:
            self._voice_connections.add(ws)
            self._voice_meta[id(ws)] = meta
        _LOGGER.info("ws.voice_connected", total=len(self._voice_connections))

    async def disconnect_voice(self, ws: WebSocket) -> None:
        async with self._lock:
            self._voice_connections.discard(ws)
            self._voice_meta.pop(id(ws), None)
        _LOGGER.info("ws.voice_disconnected", total=len(self._voice_connections))

    def increment_message_count(self, ws: WebSocket) -> None:
        """Increment the turn counter for a connected voice session."""
        meta = self._voice_meta.get(id(ws))
        if meta is not None:
            meta["message_count"] += 1

    def list_voice_sessions(self) -> list[dict]:
        """Return a snapshot of active avatar (voice WS) sessions."""
        now = time.time()
        result = []
        for ws_id, meta in list(self._voice_meta.items()):
            result.append({
                "session_id":        meta["session_id"],
                "connected_seconds": round(now - meta["connected_at"]),
                "message_count":     meta["message_count"],
                "metadata": {
                    "host":       meta["remote_addr"],
                    "user_agent": meta["user_agent"],
                },
            })
        return result

    async def broadcast_to_voice_json(self, payload: dict[str, Any]) -> None:
        """Send a JSON message to every connected voice (browser) client."""
        await self._send_json_to(self._voice_connections, payload)

    async def broadcast_to_voice_bytes(self, data: bytes) -> None:
        """Send binary data (WAV audio) to every connected voice (browser) client."""
        if not self._voice_connections:
            return
        async with self._lock:
            snapshot = list(self._voice_connections)
        dead: list[WebSocket] = []
        for ws in snapshot:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._voice_connections.discard(ws)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _send_json_to(self, pool: set[WebSocket], payload: dict[str, Any]) -> None:
        if not pool:
            return
        message = json.dumps(payload)
        async with self._lock:
            snapshot = list(pool)
        dead: list[WebSocket] = []
        for ws in snapshot:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    pool.discard(ws)
            _LOGGER.info("ws.pruned_dead_connections", count=len(dead))

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def voice_connection_count(self) -> int:
        return len(self._voice_connections)
