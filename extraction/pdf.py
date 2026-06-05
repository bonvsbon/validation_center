"""PDF ingestion: load text per page, hash the file, detect scanned vs digital.

Digital PDFs yield a text layer directly (pypdf). A scanned PDF yields little/no
text -> flagged is_scanned=True, where production would route to OCR (Azure
Document Intelligence / Textract / Tesseract) before extraction.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List
import hashlib

from pypdf import PdfReader


@dataclass
class PdfPage:
    page: int                 # 1-based
    text: str
    lines: List[str] = field(default_factory=list)


@dataclass
class PdfDoc:
    file_hash: str
    page_count: int
    pages: List[PdfPage]
    is_scanned: bool

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pdf(path: str) -> PdfDoc:
    reader = PdfReader(path)
    pages: List[PdfPage] = []
    total_chars = 0
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        total_chars += len(text.strip())
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        pages.append(PdfPage(page=i, text=text, lines=lines))
    is_scanned = total_chars < 20  # essentially no text layer
    return PdfDoc(
        file_hash=file_hash(path),
        page_count=len(pages),
        pages=pages,
        is_scanned=is_scanned,
    )
