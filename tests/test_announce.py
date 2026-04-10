"""
Phase 6 — /announce endpoint tests.

All HA speaker calls and TTS synthesis are mocked.
"""
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from avatar_backend.middleware.auth import verify_api_key
from avatar_backend.routers.announce import router as announce_router
from avatar_backend.services.camera_event_service import CameraEventService
from avatar_backend.services.event_service import EventService
from avatar_backend.services.speaker_service import SpeakerService
from avatar_backend.services.surface_state_service import SurfaceStateService
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
    tts_mock.synthesise_with_timing = AsyncMock(return_value=(tts_wav, []))
    tts_mock.synthesise = AsyncMock(return_value=tts_wav)

    speaker_mock = MagicMock(spec=SpeakerService)
    speaker_mock.is_configured = True
    if speaker_ok:
        speaker_mock.speak = AsyncMock()
    else:
        speaker_mock.speak = AsyncMock(side_effect=RuntimeError("speaker down"))

    ws_mock = MagicMock(spec=ConnectionManager)
    ws_mock.broadcast_json = AsyncMock()
    ws_mock.broadcast_to_voice_json = AsyncMock()
    ws_mock.broadcast_to_voice_bytes = AsyncMock()

    app.state.tts_service     = tts_mock
    app.state.speaker_service = speaker_mock
    app.state.ws_manager      = ws_mock
    app.state.audio_cache     = {}
    app.state.ha_proxy        = MagicMock()
    app.state.ha_proxy.resolve_camera_entity = MagicMock(side_effect=lambda entity_id: entity_id)
    app.state.ha_proxy.fetch_camera_image = AsyncMock(return_value=b"fake-image")
    app.state.llm_service     = MagicMock()
    app.state.llm_service.describe_image = AsyncMock(return_value="A visitor is standing at the front door.")
    app.state.llm_service.describe_image_with_gemini = AsyncMock(return_value="A person is moving near the entrance.")
    app.state.motion_clip_service = MagicMock()
    app.state.motion_clip_service.schedule_capture = MagicMock()
    app.state.motion_announce_cooldowns = {}
    app.state.recent_event_contexts = {}
    app.state.surface_state_service = SurfaceStateService()
    app.state.event_service = EventService()
    app.state.camera_event_service = CameraEventService(
        ha_proxy=app.state.ha_proxy,
        llm_service=app.state.llm_service,
        event_service=app.state.event_service,
    )

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
    tts_mock.synthesise_with_timing.assert_called_once_with("Dinner is ready")


def test_announce_calls_speaker_speak():
    app, _, speaker_mock, _ = _make_app()
    client = TestClient(app)
    client.post("/announce", json={"message": "Lights off in 5 minutes"},
                headers={"X-API-Key": API_KEY})
    assert speaker_mock.speak.await_count + speaker_mock.speak_wav.await_count == 1
    if speaker_mock.speak.await_count:
        speaker_mock.speak.assert_called_once_with("Lights off in 5 minutes")
    else:
        args = speaker_mock.speak_wav.await_args.args
        assert args[0] == "Lights off in 5 minutes"
        assert args[1].startswith("http")


def test_announce_passes_target_area_to_speaker_service():
    app, _, speaker_mock, _ = _make_app()
    client = TestClient(app)
    client.post(
        "/announce",
        json={"message": "Bedroom reminder", "priority": "normal", "target_areas": ["Main Bedroom"]},
        headers={"X-API-Key": API_KEY},
    )
    if speaker_mock.speak.await_count:
        assert speaker_mock.speak.await_args.kwargs["target_areas"] == ["Main Bedroom"]
        assert speaker_mock.speak.await_args.kwargs["area_aware"] is True
    else:
        assert speaker_mock.speak_wav.await_args.kwargs["target_areas"] == ["Main Bedroom"]
        assert speaker_mock.speak_wav.await_args.kwargs["area_aware"] is True


def test_announce_broadcasts_voice_payload_and_audio():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    client.post("/announce", json={"message": "Hello there"},
                headers={"X-API-Key": API_KEY})

    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    payload = next(call for call in voice_calls if call.get("type") == "announce")
    assert payload["type"] == "announce"
    assert payload["text"] == "Hello there"
    ws_mock.broadcast_to_voice_bytes.assert_called_once()


def test_doorbell_announce_emits_visual_event_before_speaking():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    resp = client.post("/announce/doorbell", json={}, headers={"X-API-Key": API_KEY})

    assert resp.status_code == 200
    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    visual_idx = next(i for i, call in enumerate(voice_calls) if call.get("type") == "visual_event")
    announce_idx = next(i for i, call in enumerate(voice_calls) if call.get("type") == "announce")
    assert voice_calls[visual_idx]["event"] == "doorbell"
    assert voice_calls[visual_idx]["camera_entity_id"] == "camera.reolink_video_doorbell_poe_fluent"
    assert visual_idx < announce_idx


def test_visual_event_endpoint_broadcasts_static_images():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/announce/visual",
        json={
            "event": "bins",
            "title": "Bin Collection Today",
            "message": "Put out the blue and green bins.",
            "image_urls": [
                "/static/bin-icons/blue-bin.svg",
                "/static/bin-icons/green-bin.svg",
            ],
            "expires_in_ms": 45000,
        },
        headers={"X-API-Key": API_KEY},
    )

    assert resp.status_code == 200
    assert resp.json()["event"] == "bins"
    assert resp.json()["event_id"]
    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    payload = next(call for call in voice_calls if call.get("type") == "visual_event")
    assert payload["type"] == "visual_event"
    assert payload["event_id"] == resp.json()["event_id"]
    assert payload["event"] == "bins"
    assert payload["image_urls"] == [
        "/static/bin-icons/blue-bin.svg",
        "/static/bin-icons/green-bin.svg",
    ]


