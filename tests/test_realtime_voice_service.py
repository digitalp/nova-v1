import asyncio
import io
import json
import wave
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from avatar_backend.services.realtime_voice_service import (
    AnthropicChatRealtimeVoiceAdapter,
    DefaultRealtimeVoiceAdapter,
    GoogleChatRealtimeVoiceAdapter,
    IDLE,
    LISTENING,
    OpenAIChatRealtimeVoiceAdapter,
    SPEAKING,
    THINKING,
    RealtimeVoiceService,
    VoiceTurnContext,
    VoiceTurnResult,
    create_realtime_voice_adapter,
)
from avatar_backend.services.ws_manager import ConnectionManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.text_messages: list[str] = []
        self.binary_messages: list[bytes] = []

    async def send_text(self, text: str) -> None:
        self.text_messages.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.binary_messages.append(data)


def _messages_of_type(ws: FakeWebSocket, message_type: str) -> list[dict]:
    return [
        json.loads(message)
        for message in ws.text_messages
        if json.loads(message).get("type") == message_type
    ]


def _pcm_wav_bytes(sample_count: int = 4000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x01\x02" * sample_count)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_send_pong_if_needed_handles_ping():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()

    handled = await service.send_pong_if_needed(ws, json.dumps({"type": "ping"}))

    assert handled is True
    assert json.loads(ws.text_messages[0]) == {"type": "pong"}


@pytest.mark.asyncio
async def test_send_initial_state_includes_adapter_metadata():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    ws.app = SimpleNamespace(
        state=SimpleNamespace(
            realtime_voice_adapter=OpenAIChatRealtimeVoiceAdapter(),
        )
    )

    await service.send_initial_state(ws, ws_mgr)

    state_msg = json.loads(ws.text_messages[0])
    caps_msg = json.loads(ws.text_messages[1])
    assert state_msg == {"type": "state", "state": "idle"}
    assert caps_msg["type"] == "voice_capabilities"
    assert caps_msg["realtime_adapter"] == "openai_chat_compat"
    assert caps_msg["realtime_provider"] == "openai"
    assert caps_msg["native_audio_input"] is False
    assert caps_msg["native_audio_output"] is False
    assert caps_msg["input_streaming"] is True
    assert caps_msg["output_streaming"] is True
    assert caps_msg["turn_context"] is True
    assert caps_msg["output_audio_formats"] == ["wav", "pcm_s16le"]


def test_create_realtime_voice_adapter_selects_openai_chat_compat():
    settings = SimpleNamespace(llm_provider="openai", openai_api_key="sk-test")

    adapter = create_realtime_voice_adapter(settings)

    assert isinstance(adapter, OpenAIChatRealtimeVoiceAdapter)


def test_create_realtime_voice_adapter_selects_google_chat_compat():
    settings = SimpleNamespace(
        llm_provider="google",
        openai_api_key="",
        google_api_key="google-key",
        anthropic_api_key="",
    )

    adapter = create_realtime_voice_adapter(settings)

    assert isinstance(adapter, GoogleChatRealtimeVoiceAdapter)


def test_create_realtime_voice_adapter_selects_anthropic_chat_compat():
    settings = SimpleNamespace(
        llm_provider="anthropic",
        openai_api_key="",
        google_api_key="",
        anthropic_api_key="anthropic-key",
    )

    adapter = create_realtime_voice_adapter(settings)

    assert isinstance(adapter, AnthropicChatRealtimeVoiceAdapter)


def test_create_realtime_voice_adapter_defaults_to_compat_adapter():
    settings = SimpleNamespace(
        llm_provider="ollama",
        openai_api_key="",
        google_api_key="",
        anthropic_api_key="",
    )

    adapter = create_realtime_voice_adapter(settings)

    assert isinstance(adapter, DefaultRealtimeVoiceAdapter)


@pytest.mark.asyncio
async def test_send_initial_state_uses_custom_adapter_capabilities():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()
    adapter = MagicMock()
    adapter.adapter_name = "wav_only_adapter"
    adapter.provider_name = "custom"
    adapter.supports_native_audio_input = False
    adapter.supports_native_audio_output = False
    adapter.supports_input_streaming = False
    adapter.supports_output_streaming = False
    adapter.supports_turn_context = False
    adapter.supported_output_audio_formats = ("wav",)
    ws.app = SimpleNamespace(
        state=SimpleNamespace(
            realtime_voice_adapter=adapter,
        )
    )

    await service.send_initial_state(ws, ws_mgr)

    caps_msg = json.loads(ws.text_messages[1])
    assert caps_msg["type"] == "voice_capabilities"
    assert caps_msg["realtime_adapter"] == "wav_only_adapter"
    assert caps_msg["input_streaming"] is False
    assert caps_msg["output_streaming"] is False
    assert caps_msg["turn_context"] is False
    assert caps_msg["output_audio_formats"] == ["wav"]


@pytest.mark.asyncio
async def test_handle_text_frame_rejects_streamed_input_when_adapter_disables_it():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    adapter = MagicMock()
    adapter.supports_input_streaming = False
    ws.app = SimpleNamespace(state=SimpleNamespace(realtime_voice_adapter=adapter))

    handled = await service.handle_text_frame(
        ws,
        "voice_test:socket",
        json.dumps({"type": "input_audio_start"}),
    )

    assert handled is True
    error = _messages_of_type(ws, "error")[0]
    assert error["detail"] == "This voice adapter does not support streamed input."


@pytest.mark.asyncio
async def test_handle_text_frame_falls_back_to_supported_output_format():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    adapter = MagicMock()
    adapter.supports_output_streaming = True
    adapter.supported_output_audio_formats = ("wav",)
    ws.app = SimpleNamespace(state=SimpleNamespace(realtime_voice_adapter=adapter))

    handled = await service.handle_text_frame(
        ws,
        "voice_test:socket",
        json.dumps({
            "type": "client_capabilities",
            "output_streaming": True,
            "output_audio_format": "pcm_s16le",
        }),
    )

    assert handled is True
    ack = _messages_of_type(ws, "client_capabilities_ack")[0]
    assert ack["output_streaming"] is True
    assert ack["output_audio_format"] == "wav"


@pytest.mark.asyncio
async def test_process_audio_happy_path_emits_expected_messages():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value="turn on the kitchen light")

    tts = MagicMock()
    tts.synthesise_with_timing = AsyncMock(return_value=(b"RIFF" + b"\x00" * 40, []))

    speaker = None

    llm = MagicMock()
    session_manager = MagicMock()
    session_manager.clear = AsyncMock()
    ha_proxy = MagicMock()
    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_service=MagicMock(),
            llm_service=llm,
            session_manager=session_manager,
            ha_proxy=ha_proxy,
            decision_log=None,
            memory_service=None,
        )
    )
    ws.app = app

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=speaker,
        app=app,
    )

    fake_result = SimpleNamespace(
        text="OK, I've turned on the kitchen light.",
        session_id="voice_test",
        tool_calls=[],
        processing_time_ms=123,
    )

    app.state.conversation_service.handle_voice_turn = AsyncMock(return_value=fake_result)
    try:
        await service.connect_session("voice_test:socket")
        await service.start_audio_turn("voice_test:socket", ctx, b"fake-audio")
        await asyncio.sleep(0.05)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    message_types = [json.loads(m)["type"] for m in ws.text_messages]
    assert "transcript" in message_types
    assert "response" in message_types
    assert "audio_start" in message_types
    assert "word_timings" in message_types
    assert "turn_started" in message_types
    assert "turn_finished" in message_types

    states = [json.loads(m)["state"] for m in ws.text_messages if json.loads(m).get("type") == "state"]
    assert states[0] == LISTENING
    assert THINKING in states
    assert SPEAKING in states
    assert states[-1] == IDLE
    for message_type in ("turn_started", "transcript", "response", "audio_start", "word_timings", "turn_finished"):
        for payload in _messages_of_type(ws, message_type):
            assert payload["turn_id"] == 1
    assert _messages_of_type(ws, "turn_finished")[0]["reason"] == "completed"
    assert ws.binary_messages


