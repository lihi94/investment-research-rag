"""
ingest_dimon_letters.py - Download + ingest Jamie Dimon's JPMorgan Chase shareholder letters.

USER-AUTHORIZED on 2026-05-28. JPMorgan Chase annual letters are public IR
documents published for all shareholders on jpmorganchase.com.

Strategy
  JPMorgan publishes annual reports with shareholder letters as PDFs on their
  IR site. We try multiple known URL patterns:

  Pattern A (recent, 2016+):
    https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/annualreport-<year>.pdf

  Pattern B (alternative path):
    https://www.jpmorganchase.com/ir/annual-report/<year>

  Pattern C (older letters, standalone):
    https://reports.jpmorganchase.com/<year>/ar/

  Since web scraping IR pages is fragile, we also try a direct PDF approach
  for the standalone shareholder letter PDFs which JPMorgan has published.

Tagging:
  source_type   = 'dimon_annual_letter'
  author        = 'Jamie Dimon'
  metadata.company  = 'JPMorgan Chase'
  metadata.year     = <year>
  metadata.channel  = 'dimon_letters'
  metadata.batch_id = <UUID for this run>

Rollback:
  python scripts/delete_by_type.py --source-type dimon_annual_letter
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import tempfile
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
from src.pdf_utils import extract_pages, PageText


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

YEARS = list(range(2006, 2025))   # Dimon became CEO in Dec 2005; first full-year letter 2006

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.jpmorganchase.com/",
}

# URL patterns to try for each year, in priority order.
# JPMorgan's URL structure has changed over the years.
def _get_urls_for_year(year: int) -> list[str]:
    """Return list of candidate PDF URLs for a given year."""
    return [
        # Modern annual report PDFs (full annual report, includes shareholder letter)
        f"https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/annualreport-{year}.pdf",
        # Alternate dam path used in some years
        f"https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/ceo-letter-to-shareholders-{year}.pdf",
        # Standalone shareholder letter PDFs (some years published separately)
        f"https://www.jpmorganchase.com/content/dam/jpmc/jpmorgan-chase-and-co/investor-relations/documents/shareholders-letter-{year}.pdf",
        # Older path pattern
        f"https://www.jpmorganchase.com/corporate/investor-relations/document/annualreport-{year}.pdf",
    ]


def _setup_logging() -> logging.Logger:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = CONFIG.log_dir / "ingest_dimon.log"
    logger = logging.getLogger("ingest_dimon")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


LOG = _setup_logging()


# -----------------------------------------------------------------------------
# Fetch
# -----------------------------------------------------------------------------

def fetch_letter_pdf(year: int) -> tuple[bytes | None, str | None]:
    """Try all URL patterns. Returns (pdf_bytes, url_used) or (None, None)."""
    for url in _get_urls_for_year(year):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                LOG.info(f"Fetched {year} from {url} ({len(r.content)//1024} KB)")
                return r.content, url
            LOG.debug(f"  {url} → {r.status_code}, not PDF")
        except Exception as e:
            LOG.debug(f"  {url} → error: {e}")
        time.sleep(0.3)
    return None, None


def _try_scrape_ir_page(year: int) -> tuple[bytes | None, str | None]:
    """
    Last resort: scrape the JPMorgan IR landing page for the given year
    and look for a PDF link to the annual report or shareholder letter.
    """
    landing_urls = [
        f"https://www.jpmorganchase.com/ir/annual-report/{year}",
        f"https://reports.jpmorganchase.com/{year}/ar/",
    ]
    for landing in landing_urls:
        try:
            r = requests.get(landing, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                continue
            # Look for .pdf hrefs in the HTML
            pdf_links = re.findall(
                r'href=["\']([^"\']*(?:annualreport|annual-report|shareholder|ceo-letter)[^"\']*\.pdf)["\']',
                r.text,
                re.IGNORECASE,
            )
            for link in pdf_links:
                # Make absolute
                if link.startswith("http"):
                    pdf_url = link
                else:
                    pdf_url = "https://www.jpmorganchase.com" + link
                try:
                    pr = requests.get(pdf_url, headers=HEADERS, timeout=60)
                    if pr.status_code == 200 and pr.content.startswith(b"%PDF"):
                        return pr.content, pdf_url
                except Exception:
                    continue
        except Exception as e:
            LOG.debug(f"Scrape {landing} failed: {e}")
        time.sleep(0.5)
    return None, None


# -----------------------------------------------------------------------------
# Ingestion
# -----------------------------------------------------------------------------

def _ingest_letter(
    year: int,
    pdf_bytes: bytes,
    url: str,
    batch_id: str,
) -> tuple[str, int]:
    """Write to temp file, parse, chunk, embed, insert. Returns (status, chunk_count)."""
    title = f"JPMorgan Chase Annual Letter to Shareholders {year}"

    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
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
        "company": "JPMorgan Chase",
        "year": year,
        "channel": "dimon_letters",
        "batch_id": batch_id,
        "source_url": url,
        "ingested_via": "ingest_dimon_letters.py",
        "content_origin": "pdf",
    }

    source = insert_source(
        source_type="dimon_annual_letter",
        title=title,
        author="Jamie Dimon",
        file_path=url,
        file_hash=file_hash,
        metadata=metadata,
    )
    LOG.info(f"START {title} (source {source['id']})")

    # extract_pages() needs a Path, so write bytes to a temp file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)

    try:
        pages: list[PageText] = list(extract_pages(tmp_path, use_ocr=False))
    except Exception as e:
        LOG.warning(f"PDF parse failed for {title}: {e}")
        tmp_path.unlink(missing_ok=True)
        return "parse_failed", 0
    finally:
        tmp_path.unlink(missing_ok=True)

    pages = [p for p in pages if p.text.strip()]
    if not pages:
        LOG.warning(f"No text extracted for {title}")
        return "empty", 0

    chunks: list[Chunk] = build_chunks(pages)
    if not chunks:
        LOG.warning(f"No chunks for {title}")
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
    print("  JPMorgan Chase / Jamie Dimon shareholder letters ingestion")
    print("=" * 70)
    print(f"  Years to try:   {YEARS[0]} – {YEARS[-1]}  ({len(YEARS)} years)")
    print(f"  Batch ID:       {batch_id}")
    print(f"  Source type:    dimon_annual_letter")
    print(f"  Author:         Jamie Dimon")
    print(f"  Log file:       {CONFIG.log_dir / 'ingest_dimon.log'}")
    print()
    print("  NOTE: JPMorgan's IR site structure varies by year. We try multiple")
    print("  URL patterns; years that fail fetch can be added manually later.")
    print()

    started = time.time()
    counters: dict[str, int] = {
        "inserted": 0,
        "skipped": 0,
        "fetch_failed": 0,
        "parse_failed": 0,
        "empty": 0,
    }
    total_chunks = 0

    for year in YEARS:
        print(f"[{year}] ", end="", flush=True)

        pdf_bytes, url = fetch_letter_pdf(year)

        # If direct patterns didn't work, try scraping the IR page
        if not pdf_bytes:
            print(f"direct failed, trying IR page...", end=" ", flush=True)
            pdf_bytes, url = _try_scrape_ir_page(year)

        if not pdf_bytes:
            counters["fetch_failed"] += 1
            print(f"fetch failed (all patterns tried)")
            time.sleep(0.5)
            continue

        size_kb = len(pdf_bytes) / 1024
        try:
            status, chunks_n = _ingest_letter(year, pdf_bytes, url, batch_id)
            counters[status] = counters.get(status, 0) + 1
            total_chunks += chunks_n
            print(f"{status.upper()}  ({chunks_n} chunks, {size_kb:.0f} KB)")
        except Exception as e:
            counters["empty"] += 1
            print(f"ERROR: {e}")
            LOG.exception(f"Year {year} ingest failed")

        time.sleep(0.8)

    elapsed = time.time() - started
    print()
    print("=" * 70)
    print(f"  Finished in {elapsed:.1f}s")
    print("=" * 70)
    print(f"  Newly ingested:  {counters['inserted']}")
    print(f"  Skipped (done):  {counters['skipped']}")
    print(f"  Fetch failed:    {counters['fetch_failed']}")
    print(f"  Parse/empty:     {counters.get('parse_failed', 0) + counters['empty']}")
    print(f"  Total chunks:    {total_chunks}")
    print()
    if counters["fetch_failed"] > 0:
        print("  Some years failed to fetch. To add them manually:")
        print("    1. Download the PDF from jpmorganchase.com/ir")
        print(f"    2. Place in data/curated/dimon_letters/<year>.pdf")
        print(f"    3. Run: python scripts/ingest_curated.py \\")
        print(f"         --dir data/curated/dimon_letters \\")
        print(f"         --source-type dimon_annual_letter \\")
        print(f"         --author 'Jamie Dimon'")
        print()
    print(f"  Rollback this channel:")
    print(f"      python scripts/delete_by_type.py --source-type dimon_annual_letter")
    print(f"  Rollback ONLY this batch:")
    print(f"      python scripts/delete_by_type.py --batch-id {batch_id}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
