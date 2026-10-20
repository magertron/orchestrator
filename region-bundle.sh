#!/usr/bin/env bash
# ─── region-bundle.sh — carry what every region must share, as ONE file ──────
#
# Every Magertron™ region must share the same platform keys, or a replica
# region rejects the primary's tokens, cannot verify its evidence records,
# cannot log into its own (replicated) databases, and cannot decrypt
# replicated credentials. It must also enforce the same external policy (PDP),
# or the same call is allowed in one region and denied in the other.
# This script moves all of it as ONE encrypted file.
#
#   On the PRIMARY (EAST):   ./region-bundle.sh export east.bundle
#   On the REPLICA (WEST):   ./install.sh ... --replica-of host:port --region-bundle east.bundle
#                      or:   ./region-bundle.sh import east.bundle      (keys only)
#   Either region:           ./region-bundle.sh verify east.bundle
#   Used by install.sh:      ./region-bundle.sh pdp east.bundle <dir>   (writes the PDP settings)
#
# What the bundle carries:
#   * the six platform keys from mcp-orchestrator-secrets
#   * the external PDP settings, if EAST has one: bundled OPA (with its policy
#     and its data file, if one was installed with --opa-data)
#     or the URL of your own PDP, plus its namespaces and fail mode
#
# The bundle is AES-256 encrypted (openssl, PBKDF2, 200k iterations) with a
# passphrase you choose. Key VALUES are never printed — only names and
# MATCH / DIFFER.
#
# Env:
#   REGION_BUNDLE_PASSPHRASE  passphrase (otherwise prompted)
#   KUBE_CONTEXT              kubectl context (default: current)
#   NAMESPACE                 default mcp-system
#   SECRET_NAME               default mcp-orchestrator-secrets
#   RELEASE_NAME              default mcp   (Helm ownership on a fresh import)
set -euo pipefail

NS="${NAMESPACE:-mcp-system}"
SECRET="${SECRET_NAME:-mcp-orchestrator-secrets}"
RELEASE="${RELEASE_NAME:-mcp}"
OPA_CM="mcp-orchestrator-opa-policy"
ORCH_STS="mcp-orchestrator"
KEYS=(MCP_JWT_PRIVATE_KEY MCP_JWT_PUBLIC_KEY MCP_EVIDENCE_PRIVATE_KEY
      MCP_DB_PASSWORD MCP_INVENTORY_DB_PASSWORD MAG_SECRET_ENCRYPTION_KEY)
# Must be present and non-empty in the primary's Secret.
REQUIRED=(MCP_JWT_PRIVATE_KEY MCP_EVIDENCE_PRIVATE_KEY MCP_DB_PASSWORD
          MCP_INVENTORY_DB_PASSWORD MAG_SECRET_ENCRYPTION_KEY)

die()  { echo "region-bundle: ERROR: $*" >&2; exit 1; }
note() { echo "region-bundle: $*"; }
kc()   { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} -n "$NS" "$@"; }

