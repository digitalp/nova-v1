"""
LLM service - supports Ollama (local), OpenAI, Google Gemini, and Anthropic.
Automatically falls back to Ollama gemma2:9b when the cloud provider is unavailable.

Set LLM_PROVIDER in .env to switch providers:
  LLM_PROVIDER=ollama      (default)
  LLM_PROVIDER=openai      + OPENAI_API_KEY + CLOUD_MODEL (e.g. gpt-4o-mini)
  LLM_PROVIDER=google      + GOOGLE_API_KEY + CLOUD_MODEL (e.g. gemini-2.0-flash)
  LLM_PROVIDER=anthropic   + ANTHROPIC_API_KEY + CLOUD_MODEL (e.g. claude-haiku-4-5-20251001)
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any

import httpx
import structlog

from avatar_backend.services._shared_http import _http_client

from avatar_backend.config import get_settings
from avatar_backend.models.messages import ToolCall
from avatar_backend.services.cost_log import CostLog as _CostLog

logger = structlog.get_logger()


def _build_operational_backend(settings) -> tuple[Any | None, str | None]:
    # Always build Gemini backend if key exists — used when per-task toggle is on
    if settings.google_api_key:
        return _GeminiBackend(settings), "google"
    return None, None


def _format_exc_reason(exc: Exception | None) -> str:
    if exc is None:
        return "unknown"
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"[:120]
    return type(exc).__name__[:120]



_DEFAULT_MODELS = {
    "openai":    "gpt-4o-mini",
    "google":    "gemini-2.5-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}
from avatar_backend.services.llm_backends import (
    _OllamaBackend,
    _OpenAICompatBackend,
    _GeminiBackend,
    _AnthropicBackend,
    _OllamaFallbackBackend,
    _select_local_text_model,
    _select_fast_local_text_model,
    set_cost_log as _set_backends_cost_log,
)
from avatar_backend.services.llm_vision import (
    _GPU_GATE,
    _VISION_SEMAPHORE,
    _DEFAULT_IMAGE_PROMPT,
    _DOORBELL_IMAGE_PROMPT,
    _MOTION_IMAGE_PROMPT,
    set_gemini_key_pool,
    _get_gemini_key,
    _report_gemini_429,
    _report_gemini_success,
    _gemini_attempt_budget,
    _vision_ollama_url,
    _vision_is_remote,
    _ollama_describe_image,
    _gemini_describe_image,
    _openai_describe_image,
)


# ── Public service ────────────────────────────────────────────────────────────

class LLMService:
    """
    Routes LLM requests to the configured provider.
    Switch providers via LLM_PROVIDER in .env - no code changes needed.
    """

    _FALLBACK_MODEL = "gemma2:9b"

    def __init__(self) -> None:
        settings = get_settings()
        provider = settings.llm_provider.lower()
        if provider == "openai":
            self._backend: Any = _OpenAICompatBackend(settings)
        elif provider == "google":
            self._backend = _GeminiBackend(settings)
        elif provider == "anthropic":
            self._backend = _AnthropicBackend(settings)
        else:
            self._backend = _OllamaBackend(settings)
        self._provider = provider

        # Ollama gemma2:9b failover - only for cloud providers
        if provider != "ollama":
            self._fallback: _OllamaBackend | None = _OllamaFallbackBackend(
                settings.ollama_url, self._FALLBACK_MODEL
            )
        else:
            self._fallback = None
        self._local_text_model = _select_local_text_model(settings)
        self._local_text_backend = _OllamaFallbackBackend(settings.ollama_url, self._local_text_model)
        self._fast_local_text_model = _select_fast_local_text_model(settings)
        self._fast_local_text_backend = _OllamaFallbackBackend(settings.ollama_url, self._fast_local_text_model)
        self._operational_backend, self._operational_provider = _build_operational_backend(settings)
        if self._operational_backend is not None and self._operational_provider == provider:
            self._operational_backend = self._backend

        logger.info("llm.provider", provider=provider, model=self._backend.model_name,
                    fallback=self._FALLBACK_MODEL if self._fallback else None)
        logger.info("llm.local_text_provider", provider="ollama", model=self._local_text_model)
        logger.info("llm.fast_local_text_provider", provider="ollama", model=self._fast_local_text_model)
        if self._operational_backend is not None:
            logger.info(
                "llm.operational_provider",
                provider=self._operational_provider or provider,
                model=self._operational_backend.model_name,
            )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
    ) -> tuple[str, list[ToolCall]]:
        try:
            # Check runtime toggle for Gemini chat
            from avatar_backend.services.home_runtime import load_home_runtime_config
            _rt = load_home_runtime_config()
            backend = self._operational_backend if (_rt.use_gemini_chat and self._operational_backend) else self._backend
            return await backend.chat(messages, use_tools)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if self._fallback is None:
                _reason = str(exc)[:120]
                raise RuntimeError(f"LLM unavailable: {_reason}") from exc
            logger.warning("llm.primary_failed_using_fallback",
                           provider=self._provider,
                           fallback=self._FALLBACK_MODEL,
                           reason=str(exc)[:120])
            try:
                # Sanitize messages for Ollama — strip tool_calls and tool role messages
                clean = []
                for m in messages:
                    if m.get("role") == "tool":
                        clean.append({"role": "user", "content": "[Tool result]: " + str(m.get("content", ""))})
                    elif "tool_calls" in m:
                        clean.append({"role": m.get("role", "assistant"), "content": m.get("content", "") or "(tool call)"})
                    else:
                        clean.append(m)
                return await self._fallback.chat(clean, use_tools=False)
            except Exception as fb_exc:
                raise RuntimeError(f"LLM fallback also failed: {fb_exc}") from fb_exc

    async def chat_operational(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
        purpose: str = "operational_chat",
        use_gemini: bool = False,
    ) -> tuple[str, list[ToolCall]]:
        backend = self._operational_backend if use_gemini else None
        if backend is None:
            return await self.chat(messages, use_tools=use_tools)
        if backend is self._backend:
            return await self.chat(messages, use_tools=use_tools)
        try:
            return await backend.chat(messages, use_tools)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            logger.warning(
                "llm.operational_failed_using_default",
                purpose=purpose,
                provider=self._operational_provider or self._provider,
                model=backend.model_name,
                reason=_format_exc_reason(exc),
            )
            return await self.chat(messages, use_tools=use_tools)

    def set_cost_log(self, log: _CostLog) -> None:
        _set_backends_cost_log(log)

    async def is_ready(self) -> bool:
        if isinstance(self._backend, _OllamaBackend):
            try:
                resp = await _http_client().get(f"{self._backend._base_url}/api/tags", timeout=3.0)
                if resp.status_code != 200:
                    return False
                models = [m["name"] for m in resp.json().get("models", [])]
                family = self._backend._model.split(":")[0]
                return any(family in m for m in models)
            except Exception:
                return False
        # Cloud providers - assume ready if API key is set
        settings = get_settings()
        provider = settings.llm_provider.lower()
        key_map  = {"openai": settings.openai_api_key,
                    "google": settings.google_api_key,
                    "anthropic": settings.anthropic_api_key}
        return bool(key_map.get(provider, ""))

    async def generate_text(self, prompt: str, timeout_s: float = 180.0) -> str:
        """
        Simple text-in / text-out generation using the active provider.
        Uses a longer timeout than chat() - suitable for large one-shot tasks
        like system prompt updates. No tools, temperature 0.2.
        """
        try:
            return await self._backend.generate_text(prompt, timeout_s=timeout_s)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if self._fallback is None:
                raise RuntimeError(f"LLM unavailable: {exc}") from exc
            logger.warning("llm.primary_failed_using_fallback",
                           provider=self._provider,
                           fallback=self._FALLBACK_MODEL,
                           reason=str(exc)[:120])
            try:
                return await self._fallback.generate_text(prompt, timeout_s=min(timeout_s, 120.0))
            except Exception as fb_exc:
                raise RuntimeError(f"LLM fallback also failed: {fb_exc}") from fb_exc

    async def generate_text_local(self, prompt: str, timeout_s: float = 120.0) -> str:
        """Strictly local text generation via the preferred Ollama model."""
        return await self._local_text_backend.generate_text(prompt, timeout_s=timeout_s)

    async def generate_text_local_fast(self, prompt: str, timeout_s: float = 60.0) -> str:
        """Strictly local text generation via the faster preferred Ollama model."""
        return await self._fast_local_text_backend.generate_text(prompt, timeout_s=timeout_s)

    async def _generate_text_local_resilient(
        self,
        *,
        backend: _OllamaFallbackBackend,
        prompt: str,
        timeout_s: float,
        retry_delay_s: float,
        purpose: str,
        fallback_timeout_s: float | None = None,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return await backend.generate_text(prompt, timeout_s=timeout_s)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "llm.local_retry_scheduled",
                        purpose=purpose,
                        model=backend.model_name,
                        retry_delay_s=retry_delay_s,
                        reason=_format_exc_reason(exc),
                    )
                    await asyncio.sleep(retry_delay_s)
                    continue
                break

        if self._provider != "ollama":
            logger.warning(
                "llm.local_failed_using_cloud",
                purpose=purpose,
                local_model=backend.model_name,
                provider=self._provider,
                cloud_model=self._backend.model_name,
                reason=_format_exc_reason(last_exc),
            )
            return await self.generate_text(prompt, timeout_s=fallback_timeout_s or min(timeout_s, 45.0))

        if last_exc is not None:
            raise RuntimeError(f"Local LLM unavailable after retry: {last_exc}") from last_exc
        raise RuntimeError("Local LLM unavailable after retry")

    async def generate_text_local_resilient(
        self,
        prompt: str,
        timeout_s: float = 120.0,
        retry_delay_s: float = 2.0,
        fallback_timeout_s: float | None = None,
        purpose: str = "local_text",
    ) -> str:
        return await self._generate_text_local_resilient(
            backend=self._local_text_backend,
            prompt=prompt,
            timeout_s=timeout_s,
            retry_delay_s=retry_delay_s,
            purpose=purpose,
            fallback_timeout_s=fallback_timeout_s,
        )

    async def generate_text_local_fast_resilient(
        self,
        prompt: str,
        timeout_s: float = 60.0,
        retry_delay_s: float = 2.0,
        fallback_timeout_s: float | None = None,
        purpose: str = "fast_local_text",
    ) -> str:
        return await self._generate_text_local_resilient(
            backend=self._fast_local_text_backend,
            prompt=prompt,
            timeout_s=timeout_s,
            retry_delay_s=retry_delay_s,
            purpose=purpose,
            fallback_timeout_s=fallback_timeout_s,
        )

    async def generate_text_grounded(self, prompt: str, timeout_s: float = 30.0) -> str:
        """Generate text using Gemini with Google Search grounding (web access).

        Falls back to standard generate_text if the operational backend is not
        Gemini or does not support search grounding.
        """
        backend = self._operational_backend
        if backend is not None and hasattr(backend, "generate_text_with_search"):
            try:
                return await backend.generate_text_with_search(prompt, timeout_s=timeout_s)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                logger.warning(
                    "llm.grounded_failed_using_fallback",
                    reason=_format_exc_reason(exc),
                )
        return await self.generate_text(prompt, timeout_s=timeout_s)

    async def _chat_local_resilient(
        self,
        *,
        backend: _OllamaFallbackBackend,
        messages: list[dict[str, Any]],
        use_tools: bool,
        retry_delay_s: float,
        purpose: str,
    ) -> tuple[str, list[ToolCall]]:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                return await backend.chat(messages, use_tools)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "llm.local_chat_retry_scheduled",
                        purpose=purpose,
                        model=backend.model_name,
                        retry_delay_s=retry_delay_s,
                        reason=_format_exc_reason(exc),
                    )
                    await asyncio.sleep(retry_delay_s)
                    continue
                break

        if self._provider != "ollama":
            logger.warning(
                "llm.local_chat_failed_using_cloud",
                purpose=purpose,
                local_model=backend.model_name,
                provider=self._provider,
                cloud_model=self._backend.model_name,
                reason=_format_exc_reason(last_exc),
            )
            return await self.chat(messages, use_tools=use_tools)

        if last_exc is not None:
            raise RuntimeError(f"Local chat unavailable after retry: {last_exc}") from last_exc
        raise RuntimeError("Local chat unavailable after retry")

    async def chat_local(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
    ) -> tuple[str, list[ToolCall]]:
        """Strictly local chat via the preferred Ollama model."""
        return await self._local_text_backend.chat(messages, use_tools)

    async def chat_local_fast(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
    ) -> tuple[str, list[ToolCall]]:
        """Strictly local chat via the faster preferred Ollama model."""
        return await self._fast_local_text_backend.chat(messages, use_tools)

    async def chat_local_fast_resilient(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
        retry_delay_s: float = 2.0,
        purpose: str = "fast_local_chat",
    ) -> tuple[str, list[ToolCall]]:
        return await self._chat_local_resilient(
            backend=self._fast_local_text_backend,
            messages=messages,
            use_tools=use_tools,
            retry_delay_s=retry_delay_s,
            purpose=purpose,
        )

    async def describe_image(self, image_bytes: bytes, prompt: str | None = None, system_instruction: str | None = None) -> str:
        """Describe a camera image using vision capability of the active LLM provider.
        Falls back to Ollama vision if the primary provider fails."""
        _prompt = prompt or _DEFAULT_IMAGE_PROMPT
        try:
            if self._provider == "google":
                api_key = _get_gemini_key() or self._backend._api_key
                return await _gemini_describe_image(image_bytes, api_key, self._backend._model, _prompt, system_instruction)
            if self._provider == "openai":
                return await _openai_describe_image(image_bytes, self._backend._api_key, self._backend._model, _prompt)
            if self._provider == "ollama":
                # Use remote Ollama if motion_vision_provider is ollama_remote
                settings = get_settings()
                if (settings.motion_vision_provider or "").strip().lower() == "ollama_remote":
                    return await _ollama_describe_image(image_bytes, _vision_ollama_url(), settings.ollama_vision_model, _prompt)
                return await _ollama_describe_image(image_bytes, self._backend._base_url, self._backend._vision_model, _prompt)
            return "Camera vision is not supported with the current LLM provider."
        except Exception as exc:
            _log_struct = structlog.get_logger()
            _log_struct.error("llm.describe_image_error", exc=str(exc))
            # Fallback to Ollama vision if primary provider failed and we're not already on Ollama
            if self._provider != "ollama":
                try:
                    settings = get_settings()
                    _log_struct.info("llm.describe_image_ollama_fallback")
                    return await _ollama_describe_image(image_bytes, _vision_ollama_url(), settings.ollama_vision_model, _prompt)
                except Exception as fb_exc:
                    _log_struct.error("llm.describe_image_ollama_fallback_failed", exc=str(fb_exc))
            return "I couldn't analyze the camera image right now."

    async def describe_image_with_gemini(self, image_bytes: bytes, prompt: str | None = None, system_instruction: str | None = None, camera_id: str | None = None) -> str:
        """
        Describe a camera image using Gemini vision, regardless of the active LLM provider.
        Uses the key pool for rotation across multiple API keys.
        Falls back to Ollama vision if all keys exhausted.
        Limited to 2 concurrent calls to prevent server overload.
        """
        # Non-blocking: if 2 vision calls already in-flight, fall back immediately
        if _VISION_SEMAPHORE.locked():
            structlog.get_logger().warning("gemini_pool.vision_busy")
            return await self._fallback_to_ollama_vision(image_bytes, prompt)

        async with _VISION_SEMAPHORE:
            settings = get_settings()
            model = settings.cloud_model if settings.llm_provider.lower() == "google" else _DEFAULT_MODELS["google"]
            _prompt = prompt or _DEFAULT_IMAGE_PROMPT

            # Try each configured key at most once before falling back.
            _vision_t0 = time.monotonic()
            for _attempt in range(_gemini_attempt_budget()):
                api_key = _get_gemini_key(camera_id)
                if not api_key:
                    break
                try:
                    result = await _gemini_describe_image(image_bytes, api_key, model, _prompt, system_instruction)
                    _report_gemini_success(api_key, latency_ms=int((time.monotonic() - _vision_t0) * 1000))
                    return result
                except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                        last_exc = exc
                        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                            _report_gemini_429(api_key)
                            continue
                        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
                            _report_gemini_429(api_key)
                            continue
                        raise

            # All keys exhausted - fall back to Ollama
            return await self._fallback_to_ollama_vision(image_bytes, prompt)

    async def _fallback_to_ollama_vision(self, image_bytes: bytes, prompt: str | None = None) -> str:
        try:
            settings = get_settings()
            structlog.get_logger().info("llm.describe_image_gemini_to_ollama_fallback")
            return await _ollama_describe_image(image_bytes, _vision_ollama_url(), settings.ollama_vision_model, prompt or _DEFAULT_IMAGE_PROMPT)
        except Exception as fb_exc:
            structlog.get_logger().error("llm.describe_image_all_failed", exc=str(fb_exc))
            return "I couldn't analyze the camera image right now."

    @property
    def model_name(self) -> str:
        return self._backend.model_name

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def operational_model_name(self) -> str:
        backend = self._operational_backend
        return backend.model_name if backend is not None else self._backend.model_name

    @property
    def operational_provider_name(self) -> str:
        return self._operational_provider or self._provider

    @property
    def local_text_model_name(self) -> str:
        return self._local_text_model

    @property
    def fast_local_text_model_name(self) -> str:
        return self._fast_local_text_model

    @property
    def gemini_model_name(self) -> str:
        settings = get_settings()
        if settings.llm_provider.lower() == "google" and settings.cloud_model:
            return settings.cloud_model
        return _DEFAULT_MODELS["google"]

    @property
    def gemini_vision_provider_name(self) -> str:
        settings = get_settings()
        if settings.google_api_key:
            return "google"
        return self._provider

    @property
    def gemini_vision_effective_model_name(self) -> str:
        settings = get_settings()
        if settings.google_api_key:
            if settings.llm_provider.lower() == "google" and settings.cloud_model:
                return settings.cloud_model
            return _DEFAULT_MODELS["google"]
        return getattr(self._backend, "_vision_model", self._backend.model_name)
