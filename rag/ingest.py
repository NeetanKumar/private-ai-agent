"""Ingest md / txt / pdf files for a user.

  python -m rag.ingest --user owner --source my_notes file1.md file2.pdf
  python -m rag.ingest --user owner --list
  python -m rag.ingest --user owner --delete <doc_id>

The document's taint comes from the source registry in infra/config.yaml. A source that is not
registered is PRIVATE, so ingesting without a registry entry can never produce CLEAN chunks.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from pathlib import Path
from typing import List, Optional

from gateway.taint import SourceRegistry

from .chunk import chunk_text
from .loaders import load_text
from .types import DocInfo, Embedder, RagError, VectorStore


async def ingest_file(store: VectorStore, embedder: Embedder, registry: SourceRegistry, user_id: str,
                      source: str, path: Path, max_chars: int = 600) -> DocInfo:
    text = load_text(path)
    chunks = chunk_text(text, max_chars=max_chars)
    if not chunks:
        raise RagError(f"{Path(path).name}: no text found")
    taint = registry.taint_of(source)
    # The source is part of the identity: the same file under another label is a different document,
    # so re-ingesting can never silently relabel (and re-taint) an existing one.
    doc_id = hashlib.sha256((source + "\0" + Path(path).name + "\0" + text).encode()).hexdigest()[:16]
    embeddings = await embedder.embed(chunks)
    store.add_document(user_id, doc_id, Path(path).name, source, taint, chunks, embeddings, embedder.id)
    return DocInfo(doc_id, Path(path).name, source, taint, len(chunks))


def main(argv: Optional[List[str]] = None) -> int:
    from .factory import build_components
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--source", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--delete")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args(argv)
    cfg, registry, store, embedder = build_components()
    if a.list:
        for d in store.list_documents(a.user):
            print(f"{d.doc_id}  {d.taint.name:<7} {d.source:<16} {d.chunks:>3} chunks  {d.doc_name}")
        return 0
    if a.delete:
        print(f"deleted {store.delete_document(a.user, a.delete)} chunks")
        return 0
    if not a.source or not a.files:
        ap.error("--source and at least one file are required")
    rc = 0
    for f in a.files:
        try:
            d = asyncio.run(ingest_file(store, embedder, registry, a.user, a.source, Path(f), cfg.rag.chunk_chars))
            print(f"ingested {d.doc_name}: {d.chunks} chunks, taint={d.taint.name}, doc_id={d.doc_id}")
        except RagError as e:
            print(f"skipped {f}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
