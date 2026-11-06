#!/usr/bin/env python3
"""Website menus: a menu opened by a click now closes when you click anywhere else on the
page, or press Esc. Choosing a link inside the menu also closes it.

Adds <script id="mag-nav-close"> before </body> on every page with the shared menu
(<style id="mag-solutions-nav">). Pages that already have it are left alone.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default; --apply writes (.bak beside each changed page).
"""
import pathlib, shutil, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()

JS = """<script id="mag-nav-close">
(function () {
  function closeAll(except) {
    document.querySelectorAll('.nav-drop.open').forEach(function (d) {
      if (d !== except) d.classList.remove('open');
    });
  }
  document.addEventListener('click', function (e) {
    var drop = e.target.closest('.nav-drop');
    if (!drop) {
      closeAll(null);
      if (document.activeElement && document.activeElement.closest('.nav-drop')) document.activeElement.blur();
      return;
    }
    if (e.target.closest('.nav-drop-menu a')) {
      closeAll(null);
      e.target.closest('.nav-drop-menu a').blur();
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      closeAll(null);
      if (document.activeElement && document.activeElement.closest('.nav-drop')) document.activeElement.blur();
    }
  });
})();
</script>
"""

changed, fine, skipped = {}, 0, []
for page in sorted(SITE.rglob("*.html")):
    if ".bak" in page.name:
        continue
    rel = page.relative_to(SITE)
    s = page.read_text(encoding="utf-8", errors="replace")
    if 'id="mag-nav-close"' in s:
        fine += 1; continue
    if 'id="mag-solutions-nav"' not in s:
        skipped.append((rel, "no shared menu")); continue
    if s.count("</body>") != 1:
        skipped.append((rel, "not exactly one </body>")); continue
    changed[page] = s.replace("</body>", JS + "</body>", 1)
    print(f"ok    {rel}")

for rel, why in skipped:
    print(f"skip  {rel}: {why}")
print(f"\n{len(changed)} page(s) to change, {fine} already done, {len(skipped)} skipped")
if APPLY:
    for page, s in changed.items():
        shutil.copy2(page, str(page) + ".bak")
        page.write_text(s, encoding="utf-8")
    print("applied (.bak beside each changed page)")
else:
    print("dry run - re-run with --apply to write")