usage() {
    sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

for t in kubectl jq openssl base64; do
    command -v "$t" >/dev/null 2>&1 || die "'$t' is required"
done

get_pass() {   # $1 = confirm (1/0)
    if [ -n "${REGION_BUNDLE_PASSPHRASE:-}" ]; then RB_PASS="$REGION_BUNDLE_PASSPHRASE"; export RB_PASS; return; fi
    read -r -s -p "Bundle passphrase: " RB_PASS; echo >&2
    [ ${#RB_PASS} -ge 12 ] || die "passphrase must be at least 12 characters"
    if [ "$1" = "1" ]; then
        read -r -s -p "Repeat passphrase: " p2; echo >&2
        [ "$RB_PASS" = "$p2" ] || die "passphrases differ"
    fi
    export RB_PASS
}

keys_json() { printf '%s\n' "${KEYS[@]}" | jq -R . | jq -s .; }

# The PDP settings this region runs with, as JSON (or "null" when there is none).
#   {mode: "bundled"|"external", url, namespaces, fail, policy_b64, data_b64}
pdp_json() {
    local env_json url ns fail mode policy="" data=""
    env_json="$(kc get statefulset "$ORCH_STS" -o json 2>/dev/null \
        | jq -c '[.spec.template.spec.containers[] | (.env // [])[]] | map({(.name): (.value // "")}) | add // {}')" \
        || { echo null; return; }
    url="$(jq -r '.MCP_PDP_URL // ""' <<<"$env_json")"
    [ -z "$url" ] && { echo null; return; }
    ns="$(jq -r '.MCP_PDP_NAMESPACES // "*"' <<<"$env_json")"
    fail="$(jq -r '.MCP_PDP_FAIL // "closed"' <<<"$env_json")"
    if kc get configmap "$OPA_CM" >/dev/null 2>&1 && [[ "$url" == *"-opa."* ]]; then
        mode="bundled"
        policy="$(kc get configmap "$OPA_CM" -o json | jq -r '.data["magertron.rego"] // ""' | base64 -w0)"
        # install.sh --opa-data: the caps the policy reads (empty when none).
        data="$(kc get configmap "$OPA_CM" -o json | jq -r '.data["data.json"] // ""' | base64 -w0)"
    else
        mode="external"
    fi
    jq -cn --arg m "$mode" --arg u "$url" --arg n "$ns" --arg f "$fail" --arg p "$policy" --arg d "$data" \
        '{mode: $m, url: $u, namespaces: $n, fail: $f, policy_b64: $p, data_b64: $d}'
}

do_export() {
    local out="$1"
    [ -e "$out" ] && die "$out already exists — choose another name"
    local sj; sj="$(kc get secret "$SECRET" -o json)" || die "cannot read $NS/$SECRET (wrong context?)"
    for k in "${REQUIRED[@]}"; do
        [ -n "$(jq -r --arg k "$k" '.data[$k] // empty' <<<"$sj")" ] \
            || die "$k is missing from $NS/$SECRET — upgrade this region to a chart that generates it, then retry"
    done
    local pdp; pdp="$(pdp_json)"
    get_pass 1
    local payload
    payload="$(jq -c --argjson ks "$(keys_json)" --argjson pdp "$pdp" \
        '{v:1, secret:.metadata.name, data: (.data as $d | reduce $ks[] as $k ({}; . + {($k): ($d[$k] // "")})), pdp: $pdp}' <<<"$sj")"
    ( umask 077; printf '%s' "$payload" | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:RB_PASS -out "$out" )
    unset RB_PASS
    note "exported ${#KEYS[@]} keys from ${KUBE_CONTEXT:-current context} $NS/$SECRET -> $out"
    case "$(jq -r '.mode // "none"' <<<"$pdp")" in
        bundled)  note "  + external PDP: bundled OPA with its policy$( [ -n "$(jq -r '.data_b64 // ""' <<<"$pdp")" ] && echo " and data file") (namespaces $(jq -r .namespaces <<<"$pdp"), fail-$(jq -r .fail <<<"$pdp"))" ;;
        external) note "  + external PDP: your own at $(jq -r .url <<<"$pdp") (namespaces $(jq -r .namespaces <<<"$pdp"), fail-$(jq -r .fail <<<"$pdp"))" ;;
        *)        note "  (no external PDP configured here — none carried)" ;;
    esac
    note "move $out to the replica region; delete it after the install."
}

decrypt() {   # $1 = file -> payload on stdout
    [ -f "$1" ] || die "no such file: $1"
    get_pass 0
    local p
    p="$(openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:RB_PASS -in "$1" 2>/dev/null | tr -d '\000')" \
        || die "cannot decrypt $1 — wrong passphrase or corrupt file"
    unset RB_PASS
    jq -e '.v == 1 and (.data | type == "object")' <<<"$p" >/dev/null 2>&1 || die "$1 is not a region bundle"
    printf '%s' "$p"
}

do_import() {
    local payload; payload="$(decrypt "$1")"
    local data; data="$(jq -c '.data | with_entries(select(.value != ""))' <<<"$payload")"
    if kc get secret "$SECRET" >/dev/null 2>&1; then
        jq -c '{data: .}' <<<"$data" | kc patch secret "$SECRET" --type merge --patch-file /dev/stdin >/dev/null \
            || die "patch of $NS/$SECRET failed"
        note "updated $NS/$SECRET with the primary's keys"
    else
        jq -c --arg n "$SECRET" --arg ns "$NS" --arg rel "$RELEASE" '{
            apiVersion: "v1", kind: "Secret", type: "Opaque",
            metadata: {
              name: $n, namespace: $ns,
              labels: {"app.kubernetes.io/managed-by": "Helm"},
              annotations: {"meta.helm.sh/release-name": $rel,
                            "meta.helm.sh/release-namespace": $ns,
                            "helm.sh/resource-policy": "keep"}},
            data: .}' <<<"$data" | kc create -f - >/dev/null \
            || die "create of $NS/$SECRET failed (does namespace $NS exist?)"
        note "created $NS/$SECRET with the primary's keys (Helm will adopt it)"
    fi
    do_verify_payload "$payload" keys || true
    if [ "$(jq -r '.pdp.mode // "none"' <<<"$payload")" != "none" ]; then
        note "the bundle also carries the primary's external PDP settings —"
        note "  install.sh --region-bundle applies them; 'import' alone does not."
    fi
    note "restart inventory and the orchestrator if they are already running:"
    note "  kubectl -n $NS rollout restart deploy/mcp-orchestrator-inventory"
    note "  kubectl -n $NS rollout restart statefulset/mcp-orchestrator"
}

