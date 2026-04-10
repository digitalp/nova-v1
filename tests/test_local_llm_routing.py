from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services import llm_service as llm_module
from avatar_backend.services.motion_clip_service import MotionClipService
from avatar_backend.services.persistent_memory import PersistentMemoryService


def test_select_local_text_model_prefers_best_installed():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "models": [
                    {"name": "gemma2:9b"},
                    {"name": "mistral-nemo:12b"},
                    {"name": "llama3.1:8b-instruct-q4_K_M"},
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url):
            return FakeResponse()

    settings = SimpleNamespace(
        ollama_local_text_model="",
        ollama_url="http://localhost:11434",
        ollama_model="llama3.1:8b-instruct-q4_K_M",
    )
    original = llm_module.httpx.Client
    llm_module.httpx.Client = FakeClient
    try:
        assert llm_module._select_local_text_model(settings) == "mistral-nemo:12b"
    finally:
        llm_module.httpx.Client = original


@pytest.mark.asyncio
async def test_persistent_memory_uses_local_text_generation():
    records = []

    class FakeDB:
        def upsert_memory(self, **kwargs):
            records.append(kwargs)

    llm = SimpleNamespace(
        generate_text_local=AsyncMock(
            return_value='[{"summary":"Penn prefers the lounge warm.","category":"comfort","confidence":0.9}]'
        )
    )
    service = PersistentMemoryService(FakeDB())

    await service._learn_from_exchange(
        session_id="s1",
        user_text="I like the lounge warm in the evening.",
        assistant_text="I will remember that preference.",
        llm=llm,
    )

    llm.generate_text_local.assert_awaited_once()
    assert records
    assert records[0]["category"] == "comfort"


@pytest.mark.asyncio
async def test_motion_clip_ranking_uses_local_text_generation(tmp_path, monkeypatch):
    monkeypatch.setattr("avatar_backend.services.motion_clip_service.data_dir", lambda: tmp_path)
    llm = SimpleNamespace(generate_text_local=AsyncMock(return_value='{"ids":[2,1]}'))
    service = MotionClipService(
        db=SimpleNamespace(),
        ha_proxy=SimpleNamespace(),
        llm_service=llm,
    )
    candidates = [
        {"id": 1, "ts": "2026-04-10T04:00:00Z", "camera_entity_id": "camera.front", "location": "Front", "description": "Parcel left"},
        {"id": 2, "ts": "2026-04-10T04:01:00Z", "camera_entity_id": "camera.driveway", "location": "Driveway", "description": "Car arrived"},
    ]

    ranked = await service._rank_candidates("car on driveway", candidates)

    llm.generate_text_local.assert_awaited_once()
    assert ranked == [2, 1]
