"""Generate a sample NCB spec PDF (the 'Business Specification' a user uploads).

The spec contains a human-readable preamble plus two machine-parseable sections
(FIELDS, RULES). The deterministic MockExtractor parses those sections; the
AnthropicExtractor would handle free-form specs via the LLM. Lines are drawn one
per row so the PDF text layer round-trips cleanly through pypdf.
"""
from __future__ import annotations
import os

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

PREAMBLE = [
    "NCB Motorcycle Hire-Purchase Submission Specification (M16)",
    "Next Capital PCL  -  Internal pre-submission data spec  -  v1.0",
    "",
    "Each account record submitted to the National Credit Bureau must satisfy the",
    "field definitions and validation rules below. Source = loan system of record;",
    "Destination = the NCB submission file being verified.",
    "",
]

FIELDS = [
    "SECTION: FIELDS",
    "account_no | STRING | key",
    "id_card | STRING | required key",
    "outstanding_balance | DECIMAL | required",
    "status | ENUM(NCB_ACCOUNT_STATUS) | required",
    "rate | DECIMAL |",
    "open_date | DATE |",
    "term | INTEGER |",
    "install | DECIMAL |",
    "total | DECIMAL |",
    "",
]

RULES = [
    "SECTION: RULES",
    "id_card :: EQUALITY :: ERROR",
    "outstanding_balance :: TOLERANCE tolerance=0.01 scale=4 :: ERROR",
    "rate :: RANGE min=0 max=36 :: WARNING",
    "id_card :: REGEX pattern=^[0-9]{13}$ :: ERROR",
    "status :: NOT_NULL :: ERROR",
    "status :: LOOKUP code_list=NCB_ACCOUNT_STATUS :: ERROR",
    "open_date :: DATE_VALID format=%Y-%m-%d not_future=true :: ERROR",
    "term,install,total :: CROSS_FIELD lhs=f_term*f_install rhs=f_total tolerance=1 :: WARNING",
]


def build_sample_pdf(path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = canvas.Canvas(path, pagesize=LETTER)
    width, height = LETTER
    y = height - 72

    def line(txt: str, size: int = 11, font: str = "Helvetica"):
        nonlocal y
        if y < 72:
            c.showPage(); y = height - 72
        c.setFont(font, size)
        c.drawString(72, y, txt)
        y -= size + 6

    for i, t in enumerate(PREAMBLE):
        line(t, size=15 if i == 0 else 11, font="Helvetica-Bold" if i == 0 else "Helvetica")
    for t in FIELDS:
        line(t, font="Helvetica-Bold" if t.startswith("SECTION") else "Courier")
    for t in RULES:
        line(t, font="Helvetica-Bold" if t.startswith("SECTION") else "Courier")

    c.showPage()
    c.save()
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "fixtures", "ncb_spec.pdf")
    print("wrote", build_sample_pdf(out))
