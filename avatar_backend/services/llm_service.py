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

from avatar_backend.config import get_settings
from avatar_backend.models.messages import ToolCall
from avatar_backend.services.cost_log import CostLog as _CostLog

logger = structlog.get_logger()
_cost_log: _CostLog | None = None

_FAST_LOCAL_TEXT_MODEL_PREFERENCES: tuple[str, ...] = (
    "qwen2.5:7b",
    "llama3.1:8b-instruct-q4_K_M",
    "llama3.1:8b",
    "mistral-nemo:12b",
    "gemma2:9b",
)


def _build_operational_backend(settings) -> tuple[Any | None, str | None]:
    if settings.google_api_key:
        return _GeminiBackend(settings), "google"
    if settings.openai_api_key:
        return _OpenAICompatBackend(settings), "openai"
    if settings.anthropic_api_key:
        return _AnthropicBackend(settings), "anthropic"
    return None, None


def _format_exc_reason(exc: Exception | None) -> str:
    if exc is None:
        return "unknown"
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"[:120]
    return type(exc).__name__[:120]

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
                "Control a Home Assistant device by calling a service (turn on/off, lock, unlock, etc). "
                "Use get_entities first if you are unsure of the entity_id. "
                "NEVER use this to read sensor values - use get_entity_state instead. "
                "NEVER call tts or media_player speak services - your text responses are automatically spoken."
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
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": (
                "Search for and play music on a speaker. Searches Music Assistant "
                "for the artist/song/album, then plays the first result on the specified speaker. "
                "Use this instead of call_ha_service for music playback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for - artist name, song title, or album.",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "The media_player entity ID to play on, e.g. media_player.living_room_sonos.",
                    },
                },
                "required": ["query", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_chore",
            "description": (
                "Record that a person completed a chore and award them points on the family scoreboard. "
                "Use when someone says they did a task like emptied the bin, made their bed, tidied their room, "
                "said prayers, etc. Valid task_ids: morning_prayer, make_bed, meal_prayer, empty_toilet_bin, "
                "tidy_bedroom, empty_kitchen_bin, tidy_living_room, wipe_kitchen, "
                "clear_table, hoover_living_room, take_recycling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task identifier, e.g. 'empty_kitchen_bin'.",
                    },
                    "person": {
                        "type": "string",
                        "description": "The person's name, e.g. 'penn' or 'tangu'.",
                    },
                },
                "required": ["task_id", "person"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scoreboard",
            "description": (
                "Look up the family chore scoreboard. Use whenever someone asks about chores: "
                "scores, rankings, who is winning, how many chores done today or this week, "
                "recent activity, or any scoreboard question. "
                "Pass a period of today, week, or recent depending on what they asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["today", "week", "recent"],
                        "description": "today=chores today, week=weekly scores, recent=last 10 logs",
                    },
                },
                "required": ["period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_enrolled_devices",
            "description": (
                "List all enrolled children's Android devices managed by Headwind MDM. "
                "Returns device number, name, online status, and last seen time. "
                "Call this first to find the device_number before blocking apps or sending messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_parental_status",
            "description": (
                "Check whether the Headwind parental management backend is reachable. "
                "Use this before deeper device-management actions if parental controls seem unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_household_forecast",
            "description": (
                "Show what is coming up for the household in the next few hours: "
                "upcoming bedtimes, homework gate windows, chore check-ins, and current device states. "
                "Use when asked what will happen next, what is scheduled, or for a household overview."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bedtime_status",
            "description": (
                "Get tonight's bedtime for a household member. "
                "Returns bedtime time, whether it is a school night, and current device state. "
                "Use when asked about bedtimes, screen time, or device curfews."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {
                        "type": "string",
                        "description": "Household member ID (e.g. jason, joel, miya).",
                    },
                },
                "required": ["person_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_location",
            "description": (
                "Get the latest known location for a household member or managed device. "
                "Provide person_id (e.g. jason, joel, miya) to look up by name — no need to "
                "call get_enrolled_devices first. Returns a human-readable address when available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {
                        "type": "string",
                        "description": "The person's ID from the household roster (e.g. jason, joel, miya).",
                    },
                    "device_number": {
                        "type": "string",
                        "description": "Direct MDM device number — only needed if person_id is unknown.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_apps",
            "description": (
                "Search Headwind's Android app catalog to find package names and whether an app is installable or allow-only. "
                "Use this before deploy_app if you are unsure of the exact package."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "App name or package fragment, e.g. 'YouTube', 'WhatsApp', or 'roblox'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "block_app",
            "description": (
                "Block (hide/disable) an Android app on a child's device. "
                "The app is removed from the home screen and cannot be opened. "
                "Use get_enrolled_devices first to find the device_number. "
                "Common packages: TikTok=com.zhiliaoapp.musically, Instagram=com.instagram.android, "
                "WhatsApp=com.whatsapp, Snapchat=com.snapchat.android, YouTube=com.google.android.youtube, "
                "Facebook=com.facebook.katana, X/Twitter=com.twitter.android, Roblox=com.roblox.client."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices, e.g. '0001'.",
                    },
                    "package": {
                        "type": "string",
                        "description": "Android package name, e.g. 'com.zhiliaoapp.musically'.",
                    },
                },
                "required": ["device_number", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unblock_app",
            "description": (
                "Unblock (re-enable) a previously blocked Android app on a child's device. "
                "Use get_enrolled_devices first to find the device_number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices.",
                    },
                    "package": {
                        "type": "string",
                        "description": "Android package name to unblock, e.g. 'com.google.android.youtube'.",
                    },
                },
                "required": ["device_number", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_app",
            "description": (
                "Deploy or allow an Android app on a child's device using Headwind's app catalog. "
                "For installable apps, Headwind marks them for install. "
                "For system or allow-only apps, Nova can only allow them, not silently install them. "
                "Use get_enrolled_devices and optionally search_apps first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices.",
                    },
                    "package": {
                        "type": "string",
                        "description": "Android package name, e.g. 'com.whatsapp'.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional display name for clearer confirmations, e.g. 'WhatsApp'.",
                    },
                },
                "required": ["device_number", "package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_homework_gate",
            "description": "Check whether a child has completed their required tasks and whether their device is currently locked or unlocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person_id": {"type": "string", "description": "The child's ID (e.g. joel, jason, miya)"},
                },
                "required": ["person_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "request_exception",
            "description": (
                "Submit a parental exception request — extra screen time, a bedtime extension, "
                "or temporary access to a blocked resource. The request is queued for a parent "
                "to approve or deny in the admin panel. Use when a child asks for something "
                "that needs parental approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Name of the person requesting (e.g. Joel)"},
                    "resource": {"type": "string", "description": "What they want (e.g. Xbox, iPad, YouTube)"},
                    "reason": {"type": "string", "description": "Their reason for the exception"},
                    "duration_minutes": {"type": "integer", "description": "How long they are asking for (minutes)", "default": 30}
                },
                "required": ["subject", "reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_device_message",
            "description": (
                "Send a full-screen push notification to a child's Android device. "
                "Good for: 'come for dinner', 'homework time', 'phone off now', 'bedtime'. "
                "Use get_enrolled_devices first to find the device_number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_number": {
                        "type": "string",
                        "description": "The device number from get_enrolled_devices.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to display on the device.",
                    },
                },
                "required": ["device_number", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_parental_configurations",
            "description": (
                "List Headwind parental configurations. Useful for enrollment and understanding which configuration a device belongs to."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_enrollment_link",
            "description": (
                "Get the enrollment URL for a Headwind configuration so a parent can enroll a device. "
                "This returns the enroll link and QR key as text, not the QR image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "config_id": {
                        "type": "integer",
                        "description": "Headwind configuration id, e.g. 2.",
                    },
                },
                "required": ["config_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deduct_points",
            "description": (
                "Deduct points from a family member as a penalty for bad behaviour. "
                "Use when a parent says someone was rude, lied, fought, used bad language, "
                "was disobedient, disrespectful, or explicitly asks to deduct/remove points. "
                "Use penalty_id from the configured list; for unlisted reasons use custom_reason + custom_points."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person": {
                        "type": "string",
                        "description": "First name of the person to penalise.",
                    },
                    "penalty_id": {
                        "type": "string",
                        "description": "Preset penalty reason ID, e.g. rude_behaviour, lying, fighting, disobedience, bad_language, disrespect, damaging_property.",
                    },
                    "custom_reason": {
                        "type": "string",
                        "description": "Free-text reason if no preset penalty_id matches.",
                    },
                    "custom_points": {
                        "type": "integer",
                        "description": "Points to deduct when using a custom reason (positive number).",
                    },
                },
                "required": ["person", "penalty_id"],
            },
        },
    },
]


# Drop scoreboard-related tools when feature is disabled
try:
    from avatar_backend.config import get_settings as _cfg_gs
    if not _cfg_gs().scoreboard_enabled:
        _sb_tools = {"log_chore", "get_scoreboard", "deduct_points"}
        HA_TOOLS = [t for t in HA_TOOLS if t["function"]["name"] not in _sb_tools]
except Exception:
    pass

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
_LOCAL_TEXT_MODEL_PREFERENCES = (
    "mistral-nemo:12b",
    "llama3.1:8b-instruct-q4_K_M",
    "gemma2:9b",
    "qwen2.5:7b",
    "llama3.1:8b",
)


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
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0)) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(f"{self._base_url}/api/chat", json=payload)
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(f"{self._base_url}/chat/completions", json=payload, headers=headers)
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
            api_key = _get_gemini_key() or self._api_key
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                    resp = await client.post(url, json=payload,
                                             headers={"Content-Type": "application/json",
                                                      "X-goog-api-key": api_key})
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
            api_key = _get_gemini_key() or self._api_key
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
                    resp = await client.post(url, json=payload,
                                             headers={"Content-Type": "application/json",
                                                      "X-goog-api-key": api_key})
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
            api_key = _get_gemini_key() or self._api_key
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
                    resp = await client.post(url, json=payload,
                                             headers={"Content-Type": "application/json",
                                                      "X-goog-api-key": api_key})
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
            resp = await client.post(f"{base_url}/api/chat", json=payload)
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
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
        resp = await client.post(url, json=payload,
                                 headers={"Content-Type": "application/json",
                                          "X-goog-api-key": api_key})
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
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
        resp = await client.post("https://api.openai.com/v1/chat/completions",
                                  json=payload,
                                  headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()



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
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                resp = await client.post(f"{self._base_url}/api/chat", json=payload)
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(f"{self._base_url}/api/chat", json=payload)
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
            return await self._backend.chat(messages, use_tools)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if self._fallback is None:
                _reason = str(exc)[:120]
                raise RuntimeError(f"LLM unavailable: {_reason}") from exc
            logger.warning("llm.primary_failed_using_fallback",
                           provider=self._provider,
                           fallback=self._FALLBACK_MODEL,
                           reason=str(exc)[:120])
            try:
                return await self._fallback.chat(messages, use_tools)
            except Exception as fb_exc:
                raise RuntimeError(f"LLM fallback also failed: {fb_exc}") from fb_exc

    async def chat_operational(
        self,
        messages: list[dict[str, Any]],
        use_tools: bool = True,
        purpose: str = "operational_chat",
    ) -> tuple[str, list[ToolCall]]:
        backend = self._operational_backend
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
        global _cost_log
        _cost_log = log

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
            for _attempt in range(_gemini_attempt_budget()):
                api_key = _get_gemini_key(camera_id)
                if not api_key:
                    break
                try:
                    return await _gemini_describe_image(image_bytes, api_key, model, _prompt, system_instruction)
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
