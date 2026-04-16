# MCP Orchestrator

**Kubernetes-native control plane for managing Model Context Protocol (MCP) servers at enterprise scale.**

MCP Orchestrator deploys, monitors, and governs MCP servers across multi-tenant Kubernetes environments. It provides a unified dashboard, CLI, and API for platform teams to manage the lifecycle of MCP servers — from deployment to scaling, health monitoring, governance enforcement, and rollback.

---

## Architecture

```
                                    ┌─────────────────────────────────────────────────────────┐
                                    │                    mcp-system namespace                  │
                                    │                                                         │
                                    │  ┌─────────────────────────────────────────────────┐    │
   Clients                          │  │          Orchestrator Pod (×2 HA)                │    │
  ┌─────────┐    HTTPS / REST       │  │  ┌─────────────────┐  ┌──────────────────┐     │    │
  │ Browser │ ──────────────────┐   │  │  │  orchestrator    │  │   mcp-sync       │     │    │
  │ (React) │                   │   │  │  │  (C++23)         │  │   (Go sidecar)   │     │    │
  └─────────┘                   │   │  │  │                  │  │                  │     │    │
                                │   │  │  │  • REST API      │  │  • Watches CRDs  │     │    │
  ┌─────────┐                   ▼   │  │  │  • JWT Auth      │  │  • Leader elect  │     │    │
  │ mcpctl  │ ──────────── ┌────────┤  │  │  • RBAC Engine   │  │  • Route sync    │     │    │
  │  (CLI)  │              │ Envoy  │  │  │  • Governance    │  │                  │     │    │
  └─────────┘              │Gateway │  │  │  • xDS Server    │  └──────────────────┘     │    │
                           │ (×2)   │  │  │  • Health Mon.   │                           │    │
  ┌─────────┐              │        │  │  │  • Webhooks      │                           │    │
  │  AI     │ ─── MCP ──── │  ADS   │  │  │  • Audit Logger  │                           │    │
  │ Agents  │  Protocol    │ (xDS)  │  │  └────────┬─────────┘                           │    │
  └─────────┘              └────┬───┤  │           │                                     │    │
                                │   │  └───────────┼─────────────────────────────────────┘    │
                                │   │              │                                          │
                                │   │     ┌────────▼─────────┐                                │
                                │   │     │   PostgreSQL      │                                │
                                │   │     │   • Users/RBAC    │                                │
                                │   │     │   • Audit events  │                                │
                                │   │     │   • Server state  │                                │
                                │   │     │   • Policies      │                                │
                                │   │     │   • Webhooks      │                                │
                                │   │     └──────────────────┘                                │
                                │   └─────────────────────────────────────────────────────────┘
                                │
                    ┌───────────┼──────────────────────────────┐
                    │           │    MCP Server Namespaces      │
                    │           ▼                               │
                    │  ┌──────────────┐  ┌──────────────┐     │
                    │  │  mcp-prod    │  │  mcp-staging  │     │
                    │  │  ┌────────┐  │  │  ┌────────┐  │     │
                    │  │  │MCP Srv │  │  │  │MCP Srv │  │     │
                    │  │  │(fast-  │  │  │  │(code-  │  │     │
                    │  │  │ time)  │  │  │  │ asst.) │  │     │
                    │  │  └────────┘  │  │  └────────┘  │     │
                    │  └──────────────┘  └──────────────┘     │
                    │           ┌──────────────┐               │
                    │           │  mcp-dev      │               │
                    │           │  ┌────────┐   │               │
                    │           │  │MCP Srv │   │               │
                    │           │  └────────┘   │               │
                    │           └──────────────┘               │
                    └──────────────────────────────────────────┘
```

## Features

### License Tier Gating
- **FREE** — Core deployment, health monitoring, scaling
- **Pro** — Live metrics charts, additional observability features
- **Enterprise** — Audit trail, governance policies, webhooks, deployment history & rollback
- Upgrade prompts with direct links shown for locked features
- License loaded from file at startup (`/etc/mcp-license/license.json`)