def test_visual_event_endpoint_stores_followup_context():
    app, _, _, _ = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/announce/visual",
        json={
            "event": "parcel_delivery",
            "title": "Parcel Delivered",
            "message": "Package left near the front door.",
            "camera_entity_id": "camera.front_door",
            "event_context": {"camera_entity_id": "camera.front_door", "source": "parcel"},
        },
        headers={"X-API-Key": API_KEY},
    )

    assert resp.status_code == 200
    event_id = resp.json()["event_id"]
    _, stored = app.state.recent_event_contexts[event_id]
    assert stored["event_type"] == "parcel_delivery"
    assert stored["event_summary"] == "Package left near the front door."
    assert stored["event_context"]["camera_entity_id"] == "camera.front_door"


def test_visual_event_endpoint_accepts_csv_image_urls():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/announce/visual",
        json={
            "event": "bins",
            "message": "Put out the brown bin.",
            "image_urls_csv": "/static/bin-icons/brown-bin.svg,/static/bin-icons/black-bin.svg",
        },
        headers={"X-API-Key": API_KEY},
    )

    assert resp.status_code == 200
    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    payload = next(call for call in voice_calls if call.get("type") == "visual_event")
    assert payload["image_urls"] == [
        "/static/bin-icons/brown-bin.svg",
        "/static/bin-icons/black-bin.svg",
    ]


def test_package_announce_uses_shared_package_camera_path():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/announce/package",
        json={},
        headers={"X-API-Key": API_KEY},
    )

    assert resp.status_code == 200
    assert resp.json()["event"] == "package_delivery"
    assert resp.json()["camera_used"] == "camera.reolink_video_doorbell_poe_fluent"
    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    payload = next(call for call in voice_calls if call.get("type") == "visual_event")
    assert payload["event"] == "package_delivery"
    assert payload["camera_entity_id"] == "camera.reolink_video_doorbell_poe_fluent"
    _, stored = app.state.recent_event_contexts[resp.json()["event_id"]]
    assert stored["event_type"] == "package_delivery"
    assert stored["event_context"]["source"] == "package_announce"


def test_motion_announce_applies_per_camera_cooldown():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)

    first = client.post(
        "/announce/motion",
        json={
            "camera_entity_id": "camera.reolink_profile000_mainstream",
            "location": "outside the front of the house",
        },
        headers={"X-API-Key": API_KEY},
    )
    second = client.post(
        "/announce/motion",
        json={
            "camera_entity_id": "camera.reolink_profile000_mainstream",
            "location": "outside the front of the house",
        },
        headers={"X-API-Key": API_KEY},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["message"].startswith("Motion detected")
    assert second.json()["message"] == "motion_cooldown"

    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    announce_calls = [call for call in voice_calls if call.get("type") == "announce"]
    assert len(announce_calls) == 0


def test_motion_announce_cooldown_is_per_camera():
    app, _, _, ws_mock = _make_app()
    client = TestClient(app)

    outdoor = client.post(
        "/announce/motion",
        json={
            "camera_entity_id": "camera.reolink_profile000_mainstream",
            "location": "outside the front of the house",
        },
        headers={"X-API-Key": API_KEY},
    )
    driveway = client.post(
        "/announce/motion",
        json={
            "camera_entity_id": "camera.rlc_1224a_fluent",
            "location": "on the driveway",
        },
        headers={"X-API-Key": API_KEY},
    )

    assert outdoor.status_code == 200
    assert driveway.status_code == 200
    assert driveway.json()["message"].startswith("Motion detected")

    voice_calls = [c.args[0] for c in ws_mock.broadcast_to_voice_json.await_args_list]
    announce_calls = [call for call in voice_calls if call.get("type") == "announce"]
    assert len(announce_calls) == 0


def test_motion_announce_persists_canonical_event_metadata():
    app, _, _, _ = _make_app()
    client = TestClient(app)

    resp = client.post(
        "/announce/motion",
        json={
            "camera_entity_id": "camera.reolink_profile000_mainstream",
            "location": "outside the front of the house",
        },
        headers={"X-API-Key": API_KEY},
    )

    assert resp.status_code == 200
    app.state.motion_clip_service.schedule_capture.assert_called_once()
    extra = app.state.motion_clip_service.schedule_capture.call_args.kwargs["extra"]
    canonical = extra["canonical_event"]
    assert canonical["event_type"] == "motion_detected"
    assert canonical["camera_entity_id"] == "camera.reolink_profile000_mainstream"
    assert canonical["event_context"]["source"] == "announce_motion"


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
    tts_mock.synthesise_with_timing = AsyncMock(side_effect=RuntimeError("piper crashed"))
    client = TestClient(app)
    resp = client.post("/announce", json={"message": "Will fail"},
                       headers={"X-API-Key": API_KEY})
    assert resp.status_code == 503


def test_announce_tts_failure_broadcasts_error_then_idle():
    app, tts_mock, _, ws_mock = _make_app()
    tts_mock.synthesise_with_timing = AsyncMock(side_effect=RuntimeError("piper crashed"))
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
