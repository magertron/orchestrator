# MCP Orchestrator — Installation Guide

This guide covers installing the MCP Orchestrator platform on Kubernetes using the Helm chart. It includes both the default deployment (bundled PostgreSQL) and production deployment with an external database.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start (Bundled PostgreSQL)](#quick-start)
3. [Production Deployment (External PostgreSQL)](#production-deployment)
4. [Database Setup](#database-setup)
5. [TLS Configuration](#tls-configuration)
6. [Dev Cluster Setup (kind + Docker Desktop)](#dev-cluster-setup-kind--docker-desktop)
7. [Load Balancer Configuration](#load-balancer-configuration)
8. [Upgrading](#upgrading)
9. [Uninstalling](#uninstalling)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Kubernetes 1.27+ (EKS, GKE, AKS, on-prem, or kind for dev)
- Helm 3.12+
- `kubectl` configured with cluster access
- metrics-server installed for live CPU/memory monitoring

### Install metrics-server

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

For kind clusters, metrics-server needs an additional flag:

```bash
kubectl patch deployment metrics-server -n kube-system \
  --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

### Generate JWT Keys

JWT keys are required for authentication. Generate an RSA keypair:

```bash
openssl genpkey -algorithm RSA -out jwt.key -pkeyopt rsa_keygen_bits:2048
openssl rsa -in jwt.key -pubout -out jwt.pub
```

Keep these files secure. They are used to sign and verify authentication tokens.

---

## Quick Start

The quickest way to get running — uses the bundled PostgreSQL included in the chart:

```bash
helm install mcp ./helm/orchestrator -n mcp-system --create-namespace \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub
```

Access the dashboard:

```bash
kubectl port-forward svc/mcp-mcp-orchestrator -n mcp-system 8080:8080
open http://localhost:8080
```

Login with `admin` / `admin`. Change the password immediately.

Install the CLI:

```bash
cd mcpctl && make build
sudo cp mcpctl /usr/local/bin/
mcpctl login http://localhost:8080 admin admin
```

---

## CLI Installation

The `mcpctl` CLI is a single binary with zero dependencies. Pre-built binaries are available for macOS and Linux.

### Download a Pre-built Binary

| Platform | Architecture | Binary |
|----------|-------------|--------|
| macOS | Apple Silicon (M1/M2/M3) | `mcpctl-darwin-arm64` |
| macOS | Intel | `mcpctl-darwin-amd64` |
| Linux | x86_64 | `mcpctl-linux-amd64` |
| Linux | ARM64 (Graviton) | `mcpctl-linux-arm64` |

```bash
# Example: macOS Apple Silicon
curl -L https://releases.yourcompany.com/mcpctl-darwin-arm64 -o /usr/local/bin/mcpctl
chmod +x /usr/local/bin/mcpctl
mcpctl version
```

### Build from Source

Requires Go 1.21+:

```bash
cd mcpctl
make build          # builds for current platform
make install        # copies to /usr/local/bin
make dist           # cross-compiles all 4 platform variants
```

`make dist` produces:
```
mcpctl-darwin-arm64    # macOS Apple Silicon
mcpctl-darwin-amd64    # macOS Intel
mcpctl-linux-amd64     # Linux x86_64
mcpctl-linux-arm64     # Linux ARM64
```

### Verify Installation

```bash
mcpctl version
mcpctl login http://localhost:8080 admin admin
mcpctl status
```

---

## Production Deployment

For production, use an external managed database (Amazon RDS, Cloud SQL, Azure Database, or your own PostgreSQL cluster) for reliability, backups, and scaling.

### Step 1: Provision PostgreSQL

Provision a PostgreSQL 14+ instance with your cloud provider or infrastructure team. Note the connection details:

- **Host**: e.g., `mcp-db.cluster-abc123.us-east-1.rds.amazonaws.com`
- **Port**: `5432`
- **Database name**: `mcp_platform`
- **Username**: e.g., `mcp_admin`
- **Password**: a strong, randomly generated password

### Step 2: Initialize the Database Schema

Connect to your PostgreSQL instance and run the schema initialization script:

```bash
psql -h <host> -U <username> -d mcp_platform -f schema/init.sql
```

The `init.sql` file is provided with the release. See [Database Setup](#database-setup) for details.

### Step 3: Install with Helm

```bash
helm install mcp ./helm/orchestrator -n mcp-system --create-namespace \
  --set postgresql.enabled=false \
  --set postgresql.host=mcp-db.cluster-abc123.us-east-1.rds.amazonaws.com \
  --set postgresql.port=5432 \
  --set postgresql.database=mcp_platform \
  --set postgresql.username=mcp_admin \
  --set secrets.dbPassword=your-secure-password \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub \
  --set orchestrator.replicaCount=3 \
  --set envoy.autoscaling.maxReplicas=20
```

### Step 4: Configure Namespaces

By default, the chart creates three namespaces: `mcp-prod`, `mcp-staging`, and `mcp-dev`. Customize with:

```bash
--set "namespaces={mcp-prod,mcp-staging,mcp-dev,mcp-sandbox}"
```

### Step 5: Verify

```bash
kubectl get pods -n mcp-system
kubectl port-forward svc/mcp-mcp-orchestrator -n mcp-system 8080:8080
mcpctl login http://localhost:8080 admin admin
mcpctl status
```

---

## Database Setup

The platform requires PostgreSQL 14 or later with the `pgcrypto` extension.

### Option A: Bundled PostgreSQL (default)

The Helm chart deploys a single-replica PostgreSQL instance with a PersistentVolumeClaim. The schema and seed data are applied automatically on first startup. No manual database setup is needed.

For production storage, specify a StorageClass:

```bash
--set postgresql.storage.storageClassName=longhorn
--set postgresql.storage.size=20Gi
```

### Option B: External PostgreSQL

When using an external database, you must initialize the schema before installing the Helm chart.

1. Create the database:

```sql
CREATE DATABASE mcp_platform;
```

2. Run the schema initialization. The file `schema/init.sql` is included in the release package and contains:

   - Table definitions (users, audit_events, deployed_servers, roles, governance_policies, webhooks)
   - Required indexes
   - The `pgcrypto` extension for UUID generation
   - Seed data: default admin user, system roles, and governance policy templates

```bash
psql -h <host> -U <username> -d mcp_platform -f schema/init.sql
```

3. Verify the schema:

```bash
psql -h <host> -U <username> -d mcp_platform -c "\dt"
```

Expected tables:

```
 Schema |        Name          | Type  
--------+----------------------+-------
 public | audit_events         | table
 public | deployed_servers     | table
 public | governance_policies  | table
 public | roles                | table
 public | users                | table
 public | webhooks             | table
```

### Default Admin User

The seed data creates an admin user:

- **Username**: `admin`
- **Password**: `admin`
- **Role**: `system:platform-admin`

Change this password immediately after first login.

### Default Roles

| Role | Description | Namespaces |
|------|-------------|------------|
| `system:platform-admin` | Full access | All |
| `system:deploy-manager` | Deploy and manage servers | mcp-prod, mcp-staging |
| `system:operator` | Deploy and manage servers | mcp-dev, mcp-staging |
| `system:viewer` | Read-only access | All |

### Default Governance Policies

| Policy | Applies To | Status |
|--------|-----------|--------|
| `enterprise-standard` | mcp-prod, mcp-staging | Enabled |
| `dev-relaxed` | mcp-dev | Enabled |

---

## TLS Configuration

### Option A: Auto-generated (default)

The Helm chart generates self-signed TLS certificates automatically. This is suitable for development and internal deployments.

### Option B: Provide Your Own Certificates

For production with trusted certificates (e.g., from Let's Encrypt or your CA):

```bash
helm install mcp ./helm/orchestrator -n mcp-system --create-namespace \
  --set tls.create=true \
  --set-file tls.cert=tls.crt \
  --set-file tls.key=tls.key \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub
```

### Option C: External Secret Management

If you manage TLS secrets externally (e.g., cert-manager):

```bash
helm install mcp ./helm/orchestrator -n mcp-system --create-namespace \
  --set tls.create=false \
  --set tls.orchestratorSecretName=my-tls-secret \
  --set tls.envoySecretName=my-envoy-tls-secret \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub
```

Ensure the secrets exist in the target namespace before installing.

---

## Upgrading

### Helm Upgrade

```bash
helm upgrade mcp ./helm/orchestrator -n mcp-system \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub
```

Helm preserves existing secrets on upgrade — JWT keys and TLS certificates are not regenerated if they already exist in the cluster.

### Database Migrations

When upgrading to a new version that requires schema changes, migration scripts will be provided in the release notes. Run them against your database before upgrading:

```bash
psql -h <host> -U <username> -d mcp_platform -f migrations/v1.1-to-v1.2.sql
helm upgrade mcp ./helm/orchestrator -n mcp-system
```

### Rollback

```bash
# List release history
helm history mcp -n mcp-system

# Rollback to a previous revision
helm rollback mcp 1 -n mcp-system
```

---

## Uninstalling

```bash
helm uninstall mcp -n mcp-system
```

This removes all Kubernetes resources created by the chart. If you used the bundled PostgreSQL, the PersistentVolumeClaim is **not** deleted automatically (to prevent data loss). To remove it:

```bash
kubectl delete pvc mcp-mcp-orchestrator-postgres-data -n mcp-system
```

To remove the namespace:

```bash
kubectl delete namespace mcp-system
```

---

## Troubleshooting

### Pods stuck in ContainerCreating

Usually a missing secret. Check:

```bash
kubectl describe pod <pod-name> -n mcp-system | tail -20
```

Common causes:
- TLS secret doesn't exist — set `tls.create=true` or create it manually
- JWT keys not provided — pass `--set-file secrets.jwtPrivateKey=jwt.key`

### Cannot login (401 Unauthorized)

- Verify JWT keys match: the private key signs tokens, the public key verifies them
- Check the orchestrator logs: `kubectl logs -n mcp-system deploy/mcp-mcp-orchestrator -c orchestrator | grep -i jwt`
- Ensure the token hasn't expired (default TTL: 1 hour)

### Health monitor shows "no matching pods"

- Verify metrics-server is running: `kubectl get pods -n kube-system | grep metrics`
- For kind clusters, metrics-server needs `--kubelet-insecure-tls`
- Check RBAC: the orchestrator service account needs `metrics.k8s.io` API access

### Server stuck in Deploying state

- Check the MCP server pod: `kubectl get pods -n <namespace>`
- If `CrashLoopBackOff`: `kubectl logs -n <namespace> <pod-name>`
- Common cause: wrong port configuration — verify the MCP server listens on the configured port

### Namespace access denied (403 Forbidden)

- Check the user's role: the role must include the target namespace in `allowed_namespaces`
- Platform admins have `["*"]` (all namespaces)
- Update role assignments via the UI or API

### Scale/restart returns 403

- The orchestrator service account needs a ClusterRole with `deployments/scale` patch permission
- Apply the RBAC fix:

```bash
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: mcp-orchestrator-manager
rules:
- apiGroups: ["apps"]
  resources: ["deployments", "deployments/scale"]
  verbs: ["get", "list", "create", "update", "patch", "delete"]
- apiGroups: [""]
  resources: ["services", "pods", "pods/log", "namespaces"]
  verbs: ["get", "list", "create", "update", "patch", "delete"]
- apiGroups: ["metrics.k8s.io"]
  resources: ["pods", "nodes"]
  verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: mcp-orchestrator-manager
subjects:
- kind: ServiceAccount
  name: mcp-orchestrator
  namespace: mcp-system
roleRef:
  kind: ClusterRole
  name: mcp-orchestrator-manager
  apiGroup: rbac.authorization.k8s.io
EOF
```

---

## Support

For issues, contact the MCP Platform team or file a support ticket.

---

## Dev Cluster Setup (kind + Docker Desktop)

This section covers setting up a local development cluster on macOS using kind. The platform ships with `create-cluster.sh` which automates everything.

### What create-cluster.sh Does

1. Detects your environment (cloud vs local)
2. Creates a kind cluster with NodePort fallback mappings
3. Installs MetalLB and auto-configures an IP pool from your Docker network
4. Prints the gateway URL when complete

### Run It

```bash
./create-cluster.sh
```

Options:
```bash
./create-cluster.sh --cluster-name my-cluster
./create-cluster.sh --metallb-range "172.18.0.200-172.18.0.250"
./create-cluster.sh --skip-metallb    # kind only, no MetalLB
```

### Full Bootstrap Sequence

After `create-cluster.sh` completes, run your secrets bootstrap script then:

```bash
# Apply core manifests
kubectl apply -f deploy/k8s/orchestrator.yaml

# Wait for pods
kubectl get pods -n mcp-system -w

# Access the UI (port-forward to Envoy gateway)
kubectl port-forward -n mcp-system svc/<release>-envoy 8080:80 &
open http://localhost:8080
```

### Understanding the Networking on macOS

Docker Desktop on macOS runs inside a hidden Linux VM. This creates a networking wall between your Mac and the cluster:

| What | Address | Reachable from Mac? |
|------|---------|-------------------|
| Kind node | `172.18.0.2` | ✅ via docker-mac-net-connect |
| MetalLB IP | `172.18.0.200` | ❌ ARP doesn't cross VM boundary |
| port-forward | `localhost:8080` | ✅ always works |

**The practical solution for dev:** always use `kubectl port-forward` to access the gateway on your Mac. MetalLB is still correctly configured — it's the right production architecture — but on macOS Docker Desktop the MetalLB IP is only reachable from inside the cluster.

**On real deployments** (bare metal, VMware, cloud) MetalLB IPs are directly routable and consumers hit them without any port-forwarding.

### NodePort Fallback

`create-cluster.sh` also configures kind `extraPortMappings` so NodePorts work via localhost:

| Service | URL |
|---------|-----|
| Envoy HTTP | `http://localhost:18080` |
| Envoy HTTPS | `https://localhost:18443` |

---

## Load Balancer Configuration

The platform supports four load balancer modes configured in `values.yaml` under `loadBalancer.provider`.

### Cloud (Default)

For AWS, GCP, or Azure — no configuration needed. Your cloud controller assigns an external IP automatically.

```yaml
loadBalancer:
  provider: cloud
```

### MetalLB (Bare Metal / Private DC / kind)

For on-premises clusters without a cloud controller. MetalLB assigns IPs from a pool you define.

```yaml
loadBalancer:
  provider: metallb
  metallb:
    install: true
    version: v0.14.5
    ipRange: "10.0.1.200-10.0.1.250"  # must be free IPs on your LAN
```

**First install MetalLB before the Helm chart:**
```bash
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.5/config/manifests/metallb-native.yaml
kubectl wait --namespace metallb-system --for=condition=ready pod --selector=app=metallb --timeout=120s
```

Or apply the standalone config:
```bash
# Edit deploy/k8s/metallb-config.yaml to set your IP range
kubectl apply -f deploy/k8s/metallb-config.yaml
```

**Choosing an IP range:**
- kind/Docker Desktop: use `docker network inspect kind | grep Subnet` — pick `.200-.250` from that subnet
- Private DC: use a free range in your LAN (e.g. `10.0.1.200-10.0.1.250`)
- VMware/Hyper-V: use a free range in your VM network

**BGP mode** (advanced — for DC with BGP-capable routers): see `deploy/k8s/metallb-config.yaml` for the BGP configuration template.

### NodePort (Air-gapped / No LB)

For clusters with no load balancer controller. Access via `<node-ip>:<nodePort>`.

```yaml
loadBalancer:
  provider: nodeport
  nodeport:
    httpPort: 30080
    httpsPort: 30443
```

Access: `http://<node-ip>:30080`

### Existing LB / Ingress Controller

For clusters that already have an nginx-ingress, Traefik, F5 BIG-IP, or similar. MCP creates the Service but doesn't install a controller.

```yaml
loadBalancer:
  provider: existing
  existing:
    # Add annotations for your LB controller
    serviceAnnotations:
      service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    # Optional: pre-assigned static IP
    loadBalancerIP: "10.0.1.100"
    # Optional: create an Ingress resource
    ingress:
      enabled: true
      className: nginx
      host: mcp.example.com
      tls: true
      tlsSecretName: mcp-envoy-tls
```

**Common annotations by provider:**

| Provider | Annotation |
|----------|-----------|
| AWS NLB | `service.beta.kubernetes.io/aws-load-balancer-type: "nlb"` |
| AWS ALB | `service.beta.kubernetes.io/aws-load-balancer-type: "external"` |
| GCP | `cloud.google.com/load-balancer-type: "Internal"` |
| F5 BIG-IP | `cis.f5.com/ip-type: "cluster"` |
| MetalLB pool | `metallb.universe.tf/address-pool: "production-pool"` |

