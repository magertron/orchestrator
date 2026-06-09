#!/bin/bash
# Magertron MCP Orchestrator — install / upgrade helper.
#
# Replaces the dev-era install.sh and reinstall.sh. Same script handles both
# fresh install and upgrade-in-place; the data-preservation behavior is
# controlled by --mode.
#
# Usage:
#   ./install.sh [--license <path>] [options]
#
# Quick examples:
#   # Free tier evaluation (no license required):
#   ./install.sh
#
#   # First-time install with license, NodePort, interactive node prompt:
#   ./install.sh --license ~/Downloads/license.json
#
#   # Upgrade in place, preserve data, pin chart:
#   ./install.sh --license ./license.json --mode upgrade --chart-version 2.5.3
#
#   # Fresh slate (destroys data!), LoadBalancer service:
#   ./install.sh --license ./license.json --mode reinstall --service-type loadbalancer
#
#   # CI / automation (no prompts):
#   ./install.sh --non-interactive
#
#   # Multi-node production with postgres pinning (opt-in):
#   ./install.sh --label-nodes --node-name worker-stateful-1 --non-interactive
#
# Two modes:
#
#   --mode upgrade   (default) — preserves data
#     - helm uninstall + orchestrator-resource cleanup is still done (required
#       for the orchestrator's NetworkPolicies, which live outside helm's
#       ownership; the chart's resource-policy: keep annotation on the
#       inventory PVC keeps the data through this dance)
#     - Keeps the mcp-system namespace (so PVCs stay bound)
#     - Keeps customer namespaces (so deployed MCP servers continue running)
#     - Keeps the license secret
#
#   --mode reinstall — destroys data, fresh slate
#     - Full teardown including mcp-system + customer namespaces
#     - Explicitly deletes the inventory PVC (resource-policy: keep means
#       we have to remove it manually if a clean start is wanted)
#     - Recreates the license secret from --license
#
# Why both modes run helm uninstall + resource cleanup:
# the orchestrator creates NetworkPolicies named mcp-server-isolation outside
# of helm's ownership. On `helm upgrade --install` these collide with helm's
# ownership-validation logic and the upgrade fails ("invalid ownership
# metadata"). The clean uninstall + resource cleanup + fresh install dance
# avoids the collision. The data-preservation delta is purely about what
# we leave alone during teardown.

set -euo pipefail

# ─── Polish: colors, glyphs, box drawing, spinners ──────────────────────────
# Pure-bash terminal polish with graceful degradation. Honors NO_COLOR env
# var (standard convention). Falls back to ASCII glyphs if the locale isn't
# UTF-8. Spinners and in-place updates disabled when stdout isn't a TTY
# (CI logs, piped output) or when --non-interactive is set.

# Detect TTY (stdout connected to terminal)
if [ -t 1 ]; then
    IS_TTY=1
else
    IS_TTY=0
fi

# Detect color support. NO_COLOR=anything disables. Otherwise enable if TTY.
if [ -n "${NO_COLOR:-}" ] || [ "$IS_TTY" = "0" ]; then
    C_RESET="" C_BOLD="" C_DIM=""
    C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN="" C_MAGENTA=""
else
    C_RESET=$'\033[0m'      C_BOLD=$'\033[1m'      C_DIM=$'\033[2m'
    C_RED=$'\033[31m'       C_GREEN=$'\033[32m'    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'      C_CYAN=$'\033[36m'     C_MAGENTA=$'\033[35m'
fi

# Detect Unicode support — if the locale claims UTF-8, use fancy glyphs;
# else fall back to ASCII so old terminals don't render boxes as `?` chars.
case "${LANG:-}${LC_ALL:-}" in
    *UTF-8*|*utf8*|*UTF8*)
        G_CHECK="✓" G_CROSS="✗" G_WARN="⚠" G_ARROW="→" G_BULLET="•"
        BX_TL="┌" BX_TR="┐" BX_BL="└" BX_BR="┘" BX_H="─" BX_V="│" BX_X="├" BX_Y="┤"
        ;;
    *)
        G_CHECK="OK" G_CROSS="X" G_WARN="!" G_ARROW=">" G_BULLET="*"
        BX_TL="+" BX_TR="+" BX_BL="+" BX_BR="+" BX_H="-" BX_V="|" BX_X="+" BX_Y="+"
        ;;
esac

# Progress step tracking — we update STEP_CURRENT as we go through the
# major install phases. STEP_TOTAL is the count of phases the user sees.
STEP_CURRENT=0
STEP_TOTAL=14   # default; recomputed after arg parsing based on flags

# ─── Output helpers ──────────────────────────────────────────────────────────

# Print a styled section header. Numbered if step tracking is on.
section() {
    STEP_CURRENT=$((STEP_CURRENT + 1))
    local label="$1"
    echo ""
    printf "${C_CYAN}${C_BOLD}[%d/%d] %s${C_RESET}\n" "$STEP_CURRENT" "$STEP_TOTAL" "$label"
    printf "${C_DIM}%s${C_RESET}\n" "$(printf '%.0s─' $(seq 1 60))"
}

# Status indicators — used inside a section for individual line items.
ok()   { printf "  ${C_GREEN}${G_CHECK}${C_RESET} %s\n" "$*"; }
warn() { printf "  ${C_YELLOW}${G_WARN}${C_RESET} %s\n" "$*"; }
err()  { printf "  ${C_RED}${G_CROSS}${C_RESET} %s\n" "$*" >&2; }
info() { printf "  ${C_DIM}%s${C_RESET}\n" "$*"; }
note() { printf "  ${C_DIM}%s${C_RESET} %s\n" "$G_ARROW" "$*"; }

# ─── Spinner helper ─────────────────────────────────────────────────────────
# Runs a background spinner with a label until killed. Used to indicate
# progress during slow operations whose own output may scroll past or
# arrive in bursts (e.g., helm uninstall).
#
# Usage:
#   spinner_start "Doing slow thing"
#   ... your slow commands ...
#   spinner_stop
#
# The spinner runs in a child shell, redrawing the same line via \r.
# Output from concurrent commands MAY interleave with the spinner — we
# don't try to capture it. Worst case the spinner's frame gets bumped to
# a new line, which still reads cleanly because the spinner is just a
# visual hint, not authoritative state.
SPINNER_PID=""
spinner_start() {
    local label="$1"
    # Skip if not a TTY (CI/non-interactive) — printing \r to a file is noise.
    if ! [ -t 1 ]; then
        info "$label"
        return
    fi
    local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
    (
        local i=0
        while :; do
            printf "\r  ${C_CYAN}%s${C_RESET} %s" "${frames[$((i % 10))]}" "$label"
            i=$((i + 1))
            sleep 0.1
        done
    ) &
    SPINNER_PID=$!
    # Disown so the kill doesn't print "[1] Terminated" noise later.
    disown "$SPINNER_PID" 2>/dev/null || true
}

