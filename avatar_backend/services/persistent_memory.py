from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog

from avatar_backend.services.llm_service import LLMService
from avatar_backend.services.metrics_db import MetricsDB

_LOGGER = structlog.get_logger()

_ALLOWED_CATEGORIES = {
    "preference",
    "policy",
    "routine",
    "household",
    "device",
    "people",
    "location",
    "security",
    "comfort",
    "media",
    "travel",
    "general",
}


class PersistentMemoryService:
    """Long-term household memory stored in SQLite and injected into chat context."""

    def __init__(self, db: MetricsDB) -> None:
        self._db = db
        _LOGGER.info("persistent_memory.initialized")

    def list_memories(self, limit: int = 200) -> list[dict]:
        return self._db.list_memories(limit=limit)

    def clear_memories(self) -> int:
        return self._db.clear_memories()

    def delete_memory(self, memory_id: int) -> bool:
        return self._db.delete_memory(memory_id)

    def add_memory(
        self,
        *,
        summary: str,
        category: str = "general",
        source: str = "manual",
        confidence: float = 0.9,
        pinned: bool = False,
    ) -> dict:
        return self._db.upsert_memory(
            summary=summary,
            category=category,
            source=source,
            confidence=confidence,
            pinned=pinned,
        )

    def build_context(self, query: str, limit: int = 5) -> tuple[str, list[int]]:
        memories = self._db.list_memories(limit=300)
        if not memories:
            return "", []

        q = self._normalize(query)
        q_tokens = self._tokens(q)
        broad_query = any(
            phrase in q
            for phrase in (
                "remember",
                "what do you know",
                "about my home",
                "about me",
                "preferences",
                "household",
            )
        )

        scored: list[tuple[float, dict]] = []
        for mem in memories:
            score = 0.0
            summary = self._normalize(mem.get("summary", ""))
            category = self._normalize(mem.get("category", "general"))
            tokens = self._tokens(summary) | self._tokens(category)

            overlap = len(q_tokens & tokens)
            if overlap:
                score += overlap * 2.0
            if summary and summary in q:
                score += 4.0
            if category and category in q:
                score += 1.5
            if broad_query:
                score += 0.5
            score += min(float(mem.get("confidence", 0.0)), 1.0)
            score += min(int(mem.get("times_seen", 1)), 5) * 0.15
            if mem.get("pinned"):
                score += 1.0
            if mem.get("last_referenced_ts"):
                score += 0.2
            if not q_tokens and not broad_query:
                score = 0.0
            scored.append((score, mem))

        chosen = [m for score, m in sorted(scored, key=lambda x: x[0], reverse=True) if score > 0][:limit]
        if not chosen and broad_query:
            chosen = memories[:limit]
        if not chosen:
            return "", []

        lines = []
        ids: list[int] = []
        for mem in chosen:
            ids.append(int(mem["id"]))
            category = mem.get("category", "general")
            lines.append(f"- [{category}] {mem.get('summary', '')}")
        context = (
            "Long-term household memory. Use only when relevant and do not treat it as newer than live Home Assistant state.\n"
            + "\n".join(lines)
        )
        return context, ids

    def mark_referenced(self, memory_ids: list[int]) -> None:
        self._db.mark_memories_referenced(memory_ids)

    def learn_from_exchange_async(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        llm: LLMService,
    ) -> None:
        if not self._should_attempt_learning(user_text, assistant_text):
            return
        asyncio.create_task(
            self._learn_from_exchange(
                session_id=session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                llm=llm,
            )
        )

    async def _learn_from_exchange(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        llm: LLMService,
    ) -> None:
        prompt = (
            "Extract durable household memories from this Nova conversation.\n\n"
            "Return ONLY valid JSON as an array.\n"
            "Each item must have: summary, category, confidence.\n"
            "Only include stable facts, preferences, routines, policies, names, or home rules "
            "that are likely still useful in future conversations.\n"
            "Do NOT include transient events, one-off requests, timestamps, or generic summaries.\n"
            "If there is nothing worth remembering, return [].\n"
            f"Allowed categories: {', '.join(sorted(_ALLOWED_CATEGORIES))}.\n\n"
            f"User:\n{user_text}\n\n"
            f"Assistant:\n{assistant_text}\n"
        )
        try:
            raw = await llm.generate_text_local_resilient(
                prompt,
                timeout_s=20.0,
                retry_delay_s=2.0,
                fallback_timeout_s=12.0,
                purpose="persistent_memory",
            )
            memories = self._parse_memories(raw)
        except Exception as exc:
            _LOGGER.warning("persistent_memory.learn_failed", session_id=session_id, exc=self._format_exc(exc))
            return

        inserted = 0
        for mem in memories[:3]:
            summary = " ".join(str(mem.get("summary", "")).split()).strip()
            if len(summary) < 12 or len(summary) > 220:
                continue
            category = str(mem.get("category", "general")).strip().lower() or "general"
            if category not in _ALLOWED_CATEGORIES:
                category = "general"
            confidence = float(mem.get("confidence", 0.6) or 0.6)
            if confidence < 0.55:
                continue
            self._db.upsert_memory(
                summary=summary,
                category=category,
                source="chat",
                confidence=confidence,
            )
            inserted += 1

        if inserted:
            _LOGGER.info("persistent_memory.learned", session_id=session_id, count=inserted)

    @staticmethod
    def _should_attempt_learning(user_text: str, assistant_text: str) -> bool:
        text = f"{user_text}\n{assistant_text}".lower()
        if len(user_text.strip()) < 12:
            return False
        triggers = (
            "remember",
            "prefer",
            "usually",
            "always",
            "never",
            "i like",
            "i don't like",
            "we like",
            "my ",
            "our ",
            "guest mode",
            "quiet hours",
            "do not disturb",
            "travel mode",
            "birthday",
            "call me",
            "at home",
        )
        return any(t in text for t in triggers)

    @staticmethod
    def _parse_memories(raw: str) -> list[dict[str, Any]]:
        text = raw.strip()
        if not text:
            return []
        candidates = [text]
        fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        candidates.extend(fenced)
        bracketed = re.findall(r"(\[\s*.*\s*\])", text, re.DOTALL)
        candidates.extend(bracketed)

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        return []

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9_]{3,}", text) if t not in {"the", "and", "with", "that", "have", "this"}}

    @staticmethod
    def _format_exc(exc: BaseException) -> str:
        message = str(exc).strip()
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
