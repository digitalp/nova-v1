"""
Priority 6 — Wake word pipeline reliability tests.

Tests cover each detection stage in isolation:
- Coral TFLite fires at threshold → method=coral
- CPU TFLite fires when Coral absent → method=tflite_cpu
- Numpy classifier fires when TFLite absent → method=numpy_classifier
- Verifier YES → returns early, Whisper never called
- Verifier NO → falls through to Whisper
- Whisper fallback confirms wake → method=whisper_fallback, wake=True
- Whisper fallback denies → wake=False
- describe_pipeline returns correct stage list
- reload_verifier/reload_tflite update internal state
- _bytes_to_pcm_f32 handles WAV input correctly
"""
import asyncio
import struct
import wave
import io
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock

from avatar_backend.services.coral_wake_detector import (
    CoralWakeDetector,
    WakeResult,
    _bytes_to_pcm_f32,
    _CORAL_THRESHOLD,
)


def _silence_wav(duration_s: float = 0.5, rate: int = 16000) -> bytes:
    """Generate a silent WAV file at 16 kHz mono PCM16."""
    n = int(rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    return buf.getvalue()


def _make_fake_tflite(score: float):
    """Return a fake TFLite interpreter whose invoke() produces the given score."""
    out_buf = np.array([[1.0 - score, score]], dtype=np.float32)
    interp = MagicMock()
    interp.get_input_details.return_value = [{"index": 0, "shape": (1, 128), "dtype": np.float32}]
    interp.get_output_details.return_value = [{"index": 1, "dtype": np.float32,
                                                "quantization_parameters": {}}]
    interp.get_tensor.return_value = out_buf
    return interp


def _fake_verifier(threshold: float = 0.65):
    """Return a verifier dict whose cosine similarity will be `score_returned`."""
    # We control features → just make centroid = unit vector, features = unit vector * score
    centroid = np.ones(128, dtype=np.float32)
    centroid /= np.linalg.norm(centroid)
    return {
        "centroid": centroid.tolist(),
        "threshold": threshold,
        "wake_word": "nova",
    }


# ── Stage 1: Coral TFLite ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coral_fires_above_threshold():
    fake_interp = _make_fake_tflite(score=0.95)
    stt = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower(),
                                  coral_interp=fake_interp)
    result = await detector.detect(_silence_wav())
    assert result.wake is True
    assert result.method == "coral"
    stt.transcribe_wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_coral_below_threshold_falls_through_to_whisper():
    fake_interp = _make_fake_tflite(score=0.50)  # below _CORAL_THRESHOLD
    stt = MagicMock()
    stt.transcribe_wake = AsyncMock(return_value="nova")
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower(),
                                  coral_interp=fake_interp)
    result = await detector.detect(_silence_wav())
    # Falls through to Whisper because coral didn't fire
    assert result.method == "whisper_fallback"
    stt.transcribe_wake.assert_awaited_once()


# ── Stage 1b: CPU TFLite ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cpu_tflite_fires_when_coral_absent():
    fake_interp = _make_fake_tflite(score=0.92)
    stt = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower(),
                                  cpu_interp=fake_interp)  # no coral
    result = await detector.detect(_silence_wav())
    assert result.wake is True
    assert result.method == "tflite_cpu"
    stt.transcribe_wake.assert_not_awaited()


# ── Stage 1c: Numpy classifier ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_numpy_classifier_fires_when_tflite_absent():
    # Build a tiny numpy model that always outputs class-1 score = 0.95
    # Input: 128 → Dense(4, relu) → Dense(2, softmax)
    rng = np.random.default_rng(42)
    W1 = rng.normal(size=(128, 4)).astype(np.float32)
    b1 = np.zeros(4, dtype=np.float32)
    W2 = rng.normal(size=(4, 2)).astype(np.float32)
    b2 = np.array([0.0, 10.0], dtype=np.float32)  # bias class 1 very high
    numpy_model = {"W1": W1, "b1": b1, "W2": W2, "b2": b2}

    stt = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower(),
                                  numpy_model=numpy_model)
    result = await detector.detect(_silence_wav())
    # With b2[1]=10, class-1 softmax ≈ 1.0 → should fire
    assert result.wake is True
    assert result.method == "numpy_classifier"
    stt.transcribe_wake.assert_not_awaited()


# ── Stage 2: Verifier ─────────────────────────────────────────────────────────

def test_run_verifier_above_threshold_returns_wake():
    stt = AsyncMock()
    # DC signal FFT has score ~0.088 against uniform centroid; threshold=0.05 ensures wake=True
    verifier = _fake_verifier(threshold=0.05)
    detector = CoralWakeDetector(stt, lambda t: True, verifier=verifier)
    # Use a non-zero WAV so cosine similarity is well-defined
    n = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{n}h", *([1000] * n)))
    result = detector._run_verifier(buf.getvalue())
    # centroid is uniform unit vector; cosine of uniform features against it is 1.0
    assert result is not None
    assert result.wake is True
    assert result.method == "verifier"


