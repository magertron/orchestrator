#!/usr/bin/env python3
"""Build docs/pricing.html from the pricing section on index.html.

pricing.html = index.html's <head> and top bar, then the pricing section (id="pricing")
exactly as it is on index.html, then index.html's footer, the Try it now form and the
page scripts. index.html itself is not changed (its pricing section stays).

  - <title> becomes "Pricing - Magertron(TM) AI Orchestrator"; a canonical link, if any,
    points at pricing.html.
  - Links inside the pricing section that jump within index.html (href="#install" etc.)
    are pointed back at index.html#... so they still work.

Afterwards run patch-site-nav-final.py (the copy that sends Pricing to pricing.html) so
every page's menu points here and pricing.html gets the standard menu.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default (prints what it takes); --apply writes pricing.html. It will not
overwrite an existing pricing.html unless you add --force.
"""
import pathlib, re, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
idx = (SITE / "index.html").read_text(encoding="utf-8")
out_path = SITE / "pricing.html"
if out_path.exists() and "--force" not in sys.argv:
    sys.exit("pricing.html already exists - add --force to rebuild it")


def die(m):
    sys.exit(f"ABORT: {m} - nothing written")


def element(start):
    """(start, end) of the element whose opening tag begins at start."""
    tag = re.match(r"<(\w+)", idx[start:]).group(1)
    depth = 0
    for m in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(idx, start):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            return start, m.end()
    die(f"no closing </{tag}>")


# top: everything through the end of the top bar
n = idx.find("</nav>")
if n < 0 or idx.count("</nav>") != 1:
    die("index.html needs exactly one </nav>")
top = idx[:n + len("</nav>")] + "\n"

# the pricing section
m = re.search(r'<(section|div)\b[^>]*\bid="pricing"[^>]*>', idx)
if not m:
    die('no <section id="pricing"> (or div) on index.html')
pa, pb = element(m.start())
pricing = idx[pa:pb]
own_ids = set(re.findall(r'\bid="([^"]+)"', pricing))
pricing = re.sub(r'href="#([\w-]+)"',
                 lambda x: x.group(0) if x.group(1) in own_ids else f'href="index.html#{x.group(1)}"',
                 pricing)

# footer
f = re.search(r"<footer\b", idx)
if not f:
    die("no <footer> on index.html")
fa, fb = element(f.start())
footer = idx[fa:fb]

# the Try it now form
mm = idx.find('<div class="modal-overlay" id="demoModal"')
if mm < 0:
    die('no <div class="modal-overlay" id="demoModal"> on index.html')
ma, mb = element(mm)
modal = idx[ma:mb]

# scripts after the top bar, except any inside the pricing section (it already has them)
scripts = [x for x in re.finditer(r"<script\b.*?</script>", idx[n:], re.S)
           if not (pa <= n + x.start() < pb)]
script_text = "\n".join(x.group(0) for x in scripts)

top = re.sub(r"<title>.*?</title>", "<title>Pricing - Magertron™ AI Orchestrator</title>", top, count=1, flags=re.S)
top = re.sub(r'(<link rel="canonical" href=")[^"]*(")',
             r"\1https://magertron.com/pricing.html\2", top, count=1)

page = (top + '\n<main style="padding-top: 24px;">\n' + pricing + "\n</main>\n\n"
        + footer + "\n\n" + modal + "\n\n" + script_text + "\n</body>\n</html>\n")

print(f"top bar + head: {len(top):,} B")
print(f"pricing section: {len(pricing):,} B  ({pricing[:pricing.find('>') + 1]})")
print(f"footer: {len(footer):,} B   form: {len(modal):,} B   scripts: {len(scripts)}")
print(f"pricing.html would be {len(page):,} B")
if APPLY:
    out_path.write_text(page, encoding="utf-8")
    print(f"wrote {out_path}")
else:
    print("dry run - re-run with --apply to write")
