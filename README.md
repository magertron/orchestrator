# MCP Platform Orchestrator

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Helm Chart](https://img.shields.io/badge/helm-v1.6.0-326CE5?logo=helm&logoColor=white)](https://magertron.com/charts)
[![Website](https://img.shields.io/badge/website-magertron.com-2563EB)](https://magertron.com)

**The Kubernetes-native [MCP](https://modelcontextprotocol.io) control plane.** Deploy, govern, and observe MCP servers in your own cluster — any language, any container image. Full lifecycle from a single Helm chart. **Free up to 20 servers, no signup required.**

This repository contains the Helm chart and installation documentation. The orchestrator software itself is distributed as a container image and runs under a commercial license for Pro and Enterprise tiers. The **Free Tier is available without a license** — no signup, no license file, no time limit.

> **License note:** This repository (the Helm chart and these docs) is Apache 2.0. The orchestrator container image you pull via this chart is commercial software. Free Tier usage is always permitted without a license. See [Tiers & Licensing](#tiers--licensing) for details.

---

## Why Magertron?

Most MCP platforms are SaaS-first, single-purpose, or tied to a specific cloud. Magertron is different:

- **Runs in your cluster, not ours.** No data leaves your perimeter. Self-hosted on any Kubernetes 1.25+.
- **Language-agnostic.** Any MCP server image — Python, TypeScript, Go, Java. We don't care how you built it.
- **Full lifecycle.** Deploy, registry, gateway, governance, observability — all in one Helm chart.
- **Truly free up to 20 servers.** Apache 2.0, no signup, no credit card, no time limit.
- **OCSF-aligned audit schema** for SIEM integration (Splunk, Elastic, Datadog, Chronicle).

If you're building MCP servers that wrap proprietary APIs, internal data, or sensitive services — running them on someone else's cloud isn't an option. Magertron is the Kubernetes-native alternative.

---

## What it does

- **Deploy** MCP servers to Kubernetes from a single control plane
- **Route** client traffic to the right MCP server via an Envoy-based gateway with dynamic xDS updates
- **Scale** MCP servers with a slider or a CLI command
- **Govern** deployments with RBAC, audit logging, and policy enforcement
- **Integrate** with your identity provider via SSO (OIDC / SAML) and SCIM provisioning *(Enterprise)*

---

## Tiers & Licensing

| Tier | License Required | Limits | Typical Use |
| --- | --- | --- | --- |
| **Free** | No | Up to 20 MCP servers | Small teams, evaluation, local dev |
| **Pro** | Yes | Unlimited servers, multiple namespaces, deployment history, CLI (`mcpctl`) | Small production teams |
| **Enterprise** | Yes | Pro + SSO/SCIM, governance engine, custom RBAC, multi-tenant isolation, webhooks | Larger orgs, regulated industries |

The platform **starts in Free Tier by default**. No license file is required to install and run — no signup, no credit card, no time limit. To unlock Pro or Enterprise features, email [licensing@magertron.com](mailto:licensing@magertron.com) — see [Licensed Install](#licensed-install-pro--enterprise).

---

## What You'll Need

Before you begin, make sure you have:

- A **Kubernetes 1.25 or newer** cluster. If you don't have one, [Step 0](#step-0--get-a-kubernetes-cluster) below gets you one in about two minutes.
- **kubectl** configured to talk to that cluster — [install](https://kubernetes.io/docs/tasks/tools/).
- **Helm 3.x** — [install](https://helm.sh/docs/intro/install/).
- **4 GB RAM** and **2 CPU cores** free on the cluster.

This chart bundles PostgreSQL — no separate database setup is required.

---

## Step 0 — Get a Kubernetes cluster

**If you already have a cluster, skip to [Step 1](#step-1--install-the-chart).**

For evaluators on a single Linux machine, we recommend **k3s**. It's a full Kubernetes distribution in one binary, runs as a systemd service, and needs no Docker.

### Install k3s (recommended for Linux evaluators)

Install k3s with a world-readable kubeconfig so you can use `kubectl` as a regular user:

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--write-kubeconfig-mode=644" sh -
```

> **Security-conscious shops:** if piping to a shell isn't allowed by your policy, k3s also ships as a Debian package, an RPM, and a standalone binary. See the [k3s quick start](https://docs.k3s.io/quick-start) for alternatives.

Wait about 30 seconds for the service to come up. Then point `kubectl` at k3s's kubeconfig by adding one line to your shell profile:

```bash
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

The first line makes it stick for future terminals; the second applies it right now. Verify:

```bash
kubectl get nodes
```

**Expected output:**

```
NAME         STATUS   ROLES                  AGE   VERSION
your-host    Ready    control-plane,master   45s   v1.30.x+k3s1
```

You have a cluster.

### Install Helm

If you don't already have it:

```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

Verify:

```bash
helm version
```

**Expected output (version numbers will vary):**

```
version.BuildInfo{Version:"v3.15.x", ...}
```

---

## Step 1 — Install the chart

**If you followed Step 0 (k3s on a single machine)**, use the NodePort flavor — it's the simplest path to a working UI. Run the whole block:

```bash
# Add the Helm repository and install in one go
helm repo add magertron https://magertron.com/charts
helm repo update
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system \
  --create-namespace \
  --set loadBalancer.provider=nodeport
```

**If you're on a cloud provider (EKS, GKE, AKS) or any cluster with a working LoadBalancer controller**, omit the `--set` flag:

```bash
helm repo add magertron https://magertron.com/charts
helm repo update
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system \
  --create-namespace
```

**For other setups** (MetalLB, existing ingress, etc.), see [Networking deep-dive](#networking-deep-dive) below — the repo-add commands there still apply.

### Wait for the pods to come up

```bash
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=mcp-orchestrator \
  -n mcp-system --timeout=300s
```

**Expected output:**

```
pod/mcp-orchestrator-xxxxxxxxxx-yyyyy condition met
```

**If this times out**, something is off. Check pod status:

```bash
kubectl get pods -n mcp-system
```

Any pod not `Running`? Describe it:

```bash
kubectl describe pod <pod-name> -n mcp-system
```

The `Events:` section at the bottom tells you what went wrong. Common causes — insufficient resources, image pull failure, missing license for a licensed install — are covered in [Troubleshooting](#troubleshooting).

---

## Step 2 — Access the UI

The simplest way to reach the UI on any install is port-forwarding:

```bash
kubectl port-forward -n mcp-system svc/mcp-envoy-gateway 8443:443
```

This command **stays running** and prints:

```
Forwarding from 127.0.0.1:8443 -> 443
Forwarding from [::1]:8443 -> 443
```

Leave that terminal window open. In a browser, go to:

```
https://localhost:8443
```

### Expected: browser warns "Not Secure"

The chart ships with a self-signed TLS certificate so the UI works out of the box without you configuring DNS or procuring a cert. Every browser will warn about this.

**Click "Advanced" → "Proceed to localhost (unsafe)"** (exact wording varies by browser). This is normal and expected for the first login. For production installs, configure a real certificate — see [TLS in production](#tls-in-production).

### First login

- **Username:** `admin`
- **Password:** `admin`

**Change the admin password immediately after logging in.** Open **Settings → Users → admin → Change Password** and set a real password. The default credentials are for evaluation convenience only and must not be used in any environment reachable from a network you don't trust.

### Alternative: LoadBalancer or Ingress

If your cluster has a real LoadBalancer (cloud) or an ingress you configured, you can reach the UI directly:

```bash
kubectl get svc -n mcp-system mcp-envoy-gateway
```

For a LoadBalancer service, the `EXTERNAL-IP` column shows where to browse. For NodePort, use any node's IP on port `30443`: `https://<node-ip>:30443`.

### Stopping the port-forward

When you're done, press `Ctrl+C` in the terminal running `kubectl port-forward`. Re-run the same command to start it again later.

---

## Step 3 — Verify your install

Once you're logged in, confirm everything is healthy. Two ways.

### Option A — via the UI (recommended)

Click the **MCP Platform** logo in the top-left of the UI. An **About** dialog opens showing:

- Version of the orchestrator
- License tier (Free, Pro, or Enterprise)
- Kubernetes version
- Database status

If all four show sensible values, you're good.

### Option B — via the API

With the port-forward still running from Step 2, in a new terminal:

```bash
curl -k https://localhost:8443/api/v1/license
```

**Expected output (Free Tier):**

```json
{"tier":"free","valid":true,...}
```

The `-k` flag tells `curl` to accept the self-signed cert. For Pro or Enterprise, the `tier` field shows `pro` or `enterprise` instead.

---

## Step 4 — Deploy your first MCP server

Let's deploy a real MCP server to prove the platform works end-to-end. We'll use the IBM `fast-time-server`, a small public MCP server that exposes `get_system_time` and `convert_time` tools.

In the UI:

1. Click **Servers** in the left sidebar.
2. Click **Deploy New Server** (top right).
3. Fill in:
   - **Name:** `fast-time-server`
   - **Namespace:** `mcp-prod` *(the UI will offer to create it)*
   - **Image:** `ghcr.io/ibm/fast-time-server:latest`
   - **Port:** `8080`
4. Click **Deploy**.

The server's status will move from **Pending** → **Deploying** → **Running** over about 30 seconds. When it reaches **Running**, click the server name to open its detail panel. The **Tools** tab should list `get_system_time` and `convert_time` — auto-discovered by the platform.

**To route traffic to it**, use any MCP client (the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is a good first choice) pointed at:

```
https://localhost:8443/servers/fast-time-server/mcp
```

You're now running a governed, audited, routable MCP server. Everything you do from here — deploy more servers, configure RBAC, set up SSO — builds on this same foundation.

---

## Networking deep-dive

The chart supports four networking modes via `loadBalancer.provider` in `values.yaml`. Step 1 above picked one for you. If you need a different mode, here's the full picture.

### Cloud LoadBalancer (default)

For **EKS, GKE, AKS**, or any cluster where `Service type: LoadBalancer` auto-provisions an external IP.

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace
```

Check the external IP with `kubectl get svc -n mcp-system mcp-envoy-gateway`. Wait for `EXTERNAL-IP` to move from `<pending>` to an actual IP, then browse to `https://<external-ip>`.

### NodePort (k3s, kind, minikube, any dev cluster)

For **local dev clusters** or any environment where cloud LoadBalancer isn't available. This is what Step 1 uses if you followed Step 0.

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set loadBalancer.provider=nodeport
```

Access via any node's IP on port `30443`: `https://<node-ip>:30443`. You can also use `kubectl port-forward` (Step 2) regardless of provider.

### MetalLB (bare-metal clusters)

For on-prem clusters where you want a real LoadBalancer IP but your environment doesn't provide one. MetalLB assigns IPs from a range you own on your LAN.

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set loadBalancer.provider=metallb \
  --set loadBalancer.metallb.ipRange="192.168.1.240-192.168.1.250"
```

**How to pick an IP range:** choose a block of IP addresses on your LAN that's outside your router's DHCP range and unused by any device. Example: if your router hands out `192.168.1.100–192.168.1.200` via DHCP, `192.168.1.240–192.168.1.250` is safe to use for MetalLB. Ask your network admin if unsure.

You must install MetalLB on your cluster separately — see the [MetalLB install docs](https://metallb.universe.tf/installation/).

### Existing Ingress

For clusters that already have an ingress controller (nginx-ingress, Traefik, Istio) or a pre-configured LoadBalancer.

Create a file called `my-values.yaml` in your working directory:

```yaml
loadBalancer:
  provider: existing
  existing:
    ingress:
      enabled: true
      className: nginx              # your ingress class name
      host: mcp.example.com         # the hostname you'll access the UI at
      tls: true
      tlsSecretName: mcp-envoy-tls  # Secret containing the TLS cert + key
```

Create the TLS Secret separately (your cert and key):

```bash
kubectl create namespace mcp-system
kubectl create secret tls mcp-envoy-tls \
  --cert=./tls.crt --key=./tls.key \
  -n mcp-system
```

Then install:

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  -f my-values.yaml
```

---

## Licensed Install (Pro / Enterprise)

Free Tier needs no license. To unlock Pro or Enterprise features, request a license file from Magertron and install it.

### Step 1 — Get your cluster UID

The license is tied to a specific cluster's `kube-system` namespace UID.

```bash
kubectl get namespace kube-system -o jsonpath='{.metadata.uid}'
```

Copy the output. It looks like `8a7f3c2e-1234-5678-90ab-cdef12345678`.

### Step 2 — Request a license

Email [licensing@magertron.com](mailto:licensing@magertron.com) with:

- Your cluster UID (from Step 1)
- Your desired tier (**Pro** or **Enterprise**)
- Your organization name

We'll send back a signed `license.json` file tied to that cluster UID. Licenses cannot be transferred between clusters.

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

Either approach works. The chart detects and reuses an existing `mcp-license` Secret.

### Pro / Enterprise CLI — `mcpctl`

Pro and Enterprise tiers include `mcpctl`, a single-binary CLI for macOS and Linux. Download and install instructions are sent with your license. `mcpctl` lets you deploy, scale, and evaluate governance from your terminal without opening the UI.

### Managing your own JWT signing keys *(production)*

By default the chart auto-generates RSA keys for signing authentication tokens at install time. This is fine for evaluation — zero setup, everything Just Works.

For production installs you'll want to manage these keys yourself so they survive chart reinstalls, can be rotated on your schedule, and can be stored in your secret backend of choice. Generate an RSA keypair once:

```bash
openssl genpkey -algorithm RSA -out jwt.key -pkeyopt rsa_keygen_bits:2048
openssl rsa -in jwt.key -pubout -out jwt.pub
```

Then pass both files at install time:

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set-file secrets.jwtPrivateKey=./jwt.key \
  --set-file secrets.jwtPublicKey=./jwt.pub
```

**Store these keys somewhere safe.** If you lose them, all existing user sessions become invalid and users will need to log in again (not catastrophic — but worth knowing). If you rotate them, do the same: new install with new keys invalidates existing sessions.

Keys provided via `--set-file` are stored in a Kubernetes Secret (`mcp-orchestrator-jwt`) which the orchestrator mounts read-only at runtime. They are not logged, transmitted, or visible in the UI.

---

## Upgrading

```bash
helm repo update
helm upgrade mcp magertron/mcp-orchestrator -n mcp-system
```

**Licenses survive upgrades.** The `mcp-license` Secret is not modified or recreated if it already exists, so you do not need to reinstall your license after every upgrade.

If you used a custom `values.yaml` (for example, for ingress or MetalLB config), pass it again:

```bash
helm upgrade mcp magertron/mcp-orchestrator -n mcp-system -f my-values.yaml
```

---

## Uninstalling

```bash
helm uninstall mcp -n mcp-system
kubectl delete namespace mcp-system
```

This removes all orchestrator components **including the PostgreSQL PersistentVolumeClaim** — all deployment history, audit logs, users, and configuration will be gone.

**To preserve data before uninstalling**, back up the PostgreSQL volume:

```bash
kubectl exec -n mcp-system mcp-postgres-0 -- \
  pg_dumpall -U postgres > mcp-backup-$(date +%Y%m%d).sql
```

Store the resulting `mcp-backup-YYYYMMDD.sql` somewhere safe. For restore instructions, email [support@magertron.com](mailto:support@magertron.com).

---

## TLS in production

The chart ships with a self-signed certificate for first-run convenience. For any install reachable from a network you don't control, replace it.

**Option 1 — provide cert and key at install time:**

```bash
helm install mcp magertron/mcp-orchestrator \
  --namespace mcp-system --create-namespace \
  --set-file tls.cert=./tls.crt \
  --set-file tls.key=./tls.key
```

**Option 2 — terminate TLS at your ingress** (the existing-ingress mode in [Networking](#existing-ingress)) and let your ingress controller handle certs via cert-manager, Let's Encrypt, or your corporate CA.

---

## Troubleshooting

**Pods stuck in `Pending`.** Usually insufficient cluster resources. Run `kubectl describe pod <name> -n mcp-system` and check the `Events:` section.

**`EXTERNAL-IP` stuck in `<pending>`.** Your cluster doesn't support `Service type: LoadBalancer`. Switch to `nodeport`, `metallb`, or `existing` — see [Networking deep-dive](#networking-deep-dive).

**`ImagePullBackOff` on the orchestrator pod.** The node can't pull the image. Check `kubectl describe pod <name> -n mcp-system` for the exact error. Common causes: no internet from the node, an HTTP proxy that isn't configured for the container runtime, or an air-gapped environment (contact us for air-gapped install instructions).

**PostgreSQL pod crashing.** Check `kubectl logs -n mcp-system mcp-postgres-0`. The most common cause is insufficient disk space on the PersistentVolume — the chart defaults to 10 GiB. Increase `postgresql.persistence.size` in your values file if you need more.

**UI shows "Not Secure" in browser.** Expected on a first install — see [Access the UI](#step-2--access-the-ui). For production, use your own cert — see [TLS in production](#tls-in-production).

**"License invalid for this cluster" at startup.** The `kube-system` namespace UID doesn't match what the license was issued for. Re-run Step 1 of [Licensed Install](#licensed-install-pro--enterprise) and request a new license for the current cluster.

**"License expired" at startup.** Your license term has ended. Email [licensing@magertron.com](mailto:licensing@magertron.com) to renew.

**SSO callback fails with "Failed to decode SAML response" *(Enterprise)*.** URL-encoded SAML assertions from some IdPs need to be decoded by the orchestrator. This is fixed in orchestrator image `v1.1` and newer — upgrade with `helm upgrade` above.

**SSO callback fails with "state provider mismatch" *(Enterprise)*.** The `provider_id` must be resolved from RelayState, not passed as a callback parameter. Fixed in `v1.1` and newer.

**Envoy returns 404 for a deployed MCP server.** The xDS push hasn't reached Envoy yet, or the MCP server pod isn't Ready. Wait 10 seconds and retry. If the 404 persists, check `kubectl get mcproutes -n <your-namespace>` — the route resource should exist and have a `Ready` condition.

**Other issues.** Email [support@magertron.com](mailto:support@magertron.com) or [open a GitHub issue](https://github.com/curtismager20/magertron-mcpm/issues).

---

## Contact

- **Sales and demos:** [sales@magertron.com](mailto:sales@magertron.com)
- **Licensing (Pro / Enterprise):** [licensing@magertron.com](mailto:licensing@magertron.com)
- **Technical support:** [support@magertron.com](mailto:support@magertron.com)
- **Website:** [magertron.com](https://magertron.com)

---

## License

This repository (the Helm chart and this documentation) is licensed under **Apache 2.0**. See [LICENSE](./LICENSE).

The MCP Platform orchestrator binary distributed via this chart is commercial software. Free Tier usage is permitted without a license. Pro and Enterprise tiers require a separate commercial license — see [Licensed Install](#licensed-install-pro--enterprise).

---

## Links

- **Website:** [magertron.com](https://magertron.com)
- **Docker image:** [hub.docker.com/r/curtismager20/mcp-orchestrator](https://hub.docker.com/r/curtismager20/mcp-orchestrator)
- **Issues:** [github.com/curtismager20/magertron-mcpm/issues](https://github.com/curtismager20/magertron-mcpm/issues)
- **MCP specification:** [modelcontextprotocol.io](https://modelcontextprotocol.io)
