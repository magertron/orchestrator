# MCP Orchestrator — Helm Chart

Install guide for the MCP Orchestrator Helm chart, focused on **on-prem and
private-cloud Kubernetes deployments**. If you're on a managed cloud
(EKS / GKE / AKS), the chart works there too — see the last appendix.

---

## What you're installing

The chart deploys the full MCP Orchestrator platform to your cluster:

- **Orchestrator** (2 replicas by default) — the control plane, REST API, and
  admin UI
- **Envoy gateway** — TLS termination and HTTP routing to MCP servers
- **PostgreSQL** — in-cluster database for users, roles, policies, audit,
  SSO providers, SCIM tokens
- **mcp-sync sidecar** — watches MCP server CRDs and pushes Envoy routes
- **Namespaces** for MCP servers (`mcp-prod`, `mcp-staging`, `mcp-dev`)
- **Network policies** isolating MCP server namespaces
- **RBAC** — a ServiceAccount + ClusterRole granting the orchestrator the
  k8s permissions it needs to deploy MCP servers on your behalf
- **CRDs** — the `mcp.io/McpRoute` custom resource used internally

After install you log in as `admin / admin`, change the password, optionally
apply an Enterprise license, and start deploying MCP servers.

---

## Prerequisites

You need these BEFORE running `helm install`. The chart won't generate them
for you (by design — you own these secrets).

### 1. A Kubernetes cluster

