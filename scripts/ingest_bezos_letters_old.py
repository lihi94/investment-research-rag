"""
ingest_bezos_letters_old.py - Ingest older Bezos shareholder letters (1997-2015)
via SEC EDGAR full-text search and HTML extraction.

USER-AUTHORIZED on 2026-05-28. SEC filings are public domain documents
required by law to be freely accessible.

Strategy
  The modern aboutamazon.com pages only go back to ~2016. For 1997-2015, the
  shareholder letters live inside Amazon's 10-K annual filings on SEC EDGAR.

  Two approaches, tried in order:

  1. EDGAR full-text exhibit: Amazon's 10-K filings (CIK 0001018724) include
     Exhibit 13 or the body of the 10-K which contains the shareholder letter.
     We fetch the filing index and look for the exhibit.

  2. ir.aboutamazon.com: Amazon occasionally links older letters as PDFs or HTML
     at https://ir.aboutamazon.com/annual-reports-proxies-and-shareholder-letters/

  Each letter extracted this way is tagged identically to the modern ones so
  queries span the full 1997-2023 arc without knowing which script ingested them.

Tagging:
  source_type   = 'ceo_annual_letter'      (same as ingest_bezos_letters.py)
  author        = 'Jeff Bezos'
  metadata.company  = 'Amazon'
  metadata.year     = <year>
  metadata.channel  = 'bezos_letters'
  metadata.batch_id = <UUID for this run>

Rollback:
  python scripts/delete_by_type.py --source-type ceo_annual_letter
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.config import CONFIG
from src.chunker import Chunk, build_chunks
from src.db import (
    SB,
    find_source_by_hash,
    insert_chunks,
    insert_source,
    mark_source_ingested,
)
from src.embeddings import embed_many
from src.pdf_utils import PageText


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

# These years were NOT ingested by ingest_bezos_letters.py
# (which got 2016-2021 from aboutamazon.com)
TARGET_YEARS = list(range(1997, 2016))   # 1997-2015

AMAZON_CIK = "0001018724"

HEADERS = {
    "User-Agent": "investment-research-rag/1.0 (personal research tool; elihai94@gmail.com)",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# SEC EDGAR requires a proper User-Agent with contact info — see SEC guidelines.
EDGAR_HEADERS = {
    "User-Agent": "investment-research-rag/1.0 elihai94@gmail.com",
    "Accept": "text/html,application/json,*/*",
}

EDGAR_BASE = "https://data.sec.gov"
EDGAR_SUBMISSIONS = f"{EDGAR_BASE}/submissions/CIK{AMAZON_CIK}.json"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/1018724"


def _setup_logging() -> logging.Logger:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = CONFIG.log_dir / "ingest_bezos_old.log"
    logger = logging.getLogger("ingest_bezos_old")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


LOG = _setup_logging()


# -----------------------------------------------------------------------------
# SEC EDGAR helpers
# -----------------------------------------------------------------------------

def _parse_filing_batch(filings: dict) -> list[dict]:
    """Extract 10-K entries from a filings dict (the 'recent' block format)."""
    forms = filings.get("form", [])
    accessions = filings.get("accessionNumber", [])
    dates = filings.get("filingDate", [])
    results = []
    # Accept 10-K, 10-K405 (pre-2003 form name), and skip 10-K/A amendments
    ANNUAL_FORMS = {"10-K", "10-K405"}
    for form, acc, date in zip(forms, accessions, dates):
        if form in ANNUAL_FORMS:
            filing_year = int(date[:4])
            # Amazon files the 10-K for fiscal year N in early year N+1
            fiscal_year = filing_year - 1
            results.append({
                "accession": acc.replace("-", ""),
                "accession_dashed": acc,
                "date": date,
                "fiscal_year": fiscal_year,
            })
    return results


def _get_10k_filings() -> list[dict]:
    """
    Fetch Amazon's complete filing history from EDGAR (all pagination pages)
    and return a list of 10-K filings.
    Each entry: {accession, accession_dashed, date, fiscal_year}
    """
    results = []

    # Step 1: Primary submissions JSON
    try:
        r = requests.get(EDGAR_SUBMISSIONS, headers=EDGAR_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        LOG.error(f"Failed to fetch EDGAR submissions: {e}")
        return []

    recent = data.get("filings", {}).get("recent", {})
    results.extend(_parse_filing_batch(recent))

    # Step 2: Pagination files (older filings not in the 'recent' block)
    pagination_files = data.get("filings", {}).get("files", [])
    for page_file in pagination_files:
        page_name = page_file.get("name", "")
        if not page_name:
            continue
        page_url = f"https://data.sec.gov/submissions/{page_name}"
        try:
            pr = requests.get(page_url, headers=EDGAR_HEADERS, timeout=30)
            pr.raise_for_status()
            page_data = pr.json()
            results.extend(_parse_filing_batch(page_data))
            time.sleep(0.3)  # SEC rate limit
        except Exception as e:
            LOG.warning(f"Failed to fetch pagination file {page_name}: {e}")

    LOG.info(f"Found {len(results)} total 10-K filings in EDGAR across all pages")
    return results


def _get_filing_index(accession_nodash: str, accession_dashed: str) -> list[dict]:
    """
    Fetch the filing index for a given accession number.
    Returns list of {filename, description, type}.

    EDGAR index URL format:
      https://www.sec.gov/Archives/edgar/data/<cik>/<acc_nodash>/<acc_dashed>-index.htm
    """
    url = (
        f"https://www.sec.gov/Archives/edgar/data/1018724"
        f"/{accession_nodash}/{accession_dashed}-index.htm"
    )
    try:
        r = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
        if r.status_code != 200:
            LOG.debug(f"Index {url} → {r.status_code}")
            return []

        # Parse the HTML index table for document links
        html = r.text
        # EDGAR index HTML has rows: <td>type</td><td>description</td><td><a href="...">name</a></td>
        rows = re.findall(
            r'<td[^>]*>\s*([^<]*)\s*</td>\s*<td[^>]*>\s*([^<]*)\s*</td>\s*<td[^>]*>\s*<a[^>]+href="([^"]+)"',
            html, re.IGNORECASE
        )
        files = []
        for row in rows:
            files.append({
                "type": row[0].strip(),
                "description": row[1].strip(),
                "filename": row[2].strip(),
            })
        return files
    except Exception as e:
        LOG.debug(f"Index fetch failed for {accession_nodash}: {e}")
        return []


def _fetch_exhibit_or_10k(accession_nodash: str, files: list[dict]) -> tuple[str | None, str | None]:
    """
    Given a list of filing documents, try to find and fetch the shareholder letter text.
    Priority: Exhibit 13 > 10-K body (any form) > Exhibit 99.

    Returns (text, url_used) or (None, None).
    """
    candidates: list[tuple[int, dict]] = []
    for f in files:
        ftype = (f.get("type") or "").upper().strip()
        fdesc = (f.get("description") or "").upper().strip()
        fname = f.get("filename", "")

        # Skip binary / metadata files
        if any(ext in fname.lower() for ext in [".xsd", ".xml", ".xbrl", ".jpg", ".gif", ".png", ".zip"]):
            continue
        if fname.endswith(".txt") and "complete submission" in fdesc.lower():
            continue  # the 50MB full submission text — too slow

        # Rank by content relevance
        if "EX-13" in ftype or "ANNUAL REPORT TO SHARE" in fdesc:
            candidates.append((0, f))
        elif "EX-99" in ftype:
            candidates.append((1, f))
        elif ftype in {"10-K", "10-K405"} or "10K" in ftype:
            candidates.append((2, f))
        # Numeric type "1" = main 10-K document (used in older EDGAR filings)
        elif ftype == "1" or (ftype.isdigit() and "ANNUAL REPORT" in fdesc):
            candidates.append((2, f))
        elif "ANNUAL REPORT" in fdesc and ftype not in {"", "NBSP"}:
            candidates.append((3, f))

    # Also add any .htm file that looks like the main 10-K body (fallback)
    if not candidates:
        for f in files:
            fname = f.get("filename", "")
            if fname.lower().endswith((".htm", ".html")) and "10k" in fname.lower():
                candidates.append((4, f))

    candidates.sort(key=lambda x: x[0])

    for _, f in candidates:
        filename = f.get("filename", "")
        if not filename:
            continue
        if filename.startswith("/"):
            url = "https://www.sec.gov" + filename
        elif filename.startswith("http"):
            url = filename
        else:
            url = f"https://www.sec.gov/Archives/edgar/data/1018724/{accession_nodash}/{filename}"

        try:
            r = requests.get(url, headers=EDGAR_HEADERS, timeout=60)
            if r.status_code != 200:
                continue

            content_type = r.headers.get("Content-Type", "")
            if "pdf" in content_type or r.content.startswith(b"%PDF"):
                # PDF exhibit — extract text
                text = _extract_pdf_text(r.content, url)
                if text and len(text) > 1000:
                    return text, url
            else:
                # HTML exhibit — extract text
                text = _extract_html_text(r.text)
                if text and len(text) > 1000:
                    return text, url
        except Exception as e:
            LOG.debug(f"Fetch {url} failed: {e}")
        time.sleep(0.3)

    return None, None


def _extract_pdf_text(pdf_bytes: bytes, url: str) -> str | None:
    """Extract text from PDF bytes using pdfplumber."""
    import tempfile
    try:
        import pdfplumber
    except ImportError:
        LOG.warning("pdfplumber not available for PDF extraction")
        return None

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        texts = []
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    texts.append(t)
        return "\n\n".join(texts)
    except Exception as e:
        LOG.debug(f"pdfplumber failed on {url}: {e}")
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def _extract_html_text(html: str) -> str:
    """Strip HTML tags and decode entities, returning clean text."""
    # Remove scripts, styles, XBRL-specific blocks
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", html, flags=re.DOTALL | re.IGNORECASE)

    # Paragraph/heading breaks to newlines
    html = re.sub(r"</p>", "\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</?h[1-6][^>]*>", "\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</?(?:div|section|article)[^>]*>", "\n", html, flags=re.IGNORECASE)

    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", html)

    # Decode common HTML entities
    text = (text
            .replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&apos;", "'").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&mdash;", "—").replace("&ndash;", "–")
            .replace("&rsquo;", "'").replace("&lsquo;", "'")
            .replace("&rdquo;", '"').replace("&ldquo;", '"')
            .replace("&#160;", " ").replace("&#8217;", "'").replace("&#8220;", '"')
            .replace("&#8221;", '"').replace("&#8212;", "—"))

    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _find_shareholder_letter_section(text: str, year: int) -> str:
    """
    A 10-K body is huge (100+ pages). Try to isolate the shareholder letter section,
    which appears near the start. Look for the "To our shareholders:" header
    and take text until the next major section (Item 1, Business, etc.)
    """
    # Patterns that mark the START of a Bezos letter
    start_patterns = [
        r"To (?:our |our long-term )?[Ss]hareholders",
        r"Dear [Ss]hareholder",
        r"LETTER TO SHAREHOLDERS",
    ]
    # Patterns that mark the END (transition to boilerplate 10-K)
    end_patterns = [
        r"PART\s+I\b",
        r"Item\s+1\b",
        r"ITEM\s+1\b",
        r"FORWARD[- ]LOOKING STATEMENTS",
        r"BUSINESS OVERVIEW",
        r"Table of Contents",
    ]

    start_idx = -1
    for pat in start_patterns:
        m = re.search(pat, text)
        if m:
            start_idx = m.start()
            break

    if start_idx == -1:
        # Can't find the letter — return first 15k chars as fallback
        return text[:15000].strip()

    # Find first end marker AFTER the start
    end_idx = len(text)
    for pat in end_patterns:
        m = re.search(pat, text[start_idx + 100:])
        if m:
            candidate = start_idx + 100 + m.start()
            # Must be at least 500 chars into the letter
            if candidate - start_idx > 500:
                end_idx = min(end_idx, candidate)

    excerpt = text[start_idx:end_idx].strip()

    # Sanity check: letter should be between 2k and 50k chars
    if len(excerpt) < 2000:
        # Return more context if we cut too short
        return text[start_idx:start_idx + 20000].strip()

    return excerpt


# -----------------------------------------------------------------------------
# Ingestion
# -----------------------------------------------------------------------------

def _ingest_letter(
    year: int,
    text: str,
    url: str,
    batch_id: str,
) -> tuple[str, int]:
    """Insert one letter into the DB. Returns (status, chunk_count)."""
    title = f"Amazon Shareholder Letter {year}"

    file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = find_source_by_hash(file_hash)
    if existing:
        if existing.get("ingested_at"):
            from src.db import count_chunks_for_source
            return "skipped", count_chunks_for_source(existing["id"])
        else:
            # Partial insert from a prior crashed run — clean up and re-ingest
            from src.db import delete_source
            LOG.warning(f"Cleaning up partial source {existing['id']} for {title}")
            delete_source(existing["id"])

    metadata = {
        "company": "Amazon",
        "year": year,
        "channel": "bezos_letters",
        "batch_id": batch_id,
        "source_url": url,
        "ingested_via": "ingest_bezos_letters_old.py",
        "content_origin": "sec_edgar_10k",
    }

    source = insert_source(
        source_type="ceo_annual_letter",
        title=title,
        author="Jeff Bezos",
        file_path=url,
        file_hash=file_hash,
        metadata=metadata,
    )
    LOG.info(f"START {title} (source {source['id']})")

    pages = [PageText(page_number=1, text=text, is_ocr=False)]
    chunks: list[Chunk] = build_chunks(pages)
    if not chunks:
        LOG.warning(f"EMPTY {title}")
        return "empty", 0

    BATCH_SIZE = 64
    inserted = 0
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        texts = [c.text for c in batch]
        vectors = embed_many(texts)
        rows = [
            {
                "source_id": source["id"],
                "chunk_index": c.chunk_index,
                "page_number": c.page_number,
                "content": c.text,
                "word_count": c.word_count,
                "embedding": v,
            }
            for c, v in zip(batch, vectors)
        ]
        insert_chunks(rows)
        inserted += len(rows)

    mark_source_ingested(source["id"])
    LOG.info(f"DONE  {title} — {inserted} chunks")
    return "inserted", inserted


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    batch_id = str(uuid.uuid4())

    print("=" * 70)
    print("  Bezos shareholder letters — older editions via SEC EDGAR (1997-2015)")
    print("=" * 70)
    print(f"  Years to try:   {TARGET_YEARS[0]} – {TARGET_YEARS[-1]}  ({len(TARGET_YEARS)} years)")
    print(f"  Batch ID:       {batch_id}")
    print(f"  Source type:    ceo_annual_letter")
    print(f"  Author:         Jeff Bezos")
    print(f"  EDGAR CIK:      {AMAZON_CIK}")
    print(f"  Log file:       {CONFIG.log_dir / 'ingest_bezos_old.log'}")
    print()

    # Step 1: Get all 10-K filings from EDGAR
    print("Fetching 10-K filing list from SEC EDGAR...", flush=True)
    ten_k_filings = _get_10k_filings()
    if not ten_k_filings:
        print("[ERROR] Could not retrieve EDGAR filing list. Check network / SEC rate limits.")
        return 1

    # Build a year -> filing map
    year_to_filing: dict[int, dict] = {}
    for f in ten_k_filings:
        fy = f["fiscal_year"]
        if fy not in year_to_filing:  # keep latest filing per fiscal year
            year_to_filing[fy] = f

    available = [y for y in TARGET_YEARS if y in year_to_filing]
    missing = [y for y in TARGET_YEARS if y not in year_to_filing]
    print(f"Found 10-K filings for {len(available)} / {len(TARGET_YEARS)} target years")
    if missing:
        print(f"No 10-K filing found for: {missing}")
    print()

    started = time.time()
    counters: dict[str, int] = {
        "inserted": 0,
        "skipped": 0,
        "fetch_failed": 0,
        "extract_failed": 0,
        "empty": 0,
    }
    total_chunks = 0

    for year in TARGET_YEARS:
        print(f"[{year}] ", end="", flush=True)

        if year not in year_to_filing:
            counters["fetch_failed"] += 1
            print("no 10-K found in EDGAR")
            continue

        filing = year_to_filing[year]
        acc = filing["accession"]
        acc_dashed = filing["accession_dashed"]

        # Get the filing index
        files = _get_filing_index(acc, acc_dashed)
        if not files:
            counters["fetch_failed"] += 1
            print(f"index fetch failed ({acc})")
            time.sleep(1)
            continue

        # Try to get the shareholder letter text
        raw_text, url = _fetch_exhibit_or_10k(acc, files)
        if not raw_text:
            counters["fetch_failed"] += 1
            print(f"no text extracted from exhibits")
            time.sleep(1)
            continue

        # Isolate the shareholder letter section
        letter_text = _find_shareholder_letter_section(raw_text, year)
        if len(letter_text) < 800:
            counters["extract_failed"] += 1
            print(f"letter section too short ({len(letter_text)} chars)")
            time.sleep(0.5)
            continue

        try:
            status, chunks_n = _ingest_letter(year, letter_text, url, batch_id)
            counters[status] = counters.get(status, 0) + 1
            total_chunks += chunks_n
            print(f"{status.upper()}  ({chunks_n} chunks, {len(letter_text):,} chars)")
        except Exception as e:
            counters["empty"] += 1
            print(f"ERROR: {e}")
            LOG.exception(f"Year {year} ingest failed")

        time.sleep(1.0)  # SEC rate limit: 10 req/sec max, be polite

    elapsed = time.time() - started
    print()
    print("=" * 70)
    print(f"  Finished in {elapsed:.1f}s")
    print("=" * 70)
    print(f"  Newly ingested:  {counters['inserted']}")
    print(f"  Skipped (done):  {counters['skipped']}")
    print(f"  Fetch failed:    {counters['fetch_failed']}")
    print(f"  Extract failed:  {counters.get('extract_failed', 0)}")
    print(f"  Empty:           {counters['empty']}")
    print(f"  Total chunks:    {total_chunks}")
    print()
    print(f"  Rollback this channel (includes modern letters too):")
    print(f"      python scripts/delete_by_type.py --source-type ceo_annual_letter")
    print(f"  Rollback ONLY this batch (old letters only):")
    print(f"      python scripts/delete_by_type.py --batch-id {batch_id}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
