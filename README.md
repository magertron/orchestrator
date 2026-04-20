# MCP Platform Orchestrator

Deployment and lifecycle management for [Model Context Protocol](https://modelcontextprotocol.io) servers on Kubernetes. Deploy, route, scale, and govern MCP servers at enterprise scale.

> This repository contains the Helm chart and installation documentation. The orchestrator software itself is distributed as a container image and runs under a commercial license. Free Tier is available without a license — see [Tiers & Licensing](#tiers--licensing) below.

---

## What it does

- **Deploy** MCP servers to Kubernetes from a single control plane
- **Route** client traffic to the right MCP server via an Envoy-based gateway with dynamic xDS
- **Scale** MCP servers automatically based on load
- **Govern** deployments with RBAC, audit logging, and policy enforcement
- **Integrate** with your identity provider via SSO (OIDC/SAML) and SCIM provisioning

---

## Tiers & Licensing

MCP Orchestrator ships with three tiers:

| Tier | License Required | Use Case |
|---|---|---|
| **Free** | No | Evaluation, small deployments, local dev |
| **Pro** | Yes | Small teams, production workloads |
| **Enterprise** | Yes | Large organizations, SSO, SCIM, governance, audit |

The software **starts in Free Tier by default**. No license file required to install and run. To unlock Pro or Enterprise features, contact [licensing@magertron.com](mailto:licensing@magertron.com).

---

## Prerequisites

You need:

- **Kubernetes 1.25+** cluster ([install options](https://kubernetes.io/docs/setup/))
- **kubectl** configured to talk to your cluster ([install](https://kubernetes.io/docs/tasks/tools/))
- **Helm 3.x** ([install](https://helm.sh/docs/intro/install/))
- At least **4 GB RAM** and **2 CPU cores** available to the cluster

This chart bundles PostgreSQL — no separate database setup required.

---

## Quick Start (Free Tier)

For evaluation, dev, or Free Tier use. No license needed.

```bash
# 1. Add the Helm repository
helm repo add magertron https://magertron.com/charts
helm repo update

# 2. Install (defaults to cloud LoadBalancer — see "Networking" below for other options)
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system \
  --create-namespace

# 3. Wait for pods to be ready
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=mcp-orchestrator \
  -n mcp-system --timeout=300s

# 4. Access the UI (see "Accessing the UI" below)
```

---

## Networking — choose your access path

The chart supports four modes via `loadBalancer.provider` in `values.yaml`:

### Option A — Cloud (default)

For **EKS, GKE, AKS**, or any cluster where `Service type: LoadBalancer` auto-provisions an external IP.

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace
```

No extra flags needed. Your cloud provider provisions the external IP. Check with:

```bash
kubectl get svc -n mcp-system mcp-envoy
```

Wait for `EXTERNAL-IP` to move from `<pending>` to an actual IP, then browse to `https://<external-ip>`.

### Option B — MetalLB (bare-metal / self-hosted)

For **on-prem clusters, kubeadm, k3s without a built-in LB**. MetalLB provides a virtual LoadBalancer.

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set loadBalancer.provider=metallb \
  --set loadBalancer.metallb.ipRange="<start-ip>-<end-ip>"
```

Replace the IP range with unused addresses on your network that MetalLB can assign.

### Option C — NodePort (dev, minikube, kind, k3d)

For **local dev clusters** or any environment where LoadBalancer isn't available.

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set loadBalancer.provider=nodeport
```

Access via any node's IP on port `30443`: `https://<node-ip>:30443`.

### Option D — Existing Ingress / LoadBalancer

For clusters that already have an ingress controller (nginx-ingress, Traefik, Istio, F5) or a pre-configured LoadBalancer.

```yaml
# my-values.yaml
loadBalancer:
  provider: existing
  existing:
    ingress:
      enabled: true
      className: nginx            # your ingress class
      host: mcp.example.com
      tls: true
      tlsSecretName: mcp-envoy-tls  # create this Secret separately
```

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  -f my-values.yaml
```

---

## Accessing the UI

After install, use one of the following depending on your provider choice:

**Port-forward (works with ANY provider — good for first-time login):**

```bash
kubectl port-forward -n mcp-system svc/mcp-envoy 8443:443
```

Browse to `https://localhost:8443`.

**Via LoadBalancer / Ingress:** whatever external IP or hostname your cluster exposed.

**First login:**

- Username: `admin`
- Password: `admin`

**You will be prompted to change the password on first login.** Do this immediately.

---

## Licensed Install (Pro / Enterprise)

To unlock Pro or Enterprise features, you need a license file from Magertron. The process:

### Step 1 — Get your cluster UID

Run this on the cluster you're installing to:

```bash
kubectl get namespace kube-system -o jsonpath='{.metadata.uid}'
```

Copy the output. It'll look like `8a7f3c2e-1234-5678-90ab-cdef12345678`.

### Step 2 — Request a license

Email [licensing@magertron.com](mailto:licensing@magertron.com) with:

- Your cluster UID (from Step 1)
- Your desired tier (Pro or Enterprise)
- Your organization name

We'll send back a signed `license.json` file tied to that cluster UID.

### Step 3 — Install with the license

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set-file license.file=./license.json
```

The chart creates a Kubernetes Secret (`mcp-license`) containing the file, which the orchestrator mounts at `/etc/mcp-license/license.json` and validates at startup.

**Alternative: create the Secret yourself**

```bash
kubectl create namespace mcp-system
kubectl create secret generic mcp-license \
  --from-file=license.json=./license.json \
  -n mcp-system
helm install mcp magertron/mcp-orchestrator -n mcp-system
```

Either approach works — the chart detects and reuses an existing Secret named `mcp-license`.

---

## Upgrading

```bash
helm repo update
helm upgrade mcp magertron/mcp-orchestrator -n mcp-system
```

Licenses survive upgrades — the `mcp-license` Secret is not recreated if it already exists.

---

## Uninstalling

```bash
helm uninstall mcp -n mcp-system
kubectl delete namespace mcp-system
```

This removes all orchestrator components **including the PostgreSQL PVC**. Back up data before uninstalling if you need to preserve state.

---

## Verifying Your Installation

After install, confirm everything is up:

```bash
# All pods should be Running
kubectl get pods -n mcp-system

# License status (check the "tier" field)
kubectl port-forward -n mcp-system svc/mcp-envoy 8443:443 &
curl -k https://localhost:8443/api/v1/license
```

Then log into the UI (see [Accessing the UI](#accessing-the-ui)). Click the "MCP Platform" logo in the top-left to open the **About** dialog — it shows your install's version, license tier, Kubernetes version, and database status.

---

## Troubleshooting

**Pods stuck in `Pending`** — usually insufficient cluster resources. Check `kubectl describe pod <name> -n mcp-system`.

**`EXTERNAL-IP` stuck in `<pending>`** — your cluster doesn't support Service type `LoadBalancer`. Switch to `metallb`, `nodeport`, or `existing` provider.

**UI shows "Not Secure" in browser** — the chart ships with a self-signed certificate. For production, provide your own via `--set-file tls.cert=./tls.crt --set-file tls.key=./tls.key` or configure TLS on your ingress.

**"License invalid for this cluster" on startup** — your `kube-system` namespace UID doesn't match what the license was issued for. Re-run Step 1 of [Licensed Install](#licensed-install-pro--enterprise) and request a new license.

**Other issues** — see the [full documentation](https://magertron.com/docs) or email [support@magertron.com](mailto:support@magertron.com).

---

## License

This repository (Helm chart and installation documentation) is licensed under **Apache 2.0**. See [LICENSE](./LICENSE) for details.

The MCP Platform orchestrator binary distributed via these charts is commercial software. Free Tier usage is permitted without a license. Pro and Enterprise tiers require a separate commercial license — contact [licensing@magertron.com](mailto:licensing@magertron.com).

---

## Links

- **Website:** [magertron.com](https://magertron.com)
- **Docker image:** [hub.docker.com/r/curtismager20/mcp-orchestrator](https://hub.docker.com/r/curtismager20/mcp-orchestrator) *(update if different)*
- **Issues:** [github.com/curtismager20/magertron-mcpm/issues](https://github.com/curtismager20/magertron-mcpm/issues)
- **MCP specification:** [modelcontextprotocol.io](https://modelcontextprotocol.io)
