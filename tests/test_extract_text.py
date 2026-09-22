"""/v1/extract-text: PDF text extraction for the UI's file attach. Stateless, no persistence."""
import sys

sys.path.insert(0, "tests/rag")
from pdfgen import write_pdf  # noqa: E402

from conftest import auth  # noqa: E402


def test_extracts_text_from_a_pdf_and_stores_nothing(make_rig, tmp_path):
    rig = make_rig()
    p = tmp_path / "resume.pdf"
    write_pdf(p, ["Neetan Kumar", "Senior Software Engineer", "Skills: Python, Docker"])
    r = rig.client.post("/v1/extract-text", headers=auth(),
                        files={"file": ("resume.pdf", p.read_bytes(), "application/pdf")})
    assert r.status_code == 200
    assert "Senior Software Engineer" in r.json()["text"]


def test_rejects_non_pdf_extensions(make_rig):
    rig = make_rig()
    r = rig.client.post("/v1/extract-text", headers=auth(),
                        files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400 and r.json()["error"] == "only_pdf_supported"


def test_rejects_oversized_upload(make_rig):
    rig = make_rig()
    big = b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024)
    r = rig.client.post("/v1/extract-text", headers=auth(),
                        files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413


def test_rejects_a_pdf_with_no_extractable_text(make_rig):
    rig = make_rig()
    r = rig.client.post("/v1/extract-text", headers=auth(),
                        files={"file": ("bad.pdf", b"not a real pdf", "application/pdf")})
    assert r.status_code == 400


def test_requires_auth(make_rig):
    rig = make_rig()
    r = rig.client.post("/v1/extract-text", files={"file": ("a.pdf", b"x", "application/pdf")})
    assert r.status_code == 401