do_verify_payload() {   # $1 = payload, $2 = "keys" to skip the PDP comparison
    local payload="$1" scope="${2:-all}" sj bad=0
    sj="$(kc get secret "$SECRET" -o json)" || die "cannot read $NS/$SECRET"
    for k in "${KEYS[@]}"; do
        local want have
        want="$(jq -r --arg k "$k" '.data[$k] // ""' <<<"$payload")"
        have="$(jq -r --arg k "$k" '.data[$k] // ""' <<<"$sj")"
        [ -z "$want" ] && continue
        if [ "$want" = "$have" ]; then echo "MATCH   $k"; else echo "DIFFER  $k"; bad=1; fi
    done
    # External PDP: same mode, scope, fail mode — and, for bundled OPA, same policy.
    local want_pdp have_pdp
    want_pdp="$(jq -c '.pdp // null' <<<"$payload")"
    if [ "$scope" != "keys" ] && [ "$want_pdp" != "null" ]; then
        have_pdp="$(pdp_json)"
        if [ "$have_pdp" = "null" ]; then
            echo "DIFFER  EXTERNAL_PDP (not configured in this region)"; bad=1
        else
            local f
            for f in mode namespaces fail policy_b64 data_b64; do
                local w h label
                w="$(jq -r --arg f "$f" '.[$f] // ""' <<<"$want_pdp")"
                h="$(jq -r --arg f "$f" '.[$f] // ""' <<<"$have_pdp")"
                label="PDP_$(tr '[:lower:]' '[:upper:]' <<<"${f%_b64}")"
                if [ "$w" = "$h" ]; then echo "MATCH   $label"; else echo "DIFFER  $label"; bad=1; fi
            done
        fi
    fi
    return $bad
}

do_verify() { local payload; payload="$(decrypt "$1")"; do_verify_payload "$payload"; }

# For install.sh: write the PDP settings the bundle carries into <dir>:
#   <dir>/pdp.env          PDP_MODE, PDP_URL, PDP_NAMESPACES, PDP_FAIL
#   <dir>/opa-policy.rego  (bundled mode only)
#   <dir>/opa-data.json    (bundled mode, when the primary has a data file)
# Writes nothing when the bundle carries no PDP. Never touches the cluster.
do_pdp() {
    local payload; payload="$(decrypt "$1")"
    local dir="$2"
    mkdir -p "$dir"
    local pdp; pdp="$(jq -c '.pdp // null' <<<"$payload")"
    [ "$pdp" = "null" ] && return 0
    {
        printf 'PDP_MODE=%q\n'       "$(jq -r .mode <<<"$pdp")"
        printf 'PDP_URL=%q\n'        "$(jq -r .url <<<"$pdp")"
        printf 'PDP_NAMESPACES=%q\n' "$(jq -r .namespaces <<<"$pdp")"
        printf 'PDP_FAIL=%q\n'       "$(jq -r .fail <<<"$pdp")"
    } > "$dir/pdp.env"
    if [ "$(jq -r .mode <<<"$pdp")" = "bundled" ]; then
        jq -r '.policy_b64' <<<"$pdp" | base64 -d > "$dir/opa-policy.rego"
        # Bundles made before data files were carried have no data_b64.
        if [ -n "$(jq -r '.data_b64 // ""' <<<"$pdp")" ]; then
            jq -r '.data_b64' <<<"$pdp" | base64 -d > "$dir/opa-data.json"
        fi
    fi
}

case "${1:-}" in
    export) [ $# -eq 2 ] || usage 1; do_export "$2" ;;
    import) [ $# -eq 2 ] || usage 1; do_import "$2" ;;
    verify) [ $# -eq 2 ] || usage 1; do_verify "$2" ;;
    pdp)    [ $# -eq 3 ] || usage 1; do_pdp "$2" "$3" ;;
    -h|--help|help|"") usage 0 ;;
    *) usage 1 ;;
esac
