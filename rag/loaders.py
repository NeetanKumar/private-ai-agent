"""Load md / txt / pdf files as plain text. Anything else is rejected."""
from __future__ import annotations

from pathlib import Path

from .types import RagError

MAX_BYTES = 20 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}


def load_text(path: Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        raise RagError(f"{path.name}: file not found or unreadable") from None
    if size > MAX_BYTES:
        raise RagError(f"{path.name}: file too large")
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise RagError(f"{path.name}: encrypted PDFs are not supported")
            pages = [(p.extract_text() or "").strip() for p in reader.pages]
        except PyPdfError as e:
            raise RagError(f"{path.name}: unreadable PDF ({type(e).__name__})") from None
        return "\n\n".join(p for p in pages if p)
    raise RagError(f"{path.name}: unsupported type {suffix or '(none)'}; use md, txt or pdf")
