"""
pdf_utils.py - PDF inspection, text extraction, and OCR fallback.

A PDF can be "text" (digital, has selectable text) or "scanned" (images of pages).
The strategy: try fast text extraction first; if a page yields almost no text,
fall back to OCR on that page only. This keeps cost minimal on mixed PDFs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pdfplumber


# Threshold for deciding a page is "scanned": fewer than this many alphabetic
# characters extracted from a non-empty page suggests an image-based page.
_TEXT_PER_PAGE_THRESHOLD = 50


@dataclass
class PageText:
    page_number: int    # 1-based, matches what humans expect
    text: str
    is_ocr: bool        # True if we used OCR to get this text


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Compute the SHA256 of a file. Used to deduplicate sources."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def list_pdfs(root: Path) -> list[Path]:
    """Recursively find all .pdf files under `root`."""
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.pdf") if p.is_file())


def detect_pdf_kind(path: Path, sample_pages: int = 5) -> str:
    """
    Inspect up to `sample_pages` pages and classify the PDF.
    Returns one of: 'text', 'scanned', 'mixed'.

    We sample (not full scan) for speed — a 500-page PDF would otherwise take
    minutes to classify.
    """
    text_pages = 0
    scanned_pages = 0
    try:
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            indices = _sample_indices(total, sample_pages)
            for i in indices:
                try:
                    txt = pdf.pages[i].extract_text() or ""
                except Exception:
                    txt = ""
                alpha_count = sum(1 for ch in txt if ch.isalpha())
                if alpha_count >= _TEXT_PER_PAGE_THRESHOLD:
                    text_pages += 1
                else:
                    scanned_pages += 1
    except Exception:
        # If pdfplumber can't even open it, treat as scanned and let OCR try.
        return "scanned"

    if text_pages and not scanned_pages:
        return "text"
    if scanned_pages and not text_pages:
        return "scanned"
    return "mixed"


def _sample_indices(total: int, n: int) -> list[int]:
    """Pick `n` evenly-spaced page indices from a `total`-page document."""
    if total <= n:
        return list(range(total))
    step = total / n
    return [int(i * step) for i in range(n)]


def extract_pages(
    path: Path,
    use_ocr: bool = False,
    ocr_lang: str = "eng+heb",
) -> Iterator[PageText]:
    """
    Yield PageText for each page in the PDF.
    If `use_ocr=False`: digital text extraction only. Pages with no text yield "".
    If `use_ocr=True`: pages with insufficient digital text fall back to OCR.

    OCR is lazy-imported because pytesseract/pdf2image have external runtime
    dependencies (Tesseract, Poppler) the user may not have installed yet.
    """
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_number = i + 1
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""

            alpha_count = sum(1 for ch in txt if ch.isalpha())
            if alpha_count >= _TEXT_PER_PAGE_THRESHOLD:
                yield PageText(page_number=page_number, text=txt, is_ocr=False)
                continue

            if not use_ocr:
                # Yield an empty page so the chunker keeps page numbering aligned.
                yield PageText(page_number=page_number, text="", is_ocr=False)
                continue

            # OCR fallback for this page
            ocr_text = _ocr_single_page(path, page_number, lang=ocr_lang)
            yield PageText(page_number=page_number, text=ocr_text, is_ocr=True)


def _ocr_single_page(path: Path, page_number: int, lang: str) -> str:
    """Run OCR on a single page. Imports are lazy so this fails only when actually used."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError as e:
        raise RuntimeError(
            "OCR dependencies not installed. Run: pip install pytesseract pdf2image pillow\n"
            "Also requires Tesseract + Poppler on the system (see README.md)."
        ) from e

    images = convert_from_path(
        str(path),
        first_page=page_number,
        last_page=page_number,
        dpi=300,  # Higher DPI = better OCR accuracy, slower. 300 is a good default.
    )
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang=lang)
