"""
ingest_curated.py - Ingest curated PDFs from a specific source with proper tagging.

Unlike 02_ingest.py (which is for general books), this script is for *curated
channels*: Howard Marks memos, Damodaran articles, Bezos shareholder letters,
hedge fund letters, etc. Each channel gets a distinct `source_type` so it can
be queried, analyzed, or *cleanly rolled back* independently.

DESIGN GOAL: ROLLBACK SAFETY
  Every record this script writes gets:
    - source_type   = <your --source-type>           (e.g. 'howard_marks_memo')
    - metadata.batch_id = <unique per script run>    (UUID)
    - metadata.channel  = <folder name>              (for human readability)
  To roll back: `python scripts/delete_by_type.py --source-type X`
  To roll back just one batch: include --batch-id in the delete script.

USAGE
  python scripts/ingest_curated.py \\
      --dir data/curated/marks_memos \\
      --source-type howard_marks_memo \\
      --author "Howard Marks"

  Optional:
      --batch-label "2026-Q2-download"   # human note stored in metadata
      --dry-run                          # show what would happen, don't write
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
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
    log_file = CONFIG.log_dir / "ingest_curated.log"
    logger = logging.getLogger("ingest_curated")
    logger.setLevel(logging.INFO)
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


def _ingest_one(
    path: Path,
    *,
    source_type: str,
    author: str | None,
    channel: str,
    batch_id: str,
    batch_label: str | None,
    use_ocr: bool,
    dry_run: bool,
) -> dict:
    """Ingest one PDF with curated tagging. Returns a dict summary."""
    summary = {"status": "error", "chunks": 0, "error": None, "title": path.stem}

    try:
        file_hash = sha256_file(path)
        existing = find_source_by_hash(file_hash)

        if existing and existing.get("ingested_at"):
            summary["status"] = "skipped"
            summary["chunks"] = count_chunks_for_source(existing["id"])
            LOG.info(f"SKIP  {path.name} (already ingested as source {existing['id']})")
            return summary

        # Construct the metadata blob that makes rollback surgical.
        metadata = {
            "original_filename": path.name,
            "channel": channel,                    # e.g. "marks_memos"
            "batch_id": batch_id,                  # UUID of this script run
            "batch_label": batch_label,            # optional human note
            "ingested_via": "ingest_curated.py",
        }

        if dry_run:
            LOG.info(f"DRYRUN {path.name}  (would create source, source_type={source_type})")
            summary["status"] = "dryrun"
            return summary

        if existing and not existing.get("ingested_at"):
            deleted = _delete_incomplete_chunks(existing["id"])
            LOG.info(f"RESUME {path.name} (deleted {deleted} incomplete chunks)")
            source = existing
            summary["status"] = "resumed"
        else:
            source = insert_source(
                source_type=source_type,
                title=path.stem,
                author=author,
                file_path=str(path),
                file_hash=file_hash,
                metadata=metadata,
            )
            LOG.info(f"START {path.name} (source {source['id']}, type={source_type})")
            summary["status"] = "inserted"

        source_id = source["id"]

        pages: list[PageText] = list(extract_pages(path, use_ocr=use_ocr))
        chunks: list[Chunk] = build_chunks(pages)
        if not chunks:
            summary["error"] = "no extractable text"
            LOG.warning(f"EMPTY {path.name} — no text extracted")
            return summary

        # Embed in batches of 64 (same as 02_ingest.py).
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
    parser = argparse.ArgumentParser(description="Ingest curated PDFs with proper source_type tagging.")
    parser.add_argument(
        "--dir",
        type=Path,
        required=True,
        help="Folder of PDFs to ingest (recursive).",
    )
    parser.add_argument(
        "--source-type",
        required=True,
        help="Value for sources.source_type. Use snake_case. "
             "Examples: howard_marks_memo, damodaran_post, bezos_letter, hedge_fund_letter.",
    )
    parser.add_argument(
        "--author",
        default=None,
        help="Default author for all PDFs in this batch (e.g. 'Howard Marks').",
    )
    parser.add_argument(
        "--batch-label",
        default=None,
        help="Optional human-readable label for this run (stored in metadata).",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Enable OCR fallback for scanned pages.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on PDFs to process this run (useful for testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without writing to the DB.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    pdfs = list_pdfs(args.dir)
    if not pdfs:
        print(f"[error] No PDFs found under {args.dir}")
        return 1

    if args.limit:
        pdfs = pdfs[:args.limit]

    batch_id = str(uuid.uuid4())
    channel = args.dir.name  # e.g. "marks_memos"

    print("=" * 70)
    print(f"  Curated ingestion")
    print("=" * 70)
    print(f"  Folder:        {args.dir}")
    print(f"  Source type:   {args.source_type}")
    print(f"  Author:        {args.author or '(none)'}")
    print(f"  Channel:       {channel}")
    print(f"  Batch ID:      {batch_id}")
    print(f"  Batch label:   {args.batch_label or '(none)'}")
    print(f"  PDFs to ingest: {len(pdfs)}")
    print(f"  OCR:           {'ON' if args.ocr else 'OFF'}")
    print(f"  Dry run:       {'YES' if args.dry_run else 'NO'}")
    print(f"  Log file:      {CONFIG.log_dir / 'ingest_curated.log'}")
    print()

    started = time.time()
    counters = {"inserted": 0, "skipped": 0, "resumed": 0, "error": 0, "dryrun": 0}
    total_chunks = 0

    for path in tqdm(pdfs, unit="pdf", desc=f"Ingesting {channel}"):
        summary = _ingest_one(
            path,
            source_type=args.source_type,
            author=args.author,
            channel=channel,
            batch_id=batch_id,
            batch_label=args.batch_label,
            use_ocr=args.ocr,
            dry_run=args.dry_run,
        )
        counters[summary["status"]] = counters.get(summary["status"], 0) + 1
        total_chunks += summary["chunks"]

        if summary["status"] == "error":
            tqdm.write(f"  [ERROR] {path.name}: {summary['error']}")
        elif summary["status"] == "skipped":
            tqdm.write(f"  [SKIP]  {path.name}  ({summary['chunks']} chunks already in DB)")
        elif summary["status"] == "dryrun":
            tqdm.write(f"  [DRY]   {path.name}")
        else:
            label = "DONE" if summary["status"] == "inserted" else "RESUMED"
            tqdm.write(f"  [{label}] {path.name}  ({summary['chunks']} chunks)")

    elapsed = time.time() - started

    print()
    print("=" * 70)
    print(f"  Finished in {elapsed:.1f}s")
    print("=" * 70)
    print(f"  Newly ingested:  {counters['inserted']}")
    print(f"  Resumed:         {counters['resumed']}")
    print(f"  Skipped (done):  {counters['skipped']}")
    print(f"  Dry-run only:    {counters['dryrun']}")
    print(f"  Errors:          {counters['error']}")
    print(f"  Total chunks:    {total_chunks}")
    print()
    print(f"  Rollback this whole source type:")
    print(f"      python scripts/delete_by_type.py --source-type {args.source_type}")
    print()
    print(f"  Rollback ONLY this batch:")
    print(f"      python scripts/delete_by_type.py --batch-id {batch_id}")
    print()

    return 0 if counters["error"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
