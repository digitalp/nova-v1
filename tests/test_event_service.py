from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from avatar_backend.services.event_service import EventService, publish_visual_event


def test_event_service_builds_canonical_event_record():
    service = EventService()
    event = service.build_event(
        event_id="evt-1",
        event_type="doorbell",
        title="Doorbell",
        message="Front door live view",
        camera_entity_id="camera.front_door",
        image_url="/static/example-1.png",
        image_urls=["/static/example-2.png"],
        event_context={"source": "doorbell"},
        expires_in_ms=45000,
    )

    assert event.event_id == "evt-1"
    assert event.event_type == "doorbell"
    assert event.title == "Doorbell"
    assert event.camera_entity_id == "camera.front_door"
    assert event.image_urls == ["/static/example-1.png", "/static/example-2.png"]
    assert event.to_surface_payload()["event"] == "doorbell"
    assert event.to_context_payload()["camera_entity_id"] == "camera.front_door"
    assert event.to_context_payload()["source"] == "doorbell"


@pytest.mark.asyncio
async def test_publish_visual_event_records_context_and_surface_payload():
    service = EventService()
    fake_db = SimpleNamespace(insert_event_history=MagicMock())
    fake_event_store = SimpleNamespace(create_event=MagicMock())
    fake_event_bus = SimpleNamespace(publish=AsyncMock())
    app = SimpleNamespace(state=SimpleNamespace(
        recent_event_contexts={},
        metrics_db=fake_db,
        event_store=fake_event_store,
        event_bus=fake_event_bus,
    ))
    ws_mgr = SimpleNamespace(broadcast_to_voice_json=AsyncMock())
    surface_state = SimpleNamespace(record_visual_event=AsyncMock())

    event = await publish_visual_event(
        app=app,
        ws_mgr=ws_mgr,
        event_service=service,
        surface_state=surface_state,
        event_id="evt-2",
        event_type="related_camera",
        title="Driveway",
        message="Driveway live view",
        camera_entity_id="camera.driveway",
        event_context={"source": "surface_action"},
        expires_in_ms=45000,
    )

    assert event.event_id == "evt-2"
    assert app.state.recent_event_contexts["evt-2"][1]["event_type"] == "related_camera"
    surface_state.record_visual_event.assert_awaited_once()
    ws_mgr.broadcast_to_voice_json.assert_awaited_once()
    payload = surface_state.record_visual_event.await_args.args[1]
    assert payload["type"] == "visual_event"
    assert payload["camera_entity_id"] == "camera.driveway"
    fake_db.insert_event_history.assert_called_once()
    fake_event_store.create_event.assert_called_once()
    fake_event_bus.publish.assert_awaited_once()
    published = fake_event_bus.publish.await_args.args[0]
    assert published.event_id == "evt-2"
    assert published.event_type == "related_camera"
