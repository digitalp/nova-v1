"""
Phase 6 — /announce endpoint tests.

All HA speaker calls and TTS synthesis are mocked.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.routers.announce import router as announce_router
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.tts_service import TTSService
from avatar_backend.services.ws_manager import ConnectionManager


API_KEY = "test-key"

# ── Test app fixture ──────────────────────────────────────────────────────────

def _make_app(tts_wav=b"RIFF" + b"\x00" * 36, speaker_ok=True):
    app = FastAPI()
    app.include_router(announce_router)

    # Bypass auth — no parameters avoids FastAPI treating 'request' as a query param
    async def _noop_auth() -> None: pass
    app.dependency_overrides[verify_api_key] = _noop_auth

    tts_mock = MagicMock(spec=TTSService)
    tts_mock.synthesise = AsyncMock(return_value=tts_wav)

    speaker_mock = MagicMock(spec=SpeakerService)
    speaker_mock.is_configured = True
    if speaker_ok:
        speaker_mock.speak = AsyncMock()
    else:
        speaker_mock.speak = AsyncMock(side_effect=RuntimeError("speaker down"))

    ws_mock = MagicMock(spec=ConnectionManager)
    ws_mock.broadcast_json = AsyncMock()

    app.state.tts_service     = tts_mock
    app.state.speaker_service = speaker_mock
    app.state.ws_manager      = ws_mock

    return app, tts_mock, speaker_mock, ws_mock


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_announce_requires_auth():
    app, *_ = _make_app()
    # Remove the override so real auth runs
    app.dependency_overrides = {}
    client = TestClient(app)
    resp = client.post("/announce", json={"message": "Hello"})
    assert resp.status_code == 401


# ── Happy path ────────────────────────────────────────────────────────────────

def test_announce_normal_priority_returns_ok():
    app, tts_mock, speaker_mock, ws_mock = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/announce",
        json={"message": "Good morning everyone", "priority": "normal"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["message"] == "Good morning everyone"
    assert data["wav_bytes"] > 0


def test_announce_alert_priority_returns_ok():
    app, *_ = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/announce",
        json={"message": "Motion detected at front door", "priority": "alert"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_announce_calls_tts_synthesise():
    app, tts_mock, *_ = _make_app()
    client = TestClient(app)
    client.post("/announce", json={"message": "Dinner is ready"},
                headers={"X-API-Key": API_KEY})
    tts_mock.synthesise.assert_called_once_with("Dinner is ready")


def test_announce_calls_speaker_speak():
    app, _, speaker_mock, _ = _make_app()
    client = TestClient(app)
    client.post("/announce", json={"message": "Lights off in 5 minutes"},
                headers={"X-API-Key": API_KEY})
    speaker_mock.speak.assert_called_once_with("Lights off in 5 minutes")


# ── State broadcasts ──────────────────────────────────────────────────────────

def test_announce_normal_broadcasts_speaking_then_idle():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    client.post("/announce", json={"message": "Hello", "priority": "normal"},
                headers={"X-API-Key": API_KEY})

    calls = [c[0][0] for c in ws_mock.broadcast_json.call_args_list]
    states = [c["state"] for c in calls if c.get("type") == "avatar_state"]
    assert states[0] == "speaking"
    assert states[-1] == "idle"


def test_announce_alert_broadcasts_alert_then_speaking_then_idle():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    client.post("/announce", json={"message": "Alert!", "priority": "alert"},
                headers={"X-API-Key": API_KEY})

    calls = [c[0][0] for c in ws_mock.broadcast_json.call_args_list]
    states = [c["state"] for c in calls if c.get("type") == "avatar_state"]
    assert states[0] == "alert"
    assert "speaking" in states
    assert states[-1] == "idle"


# ── Validation ────────────────────────────────────────────────────────────────

def test_announce_empty_message_rejected():
    app, *_ = _make_app()
    client = TestClient(app)
    resp = client.post("/announce", json={"message": ""},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 422  # pydantic min_length=1


def test_announce_missing_message_rejected():
    app, *_ = _make_app()
    client = TestClient(app)
    resp = client.post("/announce", json={},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 422


def test_announce_invalid_priority_rejected():
    app, *_ = _make_app()
    client = TestClient(app)
    resp = client.post("/announce",
                       json={"message": "Hi", "priority": "urgent"},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 422


def test_announce_default_priority_is_normal():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    # Omit priority — should default to "normal"
    client.post("/announce", json={"message": "Test"},
                headers={"X-API-Key": API_KEY})
    calls = [c[0][0] for c in ws_mock.broadcast_json.call_args_list]
    states = [c["state"] for c in calls if c.get("type") == "avatar_state"]
    assert states[0] == "speaking"   # not "alert"


# ── Error handling ────────────────────────────────────────────────────────────

def test_announce_tts_failure_returns_503():
    app, tts_mock, *_ = _make_app()
    tts_mock.synthesise = AsyncMock(side_effect=RuntimeError("piper crashed"))
    client = TestClient(app)
    resp = client.post("/announce", json={"message": "Will fail"},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 503


def test_announce_tts_failure_broadcasts_error_then_idle():
    app, tts_mock, _, ws_mock = _make_app()
    tts_mock.synthesise = AsyncMock(side_effect=RuntimeError("piper crashed"))
    client = TestClient(app)
    client.post("/announce", json={"message": "Will fail"},
                headers={"X-API-Key": API_KEY})
    calls = [c[0][0] for c in ws_mock.broadcast_json.call_args_list]
    states = [c["state"] for c in calls if c.get("type") == "avatar_state"]
    assert "error" in states
    assert states[-1] == "idle"


def test_announce_speaker_failure_does_not_return_error():
    """A speaker error should be logged but not fail the announce call."""
    app, _, speaker_mock, _ = _make_app(speaker_ok=False)
    client = TestClient(app)
    resp = client.post("/announce", json={"message": "Speaker broken"},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200  # still succeeds


def test_announce_no_speaker_configured_still_succeeds():
    app, tts_mock, speaker_mock, _ = _make_app()
    speaker_mock.is_configured = False
    client = TestClient(app)
    resp = client.post("/announce", json={"message": "No speakers"},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    speaker_mock.speak.assert_not_called()
