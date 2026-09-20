"""Interfaces for the retrieval layer. Storage and embedding backends plug in behind these."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Sequence

from gateway.fragments import Taint


class RagError(Exception):
    """Retrieval failed. Callers treat this as fail-closed (no answer, no fallback)."""


@dataclass(frozen=True)
class Hit:
    doc_id: str
    doc_name: str
    source: str
    taint: Taint            # inherited from the source document
    chunk_idx: int
    text: str
    score: float


@dataclass(frozen=True)
class DocInfo:
    doc_id: str
    doc_name: str
    source: str
    taint: Taint
    chunks: int


class Embedder(Protocol):
    id: str                 # identifies model + dimension; stored so backends are never mixed

    async def embed(self, texts: Sequence[str]) -> List[List[float]]: ...


class VectorStore(Protocol):
    """Every operation is scoped by user_id. There is no call that spans users."""

    def add_document(self, user_id: str, doc_id: str, doc_name: str, source: str, taint: Taint,
                     chunks: Sequence[str], embeddings: Sequence[Sequence[float]],
                     embedder_id: str) -> None: ...

    def query(self, user_id: str, vector: Sequence[float], k: int,
              filter: Optional[Dict[str, str]] = None) -> List[Hit]: ...

    def delete_document(self, user_id: str, doc_id: str) -> int: ...

    def list_documents(self, user_id: str) -> List[DocInfo]: ...
