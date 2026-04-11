"""
Text-to-speech service — supports Piper (local) and ElevenLabs (cloud).

Select provider via TTS_PROVIDER env var: "piper" (default) or "elevenlabs".
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import io
import json
import os
import re
import struct
import subprocess
import threading
import warnings
import wave
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

# Suppress phonemizer verbosity before the module is imported
warnings.filterwarnings("ignore", message=".*words count mismatch.*")
warnings.filterwarnings("ignore", message=".*phonemizer.*")

import httpx
import structlog

_LOGGER = structlog.get_logger()
_STDERR_REDIRECT_LOCK = threading.Lock()

_VOICES_DIR = Path("/opt/avatar-server/config/piper_voices")
_PIPER_BIN  = Path("/opt/avatar-server/piper/piper")


def _normalize_tts_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = text.replace("•", ". ").replace("–", "-").replace("—", "-")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s*\n\s*", ". ", text)
    text = re.sub(r"\s*\.\s*\.\s*", ". ", text)
    text = re.sub(r"\.\s+\.", ".", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"([!?]){2,}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@contextlib.contextmanager
def _suppress_python_output():
    with open(os.devnull, "w") as devnull:
        with contextlib.redirect_stderr(devnull), contextlib.redirect_stdout(devnull):
            yield


# ── Base ──────────────────────────────────────────────────────────────────────

class BaseTTSService(ABC):
    @abstractmethod
    async def synthesise_with_timing(self, text: str) -> tuple[bytes, list[dict]]:
        ...

    @abstractmethod
    async def synthesise(self, text: str) -> bytes:
        ...

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        ...


# ── Piper ─────────────────────────────────────────────────────────────────────

class PiperTTSService(BaseTTSService):
    """Local TTS using the Piper binary."""

    def __init__(self, voice_name: str = "en_US-lessac-medium") -> None:
        self._voice_name  = voice_name
        self._model_path  = str(_VOICES_DIR / f"{voice_name}.onnx")
        self._config_path = str(_VOICES_DIR / f"{voice_name}.onnx.json")
        self._sample_rate = self._read_sample_rate()

    def _read_sample_rate(self) -> int:
        try:
            with open(self._config_path) as f:
                data = json.load(f)
                return int(data.get("audio", {}).get("sample_rate", 22050))
        except Exception:
            return 22050

    async def synthesise_with_timing(self, text: str) -> tuple[bytes, list[dict]]:
        wav_bytes = await self.synthesise(text)
        return wav_bytes, _estimate_word_timings(text, wav_bytes)

    async def synthesise(self, text: str) -> bytes:
        text = _normalize_tts_text(text)
        if not text:
            return _silent_wav(self._sample_rate)
        if not _PIPER_BIN.exists():
            raise RuntimeError(f"Piper binary not found at {_PIPER_BIN}.")
        if not Path(self._model_path).exists():
            raise FileNotFoundError(f"Piper voice model not found: {self._model_path}.")
        wav_bytes = await _run_piper(str(_PIPER_BIN), self._model_path, text)
        _LOGGER.info("tts.piper.synthesised", chars=len(text), wav_bytes=len(wav_bytes))
        return wav_bytes

    @property
    def is_ready(self) -> bool:
        return _PIPER_BIN.exists() and Path(self._model_path).exists()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate


# ── ElevenLabs ────────────────────────────────────────────────────────────────

_ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
_EL_SAMPLE_RATE  = 22050  # matches pcm_22050 output format


class ElevenLabsTTSService(BaseTTSService):
    """Cloud TTS using the ElevenLabs API."""

    def __init__(self, api_key: str, voice_id: str, model: str) -> None:
        self._api_key  = api_key
        self._voice_id = voice_id
        self._model    = model

    async def synthesise_with_timing(self, text: str) -> tuple[bytes, list[dict]]:
        text = (text or "").strip()
        if not text:
            return _silent_wav(_EL_SAMPLE_RATE), []

        url = f"{_ELEVENLABS_BASE}/text-to-speech/{self._voice_id}/with-timestamps"
        payload = {
            "text": text,
            "model_id": self._model,
        }
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers,
                                     params={"output_format": "pcm_22050"})

        if resp.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs API error {resp.status_code}: {resp.text[:200]}"
            )

        data       = resp.json()
        pcm_bytes  = base64.b64decode(data["audio_base64"])
        wav_bytes  = _pcm_to_wav(pcm_bytes, _EL_SAMPLE_RATE)
        alignment  = data.get("alignment") or {}
        word_timings = _el_alignment_to_word_timings(alignment)

        if not word_timings:
            word_timings = _estimate_word_timings(text, wav_bytes)

        _LOGGER.info("tts.elevenlabs.synthesised",
                     chars=len(text), wav_bytes=len(wav_bytes),
                     words=len(word_timings))
        return wav_bytes, word_timings

    async def synthesise(self, text: str) -> bytes:
        wav_bytes, _ = await self.synthesise_with_timing(text)
        return wav_bytes

    @property
    def is_ready(self) -> bool:
        return bool(self._api_key)



# ── AfroTTS (Kokoro) ──────────────────────────────────────────────────────────

class AfroTTSService(BaseTTSService):
    """Local high-quality TTS using the Kokoro engine.

    Runs fully on-device (CPU) so it does not compete with the GPU LLM.
    Voice IDs: af_heart, af_nicole, af_sarah, af_sky, am_adam, am_michael,
               bf_emma, bf_isabella, bm_george, bm_lewis
    """

    def __init__(self, voice: str = 'af_heart', speed: float = 1.0) -> None:
        self._voice       = voice
        self._speed       = speed
        self._pipeline    = None   # lazy-loaded on first use
        self._sample_rate = 24000  # Kokoro native output rate

    def _get_pipeline(self):
        if self._pipeline is None:
            with _suppress_python_output():
                with _suppress_process_stderr():
                    from kokoro import KPipeline  # type: ignore[import]
                    # lang_code 'a' = American English, 'b' = British English
                    lang = 'a' if self._voice[:1].lower() == 'a' else 'b'
                    self._pipeline = KPipeline(lang_code=lang, device='cpu')
            _LOGGER.info('tts.afrotts.pipeline_loaded', voice=self._voice, lang=lang)
        return self._pipeline

    async def synthesise_with_timing(self, text: str) -> tuple[bytes, list[dict]]:
        wav_bytes = await self.synthesise(text)
        return wav_bytes, _estimate_word_timings(text, wav_bytes)

    async def synthesise(self, text: str) -> bytes:
        text = (text or '').strip()
        if not text:
            return _silent_wav(self._sample_rate)
        loop = asyncio.get_event_loop()
        wav_bytes = await loop.run_in_executor(None, self._synthesise_sync, text)
        _LOGGER.info('tts.afrotts.synthesised', chars=len(text), wav_bytes=len(wav_bytes))
        return wav_bytes

    def _synthesise_sync(self, text: str) -> bytes:
        import io as _io
        import wave as _wave
        import numpy as np
        with _suppress_python_output():
            pipeline = self._get_pipeline()
            chunks: list = []
            with _suppress_process_stderr():
                for _, _, audio in pipeline(text, voice=self._voice, speed=self._speed):
                    if audio is not None:
                        # Kokoro yields PyTorch tensors — convert to numpy
                        if hasattr(audio, 'detach'):
                            audio = audio.detach().cpu().numpy()
                        chunks.append(audio)
        if not chunks:
            return _silent_wav(self._sample_rate)
        audio_np = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        pcm16 = (np.clip(audio_np, -1.0, 1.0) * 32767).astype(np.int16)
        buf = _io.BytesIO()
        with _wave.open(buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm16.tobytes())
        return buf.getvalue()

    @property
    def is_ready(self) -> bool:
        return True  # Kokoro downloads model weights on first use


class IntronAfroTTSService(BaseTTSService):
    """HTTP-backed XTTS sidecar for accented speech synthesis.

    The sidecar runs in a Python 3.11 GPU container because Coqui TTS does not
    currently support this app's Python 3.12 runtime directly.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 90.0,
        reference_wav: str = "",
        language: str = "en",
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._timeout_s = max(float(timeout_s or 90.0), 5.0)
        self._reference_wav = str(reference_wav or "").strip()
        self._language = str(language or "en").strip() or "en"
        self._sample_rate = 24000

    async def synthesise_with_timing(self, text: str) -> tuple[bytes, list[dict]]:
        wav_bytes = await self.synthesise(text)
        return wav_bytes, _estimate_word_timings(text, wav_bytes)

    async def synthesise(self, text: str) -> bytes:
        text = _normalize_tts_text(text)
        if not text:
            return _silent_wav(self._sample_rate)
        if not self._base_url:
            raise RuntimeError("Intron Afro TTS URL is not configured.")
        payload = {
            "text": text,
            "language": self._language,
        }
        if self._reference_wav:
            payload["reference_wav"] = self._reference_wav
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_s)) as client:
            resp = await client.post(
                f"{self._base_url}/v1/synth",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        wav_bytes = resp.content
        _LOGGER.info(
            "tts.intron_afro_tts.synthesised",
            chars=len(text),
            wav_bytes=len(wav_bytes),
            reference_wav=bool(self._reference_wav),
        )
        return wav_bytes

    async def is_ready_remote(self) -> bool:
        if not self._base_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(min(self._timeout_s, 10.0))) as client:
                resp = await client.get(f"{self._base_url}/health")
            return resp.status_code == 200
        except Exception:
            return False

    @property
    def is_ready(self) -> bool:
        return bool(self._base_url)

