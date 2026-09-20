"""Chunking: pack sentences into chunks of at most `max_chars`, keeping the markdown heading path
as a prefix so a chunk stays interpretable on its own."""
from __future__ import annotations

import re
from typing import List

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_SENT = re.compile(r"(?<=[.!?])\s+")


def _blocks(text: str):
    """Yield (heading_path, paragraph) pairs."""
    path: List[str] = []
    para: List[str] = []

    def flush():
        if para:
            yield_val = (" > ".join(path), " ".join(para).strip())
            para.clear()
            return yield_val
        return None

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            out = flush()
            if out:
                yield out
            level = len(m.group(1))
            del path[level - 1:]
            path.append(m.group(2))
        elif not line.strip():
            out = flush()
            if out:
                yield out
        else:
            para.append(line.strip())
    out = flush()
    if out:
        yield out


def chunk_text(text: str, max_chars: int = 600, overlap_sentences: int = 1) -> List[str]:
    chunks: List[str] = []
    cur_path = ""
    cur: List[str] = []

    def emit():
        if cur:
            body = " ".join(cur)
            chunks.append((f"[{cur_path}] " if cur_path else "") + body)

    for path, para in _blocks(text):
        sentences = [s for s in _SENT.split(para) if s]
        for s in sentences:
            while len(s) > max_chars:                       # pathological long sentence
                piece, s = s[:max_chars], s[max_chars:]
                if cur:
                    emit(); cur = []
                cur_path = path
                cur = [piece]; emit(); cur = []
            if path != cur_path or (cur and len(" ".join(cur)) + 1 + len(s) > max_chars):
                carry = cur[-overlap_sentences:] if (path == cur_path and overlap_sentences) else []
                emit()
                cur_path, cur = path, list(carry)
            cur.append(s)
    emit()
    return chunks
