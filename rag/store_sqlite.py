"""SQLite + numpy VectorStore. Brute-force cosine search: fine at template scale, and it keeps
the data in one local file with no server and no telemetry. Swap it by implementing
rag.types.VectorStore.

Isolation: every statement is filtered by user_id, and every returned row is re-checked against
the requested user before it leaves this module.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from gateway.fragments import Taint

from .types import DocInfo, Hit, RagError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL, doc_id TEXT NOT NULL, doc_name TEXT NOT NULL,
  source TEXT NOT NULL, taint INTEGER NOT NULL, chunk_idx INTEGER NOT NULL,
  text TEXT NOT NULL, embedding BLOB NOT NULL, dim INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_user ON chunks(user_id);
CREATE INDEX IF NOT EXISTS chunks_user_doc ON chunks(user_id, doc_id);
"""


class SqliteStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _check_embedder(self, embedder_id: str) -> None:
        row = self._db.execute("SELECT value FROM meta WHERE key='embedder_id'").fetchone()
        if row is None:
            self._db.execute("INSERT INTO meta VALUES('embedder_id', ?)", (embedder_id,))
        elif row[0] != embedder_id:
            raise RagError(f"store was built with embedder {row[0]!r}; re-ingest to use {embedder_id!r}")

    def add_document(self, user_id: str, doc_id: str, doc_name: str, source: str, taint: Taint,
                     chunks: Sequence[str], embeddings: Sequence[Sequence[float]], embedder_id: str) -> None:
        if not user_id or len(chunks) != len(embeddings):
            raise RagError("bad_ingest")
        with self._lock, self._db:
            self._check_embedder(embedder_id)
            self._db.execute("DELETE FROM chunks WHERE user_id=? AND doc_id=?", (user_id, doc_id))
            self._db.executemany(
                "INSERT INTO chunks(user_id,doc_id,doc_name,source,taint,chunk_idx,text,embedding,dim)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [(user_id, doc_id, doc_name, source, int(taint), i, t,
                  np.asarray(e, dtype=np.float32).tobytes(), len(e))
                 for i, (t, e) in enumerate(zip(chunks, embeddings))])

    def query(self, user_id: str, vector: Sequence[float], k: int,
              filter: Optional[Dict[str, str]] = None) -> List[Hit]:
        if not user_id:
            raise RagError("user_required")
        sql = ("SELECT id,user_id,doc_id,doc_name,source,taint,chunk_idx,text,embedding,dim "
               "FROM chunks WHERE user_id=?")
        args: list = [user_id]
        for col in ("source", "doc_id"):                      # metadata filter, allow-listed columns
            if filter and col in filter:
                sql += f" AND {col}=?"
                args.append(filter[col])
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        rows = [r for r in rows if r[1] == user_id]           # defence in depth
        q = np.asarray(vector, dtype=np.float32)
        rows = [r for r in rows if r[9] == q.shape[0]]
        if not rows:
            return []
        mat = np.vstack([np.frombuffer(r[8], dtype=np.float32) for r in rows])
        qn = np.linalg.norm(q) or 1.0
        scores = (mat @ q) / (np.linalg.norm(mat, axis=1).clip(min=1e-9) * qn)
        top = np.argsort(-scores)[:k]
        return [Hit(doc_id=rows[i][2], doc_name=rows[i][3], source=rows[i][4], taint=Taint(rows[i][5]),
                    chunk_idx=rows[i][6], text=rows[i][7], score=float(scores[i])) for i in top]

    def delete_document(self, user_id: str, doc_id: str) -> int:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM chunks WHERE user_id=? AND doc_id=?",
                                    (user_id, doc_id)).rowcount

    def list_documents(self, user_id: str) -> List[DocInfo]:
        with self._lock:
            rows = self._db.execute(
                "SELECT doc_id,doc_name,source,MAX(taint),COUNT(*) FROM chunks WHERE user_id=? "
                "GROUP BY doc_id,doc_name,source ORDER BY doc_name", (user_id,)).fetchall()
        return [DocInfo(r[0], r[1], r[2], Taint(r[3]), r[4]) for r in rows]
