"""
Phase 5 — SpeakerService unit tests.
HA calls are mocked — no real Home Assistant required.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from avatar_backend.services.speaker_service import SpeakerService


def _make_svc(speakers=None):
    if speakers is None:
        speakers = ["media_player.echo_living_room"]
    return SpeakerService(
        ha_url="http://ha.local:8123",
        ha_token="test-token",
        speakers=speakers,
    )


def _mock_resp(status_code: int, text: str = "[]"):
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.text = text
    return mock


# ── is_configured ─────────────────────────────────────────────────────────────

def test_is_configured_true_with_speakers():
    svc = _make_svc(["media_player.echo_living_room"])
    assert svc.is_configured is True


def test_is_configured_false_with_no_speakers():
    svc = _make_svc([])
    assert svc.is_configured is False


# ── speak (no-op paths) ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_empty_text_does_nothing():
    svc = _make_svc()
    # Should not call HA at all
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        await svc.speak("")
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_speak_whitespace_only_does_nothing():
    svc = _make_svc()
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        await svc.speak("   ")
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_speak_no_speakers_does_nothing():
    svc = _make_svc(speakers=[])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        await svc.speak("Hello world")
    mock_post.assert_not_called()


# ── speak (success) ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_calls_tts_speak_endpoint():
    svc = _make_svc(["media_player.living_room_sonos"])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_resp(200)) as mock_post:
        await svc.speak("Lights are on")
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "/api/services/tts/speak" in call_args[0][0]
    assert call_args[1]["json"]["message"] == "Lights are on"
    assert call_args[1]["json"]["media_player_entity_id"] == "media_player.living_room_sonos"


@pytest.mark.asyncio
async def test_speak_multiple_speakers_calls_each():
    svc = _make_svc([
        "media_player.echo_living_room",
        "media_player.sonos_bedroom",
    ])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_resp(200)) as mock_post:
        await svc.speak("Good morning")
    assert mock_post.call_count == 2


# ── speak (fallback to cloud_say on 404) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_404_is_caught_without_raising():
    svc = _make_svc(["media_player.kitchen_sonos"])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_resp(404)):
        await svc.speak("Testing 404 handling")


# ── speak (error handling) ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_speak_error_on_one_speaker_does_not_abort_others():
    """An error on speaker 1 should not prevent speaker 2 from being called."""
    svc = _make_svc([
        "media_player.dead_speaker",
        "media_player.good_speaker",
    ])

    call_count = 0
    async def fake_post(url, **kwargs):
        nonlocal call_count
        call_count += 1
        if "dead_speaker" in str(kwargs.get("json", {})):
            raise httpx.ConnectError("refused")
        return _mock_resp(200)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               side_effect=fake_post):
        # Should not raise even though one speaker fails
        await svc.speak("Test message")

    assert call_count == 2


@pytest.mark.asyncio
async def test_speak_ha_500_raises_but_is_caught():
    """HTTP 500 from HA should be logged, not propagate to the caller."""
    svc = _make_svc(["media_player.echo"])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_resp(500, "Internal Server Error")):
        # Should complete without raising
        await svc.speak("Will this work?")


@pytest.mark.asyncio
async def test_speak_area_aware_prefers_occupied_area(monkeypatch):
    svc = _make_svc(["media_player.kitchen_sonos", "media_player.bedroom_sonos"])
    monkeypatch.setattr(
        svc,
        "get_speaker_catalog",
        AsyncMock(return_value=[
            {"entity_id": "media_player.kitchen_sonos", "friendly_name": "Kitchen", "area_name": "Kitchen", "enabled": True, "use_alexa": False},
            {"entity_id": "media_player.bedroom_sonos", "friendly_name": "Bedroom", "area_name": "Main Bedroom", "enabled": True, "use_alexa": False},
        ]),
    )
    monkeypatch.setattr(svc, "get_occupied_areas", AsyncMock(return_value=["Main Bedroom"]))

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=_mock_resp(200)) as mock_post:
        await svc.speak("Bedtime reminder", area_aware=True)

    assert mock_post.call_count == 1
    assert mock_post.call_args[1]["json"]["media_player_entity_id"] == "media_player.bedroom_sonos"


@pytest.mark.asyncio
async def test_speak_area_aware_honours_explicit_target_area(monkeypatch):
    svc = _make_svc(["media_player.kitchen_sonos", "media_player.bedroom_sonos"])
    monkeypatch.setattr(
        svc,
        "get_speaker_catalog",
        AsyncMock(return_value=[
            {"entity_id": "media_player.kitchen_sonos", "friendly_name": "Kitchen", "area_name": "Kitchen", "enabled": True, "use_alexa": False},
            {"entity_id": "media_player.bedroom_sonos", "friendly_name": "Bedroom", "area_name": "Main Bedroom", "enabled": True, "use_alexa": False},
        ]),
    )
    monkeypatch.setattr(svc, "get_occupied_areas", AsyncMock(return_value=["Kitchen"]))

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=_mock_resp(200)) as mock_post:
        await svc.speak("Bedroom only", area_aware=True, target_areas=["Main Bedroom"])

    assert mock_post.call_count == 1
    assert mock_post.call_args[1]["json"]["media_player_entity_id"] == "media_player.bedroom_sonos"


def test_set_speaker_preferences_disables_unchecked_default_speakers(tmp_path):
    svc = SpeakerService(
        ha_url="http://ha.local:8123",
        ha_token="test-token",
        speakers=["media_player.kitchen_sonos", "media_player.bedroom_sonos"],
    )
    svc._settings_path = tmp_path / "speaker_settings.json"

    svc.set_speaker_preferences([
        {"entity_id": "media_player.kitchen_sonos", "enabled": True},
        {"entity_id": "media_player.bedroom_sonos", "enabled": False},
    ])

    assert svc.is_configured is True
    assert svc._configured_speakers_sync() == [("media_player.kitchen_sonos", False)]


def test_prompt_area_map_fills_unassigned_catalog(monkeypatch, tmp_path):
    svc = SpeakerService(
        ha_url="http://ha.local:8123",
        ha_token="test-token",
        speakers=["media_player.bedroom_sonos"],
    )
    svc._settings_path = tmp_path / "speaker_settings.json"
    monkeypatch.setattr(
        svc,
        "_fetch_speaker_catalog",
        AsyncMock(return_value=[
            {"entity_id": "media_player.bedroom_sonos", "friendly_name": "Bedroom Sonos", "area_name": "Unassigned"},
        ]),
    )
    monkeypatch.setattr(svc, "_load_prompt_area_map", MagicMock(return_value={"media_player.bedroom_sonos": "MAIN BEDROOM"}))

    catalog = asyncio.run(svc.get_speaker_catalog(force_refresh=True))

    assert catalog[0]["area_name"] == "MAIN BEDROOM"