@pytest.mark.asyncio
async def test_start_audio_turn_cancels_prior_turn_before_reply():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    first_gate = asyncio.Event()
    second_gate = asyncio.Event()
    transcripts = ["first turn", "second turn"]

    async def transcribe(_audio: bytes) -> str:
        text = transcripts.pop(0)
        gate = first_gate if text == "first turn" else second_gate
        await gate.wait()
        return text

    stt = MagicMock()
    stt.transcribe = AsyncMock(side_effect=transcribe)

    tts = MagicMock()
    tts.synthesise_with_timing = AsyncMock(return_value=(b"RIFF" + b"\x00" * 40, []))

    session_manager = MagicMock()
    session_manager.clear = AsyncMock()
    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_service=MagicMock(),
            llm_service=MagicMock(),
            session_manager=session_manager,
            ha_proxy=MagicMock(),
            decision_log=None,
            memory_service=None,
        )
    )
    ws.app = app

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=None,
        app=app,
    )

    fake_result = SimpleNamespace(
        text="Handled the latest turn.",
        session_id="voice_test",
        tool_calls=[],
        processing_time_ms=55,
    )

    app.state.conversation_service.handle_voice_turn = AsyncMock(return_value=fake_result)
    try:
        await service.connect_session("voice_test:socket")
        await service.start_audio_turn("voice_test:socket", ctx, b"first")
        await asyncio.sleep(0)
        await service.start_audio_turn("voice_test:socket", ctx, b"second")
        second_gate.set()
        await asyncio.sleep(0.05)
        first_gate.set()
        await asyncio.sleep(0.05)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    responses = _messages_of_type(ws, "response")
    interruptions = _messages_of_type(ws, "turn_interrupted")
    audio_starts = _messages_of_type(ws, "audio_start")
    turn_started = _messages_of_type(ws, "turn_started")
    turn_finished = _messages_of_type(ws, "turn_finished")

    assert len(interruptions) == 1
    assert interruptions[0]["interrupted_turn_id"] == 1
    assert [payload["turn_id"] for payload in turn_started] == [1, 2]
    assert len(turn_finished) == 2
    assert turn_finished[0]["turn_id"] == 1
    assert turn_finished[0]["reason"] == "interrupted"
    assert turn_finished[1]["turn_id"] == 2
    assert turn_finished[1]["reason"] == "completed"
    assert responses[0]["turn_id"] == 2
    assert audio_starts[0]["turn_id"] == 2
    assert len(responses) == 1
    assert responses[0]["text"] == "Handled the latest turn."


