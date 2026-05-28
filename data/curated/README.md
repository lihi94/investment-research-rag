# Curated Content Channels

Each subfolder here is a **curated content channel** — a deliberate, named
group of PDFs that get ingested with a distinct `source_type`. This lets you:

- Query them separately (e.g. "find Howard Marks's take on X")
- Roll back a whole channel cleanly if it's not adding value
- Add new channels without touching existing code

## Folder convention

```
data/curated/
├── marks_memos/          → source_type=howard_marks_memo
├── damodaran/            → source_type=damodaran_post   (future)
├── bezos_letters/        → source_type=bezos_letter     (future)
├── hedge_fund_letters/   → source_type=hedge_fund_letter (future)
└── ...
```

## How to ingest a channel

```powershell
python scripts/ingest_curated.py \
    --dir data/curated/marks_memos \
    --source-type howard_marks_memo \
    --author "Howard Marks"
```

Optional flags:
- `--batch-label "2026-Q2"` — human note saved in metadata
- `--dry-run` — preview only, no DB writes
- `--limit 5` — only process first N PDFs (for testing)
- `--ocr` — enable OCR for scanned pages

## How to roll back a channel

```powershell
# Dry run first to see what would be deleted
python scripts/delete_by_type.py --source-type howard_marks_memo

# Actually delete after reviewing
python scripts/delete_by_type.py --source-type howard_marks_memo --yes

# Or just one batch:
python scripts/delete_by_type.py --batch-id <uuid> --yes
```

## Where to get content for each channel

### `marks_memos/` — Howard Marks memos
- **Source:** https://www.oaktreecapital.com/insights/memos
- **Format:** PDF, free download per memo
- **Volume:** ~100 memos (1990-present)
- **Tip:** Memos pre-2008 are gold for understanding cycles

### `damodaran/` — Aswath Damodaran (NYU)
- **Source:** https://aswathdamodaran.blogspot.com (blog) +
  https://pages.stern.nyu.edu/~adamodar/New_Home_Page/papers.html (papers)
- **Format:** Convert blog pages to PDF (browser → Save as PDF), or download papers directly

### `bezos_letters/` — Amazon shareholder letters
- **Source:** https://www.aboutamazon.com/news/company-news/2020-letter-to-shareholders
  (also archived: https://www.aboutamazon.com/about-us/our-leadership-team/jeff-bezos-letters)
- **Format:** Available as PDF for each year 1997-2021

### `hedge_fund_letters/` — Quarterly/annual letters
- **Source:** ValueWalk.com, Reuters, or fund websites directly
- **Funds to look for:** Pershing Square (Ackman), Greenlight (Einhorn),
  Pabrai Funds, Third Point (Loeb), Baupost (Klarman), Sequoia Fund

## Adding a new channel

1. Create a new folder under `data/curated/<channel_name>/`
2. Drop the PDFs in
3. Run `ingest_curated.py` with a new `--source-type`
4. Document the source in this README

## Why a separate script from `02_ingest.py`?

- `02_ingest.py` is for the general PDF library (`source_type=pdf_book`)
- `ingest_curated.py` adds explicit channel tagging for surgical rollback
- Both share the same `src/` modules — no code duplication
