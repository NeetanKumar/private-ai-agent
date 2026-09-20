"""Build the retrieval components from infra/config.yaml. Swapping a backend is a config change."""
from __future__ import annotations

from gateway.config import Settings, get_settings
from gateway.taint import SourceRegistry

from .embed import HashingEmbedder, OllamaEmbedder
from .retrieve import Retriever
from .store_sqlite import SqliteStore
from .types import Embedder, RagError, VectorStore


def build_embedder(cfg: Settings) -> Embedder:
    e = cfg.rag.embedder
    if e.kind == "hashing":
        return HashingEmbedder(e.dim)
    if e.kind == "ollama":
        base = e.base_url or cfg.model_server.base_url.rsplit("/v1", 1)[0]
        return OllamaEmbedder(base, e.model, timeout=cfg.model_server.timeout_seconds)
    raise RagError(f"unknown embedder kind {e.kind!r}")


def build_store(cfg: Settings) -> VectorStore:
    if cfg.rag.store == "sqlite":
        return SqliteStore(cfg.rag.db_path)
    raise RagError(f"unknown store {cfg.rag.store!r}")


def build_retriever(cfg: Settings) -> Retriever:
    return Retriever(build_store(cfg), build_embedder(cfg), cfg.rag.top_k, cfg.rag.min_score)


def build_components():
    cfg = get_settings()
    return cfg, SourceRegistry(cfg.sources), build_store(cfg), build_embedder(cfg)
