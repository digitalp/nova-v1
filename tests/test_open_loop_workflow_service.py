from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService


def test_open_loop_workflow_service_summarizes_due_work():
    service = OpenLoopWorkflowService()

    summary = service.summarize_due_work(
        [
            {
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
            },
            {
                "event_id": "evt-2",
                "title": "Doorbell",
                "summary": "Front door live view",
                "status": "acknowledged",
                "event_type": "doorbell",
                "event_source": "doorbell",
                "ts": "2026-04-09T14:05:00+00:00",
                "data": {
                    "open_loop_state": "acknowledged",
                    "open_loop_active": True,
                    "open_loop_started_ts": "2026-04-09T14:05:00+00:00",
                    "open_loop_updated_ts": "2026-04-09T14:05:00+00:00",
                },
            },
        ],
        limit=5,
    )

    assert summary["counts"]["total_open_loops"] == 2
    assert summary["counts"]["stale"] >= 1
    assert summary["counts"]["reminder_due"] >= 1
    assert summary["counts"]["escalation_due"] >= 1
    assert summary["next_actions"]["escalation_due"][0]["event_id"] == "evt-1"


def test_open_loop_workflow_service_plans_due_actions():
    service = OpenLoopWorkflowService()

    planned = service.plan_due_actions(
        [
            {
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
            },
            {
                "event_id": "evt-2",
                "title": "Doorbell",
                "summary": "Front door live view",
                "status": "acknowledged",
                "event_type": "doorbell",
                "event_source": "doorbell",
                "ts": "2026-04-09T14:05:00+00:00",
                "data": {
                    "open_loop_state": "acknowledged",
                    "open_loop_active": True,
                    "open_loop_started_ts": "2026-04-09T14:05:00+00:00",
                    "open_loop_updated_ts": "2026-04-09T14:05:00+00:00",
                },
            },
        ],
        limit=5,
    )

    assert planned[0]["event_id"] == "evt-1"
    assert planned[0]["workflow_action"] == "escalate_high"
    assert planned[0]["open_loop_note"] == "Urgent escalation required"
