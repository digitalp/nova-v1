"""
CostLog — tracks LLM invocation costs in a ring buffer with SSE fan-out.

Pricing is per-million tokens (input, output). Updated April 2026.
Free / local providers (ollama) cost $0.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any

# ── Pricing table: model_name → (input $/M, output $/M) ──────────────────────
# Match against model name prefix (longest match wins).
_PRICING: list[tuple[str, float, float]] = [
    # Google Gemini
    ("gemini-2.5-flash",        0.15,   0.60),
    ("gemini-2.5-pro",          1.25,   10.00),
    ("gemini-2.0-flash",        0.10,   0.40),
    ("gemini-1.5-flash",        0.075,  0.30),
    ("gemini-1.5-pro",          1.25,   5.00),
    # OpenAI
    ("gpt-4o-mini",             0.15,   0.60),
    ("gpt-4o",                  2.50,   10.00),
    ("gpt-4-turbo",             10.00,  30.00),
    ("gpt-3.5-turbo",           0.50,   1.50),
    # Anthropic Claude
    ("claude-opus-4",           15.00,  75.00),
    ("claude-sonnet-4",         3.00,   15.00),
    ("claude-haiku-4",          0.80,   4.00),
    ("claude-opus-3-5",         15.00,  75.00),
    ("claude-sonnet-3-5",       3.00,   15.00),
    ("claude-haiku-3-5",        0.80,   4.00),
    # Fallback — ollama/unknown
    ("",                        0.00,   0.00),
]

_MAX_ENTRIES = 500


def _get_price(model: str) -> tuple[float, float]:
    model_lc = model.lower()
    for prefix, price_in, price_out in _PRICING:
        if model_lc.startswith(prefix):
            return price_in, price_out
    return 0.0, 0.0


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = _get_price(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


class CostLog:
    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._session_cost: float = 0.0
        self._db = None  # MetricsDB — set via set_db()

    def set_db(self, db) -> None:
        """Attach a MetricsDB instance for persistent storage."""
        self._db = db

    # ── Public API ────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        purpose: str = "chat",   # chat | triage | vision | proactive
        elapsed_ms: int = 0,
    ) -> dict:
        cost = _calc_cost(model, input_tokens, output_tokens)
        price_in, price_out = _get_price(model)
        entry: dict[str, Any] = {
            "ts":           datetime.now().strftime("%H:%M:%S"),
            "provider":     provider,
            "model":        model,
            "purpose":      purpose,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":     round(cost, 8),
            "elapsed_ms":   elapsed_ms,
            "price_in":     price_in,
            "price_out":    price_out,
        }
        self._entries.append(entry)
        self._session_cost += cost
        if self._db is not None:
            try:
                self._db.insert_invocation({
                    "provider":     provider,
                    "model":        model,
                    "purpose":      purpose,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd":     cost,
                    "elapsed_ms":   elapsed_ms,
                })
            except Exception as _exc:
                import logging as _logging
                _logging.getLogger(__name__).warning("cost_log.db_write_error: %s", _exc)
        if len(self._entries) > _MAX_ENTRIES:
            self._entries.pop(0)
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)
        return entry

    def recent(self, n: int = 200) -> list[dict]:
        return list(self._entries[-n:])

    def totals(self) -> dict:
        total_input  = sum(e["input_tokens"]  for e in self._entries)
        total_output = sum(e["output_tokens"] for e in self._entries)
        total_cost   = sum(e["cost_usd"]      for e in self._entries)
        by_model: dict[str, dict] = {}
        for e in self._entries:
            key = f"{e['provider']}/{e['model']}"
            if key not in by_model:
                by_model[key] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                                  "price_in": e["price_in"], "price_out": e["price_out"]}
            by_model[key]["calls"]         += 1
            by_model[key]["input_tokens"]  += e["input_tokens"]
            by_model[key]["output_tokens"] += e["output_tokens"]
            by_model[key]["cost_usd"]      += e["cost_usd"]
        # round
        for v in by_model.values():
            v["cost_usd"] = round(v["cost_usd"], 6)
        return {
            "session_calls":        len(self._entries),
            "session_input_tokens": total_input,
            "session_output_tokens": total_output,
            "session_cost_usd":     round(total_cost, 6),
            "by_model":             by_model,
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass
