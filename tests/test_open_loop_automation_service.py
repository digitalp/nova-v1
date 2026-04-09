from types import SimpleNamespace

import pytest

from avatar_backend.services.open_loop_automation_service import OpenLoopAutomationService
from avatar_backend.services.open_loop_workflow_service import OpenLoopWorkflowService


class _FakeMetricsDB:
    def recent_event_history(self, n=100):
        return [
            {
                "ts": "2026-04-08T10:05:00+00:00",
                "event_id": "evt-1",
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


class _FakeActionService:
    def __init__(self) -> None:
        self.calls = []

    async def handle_event_history_action(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "ok": True,
            "event_id": kwargs["event_id"],
            "workflow_action": kwargs["workflow_action"],
            "status": kwargs["status"],
        }


@pytest.mark.asyncio
async def test_open_loop_automation_service_runs_due_actions():
    action_service = _FakeActionService()
    app = SimpleNamespace(
        state=SimpleNamespace(
            metrics_db=_FakeMetricsDB(),
            action_service=action_service,
            ws_manager=None,
        )
    )
    service = OpenLoopAutomationService(
        app,
        workflow_service=OpenLoopWorkflowService(),
        interval_s=300,
        startup_delay_s=0,
        max_actions_per_run=3,
    )

    result = await service.run_once()

    assert result["planned"] == 1
    assert result["applied"] == 1
    assert result["applied_actions"][0]["workflow_action"] == "escalate_high"
    assert action_service.calls[0]["event_id"] == "evt-1"
    assert service.get_status()["last_run_summary"]["applied"] == 1