spinner_stop() {
    if [ -n "$SPINNER_PID" ]; then
        kill "$SPINNER_PID" 2>/dev/null || true
        wait "$SPINNER_PID" 2>/dev/null || true
        SPINNER_PID=""
        # Clear the spinner line so trailing output renders cleanly.
        printf "\r%*s\r" "$(tput cols 2>/dev/null || echo 80)" ""
    fi
}

# A simple spinner that runs while a command executes. Disabled when not
# on a TTY (so CI logs aren't filled with \r noise) or in --non-interactive.
# Usage:  spinner "Waiting for rollout..." kubectl rollout status ...
spinner() {
    local msg="$1"; shift
    if [ "$IS_TTY" = "0" ] || [ "${NON_INTERACTIVE:-0}" = "1" ]; then
        printf "  %s\n" "$msg"
        "$@"
        return $?
    fi
    local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    case "${LANG:-}${LC_ALL:-}" in
        *UTF-8*|*utf8*|*UTF8*) ;;
        *) frames='|/-\\' ;;
    esac
    "$@" &
    local pid=$!
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        local frame="${frames:$((i % ${#frames})):1}"
        printf "\r  ${C_CYAN}%s${C_RESET} %s" "$frame" "$msg"
        i=$((i + 1))
        sleep 0.1
    done
    wait "$pid"
    local rc=$?
    # Clear the spinner line. Then print outcome.
    printf "\r%-$(($(tput cols 2>/dev/null || echo 80)))s\r" ""
    if [ "$rc" = "0" ]; then
        ok "$msg"
    else
        err "$msg (exit $rc)"
    fi
    return $rc
}

# Live rollout progress bar — polls all pods in the namespace whose name
# starts with the given prefix and counts how many are Ready. Truthful
# (not decorative) — reflects platform-wide readiness, not just one
# deployment.
#
# Usage:  rollout_progress <namespace> <pod-name-prefix> <timeout-seconds>
# Returns 0 if all pods become Ready, 1 on timeout.
#
# Falls back to a plain wait when stdout isn't a TTY (CI logs).
rollout_progress() {
    local ns="$1" prefix="$2" timeout="$3"
    local bar_width=24

    if [ "$IS_TTY" = "0" ] || [ "${NON_INTERACTIVE:-0}" = "1" ]; then
        # No fancy rendering in CI — wait for the main orchestrator
        # deployment via the canonical kubectl rollout status call.
        kubectl rollout status -n "$ns" "deploy/${prefix}" --timeout="${timeout}s"
        return $?
    fi

    # Pick bar glyphs based on Unicode support.
    local g_full g_empty
    case "${LANG:-}${LC_ALL:-}" in
        *UTF-8*|*utf8*|*UTF8*) g_full="█" g_empty="░" ;;
        *)                     g_full="#" g_empty="-" ;;
    esac

    local start_ts=$(date +%s)
    while :; do
        local now=$(date +%s)
        local elapsed=$((now - start_ts))

        # Timeout?
        if [ "$elapsed" -ge "$timeout" ]; then
            printf "\r%-$(($(tput cols 2>/dev/null || echo 80)))s\r" ""
            return 1
        fi

        # Query all matching pods. For each, capture:
        #  - name (for prefix match)
        #  - Ready condition status (True / False)
        #  - deletionTimestamp (set when pod is terminating)
        # Terminating pods report Ready=True until they exit but are not
        # serving traffic — count them as NOT ready so the bar reflects
        # what the customer actually has up.
        local counts
        counts=$(kubectl get pods -n "$ns" \
            -o jsonpath='{range .items[?(@.metadata.name)]}{.metadata.name}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\t"}{.metadata.deletionTimestamp}{"\n"}{end}' \
            2>/dev/null \
            | awk -v p="^${prefix}" '
                $1 ~ p {
                    total++
                    # $2 = Ready status, $3 = deletionTimestamp (empty if not terminating)
                    if ($2 == "True" && $3 == "") ready++
                }
                END { printf "%d %d\n", (ready ? ready : 0), (total ? total : 0) }
            ')
        local ready="${counts%% *}"
        local total="${counts##* }"
        ready="${ready:-0}"
        total="${total:-0}"

        # If no pods yet (deployment still being created), render an
        # empty bar with placeholder total so user sees something happening.
        local display_total=$total
        [ "$total" = "0" ] && display_total=1

        # Compute filled width.
        local filled=$((ready * bar_width / display_total))
        [ "$filled" -gt "$bar_width" ] && filled=$bar_width
        local empty=$((bar_width - filled))

        # Build the bar string.
        local bar=""
        local i=0
        while [ "$i" -lt "$filled" ]; do bar="${bar}${g_full}";  i=$((i + 1)); done
        while [ "$i" -lt "$bar_width" ]; do bar="${bar}${g_empty}"; i=$((i + 1)); done

        # Render. \r at start, no newline — overwrites previous line.
        local total_display="$total"
        [ "$total" = "0" ] && total_display="?"
        printf "\r  ${C_CYAN}[%s]${C_RESET} ${C_BOLD}%d/%s${C_RESET} pods ready ${C_DIM}· %ds elapsed${C_RESET}   " \
            "$bar" "$ready" "$total_display" "$elapsed"

        # Done? Only "done" if we found at least one pod AND all matching
        # pods are Ready. Avoid the false-positive of "0/0 done" before
        # any pods have been scheduled.
        if [ "$total" -gt 0 ] && [ "$ready" = "$total" ]; then
            # Clear, then print final OK line.
            printf "\r%-$(($(tput cols 2>/dev/null || echo 80)))s\r" ""
            ok "Rollout complete · ${total}/${total} pods ready (${elapsed}s)"
            return 0
        fi

        sleep 1
    done
}

