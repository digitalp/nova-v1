from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any

import httpx
from avatar_backend.services._shared_http import _http_client
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


def _classify_query_intent(query: str) -> set:
    """Return set of mem_class values relevant for this query."""
    q = (query or "").lower()
    classes: set = {"policy"}  # always inject policy memories
    pref_words = {"prefer", "like", "dislike", "want", "enjoy", "hate",
                  "always", "never", "usually", "favourite", "favorite"}
    profile_words = {"who", "jason", "joel", "miya", "penn", "family",
                     "child", "person", "birthday", "age", "room"}
    episodic_words = {"remember", "earlier", "yesterday", "last time",
                      "before", "recent", "did", "happened", "was"}
    if any(w in q for w in pref_words):
        classes.add("preference")
    if any(w in q for w in profile_words):
        classes.add("profile")
    if any(w in q for w in episodic_words):
        classes.add("episodic")
    if len(classes) == 1:  # only policy matched — default to broad retrieval
        classes |= {"preference", "profile"}
    return classes


class PersistentMemoryService:
    """Long-term household memory stored in SQLite and injected into chat context."""

    def __init__(self, db: MetricsDB, ollama_url: str = "http://localhost:11434") -> None:
        self._db = db
        self._ollama_url = ollama_url.rstrip("/")
        self._embedding_cache: dict[tuple[int, str], list[float]] = {}
        self._db.ensure_memory_columns()
        self._db.expire_stale_memories()
        _LOGGER.info("persistent_memory.initialized")

    async def _get_embedding(self, text: str) -> list[float] | None:
        try:
            resp = await _http_client().post(
                    f"{self._ollama_url}/api/embed",
                    json={"model": "qwen2.5:7b", "input": text},
                    timeout=10.0,
                )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings")
            if embeddings and len(embeddings) > 0:
                return embeddings[0]
        except Exception as exc:
            _LOGGER.debug("persistent_memory.embedding_failed", exc=str(exc))
        return None

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def list_memories(self, limit: int = 200) -> list[dict]:
        return self._db.list_memories(limit=limit)

    def clear_memories(self) -> int:
        return self._db.clear_memories()

    def mark_stale(self, memory_id: int, superseded_by: int | None = None) -> bool:
        ok = self._db.mark_stale(memory_id, superseded_by)
        if ok:
            self.invalidate_embedding_cache(memory_id)
        return ok

    def restore_memory(self, memory_id: int) -> bool:
        return self._db.restore_memory(memory_id)

    def list_stale_memories(self, limit: int = 100) -> list[dict]:
        """Return stale memories for admin review."""
        mems = self._db.list_memories(limit=limit, include_stale=True)
        return [m for m in mems if m.get('stale')]

    def add_memory(
        self,
        *,
        summary: str,
        category: str = "general",
        source: str = "manual",
        confidence: float = 0.9,
        pinned: bool = False,
        expires_ts: str | None = None,
    ) -> dict:
        result = self._db.upsert_memory(
            summary=summary,
            category=category,
            source=source,
            confidence=confidence,
            pinned=pinned,
            expires_ts=expires_ts,
        )
        if result and result.get("id"):
            self.invalidate_embedding_cache(int(result["id"]))
        return result

    async def _check_contradictions(self, new_memory: dict) -> None:
        """After inserting a memory, flag older same-category memories that contradict it."""
        try:
            new_id = int(new_memory.get("id", 0))
            if not new_id:
                return
            new_summary = new_memory.get("summary", "")
            new_cat = new_memory.get("category", "general")
            new_emb = await self._get_embedding(new_summary)
            if not new_emb:
                return
            # Get all active (non-stale) memories in the same category
            all_mems = self._db.list_memories(limit=500)
            same_cat = [m for m in all_mems
                        if m.get("category") == new_cat and int(m.get("id", 0)) != new_id]
            for mem in same_cat:
                mem_id = int(mem.get("id", 0))
                mem_summary = mem.get("summary", "")
                cache_key = (mem_id, mem_summary)
                if cache_key in self._embedding_cache:
                    mem_emb = self._embedding_cache[cache_key]
                else:
                    mem_emb = await self._get_embedding(mem_summary)
                    if mem_emb:
                        self._embedding_cache[cache_key] = mem_emb
                if not mem_emb:
                    continue
                sim = self._cosine_similarity(new_emb, mem_emb)
                # High similarity (same topic) but different fingerprint = likely contradiction
                if sim >= 0.88 and mem.get("fingerprint") != new_memory.get("fingerprint"):
                    self._db.mark_stale(mem_id, superseded_by=new_id)
                    self.invalidate_embedding_cache(mem_id)
                    _LOGGER.info("memory.contradiction_detected",
                                 old_id=mem_id, new_id=new_id,
                                 similarity=round(sim, 3), category=new_cat)
        except Exception as exc:
            _LOGGER.debug("memory.contradiction_check_failed", exc=str(exc)[:80])


    async def add_memory_async(
        self,
        *,
        summary: str,
        category: str = "general",
        source: str = "manual",
        confidence: float = 0.9,
        pinned: bool = False,
        expires_ts: str | None = None,
    ) -> dict:
        """Async variant of add_memory — also runs contradiction detection."""
        result = self.add_memory(
            summary=summary, category=category, source=source,
            confidence=confidence, pinned=pinned, expires_ts=expires_ts,
        )
        if result and result.get("id"):
            await self._check_contradictions(result)
        return result

    def delete_memory(self, memory_id: int) -> bool:
        return self._db.delete_memory(memory_id)

    def update_memory(
        self,
        memory_id: int,
        *,
        summary: str,
        category: str = "general",
        confidence: float = 0.9,
        pinned: bool = False,
    ) -> dict | None:
        return self._db.update_memory(
            memory_id,
            summary=summary,
            category=category,
            confidence=confidence,
            pinned=pinned,
        )


    async def build_context_async(self, query: str, limit: int = 5,
                                  session_id: str = "") -> tuple[str, list[int]]:
        intent_classes = _classify_query_intent(query)
        all_memories = self._db.list_memories(limit=300)
        # Gate: prefer memories whose class matches intent; fall back to all if too few
        memories = [m for m in all_memories if m.get("mem_class", "episodic") in intent_classes]
        if len(memories) < 3:
            memories = all_memories
        if not memories:
            return "", []

        # Try embedding-based ranking
        query_emb = await self._get_embedding(query)
        if query_emb:
            scored: list[tuple[float, dict]] = []
            for mem in memories:
                summary = mem.get("summary", "")
                cache_key = (int(mem["id"]), summary)
                if cache_key in self._embedding_cache:
                    mem_emb = self._embedding_cache[cache_key]
                else:
                    mem_emb = await self._get_embedding(summary)
                    if mem_emb:
                        self._embedding_cache[cache_key] = mem_emb
                if mem_emb:
                    sim = self._cosine_similarity(query_emb, mem_emb)
                    # Boost pinned and high-confidence memories
                    if mem.get("pinned"):
                        sim += 0.1
                    sim += min(float(mem.get("confidence", 0.0)), 1.0) * 0.05
                    scored.append((sim, mem))
                else:
                    # Fallback: keyword score for this memory
                    kw_score = self._keyword_score(query, mem)
                    if kw_score > 0:
                        scored.append((kw_score * 0.3, mem))

            chosen = [m for score, m in sorted(scored, key=lambda x: x[0], reverse=True) if score > 0.3][:limit]
            if chosen:
                try:
                    self._db.log_memory_usage([m['id'] for m in chosen], query, session_id)
                except Exception:
                    pass
                return self._format_context(chosen)

        # Fallback to keyword scoring
        return self.build_context(query, limit)

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

    def build_enforced_preferences_context(self, limit: int = 8) -> tuple[str, list[int]]:
        memories = self._db.list_memories(limit=300)
        if not memories:
            return "", []

        chosen: list[dict] = []
        for mem in memories:
            category = str(mem.get("category", "general")).strip().lower()
            summary = " ".join(str(mem.get("summary", "")).split()).strip()
            if not summary:
                continue
            if mem.get("pinned") or category in {"preference", "policy"}:
                chosen.append(mem)

        if not chosen:
            return "", []

        chosen = chosen[:limit]
        ids = [int(mem["id"]) for mem in chosen if str(mem.get("id", "")).isdigit()]
        lines = [f"- [{mem.get('category', 'general')}] {mem.get('summary', '')}" for mem in chosen]
        context = (
            "Enforced household preferences and policies. These are binding unless the user explicitly overrides them in this turn.\n"
            + "\n".join(lines)
        )
        return context, ids

    def _keyword_score(self, query: str, mem: dict) -> float:
        q = self._normalize(query)
        q_tokens = self._tokens(q)
        summary = self._normalize(mem.get("summary", ""))
        tokens = self._tokens(summary) | self._tokens(self._normalize(mem.get("category", "")))
        overlap = len(q_tokens & tokens)
        score = overlap * 2.0
        if mem.get("pinned"):
            score += 1.0
        score += min(float(mem.get("confidence", 0.0)), 1.0)
        return score

    @staticmethod
    def _format_context(chosen: list[dict]) -> tuple[str, list[int]]:
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
    def invalidate_embedding_cache(self, memory_id: int) -> None:
        keys_to_remove = [k for k in self._embedding_cache if k[0] == memory_id]
        for k in keys_to_remove:
            del self._embedding_cache[k]

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
            try:
                raw = await llm.generate_text_local_fast_resilient(
                    prompt,
                    timeout_s=8.0,
                    retry_delay_s=2.0,
                    fallback_timeout_s=10.0,
                    purpose="persistent_memory",
                )
            except Exception:
                raw = await llm.generate_text_local_resilient(
                    prompt,
                    timeout_s=12.0,
                    retry_delay_s=2.0,
                    fallback_timeout_s=12.0,
                    purpose="persistent_memory_fallback",
                )
            memories = self._parse_memories(raw)
        except Exception as exc:
            _LOGGER.info("persistent_memory.learn_skipped", session_id=session_id, exc=self._format_exc(exc))
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
            result = self._db.upsert_memory(
                summary=summary,
                category=category,
                source="chat",
                confidence=confidence,
            )
            if result and result.get("id"):
                self.invalidate_embedding_cache(int(result["id"]))
                await self._check_contradictions(result)
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
