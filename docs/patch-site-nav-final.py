#!/usr/bin/env python3
"""Website headers: exactly the same top bar on every page.

  Platform ▾ (AI Orchestrator · AI Data Reach · AgentCzar)
  Solutions ▾ (Banking · Accounts payable · Sales & CRM · Network operations)
  Developers ▾ (Overview · Install · CLI · Developer Portal · API Documentation · GitHub · Build an agent ·
                VS Code · Devin · Cursor · Claude Code)
  Pricing · Cost & Reporting · Security · [the page's own Try it now]

On every page whose <nav> has a  <div class="nav-links">  and a "Try it now" button
(class nav-cta), everything in front of the button is rewritten to the list above, whatever
it held before - pages the earlier scripts skipped get it too. The button is kept as it is.

  - Links use each page's depth: "data-reach.html" at the top, "../data-reach.html" one
    folder down. On index.html, AI Orchestrator and Pricing jump within the page.
  - The page you are on shows white; so does its menu (Platform or Solutions).
  - The menu styles (<style id="mag-solutions-nav">) are added where missing.

Pages without that nav block are LISTED, not changed: send the list back.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default (each page: its top bar before -> after); --apply writes (.bak beside
each changed page). Pages already exactly right are left alone.
"""
import pathlib, re, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()

PLATFORM = [("AI Orchestrator", "index.html#governed-surfaces"), ("AI Data Reach", "data-reach.html"),
            ("AgentCzar", "agentczar.html")]
SOLUTIONS = [("Banking", "solutions-banking.html"), ("Accounts payable", "solutions-accounts-payable.html"),
             ("Sales &amp; CRM", "solutions-crm.html"), ("Network operations", "solutions-network-operations.html"),
             ("Across SaaS platforms", "solutions-saas.html")]
DEVELOPERS = [("Overview", "developers.html"), ("Install", "index.html#install"), ("CLI", "index.html#cli"),
              ("Developer Portal", "developer-portal.html"), ("API Documentation", "api/v1/"),
              ("GitHub", "https://github.com/magertron/orchestrator"),
              ("Build an agent", "developers.html"),
              ("VS Code", "vscode/"), ("Devin", "devin/"), ("Cursor", "cursor/"), ("Claude Code", "claude-code/")]
PLAIN = [("Pricing", "pricing.html"),
         ("Cost &amp; Reporting", "cost.html"), ("Security", "security.html")]
ONCLICK = ("onclick=\"var d=this.parentNode;document.querySelectorAll('.nav-drop.open').forEach("
           "function(x){if(x!==d)x.classList.remove('open')});d.classList.toggle('open')\"")
CUR = ' style="color: var(--text);"'

NAV_CSS = """<style id="mag-solutions-nav">
.nav-drop { position: relative; }
.nav-drop-btn { background: none; border: 0; padding: 0; font: inherit; font-size: 14px; cursor: pointer;
  color: var(--text-secondary); transition: color 0.2s; }
.nav-drop-btn:hover, .nav-drop.on .nav-drop-btn { color: var(--text); }
.nav-drop-btn span { font-size: 10px; margin-left: 4px; }
.nav-drop-menu { display: none; position: absolute; top: calc(100% + 14px); left: -12px; min-width: 210px;
  flex-direction: column; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 6px; box-shadow: 0 12px 32px rgba(0,0,0,.35); }
.nav-drop-menu::before { content: ""; position: absolute; left: 0; right: 0; top: -16px; height: 16px; }
.nav-drop:hover .nav-drop-menu, .nav-drop:focus-within .nav-drop-menu, .nav-drop.open .nav-drop-menu { display: flex; }
.nav-links .nav-drop-menu a { display: block; padding: 8px 10px; border-radius: 6px; white-space: nowrap; }
.nav-links .nav-drop-menu a:hover { background: rgba(255,255,255,.05); }
@media (max-width: 1180px) { .nav-links { gap: 22px; } }
@media (max-width: 768px) {
  .nav-links .nav-drop { display: none; }
  nav.open .nav-links .nav-drop { display: block; }
  nav.open .nav-drop-btn { display: block; width: 100%; text-align: left; padding: 14px 2px; font-size: 16px;
    color: var(--text); border-bottom: 1px solid var(--border); }
  nav.open .nav-drop-menu { display: flex; position: static; min-width: 0; border: 0; border-radius: 0;
    box-shadow: none; padding: 0 0 0 14px; background: none; }
  nav.open .nav-drop-menu::before { display: none; }
}
</style>
"""


