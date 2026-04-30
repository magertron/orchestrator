# MCP Orchestrator — HA Deployment Guide (v1.7)

This guide covers running MCP Orchestrator in multi-replica high-availability
configuration. Audience: platform engineers, SREs, anyone responsible for
production reliability.

For pre-install evaluation guidance, see [README.md](../README.md). For
post-install operational notes, run `helm get notes mcp-orchestrator -n mcp-system`.

## What you should know up front

**v1.7 multi-replica HA is architecturally complete and validated in lab
conditions.** It is NOT a substitute for an SLA-backed managed service.
Sections labeled *Tested* describe behavior we've validated in a development
cluster. Sections labeled *Designed* describe expected behavior based on the
architecture; production-grade validation is v1.8 work.

If you're considering this for a regulated environment (PCI, HIPAA, FedRAMP),
talk to us first.

---

## 1. What HA gives you (and what it doesn't)

### What HA gives you

**Active-passive orchestration with single-writer guarantees.** With multiple
orchestrator pods running, exactly one acts as the leader at any given time.
The leader handles all state mutations: deploying servers, updating routes,
running health probes. Followers serve reads (the API for listing servers
and routes works on every pod) and stay synchronized via PostgreSQL.

**Fast failover.** When the leader pod fails (crash, eviction, network
partition from API server), a warm follower acquires leadership and resumes
mutations within seconds. Existing Envoy connections to the surviving pods
continue to receive xDS configuration updates.

**Consistent state across pods.** All orchestrator pods see the same view of
deployed servers and active routes. The /api/v1/servers and /api/v1/routes
endpoints return identical results regardless of which pod handles the
request.

**Stateless Envoy gateway scaling.** Multiple Envoy pods can run behind the
NodePort service; each independently connects to an orchestrator pod for xDS
config and receives identical configuration.

### What HA does NOT give you

- **Active-active writes.** Mutation operations land on the leader, period.
  Followers reject writes and return a warning in their log.
- **Geographic distribution.** v1.7 assumes single-cluster deployment. Multi-
  cluster federation is out of scope.
- **Zero-downtime upgrades guaranteed.** Rolling upgrades work but the brief
  leadership transition window (~2-3 seconds) may delay individual mutations.
- **Postgres HA.** This guide assumes you've handled Postgres availability
  separately. The chart's bundled Postgres is for development only.

---

## 2. Sizing

### Replica counts

| Use case | Orchestrator replicas | Envoy replicas | Tested |
|----------|----------------------|----------------|--------|
| Development / demo | 1 | 1 | Yes |
| Production single-AZ | 2 | 2-3 | Yes (2x2 only) |
| Production multi-AZ | 3 (one per AZ) | 3+ (one per AZ) | Designed, not validated |
| High-scale | 5+ | 10+ | Designed, not validated |

**Recommendation for first production deployment**: 2 orchestrator replicas
+ 2 Envoy replicas. This is the configuration we have empirical proof of.
Scale up after observing steady-state behavior for at least one week.

### Resource requirements per pod

These are conservative defaults for the chart. Tune based on observed usage.

**Orchestrator pod**:
- CPU request: 200m, limit: 1000m
- Memory request: 256Mi, limit: 512Mi
- Each pod runs: REST API, xDS gRPC server, ext_authz gRPC server,
  HealthMonitor, registry polling thread, xDS snapshot polling thread

**Envoy pod**:
- CPU request: 100m, limit: 500m
- Memory request: 128Mi, limit: 256Mi
- Each Envoy maintains one persistent xDS gRPC stream to one orchestrator pod

**PostgreSQL**:
- Storage: 10Gi minimum for v1.7. Grows over time (xds_snapshots and audit
  events accumulate; v1.8 adds retention policies).
- For production: bring your own Postgres. The chart's bundled Postgres has
  no replication, no backup automation, and uses a single PVC.

### Cluster-level requirements

- Kubernetes 1.27+ (uses coordination.k8s.io/v1 Lease API)
- One ServiceAccount with permissions for: Lease (get/create/update),
  Pods/Deployments/Services in MCP namespaces, ConfigMaps and Secrets in
  the orchestrator's namespace
