from types import SimpleNamespace

import pytest

from avatar_backend.routers.admin import (
    EventHistoryDomainActionBody,
    EventHistoryWorkflowRunBody,
    _serialize_motion_clip,
    get_event_history,
    run_event_history_domain_action,
    get_event_history_workflow_summary,
    get_event_history_workflow_status,
    run_event_history_workflow,
)
from avatar_backend.services.metrics_db import MetricsDB
from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService


def test_serialize_motion_clip_exposes_canonical_event_fields():
    clip = _serialize_motion_clip(
        {
            "id": 7,
            "video_relpath": "2026/04/08/example.mp4",
            "extra": {
                "source": "announce_motion",
                "canonical_event": {
                    "event_id": "evt-7",
                    "event_type": "motion_detected",
                    "event_context": {"source": "announce_motion"},
                },
            },
        }
    )

    assert clip["video_url"] == "/admin/motion-clips/7/video"
    assert clip["canonical_event_id"] == "evt-7"
    assert clip["canonical_event_type"] == "motion_detected"
    assert clip["event_source"] == "announce_motion"


class _FakeSurfaceState:
    async def get_snapshot(self):
        return {
            "recent_events": [
                {
                    "event_id": "evt-surface-1",
                    "event": "doorbell",
                    "title": "Doorbell",
                    "message": "Front door live view",
                    "status": "active",
                    "camera_entity_id": "camera.front_door",
                    "ts": 1712600000.0,
                }
            ]
        }


class _FakeDB:
    def list_event_records(self, limit=20, **kwargs):
        return [
            {
                "event_id": "evt-canonical-1",
                "event_type": "package_delivery",
                "source": "package_announce",
                "camera_entity_id": "camera.front_door",
                "summary": "A package was delivered.",
                "details": "Package Delivery",
                "status": "active",
                "created_at": "2026-04-08T20:06:00+00:00",
                "data": {
                    "open_loop_state": "active",
                    "open_loop_active": True,
                    "open_loop_started_ts": "2026-04-08T20:06:00+00:00",
                    "open_loop_updated_ts": "2026-04-08T20:06:00+00:00",
                },
            }
        ]

    def recent_event_history(self, n=20):
        return [
            {
                "ts": "2026-04-08T20:05:00+00:00",
                "event_id": "evt-persisted-1",
                "event_type": "doorbell",
                "title": "Doorbell",
                "summary": "Front door live view",
                "status": "active",
                "event_source": "doorbell",
                "camera_entity_id": "camera.front_door",
                "data": {
                    "open_loop_state": "active",
                    "open_loop_active": True,
                    "open_loop_started_ts": "2026-04-08T20:05:00+00:00",
                    "open_loop_updated_ts": "2026-04-08T20:05:00+00:00",
                },
            },
            {
                "ts": "2026-04-08T10:05:00+00:00",
                "event_id": "evt-persisted-2",
                "event_type": "driveway_vehicle",
                "title": "Driveway vehicle",
                "summary": "Vehicle still unresolved",
                "status": "active",
                "event_source": "driveway",
                "camera_entity_id": "camera.driveway",
                "data": {
                    "open_loop_state": "active",
                    "open_loop_active": True,
                    "open_loop_started_ts": "2026-04-08T10:05:00+00:00",
                    "open_loop_updated_ts": "2026-04-08T10:05:00+00:00",
                },
            }
        ]

    def recent_motion_clips(self, limit=60):
        return [
            {
                "id": 12,
                "ts": "2026-04-08T20:00:00+00:00",
                "camera_entity_id": "camera.driveway",
                "location": "Driveway",
                "description": "A car arrived.",
                "status": "ready",
                "video_relpath": "2026/04/08/example.mp4",
                "extra": {
                    "source": "announce_motion",
                    "canonical_event": {
                        "event_id": "evt-motion-12",
                        "event_type": "vehicle_detected",
                        "event_context": {"source": "announce_motion"},
                    },
                },
            }
        ]


@pytest.mark.asyncio
async def test_event_history_combines_motion_and_surface_events(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10)
    assert len(data["events"]) == 5
    assert data["events"][0]["kind"] == "canonical_event"
    assert data["events"][0]["event_type"] == "package_delivery"
    assert data["events"][0]["event_source"] == "package_announce"
    assert data["events"][0]["open_loop_active"] is True
    available_actions = {action["action"] for action in data["events"][0]["available_actions"]}
    assert "ask_about_event" in available_actions
    persisted = next(item for item in data["events"] if item["kind"] == "persisted_event" and item["event_type"] == "doorbell")
    assert persisted["open_loop_active"] is True
    assert persisted["open_loop_stale"] is True
    assert persisted["open_loop_priority"] in {"medium", "high"}
    available_actions = {action["action"] for action in persisted["available_actions"]}
    assert "send_reminder" in available_actions
    assert "escalate_medium" in available_actions or "escalate_high" in available_actions
    event_pairs = [(item["kind"], item["event_type"]) for item in data["events"]]
    assert ("canonical_event", "package_delivery") in event_pairs
    assert ("persisted_event", "driveway_vehicle") in event_pairs
    assert ("motion_clip", "vehicle_detected") in event_pairs
    assert ("surface_event", "doorbell") in event_pairs


