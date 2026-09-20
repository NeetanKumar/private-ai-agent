"""Score the 20-task tool-use set.

  python tests/agent/run_eval.py --url http://localhost:11434/v1 --model qwen3.5:9b   real model
  python tests/agent/run_eval.py --self-check                                          harness only

--self-check uses a scripted stand-in that always answers correctly. It proves the harness and the
task set are sound. It says nothing about any real model.
"""
import argparse
import asyncio
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "agent"), str(ROOT / "tests" / "rag")]

import tool_eval                                             # noqa: E402
import rag_eval                                              # noqa: E402
from gateway.config import get_settings                      # noqa: E402
from gateway.taint import SourceRegistry                     # noqa: E402
from gateway.tools import ToolContext                        # noqa: E402
from rag.embed import HashingEmbedder                        # noqa: E402
from rag.retrieve import Retriever                           # noqa: E402
from rag.store_sqlite import SqliteStore                     # noqa: E402


async def fixtures(tmp: pathlib.Path) -> ToolContext:
    root = tmp / "files" / "eval"
    root.mkdir(parents=True)
    for f in rag_eval.CORPUS.glob("*"):
        (root / f.name).write_bytes(f.read_bytes())
    rag_eval.ensure_pdf(root)
    r = Retriever(SqliteStore(":memory:"), HashingEmbedder(2048), top_k=5, min_score=0.10)
    await rag_eval.ingest_corpus(r, SourceRegistry({}), "eval", "eval_corpus", tmp)
    return ToolContext("eval", tmp / "files", 20000, 1_000_000, r)


async def main(a):
    cfg = get_settings()
    with tempfile.TemporaryDirectory() as d:
        ctx = await fixtures(pathlib.Path(d))
        if a.self_check:
            from oracle import oracle_model
            model = oracle_model(tool_eval.load_tasks())
        else:
            model = tool_eval.http_model(a.url, a.model)
        rep = await tool_eval.run_eval(model, tool_eval.load_tasks(), ctx, tool_eval.system_prompt(cfg))
    print(rep.summary())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url"), ap.add_argument("--model"), ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if not a.self_check and not (a.url and a.model):
        ap.error("give --url and --model, or --self-check")
    asyncio.run(main(a))