### Server Lifecycle Management
- **Deploy** MCP servers from container images with configurable CPU, memory, replicas, and transport
- **Scale** replicas up/down with slider or API
- **Restart** with zero-downtime rolling restarts
- **Undeploy** with full resource cleanup
- **Rollback** to any previous deployment version from the history timeline

### Health Monitoring & Metrics
- Real-time CPU and memory metrics from Kubernetes metrics-server API
- Live area charts in the server detail panel (polling every 5s)
- Automatic state transitions: Deploying → Running → Degraded → Failed
- Auto tool discovery when servers become ready (MCP Streamable HTTP protocol)

### Multi-Tenant Namespace Isolation
- Roles define `allowed_namespaces` — users can only write to their assigned namespaces
- Read operations are unrestricted — anyone can view all servers
- Deploy modal shows only the namespaces the current user has access to
- Four seed roles: Platform Admin (`*`), Deploy Manager (prod/staging), Operator (dev/staging), Viewer (read-only)

### Governance Policy Engine
- Dot-path field resolution with wildcard array support
- Operators: `equals`, `not_equals`, `in`, `not_in`, `gte`, `lte`, `exists`, `regex`
- Two severity levels: `error` (blocks deploy), `warning` (flags for review)
- **Namespace-scoped policies** — `enterprise-standard` applies to prod/staging, `dev-relaxed` applies to dev only
- Dry-run evaluator — paste a spec and see violations before deploying
- Export/import policies as JSON for multi-cluster management

### RBAC & Authentication
- JWT RS256 authentication with configurable TTL
- Roles loaded from PostgreSQL (single source of truth)
- Permission string → Action bitmask conversion at startup
- Namespace isolation enforced on all write operations
- Token auto-refresh (50-minute cycle, 60-minute expiry)
- Global 401 handler with automatic logout

### Webhook Notifications
- Async HTTP POST delivery with background worker thread
- Slack-aware formatting (detects `hooks.slack.com`, sends emoji-rich messages)
- Event type and namespace filtering per webhook
- HMAC signature support (`X-MCP-Signature` header)
- Retry logic: 3 attempts with exponential backoff
- Test button to verify webhook connectivity

### Envoy Gateway
- Dynamic xDS (ADS) — push-based route updates with sub-second latency
- Automatic cluster and listener configuration when servers are deployed
- **Prefix rewriting** — strips `/mcp/<server-name>/` before forwarding to pods, so MCP servers receive clean paths (e.g. `/http`, `/sse`)
- **Dynamic RDS** — route configuration served via xDS Route Discovery Service, no static bootstrap routes needed
- HTTPS termination with TLS certificates
- JSON access logging
- Supports MetalLB (bare metal), cloud load balancers (AWS/GCP/Azure), NodePort, and existing Ingress controllers

### Deployment History & Rollback
- Full audit trail of every deployment event (deploy, scale, update, restart, health change)
- Timeline view with color-coded event types
- One-click rollback to any previous version
- Spec snapshots stored in audit events

---

## Quick Start

### Prerequisites
- Kubernetes cluster (kind, EKS, GKE, AKS, or on-prem)
- Helm 3.x
- `kubectl` configured
- metrics-server installed (`kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml`)

### Dev Cluster (kind + Docker Desktop)

The platform ships with a bootstrap script that creates a kind cluster, installs MetalLB, and auto-configures networking:

```bash
./create-cluster.sh
```

