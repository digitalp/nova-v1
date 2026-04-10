from types import SimpleNamespace
import httpx
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services import llm_service as llm_module
from avatar_backend.services import sensor_watch_service as sensor_watch_module
from avatar_backend.services.sensor_watch_service import SensorWatchService
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


def test_select_fast_local_text_model_prefers_qwen_when_available():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "models": [
                    {"name": "mistral-nemo:12b"},
                    {"name": "qwen2.5:7b"},
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
        proactive_ollama_model="",
        ollama_local_text_model="",
        ollama_url="http://localhost:11434",
        ollama_model="mistral-nemo:12b",
    )
    original = llm_module.httpx.Client
    llm_module.httpx.Client = FakeClient
    try:
        assert llm_module._select_fast_local_text_model(settings) == "qwen2.5:7b"
    finally:
        llm_module.httpx.Client = original


@pytest.mark.asyncio
async def test_generate_text_local_fast_resilient_falls_back_to_cloud_after_retry(monkeypatch):
    service = llm_module.LLMService.__new__(llm_module.LLMService)
    service._provider = "google"
    service._backend = SimpleNamespace(model_name="gemini-2.5-flash")
    service._local_text_backend = SimpleNamespace(model_name="mistral-nemo:12b")
    service._fast_local_text_backend = SimpleNamespace(
        model_name="qwen2.5:7b",
        generate_text=AsyncMock(side_effect=[httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out")]),
    )
    service.generate_text = AsyncMock(return_value='{"announce": false}')
    monkeypatch.setattr(llm_module.asyncio, "sleep", AsyncMock())

    result = await service.generate_text_local_fast_resilient(
        "prompt",
        timeout_s=10.0,
        retry_delay_s=0.1,
        fallback_timeout_s=5.0,
        purpose="test",
    )

    assert result == '{"announce": false}'
    assert service._fast_local_text_backend.generate_text.await_count == 2
    service.generate_text.assert_awaited_once_with("prompt", timeout_s=5.0)


def test_select_sensor_watch_model_prefers_faster_review_model():
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "models": [
                    {"name": "mistral-nemo:12b"},
                    {"name": "qwen2.5:7b"},
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
        sensor_watch_ollama_model="",
        ollama_local_text_model="",
        ollama_url="http://localhost:11434",
        ollama_model="mistral-nemo:12b",
    )
    original_client = sensor_watch_module.httpx.Client
    try:
        sensor_watch_module.httpx.Client = FakeClient
        assert sensor_watch_module._select_sensor_watch_model(settings) == "qwen2.5:7b"
    finally:
        sensor_watch_module.httpx.Client = original_client


def test_sensor_watch_review_snapshot_is_compacted_by_priority():
    snapshot = [
        {"entity_id": "sensor.room_ok", "device_class": "temperature", "state": "20", "friendly": "Room", "unit": "°C"},
        {"entity_id": "sensor.low_battery", "device_class": "battery", "state": "8", "friendly": "Battery", "unit": "%"},
        {"entity_id": "sensor.humid_room", "device_class": "humidity", "state": "85", "friendly": "Humidity", "unit": "%"},
        {
            "entity_id": "sensor.19_patterdale_place_black_bin_days_until_collection",
            "device_class": "",
            "state": "1",
            "friendly": "Black Bin",
            "unit": "days",
        },
    ] + [
        {"entity_id": f"sensor.extra_{i}", "device_class": "temperature", "state": "21", "friendly": f"Extra {i}", "unit": "°C"}
        for i in range(40)
    ]

    compacted = sensor_watch_module._compress_snapshot_for_review(snapshot, limit=3)

    ids = {row["entity_id"] for row in compacted}
    assert "sensor.low_battery" in ids
    assert "sensor.humid_room" in ids
    assert "sensor.19_patterdale_place_black_bin_days_until_collection" in ids


@pytest.mark.asyncio
async def test_persistent_memory_uses_local_text_generation():
    records = []

    class FakeDB:
        def upsert_memory(self, **kwargs):
            records.append(kwargs)

    llm = SimpleNamespace(
        generate_text_local_resilient=AsyncMock(
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

    llm.generate_text_local_resilient.assert_awaited_once()
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


@pytest.mark.asyncio
async def test_sensor_watch_uses_preferred_local_model_and_timeout(monkeypatch):
    monkeypatch.setattr(
        "avatar_backend.services.sensor_watch_service.get_settings",
        lambda: SimpleNamespace(
            sensor_watch_ollama_model="",
            sensor_watch_review_timeout_s=150.0,
            ollama_local_text_model="",
            ollama_url="http://localhost:11434",
            ollama_model="llama3.1:8b-instruct-q4_K_M",
        ),
    )
    service = SensorWatchService(
        ha_url="http://ha.local",
        ha_token="token",
        ollama_url="http://localhost:11434",
        announce_fn=AsyncMock(),
    )
    monkeypatch.setattr(service, "_fetch_sensor_snapshot", AsyncMock(return_value=[
        {
            "entity_id": "sensor.room_temp",
            "friendly": "Room Temperature",
            "state": "12",
            "unit": "°C",
            "device_class": "temperature",
        }
    ]))
    calls = {}

    async def fake_ollama_generate(prompt, ollama_url, model, timeout_s=120.0):
        calls["ollama_url"] = ollama_url
        calls["model"] = model
        calls["timeout_s"] = timeout_s
        return '{"announce": false}'

    monkeypatch.setattr("avatar_backend.services.sensor_watch_service._ollama_generate", fake_ollama_generate)

    await service._run_snapshot_review()

    assert calls["ollama_url"] == "http://localhost:11434"
    assert calls["model"] == "qwen2.5:7b"
    assert calls["timeout_s"] == 150.0


@pytest.mark.asyncio
async def test_sensor_watch_review_falls_back_to_cloud_after_retry(monkeypatch):
    monkeypatch.setattr(
        "avatar_backend.services.sensor_watch_service.get_settings",
        lambda: SimpleNamespace(
            sensor_watch_ollama_model="",
            sensor_watch_review_timeout_s=30.0,
            ollama_local_text_model="",
            ollama_url="http://localhost:11434",
            ollama_model="llama3.1:8b-instruct-q4_K_M",
        ),
    )
    service = SensorWatchService(
        ha_url="http://ha.local",
        ha_token="token",
        ollama_url="http://localhost:11434",
        announce_fn=AsyncMock(),
        llm_service=SimpleNamespace(
            provider_name="google",
            model_name="gemini-2.5-flash",
            generate_text=AsyncMock(return_value='{"announce": false}'),
        ),
    )
    monkeypatch.setattr(
        "avatar_backend.services.sensor_watch_service._ollama_generate",
        AsyncMock(side_effect=[httpx.ReadTimeout("timed out"), httpx.ReadTimeout("timed out")]),
    )
    monkeypatch.setattr(sensor_watch_module.asyncio, "sleep", AsyncMock())

    result = await service._generate_review_text("prompt")

    assert result == '{"announce": false}'
    service._llm_service.generate_text.assert_awaited_once_with("prompt", timeout_s=25.0)
