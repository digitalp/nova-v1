from __future__ import annotations

import asyncio
import io
import os
import wave
from pathlib import Path

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from huggingface_hub import hf_hub_download
from pydantic import BaseModel

MODEL_REPO = os.environ.get("INTRON_AFRO_TTS_MODEL_REPO", "intronhealth/afro-tts")
MODEL_DIR = Path(os.environ.get("INTRON_AFRO_TTS_MODEL_DIR", "/models/intron_afro_tts"))
DEFAULT_REFERENCE_WAV = os.environ.get(
    "INTRON_AFRO_TTS_REFERENCE_WAV",
    str(MODEL_DIR / "audios" / "reference_accent.wav"),
)
DEFAULT_LANGUAGE = os.environ.get("INTRON_AFRO_TTS_LANGUAGE", "en")
HF_TOKEN = os.environ.get("HF_TOKEN") or None
REQUIRED_FILES = [
    "config.json",
    "model.pth",
    "dvae.pth",
    "mel_stats.pth",
    "vocab.json",
    "audios/reference_accent.wav",
]

app = FastAPI(title="Intron Afro TTS Sidecar")
_ENGINE = None
_ENGINE_LOCK = asyncio.Lock()
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_SAMPLE_RATE = 24000


class SynthBody(BaseModel):
    text: str
    reference_wav: str | None = None
    language: str | None = None


def _ensure_model_downloaded() -> Path:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if all((MODEL_DIR / rel).exists() for rel in REQUIRED_FILES):
        return MODEL_DIR
    for rel_path in REQUIRED_FILES:
        hf_hub_download(
            repo_id=MODEL_REPO,
            filename=rel_path,
            local_dir=str(MODEL_DIR),
            token=HF_TOKEN,
        )
    return MODEL_DIR


def _load_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    model_dir = _ensure_model_downloaded()
    from TTS.api import TTS  # Imported lazily inside the sidecar runtime.

    engine = TTS(
        # XTTS expects the checkpoint directory here and resolves model.pth itself.
        model_path=str(model_dir),
        config_path=str(model_dir / "config.json"),
    )
    if hasattr(engine, "to"):
        engine = engine.to(_DEVICE)
    _ENGINE = engine
    return _ENGINE


async def _get_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    async with _ENGINE_LOCK:
        if _ENGINE is None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _load_engine)
    return _ENGINE


def _pcm_to_wav_bytes(audio: np.ndarray) -> bytes:
    audio = np.asarray(audio, dtype=np.float32)
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "device": _DEVICE,
        "repo": MODEL_REPO,
        "model_dir": str(MODEL_DIR),
        "reference_wav": DEFAULT_REFERENCE_WAV,
        "loaded": _ENGINE is not None,
    }


@app.get("/v1/voices")
async def list_voices():
    """List available reference WAV files that can be used as voice presets."""
    audios_dir = MODEL_DIR / "audios"
    voices = []
    if audios_dir.exists():
        for wav_file in sorted(audios_dir.glob("*.wav")):
            name = wav_file.stem.replace("_", " ").title()
            voices.append({
                "id": wav_file.stem,
                "name": name,
                "path": str(wav_file),
            })
    return {"voices": voices, "default": DEFAULT_REFERENCE_WAV}


@app.post("/v1/synth")
async def synth(body: SynthBody):
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    _ensure_model_downloaded()
    reference_wav = (body.reference_wav or DEFAULT_REFERENCE_WAV).strip()
    language = (body.language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
    if not Path(reference_wav).exists():
        raise HTTPException(status_code=400, detail=f"reference_wav not found: {reference_wav}")

    engine = await _get_engine()

    def _run():
        return engine.tts(
            text=text,
            speaker_wav=reference_wav,
            language=language,
        )

    loop = asyncio.get_event_loop()
    try:
        audio = await loop.run_in_executor(None, _run)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    wav_bytes = _pcm_to_wav_bytes(np.asarray(audio))
    return Response(content=wav_bytes, media_type="audio/wav")
