from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from avatar_backend.services.conversation_service import (
    ConversationService,
    ConversationTurnRequest,
    EventFollowupRequest,
    PendingEventFollowupContext,
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
                "lights": ["kitchen", "hallway"],
                "climate": {"mode": "eco", "target": 21},
                "bad key": "ignored",
            },
        )
    )

    assert result == "ok"
    assert captured["session_id"] == "chat-1"
    assert captured["user_text"] == (
        "What is on?\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  active_device: TV Living room\n"
        "  lights.0: kitchen\n"
        "  lights.1: hallway\n"
        "  climate.mode: eco\n"
        "  climate.target: 21"
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
            event_context={
                "camera": "front_door",
                "severity": "normal",
                "captures": ["snapshot", "overview"],
                "source": {"entity_id": "camera.front_door"},
            },
        )
    )

    assert result == "ok"
    assert captured["session_id"] == "event-1"
    assert captured["user_text"] == (
        "What should I do?\n\n[Event context]\n"
        "  type: package_delivery\n"
        "  summary: Package left at front door\n"
        "  camera: front_door\n"
        "  severity: normal\n"
        "  captures.0: snapshot\n"
        "  captures.1: overview\n"
        "  source.entity_id: camera.front_door"
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


@pytest.mark.asyncio
async def test_home_context_persists_across_later_text_turns():
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

    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="sticky-text",
            user_text="Status?",
            context={"room": "Kitchen", "mode": "Evening"},
        )
    )
    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="sticky-text",
            user_text="What changed?",
        )
    )

    assert captured[0] == (
        "Status?\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  mode: Evening"
    )
    assert captured[1] == (
        "What changed?\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  mode: Evening"
    )


@pytest.mark.asyncio
async def test_home_context_merges_incremental_updates_across_turns():
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

    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="merge-text",
            user_text="Initial state?",
            context={"room": "Kitchen"},
        )
    )
    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="merge-text",
            user_text="Add mode.",
            context={"mode": "Evening"},
        )
    )
    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="merge-text",
            user_text="What changed?",
        )
    )

    assert captured[1] == (
        "Add mode.\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  mode: Evening"
    )
    assert captured[2] == (
        "What changed?\n\n[Home context]\n"
        "  room: Kitchen\n"
        "  mode: Evening"
    )


@pytest.mark.asyncio
async def test_empty_context_explicitly_clears_persisted_home_context():
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

    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="clear-text",
            user_text="Start with context.",
            context={"room": "Kitchen", "mode": "Evening"},
        )
    )
    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="clear-text",
            user_text="Clear it now.",
            context={},
        )
    )
    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="clear-text",
            user_text="What changed?",
        )
    )

    assert captured[1] == "Clear it now."
    assert captured[2] == "What changed?"


@pytest.mark.asyncio
async def test_voice_turn_uses_persisted_home_context_and_pending_event_overlay():
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

    await service.handle_text_turn(
        ConversationTurnRequest(
            session_id="sticky-voice",
            user_text="Use the driveway camera context.",
            context={"camera": "driveway", "severity": "normal"},
        )
    )
    await service.set_event_followup_context(
        "sticky-voice",
        PendingEventFollowupContext(
            event_type="vehicle_arrival",
            event_summary="A car pulled into the driveway",
            event_context={"source": "driveway_camera"},
        ),
    )
    await service.handle_voice_turn(session_id="sticky-voice", user_text="Who is that?")

    assert captured[1] == (
        "Who is that?\n\n[Home context]\n"
        "  camera: driveway\n"
        "  severity: normal\n\n"
        "[Event context]\n"
        "  type: vehicle_arrival\n"
        "  summary: A car pulled into the driveway\n"
        "  source: driveway_camera"
    )
