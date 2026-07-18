#!/bin/bash
# Magertron MCP Orchestrator — fleet restore / reconcile helper.
#
# Rebuilds the Kubernetes workloads for MCP servers that exist in the
# Magertron registry but have no pods running.
#
# WHEN YOU NEED THIS
#
#   You restored a Magertron database onto a different cluster (DR, hardware
#   migration, staging refresh). The registry has your servers; Kubernetes has
#   never heard of them. Magertron's UI and API show them as "Running" because
#   they read the database — but the gateway 503s, because there are no pods.
#
#   Magertron loads the registry from the database on startup, but it does NOT
#   ask Kubernetes whether the workloads exist. The database records what was
#   deployed, not what IS deployed. On a normal cluster those are the same
#   thing, because the code that writes the row also creates the pod. A restore
#   is the case where they diverge.
#
#   This script closes that gap: it reads the registry, asks Kubernetes what's
#   actually there, and re-deploys what's missing.
#
# IT ALSO CHECKS CREDENTIALS
#
#   Magertron stores a *reference* to a Kubernetes Secret, never the secret
#   itself. That is deliberate — a database dump carrying plaintext vendor keys
#   would be a liability, not a feature — and it means credentials NEVER travel
#   with a restore. External servers come back pointing at secrets that do not
#   exist on the new cluster.
#
#   Those fail quietly. An internal server with no pod 503s at the gateway and
#   you know instantly. An external server with a dangling credential reference
#   shows "Active" in the UI and fails one request at a time, at injection.
#
#   This script tells you which references are dangling. It cannot fill them in
#   — the values live in your secret store, and Magertron never had them.
#
# WHEN YOU DO *NOT* NEED THIS
#
#   - Normal upgrades (./install.sh --mode upgrade). Your pods keep running.
#   - A single server that won't start. That's a deployment problem, not a
#     registry problem — check `kubectl describe` first.
#   - Orphaned pods (workloads with no registry row). This script does not
#     delete anything it did not create. See --help for why.
#
# Usage:
#   ./restore.sh                      # dry run — report only, changes nothing
#   ./restore.sh --apply              # create the missing workloads
#   ./restore.sh --apply --force      # also rebuild half-deployed servers
#
# Required environment:
#   MAG_URL          Gateway base URL, e.g. https://magertron.example.com:30443
#   MAG_TOKEN_FILE   Path to a file containing a service-account JWT
#
# Optional environment:
#   MAG_NAMESPACE    Orchestrator namespace          (default: mcp-system)
#   MAG_INSECURE     Skip TLS verify: 1 or 0         (default: 0)
#   MAG_TIMEOUT      Per-request timeout, seconds    (default: 30)
#
# Getting a token:
#   Magertron's admin login is two-phase (password, then IdP MFA), so it can't
#   be scripted. Mint a service account instead:
#     1. UI → Settings → Service Accounts → Create
#     2. Role: system:platform-admin
#     3. Save the JWT to a file, chmod 600
#     4. export MAG_TOKEN_FILE=/path/to/that/file
#   The SA is revocable and its actions are audited under its own subject —
#   do not reuse a human's token.
#
# Example:
#   export MAG_URL=https://192.168.1.150:30443
#   export MAG_TOKEN_FILE=/etc/mcp/restore-sa.key
#   export MAG_INSECURE=1              # self-signed cert on a lab cluster
#   ./restore.sh                       # look first
#   ./restore.sh --apply               # then act
#
# ── READ THIS BEFORE YOU RUN IT ─────────────────────────────────────────────
#
# Resource limits on servers deployed before chart 3.1.x are NOT in the
# database. Those releases persisted the routing fields but not cpu/memory/
# scaling, so this script re-creates such servers at the platform defaults
# (cpu_limit 1.0, memory_limit_mb 512, max_replicas 10). If you tuned a server
# and it predates 3.1.x, that tuning is gone — the only surviving copy was the
# live Deployment. Check before you rely on this:
#
#   kubectl get deploy -n <namespace> <name> \
#     -o jsonpath='{.spec.template.spec.containers[0].resources}'
#
# Servers deployed on 3.1.x and later round-trip completely.
#
set -euo pipefail

# ─── Polish: colors, glyphs (mirrors install.sh) ─────────────────────────────
if [ -t 1 ]; then IS_TTY=1; else IS_TTY=0; fi

