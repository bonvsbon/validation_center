"""OCR engines for scanned PDFs (no text layer).

`pdf.load_pdf(path, ocr=...)` routes a scanned document through one of these to
recover text before extraction. Swappable like every other AI/IO boundary:

  * MockOcr      — returns canned text; lets the scanned path be tested offline.
  * TesseractOcr — production OCR via pdf2image (poppler) + pytesseract (Tesseract).
                   Gated on those being installed; raises a clear error otherwise.
"""
from __future__ import annotations
from typing import List

from .pdf import PdfPage


class OcrEngine:
    name = "ocr"

    def recognize(self, path: str) -> List[PdfPage]:  # pragma: no cover
        raise NotImplementedError


class MockOcr(OcrEngine):
    """Deterministic stand-in: returns provided text as the recognized content."""
    name = "mock-ocr/1.0.0"

    def __init__(self, text: str, pages: int = 1):
        self._text = text
        self._pages = max(1, pages)

    def recognize(self, path: str) -> List[PdfPage]:
        lines = [ln.strip() for ln in self._text.splitlines() if ln.strip()]
        # single page is sufficient for the spec; multi-page would split lines
        return [PdfPage(page=1, text=self._text, lines=lines)]


class TesseractOcr(OcrEngine):
    """Production OCR. Requires:  pip install pdf2image pytesseract  + the Tesseract
    binary and poppler installed on the host. Defaults to Thai+English."""

    def __init__(self, dpi: int = 200, lang: str = "tha+eng"):
        self.dpi = dpi
        self.lang = lang

    @property
    def name(self) -> str:
        return f"tesseract/{self.lang}"

    def recognize(self, path: str) -> List[PdfPage]:
        try:
            from pdf2image import convert_from_path
            import pytesseract
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "TesseractOcr needs 'pdf2image' + 'pytesseract' (and the Tesseract "
                "binary + poppler installed on the host)") from e
        pages: List[PdfPage] = []
        for i, img in enumerate(convert_from_path(path, dpi=self.dpi), start=1):
            text = pytesseract.image_to_string(img, lang=self.lang) or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            pages.append(PdfPage(page=i, text=text, lines=lines))
        return pages