@pytest.mark.asyncio
async def test_turn_context_routes_next_voice_turn_to_event_followup():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value="What should I do?")

    tts = MagicMock()
    tts.synthesise_with_timing = AsyncMock(return_value=(b"RIFF" + b"\x00" * 40, []))

    conversation_service = MagicMock()
    conversation_service.set_event_followup_context = AsyncMock()
    conversation_service.handle_voice_turn = AsyncMock(return_value=SimpleNamespace(
        text="This looks like a normal delivery.",
        session_id="voice_test",
        tool_calls=[],
        processing_time_ms=64,
    ))

    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_service=conversation_service,
            llm_service=MagicMock(),
            session_manager=MagicMock(),
            ha_proxy=MagicMock(),
            decision_log=None,
            memory_service=None,
            recent_event_contexts={
                "evt-1": (
                    0.0,
                    {
                        "event_type": "parcel_delivery",
                        "event_summary": "Package left near the front door.",
                        "event_context": {"camera_entity_id": "camera.front_door"},
                    },
                )
            },
        )
    )
    ws.app = app

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=None,
        app=app,
    )

    await service.connect_session("voice_test:socket")
    try:
        handled = await service.handle_text_frame(
            ws,
            "voice_test:socket",
            json.dumps({"type": "turn_context", "event_id": "evt-1"}),
        )
        assert handled is True

        await service.start_audio_turn("voice_test:socket", ctx, b"fake-audio")
        await asyncio.sleep(0.05)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    conversation_service.set_event_followup_context.assert_awaited_once()
    conversation_service.handle_voice_turn.assert_awaited_once()
    ack = _messages_of_type(ws, "turn_context_ack")[0]
    assert ack["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_streaming_input_buffers_binary_frames_until_commit():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value="streamed turn")

    tts = MagicMock()
    tts.synthesise_with_timing = AsyncMock(return_value=(b"RIFF" + b"\x00" * 40, []))

    conversation_service = MagicMock()
    conversation_service.handle_voice_turn = AsyncMock(return_value=SimpleNamespace(
        text="Handled streamed input.",
        session_id="voice_test",
        tool_calls=[],
        processing_time_ms=33,
    ))

    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_service=conversation_service,
            llm_service=MagicMock(),
            session_manager=MagicMock(),
            ha_proxy=MagicMock(),
            decision_log=None,
            memory_service=None,
            recent_event_contexts={},
            stt_service=stt,
            tts_service=tts,
            ws_manager=ws_mgr,
            speaker_service=None,
        )
    )
    ws.app = app
    ws._nova_session_id = "voice_test"

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=None,
        app=app,
    )

    await service.connect_session("voice_test:socket")
    try:
        handled = await service.handle_text_frame(
            ws,
            "voice_test:socket",
            json.dumps({"type": "input_audio_start"}),
        )
        assert handled is True

        await service.handle_binary_frame("voice_test:socket", ctx, b"chunk-1")
        await service.handle_binary_frame("voice_test:socket", ctx, b"chunk-2")

        handled = await service.handle_text_frame(
            ws,
            "voice_test:socket",
            json.dumps({"type": "input_audio_commit"}),
        )
        assert handled is True
        await asyncio.sleep(0.05)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    stt.transcribe.assert_awaited_once_with(b"chunk-1chunk-2")
    assert _messages_of_type(ws, "input_audio_start_ack")
    commit_ack = _messages_of_type(ws, "input_audio_commit_ack")[0]
    assert commit_ack["byte_length"] == len(b"chunk-1chunk-2")
    assert _messages_of_type(ws, "turn_started")[0]["turn_id"] == 1
    assert _messages_of_type(ws, "response")[0]["text"] == "Handled streamed input."


