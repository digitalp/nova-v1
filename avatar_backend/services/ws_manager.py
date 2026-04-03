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
from typing import Any

import structlog
from fastapi import WebSocket

_LOGGER = structlog.get_logger()


class ConnectionManager:
    """Thread-safe registry of active WebSocket connections with broadcast support."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()        # /ws/avatar
        self._voice_connections: set[WebSocket] = set()  # /ws/voice
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
        async with self._lock:
            self._voice_connections.add(ws)
        _LOGGER.info("ws.voice_connected", total=len(self._voice_connections))

    async def disconnect_voice(self, ws: WebSocket) -> None:
        async with self._lock:
            self._voice_connections.discard(ws)
        _LOGGER.info("ws.voice_disconnected", total=len(self._voice_connections))

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
