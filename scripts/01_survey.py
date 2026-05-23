"""
01_survey.py - Scan the PDF directory and produce a pre-ingestion report.

WHAT THIS SCRIPT DOES (and what it does NOT do):
  - Counts PDFs, computes file sizes.
  - Detects which PDFs are digital-text vs scanned (samples 5 pages each).
  - Extracts text from digital PDFs to estimate chunk count and token cost.
  - Skips scanned PDFs in the estimate (their text content is unknown until OCR).
  - Prints a clean report.

It does NOT:
  - Touch the database.
  - Call OpenAI or any paid API.
  - Modify any files.

Run this before 02_ingest.py to know what you're getting into.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# Make `src` importable when running from project root: python scripts/01_survey.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tqdm import tqdm

from src.config import CONFIG
from src.chunker import build_chunks
from src.embeddings import count_tokens, estimate_cost, PRICE_PER_MILLION_TOKENS
from src.pdf_utils import (
    PageText,
    detect_pdf_kind,
    extract_pages,
    list_pdfs,
    sha256_file,
)


# -----------------------------------------------------------------------------
# Per-file inspection
# -----------------------------------------------------------------------------

class FileReport:
    """Holds the results of inspecting one PDF."""

    def __init__(self, path: Path):
        self.path = path
        self.size_mb: float = path.stat().st_size / (1024 * 1024)
        self.kind: str = "unknown"          # 'text' | 'scanned' | 'mixed' | 'unknown' | 'error'
        self.error: str | None = None
        self.estimated_chunks: int = 0
        self.estimated_tokens: int = 0


def inspect_file(path: Path) -> FileReport:
    """Inspect one PDF and return a report. Never raises — errors stored on the report."""
    report = FileReport(path)
    try:
        report.kind = detect_pdf_kind(path)
    except Exception as e:
        report.kind = "error"
        report.error = str(e)
        return report

    # Only digital-text PDFs get a chunk/cost estimate here.
    # Scanned PDFs would require OCR (slow and out of scope for the survey).
    if report.kind in ("text", "mixed"):
        try:
            pages: list[PageText] = list(extract_pages(path, use_ocr=False))
            chunks = build_chunks(pages)
            report.estimated_chunks = len(chunks)
            report.estimated_tokens = sum(count_tokens(c.text) for c in chunks)
        except Exception as e:
            # Don't crash the whole survey for one bad PDF.
            report.error = f"chunking error: {e}"

    return report


# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------

def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _human_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.1f} MB"


def print_report(reports: list[FileReport]) -> None:
    total = len(reports)
    by_kind = Counter(r.kind for r in reports)
    errors = [r for r in reports if r.error]

    total_size_mb = sum(r.size_mb for r in reports)
    total_chunks = sum(r.estimated_chunks for r in reports)
    total_tokens = sum(r.estimated_tokens for r in reports)

    scanned_count = by_kind.get("scanned", 0)
    text_count = by_kind.get("text", 0) + by_kind.get("mixed", 0)

    _section("OVERVIEW")
    print(f"  PDF directory:        {CONFIG.pdf_dir}")
    print(f"  Total PDFs found:     {total}")
    print(f"  Total size:           {_human_size(total_size_mb)}")
    print()
    print(f"  Digital text PDFs:    {by_kind.get('text', 0)}")
    print(f"  Mixed (some scanned): {by_kind.get('mixed', 0)}")
    print(f"  Fully scanned:        {scanned_count}  (need OCR)")
    print(f"  Failed to inspect:    {by_kind.get('error', 0)}")

    _section("EMBEDDING COST ESTIMATE (digital-text PDFs only)")
    print(f"  Estimated chunks:     {total_chunks:,}")
    print(f"  Estimated tokens:     {total_tokens:,}")
    cost = estimate_cost(total_tokens)
    print(f"  Estimated cost:       ${cost:.4f} USD")
    print(f"  ({PRICE_PER_MILLION_TOKENS:.3f} USD per 1M tokens for {CONFIG.embedding_model})")

    if scanned_count > 0:
        _section("SCANNED PDFs (excluded from cost estimate)")
        print(f"  {scanned_count} PDFs are image-based and would require OCR.")
        print(f"  Their token count is unknown until OCR runs.")
        print(f"  Rough rule: assume similar token density to digital PDFs of the same size.")
        if text_count > 0:
            text_size_mb = sum(r.size_mb for r in reports if r.kind in ("text", "mixed"))
            scanned_size_mb = sum(r.size_mb for r in reports if r.kind == "scanned")
            if text_size_mb > 0:
                tokens_per_mb = total_tokens / text_size_mb
                projected_scanned_tokens = int(tokens_per_mb * scanned_size_mb)
                projected_cost = estimate_cost(projected_scanned_tokens)
                print(f"  Projected scanned tokens: ~{projected_scanned_tokens:,}")
                print(f"  Projected scanned cost:   ~${projected_cost:.4f} USD")

    if errors:
        _section(f"ERRORS ({len(errors)})")
        for r in errors[:20]:
            print(f"  [!] {r.path.name}")
            print(f"      {r.error}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more.")

    _section("NEXT STEP")
    if total == 0:
        print(f"  No PDFs found. Put your books in: {CONFIG.pdf_dir}")
    else:
        total_estimated_cost = cost
        if scanned_count > 0 and text_count > 0:
            total_estimated_cost += estimate_cost(projected_scanned_tokens)
        print(f"  If the numbers above look reasonable, run:")
        print(f"      python scripts/02_ingest.py")
        print(f"  Total projected cost (digital + OCR projection):  ~${total_estimated_cost:.4f}")
    print()


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    pdf_dir = CONFIG.pdf_dir
    if not pdf_dir.exists():
        print(f"[error] PDF_DIR does not exist: {pdf_dir}")
        print(f"        Create it and place your PDFs inside, then re-run.")
        return 1

    pdfs = list_pdfs(pdf_dir)
    if not pdfs:
        print(f"[info] No PDF files found under: {pdf_dir}")
        print(f"       Place .pdf files there (subdirectories OK) and re-run.")
        return 0

    print(f"[info] Found {len(pdfs)} PDF(s). Inspecting (this samples pages, not full read)...")
    reports: list[FileReport] = []
    for path in tqdm(pdfs, unit="pdf"):
        reports.append(inspect_file(path))

    print_report(reports)
    return 0


if __name__ == "__main__":
    sys.exit(main())
