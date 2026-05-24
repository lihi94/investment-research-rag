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
    ocr_lang: str = "eng",
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


_TESSERACT_CONFIGURED = False
_POPPLER_PATH: str | None = None


def _configure_tesseract() -> None:
    """
    Auto-locate Tesseract on Windows if it's not in PATH.

    The standard UB-Mannheim Windows installer puts tesseract.exe in
    'C:\\Program Files\\Tesseract-OCR\\' but doesn't always update PATH for
    silent installs. We probe the common install locations and tell pytesseract
    where to find it.
    """
    global _TESSERACT_CONFIGURED
    if _TESSERACT_CONFIGURED:
        return

    import os
    import shutil
    import pytesseract

    # If tesseract is on PATH, nothing to do.
    if shutil.which("tesseract"):
        _TESSERACT_CONFIGURED = True
        return

    # Probe common Windows install locations.
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for path in candidates:
        if Path(path).exists():
            pytesseract.pytesseract.tesseract_cmd = path
            _TESSERACT_CONFIGURED = True
            return

    # Fall through: leave pytesseract default — it'll raise a clear error on first call.
    _TESSERACT_CONFIGURED = True


def _find_poppler_path() -> str | None:
    """
    Auto-locate Poppler's bin directory on Windows.

    Checks PATH first; falls back to common install locations including the
    winget package directory (which has a version-specific path that changes
    each upgrade, so we glob for it).
    """
    global _POPPLER_PATH
    if _POPPLER_PATH is not None:
        return _POPPLER_PATH or None  # cache hit (may be empty string for "not found")

    import os
    import shutil

    if shutil.which("pdftoppm"):
        _POPPLER_PATH = ""  # Empty => pdf2image will use PATH
        return None

    # Probe common install locations (winget, scoop, manual installs).
    search_roots = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"),
        os.path.expandvars(r"%USERPROFILE%\scoop\apps\poppler"),
        r"C:\Program Files\poppler",
        r"C:\Program Files (x86)\poppler",
    ]
    for root in search_roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        # Look for any pdftoppm.exe under this root (recursive).
        for exe in root_path.rglob("pdftoppm.exe"):
            _POPPLER_PATH = str(exe.parent)
            return _POPPLER_PATH

    _POPPLER_PATH = ""  # Not found
    return None


def _ocr_single_page(path: Path, page_number: int, lang: str) -> str:
    """Run OCR on a single page. Imports are lazy so this fails only when actually used."""
    try:
        from pdf2image import convert_from_path
        import pytesseract  # noqa: F401  (needed for _configure_tesseract)
    except ImportError as e:
        raise RuntimeError(
            "OCR dependencies not installed. Run: pip install pytesseract pdf2image pillow\n"
            "Also requires Tesseract + Poppler on the system (see README.md)."
        ) from e

    _configure_tesseract()
    import pytesseract  # re-import after configuration

    poppler_path = _find_poppler_path()

    images = convert_from_path(
        str(path),
        first_page=page_number,
        last_page=page_number,
        dpi=300,  # Higher DPI = better OCR accuracy, slower. 300 is a good default.
        poppler_path=poppler_path,
    )
    if not images:
        return ""
    return pytesseract.image_to_string(images[0], lang=lang)
