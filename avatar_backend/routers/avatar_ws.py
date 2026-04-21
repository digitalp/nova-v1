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

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from avatar_backend.bootstrap.container import AppContainer, get_container
from avatar_backend.middleware.auth import verify_api_key_ws
from avatar_backend.services.action_service import ActionService
from avatar_backend.services.ws_manager import ConnectionManager

_LOGGER = structlog.get_logger()

router = APIRouter()


@router.websocket("/ws/avatar")
async def avatar_state_websocket(
    ws: WebSocket,
    _: None = Depends(verify_api_key_ws),
    container: AppContainer = Depends(get_container),
):
    """
    State-only WebSocket for avatar UI components.
    Joins the broadcast group; state updates arrive automatically
    whenever the voice pipeline changes state.
    """
    ws_mgr: ConnectionManager = container.ws_manager

    await ws_mgr.connect(ws)
    surface_state = await container.surface_state_service.get_snapshot()
    await ws.send_text(json.dumps({"type": "avatar_state", "state": surface_state["avatar_state"]}))
    await ws.send_text(json.dumps({"type": "surface_state", **surface_state}))

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
                    elif data.get("type") == "surface_action":
                        action = str(data.get("action") or "")
                        event_id = str(data.get("event_id") or "").strip()
                        action_service = getattr(container, "action_service", None) or ActionService()
                        ack = await action_service.handle_surface_action(
                            app=ws.app,
                            ws_mgr=ws_mgr,
                            action=action,
                            event_id=event_id,
                            action_payload=data,
                        )
                        await ws.send_text(json.dumps(ack))
                except Exception:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _LOGGER.warning("avatar_ws.error", exc=str(exc))
    finally:
        await ws_mgr.disconnect(ws)
