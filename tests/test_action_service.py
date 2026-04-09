from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services.action_service import ActionService
from avatar_backend.services.conversation_service import ConversationService
from avatar_backend.services.event_service import EventService
from avatar_backend.services.surface_state_service import SurfaceStateService
from avatar_backend.services.ws_manager import ConnectionManager


class _FakeHAProxy:
    def resolve_camera_entity(self, entity_id: str) -> str:
        if entity_id == "camera.outdoor_2":
            return "camera.rlc_1224a_fluent"
        return entity_id


class _FakeMetricsDB:
    def __init__(self) -> None:
        self.updated: list[tuple[str, str, str | None, str | None]] = []
        self.inserted: list[dict] = []
        self.policy_updates: list[tuple[str, bool, str | None]] = []

    def update_event_history_status(self, event_id: str, status: str, open_loop_note: str | None, admin_note: str | None) -> bool:
        self.updated.append((event_id, status, open_loop_note, admin_note))
        return False

    def insert_event_history(self, entry: dict) -> None:
        self.inserted.append(entry)

    def update_event_history_policy(self, event_id: str, *, reminder_sent: bool = False, escalation_level: str | None = None) -> bool:
        self.policy_updates.append((event_id, reminder_sent, escalation_level))
        return True


class _FakeEventStore:
    def __init__(self) -> None:
        self.updated: list[dict] = []
        self.created: list[dict] = []
        self.recorded_actions: list[dict] = []

    def update_status(
        self,
        event_id: str,
        *,
        status: str,
        open_loop_note: str | None = None,
        admin_note: str | None = None,
        reminder_sent: bool = False,
        escalation_level: str | None = None,
    ) -> dict | None:
        entry = {
            "event_id": event_id,
            "status": status,
            "open_loop_note": open_loop_note,
            "admin_note": admin_note,
            "reminder_sent": reminder_sent,
            "escalation_level": escalation_level,
        }
        self.updated.append(entry)
        if len(self.updated) == 1:
            return None
        return entry

    def create_event(self, event: dict) -> dict:
        self.created.append(event)
        return event

    def record_action(
        self,
        *,
        event_id: str,
        action_id: str,
        action_type: str,
        status: str = "completed",
        result: dict | None = None,
    ) -> list[dict]:
        entry = {
            "event_id": event_id,
            "action_id": action_id,
            "action_type": action_type,
            "status": status,
            "result": result or {},
        }
        self.recorded_actions.append(entry)
        return [entry]


