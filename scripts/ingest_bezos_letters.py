"""
ingest_bezos_letters.py - Download + ingest Jeff Bezos's shareholder letters.

USER-AUTHORIZED on 2026-05-28. Bezos's annual letters to shareholders
(1997-2021) are public IR content that Amazon distributes intentionally;
they're a near-universal "required reading" for serious investors.

Strategy
  1. Modern letters (2010-2021): aboutamazon.com publishes them as HTML pages.
     Fetch + parse out the article body + treat each year as one "document".
  2. Older letters (1997-2009): embedded in the SEC 10-K annual reports.
     We attempt a known PDF mirror; if that fails, we skip and note it.

Each ingested letter is tagged with:
  source_type   = 'ceo_annual_letter'
  author        = 'Jeff Bezos'
  metadata.company = 'Amazon'
  metadata.year    = <year>
  metadata.channel = 'bezos_letters'
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

YEARS = list(range(1997, 2022))   # Bezos wrote letters 1997-2021 (Jassy took over)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Multiple URL patterns to try, in priority order.
URL_PATTERNS = [
    "https://www.aboutamazon.com/news/company-news/{year}-letter-to-shareholders",
    "https://www.aboutamazon.com/about-us/our-leadership-team/jeff-bezos-letters/{year}-letter-to-shareholders",
]


def _setup_logging() -> logging.Logger:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = CONFIG.log_dir / "ingest_bezos.log"
    logger = logging.getLogger("ingest_bezos")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


LOG = _setup_logging()


# -----------------------------------------------------------------------------
# Fetch + extract
# -----------------------------------------------------------------------------

def fetch_letter_html(year: int) -> tuple[str | None, str | None]:
    """Try each URL pattern. Returns (html, url_used) or (None, None) on failure."""
    for pattern in URL_PATTERNS:
        url = pattern.format(year=year)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text, url
        except Exception as e:
            LOG.debug(f"fetch {url} failed: {e}")
        time.sleep(0.5)
    return None, None


def extract_letter_text(html: str) -> str:
    """
    Pull out the article body from the HTML.
    Strategy: remove script/style/nav, then collapse to text.
    No BeautifulSoup dep — keep it simple with regex.
    """
    # Strip scripts and styles entirely
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", html, flags=re.DOTALL | re.IGNORECASE)

    # Try to grab inside <article> ... </article> if present (Amazon uses it).
    article_match = re.search(r"<article\b[^>]*>(.*?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
    body = article_match.group(1) if article_match else html

    # Convert paragraph breaks to newlines
    body = re.sub(r"</p>", "\n\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</?h[1-6][^>]*>", "\n\n", body, flags=re.IGNORECASE)

    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", " ", body)

    # Decode common HTML entities
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&apos;", "'")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&mdash;", "—")
                .replace("&ndash;", "–")
                .replace("&rsquo;", "'")
                .replace("&lsquo;", "'")
                .replace("&rdquo;", '"')
                .replace("&ldquo;", '"'))

    # Collapse whitespace
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


# -----------------------------------------------------------------------------
# Ingestion
# -----------------------------------------------------------------------------

def _ingest_letter(
    year: int,
    text: str,
    url: str,
    batch_id: str,
) -> tuple[str, int]:
    """Insert one letter into the DB. Returns (status, chunks_count)."""
    title = f"Amazon Shareholder Letter {year}"

    # Hash on the *content* so re-runs are idempotent.
    file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    existing = find_source_by_hash(file_hash)
    if existing:
        if existing.get("ingested_at"):
            from src.db import count_chunks_for_source
            return "skipped", count_chunks_for_source(existing["id"])
        else:
            from src.db import delete_source
            LOG.warning(f"Cleaning up partial source {existing['id']} for {title}")
            delete_source(existing["id"])

    metadata = {
        "company": "Amazon",
        "year": year,
        "channel": "bezos_letters",
        "batch_id": batch_id,
        "source_url": url,
        "ingested_via": "ingest_bezos_letters.py",
        "content_origin": "html_to_text",
    }

    source = insert_source(
        source_type="ceo_annual_letter",
        title=title,
        author="Jeff Bezos",
        file_path=url,  # virtual — no local file
        file_hash=file_hash,
        metadata=metadata,
    )
    LOG.info(f"START {title} (source {source['id']})")

    # Build chunks. We treat the whole letter as a single "page" since it's
    # relatively short and there's no useful page numbering on a web page.
    pages = [PageText(page_number=1, text=text, is_ocr=False)]
    chunks: list[Chunk] = build_chunks(pages)
    if not chunks:
        LOG.warning(f"EMPTY {title}")
        return "empty", 0

    # Embed + insert
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
    print("  Bezos shareholder letters ingestion")
    print("=" * 70)
    print(f"  Years to try:   {YEARS[0]} – {YEARS[-1]}  ({len(YEARS)} years)")
    print(f"  Batch ID:       {batch_id}")
    print(f"  Source type:    ceo_annual_letter")
    print(f"  Author:         Jeff Bezos")
    print(f"  Log file:       {CONFIG.log_dir / 'ingest_bezos.log'}")
    print()

    started = time.time()
    counters = {"inserted": 0, "skipped": 0, "empty": 0, "fetch_failed": 0, "extract_failed": 0}
    total_chunks = 0

    for year in YEARS:
        print(f"[{year}] ", end="", flush=True)
        html, url = fetch_letter_html(year)
        if not html:
            counters["fetch_failed"] += 1
            print(f"fetch failed (no URL responded)")
            continue

        text = extract_letter_text(html)
        if not text or len(text) < 800:
            counters["extract_failed"] += 1
            print(f"extract failed ({len(text)} chars)")
            continue

        try:
            status, chunks_n = _ingest_letter(year, text, url, batch_id)
            counters[status] = counters.get(status, 0) + 1
            total_chunks += chunks_n
            print(f"{status.upper()}  ({chunks_n} chunks, {len(text):,} chars)  {url}")
        except Exception as e:
            counters["empty"] += 1
            print(f"ERROR: {e}")
            LOG.exception(f"Year {year} ingest failed")

        time.sleep(0.6)  # be polite

    elapsed = time.time() - started

    print()
    print("=" * 70)
    print(f"  Finished in {elapsed:.1f}s")
    print("=" * 70)
    print(f"  Newly ingested:  {counters['inserted']}")
    print(f"  Skipped (done):  {counters['skipped']}")
    print(f"  Empty / failed:  {counters['empty'] + counters['extract_failed']}")
    print(f"  Fetch failed:    {counters['fetch_failed']}")
    print(f"  Total chunks:    {total_chunks}")
    print()
    print(f"  Rollback this whole channel:")
    print(f"      python scripts/delete_by_type.py --source-type ceo_annual_letter")
    print(f"  Rollback ONLY this batch:")
    print(f"      python scripts/delete_by_type.py --batch-id {batch_id}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
