#!/usr/bin/env python3
"""index.html: give the CLI section ("A CLI that gets out of your way") an anchor, #cli,
so Developers > CLI in the top menu can jump to it.

Adds an invisible <a id="cli"> as the first thing inside that section, offset so the
section heading isn't hidden under the fixed top bar. Re-running changes nothing.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes index.html (index.html.bak beside it).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
page = SITE / "index.html"
s = page.read_text(encoding="utf-8")

if 'id="cli"' in s:
    sys.exit('index.html already has id="cli" - nothing to do')
key = s.find("A CLI that gets out")
if key < 0:
    sys.exit("'A CLI that gets out' not found in index.html")

opens = list(re.finditer(r"<section\b[^>]*>", s[:key]))
if not opens:
    sys.exit("no <section> before the CLI heading - send me the lines around it")
m = opens[-1]
if s.find("</section>", m.end()) < key:
    sys.exit("the CLI heading isn't inside a <section> - send me the lines around it")

anchor = '<a id="cli" aria-hidden="true" style="display:block;position:relative;top:-90px;visibility:hidden;"></a>'
print("in:  " + m.group(0) + "\nadd: " + anchor)
s = s[:m.end()] + "\n" + anchor + s[m.end():]
if APPLY:
    shutil.copy2(page, str(page) + ".bak")
    page.write_text(s, encoding="utf-8")
    print("applied (index.html.bak beside it)")
else:
    print("dry run - re-run with --apply to write")
