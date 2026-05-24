#!/bin/bash
# Magertron MCP Orchestrator — install / upgrade helper.
#
# Replaces the dev-era install.sh and reinstall.sh. Same script handles both
# fresh install and upgrade-in-place; the data-preservation behavior is
# controlled by --mode.
#
# Usage:
#   ./install.sh --license <path> [options]
#
# Quick examples:
#   # First-time install, NodePort, latest chart, interactive node prompt:
#   ./install.sh --license ~/Downloads/license.json
#
#   # Upgrade in place, preserve data, pin chart:
#   ./install.sh --license ./license.json --mode upgrade --chart-version 2.5.3
#
#   # Fresh slate (destroys data!), LoadBalancer service:
#   ./install.sh --license ./license.json --mode reinstall --service-type loadbalancer
#
#   # CI / automation (no prompts):
#   ./install.sh --license ./license.json --skip-node-label --non-interactive
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

# ─── Defaults ───────────────────────────────────────────────────────────────
LICENSE_FILE="${LICENSE_FILE:-}"
MODE="${MODE:-upgrade}"
SERVICE_TYPE="${SERVICE_TYPE:-nodeport}"
NODE_PORT="${NODE_PORT:-30443}"
CHART_VERSION="${CHART_VERSION:-}"
NAMESPACE="${NAMESPACE:-mcp-system}"
SKIP_NODE_LABEL="${SKIP_NODE_LABEL:-0}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"
NODE_NAME="${NODE_NAME:-}"
HELM_REPO_NAME="${HELM_REPO_NAME:-magertron}"
RELEASE_NAME="${RELEASE_NAME:-mcp}"

usage() {
    cat <<'EOF'
Usage:
  ./install.sh --license <path> [options]

Required:
  --license <path>            Path to license.json (or set LICENSE_FILE env var)

Common options:
  --mode <upgrade|reinstall>  upgrade preserves data (default).
                              reinstall destroys data for a fresh start.
  --service-type <type>       nodeport (default), loadbalancer, clusterip
  --node-port <number>        NodePort to pin (default 30443; only used with
                              --service-type nodeport)
  --chart-version <version>   Pin chart version (default: latest --devel)
  --namespace <name>          Install namespace (default mcp-system)
  --node-name <node>          Node to label workload=stateful and
                              workload-inventory=true. If unset and the
                              shell is interactive, you'll be prompted.
  --skip-node-label           Skip node labeling entirely. Use this only
                              if your cluster has a default StorageClass
                              that handles PVC placement without node
                              affinity.
  --non-interactive           Fail on any prompt instead of asking. Use
                              for CI / automation.
  -h, --help                  Show this message

Environment variables (override defaults; CLI flags override env):
  LICENSE_FILE, MODE, SERVICE_TYPE, NODE_PORT, CHART_VERSION,
  NAMESPACE, NODE_NAME, SKIP_NODE_LABEL, NON_INTERACTIVE,
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
        --skip-node-label) SKIP_NODE_LABEL=1; shift ;;
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

if [ -z "$LICENSE_FILE" ]; then
    echo "ERROR: --license <path> is required." >&2
    echo "" >&2
    usage >&2
    exit 1
fi
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

# ─── Banner ──────────────────────────────────────────────────────────────────
echo ""
echo "================================================================"
echo "  Magertron MCP Orchestrator install / upgrade"
echo "================================================================"
echo "  mode:           $MODE"
echo "  namespace:      $NAMESPACE"
echo "  service type:   $SERVICE_TYPE$([ "$SERVICE_TYPE" = "nodeport" ] && echo " (port $NODE_PORT)" || true)"
echo "  license file:   $LICENSE_FILE"
echo "  chart version:  ${CHART_VERSION:-<auto-detect latest>}"
echo "  helm repo:      $HELM_REPO_NAME"
echo "  release name:   $RELEASE_NAME"
echo "================================================================"

if [ "$MODE" = "reinstall" ]; then
    echo ""
    echo "WARNING: --mode reinstall will DESTROY all data in $NAMESPACE,"
    echo "         including all service accounts, audit history, and"
    echo "         deployed MCP servers in customer namespaces."
    if [ "$NON_INTERACTIVE" != "1" ]; then
        echo ""
        read -r -p "Type 'destroy' to confirm: " confirm
        if [ "$confirm" != "destroy" ]; then
            echo "Aborted."
            exit 1
        fi
    fi
fi

# ─── Preflight: required tools ───────────────────────────────────────────────
echo ""
echo "========= Preflight: tools =============================="
for tool in kubectl helm python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: $tool is not installed or not on PATH." >&2
        exit 1
    fi
    echo "  $tool: $(command -v "$tool")"
