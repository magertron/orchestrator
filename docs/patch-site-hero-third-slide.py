#!/usr/bin/env python3
"""index.html hero carousel: add a third screenshot.

  slide-1  dashboard-galaxy.png      (unchanged)
  slide-2  ai-cost-dashboard.png     (unchanged)
  slide-3  dashboard-overview.png    (new: put it in docs/screenshots/ first)

The cycle goes from 12s for two shots to 18s for three: each shot shows about 6s, with
the same crossfade between them. With reduced motion on, only the first shot shows,
as today.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes index.html (index.html.bak beside it).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
page = SITE / "index.html"
s = page.read_text(encoding="utf-8")

if "slide-3" in s:
    sys.exit("index.html already has slide-3 - nothing to do")
if not (SITE / "screenshots" / "dashboard-overview.png").exists():
    sys.exit("missing docs/screenshots/dashboard-overview.png - copy it there first")

def once(text, what):
    n = s.count(text)
    if n != 1:
        sys.exit(f"expected exactly one {what}, found {n} - send me lines 270-300 of index.html")
    return s.index(text)

# 1. slide-3 overlays the first shot exactly like slide-2 does
once(".hero-carousel img.slide-2 {\n", "slide-2 overlay rule")
s = s.replace(".hero-carousel img.slide-2 {\n",
              ".hero-carousel img.slide-2, .hero-carousel img.slide-3 {\n", 1)

# 2. replace the animation rules, keyframes and reduced-motion block
a = once(".hero-carousel img.slide-1 { animation:", "slide-1 animation rule")
d = once(".hero-carousel img.slide-2 { display: none; }", "reduced-motion rule")
media = s.rfind("@media", a, d)
if media < 0:
    sys.exit("no @media before the reduced-motion rule - send me lines 270-300 of index.html")
media_open = s[media:s.index("{", media) + 1]
end = s.index("}", d + len(".hero-carousel img.slide-2 { display: none; }")) + 1
ind = re.match(r"[ \t]*", s[s.rfind("\n", 0, d) + 1:]).group(0)

new_css = f""".hero-carousel img.slide-1 {{ animation: heroFade3 18s ease-in-out infinite; }}
.hero-carousel img.slide-2 {{ animation: heroFade3 18s ease-in-out -12s infinite; }}
.hero-carousel img.slide-3 {{ animation: heroFade3 18s ease-in-out -6s infinite; }}
/* each shot: on for a third of the cycle, ~1s crossfade at each end */
@keyframes heroFade3 {{
  0%, 28% {{ opacity: 1; }}
  33.4%, 94.5% {{ opacity: 0; }}
  100% {{ opacity: 1; }}
}}
{media_open}
{ind}.hero-carousel img.slide-1, .hero-carousel img.slide-2, .hero-carousel img.slide-3 {{ animation: none; }}
{ind}.hero-carousel img.slide-2, .hero-carousel img.slide-3 {{ display: none; }}
}}"""
old_css = s[a:end]
s = s[:a] + new_css + s[end:]

# 3. the third image, right after the second
m = re.search(r'^([ \t]*)<img class="slide-2"[^\n]*\n', s, re.M)
if not m:
    sys.exit("slide-2 <img> not found")
img = (f'{m.group(1)}<img class="slide-3" src="screenshots/dashboard-overview.png" '
       f'alt="Magertron dashboard - servers, tools, routes, users and policies at a glance, '
       f'LLM tokens and cost by model, budget vs actual, server health and audit activity">\n')
s = s[:m.end()] + img + s[m.end():]

print("--- old carousel CSS ---\n" + old_css + "\n--- new carousel CSS ---\n" + new_css)
print("\n+ " + img.strip())
if APPLY:
    shutil.copy2(page, str(page) + ".bak")
    page.write_text(s, encoding="utf-8")
    print("\napplied (index.html.bak beside it)")
else:
    print("\ndry run - re-run with --apply to write")
