"""Vision describe helpers — Ollama, Gemini, and OpenAI image analysis."""
from __future__ import annotations
import asyncio
import io
import time

import httpx
import structlog

from avatar_backend.config import get_settings
from avatar_backend.services._shared_http import _http_client

# ── Vision helpers ───────────────────────────────────────────────────────────

from avatar_backend.services.gpu_gate import GPUMemoryGate as _GPUMemoryGate
_GPU_GATE = _GPUMemoryGate(min_free_mb=200)  # Ollama swaps models dynamically - just need enough for CUDA overhead

# Gemini key pool - set by bootstrap, used by vision calls
_gemini_key_pool = None

# Limit concurrent vision API calls to prevent server overload
_VISION_SEMAPHORE = asyncio.Semaphore(2)

def set_gemini_key_pool(pool) -> None:
    global _gemini_key_pool
    _gemini_key_pool = pool


def _gemini_attempt_budget() -> int:
    if _gemini_key_pool and _gemini_key_pool.size:
        return max(1, _gemini_key_pool.size)
    return 1

def _get_gemini_key(camera_id: str | None = None) -> str | None:
    """Get a Gemini API key from the pool, or fall back to settings."""
    if _gemini_key_pool and _gemini_key_pool.size:
        return _gemini_key_pool.get_key(camera_id)
    return get_settings().google_api_key or None

def _report_gemini_429(key: str) -> None:
    if _gemini_key_pool:
        _gemini_key_pool.report_429(key)


def _report_gemini_success(key: str, latency_ms: float = 0, tokens: int = 0) -> None:
    if _gemini_key_pool:
        _gemini_key_pool.report_success(key, latency_ms=latency_ms, tokens=tokens)


def _report_gemini_error(key: str) -> None:
    if _gemini_key_pool:
        _gemini_key_pool.report_error(key)


def _vision_ollama_url() -> str:
    """Return the Ollama URL for vision calls - remote if configured, local otherwise."""
    from avatar_backend.config import get_settings
    s = get_settings()
    return (s.ollama_vision_url or "").strip() or s.ollama_url


def _vision_is_remote() -> bool:
    from avatar_backend.config import get_settings
    return bool((get_settings().ollama_vision_url or "").strip())

_DEFAULT_IMAGE_PROMPT = (
    "Describe what you see in this security camera image in 2-3 sentences. "
    "Focus on people, vehicles, objects, and any notable activity."
)

_DOORBELL_IMAGE_PROMPT = (
    "This is a snapshot from a front-door security camera taken because the doorbell was just rung. "
    "Is there a person clearly visible at or approaching the door? "
    "If YES: describe them in one sentence - clothing colours and any items they are carrying only. "
    "Do NOT mention age, race, gender, or any personal attributes. "
    "If NO person is clearly visible, reply with exactly: NO_PERSON"
)


_MOTION_IMAGE_PROMPT = (
    "Motion has been detected on an outdoor camera. Analyse this image and decide if it warrants an alert. "
    "Only alert if you see: a person, an unfamiliar or unexpected vehicle, or unexpected activity. "
    "If the motion was caused only by a known/parked vehicle or there is no obvious cause, "
    "reply with exactly: NO_MOTION "
    "Otherwise reply with a single concise sentence describing what you see."
)

_MOONDREAM_MOTION_PROMPT = (
    "Describe what you see in this outdoor security camera image in one sentence. "
    "Focus on people, vehicles, and activity."
)

_MOONDREAM_DEFAULT_PROMPT = (
    "Describe what you see in this image in one sentence."
)

_MOONDREAM_DOORBELL_PROMPT = (
    "Describe the person at the door. Mention clothing and any items they carry."
)


def _is_moondream(model: str) -> bool:
    return "moondream" in (model or "").lower()

def _resize_for_ollama(image_bytes: bytes, max_width: int = 1280) -> bytes:
    """Downscale image to max_width if wider, preserving aspect ratio.

    llama3.2-vision tiles images at 560x560.  A 2560x1440 camera frame creates
    ~15 tiles, which takes >60 s to process on an RTX 2060.  Capping at 1280 px
    wide reduces that to ~4 tiles and keeps inference well under 60 s with no
    meaningful quality loss for security-camera analysis.
    """
    try:
        import io
        from PIL import Image as _PILImage  # type: ignore[import]
        img = _PILImage.open(io.BytesIO(image_bytes))
        if img.width <= max_width:
            return image_bytes
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, _PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return image_bytes  # fall back to original if Pillow unavailable


async def _ollama_describe_image(image_bytes: bytes, base_url: str, model: str, prompt: str = _DEFAULT_IMAGE_PROMPT) -> str:
    # Swap to simpler prompts for small models like Moondream
    if _is_moondream(model):
        if prompt == _MOTION_IMAGE_PROMPT:
            prompt = _MOONDREAM_MOTION_PROMPT
        elif prompt == _DOORBELL_IMAGE_PROMPT:
            prompt = _MOONDREAM_DOORBELL_PROMPT
        elif prompt == _DEFAULT_IMAGE_PROMPT:
            prompt = _MOONDREAM_DEFAULT_PROMPT
    # Only gate local GPU - remote Ollama has its own resources
    if not _vision_is_remote():
        acquired = await _GPU_GATE.acquire(caller="ollama_vision")
        if not acquired:
            raise RuntimeError("Insufficient GPU memory for vision model")
    try:
        import base64 as _b64
        image_bytes = _resize_for_ollama(image_bytes)
        b64 = _b64.b64encode(image_bytes).decode()
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [b64],
            }],
            "stream": False,
        }
        resp = await _http_client().post(f"{base_url}/api/chat", json=payload, timeout=httpx.Timeout(180.0))
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    finally:
        if not _vision_is_remote():
            _GPU_GATE.release()


async def _gemini_describe_image(image_bytes: bytes, api_key: str, model: str, prompt: str = _DEFAULT_IMAGE_PROMPT, system_instruction: str | None = None) -> str:
    import base64 as _b64
    b64 = _b64.b64encode(image_bytes).decode()
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": prompt},
            ],
        }],
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = await _http_client().post(
        url, json=payload,
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
        timeout=httpx.Timeout(8.0),
    )
    resp.raise_for_status()
    data = resp.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return " ".join(p.get("text", "") for p in parts if "text" in p).strip()


async def _openai_describe_image(image_bytes: bytes, api_key: str, model: str, prompt: str = _DEFAULT_IMAGE_PROMPT) -> str:
    import base64 as _b64
    b64 = _b64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": 300,
    }
    resp = await _http_client().post(
        "https://api.openai.com/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=httpx.Timeout(8.0),
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


async def _fireworks_describe_image(image_bytes: bytes, api_key: str, model: str, prompt: str) -> str:
    """Describe an image using Fireworks AI vision model (OpenAI-compatible format)."""
    import base64
    img_b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]
        }],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(
            "https://api.fireworks.ai/inference/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
