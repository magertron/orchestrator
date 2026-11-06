#!/usr/bin/env python3
"""Solutions pages: move "The risk" section down a bit, by adding 48px of space above it.

  python3 patch-site-risk-lower.py crm            solutions-crm.html
  python3 patch-site-risk-lower.py ap             solutions-accounts-payable.html

Finds the <section> that holds the page's risk headline (or, if there is none, the
smallest <div> holding both "The risk" and that line) and adds margin-top to its style.
Re-running adds nothing more; re-run with --px N to change the amount.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)
  --px N        how much space (default 48)

Dry run by default; --apply writes the page (.bak beside it).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
PX = int(sys.argv[sys.argv.index("--px") + 1]) if "--px" in sys.argv else 48
PAGES = {
    "ap":  ("solutions-accounts-payable.html", "Business email compromise no longer needs"),
    "crm": ("solutions-crm.html", "A CRM agent can access anything the AE can"),
}
which = next((a for a in sys.argv[1:] if a in PAGES), None)
if not which:
    sys.exit("say which page: ap or crm")
fname, KEY = PAGES[which]
page = SITE / fname
s = page.read_text(encoding="utf-8")
MARK = "data-risk-lowered"

key = s.find(KEY)
if key < 0:
    sys.exit(f"'{KEY}' not found in {fname} - send me the grep of 'The risk'")


def block_end(tag, start):
    depth = 0
    for m in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(s, start):
        depth += 1 if not m.group(0).startswith("</") else -1
        if depth == 0:
            return m.end()
    return -1


def enclosing(tag, need=None):
    best = None
    for m in re.finditer(rf"<{tag}\b[^>]*>", s[:key], re.I):
        b = block_end(tag, m.start())
        if b > key and (need is None or need in s[m.start():b]):
            if best is None or b - m.start() < best[2]:
                best = (m.start(), m.end(), b - m.start())
    return best

hit = enclosing("section") or enclosing("div", "The risk")
if not hit:
    sys.exit("could not find the section - send me the lines around 'The risk'")
a, open_end, _ = hit
tag = s[a:open_end]
if MARK in tag:
    if "--px" not in sys.argv:
        sys.exit("already moved down - add --px N to change the amount")
    new = re.sub(r"margin-top:\s*\d+px", f"margin-top: {PX}px", tag, count=1)
elif re.search(r'\bstyle="', tag):
    new = re.sub(r'\bstyle="([^"]*)"',
                 lambda m: f'style="{m.group(1).rstrip("; ")}{"; " if m.group(1).strip() else ""}margin-top: {PX}px;"',
                 tag, count=1)
else:
    new = tag[:-1] + f' style="margin-top: {PX}px;"' + ">"
if MARK not in new:
    new = new[:-1] + f" {MARK}>"

print("- " + tag + "\n+ " + new)
if APPLY:
    shutil.copy2(page, str(page) + ".bak")
    page.write_text(s[:a] + new + s[open_end:], encoding="utf-8")
    print("applied (.bak beside it)")
else:
    print("dry run - re-run with --apply to write")
