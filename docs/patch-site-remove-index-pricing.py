#!/usr/bin/env python3
"""Remove the pricing section from index.html now that pricing.html has it, and point
every link to it at the new page.

  - index.html: the element with id="pricing" is removed; href="#pricing" -> pricing.html
  - every page: href="index.html#pricing" (any ../ depth) -> pricing.html at that depth,
                href="/#pricing" -> /pricing.html

Run build-pricing-page.py --apply FIRST - this refuses to run without docs/pricing.html.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes (.bak beside each changed page).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
if not (SITE / "pricing.html").exists():
    sys.exit("docs/pricing.html is missing - run build-pricing-page.py --apply first")

out = {}
for page in sorted(SITE.rglob("*.html")):
    if ".bak" in page.name or page.name == "pricing.html":
        continue
    rel = page.relative_to(SITE)
    s0 = page.read_text(encoding="utf-8", errors="replace")
    s, notes = s0, []

    if rel.as_posix() == "index.html":
        m = re.search(r'<(section|div)\b[^>]*\bid="pricing"[^>]*>', s)
        if m:
            tag, depth, end = m.group(1), 0, None
            for t in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(s, m.start()):
                depth += -1 if t.group(0).startswith("</") else 1
                if depth == 0:
                    end = t.end(); break
            if end is None:
                sys.exit("index.html: could not find the end of the pricing section")
            a = s.rfind("\n", 0, m.start()) + 1
            if s[end:end + 1] == "\n":
                end += 1
            notes.append(f"removed pricing section ({end - a:,} B, starts: {m.group(0)})")
            s = s[:a] + s[end:]
        n = len(re.findall(r'href="#pricing"', s))
        if n:
            s = s.replace('href="#pricing"', 'href="pricing.html"')
            notes.append(f'{n} link(s) #pricing -> pricing.html')

    n1 = len(re.findall(r'href="((?:\.\./)*)index\.html#pricing"', s))
    s = re.sub(r'href="((?:\.\./)*)index\.html#pricing"', r'href="\1pricing.html"', s)
    n2 = s.count('href="/#pricing"')
    s = s.replace('href="/#pricing"', 'href="/pricing.html"')
    if n1 + n2:
        notes.append(f"{n1 + n2} link(s) index.html#pricing -> pricing.html")

    if s != s0:
        out[page] = s
        print(f"ok    {rel}: " + "; ".join(notes))

if 'id="pricing"' in out.get(SITE / "index.html", (SITE / "index.html").read_text(encoding="utf-8")):
    sys.exit("index.html still has id=\"pricing\" after the change - not writing anything")
print(f"\n{len(out)} page(s) to change")
if APPLY:
    for page, s in out.items():
        shutil.copy2(page, str(page) + ".bak")
        page.write_text(s, encoding="utf-8")
    print("applied (.bak beside each changed page)")
else:
    print("dry run - re-run with --apply to write")