done

# ─── Preflight: cluster reachable ────────────────────────────────────────────
echo ""
echo "========= Preflight: cluster ============================"
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
echo ""
echo "========= Preflight: helm repo =========================="
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

# ─── Node labeling (interactive prompt or skip) ──────────────────────────────
# Postgres pods (orchestrator's and inventory's) use nodeSelector to pin to
# a specific node. This protects data from accidental rescheduling onto a
# node without the right PV mount. The two labels are deliberately
# distinct keys (workload=stateful AND workload-inventory=true) so they
# can later live on different nodes if a customer wants to separate them.
echo ""
echo "========= Node labeling ================================="
if [ "$SKIP_NODE_LABEL" = "1" ]; then
    echo "  Skipping node labeling (--skip-node-label)."
    echo "  Postgres pods will rely on your cluster's default StorageClass"
    echo "  for PV placement. Confirm a default StorageClass exists:"
    echo "    kubectl get storageclass"
else
    # Determine which node to label.
    if [ -z "$NODE_NAME" ]; then
        if [ "$NON_INTERACTIVE" = "1" ]; then
            echo "ERROR: --node-name not set and --non-interactive prevents prompting." >&2
            echo "       Either pass --node-name <node>, or use --skip-node-label." >&2
            exit 1
        fi
        # Interactive prompt
        echo "  Choose a node to label for stateful workloads (Postgres):"
        echo ""
        # List nodes with a 1-indexed picker
        mapfile -t NODES < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
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
                SKIP_NODE_LABEL=1
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

    if [ "$SKIP_NODE_LABEL" != "1" ]; then
        echo "  Labeling '$NODE_NAME' workload=stateful and workload-inventory=true"
        kubectl label node "$NODE_NAME" workload=stateful --overwrite >/dev/null
        kubectl label node "$NODE_NAME" workload-inventory=true --overwrite >/dev/null
        echo "  Labels applied."
    fi
fi

# ─── Chart version resolution ────────────────────────────────────────────────
echo ""
echo "========= Chart version ================================="
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
echo ""
echo "========= Tearing down existing release ================="
helm uninstall "$RELEASE_NAME" -n "$NAMESPACE" 2>/dev/null || true
kubectl delete secret -n "$NAMESPACE" -l "name=${RELEASE_NAME},owner=helm" 2>/dev/null || true
kubectl delete crd mcproutes.mcp.io 2>/dev/null || true

# ─── Orchestrator-managed resource cleanup (both modes) ──────────────────────
# The orchestrator auto-creates NetworkPolicies named mcp-server-isolation
# in any namespace where an MCP server is deployed. Labels say
# managed-by=mcp-orchestrator, not Helm. On the next helm install these
# would block adoption: "invalid ownership metadata; label validation
# error: managed-by must equal Helm".
#
# Delete them here in both modes. The orchestrator recreates them on
# startup as part of its reconcile loop against the deploy_servers DB.
echo ""
echo "========= Cleaning orchestrator-managed leftovers ======="
kubectl delete networkpolicy -A -l managed-by=mcp-orchestrator --ignore-not-found 2>/dev/null || true

# ─── Tear down namespaces (reinstall mode only) ──────────────────────────────
if [ "$MODE" = "reinstall" ]; then
    echo ""
    echo "========= Tearing down namespaces (mode=reinstall) ======"
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
    echo "========= Clean slate check ============================="
    remaining_ns=$(kubectl get ns -l managed-by=mcp-orchestrator -o name 2>/dev/null || true)
    remaining_np=$(kubectl get networkpolicies -A -l managed-by=mcp-orchestrator 2>/dev/null | grep -v "^NAMESPACE" || true)
    remaining_crd=$(kubectl get crd 2>/dev/null | grep mcp || true)
    [ -n "${remaining_ns}" ]  && echo "  ns:        ${remaining_ns}"  || echo "  ns:        clean"
    [ -n "${remaining_np}" ]  && echo "  netpol:    ${remaining_np}"  || echo "  netpol:    clean"
    [ -n "${remaining_crd}" ] && echo "  crds:      ${remaining_crd}" || echo "  crds:      clean"
else
    echo ""
    echo "========= Preserving namespaces (mode=upgrade) =========="
    echo "  $NAMESPACE + customer namespaces stay."
    echo "  Inventory PVC + license secret + customer deployments preserved."
fi

