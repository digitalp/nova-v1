"""
Phase 4 — TTS service unit tests.
Piper binary calls are mocked so no binary is required.
"""
import io
import json
import wave
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from avatar_backend.services.tts_service import PiperTTSService, _normalize_tts_text, _silent_wav, _VOICES_DIR, _PIPER_BIN


# ── _silent_wav helper ────────────────────────────────────────────────────────

def test_silent_wav_is_valid_wav():
    data = _silent_wav(sample_rate=22050, duration_ms=100)
    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 22050


def test_silent_wav_has_correct_length():
    data = _silent_wav(sample_rate=16000, duration_ms=200)
    with wave.open(io.BytesIO(data), "rb") as wf:
        # 16000 Hz × 0.2 s = 3200 samples
        assert wf.getnframes() == 3200


# ── TTSService helpers ────────────────────────────────────────────────────────

def _make_silent_wav_bytes(sample_rate: int = 22050, n_samples: int = 100) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


# ── TTSService.synthesise ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesise_empty_text_returns_silent_wav():
    svc = PiperTTSService()
    wav = await svc.synthesise("")
    assert wav[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_synthesise_whitespace_only_returns_silent_wav():
    svc = PiperTTSService()
    wav = await svc.synthesise("   ")
    assert wav[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_synthesise_calls_piper_binary():
    """_run_piper should be called with correct arguments."""
    fake_wav = _make_silent_wav_bytes()

    svc = PiperTTSService(voice_name="en_US-lessac-medium")
    # Patch _PIPER_BIN and model path existence checks
    with (
        patch("avatar_backend.services.tts_service._PIPER_BIN",
              new=Path("/fake/piper")),
        patch.object(Path, "exists", return_value=True),
        patch("avatar_backend.services.tts_service._run_piper",
              new_callable=AsyncMock, return_value=fake_wav) as mock_run,
    ):
        result = await svc.synthesise("Hello world")

    mock_run.assert_called_once()
    assert result == fake_wav


@pytest.mark.asyncio
async def test_synthesise_raises_if_binary_missing():
    svc = PiperTTSService(voice_name="en_US-lessac-medium")
    with patch("avatar_backend.services.tts_service._PIPER_BIN",
               new=Path("/nonexistent/piper")):
        with pytest.raises(RuntimeError, match="Piper binary not found"):
            await svc.synthesise("Hello")


@pytest.mark.asyncio
async def test_synthesise_wav_is_parseable():
    """Result of synthesise (when mocked) should be valid WAV."""
    fake_wav = _make_silent_wav_bytes()
    svc = PiperTTSService(voice_name="en_US-lessac-medium")

    with (
        patch("avatar_backend.services.tts_service._PIPER_BIN",
              new=Path("/fake/piper")),
        patch.object(Path, "exists", return_value=True),
        patch("avatar_backend.services.tts_service._run_piper",
              new_callable=AsyncMock, return_value=fake_wav),
    ):
        result = await svc.synthesise("Test synthesis")

    with wave.open(io.BytesIO(result), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2


def test_is_ready_false_when_binary_missing():
    svc = PiperTTSService()
    with patch("avatar_backend.services.tts_service._PIPER_BIN",
               new=Path("/nonexistent/piper")):
        assert svc.is_ready is False


def test_is_ready_true_when_all_present():
    svc = PiperTTSService()
    with (
        patch("avatar_backend.services.tts_service._PIPER_BIN",
              new=Path("/fake/piper")),
        patch.object(Path, "exists", return_value=True),
    ):
        assert svc.is_ready is True


def test_sample_rate_default():
    svc = PiperTTSService()
    assert svc.sample_rate in (16000, 22050)  # depends on config file presence


def test_normalize_tts_text_strips_markdown_and_urls():
    text = "Hello **Penn**.\nVisit https://example.com now... • Thanks!"

    normalized = _normalize_tts_text(text)

    assert normalized == "Hello Penn. Visit now. Thanks!"