- **Version**: 1.28 or newer
- **Who you talk to when you configure it**: k8s admin (you)
- **Supported flavors for on-prem**:
  - [k3s](https://k3s.io) — recommended for single-box deployments
  - kubeadm-installed — for multi-node production clusters
  - RKE2 / OpenShift / Rancher — also fine, not explicitly tested

> **If using k3s, disable its bundled Traefik:**
> ```
> curl -sfL https://get.k3s.io | sh -s - --disable=traefik
> ```
> Otherwise Traefik and Envoy both try to bind port 443 and one will fail.

### 2. Helm 3.x and kubectl

```
helm version    # want v3.x
kubectl version --client
kubectl get nodes     # confirms you can reach the cluster
```

### 3. A TLS certificate for your hostname

MCP Orchestrator terminates TLS at the Envoy gateway using a cert you
provide. Pick the hostname customers will use to reach the orchestrator
(e.g. `mcp.yourcompany.com`) and get a cert+key for it.

**Options to get a cert:**
- Let's Encrypt via certbot (free, 90-day rotation)
- Your company's internal CA
- A commercial cert vendor

You need two files: `tls.crt` (full chain) and `tls.key` (private key).

The chart CAN generate a self-signed cert if you don't provide one, but
self-signed certs only work for testing — browsers warn and IdPs will
reject them.

### 4. JWT signing keypair

The orchestrator signs session tokens with RS256. Generate a keypair:

```
openssl genpkey -algorithm RSA -out jwt.key -pkeyopt rsa_keygen_bits:2048
openssl rsa -in jwt.key -pubout -out jwt.pub
```

Keep these files safe. Rotating them invalidates all active user sessions.

### 5. metrics-server (optional but recommended)

Needed for CPU/memory charts in the UI and for HorizontalPodAutoscaler
on Envoy. Most production clusters have it already. If not:

```
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

For non-cloud clusters you may need to add `--kubelet-insecure-tls` to the
metrics-server deployment args.

### 6. License file (optional — Enterprise tier only)

Free tier covers core deployment, health monitoring, scaling, RBAC.
Enterprise tier adds SSO, SCIM, governance, audit export, webhooks,
deployment history, rollback.

If you have a license JSON file, you can apply it at install time or
later. See "Apply a license" below.

---

## Choose your ingress path

Your cluster needs to expose the Envoy gateway to the outside world
somehow. This is the single biggest decision for on-prem installs.

Pick one of these based on your environment:

### NodePort (recommended for single-box / small installs)

Envoy runs as a `NodePort` Service on port 30443 (configurable). You
configure your firewall or router to forward external 443 (or any port)
to `<cluster-node-ip>:30443`. Works everywhere, no k8s add-ons required.

**values.yaml**:
```yaml
loadBalancer:
  provider: nodeport
  nodeport:
    httpsPort: 30443   # can be 30000-32767
```

### MetalLB (multi-node bare-metal with LAN IP pool)

MetalLB assigns a "real" LoadBalancer IP from a pool you give it. Good
when you have multiple cluster nodes and want a single stable IP that
follows the Envoy pod.

**Requires**: a block of IPs on your LAN that aren't being used by DHCP.

**values.yaml**:
```yaml
loadBalancer:
  provider: metallb
  metallb:
    install: true                # chart installs MetalLB
    ipRange: "10.0.1.240-10.0.1.250"   # adjust to YOUR LAN
```

### Existing ingress controller (nginx-ingress, Traefik, etc.)

If your cluster already has an ingress controller handling TLS and HTTP
routing for other apps, use it for the orchestrator too.

**values.yaml**:
```yaml
loadBalancer:
  provider: existing
  existing:
    ingress:
      enabled: true
      className: nginx
      host: mcp.yourcompany.com
      tls: true
      tlsSecretName: mcp-envoy-tls
```

### Cloud LoadBalancer (EKS / GKE / AKS)

Default mode. Your cloud provider's LoadBalancer controller assigns a
public IP automatically. See the cloud appendix at the bottom.

---

## Install

### Minimum viable install (Free tier, self-signed cert, NodePort)

Fine for kicking the tires. **Not for production.**

```
helm install mcp ./helm/orchestrator \
  --namespace mcp-system --create-namespace \
  --set loadBalancer.provider=nodeport \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub
```

### Production install (Free tier)

```
helm install mcp ./helm/orchestrator \
  --namespace mcp-system --create-namespace \
  --set loadBalancer.provider=nodeport \
  --set orchestrator.env.apiPublicUrl=https://mcp.yourcompany.com \
  --set secrets.dbPassword='<strong-password>' \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub \
  --set-file tls.cert=tls.crt \
  --set-file tls.key=tls.key
```

### Production install (Enterprise tier with license)

Same as above plus one more flag:

```
  --set-file license.file=license.json
```

### What the flags mean

| Flag | Purpose |
|---|---|
| `--namespace mcp-system --create-namespace` | Install into a dedicated namespace |
| `loadBalancer.provider=...` | Pick your ingress path (see above) |
| `orchestrator.env.apiPublicUrl=...` | Public HTTPS URL for SCIM meta.location + OIDC redirects. Must match the URL customers actually use. |
| `secrets.dbPassword=...` | Set the PostgreSQL password. Default is `changeme` — don't ship it that way. |
| `--set-file secrets.jwtPrivateKey=...` | JWT signing key (RS256 PEM) |
| `--set-file secrets.jwtPublicKey=...` | JWT verification key (RS256 PEM) |
| `--set-file tls.cert=...` | Your TLS cert (full chain PEM) |
| `--set-file tls.key=...` | Your TLS private key (PEM) |
| `--set-file license.file=...` | Enterprise license JSON (optional) |

---

## After install

### 1. Watch the rollout

```
kubectl get pods -n mcp-system -w
```

Expected pods (give it 2-3 minutes on first install):

```
mcp-mcp-orchestrator-xxxxx-xxxx     2/2 Running
mcp-mcp-orchestrator-xxxxx-xxxx     2/2 Running
mcp-mcp-orchestrator-envoy-xxxxx    1/1 Running
mcp-mcp-orchestrator-envoy-xxxxx    1/1 Running
mcp-mcp-orchestrator-postgres-xxxx  1/1 Running
```

If anything stays in `Init` or `Pending` for more than a minute, see
Troubleshooting below.

### 2. Access the dashboard

For NodePort mode, if you haven't configured your firewall yet, quick test
via port-forward:

```
kubectl port-forward svc/mcp-mcp-orchestrator-envoy \
  -n mcp-system 8443:443
```

Then open `https://localhost:8443` in your browser. Self-signed cert warning
expected if you didn't provide a real cert.

Log in with:
- Username: `admin`
- Password: `admin`

**Change the password immediately** via Users tab → admin → Password.

### 3. Apply a license (skip if Free tier is fine)

If you didn't pass `--set-file license.file=...` at install:

```
kubectl create secret generic mcp-license \
  --from-file=license.json=/path/to/license.json \
  -n mcp-system

kubectl rollout restart deployment/mcp-mcp-orchestrator -n mcp-system
```

After the pods restart, check that Enterprise features unlock:
- In the UI, the "Governance" tab should no longer show an upgrade prompt
- The "Identity Providers" page should be accessible

### 4. Configure SSO (Enterprise — optional)

Admin → "Identity Providers" tab → "+ Add provider"

Fill in the OIDC or SAML details from your IdP (Okta, Azure AD, Google
Workspace, etc.). For OIDC you'll typically need:
- Discovery URL (e.g. `https://your-org.okta.com/.well-known/openid-configuration`)
- Client ID
- Client Secret
- Redirect URI: `<apiPublicUrl>/api/v1/auth/sso/oidc/callback`

If you want users to be auto-provisioned on first SSO login, set:

```yaml
orchestrator:
  env:
    ssoAutoProvisionDomains: "yourcompany.com,partner.com"
```

Then `helm upgrade` to apply.

### 5. Configure SCIM (Enterprise — optional)

Admin → "Identity Providers" tab → "SCIM Tokens" sub-tab → "+ Mint token"

The modal shows the raw token exactly once. **Copy it immediately** —
after dismissal, only the prefix is visible and you can't retrieve the
raw value. If you lose the token, revoke and mint a new one.

In your IdP, create a SCIM 2.0 app pointing at:
- Base URL: `<apiPublicUrl>/scim/v2`
- Authentication: OAuth Bearer Token
- Token: the raw token you just minted

Most IdPs have a "Test API Credentials" button. That's the fastest way
to confirm the integration works.

---

## Day-2 operations

### Upgrade the chart

```
helm upgrade mcp ./helm/orchestrator \
  --namespace mcp-system \
  --reuse-values         # keeps your install-time --set values
```

**CRDs are NOT upgraded by `helm upgrade`** — that's intentional (helm
treats CRDs as install-only because they can be destructive). If the
chart ships new CRD versions, apply them manually:

