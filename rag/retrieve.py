"""Retrieval: embed the query, fetch top-k for this user, drop anything below the score floor.

Retrieved chunks carry the taint of their source document. The floor is a deterministic gate:
if nothing clears it the answer is "not in documents" and no model is called.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .types import Embedder, Hit, VectorStore

NOT_IN_DOCUMENTS = "not in documents"


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder, top_k: int = 5, min_score: float = 0.2):
        self.store, self.embedder, self.top_k, self.min_score = store, embedder, top_k, min_score

    async def search(self, user_id: str, query: str, k: Optional[int] = None,
                     filter: Optional[Dict[str, str]] = None) -> List[Hit]:
        """Top-k for this user, unfiltered by score (used by the evaluation harness)."""
        vec = (await self.embedder.embed([query]))[0]
        return self.store.query(user_id, vec, k or self.top_k, filter)

    async def retrieve(self, user_id: str, query: str, k: Optional[int] = None,
                       filter: Optional[Dict[str, str]] = None) -> List[Hit]:
        return [h for h in await self.search(user_id, query, k, filter) if h.score >= self.min_score]
