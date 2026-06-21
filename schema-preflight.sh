#!/usr/bin/env bash
# schema-preflight.sh — validate that the chart's postgres-init ConfigMaps
# produce, from ZERO, the same schema foxygirl is actually running.
#
# Read-only against live DBs (pg_dump --schema-only). All scratch work happens
# in the throwaway namespace `schema-test` and is torn down at the end.
#
# Run from the CHART repo root:  ~/GetHub/orchestrator
set -uo pipefail   # NOT -e: we want to keep going and report, not abort silently

# ─────────────────────────── tunables ───────────────────────────
CHART="helm/orchestrator/"
RELEASE="orchestrator"
LIVE_NS="mcp-system"                       # where foxygirl's real DBs run
SCRATCH_NS="schema-test"
WORK=/tmp/schema-preflight
mkdir -p "$WORK"

# Live pod selectors (from your ops notes)
PLATFORM_SELECTOR="app=mcp-orchestrator-postgres"
# Inventory selector — confirmed against running cluster.
INVENTORY_SELECTOR="app=mcp-orchestrator-inventory-postgres"

# Templates to render (platform first — the nail-biter)
PLATFORM_TPL="templates/postgres-init.yaml"
INVENTORY_TPL="templates/inventory-postgres-init.yaml"   # may differ; see note

echo "==> workspace: $WORK"
command -v yq >/dev/null 2>&1 && HAVE_YQ=1 || HAVE_YQ=0
echo "==> yq present: $HAVE_YQ"

# ─────────────── helper: pull a rendered ConfigMap's SQL keys ───────────────
# args: <rendered-yaml-file> <output-sql-file>
# concatenates every *.sql key in sorted (numeric/alpha) order.
extract_sql () {
  local rendered="$1" out="$2"
  : > "$out"
  # list keys in file order; they're already 01/02/03 so sort -V keeps order
  local keys
  keys=$(grep -oE '^[[:space:]]+[A-Za-z0-9._-]+\.sql:' "$rendered" \
          | tr -d ' :' | sort -V | uniq)
  if [ -z "$keys" ]; then
    echo "    !! no *.sql keys found in $rendered" >&2
    return 1
  fi
  echo "    keys (apply order):"
  for k in $keys; do echo "      - $k"; done
  if [ "$HAVE_YQ" = "1" ]; then
    for k in $keys; do
      echo "-- ===== $k =====" >> "$out"
      yq ".data.\"$k\"" "$rendered" >> "$out"
      echo "" >> "$out"
    done
  else
    # yq-less fallback: use python to parse the single-doc YAML
    python3 - "$rendered" "$out" <<'PY'
import sys, yaml
rendered, out = sys.argv[1], sys.argv[2]
docs = [d for d in yaml.safe_load_all(open(rendered)) if d]
with open(out, "w") as f:
    for d in docs:
        data = (d or {}).get("data", {}) or {}
        for k in sorted(data, key=lambda s: s):
            if k.endswith(".sql"):
                f.write(f"-- ===== {k} =====\n{data[k]}\n\n")
PY
  fi
  echo "    -> $out ($(grep -c 'CREATE TABLE' "$out") CREATE TABLE stmts)"
}

# ─────────────── helper: stand up a throwaway pg, apply sql, dump ───────────
# args: <tag> <dbname> <init-sql-file> <out-dump-file>
throwaway_dump () {
  local tag="$1" db="$2" sql="$3" dump="$4"
  local pod="pg-scratch-$tag"
  echo "==> [$tag] throwaway Postgres, db=$db"

  kubectl get ns "$SCRATCH_NS" >/dev/null 2>&1 || kubectl create ns "$SCRATCH_NS"

  kubectl apply -n "$SCRATCH_NS" -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: ${pod} }
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 2Gi } }
---
apiVersion: v1
kind: Pod
metadata: { name: ${pod} }
spec:
  containers:
  - name: postgres
    image: ${PG_IMAGE}
    env:
    - { name: POSTGRES_USER,     value: mcp }
    - { name: POSTGRES_PASSWORD, value: scratchpass }
    - { name: POSTGRES_DB,       value: ${db} }
    volumeMounts:
    - { name: data, mountPath: /var/lib/postgresql/data }
  volumes:
  - name: data
    persistentVolumeClaim: { claimName: ${pod} }
EOF

  echo "    waiting for ready..."
  kubectl wait --for=condition=Ready "pod/${pod}" -n "$SCRATCH_NS" --timeout=150s \
    || { echo "    !! pod never became ready"; kubectl logs -n "$SCRATCH_NS" "$pod" | tail -20; return 1; }
  sleep 3  # let postgres finish its own first-boot before we hammer it

  kubectl cp "$sql" "${SCRATCH_NS}/${pod}:/tmp/init.sql"
  echo "    applying init SQL (ON_ERROR_STOP=1) — watch for failures:"
  kubectl exec -n "$SCRATCH_NS" "$pod" -- \
    psql -U mcp -d "$db" -v ON_ERROR_STOP=1 -f /tmp/init.sql \
    2>&1 | tee "${WORK}/${tag}-apply.log" | grep -iE 'error|fatal|abort' \
    && echo "    !! ^ errors during apply — this is a real finding" \
    || echo "    apply completed with no error lines"

  kubectl exec -n "$SCRATCH_NS" "$pod" -- \
    pg_dump -U mcp -d "$db" --schema-only --no-owner --no-privileges \
    > "$dump" 2>/dev/null
  echo "    -> $dump ($(grep -c 'CREATE TABLE' "$dump") CREATE TABLE stmts)"
}

