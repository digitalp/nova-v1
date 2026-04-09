from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services.conversation_service import (
    ConversationService,
    ConversationTurnRequest,
    EventFollowupRequest,
)


@pytest.mark.asyncio
async def test_handle_text_turn_injects_sanitized_context():
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_service=object(),
            session_manager=object(),
            ha_proxy=object(),
            decision_log=None,
            memory_service=None,
        )
    )
    service = ConversationService(app)

    captured: dict = {}

    async def fake_run_turn(*, session_id: str, user_text: str):
        captured["session_id"] = session_id
        captured["user_text"] = user_text
        return "ok"

    service._run_turn = AsyncMock(side_effect=fake_run_turn)

    result = await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="chat-1",
            user_text="What is on?",
            context={
                "room": "Kitchen",
                "active_device": "TV\nLiving room",
                "bad key": "ignored",
            },
        )
    )

    assert result == "ok"
    assert captured["session_id"] == "chat-1"
    assert captured["user_text"] == (
        "What is on?\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  active_device: TV Living room"
    )


@pytest.mark.asyncio
async def test_handle_voice_turn_uses_raw_text():
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_service=object(),
            session_manager=object(),
            ha_proxy=object(),
            decision_log=None,
            memory_service=None,
        )
    )
    service = ConversationService(app)
    service._run_turn = AsyncMock(return_value="ok")

    result = await service.handle_voice_turn(session_id="voice-1", user_text="turn on the light")

    assert result == "ok"
    service._run_turn.assert_awaited_once_with(
        session_id="voice-1",
        user_text="turn on the light",
    )


@pytest.mark.asyncio
async def test_handle_event_followup_injects_event_context():
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_service=object(),
            session_manager=object(),
            ha_proxy=object(),
            decision_log=None,
            memory_service=None,
        )
    )
    service = ConversationService(app)

    captured: dict = {}

    async def fake_run_turn(*, session_id: str, user_text: str):
        captured["session_id"] = session_id
        captured["user_text"] = user_text
        return "ok"

    service._run_turn = AsyncMock(side_effect=fake_run_turn)

    result = await service.handle_event_followup(
        EventFollowupRequest(
            session_id="event-1",
            user_text="What should I do?",
            event_type="package_delivery",
            event_summary="Package left at front door",
            event_context={"camera": "front_door", "severity": "normal"},
        )
    )

    assert result == "ok"
    assert captured["session_id"] == "event-1"
    assert captured["user_text"] == (
        "What should I do?\n\n[Event context]\n"
        "  type: package_delivery\n"
        "  summary: Package left at front door\n"
        "  camera: front_door\n"
        "  severity: normal"
    )


@pytest.mark.asyncio
async def test_pending_event_context_is_consumed_once_by_text_turn():
    app = SimpleNamespace(
        state=SimpleNamespace(
            llm_service=object(),
            session_manager=object(),
            ha_proxy=object(),
            decision_log=None,
            memory_service=None,
        )
    )
    service = ConversationService(app)

    captured: list[str] = []

    async def fake_run_turn(*, session_id: str, user_text: str):
        captured.append(user_text)
        return "ok"

    service._run_turn = AsyncMock(side_effect=fake_run_turn)

    await service.handle_event_followup(
        EventFollowupRequest(
            session_id="event-2",
            user_text="Who is there?",
            event_type="doorbell",
            event_summary="Someone is at the front door",
            event_context={"camera": "front_door"},
        )
    )

    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="event-2",
            user_text="And what are they carrying?",
        )
    )

    assert captured[0] == (
        "Who is there?\n\n[Event context]\n"
        "  type: doorbell\n"
        "  summary: Someone is at the front door\n"
        "  camera: front_door"
    )
    assert captured[1] == "And what are they carrying?"
