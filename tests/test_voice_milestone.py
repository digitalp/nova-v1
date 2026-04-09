"""
Phase 4 milestone test — end-to-end voice WebSocket pipeline.

Strategy: inject mocked services via app.state so that the real run_chat
function executes but uses fast, controllable mocks (no LLM/HA needed).
This avoids unittest.mock.patch thread-propagation issues with TestClient.

Pipeline under test:
  binary audio → STT → run_chat(llm, sm, ha) → TTS → binary WAV + JSON msgs
"""
import asyncio
import io
import json
import wave
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from avatar_backend.middleware.auth import verify_api_key_ws
from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.models.messages import ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.routers.chat import router as chat_router
from avatar_backend.routers.voice import router as voice_router
from avatar_backend.services.conversation_service import ConversationService
from avatar_backend.services.realtime_voice_service import RealtimeVoiceService
from avatar_backend.services.session_manager import SessionManager
from avatar_backend.services.ws_manager import ConnectionManager


def _make_silent_wav(n_samples: int = 3200, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


def _build_test_app() -> FastAPI:
    """
    Minimal app with all services mocked via app.state.

    The real run_chat() calls:
      llm.chat(messages)           → AsyncMock returns ("I turned it on.", [])
      sm.add_message(...)          → AsyncMock (no-op)
      sm.get_messages(session_id)  → AsyncMock returns []
      ha.execute_tool_call(tc)     → not called (no tool calls from llm)

    STT and TTS are also mocked.
    """
    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(voice_router)

    # Bypass auth
    async def _noop_http_auth() -> None:
        pass
    async def _noop_auth(websocket: WebSocket) -> None:
        pass
    app.dependency_overrides[verify_api_key] = _noop_http_auth
    app.dependency_overrides[verify_api_key_ws] = _noop_auth

    # STT mock
    stt_mock = MagicMock()
    stt_mock.transcribe = AsyncMock(return_value="turn on the kitchen light")

    # TTS mock — returns valid silent WAV
    tts_mock = MagicMock()
    tts_mock.synthesise_with_timing = AsyncMock(return_value=(
        _make_silent_wav(n_samples=100, sample_rate=22050),
        [],
    ))

    # LLM mock — returns a plain text reply (no tool calls)
    llm_mock = MagicMock()
    llm_mock.model_name = "test-model"
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(
        return_value=("OK, I've turned on the kitchen light.", [])
    )

    # SessionManager mock
    sm_mock = MagicMock()
    sm_mock.add_message = AsyncMock()
    sm_mock.get_messages = AsyncMock(return_value=[])

    # HAProxy mock (not called in this flow — no tool calls)
    ha_mock = MagicMock()
    ha_mock.execute_tool_call = AsyncMock(
        return_value=ToolResult(success=True, message="done", entity_id="", service_called="", ha_status_code=200)
    )

    app.state.stt_service     = stt_mock
    app.state.tts_service     = tts_mock
    app.state.llm_service     = llm_mock
    app.state.session_manager = sm_mock
    app.state.ha_proxy        = ha_mock
    app.state.ws_manager      = ConnectionManager()
    app.state.realtime_voice_service = RealtimeVoiceService()
    app.state.conversation_service = ConversationService(app)
    app.state.decision_log = None
    app.state.memory_service = None
    app.state.recent_event_contexts = {}

    return app


def test_voice_ws_full_pipeline():
    """
    Full voice round-trip: WAV audio → transcript → LLM response → WAV audio back.

    Verifies:
      - initial idle state on connect
      - listening state when audio arrives
      - transcript message with correct content
      - thinking state during LLM call
      - response message with text + session_id
      - binary WAV audio in response
      - return to idle state
    """
    app = _build_test_app()

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice?api_key=test-key") as ws:
            # Initial state on connect
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            # Send audio
            ws.send_bytes(_make_silent_wav())

            # Collect messages
            states_seen: list[str] = []
            transcript_seen = False
            response_seen = False
            audio_received = False

            for _ in range(30):
                # Try to receive next message — break on error (disconnect)
                try:
                    data = ws.receive()
                except Exception:
                    break

                # ws.receive() returns an ASGI Message dict:
                #   {"type": "websocket.send", "text": "..."} for JSON frames
                #   {"type": "websocket.send", "bytes": b"..."} for binary frames
                if not isinstance(data, dict):
                    continue

                if data.get("bytes"):
                    audio_received = True
                    continue

                try:
                    msg = json.loads(data.get("text", ""))
                except Exception:
                    continue

                mtype = msg.get("type")
                if mtype == "state":
                    states_seen.append(msg["state"])
                    if msg["state"] == "idle" and response_seen:
                        break
                elif mtype == "transcript":
                    transcript_seen = True
                    assert "kitchen" in msg["text"].lower()
                elif mtype == "response":
                    response_seen = True
                    assert msg.get("text"), "response text should be non-empty"
                    assert "session_id" in msg
                elif mtype == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert "listening" in states_seen, f"expected 'listening', got {states_seen}"
    assert "thinking"  in states_seen, f"expected 'thinking', got {states_seen}"
    assert transcript_seen, "expected a transcript message"
    assert response_seen,   "expected a response message"
    assert audio_received,  "expected WAV audio bytes"


def test_voice_ws_negotiates_streamed_output_capabilities():
    app = _build_test_app()

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice?api_key=test-key") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"
            assert msg["input_streaming"] is True
            assert msg["output_streaming"] is True
            assert "pcm_s16le" in msg["output_audio_formats"]

            ws.send_text(json.dumps({
                "type": "client_capabilities",
                "output_streaming": True,
                "output_audio_format": "pcm_s16le",
            }))

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "client_capabilities_ack"
            assert msg["output_streaming"] is True
            assert msg["output_audio_format"] == "pcm_s16le"

            ws.send_bytes(_make_silent_wav())

            output_started = None
            output_ended = False
            chunk_count = 0

            for _ in range(40):
                data = ws.receive()
                if data.get("bytes"):
                    chunk_count += 1
                    continue

                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "output_audio_start":
                    output_started = msg
                elif msg.get("type") == "output_audio_end":
                    output_ended = True
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert output_started is not None, "expected output_audio_start metadata"
    assert output_started["audio_format"] == "pcm_s16le"
    assert output_started["sample_rate"] == 22050
    assert output_started["channels"] == 1
    assert output_started["sample_width_bytes"] == 2
    assert output_started["chunk_count"] >= 1
    assert chunk_count == output_started["chunk_count"]
    assert output_ended is True


def test_voice_ws_streamed_input_commit_combines_home_context_with_event_overlay():
    app = _build_test_app()

    streamed_audio = _make_silent_wav(n_samples=160, sample_rate=16000)
    split_at = len(streamed_audio) // 2
    first_chunk = streamed_audio[:split_at]
    second_chunk = streamed_audio[split_at:]

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(return_value="What should I do with this event?")

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(side_effect=[
        ("Driveway context captured.", []),
        ("This looks routine; you can leave it for now.", []),
    ])

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)
    app.state.recent_event_contexts["evt-streamed"] = (
        0.0,
        {
            "event_type": "parcel_delivery",
            "event_summary": "Package left near the driveway gate.",
            "event_context": {
                "camera_entity_id": "camera.driveway",
                "source": "parcel",
            },
        },
    )

    with TestClient(app) as client:
        chat_resp = client.post(
            "/chat",
            json={
                "session_id": "coordinator-streamed-overlay",
                "text": "Remember the driveway context.",
                "context": {"camera": "driveway", "severity": "normal"},
            },
            headers={"X-API-Key": "test-key"},
        )
        assert chat_resp.status_code == 200
        assert chat_resp.json()["text"] == "Driveway context captured."

        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-streamed-overlay") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"
            assert msg["input_streaming"] is True

            ws.send_text(json.dumps({
                "type": "turn_context",
                "event_id": "evt-streamed",
                "followup_prompt": "Focus on whether I need to act now.",
            }))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "turn_context_ack"
            assert ack["event_id"] == "evt-streamed"
            assert ack["followup_prompt"] == "Focus on whether I need to act now."

            ws.send_text(json.dumps({"type": "input_audio_start"}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "input_audio_start_ack"

            ws.send_bytes(first_chunk)
            ws.send_bytes(second_chunk)

            ws.send_text(json.dumps({"type": "input_audio_commit"}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "input_audio_commit_ack"
            assert ack["byte_length"] == len(streamed_audio)

            response_seen = False
            audio_received = False
            for _ in range(50):
                data = ws.receive()
                if data.get("bytes"):
                    audio_received = True
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "response":
                    response_seen = True
                elif msg.get("type") == "state" and msg.get("state") == "idle" and response_seen:
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert response_seen is True
    assert audio_received is True
    assert stt_mock.transcribe.await_count == 1
    assert stt_mock.transcribe.await_args.args[0] == streamed_audio
    assert llm_mock.chat.await_count == 2
    second_messages = llm_mock.chat.await_args_list[1].args[0]
    assert second_messages[-1]["role"] == "user"
    assert second_messages[-1]["content"] == (
        "What should I do with this event?\n\n[Home context]\n"
        "  camera: driveway\n"
        "  severity: normal\n\n"
        "[Event context]\n"
        "  type: parcel_delivery\n"
        "  summary: Package left near the driveway gate.\n"
        "  followup_prompt: Focus on whether I need to act now.\n"
        "  camera_entity_id: camera.driveway\n"
        "  source: parcel"
    )


def test_voice_ws_streamed_input_cancel_discards_buffer_before_next_turn():
    app = _build_test_app()

    stale_chunk = b"discard-me"
    fresh_audio = _make_silent_wav(n_samples=120, sample_rate=16000)

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(return_value="What changed after cancel?")

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(return_value=("Only the fresh audio was used.", []))

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-stream-cancel") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"
            assert msg["input_streaming"] is True

            ws.send_text(json.dumps({"type": "input_audio_start"}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "input_audio_start_ack"

            ws.send_bytes(stale_chunk)

            ws.send_text(json.dumps({"type": "input_audio_cancel"}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "input_audio_cancel_ack"
            assert ack["active"] is True

            ws.send_bytes(fresh_audio)

            response_seen = False
            audio_received = False
            for _ in range(40):
                data = ws.receive()
                if data.get("bytes"):
                    audio_received = True
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "response":
                    response_seen = True
                elif msg.get("type") == "state" and msg.get("state") == "idle" and response_seen:
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert response_seen is True
    assert audio_received is True
    assert stt_mock.transcribe.await_count == 1
    assert stt_mock.transcribe.await_args.args[0] == fresh_audio
    assert llm_mock.chat.await_count == 1


def test_voice_ws_second_turn_interrupts_first_turn_on_same_socket():
    app = _build_test_app()

    first_gate = asyncio.Event()
    second_gate = asyncio.Event()
    transcripts = ["first interrupted turn", "second winning turn"]

    async def transcribe(_audio: bytes) -> str:
        text = transcripts.pop(0)
        gate = first_gate if text == "first interrupted turn" else second_gate
        await gate.wait()
        return text

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(side_effect=transcribe)

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(return_value=("Handled only the latest turn.", []))

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-interrupt") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"

            ws.send_bytes(_make_silent_wav(n_samples=90))

            turn_started_ids: list[int] = []
            interrupted_turn_id = None
            turn_finished: dict[int, str] = {}
            response_turn_ids: list[int] = []
            audio_turn_ids: list[int] = []
            states_seen: list[tuple[int | None, str]] = []

            for _ in range(20):
                data = ws.receive()
                if data.get("bytes"):
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "turn_started":
                    turn_started_ids.append(msg["turn_id"])
                    if msg["turn_id"] == 1:
                        ws.send_bytes(_make_silent_wav(n_samples=100))
                        second_gate.set()
                elif msg.get("type") == "turn_interrupted":
                    interrupted_turn_id = msg["interrupted_turn_id"]
                    first_gate.set()
                elif msg.get("type") == "turn_finished":
                    turn_finished[msg["turn_id"]] = msg["reason"]
                    if (
                        turn_finished.get(2) == "completed"
                        and 2 in response_turn_ids
                    ):
                        break
                elif msg.get("type") == "response":
                    response_turn_ids.append(msg["turn_id"])
                    if msg["turn_id"] == 2:
                        assert msg["text"] == "Handled only the latest turn."
                    if (
                        msg["turn_id"] == 2
                        and turn_finished.get(2) == "completed"
                    ):
                        break
                elif msg.get("type") == "audio_start":
                    audio_turn_ids.append(msg["turn_id"])
                elif msg.get("type") == "state":
                    states_seen.append((msg.get("turn_id"), msg["state"]))
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert turn_started_ids == [1, 2]
    assert interrupted_turn_id == 1
    assert turn_finished[1] == "interrupted"
    assert turn_finished[2] == "completed"
    assert response_turn_ids == [2]
    assert audio_turn_ids == [2]
    listening_count = sum(1 for _, state in states_seen if state == "listening")
    assert listening_count >= 2
    assert (2, "thinking") in states_seen
    assert (2, "speaking") in states_seen
    assert (2, "idle") in states_seen
    assert stt_mock.transcribe.await_count == 2
    assert llm_mock.chat.await_count == 1


def test_voice_ws_uses_persisted_home_context_from_prior_chat_turn():
    app = _build_test_app()

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(return_value="What changed?")

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(side_effect=[
        ("Kitchen status captured.", []),
        ("The driveway still looks normal.", []),
    ])

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)

    with TestClient(app) as client:
        chat_resp = client.post(
            "/chat",
            json={
                "session_id": "coordinator-sticky",
                "text": "Remember the driveway context.",
                "context": {"camera": "driveway", "severity": "normal"},
            },
            headers={"X-API-Key": "test-key"},
        )
        assert chat_resp.status_code == 200
        assert chat_resp.json()["text"] == "Kitchen status captured."

        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-sticky") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"

            ws.send_bytes(_make_silent_wav())

            response_seen = False
            audio_received = False
            for _ in range(40):
                data = ws.receive()
                if data.get("bytes"):
                    audio_received = True
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "response":
                    response_seen = True
                elif msg.get("type") == "state" and msg.get("state") == "idle" and response_seen:
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert response_seen is True
    assert audio_received is True
    assert llm_mock.chat.await_count == 2
    second_messages = llm_mock.chat.await_args_list[1].args[0]
    assert second_messages[-1]["role"] == "user"
    assert second_messages[-1]["content"] == (
        "What changed?\n\n[Home context]\n"
        "  camera: driveway\n"
        "  severity: normal"
    )


def test_voice_ws_combines_persisted_home_context_with_event_followup_overlay():
    app = _build_test_app()

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(return_value="What should I do?")

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(side_effect=[
        ("Driveway context captured.", []),
        ("This looks like a normal delivery.", []),
    ])

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)
    app.state.recent_event_contexts["evt-1"] = (
        0.0,
        {
            "event_type": "parcel_delivery",
            "event_summary": "Package left near the driveway gate.",
            "event_context": {"camera_entity_id": "camera.driveway", "source": "parcel"},
        },
    )

    with TestClient(app) as client:
        chat_resp = client.post(
            "/chat",
            json={
                "session_id": "coordinator-overlay",
                "text": "Remember the driveway camera context.",
                "context": {"camera": "driveway", "severity": "normal"},
            },
            headers={"X-API-Key": "test-key"},
        )
        assert chat_resp.status_code == 200
        assert chat_resp.json()["text"] == "Driveway context captured."

        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-overlay") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"

            ws.send_text(json.dumps({
                "type": "turn_context",
                "event_id": "evt-1",
                "followup_prompt": "Focus on whether I need to act now.",
            }))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "turn_context_ack"
            assert ack["event_id"] == "evt-1"
            assert ack["followup_prompt"] == "Focus on whether I need to act now."

            ws.send_bytes(_make_silent_wav())

            response_seen = False
            audio_received = False
            for _ in range(40):
                data = ws.receive()
                if data.get("bytes"):
                    audio_received = True
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "response":
                    response_seen = True
                elif msg.get("type") == "state" and msg.get("state") == "idle" and response_seen:
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert response_seen is True
    assert audio_received is True
    assert llm_mock.chat.await_count == 2
    second_messages = llm_mock.chat.await_args_list[1].args[0]
    assert second_messages[-1]["role"] == "user"
    assert second_messages[-1]["content"] == (
        "What should I do?\n\n[Home context]\n"
        "  camera: driveway\n"
        "  severity: normal\n\n"
        "[Event context]\n"
        "  type: parcel_delivery\n"
        "  summary: Package left near the driveway gate.\n"
        "  followup_prompt: Focus on whether I need to act now.\n"
        "  camera_entity_id: camera.driveway\n"
        "  source: parcel"
    )


def test_voice_ws_does_not_reuse_home_context_after_chat_explicitly_clears_it():
    app = _build_test_app()

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(return_value="What changed?")

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(side_effect=[
        ("Driveway context captured.", []),
        ("Cleared context.", []),
        ("No sticky context remained.", []),
    ])

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)

    with TestClient(app) as client:
        set_resp = client.post(
            "/chat",
            json={
                "session_id": "coordinator-clear",
                "text": "Remember the driveway camera context.",
                "context": {"camera": "driveway", "severity": "normal"},
            },
            headers={"X-API-Key": "test-key"},
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["text"] == "Driveway context captured."

        clear_resp = client.post(
            "/chat",
            json={
                "session_id": "coordinator-clear",
                "text": "Clear that stored context.",
                "context": {},
            },
            headers={"X-API-Key": "test-key"},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["text"] == "Cleared context."

        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-clear") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"

            ws.send_bytes(_make_silent_wav())

            response_seen = False
            audio_received = False
            for _ in range(40):
                data = ws.receive()
                if data.get("bytes"):
                    audio_received = True
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "response":
                    response_seen = True
                elif msg.get("type") == "state" and msg.get("state") == "idle" and response_seen:
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert response_seen is True
    assert audio_received is True
    assert llm_mock.chat.await_count == 3
    third_messages = llm_mock.chat.await_args_list[2].args[0]
    assert third_messages[-1]["role"] == "user"
    assert third_messages[-1]["content"] == "What changed?"


def test_voice_ws_reuses_followup_event_context_seeded_by_chat_route():
    app = _build_test_app()

    stt_mock = app.state.stt_service
    stt_mock.transcribe = AsyncMock(return_value="And what should I do now?")

    llm_mock = app.state.llm_service
    llm_mock.is_ready = AsyncMock(return_value=True)
    llm_mock.chat = AsyncMock(side_effect=[
        ("The package looks routine.", []),
        ("You can leave it for now.", []),
    ])

    app.state.session_manager = SessionManager("System prompt")
    app.state.conversation_service = ConversationService(app)
    app.state.recent_event_contexts["evt-followup"] = (
        0.0,
        {
            "event_type": "parcel_delivery",
            "event_summary": "Package left near the front door.",
            "event_context": {
                "camera_entity_id": "camera.front_door",
                "source": "parcel",
                "captures": ["doorstep", "wide"],
            },
        },
    )

    with TestClient(app) as client:
        followup_resp = client.post(
            "/chat/followup-event",
            json={
                "session_id": "coordinator-followup-voice",
                "text": "Is this urgent?",
                "event_id": "evt-followup",
            },
            headers={"X-API-Key": "test-key"},
        )
        assert followup_resp.status_code == 200
        assert followup_resp.json()["text"] == "The package looks routine."

        with client.websocket_connect("/ws/voice?api_key=test-key&session_id=coordinator-followup-voice") as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "state"
            assert msg["state"] == "idle"

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "voice_capabilities"

            ws.send_bytes(_make_silent_wav())

            response_seen = False
            audio_received = False
            for _ in range(40):
                data = ws.receive()
                if data.get("bytes"):
                    audio_received = True
                    continue
                msg = json.loads(data.get("text", ""))
                if msg.get("type") == "response":
                    response_seen = True
                elif msg.get("type") == "state" and msg.get("state") == "idle" and response_seen:
                    break
                elif msg.get("type") == "error":
                    pytest.fail(f"Got error from server: {msg}")

    assert response_seen is True
    assert audio_received is True
    assert llm_mock.chat.await_count == 2
    second_messages = llm_mock.chat.await_args_list[1].args[0]
    assert second_messages[-1]["role"] == "user"
    assert second_messages[-1]["content"] == (
        "And what should I do now?\n\n[Event context]\n"
        "  type: parcel_delivery\n"
        "  summary: Package left near the front door.\n"
        "  camera_entity_id: camera.front_door\n"
        "  source: parcel\n"
        "  captures.0: doorstep\n"
        "  captures.1: wide"
    )