- Persistent storage for Postgres (or external Postgres connection)
- LoadBalancer or NodePort access to Envoy's port 30443

---

## 3. Configuration

### Chart values for HA

```yaml
orchestrator:
  replicaCount: 2          # minimum for HA; tested at 2

  env:
    leaderElectionEnabled: true     # required for multi-replica
    xdsPollIntervalMs: "1000"       # default 1s; tune for change rate
    registryPollIntervalMs: "1000"  # default 1s; tune for change rate

envoy:
  replicaCount: 2          # match orchestrator for HA evaluation;
                           # tested at 2

# Lease behavior — defaults are appropriate for most clusters.
# Override only if you understand the implications.
leaderLock:
  leaseDurationSeconds: 15    # how long a leader holds before requiring
                              # renewal; if leader dies, follower waits
                              # this long before taking over (worst case)
  renewIntervalSeconds: 5     # how often the leader renews; must be < lease
  retryIntervalSeconds: 1     # follower wake-up frequency to attempt
                              # acquisition; lower = faster failover, more
                              # API server load
```

### Environment variables (advanced)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_LEADER_ELECTION_ENABLED` | `false` | Set to `true` when replicaCount > 1. Auto-set by chart. |
| `MCP_XDS_POLL_INTERVAL_MS` | `1000` | xDS snapshot polling interval; range 100-60000. |
| `MCP_REGISTRY_POLL_INTERVAL_MS` | `1000` | Server registry polling interval; range 100-60000. |
| `MCP_VERSION` | injected | Chart appVersion; surfaced in logs. |
| `MCP_GIT_SHA` | injected | Image build git SHA; for support diagnostics. |

### Postgres connection

For production, configure an external Postgres:

```yaml
postgresql:
  enabled: false                   # disable bundled Postgres

externalPostgres:
  host: "pg.production.example.com"
  port: 5432
  database: "mcp_platform"
  # username and password come from the Secret created by:
  # kubectl create secret generic mcp-orchestrator-secrets \
  #   --from-literal=MCP_DB_USERNAME=mcp \
  #   --from-literal=MCP_DB_PASSWORD=... \
  #   -n mcp-system
```

The orchestrator's three-tier Secret fallback respects pre-created Secrets,
so you can manage credentials externally (Vault, AWS Secrets Manager, etc).
See README.md *Production hardening* for details.

---

## 4. Deployment topology

### Required Kubernetes resources (chart-managed)

The chart creates:
- One Deployment for orchestrator pods (replicas configurable)
- One Deployment for Envoy pods (replicas configurable)
- One StatefulSet or Deployment for bundled Postgres (dev only)
- ClusterIP service `mcp-orchestrator` (REST + gRPC ports for client access)
- Headless ClusterIP service `mcp-orchestrator-xds-headless` (port 18000;
  Envoy connects here for xDS, returns all pod IPs for DNS-based load
  balancing)
- NodePort service `mcp-orchestrator-envoy` (port 30443 for HTTPS traffic)
- ServiceAccount, Role, RoleBinding for Lease access in mcp-system
- Lease resource: `mcp-orchestrator-leader` (created by orchestrator on
  first leader acquisition; not chart-managed)

### Service discovery

**For clients of the MCP servers**: connect to the Envoy NodePort or
LoadBalancer at `:30443`. Envoy routes `/mcp/<namespace>/<server-name>/...`
to the appropriate upstream MCP server.

**For clients of the orchestrator API**: connect to the `mcp-orchestrator`
ClusterIP service on port 8080 (HTTP) or 9090 (gRPC). The Kubernetes service
load-balances across orchestrator pods. Read operations work on any pod;
write operations should land on the leader (the chart's service includes
all pods, so writes may land on a follower and get rejected — see
*Failure modes* below).

