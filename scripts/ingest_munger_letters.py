"""
ingest_munger_letters.py - Download + ingest Berkshire Hathaway annual letters.

USER-AUTHORIZED on 2026-05-28. Public IR documents distributed by Berkshire
Hathaway, freely available at berkshirehathaway.com/letters/.

What we ingest
  Berkshire annual letters (1997-2023) from:
    https://www.berkshirehathaway.com/letters/<year>ltr.pdf

  These are Warren Buffett's annual letters but Berkshire's letters are
  universally studied as Buffett+Munger partnership documents; Charlie
  Munger's influence and commentary permeate them through 2023.

Tagging:
  source_type   = 'berkshire_annual_letter'
  author        = 'Warren Buffett'
  metadata.company  = 'Berkshire Hathaway'
  metadata.year     = <year>
  metadata.channel  = 'berkshire_letters'
  metadata.batch_id = <UUID for this run>

Rollback:
  python scripts/delete_by_type.py --source-type berkshire_annual_letter
"""

from __future__ import annotations

import hashlib
import logging
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

YEARS = list(range(1997, 2024))  # 1997-2023

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PDF_URL_TEMPLATE = "https://www.berkshirehathaway.com/letters/{year}ltr.pdf"


def _setup_logging() -> logging.Logger:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = CONFIG.log_dir / "ingest_berkshire.log"
    logger = logging.getLogger("ingest_berkshire")
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

def fetch_letter_pdf(year: int) -> bytes | None:
    """Download the Berkshire annual letter PDF. Returns raw bytes or None."""
    url = PDF_URL_TEMPLATE.format(year=year)
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        if r.status_code == 200 and r.content.startswith(b"%PDF"):
            return r.content
        LOG.debug(f"fetch {url} → status {r.status_code}, size {len(r.content)}")
    except Exception as e:
        LOG.debug(f"fetch {url} failed: {e}")
    return None


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
    title = f"Berkshire Hathaway Annual Letter {year}"

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
        "company": "Berkshire Hathaway",
        "year": year,
        "channel": "berkshire_letters",
        "batch_id": batch_id,
        "source_url": url,
        "ingested_via": "ingest_munger_letters.py",
        "content_origin": "pdf",
        "note": "Buffett annual letter — includes Munger partnership commentary",
    }

    source = insert_source(
        source_type="berkshire_annual_letter",
        title=title,
        author="Warren Buffett",
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

    # Filter out blank pages
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
    print("  Berkshire Hathaway annual letters ingestion")
    print("  (Buffett + Munger partnership documents, 1997-2023)")
    print("=" * 70)
    print(f"  Years to try:   {YEARS[0]} – {YEARS[-1]}  ({len(YEARS)} years)")
    print(f"  Batch ID:       {batch_id}")
    print(f"  Source type:    berkshire_annual_letter")
    print(f"  Author:         Warren Buffett")
    print(f"  Log file:       {CONFIG.log_dir / 'ingest_berkshire.log'}")
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
        pdf_bytes = fetch_letter_pdf(year)
        if not pdf_bytes:
            counters["fetch_failed"] += 1
            print(f"fetch failed")
            time.sleep(0.5)
            continue

        url = PDF_URL_TEMPLATE.format(year=year)
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

        time.sleep(0.8)  # be polite to berkshirehathaway.com

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
    print(f"  Rollback this channel:")
    print(f"      python scripts/delete_by_type.py --source-type berkshire_annual_letter")
    print(f"  Rollback ONLY this batch:")
    print(f"      python scripts/delete_by_type.py --batch-id {batch_id}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