if [ -n "${NO_COLOR:-}" ] || [ "$IS_TTY" = "0" ]; then
    C_RESET="" C_BOLD="" C_DIM=""
    C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN="" C_MAGENTA=""
else
    C_RESET=$'\033[0m'      C_BOLD=$'\033[1m'      C_DIM=$'\033[2m'
    C_RED=$'\033[31m'       C_GREEN=$'\033[32m'    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'      C_CYAN=$'\033[36m'     C_MAGENTA=$'\033[35m'
fi

case "${LANG:-}${LC_ALL:-}" in
    *UTF-8*|*utf8*|*UTF8*)
        G_CHECK="✓" G_CROSS="✗" G_WARN="⚠" G_ARROW="→" G_BULLET="•"
        ;;
    *)
        G_CHECK="OK" G_CROSS="X" G_WARN="!" G_ARROW=">" G_BULLET="*"
        ;;
esac

STEP_CURRENT=0
STEP_TOTAL=6

section() {
    STEP_CURRENT=$((STEP_CURRENT + 1))
    local label="$1"
    echo ""
    printf "${C_CYAN}${C_BOLD}[%d/%d] %s${C_RESET}\n" "$STEP_CURRENT" "$STEP_TOTAL" "$label"
    printf "${C_DIM}%s${C_RESET}\n" "$(printf '%.0s─' $(seq 1 60))"
}

ok()   { printf "  ${C_GREEN}${G_CHECK}${C_RESET} %s\n" "$*"; }
warn() { printf "  ${C_YELLOW}${G_WARN}${C_RESET} %s\n" "$*"; }
err()  { printf "  ${C_RED}${G_CROSS}${C_RESET} %s\n" "$*" >&2; }
info() { printf "  ${C_DIM}%s${C_RESET}\n" "$*"; }
note() { printf "  ${C_DIM}%s${C_RESET} %s\n" "$G_ARROW" "$*"; }

die() { err "$*"; exit 1; }

# ─── Usage ───────────────────────────────────────────────────────────────────
usage() {
    cat <<'RESTORE_USAGE_EOF'
Magertron fleet restore — rebuild MCP server workloads from the registry.

USAGE
    ./restore.sh [--apply] [--force] [--namespace NS] [--only NS/NAME]

OPTIONS
    --apply           Actually create the missing workloads.
                      Without this, the script only reports (default).

    --force           Also rebuild servers that are half-deployed (a
                      Deployment without a Service, or vice versa). This
                      DELETES the surviving half at the Kubernetes layer and
                      re-creates both. It does not touch the registry row or
                      any billing history.

                      Half-deployed servers are refused without this flag
                      because re-deploying over one fails partway and leaves
                      more wreckage than it clears.

    --namespace NS    Orchestrator namespace (default: mcp-system, or
                      $MAG_NAMESPACE).

    --only NS/NAME    Reconcile a single server. Repeatable.

    -h, --help        This text.

ENVIRONMENT
    MAG_URL           (required) Gateway base URL.
    MAG_TOKEN_FILE    (required) File holding a service-account JWT.
    MAG_NAMESPACE     Orchestrator namespace. Default mcp-system.
    MAG_INSECURE      1 to skip TLS verification. Default 0.
    MAG_TIMEOUT       Per-request timeout in seconds. Default 30.

CREDENTIALS
    Every run also checks that each server's credential_secret_ref names a
    Secret that actually exists. It does this even when the workloads are
    fine, because a restored cluster commonly has healthy pods and no
    secrets — and that combination looks green in the UI while every call
    to an external server fails at injection.

    It reports; it cannot repair. Magertron stores a reference, never a
    value, so the credentials were never in the dump. Recreate them from
    your secret store.

    Missing secrets do not fail the run (exit code stays 0). Bringing the
    fleet up first and wiring credentials second is a legitimate order.

WHAT IT DOES NOT DO
    - It does not delete orphaned workloads (pods with no registry row).
      Those are usually servers from a previous install that Helm never
      owned. Removing them is a judgement call about someone's running
      traffic, so it stays manual.

    - It does not fix drift. A server whose live pod differs from its
      registry row is left alone; this script only acts on absence.

    - It does not restore external/REST servers, which have no workload by
      design. They are proxy registrations — a row IS the whole server.

EXIT CODES
    0  nothing to do, or --apply succeeded
    1  usage / environment error
    2  reconcile needed (dry-run found missing servers)
    3  one or more deploys failed
RESTORE_USAGE_EOF
}

