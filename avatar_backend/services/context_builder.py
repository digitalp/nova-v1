from __future__ import annotations

import re
from typing import Any


_CTX_KEY_RE = re.compile(r"^[a-zA-Z0-9_\-\.]{1,64}$")
_CTX_MAX_VAL = 256
_CTX_MAX_ITEMS = 32


class ContextBuilder:
    """Builds compatibility-first structured context blocks for conversation turns."""

    def sanitize_context(self, context: dict[str, Any] | None = None) -> dict[str, str]:
        return self._sanitize_context(context)

    def build_text_context(self, user_text: str, context: dict[str, Any] | None = None) -> str:
        sanitized = self._sanitize_context(context)
        if not sanitized:
            return user_text
        ctx_lines = "\n".join(f"  {key}: {value}" for key, value in sanitized.items())
        return f"{user_text}\n\n[Home context]\n{ctx_lines}"

    def build_event_followup_context(
        self,
        *,
        user_text: str,
        event_type: str,
        event_summary: str | None = None,
        event_context: dict[str, Any] | None = None,
        followup_prompt: str | None = None,
    ) -> str:
        parts = [user_text]
        lines = [f"  type: {event_type}"]
        if event_summary:
            lines.append(f"  summary: {self._sanitize_value(event_summary)}")
        if followup_prompt:
            lines.append(f"  followup_prompt: {self._sanitize_value(followup_prompt)}")
        for key, value in self._sanitize_context(event_context).items():
            lines.append(f"  {key}: {value}")
        parts.append("[Event context]\n" + "\n".join(lines))
        return "\n\n".join(parts)

    def _sanitize_context(self, context: dict[str, Any] | None) -> dict[str, str]:
        if not context:
            return {}
        sanitized: dict[str, str] = {}
        self._flatten_context(context, sanitized)
        return sanitized

    def _flatten_context(
        self,
        value: Any,
        sanitized: dict[str, str],
        *,
        path: tuple[str, ...] = (),
    ) -> None:
        if len(sanitized) >= _CTX_MAX_ITEMS:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or not _CTX_KEY_RE.match(key):
                    continue
                self._flatten_context(child, sanitized, path=(*path, key))
                if len(sanitized) >= _CTX_MAX_ITEMS:
                    return
            return
        if isinstance(value, (list, tuple)):
            if not value and path:
                sanitized[".".join(path)] = "[]"
                return
            for idx, child in enumerate(value):
                self._flatten_context(child, sanitized, path=(*path, str(idx)))
                if len(sanitized) >= _CTX_MAX_ITEMS:
                    return
            return
        if not path:
            return
        sanitized[".".join(path)] = self._sanitize_value(value)

    @staticmethod
    def _sanitize_value(value: Any) -> str:
        return str(value).replace("\n", " ").replace("\r", " ")[:_CTX_MAX_VAL]