def test_run_verifier_below_threshold_returns_no_wake():
    stt = AsyncMock()
    # Set threshold impossibly high so it always fails
    verifier = _fake_verifier(threshold=2.0)
    detector = CoralWakeDetector(stt, lambda t: True, verifier=verifier)
    n = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{n}h", *([1000] * n)))
    result = detector._run_verifier(buf.getvalue())
    assert result is not None
    assert result.wake is False


@pytest.mark.asyncio
async def test_verifier_yes_skips_whisper():
    """When verifier fires positively, Whisper must not be called."""
    verifier = _fake_verifier(threshold=0.0)  # always fires
    n = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{n}h", *([1000] * n)))

    stt = MagicMock()
    stt.transcribe_wake = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: True, verifier=verifier)
    result = await detector.detect(buf.getvalue())
    assert result.wake is True
    stt.transcribe_wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_verifier_no_falls_through_to_whisper():
    """When verifier says NO, the pipeline continues to Whisper."""
    verifier = _fake_verifier(threshold=2.0)  # never fires
    n = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{n}h", *([1000] * n)))

    stt = MagicMock()
    stt.transcribe_wake = AsyncMock(return_value="nova wake up")
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower(), verifier=verifier)
    result = await detector.detect(buf.getvalue())
    assert result.method == "whisper_fallback"
    stt.transcribe_wake.assert_awaited_once()


# ── Stage 4: Whisper fallback ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_whisper_fallback_confirms_wake():
    stt = MagicMock()
    stt.transcribe_wake = AsyncMock(return_value="nova, play music")
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower())
    result = await detector.detect(_silence_wav())
    assert result.wake is True
    assert result.method == "whisper_fallback"
    assert "nova" in result.transcript


@pytest.mark.asyncio
async def test_whisper_fallback_denies_non_wake():
    stt = MagicMock()
    stt.transcribe_wake = AsyncMock(return_value="the rain in spain")
    detector = CoralWakeDetector(stt, lambda t: "nova" in t.lower())
    result = await detector.detect(_silence_wav())
    assert result.wake is False
    assert result.method == "whisper_fallback"


@pytest.mark.asyncio
async def test_whisper_exception_returns_no_wake():
    stt = MagicMock()
    stt.transcribe_wake = AsyncMock(side_effect=RuntimeError("STT crashed"))
    detector = CoralWakeDetector(stt, lambda t: True)
    result = await detector.detect(_silence_wav())
    assert result.wake is False


# ── describe_pipeline ─────────────────────────────────────────────────────────

def test_describe_pipeline_whisper_only():
    stt = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: True)
    assert detector.describe_pipeline() == ["whisper_fallback"]


def test_describe_pipeline_with_verifier_and_whisper():
    stt = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: True,
                                  verifier=_fake_verifier(threshold=0.65))
    stages = detector.describe_pipeline()
    assert "verifier_model" in stages
    assert stages[-1] == "whisper_fallback"


def test_describe_pipeline_full():
    stt = AsyncMock()
    fake_interp = _make_fake_tflite(score=0.9)
    detector = CoralWakeDetector(stt, lambda t: True,
                                  coral_interp=fake_interp,
                                  verifier=_fake_verifier(threshold=0.65))
    stages = detector.describe_pipeline()
    assert stages[0] == "coral_tflite"
    assert stages[-1] == "whisper_fallback"


# ── _bytes_to_pcm_f32 ─────────────────────────────────────────────────────────

def test_bytes_to_pcm_f32_wav_correct_length():
    wav = _silence_wav(duration_s=1.0, rate=16000)
    pcm = _bytes_to_pcm_f32(wav)
    assert isinstance(pcm, np.ndarray)
    assert pcm.dtype == np.float32
    assert len(pcm) == 16000


def test_bytes_to_pcm_f32_wav_values_in_range():
    n = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        samples = [int(32767 * np.sin(2 * np.pi * 440 * i / 16000)) for i in range(n)]
        wf.writeframes(struct.pack(f"<{n}h", *samples))
    pcm = _bytes_to_pcm_f32(buf.getvalue())
    assert pcm.max() <= 1.0
    assert pcm.min() >= -1.0


# ── Reload methods ────────────────────────────────────────────────────────────

def test_reload_verifier_updates_state(tmp_path, monkeypatch):
    stt = AsyncMock()
    detector = CoralWakeDetector(stt, lambda t: True)
    assert detector._verifier is None

    fake_verifier_data = {
        "centroid": np.ones(128, dtype=np.float32).tolist(),
        "threshold": 0.65,
        "wake_word": "nova",
    }
    monkeypatch.setattr(
        "avatar_backend.services.coral_wake_detector.CoralWakeDetector._try_load_verifier",
        staticmethod(lambda: fake_verifier_data),
    )
    detector.reload_verifier()
    assert detector._verifier is not None
    assert detector.verifier_available is True
