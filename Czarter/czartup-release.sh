#!/usr/bin/env bash
# Release Czartup(TM): build, push, make it the chart's default, roll it out.
#
#   czartup-release.sh            dry run: shows the version and what would happen
#   czartup-release.sh --apply    does it
#
# The version comes from Czartup itself (serverInfo.version in app/server.py),
# so there is nothing to type and nothing to keep in step by hand.
#
# Why the chart default: install.sh reinstalls from the chart. A tag set with
# `helm upgrade --set-string czartup.image.tag=...` lives only in that release
# and is gone after the next install.sh. With the chart's values.yaml pointing
# at the newest Czartup, every install - install.sh, a fresh box, DR - gets it
# with no flag. The values.yaml change is committed and pushed so the next
# charts-dev build carries it.
#
# Paths (override with env): CZARTUP_DIR=~/GetHub/Czarter
#                            CHART_DIR=~/GetHub/orchestrator/helm/orchestrator
set -euo pipefail

CZARTUP_DIR="${CZARTUP_DIR:-$HOME/GetHub/Czarter}"
CHART_DIR="${CHART_DIR:-$HOME/GetHub/orchestrator/helm/orchestrator}"
REPO_IMAGE="curtismager20/mcp-czartup"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

VER=$(python3 - "$CZARTUP_DIR/app/server.py" <<'PY'
import re, sys
m = re.search(r'"serverInfo":\s*\{[^}]*"version":\s*"([0-9][0-9.]*)"', open(sys.argv[1]).read())
print(m.group(1) if m else "")
PY
)
[ -n "$VER" ] || { echo "could not read the version from $CZARTUP_DIR/app/server.py"; exit 1; }

VALUES="$CHART_DIR/values.yaml"
CUR=$(python3 - "$VALUES" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
m = re.search(r'(?ms)^czartup:\s*\n.*?^\s+image:\s*\n.*?^\s+tag:\s*"?([^"\n]*)"?', s)
print(m.group(1) if m else "")
PY
)
[ -n "$CUR" ] || { echo "could not find czartup.image.tag in $VALUES"; exit 1; }

CHART_VER=$(helm list -n mcp-system -o json | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['chart'].removeprefix('mcp-orchestrator-'))")

echo "Czartup version:        $VER   (from app/server.py)"
echo "chart default today:    $CUR   (values.yaml)"
echo "installed chart:        $CHART_VER"
echo
echo "1. docker build -t $REPO_IMAGE:$VER $CZARTUP_DIR"
echo "2. docker push $REPO_IMAGE:$VER"
echo "3. values.yaml czartup.image.tag: $CUR -> $VER, commit and push the chart repo"
echo "4. helm upgrade --reuse-values --set-string czartup.image.tag=$VER (the running install, now)"
echo "5. release-czartup-cli.sh --apply: the Czartup CLI $VER on magertron.com (apt + install-czartup.sh)"

# Files a release no longer ships. Unzipping a new Czarter.zip over the folder adds and replaces
# files but never deletes, so a retired one would stay and go into the image (a moved template
# kept showing up as a duplicate card in the UI). Each is removed before the build.
RETIRED=(examples/daytrader.czarter.yaml examples/daytrader.runs.json)
for f in "${RETIRED[@]}"; do
    if [ -e "$CZARTUP_DIR/$f" ]; then
        echo "0. remove retired $f"
        [ "$APPLY" = 1 ] && rm -f "$CZARTUP_DIR/$f"
    fi
done
echo "   templates in the image: $(cd "$CZARTUP_DIR/examples" && ls *.czarter.yaml | tr '\n' ' ')"
[ "$APPLY" = 1 ] || { echo; echo "Dry run only - re-run with --apply."; exit 0; }

docker build -t "$REPO_IMAGE:$VER" "$CZARTUP_DIR"
docker push "$REPO_IMAGE:$VER"

if [ "$CUR" != "$VER" ]; then
  python3 - "$VALUES" "$VER" <<'PY'
import re, sys
p, ver = sys.argv[1], sys.argv[2]
s = open(p).read()
s2, n = re.subn(r'(?ms)(^czartup:\s*\n.*?^\s+image:\s*\n.*?^\s+tag:\s*)"?[^"\n]*"?', r'\g<1>"' + ver + '"', s, count=1)
assert n == 1, "czartup.image.tag not found"
open(p, "w").write(s2)
PY
  git -C "$CHART_DIR" add values.yaml
  git -C "$CHART_DIR" commit -m "chart: Czartup $VER is the default"
  # The release pipeline pushes to main too: take its commits first, ours on top.
  git -C "$CHART_DIR" pull --rebase --autostash   # unrelated edits (the build's own) are set aside and restored
  git -C "$CHART_DIR" push
else
  echo "values.yaml already says $VER - nothing to commit"
fi

helm upgrade mcp mcp-orchestrator --repo https://magertron.com/charts-dev --version "$CHART_VER" \
  -n mcp-system --reuse-values --set-string "czartup.image.tag=$VER"
# The CLI ships with the same version, next to mcpctl (soft: the server release above stands either way).
if ! bash "$CZARTUP_DIR/release-czartup-cli.sh" --apply; then
  echo "WARNING: the Czartup CLI was NOT published - the server release above is fine." >&2
  echo "         Retry on its own: $CZARTUP_DIR/release-czartup-cli.sh --apply" >&2
fi
echo
# Wait for the new pod before saying done (0.2.83): a page used during the rollout gets a 503 halfway.
echo "Waiting for the new Czartup pod..."
kubectl rollout status deploy/mcp-orchestrator-czartup -n mcp-system --timeout=180s \
  || echo "WARNING: the new pod is not ready yet - wait before using Czartup" >&2
echo "Czartup $VER is built, pushed, the chart default, and running."
echo "New tools approve themselves: the first admin to reach Czartup (its page, an IDE or the CLI)"
echo "lets it re-probe itself and approve this version's tools, audited to that admin. Reload Czartup in the IDE."
# Nudge it now, if this machine's CLI is signed in: wait for the new pod, then one harmless call.
if command -v czartup >/dev/null 2>&1; then
  kubectl rollout status deploy/mcp-orchestrator-czartup -n mcp-system --timeout=180s >/dev/null 2>&1 || sleep 30
  if czartup list >/dev/null 2>&1; then
    echo "Nudged Czartup through the CLI: its new tools are being approved."
  fi
fi
