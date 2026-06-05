# PDF AI Extraction (P3) — PDF spec → Draft Template

Turns an uploaded **PDF business specification** into a **Draft Template** of fields
and validation rules. Every item is born `SUGGESTED` with a **citation back to the
PDF**, a **confidence**, and a one-line **reasoning**. Nothing is usable until a
human approves it.

> *AI suggests · humans approve.* The extractor proposes; it never decides a
> comparison outcome, and an un-approved draft cannot run (enforced in the API and
> by `approval.require_approved`).

## Pipeline
```
PDF ─► pdf.load_pdf()         text per page + file hash + scanned detection
    ─► Extractor.extract()    -> ExtractionResult (fields[], rules[], citations, confidence)
    ─► new_extraction_run()   -> audit record (file_hash, model, prompt_version, low-conf)
    ─► to_draft_template()    -> Draft Template (status=DRAFT, all review_status=SUGGESTED)
    ─► approval.approve_all()  (human gate) -> PUBLISHED, locked
    ─► resolve()+engine.run()  reconcile  (same engine as the manual path)
```

## Extractors (swappable, like the data plane)
| Extractor | Use | Runs offline? |
|---|---|---|
| `MockExtractor` | deterministic parse of FIELDS/RULES sections; reference impl + tests | ✅ |
| `AnthropicExtractor` | free-form specs via Claude, constrained to the `emit_template` tool (structured output) | needs `ANTHROPIC_API_KEY` |

Both return the same `ExtractionResult`; the API defaults to `MockExtractor` and can
be constructed with `create_app(extractor=AnthropicExtractor())`.

## Scanned PDFs (OCR)
A PDF with no text layer is flagged `is_scanned` and **blocks extraction** until text
is recovered. `load_pdf(path, ocr=engine)` routes it through an `OcrEngine`:
| Engine | Use | Offline? |
|---|---|---|
| `MockOcr(text)` | deterministic stand-in; tests the scanned path | ✅ |
| `TesseractOcr(lang="tha+eng")` | production OCR via pdf2image + pytesseract | needs Tesseract + poppler |

Wire it in the API with `create_app(ocr=TesseractOcr())`; uploads that are scanned then
extract instead of returning 422. Install the prod deps with:
`pip install pdf2image pytesseract` (+ the Tesseract binary and poppler on the host).

## Guarantees
- **Human-in-the-loop:** draft items are `SUGGESTED`; `POST /recon/runs` returns 422
  until the template is approved.
- **Traceable:** each field/rule carries `{page, source_text}` (and `bbox` once OCR/
  layout provides it) → the report's *PDF Ref* column links a verdict to its clause.
- **Reproducible/audited:** `ExtractionRun` stamps `file_hash` + `model` +
  `prompt_version`; low-confidence items are flagged for review.
- **No hallucinated outcomes:** the LLM is constrained to a JSON tool schema and only
  the 8 supported rule types.

## Try it
```bash
python -m extraction.sample            # writes extraction/fixtures/ncb_spec.pdf
python -m extraction.test_extraction   # PDF -> draft -> approve -> reconcile (PASS)
python -m api.test_api_extraction      # same flow over HTTP
```
