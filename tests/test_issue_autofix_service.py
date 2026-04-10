from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.routers import health as health_module
from avatar_backend.services.issue_autofix_service import IssueAutoFixService


@pytest.mark.asyncio
async def test_issue_autofix_restarts_sensor_watch_after_threshold():
    llm = SimpleNamespace(generate_text=AsyncMock(return_value='{"action":"restart_sensor_watch","reason":"reconnect watcher"}'))
    sensor_watch = SimpleNamespace(stop=AsyncMock(), start=AsyncMock())
    proactive = SimpleNamespace(stop=AsyncMock(), start=AsyncMock())
    decision_log = SimpleNamespace(record=AsyncMock())
    metrics_db = SimpleNamespace(insert_event_history=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_service=llm,
            sensor_watch=sensor_watch,
            proactive_service=proactive,
            decision_log=SimpleNamespace(record=lambda *args, **kwargs: None),
            metrics_db=SimpleNamespace(insert_event_history=lambda payload: None),
        )
    )
    service = IssueAutoFixService(app)

    for _ in range(3):
        result = await service.report_issue(
            "sensor_watch_ws_disconnected",
            source="test",
            summary="Sensor watch dropped",
            details={"exc": "boom"},
        )

    assert result["triggered"] is True
    assert result["action"] == "restart_sensor_watch"
    assert result["success"] is True
    sensor_watch.stop.assert_awaited_once()
    sensor_watch.start.assert_awaited_once()
    proactive.stop.assert_not_called()


@pytest.mark.asyncio
async def test_issue_autofix_refreshes_motion_clip_storage():
    llm = SimpleNamespace(generate_text=AsyncMock(return_value='{"action":"refresh_motion_clip_storage","reason":"probe storage"}'))
    motion_clip = SimpleNamespace(refresh_storage_status=AsyncMock(return_value=True))
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_service=llm,
            motion_clip_service=motion_clip,
            decision_log=SimpleNamespace(record=lambda *args, **kwargs: None),
            metrics_db=SimpleNamespace(insert_event_history=lambda payload: None),
        )
    )
    service = IssueAutoFixService(app)

    first = await service.report_issue("motion_clip_storage_unavailable", source="test")
    second = await service.report_issue("motion_clip_storage_unavailable", source="test")

    assert first["triggered"] is False
    assert second["triggered"] is True
    assert second["success"] is True
    motion_clip.refresh_storage_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_check_reports_timeout_issue(monkeypatch):
    monkeypatch.setattr(health_module, "_probe_ollama", AsyncMock(return_value="reachable"))
    monkeypatch.setattr(health_module, "_probe_ha", AsyncMock(return_value="timeout"))
    monkeypatch.setattr(health_module, "_probe_whisper", lambda request: "ready")
    monkeypatch.setattr(health_module, "_probe_piper", lambda request: "ready")

    issue_service = SimpleNamespace(report_issue=AsyncMock(), resolve_issue=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(issue_autofix_service=issue_service)))

    result = await health_module.health_check(request)

    assert result["status"] == "degraded"
    issue_service.report_issue.assert_awaited_once()
    issue_service.resolve_issue.assert_not_called()
