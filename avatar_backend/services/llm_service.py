"""
LLM service — supports Ollama (local), OpenAI, Google Gemini, and Anthropic.

Set LLM_PROVIDER in .env to switch providers:
  LLM_PROVIDER=ollama      (default)
  LLM_PROVIDER=openai      + OPENAI_API_KEY + CLOUD_MODEL (e.g. gpt-4o-mini)
  LLM_PROVIDER=google      + GOOGLE_API_KEY + CLOUD_MODEL (e.g. gemini-2.0-flash)
  LLM_PROVIDER=anthropic   + ANTHROPIC_API_KEY + CLOUD_MODEL (e.g. claude-haiku-4-5-20251001)
"""
from __future__ import annotations
import json
import time
from typing import Any

import httpx
import structlog

from avatar_backend.config import get_settings
from avatar_backend.models.messages import ToolCall

logger = structlog.get_logger()

# ── Tool schemas (OpenAI/Ollama format) ───────────────────────────────────────

HA_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_entities",
            "description": (
                "List available Home Assistant entities for a domain with their "
                "current state. Always call this FIRST if you are unsure of the "
                "exact entity_id before calling call_ha_service."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Entity domain to list. Examples: light, switch, "
                            "media_player, climate, cover, fan, sensor, "
                            "binary_sensor, lock, automation, input_boolean."
                        ),
                    },
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_state",
            "description": (
                "Get the current state and value of a specific Home Assistant entity. "
                "Use this to answer questions like 'what is the power consumption', "
                "'is the light on', 'what is the temperature', etc. "
                "Use get_entities first if you don't know the exact entity_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The full entity ID, e.g. sensor.total_power, light.kitchen.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_ha_service",
            "description": (
                "Control a Home Assistant device by calling a service. "
                "Use get_entities first if you are unsure of the entity_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain":       {"type": "string"},
                    "service":      {"type": "string"},
                    "entity_id":    {"type": "string"},
                    "service_data": {"type": "object"},
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_camera",
            "description": (
                "Capture a snapshot from a Home Assistant camera and describe what it sees. "
                "Use get_entities('camera') first if you don't know the entity_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The camera entity ID, e.g. camera.front_door.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
]

# Anthropic uses a slightly different tool schema format
_ANTHROPIC_TOOLS: list[dict] = [
    {
        "name":         t["function"]["name"],
        "description":  t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in HA_TOOLS
]

_DEFAULT_MODELS = {
    "openai":    "gpt-4o-mini",
    "google":    "gemini-2.5-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}


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


def _log(provider, model, t0, text, tools):
    logger.info(
        "llm.response",
        provider=provider,
        model=model,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        has_tool_calls=bool(tools),
        text_chars=len(text),
    )


# ── Provider backends ─────────────────────────────────────────────────────────

class _OllamaBackend:
    def __init__(self, settings) -> None:
        self._base_url = settings.ollama_url.rstrip("/")
        self._model    = settings.ollama_model

    async def chat(self, messages: list[dict], use_tools: bool) -> tuple[str, list[ToolCall]]:
        payload: dict[str, Any] = {
            "model":    self._model,
            "messages": messages,
            "stream":   False,
            "options":  {"temperature": 0.7, "num_ctx": 4096},
        }
        if use_tools:
            payload["tools"] = HA_TOOLS

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
            resp = await client.post(f"{self._base_url}/api/chat", json=payload)
            resp.raise_for_status()

        data    = resp.json()
        message = data.get("message", {})
        text    = (message.get("content") or "").strip()
        tools   = _parse_tool_calls_openai(message.get("tool_calls") or [])
        _log("ollama", self._model, t0, text, tools)
        return text, tools

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
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload, headers=headers,
            )
            resp.raise_for_status()

        data    = resp.json()
        choice  = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        text    = (message.get("content") or "").strip()
        tools   = _parse_tool_calls_openai(message.get("tool_calls") or [])
        _log("openai", self._model, t0, text, tools)
        return text, tools

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
            f"/{self._model}:generateContent?key={self._api_key}"
        )

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(url, json=payload,
                                     headers={"Content-Type": "application/json"})
            resp.raise_for_status()

        data  = resp.json()
        parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])

        text  = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
        tools = [
            ToolCall(
                function_name=p["functionCall"]["name"],
                arguments=p["functionCall"].get("args", {}),
            )
            for p in parts if "functionCall" in p
        ]
        _log("google", self._model, t0, text, tools)
        return text, tools

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
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload, headers=headers,
            )
            resp.raise_for_status()

        data    = resp.json()
        content = data.get("content", [])
        text    = " ".join(
            b.get("text", "") for b in content if b.get("type") == "text"
        ).strip()
        tools   = _parse_tool_calls_anthropic(content)
        _log("anthropic", self._model, t0, text, tools)
        return text, tools

    @property
    def model_name(self) -> str:
        return self._model