# Draw a box around content. Pass content as multiple args, each becomes
# a line. Auto-sizes width to longest line.
box() {
    # Find the widest line (without ANSI codes — strip them for measurement)
    local lines=("$@")
    local max_w=0
    for line in "${lines[@]}"; do
        # Strip ANSI escape sequences for accurate width.
        local stripped
        stripped=$(printf '%s' "$line" | sed -E 's/\x1B\[[0-9;]*[mK]//g')
        local w=${#stripped}
        [ "$w" -gt "$max_w" ] && max_w=$w
    done
    local total_w=$((max_w + 4))
    local hline=""
    local i=0
    while [ "$i" -lt "$total_w" ]; do
        hline="${hline}${BX_H}"
        i=$((i + 1))
    done
    printf "  ${C_CYAN}${BX_TL}%s${BX_TR}${C_RESET}\n" "$hline"
    for line in "${lines[@]}"; do
        local stripped
        stripped=$(printf '%s' "$line" | sed -E 's/\x1B\[[0-9;]*[mK]//g')
        local pad=$((max_w - ${#stripped}))
        printf "  ${C_CYAN}${BX_V}${C_RESET}  %s%*s  ${C_CYAN}${BX_V}${C_RESET}\n" "$line" "$pad" ""
    done
    printf "  ${C_CYAN}${BX_BL}%s${BX_BR}${C_RESET}\n" "$hline"
}

# Print the Magertron banner.
banner() {
    if [ -n "${NO_COLOR:-}" ]; then
        echo ""
        echo "  | MAGERTRON  MCP Orchestrator Installer"
        echo ""
    else
        echo ""
        printf "  ${C_CYAN}${C_BOLD}▌${C_RESET} ${C_BOLD}MAGERTRON${C_RESET}  ${C_DIM}MCP Orchestrator Installer${C_RESET}\n"
        echo ""
    fi
}

# ─── Defaults ───────────────────────────────────────────────────────────────
LICENSE_FILE="${LICENSE_FILE:-}"
MODE="${MODE:-upgrade}"
SERVICE_TYPE="${SERVICE_TYPE:-nodeport}"
NODE_PORT="${NODE_PORT:-30443}"
CHART_VERSION="${CHART_VERSION:-}"
NAMESPACE="${NAMESPACE:-mcp-system}"
SKIP_NODE_LABEL="${SKIP_NODE_LABEL:-0}"
LABEL_NODES="${LABEL_NODES:-0}"   # Session 2.13: opt-IN to labeling. Chart's default
                                  # values.yaml no longer sets nodeSelector on postgres
                                  # pods, so labeling isn't required for the pods to
                                  # schedule. Customers running multi-node production
                                  # with local-path PVs should pass --label-nodes
                                  # AND add the matching nodeSelector overrides in
                                  # their values file. SKIP_NODE_LABEL is kept as a
                                  # no-op for backward compat (was an opt-OUT flag).
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"
NODE_NAME="${NODE_NAME:-}"
# External HTTPS URL where users' browsers reach this orchestrator
# (e.g. https://magertron.customer.com). Injected as
# orchestrator.env.apiPublicUrl -> MCP_API_PUBLIC_URL, which drives the
# delegated-OAuth callback redirect_uri. Empty = chart default (request-
# relative / localhost dev fallback). Prompted interactively if unset and
# not --non-interactive.
API_PUBLIC_URL="${API_PUBLIC_URL:-}"
HELM_REPO_NAME="${HELM_REPO_NAME:-magertron}"
RELEASE_NAME="${RELEASE_NAME:-mcp}"

usage() {
    cat <<'EOF'
Usage:
  ./install.sh [--license <path>] [options]

License (optional):
  --license <path>            Path to license.json (or set LICENSE_FILE env var).
                              Omit to run in Free tier — all core platform
                              features work; paid features (SSO, SCIM, governance,
                              webhooks, audit export) remain gated until a
                              license is later applied.

Common options:
  --mode <upgrade|reinstall>  upgrade preserves data (default).
                              reinstall destroys data for a fresh start.
  --service-type <type>       nodeport (default), loadbalancer, clusterip
  --node-port <number>        NodePort to pin (default 30443; only used with
                              --service-type nodeport)
  --chart-version <version>   Pin chart version (default: latest --devel)
  --namespace <name>          Install namespace (default mcp-system)
  --node-name <node>          Node to label workload=stateful and
                              workload-inventory=true. Only used with
                              --label-nodes. If --label-nodes is set
                              and --node-name is unset and the shell
                              is interactive, you'll be prompted.
  --label-nodes               Opt in to labeling a node for postgres
                              pinning. Required ONLY for multi-node
                              production clusters where you want
                              postgres pinned to a specific node (and
                              you've added matching nodeSelector
                              overrides in your values file). Default
                              behavior (no flag) is no labeling, which
                              works out of the box on single-node
                              clusters (Docker Desktop, kind, k3d,
                              minikube) and on multi-node clusters
                              with a default StorageClass.
  --skip-node-label           Deprecated no-op. Labeling is off by
                              default. Flag retained for backward
                              compat with existing scripts.
  --api-public-url <url>      External HTTPS URL where users' browsers
                              reach this orchestrator (e.g.
                              https://magertron.example.com). Sets the
                              delegated-OAuth callback redirect target.
                              If unset and the shell is interactive,
                              you'll be prompted (empty answer = use the
                              chart's request-relative/localhost default).
  --non-interactive           Fail on any prompt instead of asking. Use
                              for CI / automation.
  -h, --help                  Show this message

Environment variables (override defaults; CLI flags override env):
  LICENSE_FILE, MODE, SERVICE_TYPE, NODE_PORT, CHART_VERSION,
  NAMESPACE, NODE_NAME, LABEL_NODES, SKIP_NODE_LABEL, NON_INTERACTIVE,
  API_PUBLIC_URL,
  HELM_REPO_NAME, RELEASE_NAME

EOF
}

# ─── Parse args ──────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --license)         LICENSE_FILE="$2"; shift 2 ;;
        --mode)            MODE="$2"; shift 2 ;;
        --service-type)    SERVICE_TYPE="$2"; shift 2 ;;
        --node-port)       NODE_PORT="$2"; shift 2 ;;
        --chart-version)   CHART_VERSION="$2"; shift 2 ;;
        --namespace)       NAMESPACE="$2"; shift 2 ;;
        --node-name)       NODE_NAME="$2"; shift 2 ;;
        --api-public-url)  API_PUBLIC_URL="$2"; shift 2 ;;
        --skip-node-label)
            # Deprecated as of Session 2.13 (chart defaults no longer
            # require labels). Accept silently for backward compat.
            SKIP_NODE_LABEL=1; shift ;;
        --label-nodes)     LABEL_NODES=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# ─── Validate args ───────────────────────────────────────────────────────────
