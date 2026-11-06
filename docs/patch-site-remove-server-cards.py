#!/usr/bin/env python3
"""index.html: remove three cards -
  Internal MCP servers  /  LLM providers  /  External MCP servers

Each card is the smallest <div>, <article> or <li> holding both its title and its own
sentence. If that leaves the grid (or its section) holding nothing at all, that empty
wrapper goes too. The dry run prints every block it removes.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes index.html (index.html.bak beside it).
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
page = SITE / "index.html"
s = page.read_text(encoding="utf-8")

CARDS = [("Internal MCP servers", "Traffic stays inside your network"),
         ("LLM providers", "Every inference call is metered"),
         ("External MCP servers", "SaaS or cloud-hosted MCP servers")]
TAGS = "div|article|li|section"


def block_end(tag, start):
    depth = 0
    for m in re.compile(rf"<{tag}\b|</{tag}\s*>", re.I).finditer(s, start):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            return m.end()
    return -1


def smallest(key_pos, need):
    best = None
    for m in re.finditer(rf"<({TAGS})\b[^>]*>", s[:key_pos], re.I):
        b = block_end(m.group(1).lower(), m.start())
        if b > key_pos and need in s[m.start():b]:
            if best is None or b - m.start() < best[1] - best[0]:
                best = (m.start(), b)
    return best


for title, sentence in CARDS:
    if s.count(sentence) != 1:
        sys.exit(f"'{sentence}' found {s.count(sentence)} times (want 1) - already removed?")
    a, b = smallest(s.index(sentence), title) or sys.exit(f"no card found for {title}")
    if b - a > 3000:
        sys.exit(f"the '{title}' block is {b - a:,} B - bigger than a card; not touching it")
    a = s.rfind("\n", 0, a) + 1
    if s[b:b + 1] == "\n":
        b += 1
    print(f"--- removing card: {title} ---\n{s[a:b]}")
    s = s[:a] + s[b:]

# drop wrappers left completely empty
changed = True
while changed:
    changed = False
    for m in re.finditer(rf"<({TAGS})\b[^>]*>(\s*)</\1\s*>[ \t]*\n?", s, re.I):
        print(f"--- removing empty wrapper ---\n{m.group(0)}")
        s = s[:m.start()] + s[m.end():]
        changed = True
        break

if APPLY:
    shutil.copy2(page, str(page) + ".bak")
    page.write_text(s, encoding="utf-8")
    print("applied (index.html.bak beside it)")
else:
    print("dry run - re-run with --apply to write")
