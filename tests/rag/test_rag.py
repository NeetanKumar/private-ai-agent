"""Phase 3 acceptance: loaders, chunking, golden-set retrieval, taint inheritance, isolation."""
import asyncio
import json
import pathlib

import pytest

from gateway.fragments import Taint
from gateway.taint import SourceRegistry
from rag.chunk import chunk_text
from rag.embed import HashingEmbedder
from rag.ingest import ingest_file
from rag.loaders import load_text
from rag.retrieve import NOT_IN_DOCUMENTS, Retriever
from rag.store_sqlite import SqliteStore
from rag.types import RagError

import rag_eval

REG = SourceRegistry({"public_docs": "CLEAN"})


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def make_retriever(min_score=0.10):
    return Retriever(SqliteStore(":memory:"), HashingEmbedder(2048), top_k=5, min_score=min_score)


@pytest.fixture(scope="module")
def corpus_retriever(tmp_path_factory):
    r = make_retriever()
    run(rag_eval.ingest_corpus(r, REG, "owner", "company_docs", tmp_path_factory.mktemp("pdf")))
    return r


# ---- loaders and chunking ------------------------------------------------------------------------

def test_loads_md_txt_and_pdf(tmp_path):
    assert "24 days" in load_text(rag_eval.CORPUS / "hr_leave_policy.md")
    assert "north car park" in load_text(rag_eval.CORPUS / "facilities_guide.txt")
    assert "42 million" in load_text(rag_eval.ensure_pdf(tmp_path))


def test_rejects_unsupported_and_broken_files(tmp_path):
    (tmp_path / "x.docx").write_bytes(b"PK")
    with pytest.raises(RagError):
        load_text(tmp_path / "x.docx")
    with pytest.raises(RagError):
        load_text(tmp_path / "missing.md")
    (tmp_path / "bad.pdf").write_bytes(b"this is not a pdf")
    with pytest.raises(RagError):
        load_text(tmp_path / "bad.pdf")


def test_chunks_keep_heading_path_and_respect_size():
    text = "# Policy\n\n## Leave\n" + " ".join(f"Sentence number {i} is here." for i in range(60))
    chunks = chunk_text(text, max_chars=300)
    assert len(chunks) > 3
    assert all(c.startswith("[Policy > Leave]") for c in chunks)
    assert all(len(c) <= 300 + len("[Policy > Leave] ") + 60 for c in chunks)     # overlap sentence allowance


def test_chunk_overlap_carries_a_sentence():
    text = "## A\n" + " ".join(f"Fact {i} is stated here plainly." for i in range(30))
    chunks = chunk_text(text, max_chars=200, overlap_sentences=1)
    first_tail = chunks[0].split(". ")[-1].rstrip(".")
    assert first_tail in chunks[1]


# ---- golden set: 50 answerable + 10 no-answer ---------------------------------------------------------

def test_golden_set_shape():
    g = rag_eval.load_golden()
    assert sum(1 for x in g if not x.get("no_answer")) == 50
    assert sum(1 for x in g if x.get("no_answer")) == 10


def test_golden_answers_exist_in_the_corpus(corpus_retriever):
    for g in rag_eval.load_golden():
        if g.get("no_answer"):
            continue
        hits = run(corpus_retriever.search("owner", g["must_contain"], k=50))
        assert any(g["must_contain"].lower() in h.text.lower() for h in hits), g["id"]


def test_recall_mrr_and_gate_on_golden_set(corpus_retriever, capsys):
    rep = run(rag_eval.evaluate_retrieval(corpus_retriever, "owner", rag_eval.load_golden()))
    print(f"\nOFFLINE hashing embedder: recall@5={rep.recall_at_5:.3f} MRR={rep.mrr:.3f} "
          f"no-answer gated={rep.no_answer_ok}/{rep.no_answer_n} false-abstain={rep.false_abstain}/{rep.n}")
    # Regression floors for the offline lexical embedder, set below the measured values.
    assert rep.recall_at_5 >= 0.85
    assert rep.mrr >= 0.75
    assert rep.false_abstain <= 5                     # the floor must not reject answerable questions
    off_topic = [d for d in rep.no_answer_detail if d["kind"] == "off_topic"]
    assert len(off_topic) == 6 and all(d["gated"] for d in off_topic)


def test_no_answer_gate_returns_empty_so_no_model_is_called(corpus_retriever):
    assert run(corpus_retriever.retrieve("owner", "What is the recipe for sourdough bread?")) == []


