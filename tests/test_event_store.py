from __future__ import annotations

from avatar_backend.models.events import EventEnvelope
from avatar_backend.services.event_store import EventStoreService
from avatar_backend.services.metrics_db import MetricsDB


def test_event_store_persists_event_records_across_reopen(tmp_path):
    db_path = tmp_path / "metrics.db"
    store = EventStoreService(MetricsDB(path=db_path))
    store.create_event(
        EventEnvelope(
            event_id="evt-1",
            event_type="doorbell",
            source="doorbell",
            summary="Front door live view",
            camera_entity_id="camera.front_door",
            confidence=0.88,
        )
    )

    reopened = EventStoreService(MetricsDB(path=db_path))
    event = reopened.get_event("evt-1")

    assert event is not None
    assert event["event_type"] == "doorbell"
    assert event["camera_entity_id"] == "camera.front_door"
    assert event["confidence"] == 0.88


def test_event_store_lists_by_type_and_date(tmp_path):
    db_path = tmp_path / "metrics.db"
    store = EventStoreService(MetricsDB(path=db_path))
    store.create_event(
        {
            "event_id": "evt-1",
            "event_type": "doorbell",
            "source": "doorbell",
            "summary": "Front door live view",
            "created_at": "2026-04-09T08:00:00+00:00",
        }
    )
    store.create_event(
        {
            "event_id": "evt-2",
            "event_type": "package_delivery",
            "source": "doorbell",
            "summary": "Package at the door",
            "created_at": "2026-04-09T09:00:00+00:00",
        }
    )

    filtered = store.list_events(
        event_type="package_delivery",
        created_after="2026-04-09T08:30:00+00:00",
    )

    assert [item["event_id"] for item in filtered] == ["evt-2"]


def test_event_store_updates_status_and_preserves_open_loop_metadata(tmp_path):
    db_path = tmp_path / "metrics.db"
    store = EventStoreService(MetricsDB(path=db_path))
    store.create_event(
        {
            "event_id": "evt-3",
            "event_type": "driveway_vehicle",
            "source": "driveway",
            "summary": "Vehicle still outside",
            "created_at": "2026-04-09T08:00:00+00:00",
            "data": {"open_loop_note": "Needs attention"},
        }
    )

    updated = store.update_status("evt-3", status="resolved", open_loop_note="Closed out", admin_note="Handled")

    assert updated is not None
    assert updated["status"] == "resolved"
    assert updated["data"]["open_loop_state"] == "resolved"
    assert updated["data"]["open_loop_active"] is False
    assert updated["data"]["admin_note"] == "Handled"


def test_event_store_records_actions_and_media(tmp_path):
    db_path = tmp_path / "metrics.db"
    db = MetricsDB(path=db_path)
    store = EventStoreService(db)
    store.create_event({"event_id": "evt-4", "event_type": "doorbell"})

    actions = store.record_action(
        event_id="evt-4",
        action_id="act-1",
        action_type="acknowledge",
        result={"ok": True},
    )
    media = store.add_media(
        event_id="evt-4",
        media_type="image",
        url="/static/example.png",
        metadata={"camera_entity_id": "camera.front_door"},
    )

    assert actions[0]["action_type"] == "acknowledge"
    assert actions[0]["result"]["ok"] is True
    assert media[0]["media_type"] == "image"
    assert media[0]["metadata"]["camera_entity_id"] == "camera.front_door"