case "$MODE" in
    upgrade|reinstall) ;;
    *) echo "ERROR: --mode must be 'upgrade' or 'reinstall' (got: $MODE)" >&2; exit 1 ;;
esac

case "$SERVICE_TYPE" in
    nodeport|loadbalancer|clusterip) ;;
    *) echo "ERROR: --service-type must be nodeport|loadbalancer|clusterip (got: $SERVICE_TYPE)" >&2; exit 1 ;;
esac

if [ -n "$LICENSE_FILE" ]; then
    if [ ! -f "$LICENSE_FILE" ]; then
        echo "ERROR: license file not found at: $LICENSE_FILE" >&2
        exit 1
    fi
    # License-file shape check. Magertron license files are JWTs (despite the
    # `.json` extension on disk) — three dot-separated base64url segments.
    # We don't verify the signature here; the orchestrator does that at
    # startup with its embedded public key. This catches the common "wrong
    # file" mistake (pointed at an empty file, a different doc, an HTML
    # download error page, etc.) before we waste time installing.
    LICENSE_FIRST_BYTES=$(head -c 2048 "$LICENSE_FILE" | tr -d '[:space:]')
    if [ -z "$LICENSE_FIRST_BYTES" ]; then
        echo "ERROR: license file is empty: $LICENSE_FILE" >&2
        exit 1
    fi
    # A JWT has exactly two dots in its first 2KB and only base64url chars
    # (A-Z, a-z, 0-9, -, _) plus the dots. Quick character-class check:
    if ! printf '%s' "$LICENSE_FIRST_BYTES" | grep -qE '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'; then
        echo "ERROR: license file doesn't look like a JWT: $LICENSE_FILE" >&2
        echo "       Expected three dot-separated base64url segments." >&2
        echo "       First 80 chars of file: ${LICENSE_FIRST_BYTES:0:80}" >&2
        exit 1
    fi
fi
# When --license is omitted, the orchestrator boots in Free tier (the
# default). All core deployment / health / RBAC features work; SSO,
# SCIM, governance, audit export, webhooks remain gated until a license
# is later added via 'kubectl create secret generic mcp-license ...'.

# ─── Banner ──────────────────────────────────────────────────────────────────
# Compute STEP_TOTAL based on which conditional sections will fire.
# Baseline (always run): preflight tools, preflight cluster, preflight helm,
#   node labeling, chart version, teardown release, cleaning leftovers,
#   license secret, helm install, rollout wait, self-mint verify, final state,
#   access info = 13 unconditional sections.
STEP_TOTAL=13
case "$MODE" in
    upgrade)
        STEP_TOTAL=$((STEP_TOTAL + 1))  # "Preserving namespaces"
        ;;
    reinstall)
        STEP_TOTAL=$((STEP_TOTAL + 3))  # tear down ns, clean slate, defensive PVC
        ;;
esac
[ "$SERVICE_TYPE" = "nodeport" ] && STEP_TOTAL=$((STEP_TOTAL + 1))  # NodePort pin

banner

# Configuration summary in a box.
SVC_DISPLAY="$SERVICE_TYPE"
[ "$SERVICE_TYPE" = "nodeport" ] && SVC_DISPLAY="$SERVICE_TYPE (port $NODE_PORT)"
box \
    "${C_BOLD}Installation plan${C_RESET}" \
    "" \
    "  mode             $C_BOLD$MODE$C_RESET" \
    "  namespace        $NAMESPACE" \
    "  service type     $SVC_DISPLAY" \
    "  license          ${LICENSE_FILE:-${C_DIM}<none — Free tier>${C_RESET}}" \
    "  chart version    ${CHART_VERSION:-${C_DIM}<auto-detect latest>${C_RESET}}" \
    "  helm repo        $HELM_REPO_NAME" \
    "  release name     $RELEASE_NAME"

if [ "$MODE" = "reinstall" ]; then
    echo ""
    warn "${C_BOLD}${C_YELLOW}--mode reinstall will DESTROY all data in $NAMESPACE${C_RESET}"
    info "including all service accounts, audit history, and"
    info "deployed MCP servers in customer namespaces."
    if [ "$NON_INTERACTIVE" != "1" ]; then
        echo ""
        read -r -p "  Type ${C_BOLD}destroy${C_RESET} to confirm: " confirm
        if [ "$confirm" != "destroy" ]; then
            err "Aborted."
            exit 1
        fi
    fi
fi

# ─── Preflight: required tools ───────────────────────────────────────────────
section "Preflight: tools"
for tool in kubectl helm python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: $tool is not installed or not on PATH." >&2
        exit 1
    fi
    echo "  $tool: $(command -v "$tool")"
done

# ─── Preflight: cluster reachable ────────────────────────────────────────────
section "Preflight: cluster"
if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: cannot reach Kubernetes cluster." >&2
    echo "       Check that kubectl is configured and your context is correct." >&2
    echo "       Run: kubectl config current-context" >&2
    exit 1
fi
echo "  context:    $(kubectl config current-context)"
echo "  server:     $(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"

# Permissions check — we need broad admin rights.
if ! kubectl auth can-i create namespace >/dev/null 2>&1; then
    echo "ERROR: current kubectl context cannot create namespaces." >&2
    echo "       The installer needs cluster-admin or equivalent." >&2
    exit 1
fi
echo "  perms:      ok (can create namespaces)"

# ─── Preflight: helm repo ────────────────────────────────────────────────────
section "Preflight: helm repo"
if ! helm repo list 2>/dev/null | grep -q "^${HELM_REPO_NAME}\b"; then
    echo "ERROR: helm repo '$HELM_REPO_NAME' is not configured." >&2
    echo "       Add it first:" >&2
    echo "         helm repo add $HELM_REPO_NAME https://magertron.com/charts" >&2
    echo "         helm repo update" >&2
    exit 1
fi
echo "  repo $HELM_REPO_NAME is configured"
helm repo update "$HELM_REPO_NAME" >/dev/null
echo "  repo cache updated"