def href(target, rel):
    """target as seen from page rel (a path relative to SITE)."""
    if target.startswith("https://"):
        return target
    if rel.as_posix() == "index.html" and "#" in target and target.startswith("index.html"):
        return target[len("index.html"):]
    return "../" * (len(rel.parts) - 1) + target


def here(target, rel):
    return target.split("#")[0] == rel.name and "#" not in target and len(rel.parts) == 1


def link(label, target, rel, ind):
    ext = ' target="_blank" rel="noopener"' if target.startswith("https://") else ""
    return f'{ind}<a href="{href(target, rel)}"{ext}{CUR if here(target, rel) else ""}>{label}</a>\n'


def menu(title, items, rel, ind):
    on = " on" if any(here(t, rel) for _, t in items) else ""
    inner = "".join(link(l, t, rel, ind + "    ") for l, t in items)
    return (f'{ind}<div class="nav-drop{on}">\n'
            f'{ind}  <button type="button" class="nav-drop-btn" aria-haspopup="true" {ONCLICK}>'
            f'{title}<span>&#9662;</span></button>\n'
            f'{ind}  <div class="nav-drop-menu">\n{inner}{ind}  </div>\n{ind}</div>\n')


def top_labels(block):
    """What a reader sees in the top bar: button titles and top-level link labels."""
    out, depth = [], 0
    for tok in re.finditer(r'<div\b[^>]*>|</div>|<button\b[^>]*>(.*?)</button>|<a\b[^>]*>(.*?)</a>', block, re.S):
        t = tok.group(0)
        if t.startswith("<div"):
            depth += 1
        elif t == "</div>":
            depth -= 1
        elif t.startswith("<button"):
            out.append(re.sub(r"<[^>]+>", "", tok.group(1)).replace("&#9662;", "").strip() + " ▾")
        elif depth == 0:
            out.append(re.sub(r"<[^>]+>", "", tok.group(2)).strip())
    return out


out, skipped, fine = {}, [], 0
for page in sorted(SITE.rglob("*.html")):
    if ".bak" in page.name:
        continue
    rel = page.relative_to(SITE)
    s0 = page.read_text(encoding="utf-8", errors="replace")
    n0, n1 = s0.find("<nav"), s0.find("</nav>")
    if n0 < 0 or n1 < 0:
        skipped.append((rel, "no <nav>")); continue
    nav = s0[n0:n1]
    d0 = nav.find('<div class="nav-links"')
    if d0 < 0:
        skipped.append((rel, "no nav-links block")); continue
    open_end = nav.find(">", d0) + 1
    cta = re.search(r'^([ \t]*)<a\b[^>]*class="[^"]*nav-cta[^"]*"', nav[open_end:], re.M)
    if not cta:
        skipped.append((rel, "no Try it now (nav-cta) button")); continue
    ind = cta.group(1)
    start, stop = open_end, open_end + cta.start()
    new_inner = ("\n" + menu("Platform", PLATFORM, rel, ind) + menu("Solutions", SOLUTIONS, rel, ind)
                 + menu("Developers", DEVELOPERS, rel, ind)
                 + "".join(link(l, t, rel, ind) for l, t in PLAIN))
    if nav[start:stop] == new_inner:
        fine += 1; continue
    before = top_labels(nav[start:stop])
    nav2 = nav[:start] + new_inner + nav[stop:]
    s = s0[:n0] + nav2 + s0[n1:]
    extra = ""
    if 'id="mag-solutions-nav"' not in s:
        if s.count("</head>") != 1:
            skipped.append((rel, "menu styles missing and not exactly one </head>")); continue
        s = s.replace("</head>", NAV_CSS + "</head>", 1)
        extra = "  (+ menu styles)"
    out[page] = s
    print(f"ok    {rel}{extra}\n        was: {' · '.join(before)}")

print(f"\nnow:  {' · '.join(top_labels(new_inner)) if out else '(nothing to change)'} · Try it now")
for rel, why in skipped:
    print(f"skip  {rel}: {why}")
print(f"\n{len(out)} page(s) to change, {fine} already right, {len(skipped)} not changed (listed)")

if APPLY:
    for page, s in out.items():
        shutil.copy2(page, str(page) + ".bak")
        page.write_text(s, encoding="utf-8")
    print("applied (.bak beside each changed page)")
else:
    print("dry run - re-run with --apply to write")
