"""Per-person semantic memory using ChromaDB.

Each family member gets their own collection. A shared 'household' collection
stores common knowledge (house rules, preferences that apply to everyone).
"""
from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

import structlog

_LOGGER = structlog.get_logger(__name__)

_HOUSEHOLD_COLLECTION = "household"


class PersonMemoryService:
    """ChromaDB-backed memory with per-person isolation."""

    def __init__(self, data_dir: str = "/opt/avatar-server/data/chroma"):
        import chromadb
        self._client = chromadb.PersistentClient(path=data_dir)
        _LOGGER.info("person_memory.initialized", path=data_dir)

    def _collection(self, person_id: str):
        """Get or create a collection for a person."""
        name = f"person_{person_id}" if person_id != _HOUSEHOLD_COLLECTION else _HOUSEHOLD_COLLECTION
        return self._client.get_or_create_collection(name=name)

    def store(self, person_id: str, text: str, metadata: dict | None = None) -> str:
        """Store a memory for a specific person."""
        col = self._collection(person_id)
        doc_id = uuid4().hex
        meta = {
            "person_id": person_id,
            "timestamp": time.time(),
            **(metadata or {}),
        }
        col.add(documents=[text], metadatas=[meta], ids=[doc_id])
        _LOGGER.info("person_memory.stored", person=person_id, chars=len(text))
        return doc_id

    def store_household(self, text: str, metadata: dict | None = None) -> str:
        """Store a shared household memory."""
        return self.store(_HOUSEHOLD_COLLECTION, text, metadata)

    def recall(self, person_id: str, query: str, n: int = 5, include_household: bool = True) -> list[dict]:
        """Recall memories relevant to a query, scoped to person + household."""
        results = []

        # Person's own memories
        try:
            col = self._collection(person_id)
            if col.count() > 0:
                r = col.query(query_texts=[query], n_results=min(n, col.count()))
                for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
                    results.append({"text": doc, "metadata": meta, "distance": dist, "source": person_id})
        except Exception as e:
            _LOGGER.warning("person_memory.recall_error", person=person_id, exc=str(e)[:80])

        # Household shared memories
        if include_household:
            try:
                hcol = self._collection(_HOUSEHOLD_COLLECTION)
                if hcol.count() > 0:
                    r = hcol.query(query_texts=[query], n_results=min(3, hcol.count()))
                    for doc, meta, dist in zip(r["documents"][0], r["metadatas"][0], r["distances"][0]):
                        results.append({"text": doc, "metadata": meta, "distance": dist, "source": "household"})
            except Exception:
                pass

        # Sort by relevance (lower distance = more relevant)
        results.sort(key=lambda x: x["distance"])
        return results[:n]

    def build_context(self, person_id: str, query: str, n: int = 5) -> str:
        """Build a context string from recalled memories for injection into the prompt."""
        memories = self.recall(person_id, query, n=n)
        if not memories:
            return ""
        lines = []
        for m in memories:
            source = f"[{m['source']}]" if m["source"] != person_id else ""
            lines.append(f"- {m['text']} {source}")
        return "Relevant memories:\n" + "\n".join(lines)

    def list_memories(self, person_id: str, limit: int = 50) -> list[dict]:
        """List all memories for a person."""
        col = self._collection(person_id)
        if col.count() == 0:
            return []
        r = col.get(limit=limit, include=["documents", "metadatas"])
        return [{"id": id, "text": doc, "metadata": meta}
                for id, doc, meta in zip(r["ids"], r["documents"], r["metadatas"])]

    def delete_memory(self, person_id: str, memory_id: str) -> bool:
        """Delete a specific memory."""
        try:
            col = self._collection(person_id)
            col.delete(ids=[memory_id])
            return True
        except Exception:
            return False

    def get_stats(self) -> dict:
        """Return memory counts per person."""
        collections = self._client.list_collections()
        stats = {}
        for col in collections:
            name = col.name.replace("person_", "")
            stats[name] = col.count()
        return stats