# ─── Node labeling (opt-in via --label-nodes) ────────────────────────────────
# Session 2.13 inversion: the chart's default values.yaml no longer sets
# nodeSelector on postgres pods, so node labeling isn't needed for pods to
# schedule. Default behavior: skip this section.
#
# Customers running multi-node production who want postgres pinned to a
# specific node (e.g. for local-path PV affinity) must do TWO things:
#   1. Add the nodeSelector to their values.yaml override:
#        postgresql:
#          nodeSelector:
#            workload: stateful
#        inventory:
#          postgresql:
#            nodeSelector:
#              workload-inventory: "true"
#   2. Pass --label-nodes to this installer (or pre-label the node manually).
#
# The two labels are deliberately distinct keys (workload=stateful AND
# workload-inventory=true) so they can later live on different nodes if
# a customer wants to separate them.
section "Node labeling"
if [ "$LABEL_NODES" != "1" ]; then
    echo "  Skipping node labeling (default behavior)."
    echo "  Postgres pods will schedule on any node via your cluster's"
    echo "  default StorageClass. For multi-node production with local-path"
    echo "  PV pinning, see --label-nodes in the help text."
    if [ "$SKIP_NODE_LABEL" = "1" ]; then
        echo ""
        echo "  Note: --skip-node-label is now a no-op (labeling is off by"
        echo "  default). The flag is retained for backward compatibility."
    fi