# ─── Argument parsing ────────────────────────────────────────────────────────
APPLY=0
FORCE=0
declare -a ONLY=()
MAG_NAMESPACE="${MAG_NAMESPACE:-mcp-system}"

while [ $# -gt 0 ]; do
    case "$1" in
        --apply)      APPLY=1; shift ;;
        --force)      FORCE=1; shift ;;
        --namespace)  MAG_NAMESPACE="${2:?--namespace needs a value}"; shift 2 ;;
        --only)       ONLY+=("${2:?--only needs NS/NAME}"); shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *)            err "unknown option: $1"; echo ""; usage; exit 1 ;;
    esac
done

# ─── Preflight ───────────────────────────────────────────────────────────────
section "Preflight"

# Required env, checked before anything else so the failure is obvious rather
# than surfacing as a confusing 401 six steps later.
[ -n "${MAG_URL:-}" ] || {
    err "MAG_URL is not set."
    info ""
    info "  export MAG_URL=https://your-gateway:30443"
    info ""
    info "This is the same URL you use for the Magertron UI, including the port."
    exit 1
}

[ -n "${MAG_TOKEN_FILE:-}" ] || {
    err "MAG_TOKEN_FILE is not set."
    info ""
    info "  export MAG_TOKEN_FILE=/path/to/service-account.jwt"
    info ""
    info "Magertron's admin login requires interactive MFA and cannot be"
    info "scripted. Mint a service account instead:"
    info "  UI → Settings → Service Accounts → Create"
    info "  Role: system:platform-admin"
    info "Save the JWT to a file (chmod 600) and point MAG_TOKEN_FILE at it."
    exit 1
}

[ -r "$MAG_TOKEN_FILE" ] || die "token file not readable: $MAG_TOKEN_FILE"
[ -s "$MAG_TOKEN_FILE" ] || die "token file is empty: $MAG_TOKEN_FILE"

TOKEN="$(tr -d '[:space:]' < "$MAG_TOKEN_FILE")"
[ -n "$TOKEN" ] || die "token file contains only whitespace: $MAG_TOKEN_FILE"

# A JWT has three dot-separated parts. Catch a pasted password or a truncated
# copy here rather than as an opaque 401.
case "$TOKEN" in
    *.*.*) : ;;
    *) die "token file does not look like a JWT (expected three dot-separated parts): $MAG_TOKEN_FILE" ;;
esac

MAG_TIMEOUT="${MAG_TIMEOUT:-30}"
CURL_OPTS=(--silent --show-error --max-time "$MAG_TIMEOUT")
if [ "${MAG_INSECURE:-0}" = "1" ]; then
    CURL_OPTS+=(--insecure)
    warn "TLS verification disabled (MAG_INSECURE=1)"
fi

for bin in curl kubectl python3; do
    command -v "$bin" >/dev/null 2>&1 || die "required binary not found: $bin"
done
ok "curl, kubectl, python3 present"

kubectl get ns "$MAG_NAMESPACE" >/dev/null 2>&1 \
    || die "cannot reach namespace '$MAG_NAMESPACE' — is your kubeconfig pointed at the right cluster?"
ok "kubectl can reach $MAG_NAMESPACE"

# Verify the token before doing anything with it.
HTTP_CODE="$(curl "${CURL_OPTS[@]}" -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    "$MAG_URL/api/v1/servers" || echo "000")"

case "$HTTP_CODE" in
    200) ok "authenticated to $MAG_URL" ;;
    000) die "cannot reach $MAG_URL — check MAG_URL, network, and MAG_INSECURE" ;;
    401) die "token rejected (401). It may be expired or revoked — mint a new service account." ;;
    403) die "token authenticated but lacks permission (403). The SA needs role system:platform-admin." ;;
    *)   die "unexpected response from $MAG_URL/api/v1/servers: HTTP $HTTP_CODE" ;;
esac

if [ "$APPLY" = "1" ]; then
    warn "APPLY mode — this will create Kubernetes workloads"
else
    info "DRY RUN — nothing will be changed. Re-run with --apply to act."
fi

# ─── Fetch the registry ──────────────────────────────────────────────────────
section "Reading the registry"

SERVERS_JSON="$(curl "${CURL_OPTS[@]}" -H "Authorization: Bearer $TOKEN" \
    "$MAG_URL/api/v1/servers")" || die "failed to list servers"

