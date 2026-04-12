"""
Phase 4 — STT service unit tests.
faster-whisper is mocked so no GPU is required.
"""
import io
import struct
import wave
import pytest
from unittest.mock import MagicMock, patch

from avatar_backend.services.stt_service import (
    STTService,
    _is_wav,
    _wav_to_f32,
    _pcm16_to_f32,
)


# ── Audio helpers ─────────────────────────────────────────────────────────────

def _make_wav(n_samples: int = 3200, sample_rate: int = 16000) -> bytes:
    """Create a minimal silent WAV file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


def _make_pcm16(n_samples: int = 3200) -> bytes:
    return b"\x00\x00" * n_samples


# ── _is_wav ───────────────────────────────────────────────────────────────────

def test_is_wav_detects_riff_header():
    wav = _make_wav()
    assert _is_wav(wav) is True


def test_is_wav_rejects_raw_pcm():
    assert _is_wav(_make_pcm16()) is False


def test_is_wav_rejects_short_data():
    assert _is_wav(b"\x00\x01") is False


# ── _wav_to_f32 ───────────────────────────────────────────────────────────────

def test_wav_to_f32_returns_float_array():
    import numpy as np
    wav = _make_wav(n_samples=160)
    arr = _wav_to_f32(wav)
    assert arr is not None
    assert arr.dtype == np.float32
    assert len(arr) == 160


def test_wav_to_f32_normalised():
    import numpy as np
    wav = _make_wav(160)
    arr = _wav_to_f32(wav)
    assert np.all(arr >= -1.0) and np.all(arr <= 1.0)


def test_wav_to_f32_returns_none_on_bad_data():
    result = _wav_to_f32(b"not a wav file")
    assert result is None


# ── _pcm16_to_f32 ─────────────────────────────────────────────────────────────

def test_pcm16_to_f32_returns_float_array():
    import numpy as np
    pcm = _make_pcm16(100)
    arr = _pcm16_to_f32(pcm)
    assert arr is not None
    assert arr.dtype == np.float32
    assert len(arr) == 100


# ── STTService ────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_whisper_model():
    """Return a fake WhisperModel that produces a single segment."""
    fake_segment = MagicMock()
    fake_segment.text = "  hello world  "
    fake_info = MagicMock()
    fake_info.duration = 2.0

    model = MagicMock()
    model.transcribe.return_value = ([fake_segment], fake_info)
    return model


@pytest.mark.asyncio
async def test_transcribe_wav_returns_text(mock_whisper_model):
    svc = STTService(model_name="tiny")
    svc._model = mock_whisper_model  # inject mock, skip real load

    wav = _make_wav()
    result = await svc.transcribe(wav)
    assert result == "hello world"


@pytest.mark.asyncio
async def test_transcribe_pcm_returns_text(mock_whisper_model):
    svc = STTService(model_name="tiny")
    svc._model = mock_whisper_model

    pcm = _make_pcm16()
    result = await svc.transcribe(pcm)
    assert result == "hello world"


@pytest.mark.asyncio
async def test_transcribe_empty_bytes_returns_empty():
    svc = STTService(model_name="tiny")
    svc._model = MagicMock()  # should not be called for empty audio

    result = await svc.transcribe(b"")
    assert result == ""


@pytest.mark.asyncio
async def test_transcribe_silent_audio_returns_empty():
    """VAD filter should strip silence; model returns no segments."""
    svc = STTService(model_name="tiny")
    model = MagicMock()
    model.transcribe.return_value = ([], MagicMock(duration=1.0))
    svc._model = model

    result = await svc.transcribe(_make_wav())
    assert result == ""


def test_is_ready_false_before_load():
    svc = STTService()
    assert svc.is_ready is False


def test_is_ready_true_after_inject():
    svc = STTService()
    svc._model = MagicMock()
    assert svc.is_ready is True