else
    # User opted IN to labeling via --label-nodes.
    # Determine which node to label.
    if [ -z "$NODE_NAME" ]; then
        if [ "$NON_INTERACTIVE" = "1" ]; then
            echo "ERROR: --label-nodes was set but --node-name was not, and" >&2
            echo "       --non-interactive prevents prompting. Either pass" >&2
            echo "       --node-name <node>, or drop --label-nodes." >&2
            exit 1
        fi
        # Interactive prompt
        echo "  Choose a node to label for stateful workloads (Postgres):"
        echo ""
        # List nodes with a 1-indexed picker.
        # Use a portable read-loop instead of `mapfile` so this works on
        # macOS bash 3.2 (which doesn't have mapfile) as well as Linux.
        NODES=()
        while IFS= read -r line; do
            [ -n "$line" ] && NODES+=("$line")
        done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
        if [ ${#NODES[@]} -eq 0 ]; then
            echo "ERROR: no nodes found in the cluster." >&2
            exit 1
        fi
        i=1
        for n in "${NODES[@]}"; do
            EXISTING=$(kubectl get node "$n" -L workload -L workload-inventory --no-headers 2>/dev/null \
                | awk '{print "workload="$(NF-1)" workload-inventory="$NF}' \
                | sed 's/<none>//g')
            printf "    %d) %s   %s\n" "$i" "$n" "$EXISTING"
            i=$((i+1))
        done
        echo "    s) skip labeling"
        echo ""
        read -r -p "  Select node [1-${#NODES[@]}, or s]: " choice
        case "$choice" in
            s|S|skip)
                LABEL_NODES=0
                echo "  Skipping node labeling."
                ;;
            *[!0-9]*|"")
                echo "ERROR: invalid choice." >&2
                exit 1
                ;;
            *)
                if [ "$choice" -lt 1 ] || [ "$choice" -gt ${#NODES[@]} ]; then
                    echo "ERROR: choice out of range." >&2
                    exit 1
                fi
                NODE_NAME="${NODES[$((choice-1))]}"
                ;;
        esac
    fi

    if [ "$LABEL_NODES" = "1" ]; then
        echo "  Labeling '$NODE_NAME' workload=stateful and workload-inventory=true"
        kubectl label node "$NODE_NAME" workload=stateful --overwrite >/dev/null
        kubectl label node "$NODE_NAME" workload-inventory=true --overwrite >/dev/null
        echo "  Labels applied."
        echo ""
        echo "  REMINDER: --label-nodes only adds the labels to the node."
        echo "  For postgres pods to actually use them, your values.yaml"
        echo "  must set the matching nodeSelector. See --help for the"
        echo "  exact YAML."
    fi
fi

# ─── Chart version resolution ────────────────────────────────────────────────
section "Chart version"
if [ -z "$CHART_VERSION" ]; then
    CHART_VERSION=$(helm search repo "${HELM_REPO_NAME}/mcp-orchestrator" --devel -o json 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["version"] if d else "")')
fi
if [ -z "$CHART_VERSION" ]; then
    echo "ERROR: could not determine chart version. Pass explicitly:" >&2
    echo "  --chart-version <version>" >&2
    exit 1
fi
echo "  target version: $CHART_VERSION"

# ─── Teardown (both modes) ───────────────────────────────────────────────────
# Helm uninstall is REQUIRED even in upgrade mode. Data preservation
# comes from the chart's resource-policy: keep annotation on the
# inventory PVC, which causes helm uninstall to leave that one resource
# alone, plus from not deleting the namespaces holding the PVCs and
# customer deployments.
section "Tearing down existing release"
spinner_start "Tearing down existing release (this can take 30-60s)"
# Suppress BOTH stdout and stderr of teardown commands. Their chatter
# (release "mcp" uninstalled / secret "..." deleted / crd "..." deleted) prints
# to STDOUT and was interleaving with the spinner's \r line, garbling it
# (stderr was already silenced; stdout was the real clobber). The spinner is the
# progress indicator here; the command output is noise. Anything that matters is
# surfaced after spinner_stop.
helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" >/dev/null 2>&1 || true
kubectl delete secret -n "$NAMESPACE" -l "name=${RELEASE_NAME},owner=helm" >/dev/null 2>&1 || true
kubectl delete crd mcproutes.mcp.io >/dev/null 2>&1 || true
spinner_stop
ok "Existing release torn down"

# ─── Orchestrator-managed resource cleanup (both modes) ──────────────────────
# The orchestrator auto-creates NetworkPolicies named mcp-server-isolation
# in any namespace where an MCP server is deployed. Labels say
# managed-by=mcp-orchestrator, not Helm. On the next helm install these
# would block adoption: "invalid ownership metadata; label validation
# error: managed-by must equal Helm".
#
# Delete them here in both modes. The orchestrator recreates them on
# startup as part of its reconcile loop against the deploy_servers DB.
section "Cleaning orchestrator-managed leftovers"
kubectl delete networkpolicy -A -l managed-by=mcp-orchestrator --ignore-not-found 2>/dev/null || true

# ─── Tear down namespaces (reinstall mode only) ──────────────────────────────
if [ "$MODE" = "reinstall" ]; then
    echo ""
    section "Tearing down namespaces (mode=reinstall)"
    # Discover orchestrator-managed namespaces dynamically. The chart
    # labels namespaces it creates with managed-by=mcp-orchestrator;
    # this covers any namespaces auto-created for MCP server deployments
    # without us hardcoding a list.
    MANAGED_NS=$(kubectl get ns -l managed-by=mcp-orchestrator -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
    if [ -n "$MANAGED_NS" ]; then
        echo "  Deleting managed namespaces: $MANAGED_NS"
        for ns in $MANAGED_NS; do
            kubectl delete namespace "$ns" --ignore-not-found 2>/dev/null || true
        done
    fi
    kubectl delete namespace "$NAMESPACE" --ignore-not-found 2>/dev/null || true

    echo "  Waiting for namespace deletion to finish..."
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if ! kubectl get ns 2>/dev/null | grep -E "(^| )${NAMESPACE}\b" | grep -q Terminating; then
            if ! kubectl get ns -l managed-by=mcp-orchestrator 2>/dev/null | grep -q Terminating; then
                break
            fi
        fi
        sleep 3
    done

    echo ""
    section "Clean slate check"
    remaining_ns=$(kubectl get ns -l managed-by=mcp-orchestrator -o name 2>/dev/null || true)
    remaining_np=$(kubectl get networkpolicies -A -l managed-by=mcp-orchestrator 2>/dev/null | grep -v "^NAMESPACE" || true)
    remaining_crd=$(kubectl get crd 2>/dev/null | grep mcp || true)
    [ -n "${remaining_ns}" ]  && echo "  ns:        ${remaining_ns}"  || echo "  ns:        clean"
    [ -n "${remaining_np}" ]  && echo "  netpol:    ${remaining_np}"  || echo "  netpol:    clean"
    [ -n "${remaining_crd}" ] && echo "  crds:      ${remaining_crd}" || echo "  crds:      clean"
else
    echo ""
    section "Preserving namespaces (mode=upgrade)"
    echo "  $NAMESPACE + customer namespaces stay."
    echo "  Inventory PVC + license secret + customer deployments preserved."
fi

# ─── Inventory PVC delete (reinstall mode only) ──────────────────────────────
# Defensive: in reinstall mode the namespace delete above cascade-deletes
# the PVC. This block catches the edge case where the namespace was
# already gone (or PVC outlived it for some reason).
if [ "$MODE" = "reinstall" ]; then
    echo ""
    section "Defensive PVC cleanup"
    kubectl create namespace "$NAMESPACE" 2>/dev/null || true
    kubectl delete pod -n "$NAMESPACE" -l component=inventory-postgresql --ignore-not-found=true >/dev/null
    kubectl delete pvc -n "$NAMESPACE" -l component=inventory-postgresql --ignore-not-found=true >/dev/null

    for i in 1 2 3 4 5; do
        if ! kubectl get pvc -n "$NAMESPACE" -l component=inventory-postgresql 2>/dev/null | grep -q inventory; then
            echo "  inventory PVC cleared."
            break
        fi
        echo "  waiting for PVC release... ($i/5)"
        sleep 2
    done
fi

# ─── License Secret ──────────────────────────────────────────────────────────
section "License Secret"
kubectl create namespace "$NAMESPACE" 2>/dev/null || true
if [ -z "$LICENSE_FILE" ]; then
    # No license provided — Free tier. If an existing license secret is
    # present from a prior install (upgrade mode), leave it; otherwise
    # skip secret creation entirely. The orchestrator boots in Free tier
    # when no license secret is mounted.
    if kubectl get secret -n "$NAMESPACE" mcp-license >/dev/null 2>&1; then
        echo "  Existing license secret present (preserved)."
    else
        echo "  No license provided — running in Free tier."
        echo "  To add a license later:"
        echo "    kubectl create secret generic mcp-license \\"
        echo "      --from-file=license.json=/path/to/license.json -n $NAMESPACE"
        echo "    kubectl rollout restart deployment/mcp-orchestrator -n $NAMESPACE"
    fi
elif [ "$MODE" = "upgrade" ] && kubectl get secret -n "$NAMESPACE" mcp-license >/dev/null 2>&1; then
    echo "  License secret already exists, leaving as-is (mode=upgrade)."
    echo "  To replace, delete the secret first:"
    echo "    kubectl delete secret -n $NAMESPACE mcp-license"
else
    kubectl delete secret -n "$NAMESPACE" mcp-license --ignore-not-found 2>/dev/null || true
    kubectl create secret generic mcp-license \
        --from-file=license.json="$LICENSE_FILE" \
        --namespace "$NAMESPACE" >/dev/null
    echo "  License secret created from: $LICENSE_FILE"
fi

# ─── Helm install ────────────────────────────────────────────────────────────
section "Helm install"

# ── External public URL (delegated-OAuth callback target) ────────────────────
# This is the one value the chart genuinely cannot infer: the externally-
# reachable URL a customer's browser uses to hit the orchestrator. It drives
# the delegated-OAuth callback redirect_uri. If a customer forgets to set it,
# delegated callbacks redirect to a localhost dev default the browser can't
# reach — a silent dead-redirect. So prompt for it interactively when unset.
#
# Validation (when a value IS provided): must have an http(s):// scheme and no
# trailing slash, because the redirect_uri is built by string concatenation
# ({public_url}/api/v1/oauth/callback) and a trailing slash or missing scheme
# produces a redirect_uri the AS will reject at consent time, not at install.
validate_public_url() {
    # $1 = candidate URL. Echoes a normalized URL on success; returns non-zero
    # with a message on stderr on failure. Empty input is allowed (caller maps
    # empty -> chart default).
    local u="$1"
    [ -z "$u" ] && { printf '%s' ""; return 0; }
    case "$u" in
        http://*|https://*) ;;
        *) echo "    must start with http:// or https:// (got: $u)" >&2; return 1 ;;
    esac
    # Strip a single trailing slash so concat doesn't double it.
    u="${u%/}"
    # Reject obvious garbage: needs a host after the scheme.
    case "$u" in
        http://|https://) echo "    missing host after scheme" >&2; return 1 ;;
    esac
    printf '%s' "$u"
    return 0
}

if [ -z "$API_PUBLIC_URL" ]; then
    if [ "$NON_INTERACTIVE" = "1" ]; then
        # Non-interactive + unset is allowed: fall through to the chart's
        # request-relative default. Note it so CI logs are explicit.
        info "API_PUBLIC_URL unset (non-interactive) — using chart default (request-relative URLs)."
    elif [ "$IS_TTY" = "0" ]; then
        info "API_PUBLIC_URL unset (no TTY) — using chart default (request-relative URLs)."
    else
        printf "\n"
        printf "  ${C_YELLOW}${C_BOLD}┌────────────────────────────────────────────────────────────┐${C_RESET}\n"
        printf "  ${C_YELLOW}${C_BOLD}│  IMPORTANT: Public URL for OAuth & DCR callbacks           │${C_RESET}\n"
        printf "  ${C_YELLOW}${C_BOLD}└────────────────────────────────────────────────────────────┘${C_RESET}\n"
        printf "\n"
        printf "  Magertron needs the ${C_BOLD}externally-reachable HTTPS URL${C_RESET} that an\n"
        printf "  end-user's browser uses to reach this orchestrator. It builds the\n"
        printf "  delegated-OAuth / DCR callback target from it:\n"
        printf "      ${C_CYAN}<your-url>/api/v1/oauth/callback${C_RESET}\n"
        printf "\n"
        printf "  ${C_BOLD}Why this matters:${C_RESET} OAuth providers redirect the user's browser\n"
        printf "  back to this exact URL after they sign in. If it's wrong or unset,\n"
        printf "  the browser is sent to a localhost/internal address it cannot reach\n"
        printf "  — and ${C_BOLD}every delegated-OAuth and DCR login fails${C_RESET} with a dead\n"
        printf "  redirect. This is the one value the installer cannot infer for you.\n"
        printf "\n"
        printf "  ${C_DIM}Examples:${C_RESET}\n"
        printf "      ${C_DIM}Production:${C_RESET}  https://magertron.example.com\n"
        printf "      ${C_DIM}Dev/tunnel:${C_RESET}  https://your-name.ngrok-free.dev\n"
        printf "\n"
        printf "  ${C_DIM}(No scheme guess, no trailing slash — paste the full https:// URL.)${C_RESET}\n"
        printf "\n"
        while :; do
            read -r -p "  $(printf "${C_BOLD}Public HTTPS URL${C_RESET}"): " _apu
            # Guard against a stray RETURN silently skipping this critical value.
            if [ -z "$_apu" ]; then
                printf "\n"
                warn "${C_BOLD}No public URL entered.${C_RESET}"
                printf "  ${C_YELLOW}Delegated OAuth and DCR onboarding will NOT work${C_RESET} without it\n"
                printf "  (callbacks will redirect to an unreachable localhost default).\n"
                printf "  You can re-run the installer later with ${C_BOLD}--api-public-url <url>${C_RESET}.\n"
                printf "\n"
                read -r -p "  Skip anyway? Type ${C_BOLD}skip${C_RESET} to confirm, or paste a URL: " _confirm
                case "$_confirm" in
                    skip|SKIP|Skip)
                        API_PUBLIC_URL=""
                        break
                        ;;
                    "")
                        # Bare RETURN again — loop, don't silently skip.
                        echo "  (No input — let's try once more.)" >&2
                        continue
                        ;;
                    *)
                        # They pasted a URL at the confirm prompt — validate it.
                        if normalized=$(validate_public_url "$_confirm"); then
                            API_PUBLIC_URL="$normalized"
                            break
                        fi
                        echo "  Invalid URL. Try again, or type 'skip' to proceed without one." >&2
                        continue
                        ;;
                esac
            fi
            if normalized=$(validate_public_url "$_apu"); then
                API_PUBLIC_URL="$normalized"
                break
            fi
            echo "  Invalid URL — must be a full https:// URL with no trailing slash. Try again." >&2
        done
        if [ -n "$API_PUBLIC_URL" ]; then
            ok "Public URL set: ${C_BOLD}$API_PUBLIC_URL${C_RESET}"
            note "OAuth/DCR callback will be: ${C_CYAN}${API_PUBLIC_URL}/api/v1/oauth/callback${C_RESET}"
        else
            warn "Proceeding with no public URL — delegated OAuth/DCR disabled until set."
        fi
        printf "\n"
    fi