# Pod-backed servers only. external/rest rows are proxy registrations with no
# workload by design — the type invariant in the schema guarantees they carry
# an endpoint_url and no image, so there is nothing here to reconcile.
WORKLOAD_SERVERS="$(printf '%s' "$SERVERS_JSON" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except Exception as e:
    sys.stderr.write("could not parse /api/v1/servers response: %s\n" % e)
    sys.exit(1)
rows = data.get("servers", data) if isinstance(data, dict) else data
if not isinstance(rows, list):
    sys.stderr.write("unexpected shape from /api/v1/servers\n")
    sys.exit(1)
for s in rows:
    if s.get("server_type") in ("internal", "hybrid"):
        print("%s\t%s" % (s.get("namespace",""), s.get("name","")))
')" || die "failed to parse server list"

# NOTE: no early exit here. Even with zero pod-backed servers there may be
# external servers with dangling credential references, and that is exactly the
# failure this script exists to surface. Skipping to "nothing to do" because the
# workload half is clean is how a restore looks healthy and is not.
SKIP_WORKLOADS=0
if [ -z "$WORKLOAD_SERVERS" ]; then
    info "No pod-backed servers in the registry — no workloads to reconcile."
    SKIP_WORKLOADS=1
fi

if [ "$SKIP_WORKLOADS" = "0" ]; then
    TOTAL_COUNT="$(printf '%s\n' "$WORKLOAD_SERVERS" | wc -l | tr -d ' ')"
    ok "$TOTAL_COUNT pod-backed server(s) in the registry"
fi

# --only filter
if [ "${#ONLY[@]}" -gt 0 ]; then
    FILTERED=""
    for want in "${ONLY[@]}"; do
        w_ns="${want%%/*}"
        w_name="${want##*/}"
        [ "$w_ns" != "$want" ] || die "--only expects NS/NAME, got: $want"
        match="$(printf '%s\n' "$WORKLOAD_SERVERS" | awk -F'\t' -v n="$w_ns" -v m="$w_name" '$1==n && $2==m')"
        [ -n "$match" ] || die "--only $want is not a pod-backed server in the registry"
        FILTERED="${FILTERED}${match}"$'\n'
    done
    WORKLOAD_SERVERS="$(printf '%s' "$FILTERED" | sed '/^$/d')"
    note "limited to ${#ONLY[@]} server(s) by --only"
fi

# ─── Classify ────────────────────────────────────────────────────────────────
section "Comparing registry against the cluster"

declare -a MISSING=()
declare -a PARTIAL=()
declare -a HEALTHY=()
# Declared here rather than in the credential section: that section only
# populates it inside a conditional, and `set -u` makes an unset array fatal
# when the Result section reads it. A registry with no credential refs at all
# is a legitimate state, not a crash.
declare -a MISSING_SECRETS=()

while IFS=$'\t' read -r ns name; do
    [ -n "$ns" ] || continue
    have_deploy=0
    have_svc=0
    kubectl get deploy -n "$ns" "$name" >/dev/null 2>&1 && have_deploy=1
    kubectl get svc    -n "$ns" "$name" >/dev/null 2>&1 && have_svc=1

    if [ "$have_deploy" = "1" ] && [ "$have_svc" = "1" ]; then
        HEALTHY+=("$ns/$name")
    elif [ "$have_deploy" = "0" ] && [ "$have_svc" = "0" ]; then
        MISSING+=("$ns/$name")
    else
        PARTIAL+=("$ns/$name|deploy=$have_deploy,svc=$have_svc")
    fi
done <<< "$WORKLOAD_SERVERS"

for h in "${HEALTHY[@]:-}"; do [ -n "$h" ] && ok "$h — workload present"; done
for m in "${MISSING[@]:-}"; do [ -n "$m" ] && warn "$m — registry row, no workload"; done
for p in "${PARTIAL[@]:-}"; do
    [ -n "$p" ] || continue
    err "${p%%|*} — half deployed (${p##*|})"
done

echo ""
info "healthy: ${#HEALTHY[@]}   missing: ${#MISSING[@]}   half-deployed: ${#PARTIAL[@]}"

