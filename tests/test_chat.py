"""
Phase 2/3 — Chat endpoint tests.
Ollama and ha_proxy are mocked so these run without real services.
"""
from datetime import datetime
import pytest
from unittest.mock import AsyncMock, patch

from avatar_backend.services import chat_service as chat_service_module
from fastapi.testclient import TestClient
from avatar_backend.services.chat_service import _sanitize_history


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


@patch("avatar_backend.services.chat_service.datetime")
@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat", new_callable=AsyncMock)
def test_chat_time_question_is_answered_deterministically(mock_chat, mock_ready, mock_datetime, client):
    mock_datetime.now.return_value = datetime(2026, 4, 10, 16, 0)

    resp = client.post("/chat", json={"session_id": "time-test", "text": "what time is it?"}, headers=HEADERS)

    assert resp.status_code == 200
    assert resp.json()["text"] == "The current time is 16:00 BST."
    assert mock_chat.await_count == 0


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


def test_sanitize_history_drops_orphan_tool_messages_after_trim():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "content": "result 1"},
        {"role": "tool", "content": "result 2"},
        {"role": "user", "content": "hello"},
    ]

    sanitized = _sanitize_history(messages)

    assert sanitized == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock)
def test_chat_context_merges_incrementally_across_requests(mock_chat, mock_ready, client):
    mock_chat.side_effect = [
        ("Captured room.", []),
        ("Captured mode.", []),
        ("Merged context answer.", []),
    ]

    resp1 = client.post(
        "/chat",
        json={
            "session_id": "context-merge",
            "text": "Remember the room.",
            "context": {"room": "Kitchen", "lights": ["kitchen", "hallway"]},
        },
        headers=HEADERS,
    )
    resp2 = client.post(
        "/chat",
        json={
            "session_id": "context-merge",
            "text": "Add the mode.",
            "context": {"mode": "Evening", "climate": {"target": 21}},
        },
        headers=HEADERS,
    )
    resp3 = client.post(
        "/chat",
        json={"session_id": "context-merge", "text": "What changed?"},
        headers=HEADERS,
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp3.status_code == 200

    third_messages = mock_chat.await_args_list[2].args[0]
    assert third_messages[-1]["role"] == "user"
    assert third_messages[-1]["content"] == (
        "What changed?\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  lights.0: kitchen\n"
        "  lights.1: hallway\n"
        "  mode: Evening\n"
        "  climate.target: 21"
    )


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat", new_callable=AsyncMock, return_value=("Using the preference.", []))
def test_chat_always_injects_enforced_preference_memories(mock_chat, mock_ready, client):
    client.app.state.memory_service.add_memory(
        summary="Nova should speak units as words rather than symbols in spoken output.",
        category="preference",
        source="manual",
        confidence=1.0,
        pinned=True,
    )

    resp = client.post(
        "/chat",
        json={"session_id": "pref-test", "text": "Give me a quick update."},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    first_messages = mock_chat.await_args_list[0].args[0]
    system_text = first_messages[0]["content"]
    assert "Enforced household preferences and policies." in system_text
    assert "speak units as words rather than symbols" in system_text


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat_operational", new_callable=AsyncMock, return_value=("Power looks high.", []))
def test_operational_session_uses_operational_llm_and_injects_power_hint(mock_chat_operational, mock_ready, client):
    resp = client.post(
        "/chat",
        json={"session_id": "ha_power_alert", "text": "Power alert at home."},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    first_messages = mock_chat_operational.await_args_list[0].args[0]
    system_text = first_messages[0]["content"]
    assert "automated Home Assistant power alert session" in system_text
    assert "Never invent services like sensor.tts.say" in system_text


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat_operational", new_callable=AsyncMock, return_value=("Power looks high.", []))
def test_power_alert_session_cooldown_skips_repeat_calls(mock_chat_operational, mock_ready, client):
    chat_service_module._LAST_AUTOMATED_SESSION_RUN_AT.clear()
    try:
        first = client.post(
            "/chat",
            json={"session_id": "ha_power_alert", "text": "Power alert at home."},
            headers=HEADERS,
        )
        second = client.post(
            "/chat",
            json={"session_id": "ha_power_alert", "text": "Power alert at home."},
            headers=HEADERS,
        )
    finally:
        chat_service_module._LAST_AUTOMATED_SESSION_RUN_AT.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["text"] == "Power looks high."
    assert second.json()["text"] == ""
    assert second.json()["model"] == "cooldown_skip"
    assert mock_chat_operational.await_count == 1


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat_operational", new_callable=AsyncMock, return_value=("Tyre warning acknowledged.", []))
def test_operational_session_injects_car_warning_hint(mock_chat_operational, mock_ready, client):
    resp = client.post(
        "/chat",
        json={"session_id": "ha_car_warning", "text": "Car warning detected."},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    first_messages = mock_chat_operational.await_args_list[0].args[0]
    system_text = first_messages[0]["content"]
    assert "automated car warning session" in system_text
    assert "Do not call binary_sensor.turn_on" in system_text


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock)
def test_chat_empty_context_clears_persisted_context_for_later_requests(mock_chat, mock_ready, client):
    mock_chat.side_effect = [
        ("Captured context.", []),
        ("Cleared context.", []),
        ("No sticky context.", []),
    ]

    resp1 = client.post(
        "/chat",
        json={
            "session_id": "context-clear",
            "text": "Remember the driveway.",
            "context": {"camera": "driveway", "severity": "normal"},
        },
        headers=HEADERS,
    )
    resp2 = client.post(
        "/chat",
        json={"session_id": "context-clear", "text": "Clear it.", "context": {}},
        headers=HEADERS,
    )
    resp3 = client.post(
        "/chat",
        json={"session_id": "context-clear", "text": "What changed?"},
        headers=HEADERS,
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp3.status_code == 200

    second_messages = mock_chat.await_args_list[1].args[0]
    third_messages = mock_chat.await_args_list[2].args[0]
    assert second_messages[-1]["content"] == "Clear it."
    assert third_messages[-1]["content"] == "What changed?"


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.llm_service.LLMService.chat",     new_callable=AsyncMock, return_value=("Cleared.", []))
def test_session_clear(mock_chat, mock_ready, client):
    client.post("/chat", json={"session_id": "to_clear", "text": "hi"}, headers=HEADERS)
    resp = client.delete("/chat/to_clear", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["cleared"] == "to_clear"


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
def test_session_clear_removes_pending_event_context(mock_ready, client):
    conversation_service = client.app.state.conversation_service

    async def seed_pending_context():
        from avatar_backend.services.conversation_service import PendingEventFollowupContext
        await conversation_service.set_event_followup_context(
            "to_clear_pending",
            PendingEventFollowupContext(
                event_type="parcel_delivery",
                event_summary="Package still outside",
                event_context={"camera_entity_id": "camera.front_door"},
            ),
        )

    import asyncio
    asyncio.run(seed_pending_context())

    resp = client.delete("/chat/to_clear_pending", headers=HEADERS)

    assert resp.status_code == 200
    assert resp.json()["cleared"] == "to_clear_pending"
    assert "to_clear_pending" not in conversation_service._session_states


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
@patch("avatar_backend.services.conversation_service.ConversationService.handle_text_turn", new_callable=AsyncMock)
@patch("avatar_backend.services.conversation_service.ConversationService.set_event_followup_context", new_callable=AsyncMock)
def test_chat_followup_event_uses_stored_event_context(mock_set_context, mock_handle_text_turn, mock_ready, client):
    from avatar_backend.services.chat_service import ChatResult

    mock_handle_text_turn.return_value = ChatResult(
        text="It looks like a normal delivery.",
        tool_calls=[],
        processing_time_ms=42,
        model="test-model",
        session_id="s-followup",
    )
    client.app.state.recent_event_contexts["evt-1"] = (
        0.0,
        {
            "event_type": "parcel_delivery",
            "event_summary": "Package left near the front door.",
            "event_context": {"camera_entity_id": "camera.front_door", "source": "parcel"},
        },
    )

    resp = client.post(
        "/chat/followup-event",
        json={"session_id": "s-followup", "text": "Is this urgent?", "event_id": "evt-1"},
        headers=HEADERS,
    )

    assert resp.status_code == 200
    assert resp.json()["text"] == "It looks like a normal delivery."
    mock_set_context.assert_awaited_once()
    context = mock_set_context.await_args.args[1]
    assert context.event_type == "parcel_delivery"
    assert context.event_summary == "Package left near the front door."
    assert context.event_context["camera_entity_id"] == "camera.front_door"
    turn = mock_handle_text_turn.await_args.args[0]
    assert turn.session_id == "s-followup"
    assert turn.user_text == "Is this urgent?"


@patch("avatar_backend.services.llm_service.LLMService.is_ready", new_callable=AsyncMock, return_value=True)
def test_chat_followup_event_404_when_event_unknown(mock_ready, client):
    resp = client.post(
        "/chat/followup-event",
        json={"session_id": "s-missing", "text": "What happened?", "event_id": "missing"},
        headers=HEADERS,
    )

    assert resp.status_code == 404
