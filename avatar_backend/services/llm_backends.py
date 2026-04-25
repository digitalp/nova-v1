"""
LLM provider backend classes: Ollama, OpenAI-compat, Gemini, Anthropic, and
fallback Ollama. Also includes message-conversion and tool-call parsing helpers.
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any

import httpx
import structlog
logger = structlog.get_logger()

_DEFAULT_MODELS = {
    "openai":    "gpt-4o-mini",
    "google":    "gemini-2.5-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}
from avatar_backend.config import get_settings
from avatar_backend.models.messages import ToolCall
from avatar_backend.services._shared_http import _http_client
from avatar_backend.services.cost_log import CostLog as _CostLog
from avatar_backend.services.ha_tool_schemas import HA_TOOLS, _ANTHROPIC_TOOLS
from avatar_backend.services.llm_vision import (
    _GPU_GATE,
    _VISION_SEMAPHORE,
    set_gemini_key_pool,
    _get_gemini_key,
    _report_gemini_429,
    _report_gemini_success,
    _gemini_attempt_budget,
    _vision_ollama_url,
    _vision_is_remote,
)

_sl = structlog.get_logger()

# Cost log — set via set_cost_log() by LLMService after construction
_cost_log: _CostLog | None = None


def set_cost_log(log: _CostLog | None) -> None:
    """Called by LLMService.set_cost_log to wire up the cost log globally."""
    global _cost_log
    _cost_log = log


# ── Tool call parsing helpers ─────────────────────────────────────────────────

def _parse_tool_calls_openai(raw: list[dict]) -> list[ToolCall]:
    result = []
    for tc in raw:
        func = tc.get("function", {})
        name = func.get("name", "").strip()
        if not name:
            continue
        args = func.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        result.append(ToolCall(function_name=name, arguments=args))
    return result


def _parse_tool_calls_anthropic(content_blocks: list[dict]) -> list[ToolCall]:
    return [
        ToolCall(function_name=b.get("name", ""), arguments=b.get("input", {}))
        for b in content_blocks if b.get("type") == "tool_use"
    ]


def _to_ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize chat history into Ollama-compatible wire format.

    Older Nova sessions may contain OpenAI-style assistant tool calls with
    ``id``/``type`` wrappers. Ollama expects only ``{"function": ...}`` tool
    call entries, and it rejects unknown structure with HTTP 400.
    """
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        entry: dict[str, Any] = {
            "role": msg.get("role", ""),
            "content": msg.get("content") or "",
        }
        raw_tool_calls = msg.get("tool_calls") or []
        if raw_tool_calls:
            cleaned_tool_calls: list[dict[str, Any]] = []
            for tc in raw_tool_calls:
                function = tc.get("function") or {}
                name = str(function.get("name") or "").strip()
                if not name:
                    continue
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                cleaned_tool_calls.append({
                    "function": {
                        "name": name,
                        "arguments": arguments if isinstance(arguments, dict) else {},
                    }
                })
            if cleaned_tool_calls:
                entry["tool_calls"] = cleaned_tool_calls
        normalized.append(entry)
    return normalized


