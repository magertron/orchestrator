#!/usr/bin/env python3
"""index.html hero carousel: replace the first shot (dashboard-galaxy.png) with the
Agent Galaxy page (agent-galaxy.png - put it in docs/screenshots/ first).

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes index.html (index.html.bak beside it).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
page = SITE / "index.html"
s = page.read_text(encoding="utf-8")

if not (SITE / "screenshots" / "agent-galaxy.png").exists():
    sys.exit("missing docs/screenshots/agent-galaxy.png - copy it there first")
found = re.findall(r'<img class="slide-1"[^>]*>', s)
if len(found) != 1:
    sys.exit(f"expected one slide-1 <img>, found {len(found)}")
old = found[0]
if "agent-galaxy.png" in old:
    sys.exit("already swapped - nothing to do")
new = ('<img class="slide-1" src="screenshots/agent-galaxy.png" alt="Magertron Agent Galaxy - '
       'which people and AI agents can reach which tools and models, entitlements versus '
       'observed usage, with unused grants and ungranted access flagged">')
s = s.replace(old, new, 1)
print("- " + old + "\n+ " + new)
if APPLY:
    shutil.copy2(page, str(page) + ".bak")
    page.write_text(s, encoding="utf-8")
    print("\napplied (index.html.bak beside it)")
else:
    print("\ndry run - re-run with --apply to write")
