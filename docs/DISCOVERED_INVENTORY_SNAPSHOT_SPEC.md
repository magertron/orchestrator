# Feature Spec: "Newly discovered inventory, by source" in the assistant snapshot

Answers the operator question **"Did Magertron ingest new inventory from a SIEM /
endpoint tool / CMDB that isn't in my fleet yet?"**

This is a **cross-service** feature. The staged-discovery data lives in the
**inventory service's own Postgres** (`inventory_servers` / `inventory_observations`),
which the orchestrator deliberately cannot reach directly (isolation: a flaky
inventory poll must never touch ext_authz). So the orchestrator asks the inventory
service over HTTP (`:8090`) via `InventoryAdminClient`, and the snapshot emits a line.

Stack (from `go.mod`): chi/v5 router, pgx/v5 pool, golang-jwt/v5. Module:
`github.com/curtismager20/mcp-platform-private/services/mcp-inventory`.

---

## Data model (confirmed from 01-schema.sql)

- `inventory_servers.state`: `discovered` → `adopted` → `deprecated`.
  **`discovered` = seen by a source, NOT yet in the fleet.** This is the "staged"
  set the question asks about. `adopted` = crossed into `deployed_servers`.
- `inventory_observations.source_kind` (TEXT NOT NULL) = which source produced the
  sighting (endpoint agent, SIEM, CMDB…). Multiple observations roll up to one
  `inventory_servers` row via the writer's correlation cascade.
- Recency: `inventory_servers.first_seen_at` / `last_seen_at`;
  `inventory_observations.received_at` (ingest time).

**Mapping:** "new inventory ingested, not in fleet, by source" =
`inventory_servers WHERE state='discovered'`, grouped by the `source_kind`s of its
observations, with recency.

---

## Piece 1 — Go inventory service: read endpoint

**REVISED after seeing handlers.go / types.go / db.go.** The `read` package has a
firm pattern: **SQL lives in the `db` package as `db.XxxQSQL` constants**, response
shapes live in **`types.go`**, handlers scan into those structs and emit via
`writeJSON`. `GetStats` is the exact template (one-shot aggregation). Auth is the
existing `ScopeInventoryRead` group — no new scope/middleware.

Note: `StatsSummary` already returns a scalar `DiscoveredServers` count via `/stats`,
so "how many discovered?" is answerable today. This endpoint adds the **by-source
breakdown** (the SIEM/endpoint/CMDB question) that `/stats` doesn't have.

### 1a. SQL constant (add to the `db` package, beside `StatsSummaryQSQL`)

> **File note:** `db.go` holds only the pool + `Migrate`. The `db.XxxQSQL` query
> constants (`StatsSummaryQSQL`, `ListServersQSQL`, …) live in a *separate file* in
> the same package — likely `db/queries.go` (grep `StatsSummaryQSQL` to find it).
> Add the new constant there, NOT in `db.go`.

```go
// DiscoveredCensusQSQL — per source_kind, count of DISTINCT servers still in
// state='discovered' (seen by a source, not yet adopted) + most recent sighting.
// COUNT(DISTINCT s.id) so observation volume doesn't inflate the server count.
const DiscoveredCensusQSQL = `
	SELECT o.source_kind,
	       COUNT(DISTINCT s.id) AS discovered_servers,
	       MAX(s.last_seen_at)  AS most_recent
	FROM inventory_servers s
	JOIN inventory_observations o ON o.server_id = s.id
	WHERE s.state = 'discovered'
	GROUP BY o.source_kind
	ORDER BY discovered_servers DESC`
```

### 1b. Response types (add to `read/types.go`)

```go
// DiscoveredSource is one source_kind's contribution to the discovered
// (not-yet-adopted) set. Backs GET /inventory/v1/discovered.
type DiscoveredSource struct {
	SourceKind        string    `json:"source_kind"`
	DiscoveredServers int       `json:"discovered_servers"`
	MostRecent        time.Time `json:"most_recent"`
}

type DiscoveredResponse struct {
	Sources []DiscoveredSource `json:"sources"`
}
```

### 1c. Handler (add to `read/handlers.go`, mirrors ListAgents' multi-row shape)