def _log(provider, model, t0, text, tools, input_tokens=0, output_tokens=0, purpose="chat"):
    elapsed = int((time.monotonic() - t0) * 1000)
    logger.info(
        "llm.response",
        provider=provider,
        model=model,
        elapsed_ms=elapsed,
        has_tool_calls=bool(tools),
        text_chars=len(text),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    if _cost_log and (input_tokens or output_tokens):
        _cost_log.record(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            purpose=purpose,
            elapsed_ms=elapsed,
        )


# ── Provider backends ─────────────────────────────────────────────────────────

class _OllamaBackend:
    def __init__(self, settings) -> None:
        self._base_url     = settings.ollama_url.rstrip("/")
        self._model        = settings.ollama_model
        self._vision_model = settings.ollama_vision_model

    def _supports_tools(self) -> bool:
        family = self._model.split(":")[0].lower()
        return family not in {"gemma2"}

    async def chat(self, messages: list[dict], use_tools: bool) -> tuple[str, list[ToolCall]]:
        payload: dict[str, Any] = {
            "model":    self._model,
            "messages": _to_ollama_messages(messages),
            "stream":   False,
            "options":  {"temperature": 0.7, "num_ctx": 4096, "num_predict": 200},
        }
        if use_tools and self._supports_tools():
            payload["tools"] = HA_TOOLS
        elif use_tools:
            logger.info("llm.ollama_tools_disabled", model=self._model)

        _GPU_GATE.chat_started()
        t0 = time.monotonic()
        try:
            resp = await _http_client().post(f"{self._base_url}/api/chat", json=payload, timeout=httpx.Timeout(90.0))
            resp.raise_for_status()
        finally:
            _GPU_GATE.chat_finished()

        data    = resp.json()
        message = data.get("message", {})
        _raw_content = message.get("content")
        if isinstance(_raw_content, list):
            text = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in _raw_content
            ).strip()
        else:
            text = (_raw_content or "").strip()
        tools   = _parse_tool_calls_openai(message.get("tool_calls") or [])
        _log("ollama", self._model, t0, text, tools,
             input_tokens=data.get("prompt_eval_count", 0),
             output_tokens=data.get("eval_count", 0))
        return text, tools

    async def generate_text(self, prompt: str, timeout_s: float = 180.0) -> str:
        payload: dict[str, Any] = {
            "model":   self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":  False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        t0 = time.monotonic()
        resp = await _http_client().post(f"{self._base_url}/api/chat", json=payload, timeout=httpx.Timeout(timeout_s))
        resp.raise_for_status()
        _d = resp.json()
        text = (_d.get("message", {}).get("content") or "").strip()
        _log("ollama", self._model, t0, text, [],
             input_tokens=_d.get("prompt_eval_count", 0),
             output_tokens=_d.get("eval_count", 0),
             purpose="proactive")
        return text

    @property
    def model_name(self) -> str:
        return self._model


class _OpenAICompatBackend:
    """OpenAI chat completions API."""

    def __init__(self, settings) -> None:
        self._base_url = "https://api.openai.com/v1"
        self._api_key  = settings.openai_api_key
        self._model    = settings.cloud_model or _DEFAULT_MODELS["openai"]

    async def chat(self, messages: list[dict], use_tools: bool) -> tuple[str, list[ToolCall]]:
        payload: dict[str, Any] = {
            "model":       self._model,
            "messages":    messages,
            "temperature": 0.7,
        }
        if use_tools:
            payload["tools"] = HA_TOOLS

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }

        t0 = time.monotonic()
        resp = await _http_client().post(
            f"{self._base_url}/chat/completions",
            json=payload, headers=headers,
            timeout=httpx.Timeout(60.0),
        )
        resp.raise_for_status()

        data    = resp.json()
        choice  = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        text    = (message.get("content") or "").strip()
        tools   = _parse_tool_calls_openai(message.get("tool_calls") or [])
        _usage  = data.get("usage", {})
        _log("openai", self._model, t0, text, tools,
             input_tokens=_usage.get("prompt_tokens", 0),
             output_tokens=_usage.get("completion_tokens", 0))
        return text, tools

    async def generate_text(self, prompt: str, timeout_s: float = 180.0) -> str:
        payload: dict[str, Any] = {
            "model":       self._model,
            "messages":    [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        t0 = time.monotonic()
        resp = await _http_client().post(f"{self._base_url}/chat/completions", json=payload, headers=headers, timeout=httpx.Timeout(timeout_s))
        resp.raise_for_status()
        _d = resp.json()
        text = (_d.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        _u = _d.get("usage", {})
        _log("openai", self._model, t0, text, [],
             input_tokens=_u.get("prompt_tokens", 0),
             output_tokens=_u.get("completion_tokens", 0),
             purpose="proactive")
        return text

    @property
    def model_name(self) -> str:
        return self._model


def _to_gemini_contents(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert OpenAI-format messages to Gemini contents + system instruction."""
    system_parts: list[str] = []
    contents: list[dict]    = []
    pending_calls: list[dict] = []

    for msg in messages:
        role    = msg["role"]
        content = msg.get("content") or ""

        if role == "system":
            system_parts.append(content)

        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": content}]})

        elif role == "assistant":
            tc_list = msg.get("tool_calls") or []
            pending_calls = list(tc_list)
            parts: list[dict] = []
            if content:
                parts.append({"text": content})
            for tc in tc_list:
                func = tc.get("function", {})
                parts.append({"functionCall": {
                    "name": func.get("name", ""),
                    "args": func.get("arguments", {}),
                }})
            if parts:
                contents.append({"role": "model", "parts": parts})

        elif role == "tool":
            # Match with the pending assistant tool call to get function name
            func_name = "tool_result"
            if pending_calls:
                tc = pending_calls.pop(0)
                func_name = tc.get("function", {}).get("name", "tool_result")
            contents.append({"role": "user", "parts": [{"functionResponse": {
                "name":     func_name,
                "response": {"result": content},
            }}]})

    return "\n\n".join(system_parts), contents


def _to_gemini_tools() -> list[dict]:
    return [{"functionDeclarations": [
        {
            "name":        t["function"]["name"],
            "description": t["function"]["description"],
            "parameters":  t["function"]["parameters"],
        }
        for t in HA_TOOLS
    ]}]


class _GeminiBackend:
    """Native Google Gemini API (generativelanguage.googleapis.com)."""

    def __init__(self, settings) -> None:
        self._api_key = settings.google_api_key
        self._model   = settings.cloud_model or _DEFAULT_MODELS["google"]

    async def chat(self, messages: list[dict], use_tools: bool) -> tuple[str, list[ToolCall]]:
        system_text, contents = _to_gemini_contents(messages)

        payload: dict[str, Any] = {
            "contents":         contents,
            "generationConfig": {"temperature": 0.7},
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if use_tools:
            payload["tools"] = _to_gemini_tools()

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models"
            f"/{self._model}:generateContent"
        )

        t0 = time.monotonic()
        last_exc = None
        _empty_retried = False
        for _attempt in range(_gemini_attempt_budget()):
            api_key = _get_gemini_key()
            if not api_key:
                raise httpx.HTTPStatusError("All Gemini keys exhausted", request=None, response=type("R", (), {"status_code": 429})())
            try:
                resp = await _http_client().post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
                    timeout=httpx.Timeout(60.0),
                )
                resp.raise_for_status()
                data = resp.json()
                _cands = data.get("candidates") or []
                _cand0 = _cands[0] if _cands else {}
                _content = _cand0.get("content") or {}
                _parts_raw = _content.get("parts", []) if isinstance(_content, dict) else []

                # Gemini 2.5 Flash sometimes returns STOP with 0 parts — retry once.
                if not _parts_raw and _cand0.get("finishReason") == "STOP" and not _empty_retried:
                    import structlog as _sl
                    _sl.get_logger().warning("gemini.empty_stop_retry",
                        usage=data.get("usageMetadata"),
                    )
                    _empty_retried = True
                    continue

                parts = [p for p in _parts_raw if not p.get("thought")]

                text  = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                tools = [
                    ToolCall(
                        function_name=p["functionCall"]["name"],
                        arguments=p["functionCall"].get("args", {}),
                    )
                    for p in parts if "functionCall" in p
                ]
                _um = data.get("usageMetadata", {})
                _log("google", self._model, t0, text, tools,
                     input_tokens=_um.get("promptTokenCount", 0),
                     output_tokens=_um.get("candidatesTokenCount", 0))
                _report_gemini_success(
                    api_key,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    tokens=_um.get("totalTokenCount", 0),
                )
                return text, tools
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    _report_gemini_429(api_key)
                    continue
                if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
                    _report_gemini_429(api_key)
                    continue
                raise
        else:
            if last_exc: raise last_exc
            raise RuntimeError("Gemini chat failed after rotation")

    async def generate_text(self, prompt: str, timeout_s: float = 180.0) -> str:
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2},
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models"
            f"/{self._model}:generateContent"
        )
        t0 = time.monotonic()
        last_exc = None
        for _attempt in range(_gemini_attempt_budget()):
            api_key = _get_gemini_key()
            if not api_key:
                raise httpx.HTTPStatusError("All Gemini keys exhausted", request=None, response=type("R", (), {"status_code": 429})())
            try:
                resp = await _http_client().post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
                    timeout=httpx.Timeout(timeout_s),
                )
                resp.raise_for_status()
                _d = resp.json()
                parts = (_d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                _um = _d.get("usageMetadata", {})
                _log("google", self._model, t0, text, [],
                     input_tokens=_um.get("promptTokenCount", 0),
                     output_tokens=_um.get("candidatesTokenCount", 0),
                     purpose="proactive")
                return text
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    _report_gemini_429(api_key)
                    continue
                if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
                    _report_gemini_429(api_key)
                    continue
                raise
        if last_exc: raise last_exc
        return ""

    async def generate_text_with_search(self, prompt: str, timeout_s: float = 30.0) -> str:
        """Call Gemini with Google Search grounding enabled for web-sourced answers."""
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4},
            "tools": [{"google_search": {}}],
        }
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models"
            f"/{self._model}:generateContent"
        )
        t0 = time.monotonic()
        last_exc = None
        for _attempt in range(_gemini_attempt_budget()):
            api_key = _get_gemini_key()
            if not api_key:
                raise httpx.HTTPStatusError("All Gemini keys exhausted", request=None, response=type("R", (), {"status_code": 429})())
            try:
                resp = await _http_client().post(
                    url, json=payload,
                    headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
                    timeout=httpx.Timeout(timeout_s),
                )
                resp.raise_for_status()
                _d = resp.json()
                parts = (_d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
                _um = _d.get("usageMetadata", {})
                _log("google", self._model, t0, text, [],
                     input_tokens=_um.get("promptTokenCount", 0),
                     output_tokens=_um.get("candidatesTokenCount", 0),
                     purpose="grounded")
                return text
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 503):
                    _report_gemini_429(api_key)
                    continue
                if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
                    _report_gemini_429(api_key)
                    continue
                raise
        if last_exc: raise last_exc
        return ""

    @property
    def model_name(self) -> str:
        return self._model


class _AnthropicBackend:
    def __init__(self, settings) -> None:
        self._api_key = settings.anthropic_api_key
        self._model   = settings.cloud_model or _DEFAULT_MODELS["anthropic"]

    async def chat(self, messages: list[dict], use_tools: bool) -> tuple[str, list[ToolCall]]:
        # Anthropic separates system messages from the conversation
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        conv         = [m for m in messages if m["role"] != "system"]

        payload: dict[str, Any] = {
            "model":      self._model,
            "max_tokens": 1024,
            "messages":   conv,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if use_tools:
            payload["tools"] = _ANTHROPIC_TOOLS

        headers = {
            "x-api-key":         self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }

        t0 = time.monotonic()
        resp = await _http_client().post(
            "https://api.anthropic.com/v1/messages",
            json=payload, headers=headers,
            timeout=httpx.Timeout(60.0),
        )
        resp.raise_for_status()

        data    = resp.json()
        content = data.get("content", [])
        text    = " ".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        ).strip()
        tools   = _parse_tool_calls_anthropic(content)
        _au = data.get("usage", {})
        _log("anthropic", self._model, t0, text, tools,
             input_tokens=_au.get("input_tokens", 0),
             output_tokens=_au.get("output_tokens", 0))
        return text, tools

    async def generate_text(self, prompt: str, timeout_s: float = 180.0) -> str:
        payload: dict[str, Any] = {
            "model":      self._model,
            "max_tokens": 8192,
            "messages":   [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key":         self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        t0 = time.monotonic()
        resp = await _http_client().post("https://api.anthropic.com/v1/messages", json=payload, headers=headers, timeout=httpx.Timeout(timeout_s))
        resp.raise_for_status()
        _d = resp.json()
        content = _d.get("content", [])
        text = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        _au = _d.get("usage", {})
        _log("anthropic", self._model, t0, text, [],
             input_tokens=_au.get("input_tokens", 0),
             output_tokens=_au.get("output_tokens", 0),
             purpose="proactive")
        return text

    @property
    def model_name(self) -> str:
        return self._model




class _OllamaFallbackBackend:
    """Lightweight Ollama backend used as failover for cloud providers."""

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model    = model

    async def chat(self, messages: list[dict], use_tools: bool) -> tuple[str, list[ToolCall]]:
        # gemma2 does not support tool_calls natively - strip tools, rely on text.
        # num_ctx must cover the full system prompt (~16k tokens for Nova's 63KB prompt)
        # plus multi-round tool conversation. mistral-nemo:12b supports 128k natively.
        payload: dict[str, Any] = {
            "model":    self._model,
            "messages": _to_ollama_messages(messages),
            "stream":   False,
            "options":  {"temperature": 0.7, "num_ctx": 16384, "num_predict": 400},
        }
        if use_tools:
            payload["tools"] = HA_TOOLS

        t0 = time.monotonic()
        _GPU_GATE.chat_started()
        try:
            resp = await _http_client().post(f"{self._base_url}/api/chat", json=payload, timeout=httpx.Timeout(180.0))
            resp.raise_for_status()
        finally:
            _GPU_GATE.chat_finished()

        data    = resp.json()
        message = data.get("message", {})
        text    = (message.get("content") or "").strip()
        tools   = _parse_tool_calls_openai(message.get("tool_calls") or [])
        _log("ollama_fallback", self._model, t0, text, tools,
             input_tokens=data.get("prompt_eval_count", 0),
             output_tokens=data.get("eval_count", 0))
        return text, tools

    async def generate_text(self, prompt: str, timeout_s: float = 120.0) -> str:
        payload: dict[str, Any] = {
            "model":    self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream":   False,
            "options":  {"temperature": 0.2, "num_ctx": 8192},
        }
        t0 = time.monotonic()
        resp = await _http_client().post(f"{self._base_url}/api/chat", json=payload, timeout=httpx.Timeout(timeout_s))
        resp.raise_for_status()
        _d = resp.json()
        text = (_d.get("message", {}).get("content") or "").strip()
        _log("ollama_fallback", self._model, t0, text, [],
             input_tokens=_d.get("prompt_eval_count", 0),
             output_tokens=_d.get("eval_count", 0),
             purpose="proactive")
        return text

    @property
    def model_name(self) -> str:
        return self._model


_ollama_tags_cache: set[str] | None = None


_LOCAL_TEXT_MODEL_PREFERENCES = (
    "mistral-nemo:12b",
    "llama3.1:8b-instruct-q4_K_M",
    "gemma2:9b",
    "qwen2.5:7b",
    "llama3.1:8b",
)
_FAST_LOCAL_TEXT_MODEL_PREFERENCES: tuple[str, ...] = (
    "qwen2.5:7b",
    "llama3.1:8b-instruct-q4_K_M",
    "llama3.1:8b",
    "mistral-nemo:12b",
    "gemma2:9b",
)


def _get_ollama_installed_models(ollama_url: str) -> set[str]:
    """Return installed Ollama model names. Cached after first call."""
    global _ollama_tags_cache
    if _ollama_tags_cache is not None:
        return _ollama_tags_cache
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{ollama_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
        _ollama_tags_cache = {str(m.get("name") or "").strip() for m in resp.json().get("models", [])}
    except Exception:
        _ollama_tags_cache = set()
    return _ollama_tags_cache


def _select_local_text_model(settings) -> str:
    configured = (getattr(settings, "ollama_local_text_model", "") or "").strip()
    if configured:
        return configured
    installed = _get_ollama_installed_models(settings.ollama_url)
    for candidate in _LOCAL_TEXT_MODEL_PREFERENCES:
        if candidate in installed:
            return candidate
    return settings.ollama_model


def _select_fast_local_text_model(settings) -> str:
    configured = (getattr(settings, "proactive_ollama_model", "") or "").strip()
    if configured:
        return configured
    installed = _get_ollama_installed_models(settings.ollama_url)
    for candidate in _FAST_LOCAL_TEXT_MODEL_PREFERENCES:
        if candidate in installed:
            return candidate
    return _select_local_text_model(settings)