if [ "${#PARTIAL[@]}" -gt 0 ] && [ "$FORCE" != "1" ]; then
    echo ""
    err "Refusing to touch half-deployed servers without --force."
    info ""
    info "A half-deployed server has one of its two Kubernetes objects. Re-"
    info "deploying over it fails partway — the create that collides aborts the"
    info "rest, leaving more wreckage than it cleared."
    info ""
    info "  --force deletes the surviving half and rebuilds both. It touches"
    info "  only Kubernetes objects; your registry row, billing history, and"
    info "  audit trail are untouched."
    info ""
    info "Re-run with --apply --force, or clean them by hand:"
    for p in "${PARTIAL[@]}"; do
        info "  kubectl delete deploy,svc -n ${p%%/*} ${p#*/} --ignore-not-found"
    done
    exit 1
fi

NOTHING_TO_DO=0
if [ "${#MISSING[@]}" = "0" ] && [ "${#PARTIAL[@]}" = "0" ]; then
    echo ""
    ok "Registry and cluster agree on workloads."
    NOTHING_TO_DO=1
fi

DRY_RUN_PENDING=0
if [ "$NOTHING_TO_DO" = "0" ] && [ "$APPLY" != "1" ]; then
    echo ""
    note "Dry run: ${#MISSING[@]} server(s) would be created."
    [ "${#PARTIAL[@]}" -gt 0 ] && note "${#PARTIAL[@]} server(s) would be rebuilt (--force)."
    note "Re-run with --apply to make these changes."
    DRY_RUN_PENDING=1
fi

# ─── Rebuild ─────────────────────────────────────────────────────────────────
FAILED=0
SUCCEEDED=0
DID_REBUILD=0

if [ "$NOTHING_TO_DO" = "0" ] && [ "$DRY_RUN_PENDING" = "0" ]; then
section "Rebuilding workloads"
DID_REBUILD=1

rebuild_one() {
    local ns="$1" name="$2" is_partial="$3"

    # Pull the full spec back. GET /servers/{ns}/{name} returns the registry
    # row rebuilt into spec shape — this is the same reconstruction the
    # orchestrator does at startup, so what we POST is what it stored.
    local spec
    spec="$(curl "${CURL_OPTS[@]}" -H "Authorization: Bearer $TOKEN" \
        "$MAG_URL/api/v1/servers/$ns/$name")" || {
        err "$ns/$name — could not fetch spec"
        return 1
    }

    # Strip the observed/derived fields. The GET response carries live state
    # (health, cluster_ip, ready_replicas) and identity (id, state) alongside
    # the spec; POSTing those back is at best ignored and at worst rejected.
    # Whitelist rather than blacklist: a field added to the response later
    # should not silently start leaking into deploy requests.
    local body
    body="$(printf '%s' "$spec" | python3 -c '
import sys, json
s = json.load(sys.stdin)
KEEP = ("name","namespace","server_type","image","image_tag","replicas",
        "mcp_port","transport","upstream_path","routing_mode","group_name",
        "env_vars","labels","annotations","command","args",
        "cpu_request","cpu_limit","memory_request_mb","memory_limit_mb",
        "min_replicas","max_replicas","chart_name","chart_version",
        "auth_type","credential_secret_ref","credential_mode",
        "upstream_http_version","tls_min_version","static_headers")
out = {k: s[k] for k in KEEP if k in s and s[k] not in ("", None)}
# type is what the deploy endpoint keys on; server_type is the stored column.
out["type"] = s.get("server_type", "internal")
json.dump(out, sys.stdout)
')" || { err "$ns/$name — could not build deploy body"; return 1; }

    if [ "$is_partial" = "1" ]; then
        note "$ns/$name — removing half-deployed objects"
        kubectl delete deploy,svc -n "$ns" "$name" --ignore-not-found >/dev/null 2>&1 || true
        # Deletion is not synchronous; a create racing a terminating object
        # gets AlreadyExists. Wait for both to actually go.
        local waited=0
        while kubectl get deploy -n "$ns" "$name" >/dev/null 2>&1 || \
              kubectl get svc    -n "$ns" "$name" >/dev/null 2>&1; do
            sleep 1
            waited=$((waited + 1))
            [ "$waited" -lt 30 ] || { err "$ns/$name — objects still present after 30s"; return 1; }
        done
    fi

    local code
    code="$(printf '%s' "$body" | curl "${CURL_OPTS[@]}" -o /tmp/restore-resp.$$ -w '%{http_code}' \
        -X POST "$MAG_URL/api/v1/servers" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        --data-binary @- || echo "000")"

    case "$code" in
        200|201)
            ok "$ns/$name — deployed"
            rm -f /tmp/restore-resp.$$
            return 0
            ;;
        409)
            # Something exists that we did not see during classification —
            # most likely a concurrent deploy, or an object we do not check.
            err "$ns/$name — conflict (409): something already exists"
            info "    $(head -c 200 /tmp/restore-resp.$$ 2>/dev/null)"
            rm -f /tmp/restore-resp.$$
            return 1
            ;;
        *)
            err "$ns/$name — deploy failed (HTTP $code)"
            info "    $(head -c 200 /tmp/restore-resp.$$ 2>/dev/null)"
            rm -f /tmp/restore-resp.$$
            return 1
            ;;
    esac
}