```
kubectl apply -f helm/orchestrator/crds/
```

### Roll pods after a config change

```
kubectl rollout restart deployment/mcp-mcp-orchestrator -n mcp-system
kubectl rollout restart deployment/mcp-mcp-orchestrator-envoy -n mcp-system
```

### Uninstall

```
helm uninstall mcp -n mcp-system
```

This removes Deployments, Services, Secrets, ConfigMaps. The PersistentVolumeClaim
for PostgreSQL is **preserved** — your data survives uninstall. To also drop
the database:

```
kubectl delete pvc -l app=mcp-orchestrator -n mcp-system
kubectl delete namespace mcp-system
```

CRDs and the namespaces you created (`mcp-prod`, etc.) are also preserved.
Delete them manually if you want a clean slate:

```
kubectl delete crd mcproutes.mcp.io
kubectl delete namespace mcp-prod mcp-staging mcp-dev
```

---

## Troubleshooting

### Orchestrator pods stuck in Init

Most common cause: **PostgreSQL isn't ready yet**. The orchestrator waits
for Postgres readiness before starting. Check the Postgres pod:

```
kubectl logs mcp-mcp-orchestrator-postgres-xxxxx -n mcp-system
```

If you see `initdb` errors, the schema init scripts may have failed.
Delete the PVC and reinstall (data loss acceptable on first install):

```
helm uninstall mcp -n mcp-system
kubectl delete pvc -l app=mcp-orchestrator -n mcp-system
helm install mcp ... # (rerun your install command)
```

### Envoy pod won't start — "cannot load TLS certificate"

Check that the `mcp-envoy-tls` Secret exists:

```
kubectl get secret mcp-envoy-tls -n mcp-system -o yaml
```

If it's missing, your install didn't pass `--set-file tls.cert=...` AND
you also disabled auto-generation. Re-run install with a valid cert+key
OR with `tls.create=true` (the default) to let the chart self-sign.

### Cannot log in — "Invalid credentials" with admin / admin

The admin seed is idempotent — if the database already has an admin user
from a previous install, the password hash from values.yaml won't be
re-applied. Reset the admin password directly:

```
kubectl exec -it mcp-mcp-orchestrator-postgres-xxxxx -n mcp-system -- \
  psql -U mcp -d mcp_platform -c \
  "UPDATE users SET password_hash = '\$2a\$12\$0OAq9ZVN96/RLKJngYzLIed1TekrTKUbdKjSDfdYCSmsTNrea7VaG' WHERE username = 'admin';"
```

(That hash is `admin`. Change the password via UI after.)

### SCIM endpoints return HTML instead of JSON

This happens when your IdP hits a path the orchestrator doesn't handle —
for v1, that's Groups (`/scim/v2/Groups`). The orchestrator returns a
proper SCIM 404 with `application/scim+json` content-type. If you see
HTML, you're on a version older than v1.2.0 — upgrade the chart.

### Okta SCIM "Test API Credentials" fails with 401

