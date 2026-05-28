"""
delete_by_type.py - Clean rollback of curated ingestions.

Designed as the safety net for `ingest_curated.py`. If a content channel turns
out to be noisy / not useful / hurting answer quality, this removes it cleanly.

USAGE
  # Preview what will be deleted (DEFAULT — does NOT delete anything):
  python scripts/delete_by_type.py --source-type howard_marks_memo

  # Same but only for one specific batch:
  python scripts/delete_by_type.py --batch-id 1234abcd-...

  # Actually delete (requires --yes):
  python scripts/delete_by_type.py --source-type howard_marks_memo --yes

SAFETY
  - By default this is a DRY RUN. You'll see exactly what would be removed.
  - Requires explicit --yes flag to actually delete.
  - Refuses to delete `source_type='pdf_book'` (our main book library) unless
    --force-pdf-book is passed. This protects the bulk of your data.
  - Uses Supabase CASCADE: deleting a source automatically removes its chunks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import SB


PROTECTED_SOURCE_TYPES = {"pdf_book"}  # require --force-pdf-book to delete these


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Roll back a curated ingestion channel by source_type or batch_id.",
    )
    parser.add_argument(
        "--source-type",
        help="Delete all sources with this source_type (e.g. 'howard_marks_memo').",
    )
    parser.add_argument(
        "--batch-id",
        help="Delete only sources tagged with this batch_id in their metadata.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform the delete (default is dry-run).",
    )
    parser.add_argument(
        "--force-pdf-book",
        action="store_true",
        help="Required to delete source_type=pdf_book (protects your main library).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not args.source_type and not args.batch_id:
        print("[error] You must specify either --source-type or --batch-id.")
        return 1

    # Build the query
    query = SB.table("sources").select("id, title, source_type, author, ingested_at, metadata")

    if args.source_type:
        if args.source_type in PROTECTED_SOURCE_TYPES and not args.force_pdf_book:
            print(f"[error] Refusing to operate on protected source_type '{args.source_type}'.")
            print(f"        If you really mean it, re-run with --force-pdf-book.")
            return 1
        query = query.eq("source_type", args.source_type)

    if args.batch_id:
        # batch_id is inside metadata JSONB - use ->>
        query = query.eq("metadata->>batch_id", args.batch_id)

    result = query.execute()
    rows = result.data or []

    if not rows:
        print("[info] No matching sources found. Nothing to delete.")
        return 0

    # Show what we found
    print("=" * 70)
    print(f"  Sources that match the filter ({len(rows)} found):")
    print("=" * 70)
    total_chunks = 0
    for r in rows:
        cc = SB.table("chunks").select("id", count="exact").eq("source_id", r["id"]).execute()
        chunks_n = cc.count or 0
        total_chunks += chunks_n
        batch = (r.get("metadata") or {}).get("batch_id", "")[:8] or "(none)"
        print(f"  - [{r['source_type']}] {r['title'][:60]:60} "
              f"chunks={chunks_n:>5}  batch={batch}")

    print()
    print(f"  Total sources: {len(rows)}")
    print(f"  Total chunks:  {total_chunks}")
    print()

    if not args.yes:
        print("=" * 70)
        print("  DRY RUN — nothing was deleted.")
        print("=" * 70)
        print("  To actually delete the above, re-run with --yes")
        return 0

    # Actually delete. CASCADE handles chunks.
    print("=" * 70)
    print(f"  DELETING {len(rows)} sources (and {total_chunks} chunks via CASCADE)...")
    print("=" * 70)
    ids_to_delete = [r["id"] for r in rows]
    # Supabase doesn't support .in_() with delete cleanly across all versions, so loop.
    deleted = 0
    for sid in ids_to_delete:
        SB.table("sources").delete().eq("id", sid).execute()
        deleted += 1
    print(f"  Deleted {deleted} sources. Their chunks were removed via CASCADE.")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