else
    # Value came from --api-public-url or the env var: validate it too, so a
    # bad scripted value fails fast at install rather than at consent time.
    if normalized=$(validate_public_url "$API_PUBLIC_URL"); then
        API_PUBLIC_URL="$normalized"
    else
        echo "ERROR: invalid --api-public-url / API_PUBLIC_URL value." >&2
        exit 1
    fi
fi

# Map our service-type to the chart's loadBalancer.provider value.
# The chart accepts: nodeport, loadbalancer, clusterip.
HELM_VALUES=(
    --version "$CHART_VERSION" --devel
    --namespace "$NAMESPACE"
    --set "envoy.v3.enabled=true"
    --set "loadBalancer.provider=$SERVICE_TYPE"
)

# Only inject apiPublicUrl when set — otherwise let the chart's own default
# (values.yaml) stand, rather than forcing an empty override.
if [ -n "$API_PUBLIC_URL" ]; then
    HELM_VALUES+=( --set "orchestrator.env.apiPublicUrl=$API_PUBLIC_URL" )
fi

# Wrapper function so we can pass it to spinner.
do_helm_install() {
    helm install "$RELEASE_NAME" "${HELM_REPO_NAME}/mcp-orchestrator" \
        "${HELM_VALUES[@]}" \
        > install.out 2>&1
}

if spinner "Installing chart ${HELM_REPO_NAME}/mcp-orchestrator @ $CHART_VERSION" do_helm_install; then
    info "Output saved to install.out"
else
    err "Helm install failed. Last 20 lines of install.out:"
    tail -20 install.out | sed 's/^/    /' >&2
    exit 1
fi

# ─── Wait for orchestrator rollout ───────────────────────────────────────────
section "Waiting for orchestrator rollout"
if ! rollout_progress "$NAMESPACE" "mcp-orchestrator" 180; then
    err "Orchestrator rollout did not finish in 180s."
    note "Check pod status:"
    info "    kubectl get pods -n $NAMESPACE"
    info "    kubectl describe pod -n $NAMESPACE -l app=mcp-orchestrator"
    exit 1
fi

# ─── Pin NodePort (only if service-type=nodeport) ────────────────────────────
# The chart picks a random NodePort by default. If the customer asked for a
# specific port (default 30443 to match the historical tooling), patch it
# in here. Skip for loadbalancer / clusterip.
if [ "$SERVICE_TYPE" = "nodeport" ]; then
    section "Pinning Envoy NodePort to $NODE_PORT"
    kubectl patch svc -n "$NAMESPACE" mcp-orchestrator-envoy \
        -p "{\"spec\":{\"ports\":[{\"name\":\"https\",\"port\":443,\"nodePort\":${NODE_PORT},\"targetPort\":10443,\"protocol\":\"TCP\"}]}}" \
        >/dev/null
    ok "NodePort pinned to $NODE_PORT"
