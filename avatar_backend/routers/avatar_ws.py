"""
Avatar state WebSocket endpoint — /ws/avatar

Read-only WebSocket that streams avatar state change events.
Used by the Lovelace avatar card and any other UI component that
wants to reflect the current avatar state without being part of
the voice pipeline.

Protocol
--------
Server → Client (text, JSON):
  {"type": "avatar_state", "state": "<idle|listening|thinking|speaking|alert|error>"}
  {"type": "pong"}

Client → Server (text, JSON):
  {"type": "ping"}   (keepalive — server replies with pong)

Authentication:
  ?api_key=<key>  query parameter
"""
from __future__ import annotations
import json
import logging

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from avatar_backend.middleware.auth import verify_api_key_ws
from avatar_backend.services.ws_manager import ConnectionManager

_LOGGER = structlog.get_logger()

router = APIRouter()


@router.websocket("/ws/avatar")
async def avatar_state_websocket(
    ws: WebSocket,
    _: None = Depends(verify_api_key_ws),
):
    """
    State-only WebSocket for avatar UI components.
    Joins the broadcast group; state updates arrive automatically
    whenever the voice pipeline changes state.
    """
    ws_mgr: ConnectionManager = ws.app.state.ws_manager

    await ws_mgr.connect(ws)
    # Send current/initial state so the card renders immediately
    await ws.send_text(json.dumps({"type": "avatar_state", "state": "idle"}))

    try:
        while True:
            msg = await ws.receive()

            if msg["type"] == "websocket.disconnect":
                break

            if msg.get("text"):
                try:
                    data = json.loads(msg["text"])
                    if data.get("type") == "ping":
                        await ws.send_text(json.dumps({"type": "pong"}))
                except Exception:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _LOGGER.warning("avatar_ws.error", exc=str(exc))
    finally:
        await ws_mgr.disconnect(ws)
