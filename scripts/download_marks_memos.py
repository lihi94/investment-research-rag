"""
download_marks_memos.py - Download Howard Marks memos from Oaktree's public memos page.

USER-AUTHORIZED: explicitly approved by the user on 2026-05-28.
Howard Marks memos are public, free, and intended for wide distribution.

How it works
  1. Fetch https://www.oaktreecapital.com/insights/memos
  2. Extract all /insights/memo/<slug> URLs
  3. For each slug, download https://www.oaktreecapital.com/docs/default-source/memos/<slug>.pdf
  4. Save to data/curated/marks_memos/<slug>.pdf

This URL pattern covers modern memos (~2018-present, the most influential
era for Marks's writing on cycles, AI, and risk). Older memos use a
different URL scheme and aren't fetched here.

USAGE
  python scripts/download_marks_memos.py             # download all 35 modern memos
  python scripts/download_marks_memos.py --limit 20  # cap to first 20
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TARGET_DIR = ROOT / "data" / "curated" / "marks_memos"
INDEX_URL = "https://www.oaktreecapital.com/insights/memos"
PDF_URL_TEMPLATE = "https://www.oaktreecapital.com/docs/default-source/memos/{slug}.pdf"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_slugs() -> list[str]:
    r = requests.get(INDEX_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    slugs = sorted(set(re.findall(r"/insights/memo/([a-z0-9\-]+)", r.text)))
    return slugs


def download_pdf(slug: str, dest: Path, retries: int = 3) -> str:
    """Returns one of: 'ok', 'skipped', '404', 'failed'."""
    if dest.exists() and dest.stat().st_size > 1000:
        return "skipped"

    url = PDF_URL_TEMPLATE.format(slug=slug)
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            if r.status_code == 404:
                return "404"
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                return "failed"
            dest.write_bytes(r.content)
            return "ok"
        except Exception:
            time.sleep(2 ** attempt)
    return "failed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Cap on memos to download.")
    args = parser.parse_args()

    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching memo list from {INDEX_URL}")
    try:
        slugs = fetch_slugs()
    except Exception as e:
        print(f"[error] Could not fetch index: {e}")
        return 1

    print(f"Found {len(slugs)} memo slugs on the page.")
    if args.limit:
        slugs = slugs[:args.limit]
        print(f"Limiting to first {len(slugs)} memos.")

    print()
    print(f"Downloading -> {TARGET_DIR}")
    print("=" * 70)

    stats = {"ok": 0, "skipped": 0, "404": 0, "failed": 0}
    for i, slug in enumerate(slugs, 1):
        dest = TARGET_DIR / f"{slug}.pdf"
        status = download_pdf(slug, dest)
        stats[status] = stats.get(status, 0) + 1

        if status == "ok":
            size_kb = dest.stat().st_size / 1024
            print(f"  [{i:2}/{len(slugs)}] [ok]      {slug}  ({size_kb:.0f} KB)")
        elif status == "skipped":
            print(f"  [{i:2}/{len(slugs)}] [skip]    {slug}  (already on disk)")
        elif status == "404":
            print(f"  [{i:2}/{len(slugs)}] [404]     {slug}  (no PDF at expected URL)")
        else:
            print(f"  [{i:2}/{len(slugs)}] [failed]  {slug}")
        time.sleep(0.6)  # polite

    print("=" * 70)
    print(f"  OK:      {stats['ok']}")
    print(f"  Skipped: {stats['skipped']} (already downloaded)")
    print(f"  404:     {stats['404']} (PDF not at expected URL)")
    print(f"  Failed:  {stats['failed']}")
    print()
    print(f"Total in folder now: {len(list(TARGET_DIR.glob('*.pdf')))} PDFs")
    print()
    print("Next step:")
    print("  python scripts/ingest_curated.py \\")
    print("    --dir data/curated/marks_memos \\")
    print("    --source-type howard_marks_memo \\")
    print("    --author 'Howard Marks'")

    return 0 if (stats["ok"] + stats["skipped"]) else 2


if __name__ == "__main__":
    sys.exit(main())