fi

# ─── Verify orchestrator inventory admin bootstrap ───────────────────────────
# The orchestrator self-mints its own inventory admin bootstrap token at
# startup using MCP_JWT_PRIVATE_KEY. Grep startup logs to confirm. We
# COUNT matching lines rather than displaying them — the raw JSON log
# output is implementation detail customers don't need to read.
section "Verifying orchestrator self-mint"
sleep 3
INV_LOG=$(kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=mcp-orchestrator \
    --tail=200 2>/dev/null || true)

SELFMINT_COUNT=$(echo "$INV_LOG" | grep -c "self-minted bootstrap" 2>/dev/null || echo 0)
INVCLIENT_COUNT=$(echo "$INV_LOG" | grep -c "InventoryAdminClient configured" 2>/dev/null || echo 0)
INVCLIENT_FAIL=$(echo "$INV_LOG" | grep -c "inventory client NOT configured" 2>/dev/null || echo 0)

if [ "$SELFMINT_COUNT" -gt 0 ] && [ "$INVCLIENT_COUNT" -gt 0 ] && [ "$INVCLIENT_FAIL" = "0" ]; then
    ok "Self-mint token issued (${SELFMINT_COUNT} orchestrator pod$([ "$SELFMINT_COUNT" -gt 1 ] && echo s))"
    ok "Inventory admin client configured (${INVCLIENT_COUNT} pod$([ "$INVCLIENT_COUNT" -gt 1 ] && echo s))"
elif [ "$INVCLIENT_FAIL" -gt 0 ]; then
    err "Inventory admin client failed to configure on ${INVCLIENT_FAIL} pod(s)"
    note "Service account creation will not work until this is resolved."
    info "    kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=mcp-orchestrator | grep -i inventory"
elif [ "$SELFMINT_COUNT" = "0" ] && [ "$INVCLIENT_COUNT" = "0" ]; then
    warn "No inventory-admin log lines found yet (pods may still be starting)"
    info "    Re-check after a few seconds:"
    info "    kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=mcp-orchestrator | grep -i bootstrap"
else
    # Partial state — some pods did one thing, not the other.
    warn "Self-mint partial: self-mint=${SELFMINT_COUNT}, client-config=${INVCLIENT_COUNT}"
fi

# ─── Final state ─────────────────────────────────────────────────────────────
section "Final cluster state"
kubectl get pods -n "$NAMESPACE"
echo ""
kubectl get svc -n "$NAMESPACE" mcp-orchestrator-envoy 2>/dev/null || true

# ─── Compute access URL for the user ─────────────────────────────────────────
# Best-effort: figure out how to reach the orchestrator UI/API based on
# service type. NodePort → http://<any-node-ip>:<NODE_PORT>.
# LoadBalancer → look for assigned external IP/hostname.
# ClusterIP → tell user to port-forward.
section "Access"
case "$SERVICE_TYPE" in
    nodeport)
        # Pick any node's external or internal IP.
        NODE_IP=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="ExternalIP")].address}{"\n"}{end}' 2>/dev/null | head -1)
        if [ -z "$NODE_IP" ]; then
            NODE_IP=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' 2>/dev/null | head -1)
        fi
        echo "  UI / API: https://${NODE_IP}:${NODE_PORT}"
        echo "  (TLS is self-signed; use -k with curl or accept the cert warning.)"
        ;;
    loadbalancer)
        LB=$(kubectl get svc -n "$NAMESPACE" mcp-orchestrator-envoy -o jsonpath='{.status.loadBalancer.ingress[0].hostname}{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
        if [ -n "$LB" ]; then
            echo "  UI / API: https://${LB}"
        else
            echo "  LoadBalancer IP/hostname not yet assigned. Check:"
            echo "    kubectl get svc -n $NAMESPACE mcp-orchestrator-envoy"
        fi
        ;;
    clusterip)
        echo "  ClusterIP only. Port-forward to access:"
        echo "    kubectl port-forward -n $NAMESPACE svc/mcp-orchestrator-envoy 8443:443"
        echo "  Then: https://localhost:8443"
        ;;
esac

# ─── Closing summary ────────────────────────────────────────────────────────
echo ""
# Capture access URL into a clean variable for the summary box.
ACCESS_URL="(see above)"
case "$SERVICE_TYPE" in
    nodeport)    [ -n "${NODE_IP:-}" ] && ACCESS_URL="https://${NODE_IP}:${NODE_PORT}" ;;
    loadbalancer) [ -n "${LB:-}" ] && ACCESS_URL="https://${LB}" ;;
    clusterip)   ACCESS_URL="https://localhost:8443 (via port-forward)" ;;
esac

if [ "$MODE" = "upgrade" ]; then
    box \
        "${C_GREEN}${C_BOLD}${G_CHECK} Upgrade complete${C_RESET}" \
        "" \
        "  URL                $C_BOLD$ACCESS_URL$C_RESET" \
        "  Data preserved     ${C_GREEN}${G_CHECK}${C_RESET} all PVCs intact" \
        "  JWT keypair        ${C_GREEN}${G_CHECK}${C_RESET} preserved across helm upgrade" \
        "" \
        "  ${C_DIM}Existing user sessions and service-account JWTs${C_RESET}" \
        "  ${C_DIM}continue to work without re-minting.${C_RESET}"
else
    box \
        "${C_GREEN}${C_BOLD}${G_CHECK} Installation complete${C_RESET}" \
        "" \
        "  URL                $C_BOLD$ACCESS_URL$C_RESET" \
        "  Username           ${C_BOLD}admin${C_RESET}" \
        "  Password           ${C_DIM}kubectl get secret -n $NAMESPACE \\${C_RESET}" \
        "                     ${C_DIM}  mcp-orchestrator-secrets \\${C_RESET}" \
        "                     ${C_DIM}  -o jsonpath='{.data.MCP_SEED_ADMIN_PASSWORD}' | base64 -d${C_RESET}" \
        "" \
        "  ${C_BOLD}Next steps${C_RESET}" \
        "    ${G_BULLET} Log in and change the admin password" \
        "    ${G_BULLET} Set the admin user's email" \
        "    ${G_BULLET} Configure webhooks for expiry reminders" \
        "    ${G_BULLET} Deploy MCP servers from the UI"
fi
echo ""
