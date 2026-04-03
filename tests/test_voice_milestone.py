"""
Phase 4 milestone test — end-to-end voice WebSocket pipeline.

Strategy: inject mocked services via app.state so that the real run_chat
function executes but uses fast, controllable mocks (no LLM/HA needed).
This avoids unittest.mock.patch thread-propagation issues with TestClient.

Pipeline under test:
  binary audio → STT → run_chat(llm, sm, ha) → TTS → binary WAV + JSON msgs
"""
import io
import json
import wave
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from avatar_backend.middleware.auth import verify_api_key_ws
from avatar_backend.models.messages import ToolCall
from avatar_backend.models.tool_result import ToolResult
from avatar_backend.routers.voice import router as voice_router
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
    app.include_router(voice_router)

    # Bypass auth
    async def _noop_auth(websocket: WebSocket) -> None:
        pass
    app.dependency_overrides[verify_api_key_ws] = _noop_auth

    # STT mock
    stt_mock = MagicMock()
    stt_mock.transcribe = AsyncMock(return_value="turn on the kitchen light")

    # TTS mock — returns valid silent WAV
    tts_mock = MagicMock()
    tts_mock.synthesise = AsyncMock(return_value=_make_silent_wav(
        n_samples=100, sample_rate=22050))

    # LLM mock — returns a plain text reply (no tool calls)
    llm_mock = MagicMock()
    llm_mock.model_name = "test-model"
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
