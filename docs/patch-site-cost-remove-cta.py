#!/usr/bin/env python3
"""cost.html: remove the "Put a brake on agent spend." call-to-action block
(its paragraph and its Try it now / View on GitHub buttons).

It removes the smallest <section> or <div> that holds both "Put a brake on agent spend"
and "View on GitHub", plus any wrapper around it that holds nothing else (so no empty
padded band is left behind). The dry run prints exactly what goes.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes cost.html (cost.html.bak beside it).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
page = SITE / "cost.html"
s = page.read_text(encoding="utf-8")

key = s.find("Put a brake on agent spend")
if key < 0:
    sys.exit("'Put a brake on agent spend' not found - already removed?")
if s.count("Put a brake on agent spend") != 1:
    sys.exit("that text appears more than once - send me the grep of it")


def block_end(tag, start):
    """index just past the </tag> that closes the <tag> opening at start."""
    depth = 0
    for m in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(s, start):
        depth += 1 if not m.group(0).startswith("</") else -1
        if depth == 0:
            return m.end()
    return -1


best = None
for m in re.finditer(r"<(section|div)\b[^>]*>", s[:key], re.I):
    a, tag = m.start(), m.group(1).lower()
    b = block_end(tag, a)
    if b > key and "View on GitHub" in s[a:b]:
        if best is None or b - a < best[1] - best[0]:
            best = (a, b)
if not best:
    sys.exit("could not find the block - send me the lines around 'Put a brake'")

a, b = best
# widen to any wrapper that holds nothing else (e.g. <section class="cta"> around a container)
while True:
    wider = None
    for m in re.finditer(r"<(section|div)\b[^>]*>", s[:a], re.I):
        b2 = block_end(m.group(1).lower(), m.start())
        if b2 >= b:
            inner_open = m.end()
            inner_close = s.rfind("</", 0, b2)
            if not (s[inner_open:a] + s[b:inner_close]).strip():
                wider = (m.start(), b2)
    if not wider:
        break
    a, b = wider
a = s.rfind("\n", 0, a) + 1                       # take the whole first line
if s[b:b + 1] == "\n":
    b += 1
print("--- removing ---\n" + s[a:b] + "--- end ---")
if len(s[a:b]) > 4000:
    sys.exit("that block is bigger than expected - not touching it; send me the output above")
if APPLY:
    shutil.copy2(page, str(page) + ".bak")
    page.write_text(s[:a] + s[b:], encoding="utf-8")
    print("applied (cost.html.bak beside it)")
else:
    print("dry run - re-run with --apply to write")