def create_tts_service(settings) -> BaseTTSService:
    """Return the configured TTS service based on settings.tts_provider."""
    provider = (settings.tts_provider or "piper").lower().strip()
    for noisy_logger in (
        "phonemizer",
        "phonemizer.logger",
        "huggingface_hub",
        "huggingface_hub.utils._http",
    ):
        _nl = structlog.stdlib.logging.getLogger(noisy_logger)
        _nl.setLevel("ERROR")
        _nl.propagate = False
    if provider == "elevenlabs":
        _LOGGER.info("tts.provider", provider="elevenlabs",
                     voice_id=settings.elevenlabs_voice_id)
        return ElevenLabsTTSService(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model=settings.elevenlabs_model,
        )
    if provider == "afrotts":
        _LOGGER.info("tts.provider", provider="afrotts",
                     voice=settings.afrotts_voice)
        return AfroTTSService(
            voice=settings.afrotts_voice,
            speed=settings.afrotts_speed,
        )
    if provider == "intron_afro_tts":
        _LOGGER.info(
            "tts.provider",
            provider="intron_afro_tts",
            url=settings.intron_afro_tts_url,
            language=settings.intron_afro_tts_language,
            reference_wav=bool(settings.intron_afro_tts_reference_wav),
        )
        return IntronAfroTTSService(
            base_url=settings.intron_afro_tts_url,
            timeout_s=settings.intron_afro_tts_timeout_s,
            reference_wav=settings.intron_afro_tts_reference_wav,
            language=settings.intron_afro_tts_language,
        )
    _LOGGER.info("tts.provider", provider="piper", voice=settings.piper_voice)
    return PiperTTSService(voice_name=settings.piper_voice)