For details on load balancer options and macOS networking see [INSTALL.md](INSTALL.md#dev-cluster-setup-kind--docker-desktop).

### Generate JWT Keys
```bash
openssl genpkey -algorithm RSA -out jwt.key -pkeyopt rsa_keygen_bits:2048
openssl rsa -in jwt.key -pubout -out jwt.pub
```

### Install with Helm
```bash
helm install mcp ./helm/orchestrator -n mcp-system --create-namespace \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub
```

### Access the Dashboard
```bash
# Port-forward to Envoy gateway (recommended for dev)
kubectl port-forward svc/mcp-mcp-orchestrator-envoy -n mcp-system 8080:80 &
open http://localhost:8080
# Login: admin / admin
```

### Install the CLI
```bash
cd mcpctl
make build
sudo cp mcpctl /usr/local/bin/
mcpctl login http://localhost:8080 admin admin
mcpctl servers
```

---

## CLI Reference

```
mcpctl v1.0.0 — MCP Orchestrator CLI

CONNECTION:
  login <url> <user> <pass>     Login to orchestrator
  logout                        Clear saved credentials
  status                        Show connection status

SERVERS:
  servers                       List all deployed servers
  deploy <name> <ns> <image>    Deploy a new MCP server
  undeploy <ns> <name>          Remove a server
  scale <ns> <name> <replicas>  Scale server replicas
  restart <ns> <name>           Rolling restart
  logs <ns> <name>              View server pod logs
  history <ns> <name>           View deployment history

GOVERNANCE:
  governance list               List all policies
  governance evaluate <file>    Evaluate spec against policies
  governance export [file]      Export policies as JSON

OBSERVABILITY:
  audit [limit]                 View audit log

USERS:
  users                         List all users
```

### Deploy Example
```bash
mcpctl deploy my-server mcp-prod ghcr.io/ibm/fast-time-server \
  --tag latest \
  --port 8080 \
  --transport streamable_http \
  --team devops \
  --memory 512
```

### Governance Evaluation
```bash
# Create a spec file
cat > spec.json << 'EOF'
{
  "name": "my-server",
  "namespace": "mcp-prod",
  "image": "my-image",
  "transport": "streamable_http",
  "replicas": 1,
  "cpu_limit": 1.0,
  "memory_limit_mb": 512,
  "labels": {"team": "platform"}
}
EOF

# Evaluate against all enabled policies for mcp-prod
mcpctl governance evaluate spec.json --namespace mcp-prod
```

---

## Helm Configuration

Key values in `values.yaml`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `orchestrator.replicaCount` | `2` | Orchestrator HA replicas |
| `orchestrator.image.tag` | `v1` | Orchestrator image tag |
| `envoy.enabled` | `true` | Deploy Envoy gateway |
| `loadBalancer.provider` | `cloud` | LB mode: `cloud` \| `metallb` \| `nodeport` \| `existing` |
| `loadBalancer.metallb.ipRange` | `172.18.0.200-172.18.0.250` | MetalLB IP pool (metallb mode) |
| `loadBalancer.nodeport.httpPort` | `30080` | NodePort for HTTP (nodeport mode) |
| `loadBalancer.existing.serviceAnnotations` | `{}` | Annotations for existing LB (existing mode) |
| `postgresql.storage.size` | `5Gi` | Database storage |
| `postgresql.storage.storageClassName` | _(default)_ | Storage class (longhorn recommended) |
| `namespaces` | `[mcp-prod, mcp-staging, mcp-dev]` | MCP server namespaces |
| `secrets.dbPassword` | `changeme` | PostgreSQL password |
| `jwt.issuer` | `https://auth.acme.internal` | JWT issuer claim |
| `jwt.tokenTTL` | `3600` | JWT expiry in seconds |
| `networkPolicy.enabled` | `true` | Enable namespace isolation |

See [INSTALL.md](INSTALL.md#load-balancer-configuration) for full load balancer configuration details.

### Production Install
```bash
helm install mcp ./helm/orchestrator -n mcp-system --create-namespace \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub \
  --set secrets.dbPassword=my-secure-password \
  --set orchestrator.replicaCount=3 \
  --set postgresql.storage.storageClassName=longhorn \
  --set envoy.autoscaling.maxReplicas=20
```

---

## API Reference

### Authentication
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/auth/login` | Login with username/password |
| `POST` | `/api/v1/auth/refresh` | Refresh JWT token |
| `GET` | `/api/v1/auth/my-namespaces` | Get allowed namespaces for current user |

### Servers
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/servers` | List all servers |
| `POST` | `/api/v1/servers` | Deploy a new server |
| `GET` | `/api/v1/servers/{ns}/{name}` | Get server details |
| `PUT` | `/api/v1/servers/{ns}/{name}` | Update server spec |
| `DELETE` | `/api/v1/servers/{ns}/{name}` | Undeploy server |
| `POST` | `/api/v1/servers/{ns}/{name}/scale` | Scale replicas |
| `POST` | `/api/v1/servers/{ns}/{name}/restart` | Rolling restart |
| `GET` | `/api/v1/servers/{ns}/{name}/logs` | Get pod logs |
| `GET` | `/api/v1/servers/{ns}/{name}/tools` | List discovered tools |
| `POST` | `/api/v1/servers/{ns}/{name}/tools/refresh` | Re-discover tools |
| `GET` | `/api/v1/servers/{ns}/{name}/history` | Deployment history |
| `POST` | `/api/v1/servers/{ns}/{name}/rollback` | Rollback to previous spec |

### Governance
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/governance/policies` | List policies |
| `POST` | `/api/v1/governance/policies` | Create policy |
| `GET` | `/api/v1/governance/policies/{id}` | Get policy |
| `PUT` | `/api/v1/governance/policies/{id}` | Update policy |
| `DELETE` | `/api/v1/governance/policies/{id}` | Delete policy |
| `POST` | `/api/v1/governance/evaluate` | Evaluate spec against policies |
| `GET` | `/api/v1/governance/export` | Export all policies as JSON |
| `POST` | `/api/v1/governance/import` | Import policies from JSON |

### Webhooks
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/webhooks` | List webhooks |
| `POST` | `/api/v1/webhooks` | Create webhook |
| `GET` | `/api/v1/webhooks/{id}` | Get webhook |
| `PUT` | `/api/v1/webhooks/{id}` | Update webhook |
| `DELETE` | `/api/v1/webhooks/{id}` | Delete webhook |
| `POST` | `/api/v1/webhooks/{id}/test` | Send test notification |

### Users & RBAC
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/users` | List users |
| `POST` | `/api/v1/users` | Create user |
| `PUT` | `/api/v1/users/{username}/roles` | Update user roles |
| `PUT` | `/api/v1/users/{username}/password` | Change password |
| `DELETE` | `/api/v1/users/{username}` | Delete user |
| `GET` | `/api/v1/roles` | List roles |
| `GET` | `/api/v1/audit` | Query audit log |

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Orchestrator | C++23, Boost.Beast, Boost.Asio |
| MCP Sync Sidecar | Go, controller-runtime, client-go |
| Gateway | Envoy Proxy v1.28 (xDS ADS) |
| Database | PostgreSQL 16 |
| UI | React 18, TypeScript, Vite, Recharts |
| CLI | Go (single binary, zero dependencies) |
| Auth | JWT RS256 (jwt-cpp) |
| Passwords | bcrypt (rg3/libbcrypt) |
| HTTP Client | libcpr, Boost.Beast |
| Packaging | Helm 3, Docker multi-stage builds |

---

## RBAC Model

| Role | Permissions | Namespaces |
|------|------------|------------|
| Platform Admin | All operations | `*` (all) |
| Deploy Manager | Deploy, Scale, Undeploy, Read, ViewLogs | mcp-prod, mcp-staging |
| Operator | Deploy, Scale, Undeploy, Read, ViewLogs | mcp-dev, mcp-staging |
| Viewer | Read, AuditRead | `*` (all, read-only) |

Roles are stored in PostgreSQL and loaded at startup. Custom roles can be created via the API or UI.

---

## License

Proprietary. All rights reserved.
