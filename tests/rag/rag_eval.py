"""Evaluation harness for the retrieval pipeline: recall@5, MRR, no-answer gate, faithfulness."""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from rag.ingest import ingest_file
from rag.retrieve import NOT_IN_DOCUMENTS, Retriever

HERE = pathlib.Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
GOLDEN = HERE / "golden.jsonl"


def load_golden() -> List[dict]:
    return [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]


def ensure_pdf(dest_dir: pathlib.Path) -> pathlib.Path:
    from pdfgen import BOARD_SUMMARY, write_pdf
    p = dest_dir / "board_summary.pdf"
    write_pdf(p, BOARD_SUMMARY)
    return p


async def ingest_corpus(retriever: Retriever, registry, user_id: str, source: str,
                        tmp_dir: pathlib.Path) -> List:
    files = sorted(CORPUS.glob("*")) + [ensure_pdf(tmp_dir)]
    return [await ingest_file(retriever.store, retriever.embedder, registry, user_id, source, f) for f in files]


@dataclass
class Report:
    n: int = 0
    recall_at_5: float = 0.0
    mrr: float = 0.0
    misses: List[dict] = field(default_factory=list)
    no_answer_ok: int = 0
    no_answer_n: int = 0
    no_answer_detail: List[dict] = field(default_factory=list)
    answerable_top_scores: List[float] = field(default_factory=list)
    no_answer_top_scores: List[float] = field(default_factory=list)
    false_abstain: int = 0          # answerable questions the score floor would wrongly reject
    faithfulness: Optional[float] = None
    correctness: Optional[float] = None


async def evaluate_retrieval(retriever: Retriever, user_id: str, golden: List[dict], k: int = 5) -> Report:
    rep = Report()
    rr_sum, hit5 = 0.0, 0
    for g in golden:
        if g.get("no_answer"):
            continue
        hits = await retriever.search(user_id, g["question"], k=10)
        rank = next((i + 1 for i, h in enumerate(hits) if g["must_contain"].lower() in h.text.lower()), None)
        rep.n += 1
        rep.answerable_top_scores.append(hits[0].score if hits else 0.0)
        if not [h for h in hits if h.score >= retriever.min_score]:
            rep.false_abstain += 1
        if rank:
            rr_sum += 1.0 / rank
            hit5 += rank <= k
        if not rank or rank > k:
            rep.misses.append({"id": g["id"], "question": g["question"], "rank": rank,
                               "top_doc": hits[0].doc_name if hits else None, "want": g["doc"]})
    rep.recall_at_5 = hit5 / rep.n
    rep.mrr = rr_sum / rep.n
    for g in golden:
        if not g.get("no_answer"):
            continue
        top = (await retriever.search(user_id, g["question"], k=1))
        kept = await retriever.retrieve(user_id, g["question"])
        ok = not kept                                  # gate closes -> "not in documents"
        rep.no_answer_n += 1
        rep.no_answer_top_scores.append(top[0].score if top else 0.0)
        rep.no_answer_ok += ok
        rep.no_answer_detail.append({"id": g["id"], "kind": g["kind"], "question": g["question"],
                                     "top_score": round(top[0].score, 3) if top else 0.0, "gated": ok})
    return rep


Answerer = Callable[[str, List[str]], Awaitable[str]]
Judge = Callable[[str, List[str]], Awaitable[bool]]


async def evaluate_generation(retriever: Retriever, user_id: str, golden: List[dict],
                              answerer: Answerer, judge: Judge, rep: Report) -> Report:
    """Faithfulness = share of answers the judge finds fully supported by the retrieved passages.
    Correctness = share of answerable questions whose answer contains the expected string.
    No-answer questions must produce exactly NOT_IN_DOCUMENTS (the gate, else the model)."""
    faithful = correct = total = 0
    na_ok = 0
    for g in golden:
        hits = await retriever.retrieve(user_id, g["question"])
        passages = [h.text for h in hits]
        if g.get("no_answer"):
            ans = NOT_IN_DOCUMENTS if not hits else await answerer(g["question"], passages)
            na_ok += ans.strip().lower().rstrip(".") == NOT_IN_DOCUMENTS
            continue
        ans = NOT_IN_DOCUMENTS if not hits else await answerer(g["question"], passages)
        total += 1
        correct += g["must_contain"].lower() in ans.lower() or _norm(g["must_contain"]) in _norm(ans)
        faithful += await judge(ans, passages) if hits else 1     # abstaining asserts nothing
    rep.faithfulness = faithful / total
    rep.correctness = correct / total
    rep.no_answer_ok = na_ok
    return rep


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())