# ─────────────── helper: dump a live foxygirl db (read-only) ────────────────
# args: <selector> <dbname> <out-file>
live_dump () {
  local sel="$1" db="$2" out="$3"
  local pod
  pod=$(kubectl get pod -n "$LIVE_NS" -l "$sel" -o name 2>/dev/null | head -1)
  if [ -z "$pod" ]; then
    echo "    !! no live pod for selector '$sel' — skipping live dump for $db" >&2
    return 1
  fi
  kubectl exec -n "$LIVE_NS" "$pod" -- \
    pg_dump -U mcp -d "$db" --schema-only --no-owner --no-privileges \
    > "$out" 2>/dev/null
  echo "    -> $out ($(grep -c 'CREATE TABLE' "$out") CREATE TABLE stmts)"
}

# ─────────────── 0. discover the live Postgres image (version match) ────────
echo "==> discovering live Postgres image for version match"
PLAT_POD=$(kubectl get pod -n "$LIVE_NS" -l "$PLATFORM_SELECTOR" -o name | head -1)
if [ -z "$PLAT_POD" ]; then
  echo "!! could not find live platform postgres pod with selector $PLATFORM_SELECTOR"
  echo "   fix PLATFORM_SELECTOR at top of script and re-run."; exit 1
fi
PG_IMAGE=$(kubectl get -n "$LIVE_NS" "$PLAT_POD" -o jsonpath='{.spec.containers[0].image}')
echo "    PG_IMAGE=$PG_IMAGE"

# ─────────────── 1. render the two init templates ──────────────────────────
echo "==> rendering platform init template"
helm template "$RELEASE" "$CHART" -s "$PLATFORM_TPL" > "$WORK/rendered-platform.yaml" \
  || { echo "!! helm render failed for $PLATFORM_TPL"; exit 1; }
extract_sql "$WORK/rendered-platform.yaml" "$WORK/platform-init.sql"

echo "==> rendering inventory init template"
if helm template "$RELEASE" "$CHART" -s "$INVENTORY_TPL" > "$WORK/rendered-inventory.yaml" 2>/dev/null; then
  extract_sql "$WORK/rendered-inventory.yaml" "$WORK/inventory-init.sql"
  HAVE_INV=1
else
  echo "    note: $INVENTORY_TPL didn't render by that name."
  echo "    list templates with: ls helm/orchestrator/templates | grep -i inventory"
  HAVE_INV=0
fi

# ─────────────── 2. platform: throwaway + dump + live dump + diff ───────────
throwaway_dump platform mcp_platform "$WORK/platform-init.sql" "$WORK/platform-from-init.sql"
echo "==> dumping LIVE platform schema (read-only)"
live_dump "$PLATFORM_SELECTOR" mcp_platform "$WORK/platform-live.sql"

echo "==> DIFF: platform (live vs init-produced)"
diff -u "$WORK/platform-live.sql" "$WORK/platform-from-init.sql" \
  > "$WORK/platform-diff.txt"
if [ -s "$WORK/platform-diff.txt" ]; then
  echo "    !! differences found — see $WORK/platform-diff.txt"
  echo "    structural lines only:"
  grep -E '^\+|^-' "$WORK/platform-diff.txt" \
    | grep -iE 'table|column|index|constraint|trigger|sequence' | head -40
else
  echo "    ✓ platform schema IDENTICAL — init reproduces foxygirl exactly"
fi

# ─────────────── 3. inventory: same passes (if template found) ──────────────
if [ "${HAVE_INV:-0}" = "1" ]; then
  throwaway_dump inventory mcp_inventory "$WORK/inventory-init.sql" "$WORK/inventory-from-init.sql"
  echo "==> dumping LIVE inventory schema (read-only)"
  live_dump "$INVENTORY_SELECTOR" mcp_inventory "$WORK/inventory-live.sql"
  echo "==> DIFF: inventory (live vs init-produced)"
  diff -u "$WORK/inventory-live.sql" "$WORK/inventory-from-init.sql" \
    > "$WORK/inventory-diff.txt"
  if [ -s "$WORK/inventory-diff.txt" ]; then
    echo "    !! differences found — see $WORK/inventory-diff.txt"
    grep -E '^\+|^-' "$WORK/inventory-diff.txt" \
      | grep -iE 'table|column|index|constraint|trigger|sequence' | head -40
  else
    echo "    ✓ inventory schema IDENTICAL"
  fi
fi

# ─────────────── 4. summary ─────────────────────────────────────────────────
echo ""
echo "=================== SUMMARY ==================="
echo "platform diff : $WORK/platform-diff.txt   ($(wc -l < "$WORK/platform-diff.txt" 2>/dev/null || echo 0) lines)"
[ "${HAVE_INV:-0}" = "1" ] && \
echo "inventory diff: $WORK/inventory-diff.txt  ($(wc -l < "$WORK/inventory-diff.txt" 2>/dev/null || echo 0) lines)"
echo "apply logs    : $WORK/platform-apply.log  $WORK/inventory-apply.log"
echo ""
echo "Reading the diff:"
echo "  '-' lines = on foxygirl but NOT produced by init  -> the dangerous gaps"
echo "              (forgot to add to init, OR db_client.cpp boot-apply adds them)"
echo "  '+' lines = produced by init but NOT on foxygirl   -> foxygirl drifted"
echo ""
echo "For each '-' gap, confirm whether boot-apply covers it:"
echo "  grep -rniE 'CREATE TABLE|ALTER TABLE|ADD COLUMN' --include=*.cpp \\"
echo "    ~/GetHub/mcp-platform-private/ | grep -i <suspect>"
echo ""
read -r -p "Tear down scratch namespace '$SCRATCH_NS' now? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  kubectl delete ns "$SCRATCH_NS" --wait=false
  echo "scratch namespace deletion requested. Diffs preserved in $WORK."
else
  echo "left $SCRATCH_NS running. Clean up later with: kubectl delete ns $SCRATCH_NS"
fi