for m in "${MISSING[@]:-}"; do
    [ -n "$m" ] || continue
    if rebuild_one "${m%%/*}" "${m#*/}" 0; then
        SUCCEEDED=$((SUCCEEDED + 1))
    else
        FAILED=$((FAILED + 1))
    fi
done

if [ "$FORCE" = "1" ]; then
    for p in "${PARTIAL[@]:-}"; do
        [ -n "$p" ] || continue
        entry="${p%%|*}"
        if rebuild_one "${entry%%/*}" "${entry#*/}" 1; then
            SUCCEEDED=$((SUCCEEDED + 1))
        else
            FAILED=$((FAILED + 1))
        fi
    done
fi

fi   # end rebuild guard

# ─── Credentials ─────────────────────────────────────────────────────────────
# Always runs — including when the workload half is clean. A restore where the
# pods are fine but the secrets are absent looks healthy and is not.
section "Checking credentials"

# Magertron stores credential_secret_ref — a NAME, not a value. The secret
# itself lives in Kubernetes (or whatever your secret store projects into it).
# A pg_dump carries the reference and never the secret, which is the correct
# design: a backup file containing your Stripe key would be a breach waiting
# to be copied onto someone's laptop.
#
# The consequence: a restored cluster has rows pointing at secrets that were
# never created there. This is the external-server equivalent of a registry row
# with no pod — except it fails far more quietly. No pod means a 503 and you
# know in seconds. A dangling credential reference means the UI shows "Active"
# in green while every call dies at injection.
#
# We can detect that. We cannot fix it: the values are yours, and Magertron
# never held them in plaintext to restore.
CRED_ROWS="$(printf '%s' "$SERVERS_JSON" | python3 -c '
import sys, json
data = json.load(sys.stdin)
rows = data.get("servers", data) if isinstance(data, dict) else data
for s in rows:
    ref = (s.get("credential_secret_ref") or "").strip()
    if ref:
        print("%s\t%s\t%s\t%s" % (s.get("namespace",""), s.get("name",""),
                                  ref, s.get("auth_type","") or "-"))
')" || die "failed to parse credential references"

if [ -z "$CRED_ROWS" ]; then
    info "No servers reference a credential secret — nothing to check."