```go
// ── GET /inventory/v1/discovered ────────────────────────────────────────────
//
// Per-source breakdown of servers still in state='discovered' — i.e. seen by a
// discovery source (endpoint agent / SIEM / CMDB) but not yet adopted into the
// fleet. The by-source complement to /stats' scalar discovered_servers count.
// No pagination: one row per source_kind, always small.
func (h *Handlers) DiscoveredCensus(w http.ResponseWriter, r *http.Request) {
	rows, err := h.pool.Query(r.Context(), db.DiscoveredCensusQSQL)
	if err != nil {
		slog.Error("read.DiscoveredCensus: query failed", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	items := make([]DiscoveredSource, 0)
	for rows.Next() {
		var d DiscoveredSource
		if err := rows.Scan(&d.SourceKind, &d.DiscoveredServers, &d.MostRecent); err != nil {
			slog.Error("read.DiscoveredCensus: scan failed", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		items = append(items, d)
	}
	if err := rows.Err(); err != nil {
		slog.Error("read.DiscoveredCensus: row iteration failed", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	writeJSON(w, http.StatusOK, DiscoveredResponse{Sources: items})
}
```

### 1d. Route registration (server.go, in the `/_admin` group)

**Scope decision (confirmed from server.go + inventory_admin_client.hpp):** the three
inventory scopes (`Write` / `Read` / `Admin`) are independent `RequireScope` groups
with no visible hierarchy, and **every** `InventoryAdminClient` call uses the
orchestrator's bootstrap token carrying `ScopeInventoryAdmin` against `/_admin/*`.
An admin token is NOT guaranteed to satisfy `RequireScope(ScopeInventoryRead)`, so
registering `/discovered` in the read group risks a 403 from the orchestrator's own
token. Register it in the **`/_admin` group** instead — where every existing client
call already succeeds — and use the `/_admin/discovered` path. It's admin/operator-
facing anyway (it feeds the admin assistant snapshot).

```go
r.With(authMW.RequireScope(auth.ScopeInventoryAdmin)).
	With(revCheck).
	Route("/_admin", func(r chi.Router) {
		// ... existing admin routes ...
		r.Get("/organizations", adminHandlers.ListOrganizations)
		r.Get("/discovered", readHandlers.DiscoveredCensus)   // NEW (read handler, admin scope)
		// ... existing tool routes ...
	})
```