@pytest.mark.asyncio
async def test_event_history_filters_by_kind_and_source(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, kind="canonical_event", event_source="package_announce")
    assert len(data["events"]) == 1
    assert data["events"][0]["kind"] == "canonical_event"
    assert data["events"][0]["event_source"] == "package_announce"


@pytest.mark.asyncio
async def test_event_history_supports_before_ts_window(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, before_ts="2026-04-08T20:03:00+00:00", window="30d")
    assert all(event["ts"] < "2026-04-08T20:03:00+00:00" for event in data["events"])


@pytest.mark.asyncio
async def test_event_history_can_filter_open_loops(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, open_loop_only=True, open_loop_state="active")
    assert len(data["events"]) >= 1
    assert all(event["open_loop_active"] is True for event in data["events"])
    assert all(event["open_loop_state"] == "active" for event in data["events"])


@pytest.mark.asyncio
async def test_event_history_can_filter_stale_open_loops(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, open_loop_stale_only=True)
    assert len(data["events"]) >= 1
    assert all(event["open_loop_stale"] is True for event in data["events"])


@pytest.mark.asyncio
async def test_event_history_can_filter_open_loop_priority(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, open_loop_priority="high")
    assert all(event["open_loop_priority"] == "high" for event in data["events"])


@pytest.mark.asyncio
async def test_event_history_can_filter_reminder_due_open_loops(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, open_loop_reminder_due_only=True)
    assert len(data["events"]) >= 1
    assert all(event["open_loop_reminder_due"] is True for event in data["events"])


@pytest.mark.asyncio
async def test_event_history_exposes_workflow_actions(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, kind="persisted_event")
    persisted = data["events"][0]
    actions = {action["action"]: action for action in persisted["available_actions"]}
    assert actions["ask_about_event"]["label"]
    assert actions["acknowledge"]["label"] == "Acknowledge"
    assert actions["resolve"]["label"] == "Resolve"
    assert "send_reminder" in actions
    assert actions["send_reminder"]["open_loop_note"] == "Reminder sent for follow-up"


@pytest.mark.asyncio
async def test_event_history_workflow_summary_reports_due_queue(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
                open_loop_workflow_service=OpenLoopWorkflowService(),
            )
        )
    )

    data = await get_event_history_workflow_summary(request, limit=5)
    assert data["counts"]["total_open_loops"] >= 1
    assert data["counts"]["reminder_due"] >= 1
    assert data["counts"]["escalation_due"] >= 1
    assert data["generated_from"]["kind"] == "persisted_event"


@pytest.mark.asyncio
async def test_event_history_workflow_run_applies_planned_actions(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    class _FakeActionService:
        def __init__(self) -> None:
            self.calls = []

        async def handle_event_history_action(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": True,
                "event_id": kwargs["event_id"],
                "status": kwargs["status"],
                "workflow_action": kwargs["workflow_action"],
            }

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})

    async def _fake_get_event_history(request, **kwargs):
        return {
            "events": [
                {
                    "kind": "persisted_event",
                    "event_id": "evt-1",
                    "title": "Driveway vehicle",
                    "summary": "Vehicle still unresolved",
                    "status": "active",
                    "event_type": "driveway_vehicle",
                    "event_source": "driveway",
                    "ts": "2026-04-08T10:05:00+00:00",
                    "data": {
                        "open_loop_state": "active",
                        "open_loop_active": True,
                        "open_loop_started_ts": "2026-04-08T10:05:00+00:00",
                        "open_loop_updated_ts": "2026-04-08T10:05:00+00:00",
                    },
                }
            ]
        }

    monkeypatch.setattr(admin_module, "get_event_history", _fake_get_event_history)
    action_service = _FakeActionService()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                open_loop_workflow_service=OpenLoopWorkflowService(),
                action_service=action_service,
                ws_manager=None,
            )
        )
    )

    result = await run_event_history_workflow(
        EventHistoryWorkflowRunBody(include_reminders=True, include_escalations=True, limit=5, dry_run=False),
        request,
    )

    assert len(result["planned"]) == 1
    assert result["planned"][0]["workflow_action"] == "escalate_high"
    assert result["applied"][0]["workflow_action"] == "escalate_high"
    assert action_service.calls[0]["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_event_history_workflow_status_exposes_automation_state(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                open_loop_automation_service=SimpleNamespace(
                    get_status=lambda: {
                        "running": True,
                        "interval_s": 900,
                        "startup_delay_s": 120,
                        "max_actions_per_run": 4,
                        "last_run_ts": "2026-04-09T18:00:00+00:00",
                        "last_run_summary": {"planned": 2, "applied": 1, "applied_actions": [{"event_id": "evt-1"}]},
                    }
                )
            )
        )
    )

    status = await get_event_history_workflow_status(request)
    assert status["running"] is True
    assert status["last_run_summary"]["applied"] == 1


