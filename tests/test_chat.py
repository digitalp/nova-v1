"""
Phase 2/3 — Chat endpoint tests.
Ollama and ha_proxy are mocked so these run without real services.
"""
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clear_settings_cache():
    from avatar_backend.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY",    "test-key-p2")
    monkeypatch.setenv("HA_URL",     "http://ha.local:8123")
    monkeypatch.setenv("HA_TOKEN",   "fake-token")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")

    acl_path    = tmp_path / "acl.yaml"
    prompt_path = tmp_path / "system_prompt.txt"
    acl_path.write_text(
        "version: 1\nrules:\n"
        "  - domain: light\n    entities: \"*\"\n    services: [turn_on, turn_off]\n"
    )
    prompt_path.write_text("You are a test assistant.")

    import avatar_backend.main as main_mod
    monkeypatch.setattr(main_mod, "_CONFIG_DIR", tmp_path)

    from avatar_backend.main import create_app
    app = create_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


HEADERS = {"X-API-Key": "test-key-p2"}


def test_chat_requires_auth(client):
    resp = client.post("/chat", json={"session_id": "s1", "text": "hello"})
    assert resp.status_code == 401


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock, return_value=("Hello! How can I help?", []))
def test_chat_text_response(mock_chat, mock_ready, client):
    resp = client.post("/chat", json={"session_id": "s1", "text": "hello"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Hello! How can I help?"
    assert body["tool_calls"] == []
    assert body["session_id"] == "s1"


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock)
@patch("avatar_backend.services.ha_proxy.HAProxy.execute_tool_call", new_callable=AsyncMock)
def test_chat_tool_call_executed_and_allowed(mock_ha, mock_chat, mock_ready, client):
    from avatar_backend.models.messages import ToolCall
    from avatar_backend.models.tool_result import ToolResult

    tool_call = ToolCall(function_name="call_ha_service", arguments={
        "domain": "light", "service": "turn_on", "entity_id": "light.kitchen"
    })
    # Round 1: LLM returns tool call; Round 2: LLM returns final text
    mock_chat.side_effect = [
        ((""), [tool_call]),
        ("The kitchen light is now on.", []),
    ]
    mock_ha.return_value = ToolResult(
        success=True, message="Done", entity_id="light.kitchen", service_called="light.turn_on"
    )

    resp = client.post("/chat", json={"session_id": "s2", "text": "turn on kitchen light"}, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "The kitchen light is now on."
    tc = body["tool_calls"][0]
    assert tc["function_name"] == "call_ha_service"
    assert tc["acl_status"] == "allowed"


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock)
@patch("avatar_backend.services.ha_proxy.HAProxy.execute_tool_call", new_callable=AsyncMock)
def test_chat_tool_call_acl_denied(mock_ha, mock_chat, mock_ready, client):
    from avatar_backend.models.messages import ToolCall
    from avatar_backend.models.tool_result import ToolResult

    tool_call = ToolCall(function_name="call_ha_service", arguments={
        "domain": "lock", "service": "unlock", "entity_id": "lock.front_door"
    })
    mock_chat.side_effect = [
        ("", [tool_call]),
        ("I cannot control door locks.", []),
    ]
    mock_ha.return_value = ToolResult(
        success=False,
        message="Permission denied: Domain 'lock' is not in the allowed list.",
        entity_id="lock.front_door",
        service_called="lock.unlock",
        ha_status_code=0,
    )

    resp = client.post("/chat", json={"session_id": "s3", "text": "unlock front door"}, headers=HEADERS)
    assert resp.status_code == 200
    tc = resp.json()["tool_calls"][0]
    assert tc["acl_status"] == "denied"


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=False)
def test_chat_503_when_model_not_ready(mock_ready, client):
    resp = client.post("/chat", json={"session_id": "s4", "text": "hello"}, headers=HEADERS)
    assert resp.status_code == 503


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock, return_value=("Got it.", []))
def test_session_history_grows(mock_chat, mock_ready, client):
    for i in range(3):
        client.post("/chat", json={"session_id": "history_test", "text": f"msg {i}"}, headers=HEADERS)
    stats = client.get("/chat/sessions/stats", headers=HEADERS)
    assert stats.json()["active_sessions"] >= 1


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock, return_value=("Cleared.", []))
def test_session_clear(mock_chat, mock_ready, client):
    client.post("/chat", json={"session_id": "to_clear", "text": "hi"}, headers=HEADERS)
    resp = client.delete("/chat/to_clear", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["cleared"] == "to_clear"
