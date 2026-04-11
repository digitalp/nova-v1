import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.routers.avatar_ws import avatar_state_websocket
from avatar_backend.services.action_service import ActionService
from avatar_backend.services.event_service import EventService
from avatar_backend.services.surface_state_service import SurfaceStateService
from avatar_backend.services.ws_manager import ConnectionManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_texts: list[str] = []
        self.app = SimpleNamespace(state=SimpleNamespace())
        self._messages = [{"type": "websocket.disconnect"}]

    async def send_text(self, text: str) -> None:
        self.sent_texts.append(text)

    async def receive(self) -> dict:
        return self._messages.pop(0)


class FakeHAProxy:
    def resolve_camera_entity(self, entity_id: str) -> str:
        if entity_id == "camera.outdoor_2":
            return "camera.rlc_1224a_fluent"
        return entity_id


@pytest.mark.asyncio
async def test_avatar_ws_sends_initial_surface_snapshot():
    ws = FakeWebSocket()
    ws.app.state.ws_manager = ConnectionManager()
    ws.app.state.ws_manager.connect = AsyncMock()
    ws.app.state.ws_manager.disconnect = AsyncMock()
    surface = SurfaceStateService()
    ws.app.state.surface_state_service = surface
    await surface.record_visual_event(
        ws.app.state.ws_manager,
        {
            "event_id": "evt-1",
            "event": "doorbell",
            "title": "Doorbell",
            "message": "Front door live view",
        },
    )

    await avatar_state_websocket(ws, None)

    first = json.loads(ws.sent_texts[0])
    second = json.loads(ws.sent_texts[1])
    assert first == {"type": "avatar_state", "state": "idle"}
    assert second["type"] == "surface_state"
    assert second["active_event"]["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_avatar_ws_handles_surface_actions():
    ws = FakeWebSocket()
    ws._messages = [
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "acknowledge_active_event"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "resolve_active_event"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "dismiss_active_event"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "acknowledge_recent_event", "event_id": "evt-1"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "dismiss_recent_event", "event_id": "evt-1"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "resolve_recent_event", "event_id": "evt-1"})},
        {"type": "websocket.receive", "text": json.dumps({"type": "surface_action", "action": "activate_recent_event", "event_id": "evt-1"})},
        {"type": "websocket.disconnect"},
    ]
    ws.app.state.ws_manager = ConnectionManager()
    ws.app.state.ws_manager.connect = AsyncMock()
    ws.app.state.ws_manager.disconnect = AsyncMock()
    ws.app.state.ha_proxy = FakeHAProxy()
    ws.app.state.event_service = EventService()
    ws.app.state.action_service = ActionService()
    surface = SurfaceStateService()
    ws.app.state.surface_state_service = surface
    ws.app.state.recent_event_contexts = {}
    await surface.record_visual_event(
        ws.app.state.ws_manager,
        {
            "event_id": "evt-1",
            "event": "doorbell",
            "title": "Doorbell",
            "message": "Front door live view",
        },
    )

    await avatar_state_websocket(ws, None)

    payloads = [json.loads(text) for text in ws.sent_texts]
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "acknowledge_active_event" for p in payloads)
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "resolve_active_event" for p in payloads)
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "dismiss_active_event" for p in payloads)
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "acknowledge_recent_event" and p.get("ok") is True for p in payloads)
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "dismiss_recent_event" and p.get("ok") is True for p in payloads)
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "resolve_recent_event" and p.get("ok") is True for p in payloads)
    assert any(p.get("type") == "surface_action_ack" and p.get("action") == "activate_recent_event" and p.get("ok") is True for p in payloads)


@pytest.mark.asyncio
async def test_avatar_ws_can_open_related_camera_event():
    ws = FakeWebSocket()
    ws._messages = [
        {"type": "websocket.receive", "text": json.dumps({
            "type": "surface_action",
            "action": "show_related_camera",
            "event_id": "evt-1",
            "target_camera_entity_id": "camera.outdoor_2",
            "target_event": "related_camera",
            "target_title": "Driveway",
            "target_message": "Driveway live view",
        })},
        {"type": "websocket.disconnect"},
    ]
    ws.app.state.ws_manager = ConnectionManager()
    ws.app.state.ws_manager.connect = AsyncMock()
    ws.app.state.ws_manager.disconnect = AsyncMock()
    ws.app.state.ha_proxy = FakeHAProxy()
    ws.app.state.action_service = ActionService()
    surface = SurfaceStateService()
    ws.app.state.surface_state_service = surface
    ws.app.state.recent_event_contexts = {}
    await surface.record_visual_event(
        ws.app.state.ws_manager,
        {
            "event_id": "evt-1",
            "event": "doorbell",
            "title": "Doorbell",
            "message": "Front door live view",
        },
    )

    await avatar_state_websocket(ws, None)

    payloads = [json.loads(text) for text in ws.sent_texts]
    related_ack = next(p for p in payloads if p.get("type") == "surface_action_ack" and p.get("action") == "show_related_camera")
    assert related_ack["ok"] is True

    snapshot = await surface.get_snapshot()
    assert snapshot["active_event"]["title"] == "Driveway"
    assert snapshot["active_event"]["event"] == "related_camera"
    assert snapshot["active_event"]["camera_entity_id"] == "camera.rlc_1224a_fluent"
    assert related_ack["opened_event_id"] in ws.app.state.recent_event_contexts
