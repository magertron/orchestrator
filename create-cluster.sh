#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  MCP Platform — Dev Cluster Bootstrap
#  Creates a kind cluster and installs MetalLB so LoadBalancer services
#  get real IPs reachable from your host machine (no port-forward needed).
#
#  Usage:
#    ./create-cluster.sh                        # default: kind + MetalLB
#    ./create-cluster.sh --skip-metallb         # kind only, no MetalLB
#    ./create-cluster.sh --metallb-range "172.18.0.200-172.18.0.250"
#    ./create-cluster.sh --cluster-name my-cluster
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
CLUSTER_NAME="${CLUSTER_NAME:-mcp-platform}"
METALLB_VERSION="${METALLB_VERSION:-v0.14.5}"
METALLB_RANGE="${METALLB_RANGE:-}"          # auto-detected if empty
SKIP_METALLB="${SKIP_METALLB:-false}"

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-metallb)     SKIP_METALLB=true; shift ;;
    --metallb-range)    METALLB_RANGE="$2"; shift 2 ;;
    --cluster-name)     CLUSTER_NAME="$2"; shift 2 ;;
    --metallb-version)  METALLB_VERSION="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────
info()    { echo "  ✓  $*"; }
warn()    { echo "  ⚠  $*"; }
section() { echo ""; echo "── $* ──────────────────────────────────────────"; }

# ── Detect cloud environment ──────────────────────────────────────────────────
detect_environment() {
  if curl -sf --max-time 2 http://169.254.169.254/latest/meta-data/ &>/dev/null; then
    echo "aws"
  elif curl -sf --max-time 2 -H "Metadata-Flavor: Google" \
       http://169.254.169.254/computeMetadata/v1/ &>/dev/null; then
    echo "gcp"
  elif curl -sf --max-time 2 -H "Metadata: true" \
       "http://169.254.169.254/metadata/instance?api-version=2021-02-01" &>/dev/null; then
    echo "azure"
  else
    echo "local"
  fi
}

# ── Auto-detect MetalLB IP range from kind Docker network ────────────────────
detect_metallb_range() {
  local subnet
  subnet=$(docker network inspect kind 2>/dev/null \
    | grep -oE '"Subnet": "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+"' \
    | head -1 \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+')

  if [[ -z "$subnet" ]]; then
    echo "172.18.0.200-172.18.0.250"
    return
  fi

  local base
  base=$(echo "$subnet" | cut -d. -f1-3)
  echo "${base}.200-${base}.250"
}

# ─────────────────────────────────────────────────────────────────────────────
section "Environment Detection"
ENV=$(detect_environment)
info "Environment: $ENV"

if [[ "$ENV" != "local" ]]; then
  warn "Cloud environment detected ($ENV) — MetalLB not needed."
  warn "Your cloud provider LoadBalancer controller will assign external IPs."
  SKIP_METALLB=true
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Kind Cluster"

# Write kind config — extra port mappings provide NodePort fallback
cat > /tmp/mcp-kind-config.yaml <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: ${CLUSTER_NAME}
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 80
        hostPort: 18080
        protocol: TCP
      - containerPort: 443
        hostPort: 18443
        protocol: TCP
EOF

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  warn "Deleting existing cluster: ${CLUSTER_NAME}"
  kind delete cluster --name "${CLUSTER_NAME}"
  sleep 2
fi

info "Creating kind cluster: ${CLUSTER_NAME}"
kind create cluster --config /tmp/mcp-kind-config.yaml
info "Cluster ready"

# ─────────────────────────────────────────────────────────────────────────────
if [[ "$SKIP_METALLB" == "true" ]]; then
  warn "Skipping MetalLB — LoadBalancer services will stay <pending>"
  echo ""
  echo "  NodePort fallback (via kind extraPortMappings):"
  echo "    HTTP  → http://localhost:18080"
  echo "    HTTPS → https://localhost:18443"
  echo ""
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
section "MetalLB ${METALLB_VERSION}"

info "Installing MetalLB..."
kubectl apply -f \
  "https://raw.githubusercontent.com/metallb/metallb/${METALLB_VERSION}/config/manifests/metallb-native.yaml"

info "Waiting for MetalLB pods to be scheduled..."
sleep 10   # give the scheduler time to create pods before we wait on them

info "Waiting for MetalLB pods..."
kubectl wait --namespace metallb-system \
  --for=condition=ready pod \
  --selector=app=metallb \
  --timeout=120s
info "MetalLB ready"

# Auto-detect range after cluster + kind network exist
if [[ -z "$METALLB_RANGE" ]]; then
  METALLB_RANGE=$(detect_metallb_range)
  info "Auto-detected IP range: ${METALLB_RANGE}"
else
  info "Using IP range: ${METALLB_RANGE}"
fi

info "Applying IPAddressPool and L2Advertisement..."
kubectl apply -f - <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: mcp-pool
  namespace: metallb-system
spec:
  addresses:
    - ${METALLB_RANGE}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: mcp-l2
  namespace: metallb-system
spec:
  ipAddressPools:
    - mcp-pool
EOF

info "MetalLB configured"

# ─────────────────────────────────────────────────────────────────────────────
section "Cluster Ready"
echo ""
echo "  Cluster:           ${CLUSTER_NAME}"
echo "  MetalLB pool:      ${METALLB_RANGE}"
echo "  NodePort HTTP:     http://localhost:18080    (kind fallback)"
echo "  NodePort HTTPS:    https://localhost:18443   (kind fallback)"
echo ""
echo "  Next steps:"
echo "    kubectl apply -f deploy/k8s/orchestrator.yaml"
echo "    helm install mcp-orchestrator helm/orchestrator -n mcp-system"
echo ""