**For Envoy → orchestrator (xDS)**: each Envoy pod resolves the headless
service `mcp-orchestrator-xds-headless` via DNS, gets all orchestrator pod
IPs, picks one (Envoy's STRICT_DNS + ROUND_ROBIN), and opens a persistent
gRPC stream. If that orchestrator pod dies, Envoy reconnects to a different
pod from the DNS results.

### Pod placement (designed, not validated)

For multi-AZ deployment, use anti-affinity rules so orchestrator pods
spread across zones:

```yaml
orchestrator:
  affinity:
    podAntiAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          labelSelector:
            matchLabels:
              app: mcp-orchestrator
          topologyKey: topology.kubernetes.io/zone
```

This is conventional Kubernetes scheduling — included here for completeness
but not v1.7-specific.

---

## 5. Operational procedures

### First-time deployment

```
helm install mcp-orchestrator <chart-source> \
  --namespace mcp-system --create-namespace \
  --set orchestrator.replicaCount=2 \
  --set envoy.replicaCount=2
```

After install:
1. Retrieve the bootstrap admin password (see post-install NOTES.txt)
2. Log in, change the password via UI
3. Verify both orchestrator pods are Ready
4. Verify the Lease has a holder: `kubectl get lease -n mcp-system
   mcp-orchestrator-leader -o yaml`
5. Verify both Envoy pods are receiving xDS config (admin endpoint at
   port 9901 inside each pod)

### Scaling orchestrator replicas

```
kubectl scale deployment mcp-orchestrator -n mcp-system --replicas=3
```

The new pod(s) will:
1. Start, run their own seed pass (no-op if DB is non-empty)
2. Initial sync from Postgres for both xDS snapshots and server registry
3. Begin polling
4. Join leader election as a follower (the existing leader retains
   leadership; new pod waits its turn for any future failover)

**Scale-down**: `kubectl scale ... --replicas=1` is safe. If the scaled-down
pod was the leader, the remaining pod(s) take over within seconds.

### Scaling Envoy replicas

```
kubectl scale deployment mcp-orchestrator-envoy -n mcp-system --replicas=3
```

New Envoy pods independently connect to an orchestrator pod via the
headless service and pull current xDS config. No orchestrator-side action
needed.

### Rolling upgrade

```
helm upgrade mcp-orchestrator <chart-source> \
  --namespace mcp-system \
  --reuse-values \
  --set image.tag=v1.7.1
```

Kubernetes performs a rolling upgrade by default: one pod at a time, waiting
for the new pod to be Ready before terminating the old one. During the
upgrade:
- If the leader is replaced, a follower takes over within ~2-3 seconds
- xDS state is preserved (Postgres-backed) — the new leader bootstraps
  from DB before accepting mutations
- Envoy connections rebalance as orchestrator pods come and go

**Mutation latency during upgrade**: brief windows (~1-2 seconds) where a
new leader is bootstrapping may delay individual mutations. Clients should
retry on connection error or 5xx response.

### Configuration changes

Most chart value changes are applied via `helm upgrade --reuse-values
--set <key>=<value>`. Some values (env vars on the orchestrator, polling
intervals) take effect on the next pod restart; trigger by `kubectl
rollout restart deployment/mcp-orchestrator -n mcp-system`.

---

## 6. Failure modes and recovery

This section is honest about what we've tested vs what the architecture is
designed to handle. Nothing here is a substitute for your own load and
chaos testing before production cutover.

### Leader pod crashes (TESTED)

**What happens**: The leader's process exits or pod is terminated. The
Lease's renewTime stops advancing. After `leaseDurationSeconds` (15s
default), followers see the Lease as expired. The first follower to attempt
acquisition wins and sets itself as the new holder. The new leader's
on_become_leader callback sets a flag triggering a force-fetch of the
latest xDS snapshot from Postgres before accepting new mutations.

**Observed recovery**: Warm follower acquires leadership in ~2 seconds when
`retryIntervalSeconds=1`. New leader is fully operational (accepting
writes, all in-memory state synced from DB) within ~3 seconds.

**Recovery action required**: None. This is the designed failover.

### Network partition between orchestrator pods (DESIGNED, NOT TESTED)

**Expected behavior**: Each orchestrator pod connects independently to the
Kubernetes API server (for Lease) and to PostgreSQL (for state). A network
partition between two orchestrator pods themselves doesn't break leader
election as long as both can reach the API server.

**If a pod is partitioned from the API server**: it cannot renew its Lease.
Other pods see the Lease expire and elect a new leader. The partitioned
pod, when it discovers it can't renew, should release leadership in its
own state (in-progress mutations on it would have failed anyway).