def test_generation_metrics_plumbing_with_stub_model(corpus_retriever):
    """Faithfulness needs a real model; this checks the harness logic with stand-ins."""
    async def answerer(q, passages):
        return passages[0]                            # "answers" with the top passage
    async def judge(ans, passages):
        return ans in passages                        # supported iff quoted from the passages
    rep = rag_eval.Report()
    rep = run(rag_eval.evaluate_generation(corpus_retriever, "owner", rag_eval.load_golden(),
                                           answerer, judge, rep))
    assert rep.faithfulness == 1.0 and 0.5 < rep.correctness <= 1.0
    assert rep.no_answer_ok >= 6                      # gated ones return exactly "not in documents"


# ---- taint inheritance --------------------------------------------------------------------------------

def test_chunks_inherit_source_taint_and_unregistered_is_private(tmp_path):
    r = make_retriever()
    f = tmp_path / "note.md"
    f.write_text("# Note\nThe launch codeword is PINEAPPLE-7.")
    pub = run(ingest_file(r.store, r.embedder, REG, "u", "public_docs", f))
    priv = run(ingest_file(r.store, r.embedder, REG, "u", "some_unregistered_source", f))
    assert pub.taint == Taint.CLEAN and priv.taint == Taint.PRIVATE
    by_source = {d.source: d.taint for d in r.store.list_documents("u")}
    assert by_source["public_docs"] == Taint.CLEAN and by_source["some_unregistered_source"] == Taint.PRIVATE
    hits = run(r.retrieve("u", "launch codeword"))
    assert {h.source: h.taint for h in hits} == {"public_docs": Taint.CLEAN, "some_unregistered_source": Taint.PRIVATE}


def test_reingesting_a_document_replaces_its_chunks(tmp_path):
    r = make_retriever()
    f = tmp_path / "a.md"
    f.write_text("# A\nThe gate code is 1234.")
    d1 = run(ingest_file(r.store, r.embedder, REG, "u", "s", f))
    d2 = run(ingest_file(r.store, r.embedder, REG, "u", "s", f))
    assert d1.doc_id == d2.doc_id and len(r.store.list_documents("u")) == 1
    assert r.store.delete_document("u", d1.doc_id) > 0 and r.store.list_documents("u") == []


def test_store_refuses_to_mix_embedders(tmp_path):
    r = make_retriever()
    f = tmp_path / "a.md"
    f.write_text("# A\nHello world facts.")
    run(ingest_file(r.store, r.embedder, REG, "u", "s", f))
    with pytest.raises(RagError):
        r.store.add_document("u", "x", "x.md", "s", Taint.PRIVATE, ["t"], [[0.1] * 4], "other-embedder-v9")


# ---- user isolation -------------------------------------------------------------------------------------

def test_user_a_can_never_retrieve_user_b_chunks(tmp_path):
    r = make_retriever()
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("# Alpha\nUser A secret: the vault code is 4471-ALPHA.")
    b.write_text("# Beta\nUser B secret: the vault code is 9082-BETA. Vault vault vault code code.")
    da = run(ingest_file(r.store, r.embedder, REG, "user_a", "notes", a))
    db = run(ingest_file(r.store, r.embedder, REG, "user_b", "notes", b))
    for q in ("What is the vault code?", "vault code 9082-BETA", "User B secret", "BETA"):
        hits = run(r.search("user_a", q, k=50))
        assert hits and all("BETA" not in h.text and h.doc_id == da.doc_id for h in hits)
        hits_b = run(r.search("user_b", q.replace("BETA", "ALPHA"), k=50))
        assert all("ALPHA" not in h.text and h.doc_id == db.doc_id for h in hits_b)
    # metadata filters cannot reach across users, and hostile filter values are just data
    assert run(r.search("user_a", "vault", filter={"doc_id": db.doc_id})) == []
    assert run(r.search("user_a", "vault", filter={"source": "notes' OR '1'='1"})) == []
    assert run(r.search("nobody", "vault")) == []
    assert r.store.list_documents("user_a")[0].doc_id == da.doc_id and len(r.store.list_documents("user_a")) == 1
    with pytest.raises(RagError):
        r.store.query("", [0.0] * 2048, 5)


def test_isolation_holds_across_the_shipped_golden_corpus(corpus_retriever, tmp_path):
    """Owner has the whole corpus; a second user with one unrelated doc must never see any of it."""
    other = tmp_path / "mine.md"
    other.write_text("# Mine\nMy cat is called Biscuit.")
    run(ingest_file(corpus_retriever.store, corpus_retriever.embedder, REG, "guest", "notes", other))
    for g in rag_eval.load_golden():
        for h in run(corpus_retriever.search("guest", g["question"], k=50)):
            assert h.doc_name == "mine.md"