@pytest.mark.asyncio
async def test_streaming_input_cancel_discards_buffered_audio():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value="should not run")

    tts = MagicMock()
    tts.synthesise_with_timing = AsyncMock(return_value=(b"RIFF" + b"\x00" * 40, []))

    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_service=MagicMock(),
            llm_service=MagicMock(),
            session_manager=MagicMock(),
            ha_proxy=MagicMock(),
            decision_log=None,
            memory_service=None,
            recent_event_contexts={},
            stt_service=stt,
            tts_service=tts,
            ws_manager=ws_mgr,
            speaker_service=None,
        )
    )
    ws.app = app
    ws._nova_session_id = "voice_test"

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=None,
        app=app,
    )

    await service.connect_session("voice_test:socket")
    try:
        await service.handle_text_frame(
            ws,
            "voice_test:socket",
            json.dumps({"type": "input_audio_start"}),
        )
        await service.handle_binary_frame("voice_test:socket", ctx, b"chunk-1")

        handled = await service.handle_text_frame(
            ws,
            "voice_test:socket",
            json.dumps({"type": "input_audio_cancel"}),
        )
        assert handled is True
        await asyncio.sleep(0.01)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    stt.transcribe.assert_not_called()
    cancel_ack = _messages_of_type(ws, "input_audio_cancel_ack")[0]
    assert cancel_ack["active"] is True


@pytest.mark.asyncio
async def test_client_output_streaming_sends_chunked_audio_messages():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value="stream output")

    tts = MagicMock()
    wav_bytes = _pcm_wav_bytes(20000)
    tts.synthesise_with_timing = AsyncMock(return_value=(wav_bytes, []))

    conversation_service = MagicMock()
    conversation_service.handle_voice_turn = AsyncMock(return_value=SimpleNamespace(
        text="Streaming output reply.",
        session_id="voice_test",
        tool_calls=[],
        processing_time_ms=41,
    ))

    app = SimpleNamespace(
        state=SimpleNamespace(
            conversation_service=conversation_service,
            llm_service=MagicMock(),
            session_manager=MagicMock(),
            ha_proxy=MagicMock(),
            decision_log=None,
            memory_service=None,
            recent_event_contexts={},
        )
    )
    ws.app = app

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=None,
        app=app,
    )

    await service.connect_session("voice_test:socket")
    try:
        handled = await service.handle_text_frame(
            ws,
            "voice_test:socket",
            json.dumps({
                "type": "client_capabilities",
                "output_streaming": True,
                "output_audio_format": "pcm_s16le",
            }),
        )
        assert handled is True

        await service.start_audio_turn("voice_test:socket", ctx, b"fake-audio")
        await asyncio.sleep(0.05)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    stream_start = _messages_of_type(ws, "output_audio_start")[0]
    stream_end = _messages_of_type(ws, "output_audio_end")[0]
    capabilities_ack = _messages_of_type(ws, "client_capabilities_ack")[0]
    assert capabilities_ack["output_streaming"] is True
    assert capabilities_ack["output_audio_format"] == "pcm_s16le"
    assert stream_start["turn_id"] == 1
    assert stream_start["audio_format"] == "pcm_s16le"
    assert stream_start["sample_rate"] == 22050
    assert stream_start["channels"] == 1
    assert stream_start["sample_width_bytes"] == 2
    assert stream_start["byte_length"] == len(wav_bytes) - 44
    assert stream_start["chunk_count"] == len(ws.binary_messages)
    assert stream_end["turn_id"] == 1
    assert len(ws.binary_messages) > 1


