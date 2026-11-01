#!/usr/bin/env python3
"""magertron.com: rebuild sitemap.xml from the pages that actually exist.

The sitemap lists 5 pages; the site has ~16 (cost, data-reach, compliance,
trust, developers, developer-portal, demos, the comparisons...). Crawlers use
the sitemap to find pages and lastmod to decide what to re-crawl.

What it lists:
  docs/*.html and docs/*/index.html   (top-level pages, and folder pages)
What it skips:
  - fragments: files without <title> (e.g. ask_showcase_block.html)
  - pages marked <meta name="robots" content="noindex">
lastmod = the date of the file's last git commit (today if uncommitted).

Run from ~/GetHub/orchestrator.   Dry run prints the sitemap; --apply writes
docs/sitemap.xml (the old one is kept as docs/sitemap.xml.bak).
"""
import datetime, pathlib, re, subprocess, sys

APPLY = "--apply" in sys.argv
BASE = "https://magertron.com"
DOCS = pathlib.Path("docs")
if not (DOCS / "index.html").is_file():
    sys.exit("ABORT: docs/index.html not found - run from ~/GetHub/orchestrator")

PRIORITY = {"index.html": "1.0", "cost.html": "0.9", "security.html": "0.9",
            "data-reach.html": "0.8", "compliance.html": "0.8", "trust.html": "0.7",
            "privacy.html": "0.3"}
FREQ = {"index.html": "weekly", "privacy.html": "yearly"}
today = datetime.date.today().isoformat()

def lastmod(p):
    try:
        d = subprocess.run(["git", "log", "-1", "--format=%cs", "--", str(p)],
                           capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", str(p)],
                               capture_output=True, text=True).stdout.strip()
        return today if (dirty or not d) else d
    except Exception:
        return today

pages = sorted(DOCS.glob("*.html")) + sorted(DOCS.glob("*/index.html"))
entries, skipped = [], []
for p in pages:
    s = p.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"<title>", s, re.I):
        skipped.append(f"{p} (no <title> - fragment)"); continue
    if re.search(r'<meta[^>]+name="robots"[^>]+noindex', s, re.I):
        skipped.append(f"{p} (noindex)"); continue
    rel = p.relative_to(DOCS).as_posix()
    loc = BASE + "/" if rel == "index.html" else BASE + "/" + (rel[:-len("index.html")] if rel.endswith("/index.html") else rel)
    entries.append((loc, lastmod(p), FREQ.get(rel, "monthly"), PRIORITY.get(rel, "0.6")))

xml = ['<?xml version="1.0" encoding="UTF-8"?>',
       f"<!-- Sitemap for magertron.com - generated {today} by gen-sitemap.py from docs/. Re-run after adding a page. -->",
       '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
for loc, lm, fq, pr in entries:
    xml += ["  <url>", f"    <loc>{loc}</loc>", f"    <lastmod>{lm}</lastmod>",
            f"    <changefreq>{fq}</changefreq>", f"    <priority>{pr}</priority>", "  </url>"]
xml.append("</urlset>")
out = "\n".join(xml) + "\n"

print(f"{len(entries)} pages:")
for loc, lm, _, pr in entries: print(f"  {lm}  {pr}  {loc}")
for s in skipped: print(f"  skip {s}")

if APPLY:
    sm = DOCS / "sitemap.xml"
    if sm.is_file(): sm.with_suffix(".xml.bak").write_text(sm.read_text())
    sm.write_text(out); print(f"applied -> {sm}")
else:
    print("\ndry run - re-run with --apply to write docs/sitemap.xml")