@pytest.mark.asyncio
async def test_event_history_domain_action_runs_followup(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    class _FakeActionService:
        async def handle_event_history_domain_action(self, **kwargs):
            return {
                "ok": True,
                "action": kwargs["action"],
                "event_id": kwargs["event_id"],
                "text": "It looks like a normal delivery.",
            }

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        ws_manager=None,
        action_service=_FakeActionService(),
    )))

    result = await run_event_history_domain_action(
        EventHistoryDomainActionBody(
            session_id="admin_event_history",
            event_id="evt-7",
            action="ask_about_event",
            title="Package",
            summary="Parcel at the front door",
            event_type="package_delivery",
            event_source="doorbell",
            camera_entity_id="camera.front_door",
            followup_prompt="Focus on where the package is.",
        ),
        request,
    )

    assert result["ok"] is True
    assert result["action"] == "ask_about_event"
    assert result["text"] == "It looks like a normal delivery."


@pytest.mark.asyncio
async def test_event_history_can_filter_escalation_due_open_loops(monkeypatch):
    from avatar_backend.routers import admin as admin_module

    monkeypatch.setattr(admin_module, "_require_session", lambda request, min_role="viewer": {"role": "viewer"})
    monkeypatch.setattr(admin_module, "_motion_clip_is_playable", lambda request, clip: True)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metrics_db=_FakeDB(),
                surface_state_service=_FakeSurfaceState(),
            )
        )
    )

    data = await get_event_history(request, limit=10, open_loop_escalation_due_only=True)
    assert len(data["events"]) >= 1
    assert all(event["open_loop_escalation_due"] is True for event in data["events"])


def test_metrics_db_event_history_tracks_open_loop_fields(tmp_path):
    db = MetricsDB(path=tmp_path / "metrics.db")
    db.insert_event_history(
        {
            "event_id": "evt-1",
            "event_type": "doorbell",
            "title": "Doorbell",
            "summary": "Front door live view",
            "status": "active",
            "event_source": "doorbell",
            "camera_entity_id": "camera.front_door",
            "data": {"open_loop_note": "Needs attention"},
        }
    )

    row = db.recent_event_history(1)[0]
    assert row["data"]["open_loop_state"] == "active"
    assert row["data"]["open_loop_active"] is True
    assert row["data"]["open_loop_started_ts"]

    updated = db.update_event_history_status("evt-1", "resolved", "Closed out", "Handled")
    assert updated is True

    row = db.recent_event_history(1)[0]
    assert row["status"] == "resolved"
    assert row["data"]["open_loop_state"] == "resolved"
    assert row["data"]["open_loop_active"] is False
    assert row["data"]["open_loop_resolved_ts"]
    assert row["data"]["admin_note"] == "Handled"


def test_metrics_db_event_history_tracks_open_loop_policy_fields(tmp_path):
    from avatar_backend.services.open_loop_service import OpenLoopService

    db = MetricsDB(path=tmp_path / "metrics.db")
    db.insert_event_history(
        {
            "ts": "2026-04-08T08:00:00+00:00",
            "event_id": "evt-policy-1",
            "event_type": "doorbell",
            "title": "Doorbell",
            "summary": "Front door live view",
            "status": "active",
            "event_source": "doorbell",
            "camera_entity_id": "camera.front_door",
            "data": {"open_loop_note": "Needs attention"},
        }
    )

    updated = db.update_event_history_policy("evt-policy-1", reminder_sent=True, escalation_level="medium")
    assert updated is True

    row = db.recent_event_history(1)[0]
    assert row["data"]["open_loop_last_reminder_ts"]
    assert row["data"]["open_loop_reminder_count"] == 1
    assert row["data"]["open_loop_escalation_level"] == "medium"
    assert row["data"]["open_loop_last_escalation_ts"]

    summary = OpenLoopService().extract_summary_fields(
        row["data"],
        status=row["status"],
        fallback_ts=row["ts"],
    )
    assert summary["open_loop_reminder_state"] == "sent"
    assert summary["open_loop_escalation_level"] == "medium"
