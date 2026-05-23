"""
02_ingest.py - Ingest PDFs into Supabase (resumable).

For each PDF in PDF_DIR:
  1. Compute SHA256 hash.
  2. Check the database: if a source with this hash exists and is fully
     ingested (ingested_at IS NOT NULL), skip it.
  3. If a source exists but was never marked ingested, clear its chunks and
     re-do it (the previous run crashed midway).
  4. Otherwise create a new source record.
  5. Extract pages, chunk, embed in batches, insert.
  6. Mark the source as ingested.

The script is safe to interrupt and re-run — file_hash deduplication ensures
already-done books are skipped.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

# Make `src` importable when running from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tqdm import tqdm

from src.config import CONFIG
from src.chunker import Chunk, build_chunks
from src.db import (
    SB,
    count_chunks_for_source,
    find_source_by_hash,
    insert_chunks,
    insert_source,
    mark_source_ingested,
)
from src.embeddings import embed_many
from src.pdf_utils import PageText, extract_pages, list_pdfs, sha256_file


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    CONFIG.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = CONFIG.log_dir / "ingest.log"
    logger = logging.getLogger("ingest")
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers if the module is re-imported.
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


LOG = _setup_logging()


# -----------------------------------------------------------------------------
# Per-file ingestion
# -----------------------------------------------------------------------------

def _delete_incomplete_chunks(source_id: str) -> int:
    """Remove any chunks left over from a failed previous run."""
    res = SB.table("chunks").delete().eq("source_id", source_id).execute()
    return len(res.data or [])


def _ingest_one(path: Path, use_ocr: bool) -> dict:
    """
    Ingest one PDF. Returns a dict summary:
      {status: 'inserted'|'skipped'|'resumed'|'error', chunks: int, error: str|None}
    """
    summary = {"status": "error", "chunks": 0, "error": None, "title": path.stem}

    try:
        # Step 1: hash + dedup check
        file_hash = sha256_file(path)
        existing = find_source_by_hash(file_hash)

        if existing and existing.get("ingested_at"):
            summary["status"] = "skipped"
            summary["chunks"] = count_chunks_for_source(existing["id"])
            LOG.info(f"SKIP  {path.name} (already ingested as source {existing['id']})")
            return summary

        if existing and not existing.get("ingested_at"):
            # Previous run crashed mid-ingest. Clean its chunks and re-do.
            deleted = _delete_incomplete_chunks(existing["id"])
            LOG.info(f"RESUME {path.name} (deleted {deleted} incomplete chunks)")
            source = existing
            summary["status"] = "resumed"
        else:
            # Fresh ingest
            source = insert_source(
                source_type="pdf_book",
                title=path.stem,
                author=None,                       # we don't try to parse authors from PDFs
                file_path=str(path),
                file_hash=file_hash,
                metadata={"original_filename": path.name},
            )
            LOG.info(f"START {path.name} (source {source['id']})")
            summary["status"] = "inserted"

        source_id = source["id"]

        # Step 2: extract pages
        pages: list[PageText] = list(extract_pages(path, use_ocr=use_ocr))

        # Step 3: chunk
        chunks: list[Chunk] = build_chunks(pages)
        if not chunks:
            summary["error"] = "no extractable text"
            LOG.warning(f"EMPTY {path.name} — no text extracted")
            return summary

        # Step 4: embed in batches and insert as we go.
        # Why per-batch and not all-at-once: if the script crashes mid-way,
        # we don't lose the embedded chunks already inserted.
        BATCH_SIZE = 64
        inserted = 0
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            texts = [c.text for c in batch]
            vectors = embed_many(texts)

            rows = [
                {
                    "source_id": source_id,
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

        # Step 5: mark done
        mark_source_ingested(source_id)
        summary["chunks"] = inserted
        LOG.info(f"DONE  {path.name} — {inserted} chunks")

    except Exception as e:
        summary["error"] = str(e)
        LOG.exception(f"ERROR {path.name}: {e}")

    return summary


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest PDFs into the RAG knowledge base.")
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Enable OCR for scanned pages. Requires Tesseract + Poppler. "
             "Default off (faster, no OCR deps needed).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of PDFs to ingest in this run (for testing).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    pdfs = list_pdfs(CONFIG.pdf_dir)
    if not pdfs:
        print(f"[error] No PDFs in {CONFIG.pdf_dir}")
        return 1

    if args.limit:
        pdfs = pdfs[:args.limit]

    print(f"[info] Ingesting {len(pdfs)} PDF(s) from {CONFIG.pdf_dir}")
    print(f"[info] OCR: {'ON' if args.ocr else 'OFF'}")
    print(f"[info] Log file: {CONFIG.log_dir / 'ingest.log'}")
    print()

    started = time.time()
    counters = {"inserted": 0, "skipped": 0, "resumed": 0, "error": 0}
    total_chunks = 0

    for path in tqdm(pdfs, unit="pdf", desc="Ingesting"):
        summary = _ingest_one(path, use_ocr=args.ocr)
        counters[summary["status"]] = counters.get(summary["status"], 0) + 1
        total_chunks += summary["chunks"]

        # Inline summary per file (visible above the progress bar after it advances)
        if summary["status"] == "error":
            tqdm.write(f"  [ERROR] {path.name}: {summary['error']}")
        elif summary["status"] == "skipped":
            tqdm.write(f"  [SKIP]  {path.name}  ({summary['chunks']} chunks already in DB)")
        else:
            label = "DONE" if summary["status"] == "inserted" else "RESUMED"
            tqdm.write(f"  [{label}] {path.name}  ({summary['chunks']} chunks)")

    elapsed = time.time() - started

    print()
    print("=" * 70)
    print(f"  Ingestion finished in {elapsed:.1f}s")
    print("=" * 70)
    print(f"  Newly ingested:  {counters['inserted']}")
    print(f"  Resumed:         {counters['resumed']}")
    print(f"  Skipped (done):  {counters['skipped']}")
    print(f"  Errors:          {counters['error']}")
    print(f"  Total chunks in this run: {total_chunks}")
    print()
    print(f"  Next step:  python scripts/03_query.py")
    print()

    return 0 if counters["error"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