# ── Helpers ───────────────────────────────────────────────────────────────────

@contextmanager
def _suppress_process_stderr():
    saved_stderr_fd = None
    devnull_fd = None
    with _STDERR_REDIRECT_LOCK:
        try:
            saved_stderr_fd = os.dup(2)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, 2)
            yield
        finally:
            if saved_stderr_fd is not None:
                os.dup2(saved_stderr_fd, 2)
                os.close(saved_stderr_fd)
            if devnull_fd is not None:
                os.close(devnull_fd)

def _el_alignment_to_word_timings(alignment: dict) -> list[dict]:
    """Convert ElevenLabs character-level alignment to word-level timings."""
    chars       = alignment.get("characters", [])
    starts      = alignment.get("character_start_times_seconds", [])
    ends        = alignment.get("character_end_times_seconds", [])

    if not chars or len(chars) != len(starts):
        return []

    timings: list[dict] = []
    word_chars: list[str] = []
    word_start: float | None = None

    for ch, s, e in zip(chars, starts, ends):
        if ch in (" ", "\t", "\n"):
            if word_chars and word_start is not None:
                timings.append({
                    "word":     "".join(word_chars),
                    "start_ms": round(word_start * 1000),
                    "end_ms":   round(e * 1000),
                })
            word_chars = []
            word_start = None
        else:
            if word_start is None:
                word_start = s
            word_chars.append(ch)

    if word_chars and word_start is not None:
        timings.append({
            "word":     "".join(word_chars),
            "start_ms": round(word_start * 1000),
            "end_ms":   round(ends[-1] * 1000),
        })

    return timings


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 22050) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a RIFF WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


async def _run_piper(piper_bin: str, model_path: str, text: str) -> bytes:
    with _suppress_process_stderr():
        proc = await asyncio.create_subprocess_exec(
            piper_bin,
            "--model", model_path,
            "--output_file", "-",
            "--quiet",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=text.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(f"Piper exited {proc.returncode}: {stderr.decode()[:200]}")
    return stdout


async def _mp3_to_wav(mp3_bytes: bytes, sample_rate: int = 22050) -> bytes:
    """Convert MP3 bytes to 16-bit mono WAV via ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", "pipe:0",
        "-ar", str(sample_rate), "-ac", "1",
        "-f", "wav", "pipe:1",
        "-loglevel", "error",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=mp3_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg MP3→WAV failed: {stderr.decode()[:200]}")
    return stdout


def _estimate_word_timings(text: str, wav_bytes: bytes) -> list[dict]:
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words:
        return []
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            duration_ms = wf.getnframes() / wf.getframerate() * 1000
    except Exception:
        duration_ms = len(words) * 300.0
    lead_ms   = 80.0
    trail_ms  = 80.0
    gap_ms    = 40.0
    total_gap = gap_ms * (len(words) - 1)
    speech_ms = max(duration_ms - lead_ms - trail_ms - total_gap, len(words) * 50.0)
    weights   = [max(len(re.sub(r"[^\w]", "", w)), 1) for w in words]
    total_wt  = sum(weights)
    timings: list[dict] = []
    cursor = lead_ms
    for word, wt in zip(words, weights):
        word_ms = (wt / total_wt) * speech_ms
        timings.append({
            "word":     word,
            "start_ms": round(cursor),
            "end_ms":   round(cursor + word_ms),
        })
        cursor += word_ms + gap_ms
    return timings


def _silent_wav(sample_rate: int = 22050, duration_ms: int = 100) -> bytes:
    n_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()


# Back-compat alias
TTSService = BaseTTSService
