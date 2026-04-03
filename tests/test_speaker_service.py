"""
Phase 5 — SpeakerService unit tests.
HA calls are mocked — no real Home Assistant required.
"""
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
    svc = _make_svc(["media_player.echo_living_room"])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               return_value=_mock_resp(200)) as mock_post:
        await svc.speak("Lights are on")
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "/api/services/tts/speak" in call_args[0][0]
    assert call_args[1]["json"]["message"] == "Lights are on"
    assert call_args[1]["json"]["entity_id"] == "media_player.echo_living_room"


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
async def test_speak_falls_back_on_404():
    """If tts.speak returns 404 (old HA), fall back to tts.cloud_say."""
    responses = [_mock_resp(404), _mock_resp(200)]
    call_count = 0

    async def fake_post(url, **kwargs):
        nonlocal call_count
        resp = responses[call_count]
        call_count += 1
        return resp

    svc = _make_svc(["media_player.echo"])
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock,
               side_effect=fake_post):
        await svc.speak("Testing fallback")

    assert call_count == 2  # tried tts/speak then tts/cloud_say


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