> The handler lives in the `read` package (it's a read query) but is *mounted* under
> `/_admin` so the orchestrator's admin token reaches it. `readHandlers` is already
> constructed in `NewRouter` (`read.NewHandlers(pool)`), so it's in scope there — no
> new wiring. If you'd rather keep read handlers out of the admin block for tidiness,
> alternatively confirm the orchestrator token also carries `inventory:read` and keep
> it in the read group; but `/_admin` is the guaranteed-working choice.

---

## Piece 2 — Orchestrator: InventoryAdminClient method (C++)

**CONFIRMED contract:** every `InventoryAdminClient` method returns
`mcp::HttpResult { bool ok; int status; std::string body; }`. `list_organizations()`
is the exact template — a parameterless GET returning `HttpResult` with raw JSON in
`.body`. The client's existing token already reaches the read/admin surface, so no
new auth is needed.

Add (see `inventory_admin_client_discovered.patch.md` for the drop-in):

```cpp
// Declaration (inventory_admin_client.hpp), beside list_organizations():
mcp::HttpResult discovered_census();

// Implementation (inventory_admin_client.cpp) — copy list_organizations()'s body,
// change only the path to "/inventory/v1/_admin/discovered":
mcp::HttpResult InventoryAdminClient::discovered_census() {
    return get_("/inventory/v1/_admin/discovered");   // use the real GET helper name
}
```
```

> Match the existing client's HTTP + JSON-parse conventions (same lib it uses for
> other inventory calls). Fail **soft** — the snapshot must render even if the
> inventory service is down (isolation principle).

---

## Piece 3 — Snapshot line (rest_server.cpp) — DONE (in the updated rest_server.cpp)

Emitted after the inventory attention/drift block, before the named REST/LLM line.
Parses `HttpResult.body` with nlohmann (already included), fail-soft on
unconfigured / upstream-error / parse-error (snapshot always renders):

```cpp
if (impl_->inventory_admin.is_configured()) {
    auto disc = impl_->inventory_admin.discovered_census();
    if (disc.ok) {
        try {
            auto j = nlohmann::json::parse(disc.body);
            const auto& sources = j.at("sources");
            long total = 0;
            std::vector<std::string> parts;
            std::string newest;
            for (const auto& s : sources) {
                long n = s.value("discovered_servers", 0L);
                total += n;
                parts.push_back(std::to_string(n) + " via " +
                                s.value("source_kind", std::string("unknown")));
                std::string mr = s.value("most_recent", std::string(""));
                if (mr > newest) newest = mr;   // ISO8601 sorts lexically
            }
            if (total == 0) {
                out += "- Newly discovered (not yet adopted): none\n";
            } else {
                out += "- Newly discovered (not yet adopted): " + std::to_string(total)
                     + " server(s) — " + snap_join(parts)
                     + (newest.empty() ? "" : "; newest " + newest) + "\n";
            }
        } catch (const std::exception& e) {
            spdlog::warn("assistant snapshot: discovered_census parse failed: {}", e.what());
        }
    } else {
        spdlog::warn("assistant snapshot: discovered_census upstream status={}", disc.status);
    }
}
```

Example rendered line:
```
- Newly discovered (not yet adopted): 12 server(s) — 7 via endpoint-agent, 4 via splunk, 1 via servicenow; newest 2026-07-21T09:40:00Z
```

---

## Piece 4 — Prompt guidance (BOTH prompt files)

Add to the Live-platform-snapshot section, near the inventory bullet:

> - **Newly discovered inventory** — the snapshot may include a "Newly discovered
>   (not yet adopted)" line: servers that external sources (endpoint agents, a SIEM,
>   a CMDB) have reported but that are NOT yet adopted into the fleet, grouped by
>   source. Answers "did we ingest anything new?", "anything discovered from my
>   SIEM?", "what's staged but not adopted?". Report the count and per-source
>   breakdown from the line; "none" means nothing is awaiting adoption. This is
>   distinct from fleet inventory (deployed servers) and from inventory needing
>   attention (deployed but not Active) — discovered = seen by a source, not yet in
>   the fleet at all. Adoption is an operator action; the assistant reports what's
>   discovered, it does not adopt.

---

## Build / ship checklist

- [ ] Go: add `DiscoveredCensusQSQL` const to the `db` package's query file
      (where `StatsSummaryQSQL` lives — likely `db/queries.go`, NOT `db.go`).
- [ ] Go: add `DiscoveredSource` + `DiscoveredResponse` to `read/types.go`.
- [ ] Go: add `DiscoveredCensus` handler to `read/handlers.go` (mirrors `ListAgents`).
- [ ] Go: register `r.Get("/discovered", readHandlers.DiscoveredCensus)` in the EXISTING
      `ScopeInventoryRead` group in server.go (one line — no new scope/mw).
- [ ] C++: add `discovered_census()` to `inventory_admin_client.hpp/.cpp` — copy
      `list_organizations()`, change path to `/inventory/v1/discovered` (see
      inventory_admin_client_discovered.patch.md).
- [x] C++: snapshot line in `assistant_platform_snapshot_block` — DONE in updated rest_server.cpp.
- [ ] Prompts: guidance in both .md files.
- [ ] Version: inventory service +1 (chart appVersion note says 2.2.4; external
      /inventory/* route is 2.2.5 — this endpoint is in-cluster only, fine for now).
      Orchestrator +1 patch.
- [ ] Regression, then rebuild — push ALL images under the SAME sha the manifests
      reference (the .62 sha-skew trap: verify the tag lands on Docker Hub before deploy).
- [ ] Verify: port-forward the inventory service, curl /inventory/v1/discovered with a
      read-scoped token; then ask the assistant "did we ingest any new inventory?" and
      confirm it matches.

---

## Notes / decisions banked

- **Discovered vs. attention vs. fleet** are three different questions on two
  different databases:
  - *Fleet* = `deployed_servers` census (type breakdown). [shipped]
  - *Needs attention* = `deployed_servers` non-Active (pending/awaiting-approval/
    drift). [built, pending patch]
  - *Newly discovered* = inventory DB `inventory_servers.state='discovered'` by
    source_kind. [this spec]
- **Auth decision (revised):** the service already has `ScopeInventoryRead` and a
  `read` handler group. The endpoint goes there — no new scope, no admin-JWT clone.
  The orchestrator already reaches the service with a suitable token. Agents keep
  write-only; nothing about the agent path changes.
- **Isolation preserved:** orchestrator never connects to the inventory DB; it
  calls the service. Snapshot fails soft if the service is unavailable.
- **source_kind values** are free-text in the schema (no CHECK). The line renders
  whatever the sources actually send — confirm the real values (e.g. is it
  "splunk" vs "siem-splunk"?) when you verify, and normalize display if needed.