**Edge case we have not validated**: what happens if a pod can reach the
API server but not Postgres. Today's design degrades to "pod can serve
reads from in-memory cache, can't write." Proven defensive but not
load-tested.

**Recovery action**: Monitor for pods stuck in CrashLoopBackoff or
unable to reach Postgres. If a pod is reporting connection errors but the
cluster as a whole is healthy, restart that pod.

### PostgreSQL becomes unavailable (NOT TESTED)

**Expected behavior**: All orchestrator pods continue running with their
last-synced in-memory state. Reads continue to work. Writes fail and are
logged. xDS configuration on connected Envoys does NOT change (last-known
state remains in effect).

When Postgres recovers:
- Followers' polling threads catch up via the next sync cycle
- Leader's writes succeed and propagate

**This degraded mode preserves data plane availability** at the cost of
control plane mutability. Existing MCP servers continue to receive traffic
through Envoy.

**Recovery action**: Restore Postgres. Verify orchestrator pods recover
their connections (visible in logs). No manual intervention on
orchestrator pods should be needed.

### Both orchestrator pods become unavailable (DESIGNED)

**Expected behavior**: Connected Envoys retain their last-known xDS
configuration and continue serving traffic. New MCP servers cannot be
deployed and existing ones cannot be reconfigured until at least one
orchestrator pod is restored.

**Recovery action**: Investigate why both pods failed. The chart's
deployment will attempt to reschedule them. If Postgres is healthy, no
data is lost — orchestrator pods recover state from DB on startup.

### Lease key corruption / unexpected state

**Recovery action**: Delete the Lease and let orchestrator pods recreate
it:

```
kubectl delete lease mcp-orchestrator-leader -n mcp-system
```

This is destructive in the sense that all pods will race to acquire the
new Lease, but no application state is lost — leadership is just a
coordination primitive on top of Postgres-backed state.

### Recovery from "everything is wrong"

```
# Worst case: orchestrator state is wedged. State is in Postgres; reset
# the orchestrator pods cleanly.
kubectl rollout restart deployment/mcp-orchestrator -n mcp-system

# Verify recovery
kubectl rollout status deployment/mcp-orchestrator -n mcp-system
kubectl get lease -n mcp-system mcp-orchestrator-leader -o yaml
kubectl logs -n mcp-system -l app=mcp-orchestrator -c orchestrator --tail=50
```

If the issue persists after restart, the problem is in Postgres state or
in chart configuration. Check the post-install notes for chart-specific
diagnostics.

---

## 7. Monitoring and observability (limited in v1.7)

**Honest disclosure**: v1.7 has no Prometheus metrics endpoint. Observability
is via log inspection and Kubernetes-level signals only. v1.8 adds metrics.

### What you can observe today

**Leader identity**:
```
kubectl get lease mcp-orchestrator-leader -n mcp-system \
  -o jsonpath='{.spec.holderIdentity}{"\n"}'
```
Tells you which pod is currently leader. Should be stable; flapping is
a problem signal (investigate Lease renewal failures).

**Lease renewal**:
```
kubectl get lease mcp-orchestrator-leader -n mcp-system \
  -o jsonpath='{.spec.renewTime}{"\n"}'
```
Compare to current time. Should be within `renewIntervalSeconds` (default
5s). Older means the leader is unhealthy.

**Per-pod role** (leader vs follower) via logs:
```
kubectl logs -n mcp-system <pod-name> -c orchestrator | \
  grep -E "acquired leadership|lost leadership|is_leader"
```

**xDS snapshot rate** via Postgres:
```
SELECT COUNT(*), MAX(created_at)
  FROM xds_snapshots
 WHERE created_at > NOW() - INTERVAL '5 minutes';
```
Active deployments = positive count. Long quiet periods = no recent
mutations.

**Server registry consistency** by querying `/api/v1/servers` on multiple
pods (use `kubectl port-forward` to hit specific pods):
```
# Should return the same set of servers from any pod
kubectl port-forward -n mcp-system pod/<orchestrator-pod-1> 18080:8080
curl -k -H "Authorization: Bearer $TOKEN" http://localhost:18080/api/v1/servers
```

### What to alert on (until v1.8 metrics exist)

