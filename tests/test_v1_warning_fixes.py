import asyncio
from types import SimpleNamespace

import httpx
import pytest

from avatar_backend.routers import health as health_module
from avatar_backend.services import motion_clip_service as motion_clip_module
from avatar_backend.services import sensor_watch_service as sensor_watch_module


def test_sensor_watch_format_exc_includes_type_for_blank_messages():
    exc = asyncio.TimeoutError()

    assert sensor_watch_module._format_exc(exc) == "TimeoutError"


@pytest.mark.asyncio
async def test_motion_clip_capture_skips_when_storage_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(motion_clip_module, "data_dir", lambda: tmp_path)

    db = SimpleNamespace(insert_motion_clip=lambda payload: pytest.fail("db write should be skipped"))
    service = motion_clip_module.MotionClipService(
        db=db,
        ha_proxy=SimpleNamespace(),
        llm_service=SimpleNamespace(),
    )
    service._clips_dir_ready = False

    clip_id = await service.capture_and_store(camera_entity_id="camera.driveway")

    assert clip_id is None


@pytest.mark.asyncio
async def test_probe_ha_reports_timeout(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(health_module.httpx, "AsyncClient", FakeAsyncClient)

    result = await health_module._probe_ha("http://ha.local:8123", "token")

    assert result == "timeout"
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].read == 8.0