Verify you picked the **Bearer Token** variant of Okta's SCIM test app,
not the **Header Auth** variant. The Header Auth variant sends the raw
token with no `Bearer ` prefix, which our orchestrator rejects. The
Bearer Token variant is the correct choice for our implementation.

### SCIM meta.location points at localhost

You forgot to set `orchestrator.env.apiPublicUrl` at install time. Most
SCIM operations still work (IdPs use the Base URL they were configured
with for actual requests), but meta.location in response bodies will
be cosmetically wrong. Fix with:

```
helm upgrade mcp ./helm/orchestrator \
  --namespace mcp-system --reuse-values \
  --set orchestrator.env.apiPublicUrl=https://mcp.yourcompany.com

kubectl rollout restart deployment/mcp-mcp-orchestrator -n mcp-system
```

### OIDC login redirects to localhost

Same root cause as above — `apiPublicUrl` not set. Fix the same way,
then go to Identity Providers and click through the OIDC provider's
details to re-trigger the redirect URL calculation. You may need to
delete and re-create the provider if the cached URL persists.

### Network policies block legit traffic

The chart creates NetworkPolicies on MCP server namespaces to restrict
ingress to the `mcp-system` namespace only. If you're running a CNI
that doesn't fully support NetworkPolicy (Flannel in default mode is a
common offender), pods in MCP namespaces won't be reachable.

Options:
1. Switch to a CNI that supports NetworkPolicy (Calico, Cilium)
2. Disable network policies: `--set networkPolicy.enabled=false`

### "Still showing Free tier after applying license"

Two things to check:

1. Did you restart the orchestrator pods after creating the Secret?
   ```
   kubectl rollout restart deployment/mcp-mcp-orchestrator -n mcp-system
   ```

2. Is the Secret actually mounted? Exec into the orchestrator and check:
   ```
   kubectl exec -it mcp-mcp-orchestrator-xxxxx -c orchestrator -n mcp-system -- \
     ls -la /etc/mcp-license/
   ```
   You should see `license.json`. If not, the Secret name probably doesn't
   match `license.secretName` (default: `mcp-license`).

---

## Full values reference

See [values.yaml](./values.yaml) for the full configuration surface. Key
knobs you'll commonly override:

| Key | Default | Purpose |
|---|---|---|
| `orchestrator.replicaCount` | 2 | Orchestrator HA replicas |
| `orchestrator.env.apiPublicUrl` | `""` | Public URL for SCIM/OIDC |
| `orchestrator.env.ssoAutoProvisionDomains` | `""` | SSO JIT allowlist |
| `envoy.replicaCount` | 2 | Envoy HA replicas |
| `loadBalancer.provider` | `cloud` | `cloud`/`metallb`/`nodeport`/`existing` |
| `postgresql.storage.size` | `5Gi` | DB volume size |
| `postgresql.storage.storageClassName` | (default) | Pick a storage class if needed |
| `namespaces` | `[mcp-prod, mcp-staging, mcp-dev]` | MCP server namespaces to create |
| `networkPolicy.enabled` | `true` | NetworkPolicy creation |
| `secrets.dbPassword` | `changeme` | PostgreSQL password |
| `license.secretName` | `mcp-license` | Name of the license Secret |
| `seed.adminPassword` | `admin` | Default admin password (display only) |
| `seed.adminPasswordHash` | (bcrypt of admin) | Actual hash applied to the users table |

---

## Appendix: Cloud Kubernetes (EKS / GKE / AKS)

Works with default settings. Your cloud provider's LoadBalancer controller
handles the public IP automatically. You still bring your own cert and JWT
keys:

```
helm install mcp ./helm/orchestrator \
  --namespace mcp-system --create-namespace \
  --set orchestrator.env.apiPublicUrl=https://mcp.yourcompany.com \
  --set-file secrets.jwtPrivateKey=jwt.key \
  --set-file secrets.jwtPublicKey=jwt.pub \
  --set-file tls.cert=tls.crt \
  --set-file tls.key=tls.key
```

Then point `mcp.yourcompany.com` DNS at the LoadBalancer IP:

```
kubectl get svc mcp-mcp-orchestrator-envoy -n mcp-system
# Look at EXTERNAL-IP column
```

Cert-manager is a common choice for automating TLS on cloud clusters.
Integration is straightforward but out of scope for this guide.

---

## Support

For issues, check the orchestrator logs first:

```
kubectl logs -n mcp-system -l app=mcp-orchestrator -c orchestrator --tail=100
```

File bug reports with the log output attached.