# ── Vision helpers ───────────────────────────────────────────────────────────

async def _gemini_describe_image(image_bytes: bytes, api_key: str, model: str) -> str:
    import base64 as _b64
    b64 = _b64.b64encode(image_bytes).decode()
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": "Describe what you see in this security camera image in 2-3 sentences. Focus on people, vehicles, objects, and any notable activity."},
            ],
        }],
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
    data = resp.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return " ".join(p.get("text", "") for p in parts if "text" in p).strip()


async def _openai_describe_image(image_bytes: bytes, api_key: str, model: str) -> str:
    import base64 as _b64
    b64 = _b64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": "Describe what you see in this security camera image in 2-3 sentences. Focus on people, vehicles, objects, and any notable activity."},
            ],
        }],
        "max_tokens": 300,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.post("https://api.openai.com/v1/chat/completions",
                                  json=payload,
                                  headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Public service ────────────────────────────────────────────────────────────

class LLMService:
    """
    Routes LLM requests to the configured provider.
    Switch providers via LLM_PROVIDER in .env — no code changes needed.
    """

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
        logger.info("llm.provider", provider=provider, model=self._backend.model_name)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
    ) -> tuple[str, list[ToolCall]]:
        try:
            return await self._backend.chat(messages, use_tools)
        except httpx.ConnectError as exc:
            raise RuntimeError(f"LLM unreachable: {exc}") from exc
        except httpx.TimeoutException:
            raise RuntimeError("LLM inference timed out") from None
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"LLM HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc

    async def is_ready(self) -> bool:
        if isinstance(self._backend, _OllamaBackend):
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{self._backend._base_url}/api/tags")
                    if resp.status_code != 200:
                        return False
                    models = [m["name"] for m in resp.json().get("models", [])]
                    family = self._backend._model.split(":")[0]
                    return any(family in m for m in models)
            except Exception:
                return False
        # Cloud providers — assume ready if API key is set
        settings = get_settings()
        provider = settings.llm_provider.lower()
        key_map  = {"openai": settings.openai_api_key,
                    "google": settings.google_api_key,
                    "anthropic": settings.anthropic_api_key}
        return bool(key_map.get(provider, ""))

    async def describe_image(self, image_bytes: bytes) -> str:
        """Describe a camera image using vision capability of the active LLM provider."""
        try:
            if self._provider == "google":
                return await _gemini_describe_image(image_bytes, self._backend._api_key, self._backend._model)
            if self._provider == "openai":
                return await _openai_describe_image(image_bytes, self._backend._api_key, self._backend._model)
            return "Camera vision is not supported with the current LLM provider. Switch to Google Gemini or OpenAI."
        except Exception as exc:
            _log_struct = structlog.get_logger()
            _log_struct.error("llm.describe_image_error", exc=str(exc))
            return "I couldn't analyze the camera image right now."

    @property
    def model_name(self) -> str:
        return self._backend.model_name
