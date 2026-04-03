"""
Text-to-speech service using Piper TTS binary (subprocess).

Downloads and caches the piper executable from GitHub releases.
Uses the ONNX voice model to synthesise speech.
Output is always 16-bit mono WAV bytes.

Why subprocess instead of piper-tts Python package?
  The piper-tts PyPI package requires piper-phonemize~=1.1.0 which has
  no wheel for Python 3.12. The standalone binary works on all Python
  versions and is the approach recommended by the Piper authors.
"""
from __future__ import annotations
import asyncio
import io
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import structlog

_LOGGER = structlog.get_logger()

_VOICES_DIR   = Path("/opt/avatar-server/config/piper_voices")
_PIPER_BIN    = Path("/opt/avatar-server/piper/piper")

# Piper binary release download URL (Linux x86_64)
_PIPER_RELEASE_URL = (
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/"
    "piper_linux_x86_64.tar.gz"
)


class TTSService:
    """Synthesises speech from text using the Piper CLI binary."""

    def __init__(self, voice_name: str = "en_US-lessac-medium") -> None:
        self._voice_name = voice_name
        self._model_path  = str(_VOICES_DIR / f"{voice_name}.onnx")
        self._config_path = str(_VOICES_DIR / f"{voice_name}.onnx.json")
        self._sample_rate: int = self._read_sample_rate()

    def _read_sample_rate(self) -> int:
        """Read sample rate from voice config JSON (default 22050)."""
        try:
            with open(self._config_path) as f:
                data = json.load(f)
                return int(data.get("audio", {}).get("sample_rate", 22050))
        except Exception:
            return 22050

    async def synthesise_with_timing(self, text: str) -> tuple[bytes, list[dict]]:
        """
        Synthesise speech and return (wav_bytes, word_timings).

        word_timings is a list of {"word": str, "start_ms": int, "end_ms": int}
        estimated proportionally from total audio duration and word lengths —
        the same approach TalkMateAI uses for driving TalkingHead lip sync.
        """
        wav_bytes = await self.synthesise(text)
        timings   = _estimate_word_timings(text, wav_bytes)
        return wav_bytes, timings

    async def synthesise(self, text: str) -> bytes:
        """
        Convert *text* to speech using the Piper binary.

        Returns raw WAV bytes (RIFF header + PCM16 mono).
        Raises RuntimeError if piper binary is not found.
        """
        text = (text or "").strip()
        if not text:
            return _silent_wav(self._sample_rate)

        if not _PIPER_BIN.exists():
            raise RuntimeError(
                f"Piper binary not found at {_PIPER_BIN}. "
                "Run scripts/download_piper.sh to install it."
            )
        if not Path(self._model_path).exists():
            raise FileNotFoundError(
                f"Piper voice model not found: {self._model_path}. "
                "Run scripts/download_piper_voice.sh to download it."
            )

        try:
            wav_bytes = await _run_piper(
                piper_bin=str(_PIPER_BIN),
                model_path=self._model_path,
                text=text,
            )
            _LOGGER.info("tts.synthesised",
                         chars=len(text), wav_bytes=len(wav_bytes))
            return wav_bytes
        except Exception as exc:
            _LOGGER.error("tts.error", exc=str(exc))
            raise

    @property
    def is_ready(self) -> bool:
        return _PIPER_BIN.exists() and Path(self._model_path).exists()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate


async def _run_piper(piper_bin: str, model_path: str, text: str) -> bytes:
    """Run the Piper binary asynchronously, return WAV bytes."""
    proc = await asyncio.create_subprocess_exec(
        piper_bin,
        "--model", model_path,
        "--output_file", "-",  # write WAV to stdout
        "--quiet",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=text.encode("utf-8"))

    if proc.returncode != 0:
        raise RuntimeError(
            f"Piper exited {proc.returncode}: {stderr.decode()[:200]}"
        )

    return stdout


def _estimate_word_timings(text: str, wav_bytes: bytes) -> list[dict]:
    """
    Estimate per-word start/end times from WAV duration + word character lengths.

    Algorithm mirrors TalkMateAI's approach: distribute total speech duration
    proportionally across words, weighted by character count, with a small
    inter-word pause. Accurate enough to drive TalkingHead lip sync.
    """
    import re

    words = [w for w in re.split(r'\s+', text.strip()) if w]
    if not words:
        return []

    # Get total audio duration from WAV header
    try:
        with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
            duration_ms = wf.getnframes() / wf.getframerate() * 1000
    except Exception:
        duration_ms = len(words) * 300.0  # rough fallback

    # Reserve ~80 ms lead-in silence + 80 ms trail, and 40 ms inter-word gap
    lead_ms   = 80.0
    trail_ms  = 80.0
    gap_ms    = 40.0
    total_gap = gap_ms * (len(words) - 1)
    speech_ms = max(duration_ms - lead_ms - trail_ms - total_gap, len(words) * 50.0)

    # Weight by cleaned word length (strip punctuation, minimum 1)
    weights   = [max(len(re.sub(r'[^\w]', '', w)), 1) for w in words]
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
    """Return a short silent WAV (for empty input)."""
    n_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return buf.getvalue()
