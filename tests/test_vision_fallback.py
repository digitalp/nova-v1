"""
Priority 5 — Isolate vision/provider failures.

Tests cover:
- describe_image Google → Ollama fallback when Gemini raises
- describe_image OpenAI → Ollama fallback when OpenAI raises
- describe_image unsupported provider returns safe string, no fallback
- describe_image Ollama with motion_vision_provider=ollama_remote routes to vision URL
- describe_image_with_gemini falls back immediately when semaphore full
- describe_image_with_gemini rotates key on 429 then falls back to Ollama
- _fallback_to_ollama_vision returns safe string when Ollama also fails
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import pytest

import avatar_backend.services.llm_service as llm_mod


def _make_service(provider: str, backend=None) -> llm_mod.LLMService:
    svc = llm_mod.LLMService.__new__(llm_mod.LLMService)
    svc._provider = provider
    svc._backend = backend or SimpleNamespace(
        model_name="test-model",
        _model="test-model",
        _api_key="key123",
        _base_url="http://ollama.local:11434",
        _vision_model="llava:7b",
    )
    return svc


@pytest.mark.asyncio
async def test_describe_image_google_falls_back_to_ollama_on_error(monkeypatch):
    """Gemini raises → should call _ollama_describe_image as fallback."""
    svc = _make_service("google")
    monkeypatch.setattr(llm_mod, "_gemini_describe_image",
                        AsyncMock(side_effect=httpx.ReadTimeout("timed out")))
    monkeypatch.setattr(llm_mod, "_ollama_describe_image",
                        AsyncMock(return_value="a delivery van in the driveway"))
    monkeypatch.setattr(llm_mod, "_vision_ollama_url", lambda: "http://ollama.local:11434")
    monkeypatch.setattr(llm_mod, "_get_gemini_key", lambda: "gkey")
    fake_settings = SimpleNamespace(
        ollama_vision_model="llava:7b",
        motion_vision_provider="",
        ollama_vision_url="",
        ollama_url="http://ollama.local:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    result = await svc.describe_image(b"fake_jpeg")

    assert result == "a delivery van in the driveway"
    llm_mod._ollama_describe_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_describe_image_openai_falls_back_to_ollama_on_error(monkeypatch):
    """OpenAI vision raises → should call _ollama_describe_image as fallback."""
    svc = _make_service("openai")
    monkeypatch.setattr(llm_mod, "_openai_describe_image",
                        AsyncMock(side_effect=httpx.ConnectError("refused")))
    monkeypatch.setattr(llm_mod, "_ollama_describe_image",
                        AsyncMock(return_value="person at the door"))
    monkeypatch.setattr(llm_mod, "_vision_ollama_url", lambda: "http://ollama.local:11434")
    fake_settings = SimpleNamespace(
        ollama_vision_model="llava:7b",
        motion_vision_provider="",
        ollama_vision_url="",
        ollama_url="http://ollama.local:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    result = await svc.describe_image(b"fake_jpeg")

    assert result == "person at the door"
    llm_mod._ollama_describe_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_describe_image_unsupported_provider_returns_safe_string(monkeypatch):
    """An unsupported provider (e.g. 'anthropic') returns a safe no-op string."""
    svc = _make_service("anthropic")
    fake_settings = SimpleNamespace(
        ollama_vision_model="llava:7b",
        motion_vision_provider="",
        ollama_vision_url="",
        ollama_url="http://ollama.local:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    result = await svc.describe_image(b"fake_jpeg")

    assert "not supported" in result.lower() or "vision" in result.lower()


@pytest.mark.asyncio
async def test_describe_image_ollama_remote_routing(monkeypatch):
    """motion_vision_provider=ollama_remote should use _vision_ollama_url(), not backend URL."""
    svc = _make_service("ollama")
    calls = {}

    async def fake_ollama_describe(image_bytes, base_url, model, prompt=None):
        calls["base_url"] = base_url
        calls["model"] = model
        return "garden clear"

    monkeypatch.setattr(llm_mod, "_ollama_describe_image", fake_ollama_describe)
    monkeypatch.setattr(llm_mod, "_vision_ollama_url", lambda: "http://remote-ollama:11434")
    fake_settings = SimpleNamespace(
        ollama_vision_model="llava:13b",
        motion_vision_provider="ollama_remote",
        ollama_vision_url="http://remote-ollama:11434",
        ollama_url="http://localhost:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    result = await svc.describe_image(b"fake_jpeg")

    assert result == "garden clear"
    assert calls["base_url"] == "http://remote-ollama:11434"
    assert calls["model"] == "llava:13b"


@pytest.mark.asyncio
async def test_describe_image_with_gemini_falls_back_when_semaphore_full(monkeypatch):
    """When _VISION_SEMAPHORE is full, describe_image_with_gemini falls back immediately."""
    svc = _make_service("google")
    monkeypatch.setattr(llm_mod, "_ollama_describe_image",
                        AsyncMock(return_value="fallback description"))
    monkeypatch.setattr(llm_mod, "_vision_ollama_url", lambda: "http://ollama.local:11434")
    fake_settings = SimpleNamespace(
        cloud_model="gemini-2.5-flash",
        llm_provider="google",
        ollama_vision_model="llava:7b",
        ollama_vision_url="",
        ollama_url="http://ollama.local:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    # Fill the semaphore so it reports as locked
    sem = asyncio.Semaphore(2)
    await sem.acquire()
    await sem.acquire()
    monkeypatch.setattr(llm_mod, "_VISION_SEMAPHORE", sem)

    result = await svc.describe_image_with_gemini(b"fake_jpeg")

    assert result == "fallback description"
    # Should have gone straight to Ollama without touching Gemini
    llm_mod._ollama_describe_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_describe_image_with_gemini_rotates_key_on_429_then_falls_back(monkeypatch):
    """Three 429 responses exhaust the key pool; should fall back to Ollama."""
    svc = _make_service("google")

    def mock_429(_key, _camera_id=None):
        pass

    # All three key attempts return 429
    async def fake_gemini(*args, **kwargs):
        resp = MagicMock()
        resp.status_code = 429
        raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)

    monkeypatch.setattr(llm_mod, "_gemini_describe_image", fake_gemini)
    monkeypatch.setattr(llm_mod, "_get_gemini_key", lambda camera_id=None: "key-rotated")
    monkeypatch.setattr(llm_mod, "_report_gemini_429", mock_429)
    monkeypatch.setattr(llm_mod, "_ollama_describe_image",
                        AsyncMock(return_value="ollama fallback after exhausted keys"))
    monkeypatch.setattr(llm_mod, "_vision_ollama_url", lambda: "http://ollama.local:11434")
    fake_settings = SimpleNamespace(
        cloud_model="gemini-2.5-flash",
        llm_provider="google",
        ollama_vision_model="llava:7b",
        ollama_vision_url="",
        ollama_url="http://ollama.local:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    result = await svc.describe_image_with_gemini(b"fake_jpeg")

    assert result == "ollama fallback after exhausted keys"
    llm_mod._ollama_describe_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_fallback_to_ollama_vision_returns_safe_string_when_ollama_also_fails(monkeypatch):
    """When Ollama also fails in the vision fallback path, return the safe sentinel string."""
    svc = _make_service("google")
    monkeypatch.setattr(llm_mod, "_ollama_describe_image",
                        AsyncMock(side_effect=httpx.ConnectError("ollama down")))
    monkeypatch.setattr(llm_mod, "_vision_ollama_url", lambda: "http://ollama.local:11434")
    fake_settings = SimpleNamespace(
        ollama_vision_model="llava:7b",
        ollama_vision_url="",
        ollama_url="http://ollama.local:11434",
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: fake_settings)

    result = await svc._fallback_to_ollama_vision(b"fake_jpeg")

    assert "couldn't analyze" in result.lower() or "camera image" in result.lower()