- Lease holder hasn't changed in 24h AND lease renewTime is stale → leader
  may be wedged
- Multiple orchestrator pods report "acquired leadership" within seconds
  of each other → leadership flapping; investigate Postgres connectivity
  or API server health
- /api/v1/servers returns different results from different pods after
  >5s wait → registry consistency broken; investigate polling thread

These are imperfect proxies for proper metrics. **Don't rely on them for
SLA-bearing production until v1.8.**

---

## 8. Known limitations (v1.7)

**Untested at scale**: We've tested 2×2 (orchestrators × Envoys). Behavior
at higher counts is designed but unvalidated. Production deployments should
start at 2×2 and scale up after observing steady-state behavior.

**No metrics endpoint**: As above. Observability is log-based. v1.8.

**Unbounded snapshot growth**: The xds_snapshots table accumulates one row
per mutation event. v1.7 has no retention policy. Manually clean up
periodically if database storage is a concern. v1.8 adds retention.

**Polling-based propagation**: Followers poll every second by default. There
is a 0-1 second delay between a leader-side mutation and follower
visibility. v1.8 replaces with LISTEN/NOTIFY for sub-100ms propagation.

**REST handler returns 201 on rejected writes**: When a write is sent to
a follower, the leader-gating guard correctly rejects the mutation but the
REST handler still returns HTTP 201. The xds_snapshots table will not have
a corresponding row, but the API client may believe their request succeeded.
Workaround: clients should verify by reading back the resource. Fix planned
for v1.7.x patch.

**`unknown_leader` race in audit field**: During the millisecond window
between leadership acquisition and the LeaderLock's holder identity becoming
readable, snapshots written get `written_by="unknown_leader"`. Cosmetic
traceability gap; doesn't affect correctness. Fix planned for v1.7.x patch.

**Bundled Postgres is dev-only**: No replication, no backup automation,
single PVC. Production should use external Postgres with the
`externalPostgres` chart configuration.

**No automated multi-cluster federation**: v1.7 is single-cluster.
Multi-cluster is out of scope for v1.7 and v1.8.

---

## Appendix: Verifying HA is working

### Quick sanity check (~5 minutes)

```
# All pods running
kubectl get pods -n mcp-system

# Lease has a holder
kubectl get lease mcp-orchestrator-leader -n mcp-system

# Leader is renewing (renewTime is recent)
kubectl get lease mcp-orchestrator-leader -n mcp-system -o yaml | grep renewTime

# Server registry is consistent across pods
LEADER=$(kubectl get lease mcp-orchestrator-leader -n mcp-system \
  -o jsonpath='{.spec.holderIdentity}' | sed 's/_.*//')
FOLLOWER=$(kubectl get pods -n mcp-system -l app=mcp-orchestrator \
  -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | grep -v "^$LEADER$" | head -1)

kubectl port-forward -n mcp-system pod/$LEADER 18080:8080 &
kubectl port-forward -n mcp-system pod/$FOLLOWER 18081:8080 &
sleep 2

# Should return the same JSON
curl -s http://localhost:18080/api/v1/servers | jq -r '.[].name' | sort
curl -s http://localhost:18081/api/v1/servers | jq -r '.[].name' | sort

kill %1 %2
```

### Failover test

```
LEADER=$(kubectl get lease mcp-orchestrator-leader -n mcp-system \
  -o jsonpath='{.spec.holderIdentity}' | sed 's/_.*//')

date +%H:%M:%S.%N
kubectl delete pod -n mcp-system $LEADER
sleep 3

NEW_LEADER=$(kubectl get lease mcp-orchestrator-leader -n mcp-system \
  -o jsonpath='{.spec.holderIdentity}' | sed 's/_.*//')

date +%H:%M:%S.%N
echo "Old leader: $LEADER"
echo "New leader: $NEW_LEADER"
```

Expect new leader within ~2-3 seconds. Cluster should remain operational
throughout.

---

## Getting help

For deployment issues, configuration questions, or production guidance:

- GitHub issues: https://github.com/mcp-platform/mcp-orchestrator/issues
- Production support: contact your account team

For broader architectural questions before deployment, request a
pre-deployment review session via the contact form on magertron.com.
