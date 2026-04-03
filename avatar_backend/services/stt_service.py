"""
Speech-to-text service using faster-whisper.

Accepts:
  - WAV (RIFF header) — decoded natively
  - WebM / OGG / MP4 / any format PyAV supports — decoded via PyAV / ffmpeg
  - Raw PCM16 mono bytes — decoded at the given sample_rate
"""
from __future__ import annotations
import io
import wave
from typing import Optional

import numpy as np
import structlog

_LOGGER = structlog.get_logger()
_WHISPER_RATE = 16000


class STTService:
    def __init__(self, model_name: str = "small", device: str = "auto") -> None:
        self._model_name = model_name
        self._device = device
        self._model = None
        self._wake_model = None  # tiny model used only for fast wake-word checks

    def _resolve_device(self) -> str:
        device = self._device
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if "cuda" in ctranslate2.get_supported_compute_types("cuda") else "cpu"
            except Exception:
                device = "cpu"
        return device

    def _load_model(self):
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper not installed") from exc

        device = self._resolve_device()
        compute_type = "int8"
        _LOGGER.info("stt.loading_model", model=self._model_name, device=device)
        self._model = WhisperModel(self._model_name, device=device, compute_type=compute_type)
        _LOGGER.info("stt.model_ready", model=self._model_name, device=device)

    def _load_wake_model(self):
        """Load the tiny model used for low-latency wake word checks."""
        if self._wake_model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper not installed") from exc

        device = self._resolve_device()
        _LOGGER.info("stt.loading_wake_model", model="tiny", device=device)
        self._wake_model = WhisperModel("tiny", device=device, compute_type="int8")
        _LOGGER.info("stt.wake_model_ready", device=device)

    async def transcribe(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        self._load_model()

        audio_f32, src_rate = _decode_audio(audio_bytes, sample_rate)
        if audio_f32 is None or len(audio_f32) == 0:
            _LOGGER.warning("stt.empty_audio", raw_bytes=len(audio_bytes))
            return ""

        rms     = float(np.sqrt(np.mean(audio_f32 ** 2)))
        max_amp = float(np.max(np.abs(audio_f32)))
        _LOGGER.info("stt.audio_received",
                     src_rate=src_rate,
                     samples=len(audio_f32),
                     duration_s=round(len(audio_f32) / _WHISPER_RATE, 2),
                     rms=round(rms, 5),
                     max_amp=round(max_amp, 5))

        segments, info = self._model.transcribe(
            audio_f32, language="en", beam_size=5, vad_filter=False,
        )
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        _LOGGER.info("stt.transcribed",
                     chars=len(transcript), duration_s=round(info.duration, 1),
                     text=transcript[:80])
        return transcript

    async def transcribe_wake(self, audio_bytes: bytes, sample_rate: int = 16000) -> str:
        """
        Fast wake-word transcription using the tiny model (beam_size=1).
        ~5-10x faster than the main transcribe() — acceptable accuracy for
        single-word detection ('nova').
        """
        self._load_wake_model()

        audio_f32, src_rate = _decode_audio(audio_bytes, sample_rate)
        if audio_f32 is None or len(audio_f32) == 0:
            return ""

        segments, info = self._wake_model.transcribe(
            audio_f32, language="en", beam_size=1, vad_filter=False,
        )
        transcript = " ".join(seg.text.strip() for seg in segments).strip()
        _LOGGER.info("stt.wake_transcribed",
                     chars=len(transcript), duration_s=round(info.duration, 1),
                     text=transcript[:60])
        return transcript

    @property
    def is_ready(self) -> bool:
        return self._model is not None


# ── Audio decoding ────────────────────────────────────────────────────────────

def _decode_audio(data: bytes, sample_rate: int) -> tuple[Optional[np.ndarray], int]:
    """Return (float32 at 16 kHz, original sample rate). Handles WAV, WebM, OGG, MP4, PCM."""
    if _is_wav(data):
        return _decode_wav(data)
    if len(data) >= 4:
        # Try PyAV for any container format (webm, ogg, mp4, etc.)
        result = _decode_av(data)
        if result[0] is not None:
            return result
    # Fall back to raw PCM16
    return _decode_pcm(data, sample_rate), sample_rate


def _is_wav(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"RIFF"


def _decode_wav(data: bytes) -> tuple[Optional[np.ndarray], int]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            src_rate = wf.getframerate()
            raw      = wf.readframes(wf.getnframes())
            samples  = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            return _resample(samples, src_rate), src_rate
    except Exception as exc:
        _LOGGER.warning("stt.wav_decode_error", exc=str(exc))
        return None, 0


def _decode_av(data: bytes) -> tuple[Optional[np.ndarray], int]:
    """Decode WebM / OGG / MP4 / any container using PyAV."""
    try:
        import av  # type: ignore
        container = av.open(io.BytesIO(data))
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        if audio_stream is None:
            return None, 0
        src_rate = audio_stream.sample_rate

        resampler = av.AudioResampler(format="fltp", layout="mono", rate=_WHISPER_RATE)
        frames: list[np.ndarray] = []

        for packet in container.demux(audio_stream):
            for frame in packet.decode():
                for rf in resampler.resample(frame):
                    arr = rf.to_ndarray()
                    frames.append(arr[0].astype(np.float32) if arr.ndim > 1 else arr.astype(np.float32))

        # Flush resampler
        for rf in resampler.resample(None):
            arr = rf.to_ndarray()
            frames.append(arr[0].astype(np.float32) if arr.ndim > 1 else arr.astype(np.float32))

        if not frames:
            return None, src_rate

        audio = np.concatenate(frames)
        return audio, src_rate
    except Exception as exc:
        _LOGGER.warning("stt.av_decode_error", exc=str(exc))
        return None, 0


def _decode_pcm(data: bytes, sample_rate: int) -> Optional[np.ndarray]:
    try:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        return _resample(samples, sample_rate)
    except Exception as exc:
        _LOGGER.warning("stt.pcm_decode_error", exc=str(exc))
        return None


def _resample(audio: np.ndarray, orig_rate: int) -> np.ndarray:
    if orig_rate == _WHISPER_RATE or orig_rate == 0:
        return audio
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(orig_rate, _WHISPER_RATE)
    return resample_poly(audio, _WHISPER_RATE // g, orig_rate // g).astype(np.float32)
