#!/usr/bin/env python3
"""Website headers: center the menu (Platform ... Security) in the top bar.

The logo stays on the left and "Try it now" stays on the right. The menu sits centered
in the space between them: the leftover room is split evenly on both sides of it.
Desktop only (wider than 768px); the phone menu is untouched.

Adds <style id="mag-nav-center"> just before </head> on every page that has the
shared menu (<style id="mag-solutions-nav">). Pages already patched are left alone.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes (.bak beside each changed page).
"""
import pathlib, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()

CSS = """<style id="mag-nav-center">
@media (min-width: 769px) {
  nav .nav-links { flex: 1; }
  nav .nav-links > :first-child { margin-left: auto; }
  nav .nav-links > .nav-cta { margin-left: auto; }
}
</style>
"""

changed, fine, skipped = {}, 0, []
for page in sorted(SITE.rglob("*.html")):
    if ".bak" in page.name:
        continue
    rel = page.relative_to(SITE)
    s = page.read_text(encoding="utf-8", errors="replace")
    if 'id="mag-nav-center"' in s:
        fine += 1; continue
    if 'id="mag-solutions-nav"' not in s:
        skipped.append((rel, "no shared menu")); continue
    if s.count("</head>") != 1:
        skipped.append((rel, "not exactly one </head>")); continue
    changed[page] = s.replace("</head>", CSS + "</head>", 1)
    print(f"ok    {rel}")

for rel, why in skipped:
    print(f"skip  {rel}: {why}")
print(f"\n{len(changed)} page(s) to change, {fine} already centered, {len(skipped)} skipped")

if APPLY:
    for page, s in changed.items():
        shutil.copy2(page, str(page) + ".bak")
        page.write_text(s, encoding="utf-8")
    print("applied (.bak beside each changed page)")
else:
    print("dry run - re-run with --apply to write")
