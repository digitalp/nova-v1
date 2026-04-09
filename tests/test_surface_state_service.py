from unittest.mock import AsyncMock, MagicMock

import pytest

from avatar_backend.services.surface_state_service import SurfaceStateService
from avatar_backend.services.ws_manager import ConnectionManager


@pytest.mark.asyncio
async def test_surface_state_tracks_avatar_state_and_events():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.set_avatar_state(ws_mgr, "thinking")
    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
        "camera_entity_id": "camera.front_door",
        "image_urls": ["/static/example.png"],
        "expires_in_ms": 45000,
    })

    snapshot = await service.get_snapshot()

    assert snapshot["avatar_state"] == "thinking"
    assert snapshot["active_event"]["event_id"] == "evt-1"
    assert snapshot["active_event"]["image_urls"] == ["/static/example.png"]
    assert snapshot["active_event"]["status"] == "active"
    assert snapshot["active_event"]["open_loop_note"] == "Needs attention"
    assert snapshot["active_event"]["open_loop_state"] == "active"
    assert snapshot["active_event"]["open_loop_active"] is True
    assert [item["action"] for item in snapshot["active_event"]["suggested_actions"]] == [
        "ask_about_event",
        "show_related_camera",
        "ask_about_event",
        "acknowledge_active_event",
        "snooze_active_event",
        "dismiss_active_event",
        "resolve_active_event",
    ]
    assert snapshot["recent_events"][0]["event"] == "doorbell"
    surface_payloads = [
        call.args[0]
        for call in ws_mgr.broadcast_to_voice_json.await_args_list
        if call.args and call.args[0].get("type") == "surface_state"
    ]
    assert surface_payloads
    assert surface_payloads[-1]["active_event"]["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_surface_state_can_dismiss_and_reactivate_recent_event():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.dismiss_active_event(ws_mgr)

    snapshot = await service.get_snapshot()
    assert snapshot["active_event"] is None
    assert snapshot["recent_events"][0]["event_id"] == "evt-1"
    assert snapshot["recent_events"][0]["status"] == "dismissed"
    assert snapshot["recent_events"][0]["open_loop_state"] == "dismissed"
    assert snapshot["recent_events"][0]["open_loop_active"] is True

    ok = await service.activate_recent_event(ws_mgr, "evt-1")
    snapshot = await service.get_snapshot()
    assert ok is True
    assert snapshot["active_event"]["event_id"] == "evt-1"
    assert snapshot["active_event"]["status"] == "active"


@pytest.mark.asyncio
async def test_surface_state_can_acknowledge_active_event():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.acknowledge_active_event(ws_mgr)

    snapshot = await service.get_snapshot()
    assert snapshot["active_event"]["status"] == "acknowledged"
    assert snapshot["recent_events"][0]["status"] == "acknowledged"
    assert snapshot["recent_events"][0]["open_loop_note"] == "Seen by user"
    assert snapshot["recent_events"][0]["open_loop_state"] == "acknowledged"
    assert snapshot["recent_events"][0]["open_loop_active"] is True


@pytest.mark.asyncio
async def test_surface_state_can_resolve_active_event():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.resolve_active_event(ws_mgr)

    snapshot = await service.get_snapshot()
    assert snapshot["active_event"] is None
    assert snapshot["recent_events"][0]["status"] == "resolved"
    assert snapshot["recent_events"][0]["open_loop_note"] == "Closed out"
    assert snapshot["recent_events"][0]["open_loop_state"] == "resolved"
    assert snapshot["recent_events"][0]["open_loop_active"] is False
    assert snapshot["recent_events"][0]["open_loop_resolved_ts"]


@pytest.mark.asyncio
async def test_surface_state_can_update_recent_event_status_without_reopening():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.dismiss_active_event(ws_mgr)

    ok = await service.acknowledge_recent_event(ws_mgr, "evt-1")
    snapshot = await service.get_snapshot()
    assert ok is True
    assert snapshot["active_event"] is None
    assert snapshot["recent_events"][0]["status"] == "acknowledged"
    assert snapshot["recent_events"][0]["open_loop_note"] == "Seen by user"

    ok = await service.dismiss_recent_event(ws_mgr, "evt-1")
    snapshot = await service.get_snapshot()
    assert ok is True
    assert snapshot["recent_events"][0]["status"] == "dismissed"
    assert snapshot["recent_events"][0]["open_loop_note"] == "Hidden for now"

    ok = await service.resolve_recent_event(ws_mgr, "evt-1")
    snapshot = await service.get_snapshot()
    assert ok is True
    assert snapshot["recent_events"][0]["status"] == "resolved"
    assert snapshot["recent_events"][0]["open_loop_note"] == "Closed out"


@pytest.mark.asyncio
async def test_surface_state_restores_attention_note_when_reactivated():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.acknowledge_recent_event(ws_mgr, "evt-1")
    ok = await service.activate_recent_event(ws_mgr, "evt-1")

    snapshot = await service.get_snapshot()
    assert ok is True
    assert snapshot["active_event"]["status"] == "active"
    assert snapshot["active_event"]["open_loop_note"] == "Needs attention"


@pytest.mark.asyncio
async def test_surface_state_recent_actions_change_with_status():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.dismiss_active_event(ws_mgr)

    snapshot = await service.get_snapshot()
    assert [item["action"] for item in snapshot["recent_events"][0]["suggested_actions"]] == ["activate_recent_event"]

    await service.activate_recent_event(ws_mgr, "evt-1")
    await service.acknowledge_active_event(ws_mgr)
    snapshot = await service.get_snapshot()
    assert [item["action"] for item in snapshot["recent_events"][0]["suggested_actions"]] == [
        "ask_about_event",
        "show_related_camera",
        "ask_about_event",
        "snooze_recent_event",
        "dismiss_recent_event",
        "resolve_recent_event",
    ]


@pytest.mark.asyncio
async def test_surface_state_can_snooze_and_unsnooze_event():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-1",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.snooze_active_event(ws_mgr)

    snapshot = await service.get_snapshot()
    assert snapshot["active_event"] is None
    assert snapshot["recent_events"][0]["status"] == "snoozed"
    assert snapshot["recent_events"][0]["open_loop_note"] == "Snoozed for 30 minutes"
    assert [item["action"] for item in snapshot["recent_events"][0]["suggested_actions"]] == ["activate_recent_event"]

    ok = await service.activate_recent_event(ws_mgr, "evt-1")
    snapshot = await service.get_snapshot()
    assert ok is True
    assert snapshot["active_event"]["status"] == "active"
    assert snapshot["active_event"]["open_loop_note"] == "Needs attention"


@pytest.mark.asyncio
async def test_surface_state_uses_domain_aware_followup_labels_without_schema_lockin():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-vehicle",
        "event": "motion",
        "title": "Outdoor Motion",
        "message": "A grey car is moving in the driveway area.",
    })
    snapshot = await service.get_snapshot()
    assert snapshot["active_event"]["suggested_actions"][0]["label"] == "Ask about the vehicle"
    assert snapshot["active_event"]["suggested_actions"][1]["action"] == "show_related_camera"
    assert snapshot["recent_events"][0]["suggested_actions"][0]["label"] == "Ask about the vehicle"

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-generic",
        "event": "visual",
        "title": "New Event",
        "message": "Something changed nearby.",
    })
    snapshot = await service.get_snapshot()
    assert snapshot["active_event"]["suggested_actions"][0]["label"] == "Ask about this"


@pytest.mark.asyncio
async def test_surface_state_recent_unresolved_events_keep_secondary_followup_actions():
    service = SurfaceStateService(max_recent_events=4)
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()

    await service.record_visual_event(ws_mgr, {
        "event_id": "evt-door",
        "event": "doorbell",
        "title": "Doorbell",
        "message": "Front door live view",
    })
    await service.acknowledge_active_event(ws_mgr)

    snapshot = await service.get_snapshot()
    labels = [item["label"] for item in snapshot["recent_events"][0]["suggested_actions"]]
    assert labels[:3] == ["Ask who is there", "Show driveway too", "Ask if it is a delivery"]