else
    CRED_OK=0

    while IFS=$'\t' read -r c_ns c_name c_ref c_auth; do
        [ -n "$c_ns" ] || continue
        if kubectl get secret -n "$c_ns" "$c_ref" >/dev/null 2>&1; then
            ok "$c_ns/$c_name — $c_ref present"
            CRED_OK=$((CRED_OK + 1))
        else
            err "$c_ns/$c_name — secret '$c_ref' NOT FOUND (auth_type: $c_auth)"
            MISSING_SECRETS+=("$c_ns|$c_name|$c_ref|$c_auth")
        fi
    done <<< "$CRED_ROWS"

    echo ""
    info "credentials present: $CRED_OK   missing: ${#MISSING_SECRETS[@]}"

    if [ "${#MISSING_SECRETS[@]}" -gt 0 ]; then
        echo ""
        warn "These servers will accept traffic and fail at credential injection."
        warn "The UI will show them Active. Recreate the secrets from your secret"
        warn "store before trusting this cluster."
        echo ""

        for entry in "${MISSING_SECRETS[@]}"; do
            IFS='|' read -r c_ns c_name c_ref c_auth <<< "$entry"
            printf "  ${C_BOLD}%s/%s${C_RESET} ${C_DIM}(%s)${C_RESET}\n" "$c_ns" "$c_name" "$c_auth"
            case "$c_auth" in
                bearer)
                    info "    kubectl create secret generic $c_ref -n $c_ns \\"
                    info "      --from-literal=token=<bearer token>"
                    ;;
                api-key)
                    info "    kubectl create secret generic $c_ref -n $c_ns \\"
                    info "      --from-literal=header_name=<e.g. X-API-Key> \\"
                    info "      --from-literal=key=<the api key>"
                    ;;
                oauth2-client-credentials)
                    # Machine-to-machine. Magertron holds ONE credential and
                    # calls the vendor as itself — no user, no browser, no
                    # redirect_uri. Recreating the secret is genuinely all
                    # that is needed here, unlike the authorization-code case
                    # below.
                    info "    kubectl create secret generic $c_ref -n $c_ns \\"
                    info "      --from-literal=client_id=<client id> \\"
                    info "      --from-literal=client_secret=<client secret> \\"
                    info "      --from-literal=token_endpoint=<token endpoint URL>"
                    info ""
                    info "    Some vendors also want discovery_url / auth_server_url —"
                    info "    copy whatever keys the original secret carried."
                    ;;
                oauth2-authorization-code)
                    info "    kubectl create secret generic $c_ref -n $c_ns \\"
                    info "      --from-literal=client_id=<client id> \\"
                    info "      --from-literal=client_secret=<client secret> \\"
                    info "      --from-literal=discovery_url=<...> \\"
                    info "      --from-literal=auth_server_url=<...> \\"
                    info "      --from-literal=authorization_endpoint=<...> \\"
                    info "      --from-literal=token_endpoint=<...>"
                    echo ""
                    warn "Re-creating this secret is NOT enough."
                    info "    This is delegated (per-user) authorization: a browser round"
                    info "    trip sends the user to the vendor and back to a redirect_uri"
                    info "    built from your public_base_url. The client_id was registered"
                    info "    with the vendor against your PREVIOUS base URL, so on a new"
                    info "    cluster that callback no longer resolves — the flow fails"
                    info "    AFTER authenticating, which reads like a vendor outage rather"
                    info "    than a config problem."
                    info ""
                    info "    Update the registered redirect_uri with the vendor (or"
                    info "    re-register the client), then re-run delegated authorize for"
                    info "    each user who had authorized this server."
                    ;;
                mtls)
                    info "    kubectl create secret generic $c_ref -n $c_ns \\"
                    info "      --from-file=tls.crt=<path> --from-file=tls.key=<path>"
                    ;;
                *)
                    info "    Secret shape depends on auth_type '$c_auth' — see the"
                    info "    External Servers section of the User Guide."
                    ;;
            esac
            echo ""
        done

        info "Key names above are what the ext_authz credential resolver expects."
        info "A secret with the right name but the wrong keys fails exactly like a"
        info "missing one — check both if a server still will not authenticate."
    fi
fi

# ─── Report ──────────────────────────────────────────────────────────────────
section "Result"

if [ "$DID_REBUILD" = "1" ]; then
    ok "$SUCCEEDED server(s) deployed"
    [ "$FAILED" -gt 0 ] && err "$FAILED server(s) failed"
    echo ""
    info "Pods take a moment to pull images and pass readiness. Watch them come up:"
    for m in "${MISSING[@]:-}"; do
        [ -n "$m" ] || continue
        info "  kubectl get pods -n ${m%%/*} -l app=${m#*/} -w"
        break
    done
    info ""
    info "Then confirm the gateway routes to them — the registry said Running all"
    info "along, so the only honest check is a real call through the gateway."
elif [ "$DRY_RUN_PENDING" = "1" ]; then
    info "Dry run — no workloads were changed. Re-run with --apply."
else
    ok "Workloads already match the registry."
fi

# Credential gaps do NOT fail the run. A staged restore — bring the fleet up
# first, wire credentials from the secret store second — is a legitimate order
# to work in, and the workload rebuild genuinely succeeded. The warning above
# is loud enough; a non-zero exit here would just train people to ignore it.
if [ "${#MISSING_SECRETS[@]}" -gt 0 ]; then
    echo ""
    warn "${#MISSING_SECRETS[@]} credential secret(s) still missing — see above."
    info "Those servers are reachable but cannot authenticate to their upstream."
fi

if [ "$FAILED" -gt 0 ]; then
    exit 3
fi
if [ "$DRY_RUN_PENDING" = "1" ]; then
    exit 2
fi
exit 0
