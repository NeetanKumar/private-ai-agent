"""Print the retrieval report: recall@5, MRR, no-answer gate, and (live mode) faithfulness.

  python tests/rag/report.py                     offline: hashing embedder, no model needed
  python tests/rag/report.py --live URL TOKEN    faithfulness via the gateway's private lane
                                                 (needs a running gateway with a local model and an
                                                 embedder configured; documents must be ingested first)
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rag.embed import HashingEmbedder                       # noqa: E402
from rag.retrieve import Retriever                          # noqa: E402
from rag.store_sqlite import SqliteStore                    # noqa: E402
from gateway.taint import SourceRegistry                    # noqa: E402
import rag_eval                                             # noqa: E402


def print_report(rep, title):
    print(f"\n== {title} ==")
    print(f"answerable questions : {rep.n}")
    print(f"recall@5             : {rep.recall_at_5:.3f}")
    print(f"MRR                  : {rep.mrr:.3f}")
    print(f"no-answer gated      : {rep.no_answer_ok}/{rep.no_answer_n}")
    print(f"false abstentions    : {rep.false_abstain}/{rep.n} answerable questions rejected by the floor")
    print("faithfulness         : " + (f"{rep.faithfulness:.3f}" if rep.faithfulness is not None
                                       else "not measured (needs the local model; run with --live)"))
    if rep.correctness is not None:
        print(f"answer correctness   : {rep.correctness:.3f}")
    if rep.misses:
        print("\nmissed at rank 5:")
        for m in rep.misses:
            print(f"  {m['id']} rank={m['rank']} top_doc={m['top_doc']} want={m['want']}  {m['question']}")
    print("\nno-answer questions (top score vs floor):")
    for d in rep.no_answer_detail:
        print(f"  {d['id']} {d['kind']:<9} top={d['top_score']:.3f} gated={d['gated']}  {d['question']}")
    if rep.answerable_top_scores:
        print("\nfloor sweep (use this to calibrate rag.min_score for a new embedder):")
        print("  floor  no-answer gated  false abstentions")
        for f in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
            gated = sum(s < f for s in rep.no_answer_top_scores)
            fa = sum(s < f for s in rep.answerable_top_scores)
            print(f"  {f:.2f}   {gated:>2}/{rep.no_answer_n:<12} {fa:>2}/{rep.n}")


async def offline(min_score: float):
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        r = Retriever(SqliteStore(":memory:"), HashingEmbedder(2048), top_k=5, min_score=min_score)
        await rag_eval.ingest_corpus(r, SourceRegistry({}), "eval", "eval_corpus", d)
        rep = await rag_eval.evaluate_retrieval(r, "eval", rag_eval.load_golden())
    print_report(rep, f"OFFLINE (hashing embedder, min_score={min_score})")


async def live(url: str, token: str):
    import httpx
    from rag.retrieve import NOT_IN_DOCUMENTS
    golden = rag_eval.load_golden()
    H = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=300) as c:
        async def ask(q):
            await c.post(url + "/session/new", headers=H)
            r = await c.post(url + "/v1/chat/completions", headers=H, json={
                "messages": [{"role": "user", "content": q}], "documents": True, "temperature": 0})
            r.raise_for_status()
            d = r.json()
            return d["choices"][0]["message"]["content"], d
        judged = correct = faithful = na_ok = total = 0
        for g in golden:
            ans, d = await ask(g["question"])
            if g.get("no_answer"):
                na_ok += ans.strip().lower().rstrip(".") == NOT_IN_DOCUMENTS
                continue
            total += 1
            correct += g["must_contain"].lower() in ans.lower()
            if ans.strip().lower().rstrip(".") == NOT_IN_DOCUMENTS:
                faithful += 1
                continue
            await c.post(url + "/session/new", headers=H)
            verdict = await c.post(url + "/v1/chat/completions", headers=H, json={
                "temperature": 0,
                "context": [{"text": t, "source": "eval_judge"} for t in [f"QUESTION: {g['question']}\nANSWER: {ans}"]],
                "messages": [{"role": "user", "content":
                              "Retrieval passages were used to write the ANSWER above. Is every claim in the "
                              "ANSWER supported by the document context the assistant had? Reply only YES or NO."}]})
            faithful += "yes" in verdict.json()["choices"][0]["message"]["content"].lower()
    print("\n== LIVE (gateway private lane) ==")
    print(f"answerable {total}; correctness {correct/total:.3f}; faithfulness {faithful/total:.3f}")
    print(f"no-answer returned '{NOT_IN_DOCUMENTS}': {na_ok}/{sum(1 for g in golden if g.get('no_answer'))}")
    print("note: the judge sees only the question and answer here; wire a stricter judge before trusting it")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=float, default=0.2)
    ap.add_argument("--live", nargs=2, metavar=("GATEWAY_URL", "TOKEN"))
    a = ap.parse_args()
    asyncio.run(live(*a.live) if a.live else offline(a.min_score))