@pytest.mark.asyncio
async def test_action_service_executes_recent_surface_transition():
    action_service = ActionService()
    ws_mgr = ConnectionManager()
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()
    surface_state = SurfaceStateService(action_service=action_service)

    await surface_state.record_visual_event(
        ws_mgr,
        {
            "event_id": "evt-1",
            "event": "package_delivery",
            "title": "Package",
            "message": "Parcel at the front door",
        },
    )

    app = SimpleNamespace(state=SimpleNamespace(surface_state_service=surface_state))
    ack = await action_service.handle_surface_action(
        app=app,
        ws_mgr=ws_mgr,
        action="acknowledge_recent_event",
        event_id="evt-1",
    )

    assert ack == {
        "type": "surface_action_ack",
        "action": "acknowledge_recent_event",
        "event_id": "evt-1",
        "ok": True,
    }
    snapshot = await surface_state.get_snapshot()
    assert snapshot["recent_events"][0]["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_action_service_can_open_related_camera_event():
    action_service = ActionService()
    ws_mgr = ConnectionManager()
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()
    surface_state = SurfaceStateService(action_service=action_service)
    await surface_state.record_visual_event(
        ws_mgr,
        {
            "event_id": "evt-1",
            "event": "doorbell",
            "title": "Doorbell",
            "message": "Front door live view",
        },
    )

    app = SimpleNamespace(state=SimpleNamespace(
        surface_state_service=surface_state,
        event_service=EventService(),
        ha_proxy=_FakeHAProxy(),
        recent_event_contexts={},
    ))
    ack = await action_service.handle_surface_action(
        app=app,
        ws_mgr=ws_mgr,
        action="show_related_camera",
        event_id="evt-1",
        action_payload={
            "target_camera_entity_id": "camera.outdoor_2",
            "target_event": "related_camera",
            "target_title": "Driveway",
            "target_message": "Driveway live view",
        },
    )

    assert ack["type"] == "surface_action_ack"
    assert ack["action"] == "show_related_camera"
    assert ack["event_id"] == "evt-1"
    assert ack["ok"] is True
    assert ack["opened_event_id"] in app.state.recent_event_contexts

    snapshot = await surface_state.get_snapshot()
    assert snapshot["active_event"]["title"] == "Driveway"
    assert snapshot["active_event"]["camera_entity_id"] == "camera.rlc_1224a_fluent"


@pytest.mark.asyncio
async def test_action_service_handles_event_history_actions_across_db_and_surface():
    action_service = ActionService()
    ws_mgr = ConnectionManager()
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()
    surface_state = SurfaceStateService(action_service=action_service)
    metrics_db = _FakeMetricsDB()
    event_store = _FakeEventStore()

    await surface_state.record_visual_event(
        ws_mgr,
        {
            "event_id": "evt-1",
            "event": "doorbell",
            "title": "Doorbell",
            "message": "Front door live view",
        },
    )

    app = SimpleNamespace(state=SimpleNamespace(
        surface_state_service=surface_state,
        metrics_db=metrics_db,
        event_store=event_store,
    ))
    result = await action_service.handle_event_history_action(
        app=app,
        ws_mgr=ws_mgr,
        event_id="evt-1",
        status="acknowledged",
        title="Doorbell",
        summary="Front door live view",
        event_type="doorbell",
        event_source="doorbell",
        camera_entity_id="camera.front_door",
        open_loop_note="Seen by admin",
        admin_note="Reviewed",
        reminder_sent=True,
        escalation_level="medium",
    )

    assert result == {
        "ok": True,
        "event_id": "evt-1",
        "status": "acknowledged",
        "workflow_action": None,
        "reminder_sent": True,
        "escalation_level": "medium",
        "persisted": True,
        "surface_updated": True,
    }
    assert metrics_db.updated == [("evt-1", "acknowledged", "Seen by admin", "Reviewed")]
    assert metrics_db.inserted[0]["event_id"] == "evt-1"
    assert metrics_db.policy_updates == [("evt-1", True, "medium")]
    assert event_store.created[0]["event_id"] == "evt-1"
    assert event_store.updated[-1]["status"] == "acknowledged"
    assert event_store.updated[-1]["escalation_level"] == "medium"
    assert event_store.recorded_actions[-1]["action_type"] == "set_status:acknowledged"

    snapshot = await surface_state.get_snapshot()
    assert snapshot["active_event"]["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_action_service_can_apply_open_loop_workflow_actions():
    action_service = ActionService()
    ws_mgr = ConnectionManager()
    ws_mgr.broadcast_json = AsyncMock()
    ws_mgr.broadcast_to_voice_json = AsyncMock()
    surface_state = SurfaceStateService(action_service=action_service)
    metrics_db = _FakeMetricsDB()
    event_store = _FakeEventStore()

    await surface_state.record_visual_event(
        ws_mgr,
        {
            "event_id": "evt-2",
            "event": "driveway_vehicle",
            "title": "Driveway vehicle",
            "message": "Vehicle still unresolved",
        },
    )

    app = SimpleNamespace(state=SimpleNamespace(
        surface_state_service=surface_state,
        metrics_db=metrics_db,
        event_store=event_store,
    ))
    result = await action_service.handle_event_history_action(
        app=app,
        ws_mgr=ws_mgr,
        event_id="evt-2",
        status="active",
        workflow_action="send_reminder",
        title="Driveway vehicle",
        summary="Vehicle still unresolved",
        event_type="driveway_vehicle",
        event_source="driveway",
        camera_entity_id="camera.driveway",
        open_loop_note="Reminder sent for follow-up",
    )

    assert result == {
        "ok": True,
        "event_id": "evt-2",
        "status": "active",
        "workflow_action": "send_reminder",
        "reminder_sent": True,
        "escalation_level": None,
        "persisted": True,
        "surface_updated": True,
    }
    assert metrics_db.updated == [("evt-2", "active", "Reminder sent for follow-up", None)]
    assert metrics_db.policy_updates == [("evt-2", True, None)]
    assert event_store.created[0]["event_id"] == "evt-2"
    assert event_store.updated[-1]["reminder_sent"] is True
    assert event_store.recorded_actions[-1]["action_type"] == "send_reminder"

    snapshot = await surface_state.get_snapshot()
    event = snapshot["active_event"]
    assert event["open_loop_note"] == "Reminder sent for follow-up"
    assert event["open_loop_reminder_count"] == 1


@pytest.mark.asyncio
async def test_action_service_handles_event_history_followup_action():
    action_service = ActionService()
    conversation = SimpleNamespace(handle_event_followup=AsyncMock(return_value=SimpleNamespace(
        text="This looks like a routine driveway arrival.",
        session_id="admin_event_history",
        processing_time_ms=42,
    )))
    app = SimpleNamespace(state=SimpleNamespace(conversation_service=conversation))

    result = await action_service.handle_event_history_domain_action(
        app=app,
        ws_mgr=None,
        session_id="admin_event_history",
        event_id="evt-3",
        action="ask_about_event",
        title="Driveway vehicle",
        summary="Vehicle still unresolved",
        event_type="driveway_vehicle",
        event_source="driveway",
        camera_entity_id="camera.driveway",
        followup_prompt="Focus on whether the vehicle looks expected.",
    )

    assert result == {
        "ok": True,
        "action": "ask_about_event",
        "event_id": "evt-3",
        "text": "This looks like a routine driveway arrival.",
        "session_id": "admin_event_history",
        "processing_time_ms": 42,
    }
    conversation.handle_event_followup.assert_awaited()
