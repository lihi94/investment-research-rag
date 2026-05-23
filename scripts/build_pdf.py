"""
build_pdf.py - Convert PROJECT_GUIDE.md to a nicely-formatted PDF.

Strategy: markdown -> HTML (with RTL CSS) -> PDF via headless Edge.
Why Edge: it renders Hebrew RTL perfectly with system fonts (Segoe UI),
no extra dependencies beyond a browser that's already on every Windows machine.

Run: python scripts/build_pdf.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import markdown


ROOT = Path(__file__).resolve().parent.parent
MD_FILE = ROOT / "PROJECT_GUIDE.md"
PDF_FILE = ROOT / "PROJECT_GUIDE.pdf"


CSS = """
@page {
  size: A4;
  margin: 1.8cm 1.5cm;
}
body {
  font-family: "Segoe UI", "Arial Hebrew", Arial, sans-serif;
  direction: rtl;
  font-size: 11pt;
  line-height: 1.65;
  color: #222;
  max-width: 100%;
}
h1 {
  color: #1a1a1a;
  border-bottom: 2.5px solid #4a90e2;
  padding-bottom: 8px;
  margin-top: 32px;
  font-size: 22pt;
  page-break-after: avoid;
}
h2 {
  color: #2a2a2a;
  border-bottom: 1px solid #ddd;
  padding-bottom: 5px;
  margin-top: 26px;
  font-size: 17pt;
  page-break-after: avoid;
}
h3 {
  color: #3a3a3a;
  margin-top: 20px;
  font-size: 13pt;
  page-break-after: avoid;
}
h4 {
  color: #4a4a4a;
  margin-top: 16px;
  font-size: 11.5pt;
  page-break-after: avoid;
}
p { margin: 8px 0; }
table {
  border-collapse: collapse;
  width: 100%;
  margin: 14px 0;
  direction: rtl;
  page-break-inside: avoid;
}
th, td {
  border: 1px solid #ccc;
  padding: 7px 11px;
  text-align: right;
  vertical-align: top;
  font-size: 10.5pt;
}
th {
  background: #eef3f8;
  font-weight: 600;
  color: #1a1a1a;
}
tr:nth-child(even) td { background: #fafbfc; }
code {
  background: #f4f4f4;
  padding: 1.5px 5px;
  border-radius: 3px;
  font-family: Consolas, "Courier New", monospace;
  font-size: 9.8pt;
  direction: ltr;
  unicode-bidi: embed;
  color: #c7254e;
}
pre {
  background: #f6f8fa;
  padding: 12px 16px;
  border-radius: 5px;
  border: 1px solid #e1e4e8;
  direction: ltr;
  text-align: left;
  overflow-x: auto;
  page-break-inside: avoid;
  font-size: 9.5pt;
  line-height: 1.45;
}
pre code {
  background: none;
  padding: 0;
  color: #24292e;
}
blockquote {
  border-right: 4px solid #4a90e2;
  padding: 4px 14px;
  color: #555;
  background: #f8fafc;
  margin: 12px 0;
  border-radius: 0 4px 4px 0;
}
hr {
  border: 0;
  border-top: 1px solid #ddd;
  margin: 28px 0;
}
a {
  color: #2563eb;
  text-decoration: none;
}
a:hover { text-decoration: underline; }
ul, ol {
  padding-right: 28px;
  padding-left: 0;
  margin: 8px 0;
}
li { margin: 4px 0; }
.cover {
  text-align: center;
  padding: 60px 0 30px;
  border-bottom: 1px solid #e1e4e8;
  margin-bottom: 24px;
}
.cover h1 {
  font-size: 28pt;
  border: none;
  margin: 0 0 8px;
  padding: 0;
}
.cover .subtitle {
  color: #666;
  font-size: 13pt;
}
.cover .date {
  color: #888;
  margin-top: 18px;
  font-size: 10.5pt;
}
"""


def build_html(md_text: str) -> str:
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists"],
    )
    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<title>investment-research-rag — מדריך פרויקט</title>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def find_edge() -> Path:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise RuntimeError(
        "Microsoft Edge not found. Edge is required to render the PDF.\n"
        f"Looked in: {candidates}"
    )


def main() -> int:
    if not MD_FILE.exists():
        print(f"[error] Source not found: {MD_FILE}")
        return 1

    print(f"[info] Reading {MD_FILE.name}")
    md_text = MD_FILE.read_text(encoding="utf-8")

    print("[info] Converting markdown -> HTML")
    html = build_html(md_text)

    # Write HTML to a temp file (Edge needs a URL, not stdin).
    tmp_html = Path(tempfile.gettempdir()) / "project_guide_tmp.html"
    tmp_html.write_text(html, encoding="utf-8")
    print(f"[info] Temporary HTML: {tmp_html}")

    print("[info] Locating Microsoft Edge")
    edge = find_edge()
    print(f"[info] Edge: {edge}")

    print("[info] Rendering PDF via headless Edge (this takes a few seconds)...")
    # Remove old PDF if exists (Edge sometimes appends rather than overwrites cleanly)
    if PDF_FILE.exists():
        PDF_FILE.unlink()

    result = subprocess.run(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            f"--print-to-pdf={PDF_FILE}",
            "--no-pdf-header-footer",
            tmp_html.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if not PDF_FILE.exists():
        print("[error] PDF was not created")
        print(f"  stdout: {result.stdout}")
        print(f"  stderr: {result.stderr}")
        return 2

    size_kb = PDF_FILE.stat().st_size / 1024
    print()
    print("=" * 60)
    print(f"  PDF created: {PDF_FILE.name}")
    print(f"  Size:        {size_kb:.1f} KB")
    print(f"  Path:        {PDF_FILE}")
    print("=" * 60)

    # Cleanup
    try:
        tmp_html.unlink()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