@pytest.mark.asyncio
async def test_client_output_streaming_starts_with_first_sentence_before_full_reply_audio():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    tts = MagicMock()
    first_wav = _pcm_wav_bytes(4000)
    rest_wav = _pcm_wav_bytes(6000)
    tts.synthesise_with_timing = AsyncMock(side_effect=[
        (first_wav, [{"word": "Hello", "start_ms": 0, "end_ms": 300}]),
        (rest_wav, [{"word": "there", "start_ms": 0, "end_ms": 300}]),
    ])

    app = SimpleNamespace(
        state=SimpleNamespace(
            speaker_service=None,
        )
    )
    ws.app = app

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=MagicMock(),
        tts=tts,
        speaker=None,
        app=app,
    )

    session = await service._get_or_create_session("voice_test:socket")
    session.current_turn_id = 1
    session.output_streaming_enabled = True
    session.output_audio_format = "pcm_s16le"

    streamed = await service._send_sentence_first_audio(
        ctx,
        DefaultRealtimeVoiceAdapter(),
        session_key="voice_test:socket",
        turn_id=1,
        reply_text="Hello there, I have started processing your request. This is the rest of a longer reply for streaming, with enough extra detail to trigger sentence-first audio delivery.",
        offset_s=0.0,
    )

    assert streamed is True
    assert tts.synthesise_with_timing.await_count == 2
    assert _messages_of_type(ws, "output_audio_start")
    assert _messages_of_type(ws, "output_audio_end")


@pytest.mark.asyncio
async def test_realtime_voice_service_uses_custom_app_adapter():
    service = RealtimeVoiceService()
    ws = FakeWebSocket()
    ws_mgr = MagicMock(spec=ConnectionManager)
    ws_mgr.broadcast_json = AsyncMock()

    custom_adapter = MagicMock(spec=DefaultRealtimeVoiceAdapter)
    custom_adapter.transcribe = AsyncMock(return_value="adapter transcript")
    custom_adapter.run_turn = AsyncMock(return_value=VoiceTurnResult(
        text="Adapter reply.",
        session_id="voice_test",
        tool_calls=[],
        processing_time_ms=12,
    ))
    custom_adapter.synthesise_reply = AsyncMock(return_value=(b"RIFF" + b"\x00" * 40, []))

    stt = MagicMock()
    stt.transcribe = AsyncMock(return_value="legacy transcript")

    tts = MagicMock()
    tts.synthesise_with_timing = AsyncMock(return_value=(b"legacy", []))

    conversation_service = MagicMock()
    conversation_service.handle_voice_turn = AsyncMock()

    app = SimpleNamespace(
        state=SimpleNamespace(
            realtime_voice_adapter=custom_adapter,
            conversation_service=conversation_service,
            llm_service=MagicMock(),
            session_manager=MagicMock(),
            ha_proxy=MagicMock(),
            decision_log=None,
            memory_service=None,
            recent_event_contexts={},
        )
    )
    ws.app = app

    ctx = VoiceTurnContext(
        ws=ws,
        ws_mgr=ws_mgr,
        session_id="voice_test",
        stt=stt,
        tts=tts,
        speaker=None,
        app=app,
    )

    await service.connect_session("voice_test:socket")
    try:
        await service.start_audio_turn("voice_test:socket", ctx, b"adapter-audio")
        await asyncio.sleep(0.05)
    finally:
        with suppress(Exception):
            await service.disconnect_session("voice_test:socket")

    custom_adapter.transcribe.assert_awaited_once_with(ctx, b"adapter-audio")
    custom_adapter.run_turn.assert_awaited_once()
    custom_adapter.synthesise_reply.assert_awaited_once_with(ctx, "Adapter reply.")
    stt.transcribe.assert_not_called()
    tts.synthesise_with_timing.assert_not_called()
    conversation_service.handle_voice_turn.assert_not_called()
    assert _messages_of_type(ws, "response")[0]["text"] == "Adapter reply."
