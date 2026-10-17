#!/usr/bin/env bash
# ─── region-bundle.sh — carry the shared platform keys to a second region ────
#
# Every Magertron™ region must share the same platform keys, or a replica
# region rejects the primary's tokens, cannot verify its evidence records,
# cannot log into its own (replicated) databases, and cannot decrypt
# replicated credentials. This script moves those keys as ONE encrypted file.
#
#   On the PRIMARY (EAST):   ./region-bundle.sh export east.bundle
#   On the REPLICA (WEST):   ./region-bundle.sh import east.bundle
#                      or:   ./install.sh ... --replica-of host:port --region-bundle east.bundle
#   Either region:           ./region-bundle.sh verify east.bundle
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
KEYS=(MCP_JWT_PRIVATE_KEY MCP_JWT_PUBLIC_KEY MCP_EVIDENCE_PRIVATE_KEY
      MCP_DB_PASSWORD MCP_INVENTORY_DB_PASSWORD MAG_SECRET_ENCRYPTION_KEY)
# Must be present and non-empty in the primary's Secret.
REQUIRED=(MCP_JWT_PRIVATE_KEY MCP_EVIDENCE_PRIVATE_KEY MCP_DB_PASSWORD
          MCP_INVENTORY_DB_PASSWORD MAG_SECRET_ENCRYPTION_KEY)

die()  { echo "region-bundle: ERROR: $*" >&2; exit 1; }
note() { echo "region-bundle: $*"; }
kc()   { kubectl ${KUBE_CONTEXT:+--context "$KUBE_CONTEXT"} -n "$NS" "$@"; }

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

for t in kubectl jq openssl; do
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

do_export() {
    local out="$1"
    [ -e "$out" ] && die "$out already exists — choose another name"
    local sj; sj="$(kc get secret "$SECRET" -o json)" || die "cannot read $NS/$SECRET (wrong context?)"
    for k in "${REQUIRED[@]}"; do
        [ -n "$(jq -r --arg k "$k" '.data[$k] // empty' <<<"$sj")" ] \
            || die "$k is missing from $NS/$SECRET — upgrade this region to a chart that generates it, then retry"
    done
    get_pass 1
    local payload
    payload="$(jq -c --argjson ks "$(keys_json)" \
        '{v:1, secret:.metadata.name, data: (.data as $d | reduce $ks[] as $k ({}; . + {($k): ($d[$k] // "")}))}' <<<"$sj")"
    ( umask 077; printf '%s' "$payload" | openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:RB_PASS -out "$out" )
    unset RB_PASS
    note "exported ${#KEYS[@]} keys from ${KUBE_CONTEXT:-current context} $NS/$SECRET -> $out"
    note "move $out to the replica region; delete it after the import."
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
    do_verify_payload "$payload"
    note "restart inventory and the orchestrator if they are already running:"
    note "  kubectl -n $NS rollout restart deploy/mcp-orchestrator-inventory"
    note "  kubectl -n $NS rollout restart statefulset/mcp-orchestrator"
}

do_verify_payload() {
    local payload="$1" sj bad=0
    sj="$(kc get secret "$SECRET" -o json)" || die "cannot read $NS/$SECRET"
    for k in "${KEYS[@]}"; do
        local want have
        want="$(jq -r --arg k "$k" '.data[$k] // ""' <<<"$payload")"
        have="$(jq -r --arg k "$k" '.data[$k] // ""' <<<"$sj")"
        [ -z "$want" ] && continue
        if [ "$want" = "$have" ]; then echo "MATCH   $k"; else echo "DIFFER  $k"; bad=1; fi
    done
    return $bad
}

do_verify() { local payload; payload="$(decrypt "$1")"; do_verify_payload "$payload"; }

case "${1:-}" in
    export) [ $# -eq 2 ] || usage 1; do_export "$2" ;;
    import) [ $# -eq 2 ] || usage 1; do_import "$2" ;;
    verify) [ $# -eq 2 ] || usage 1; do_verify "$2" ;;
    -h|--help|help|"") usage 0 ;;
    *) usage 1 ;;
esac