# ─── Inventory PVC delete (reinstall mode only) ──────────────────────────────
# Defensive: in reinstall mode the namespace delete above cascade-deletes
# the PVC. This block catches the edge case where the namespace was
# already gone (or PVC outlived it for some reason).
if [ "$MODE" = "reinstall" ]; then
    echo ""
    echo "========= Defensive PVC cleanup ========================="
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
echo ""
echo "========= License Secret ================================"
kubectl create namespace "$NAMESPACE" 2>/dev/null || true
if [ "$MODE" = "upgrade" ] && kubectl get secret -n "$NAMESPACE" mcp-license >/dev/null 2>&1; then
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
echo ""
echo "========= Helm install =================================="

# Map our service-type to the chart's loadBalancer.provider value.
# The chart accepts: nodeport, loadbalancer, clusterip.
HELM_VALUES=(
    --version "$CHART_VERSION" --devel
    --namespace "$NAMESPACE"
    --set "envoy.v3.enabled=true"
    --set "loadBalancer.provider=$SERVICE_TYPE"
)

helm install "$RELEASE_NAME" "${HELM_REPO_NAME}/mcp-orchestrator" \
    "${HELM_VALUES[@]}" \
    > install.out
echo "  Helm install complete (output: install.out)"

# ─── Wait for orchestrator rollout ───────────────────────────────────────────
echo ""
echo "========= Waiting for orchestrator rollout =============="
if ! kubectl rollout status -n "$NAMESPACE" deploy/mcp-orchestrator --timeout=180s; then
    echo "ERROR: orchestrator rollout did not finish in 180s." >&2
    echo "  Check pod status:" >&2
    echo "    kubectl get pods -n $NAMESPACE" >&2
    echo "    kubectl describe pod -n $NAMESPACE -l app=mcp-orchestrator" >&2
    exit 1
fi

# ─── Pin NodePort (only if service-type=nodeport) ────────────────────────────
# The chart picks a random NodePort by default. If the customer asked for a
# specific port (default 30443 to match the historical tooling), patch it
# in here. Skip for loadbalancer / clusterip.
if [ "$SERVICE_TYPE" = "nodeport" ]; then
    echo ""
    echo "========= Pinning Envoy NodePort to $NODE_PORT ==============="
    kubectl patch svc -n "$NAMESPACE" mcp-orchestrator-envoy \
        -p "{\"spec\":{\"ports\":[{\"name\":\"https\",\"port\":443,\"nodePort\":${NODE_PORT},\"targetPort\":10443,\"protocol\":\"TCP\"}]}}" \
        >/dev/null
    echo "  NodePort pinned to $NODE_PORT"
fi

# ─── Verify orchestrator inventory admin bootstrap ───────────────────────────
# The orchestrator self-mints its own inventory admin bootstrap token at
# startup using MCP_JWT_PRIVATE_KEY. Grep startup logs to confirm. If the
# line isn't there, the binary may be too old or the env may be overriding;
# log it but don't block — operator can investigate.
echo ""
echo "========= Verifying orchestrator self-mint =============="
sleep 3
INV_LOG=$(kubectl logs -n "$NAMESPACE" -l app.kubernetes.io/name=mcp-orchestrator \
    --tail=200 2>/dev/null | grep -iE "self-minted bootstrap|InventoryAdminClient configured|inventory client NOT configured" \
    | head -5 || true)
if [ -n "${INV_LOG}" ]; then
    echo "${INV_LOG}" | sed 's/^/  /'
else
    echo "  WARN: no inventory-admin log lines found in orchestrator logs."
    echo "        The install may still work; check logs manually if you see"
    echo "        problems creating service accounts:"
    echo "          kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=mcp-orchestrator"
fi

# ─── Final state ─────────────────────────────────────────────────────────────
echo ""
echo "========= Final cluster state ==========================="
kubectl get pods -n "$NAMESPACE"
echo ""
kubectl get svc -n "$NAMESPACE" mcp-orchestrator-envoy 2>/dev/null || true

# ─── Compute access URL for the user ─────────────────────────────────────────
# Best-effort: figure out how to reach the orchestrator UI/API based on
# service type. NodePort → http://<any-node-ip>:<NODE_PORT>.
# LoadBalancer → look for assigned external IP/hostname.
# ClusterIP → tell user to port-forward.
echo ""
echo "========= Access ========================================"
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
echo "========================================================"
if [ "$MODE" = "upgrade" ]; then
    echo "  Done. mode=upgrade complete."
    echo "  Data preserved. JWT keypair may have regenerated;"
    echo "  if so, existing JWTs are invalid and must be re-minted."
else
    echo "  Done. mode=reinstall complete; clean slate."
    echo "  Log in at the URL above as 'admin' and:"
    echo "    1. Change the admin password"
    echo "    2. Set the admin user's email"
    echo "    3. Configure webhooks if you want expiry reminders"
    echo "    4. Deploy MCP servers from the UI"
fi
echo "========================================================"
echo ""
