// Magertron Czartup(TM). The Czarter being edited is one JSON object (DOC); each stage
// edits part of it. The server validates against the schema and saves YAML.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clone = o => JSON.parse(JSON.stringify(o));

let DOC = null;          // the Czarter being edited
let STAGE = 0;
let ERRORS = [];         // [{path, message}] from the schema
let DIRTY = false;
let LAST_DRY = null;     // {action} when a dry run is waiting for "make the change"
const OPEN = new Set();  // list items shown open, e.g. "checks.2"
const GFOLD = new Set(); // sections folded, e.g. "identity:1"
const CSTEP = new Map();  // Review & run: step title -> open (true) / folded (false), when the person chose
// Values Create app fills in (0.2.75): the app's Client ID and the agent's External ID, for an IdP
// Czartup creates apps in. They are not the person's to fix, so they count amber ("later"), not red.
const LATER_PATHS = { "identity/app/clientId": "Client ID", "identity/sts/agent/externalId": "External ID" };
function laterErr(e) {
  if (!AUTOMATED[getP(DOC, "identity.idp.provider") || "auth0"]) return false;
  if (LATER_PATHS[e.path]) return true;
  const m = /^'(\w+)' is a required property$/.exec(e.message || "");
  return !!m && !!LATER_PATHS[`${e.path}/${m[1]}`];
}
const LATER_TIP = "Filled in by Create app on step 8, Review & run - nothing to do here yet.";
function errsUnder(pre) { return ERRORS.filter(e => e.path === pre || e.path.startsWith(pre + "/")); }
// A badge: red with the count of things to fix (hover lists them), amber when only Create app's values are waiting.
function badgeHtml(real, later, attrs = "") {
  if (real.length) return `<span class="badge" ${attrs} title="${esc(real.map(e => (e.path || "(top)") + ": " + e.message).join("\n"))}">${real.length}</span>`;
  if (later.length) return `<span class="badge later" ${attrs} title="${esc(LATER_TIP)}">${later.length}</span>`;
  return `<span class="badge ok" ${attrs}>✓</span>`;
}
function setBadge(b, real, later) { const t = document.createElement("span"); t.innerHTML = badgeHtml(real, later); const n = t.firstChild;
  b.className = n.className; b.textContent = n.textContent; b.title = n.title || ""; }
function groupErrs(g) {
  if (g.list) return errsUnder(g.list.replace(/\./g, "/"));
  return ERRORS.filter(e => g.fields.some(f => { const k = f.p.replace(/\./g, "/");
    const parent = k.includes("/") ? k.slice(0, k.lastIndexOf("/")) : "(top)", leaf = k.split("/").at(-1);
    return e.path === k || e.path.startsWith(k + "/") || (e.path === parent && e.message === `'${leaf}' is a required property`); }));
}
function groupErrors(g) { return groupErrs(g).filter(e => !laterErr(e)).length; }
function itemErrors(k) { return errsUnder(k.replace(/\./g, "/")).filter(e => !laterErr(e)).length; }
const MAG = { state: "idle", servers: [], tools: {}, error: "", api: "" };   // the live Magertron(TM)

async function loadMagertron(force = false) {
  if (MAG.state === "loading" || (MAG.state === "ok" && !force)) return;
  MAG.state = "loading"; render();
  try {
    const r = await api("/api/magertron/servers");
    if (r.error) throw new Error(r.error);
    Object.assign(MAG, { state: "ok", servers: r.servers, tools: r.tools, api: r.api, error: "" });
  } catch (e) { Object.assign(MAG, { state: "error", error: e.message }); }
  render();
}

// Picking a server fills in its namespace and kind, then shows its tools.
function pickServer(v, key) {
  const base = key.replace(/\.name$/, "");
  if (!v.includes("/")) return;                       // typed by hand
  const [ns, name] = [v.slice(0, v.indexOf("/")), v.slice(v.indexOf("/") + 1)];
  const own = getP(DOC, "metadata.namespace");
  setP(DOC, base + ".name", name);
  setP(DOC, base + ".namespace", ns === own ? "" : ns);
  const row = MAG.servers.find(x => x.name === name && x.namespace === ns);
  if (row) setP(DOC, base + ".kind", row.kind);
  touch(); render();
}

// Controls pick from what the Czarter already connects to.
const connNames = d => (d.connections || []).filter(x => x.name)
  .map(x => [x.name, `${x.name}${x.kind ? "  ·  " + x.kind.toUpperCase() : ""}`]);
function connTools(d, key) {
  const srv = getP(d, key.replace(/\.tool$/, ".server"));
  const c = (d.connections || []).find(x => x.name === srv);
  let t = c && c.tools && c.tools.length ? c.tools : [];
  if (!t.length) { const m = MAG.servers.find(x => x.name === srv); if (m) t = MAG.tools[`${m.namespace}/${m.name}`] || []; }
  return t.filter(x => x !== "*");
}
function llmServers(d) {
  const seen = new Map();
  (d.connections || []).filter(x => x.name && x.kind === "llm").forEach(x => seen.set(x.name, `${x.name}  ·  in this Czarter`));
  MAG.servers.filter(x => x.kind === "llm").forEach(x => { if (!seen.has(x.name)) seen.set(x.name, `${x.name}  ·  ${x.namespace}${x.model ? "  ·  " + x.model : ""}`); });
  return [...seen].map(([v, l]) => [v, l]);
}

// ---------- checks written from the controls they prove ----------
function checkTargets(d) {
  const c = d.controls || {};
  return [["", "— choose —"], ["signin", "A person can sign in (with MFA)"], ["exchange", "The agent's token exchanges at the STS"],
    ...(c.limits || []).filter(l => l.id).map(l => [`limit:${l.id}`, `Limit: ${l.description || l.id}`]),
    ...(c.holds || []).filter(h => h.id).map(h => [`hold:${h.id}`, `Hold: ${h.reason || h.id}`])];
}

// An argument value that breaks a limit's rule.
function breaking(op, v) {
  const n = typeof v === "number";
  switch (op) {
    case "<=": return n ? Math.ceil(v * 1.5) || v + 1 : v;
    case "<": return v;
    case ">=": return n ? Math.floor(v / 2) : v;
    case ">": return v;
    case "==": return n ? v + 1 : `not-${v}`;
    case "in": return "something-else";
    case "not-in": return Array.isArray(v) ? v[0] : v;
  }
  return v;
}

function fillCheck(v, key) {
  const base = key.replace(/\.tests$/, ""), c = DOC.controls || {}, ag = getP(DOC, "identity.sts.agent.serviceAccount") || "the agent";
  const set = (k, x) => setP(DOC, `${base}.${k}`, x);
  const [what, id] = v.includes(":") ? v.split(/:(.*)/s) : [v, ""];
  if (what === "signin") {
    set("id", "person-signin"); set("kind", "signin"); set("call", "");
    set("description", "A person signs in" + (getP(DOC, "identity.people.mfa.required") ? " with password and MFA" : ""));
    set("expect", "Signed in" + (getP(DOC, "identity.people.mfa.required") ? "; MFA asked" : ""));
  } else if (what === "exchange") {
    set("id", "agent-exchange"); set("kind", "exchange"); set("call", "");
    set("description", "The agent's token is exchanged at the STS"); set("expect", `Acts as ${ag}`);
  } else if (what === "limit") {
    const l = (c.limits || []).find(x => x.id === id); if (!l) return;
    const args = l.field ? { [l.field]: breaking(l.op, l.value) } : {};
    set("id", `${l.id}-refused`); set("kind", "deny");
    set("description", `A call that breaks the limit: ${l.description || l.id}`);
    set("call", { server: l.server, tool: l.tool, arguments: args });
    set("expect", `Refused: ${l.description || l.id}`);
  } else if (what === "hold") {
    const h = (c.holds || []).find(x => x.id === id); if (!h) return;
    const w = h.when || {}, args = { ...(w.argEquals || {}) };
    if (w.argPresent) args[w.argPresent] = "test";
    for (const [k, v] of Object.entries(w.argAbove || {})) args[k] = v + 1;
    set("id", `${h.id}-held`); set("kind", "hold");
    set("description", `A call the hold applies to: ${h.reason || h.id}`);
    set("call", { server: h.server, tool: h.tool, arguments: args });
    set("expect", "Held for a person's approval (403 with x-magertron-hold-id)");
  }
  touch(); render();
}

function toolsFor(key) {
  const it = getP(DOC, key.replace(/\.tools$/, "")) || {};
  const ns = it.namespace || getP(DOC, "metadata.namespace");
  const t = MAG.tools[`${ns}/${it.name}`] || [];
  if (t.length) return t;
  // An LLM server's one "tool" is its model (0.2.85): offer it instead of an empty box.
  const srv = MAG.servers.find(x => x.name === it.name && x.namespace === ns && x.kind === "llm");
  return srv && srv.model ? [srv.model] : [];
}

// An LLM connection lists its model, and the agent uses that model (0.2.85). Drafts left the
// connection's Tools empty and wrote a model name from memory (claude-3-5-haiku) on the agent,
// so Verify failed on a model nobody picked. Repaired when the page shows the Czarter.
function fixLlmTools() {
  if (!DOC || !MAG.servers.length) return false;
  let changed = false;
  const ns0 = getP(DOC, "metadata.namespace");
  for (const x of DOC.connections || []) {
    if (!x || x.kind !== "llm") continue;
    const srv = MAG.servers.find(y => y.name === x.name && y.namespace === (x.namespace || ns0) && y.kind === "llm");
    if (!srv || !srv.model) continue;
    if (!(x.tools || []).length) { x.tools = [srv.model]; changed = true; }
    for (const ag of ((DOC.functionality || {}).agents || [])) {
      if (!ag || !Array.isArray(ag.tools)) continue;
      const t2 = ag.tools.map(t => { const [s_, tool] = String(t).split("/");
        return s_ === x.name && tool && !x.tools.includes(tool) ? `${x.name}/${x.tools[0]}` : t; });
      const t3 = [...new Set(t2)];
      if (JSON.stringify(t3) !== JSON.stringify(ag.tools)) { ag.tools = t3; changed = true; }
    }
  }
  return changed;
}

// ---------- the questions ----------
// Options are "value" or ["value", "label"]; a function gets the Czarter and returns them.
const provider = d => getP(d, "identity.idp.provider") || "auth0";
// The IdPs Czartup creates apps in, and this install's own IdP (a trial tenant's Keycloak) when it has one.
const AUTOMATED = { auth0: "Auth0", keycloak: "Keycloak" };
let IDP_HERE = null;
let ME = { user: "", email: "" };     // who is signed in (from /api/status)
let SAVED = [];                       // the saved Czarters' names (home())
function useHere(i) {
  if (!IDP_HERE || !IDP_HERE.provider) return;
  i.idp = { ...i.idp, provider: IDP_HERE.provider, domain: IDP_HERE.domain, audience: IDP_HERE.audience || i.idp?.audience };
  if (IDP_HERE.provider !== "auth0" && i.people) delete i.people.connections;   // Auth0 connection names mean nothing elsewhere
}
const CONNECTIONS = {
  auth0: [["Username-Password-Authentication", "Auth0 username & password"], ["google-oauth2", "Google"], ["github", "GitHub"],
          ["windowslive", "Microsoft account"], ["apple", "Apple"], ["email", "Passwordless email"], ["sms", "Passwordless SMS"]],
  okta: [["okta", "Okta directory"], ["google", "Google"], ["microsoft", "Microsoft"], ["saml", "Enterprise SAML IdP"]],
  entra: [["entra-id", "Entra ID work accounts"], ["msa", "Personal Microsoft accounts"], ["b2b-guests", "B2B guest users"]],
  keycloak: [["keycloak-users", "Keycloak users"], ["ldap", "LDAP / Active Directory"], ["google", "Google"]],
  other: [],
};
const MFA_HOW = {
  auth0: [["post-login Action, this app only", "Post-login Action for this app only (recommended)"],
          ["MFA policy: Always (tenant-wide)", "MFA policy: Always (every app on the tenant)"],
          ["Adaptive MFA (risk-based)", "Adaptive MFA (risk-based)"]],
  okta: [["app authentication policy", "App authentication policy"], ["global session policy", "Global session policy (every app)"]],
  entra: [["Conditional Access policy for this app", "Conditional Access policy for this app"],
          ["security defaults (tenant-wide)", "Security defaults (every app)"]],
  keycloak: [["client authentication flow with OTP", "Client authentication flow with OTP"],
             ["realm required action: OTP", "Realm required action: OTP (every client)"]],
  other: [],
};
const OPS = ["<=", "<", ">=", ">", "==", "in", "not-in"];
const STAGES = [
  { key: "metadata", title: "Project", help: "Which customer, which namespace, who owns it and who may edit it.",
    groups: [
      { title: "The app", fields: [
        { p: "metadata.name", l: "App name", t: "text", hint: "lowercase, digits and dashes, e.g. daytrader", onchange: syncNamespace },
        { p: "metadata.tenant", l: "Customer (tenant)", t: "text" },
        { p: "metadata.namespace", l: "Magertron™ namespace", t: "text", hint: "defaults to the app name" },
        { p: "metadata.template", l: "Template", t: "text", hint: "the catalog template it started from" },
        { p: "metadata.owner", l: "Owner", t: "text", hint: "the customer person who owns this Czarter - their email also adds them to Identity", onchange: syncOwnerPerson },
        { p: "metadata.description", l: "What it is", t: "textarea", wide: true } ] },
      { title: "Who may edit it", list: "metadata.authors", item: "Author", fields: [
        { p: "who", l: "Who", t: "text" }, { p: "org", l: "Organization", t: "text" },
        { p: "role", l: "Role", t: "select", opts: ["owner", "si-delegate", "reviewer"] } ] } ] },

  { key: "identity", title: "Identity", help: "The IdP signs in the person and the app; the Magertron™ STS joins the two.",
    note: "Make the decisions now. The values marked <b>after you create the app in your IdP</b> come from the IdP - leave them showing Required until then.",
    groups: [
      { title: "Identity provider", fields: [
        { p: "identity.idp.provider", l: "Provider", t: "select", onchange: () => render(),
          opts: [["auth0", "Auth0"], ["okta", "Okta"], ["entra", "Microsoft Entra ID"], ["keycloak", "Keycloak"], ["other", "Other"]] },
        { p: "identity.idp.domain", l: "Tenant domain (Keycloak: the realm's issuer URL)", t: "text",
          hint: "Auth0: dev-xxxx.us.auth0.com · Keycloak: https://host/idp/realms/magertron" },
        { p: "identity.idp.audience", l: "Token audience (Auth0: API Identifier)", t: "text",
          hint: "a name, not a URL you call: tokens must say they are for the Magertron™ STS" } ] },
      { title: "People signing in", fields: [
        { p: "identity.people.signIn", l: "Sign-in flow", t: "select", opts: [["authorization_code_pkce", "Sign-in page with code exchange (PKCE)"]] },
        { p: "identity.people.promptLogin", l: "Always ask for a fresh login", t: "bool",
          hint: "stops an existing IdP session (e.g. an admin's) being reused" },
        { p: "identity.people.connections", l: "Allowed sign-in connections", t: "multi", wide: true, other: true,
          opts: d => CONNECTIONS[provider(d)] || [], hint: "enabled for this app only - avoid social logins an administrator also uses" },
        { p: "identity.people.mfa.required", l: "Require MFA", t: "bool" },
        { p: "identity.people.mfa.factors", l: "MFA factors", t: "multi",
          opts: [["otp", "Authenticator app"], ["webauthn", "Passkey / security key"], ["push", "Push"], ["sms", "SMS code"]] },
        { p: "identity.people.mfa.scope", l: "MFA scope", t: "select", opts: [["this-app-only", "This app only"], ["tenant-wide", "Every app on the tenant"]],
          hint: "\"This app only\" leaves your other apps on the tenant unchanged" },
        { p: "identity.people.mfa.how", l: "How MFA is enforced", t: "select", other: true, opts: d => MFA_HOW[provider(d)] || [] } ] },
      { title: "The app's identity",
        note: "<b>Auth0:</b> leave the <b>Client ID</b> empty - <b>Create app</b> on Review &amp; run creates the app, stores its secret where you say here, and fills the Client ID in. <b>Other IdPs:</b> create the app there first, then fill in the Client ID.",
        fields: [
        { p: "identity.app.type", l: "App type", t: "select",
          opts: [["regular-web", "Web app with a server"], ["spa", "Single-page app"], ["native", "Mobile / desktop app"], ["machine", "Machine to machine"]] },
        { p: "identity.app.clientId", l: "Client ID", t: "text", hint: "not secret - Auth0: filled in by Create app", onchange: syncExternalId },
        { p: "identity.app.grantTypes", l: "Grant types", t: "multi",
          opts: [["authorization_code", "Authorization code (people sign in)"], ["refresh_token", "Refresh token (stay signed in)"],
                 ["client_credentials", "Client credentials (the app's own token)"]] },
        { p: "identity.app.secretRef", l: "Where the client secret lives", t: "text",
          hint: "vault:<path>#<key>, file:<path> or platform - never the secret itself" },
        { p: "identity.app.callbackUrls", l: "Callback URLs", t: "tags", wide: true,
          hint: "filled in with the first free local port when empty - change it to where the app will run" },
        { p: "identity.app.logoutUrls", l: "Logout URLs", t: "tags", wide: true } ] },
      { title: "STS: people", list: "identity.sts.people", item: "Person",
        linked: true,
        fields: [
        { p: "email", l: "Email", t: "text" },
        { p: "name", l: "Name", t: "text" } ] },
      { title: "STS: the agent",
        note: "The <b>External ID</b> fills itself in from the Client ID.",
        fields: [
        { p: "identity.sts.agent.serviceAccount", l: "Service account", t: "text", hint: "e.g. daytrader-agent" },
        { p: "identity.sts.agent.externalId", l: "External ID", t: "text", hint: "Auth0: <clientId>@clients (filled in for you)" },
        { p: "identity.sts.agent.roles", l: "Roles", t: "tags", wide: true,
          hint: "the app's own role first. Avoid broad roles like MCP consumer: their * grants make tool lists decorative" } ] } ] },

  { key: "connections", title: "Connections", help: "Every server the app calls. Each goes through Magertron™; list the exact tools it uses.",
    note: () => MAG.state === "ok"
      ? `Servers and tools below come from <b>${esc(MAG.api)}</b> (${MAG.servers.length} servers). Not there yet, like a server you are about to build? Choose <b>Other…</b> and type it. <button class="ghost tiny" data-mag="reload">Reload</button>`
      : MAG.state === "loading" ? "Reading the servers registered in Magertron™…"
      : MAG.state === "error" ? `Could not read the servers from Magertron™ (${esc(MAG.error)}). Type names by hand, or <button class="ghost tiny" data-mag="reload">try again</button>.`
      : "",
    onEnter: () => loadMagertron(),
    groups: [
      { title: "Servers", list: "connections", item: "Connection", fields: [
        { p: "name", l: "Server", t: "server", onchange: pickServer },
        { p: "namespace", l: "Namespace", t: "text", hint: "filled in when you pick a server; blank = this Czarter's namespace" },
        { p: "kind", l: "Kind", t: "select", opts: [["mcp", "MCP server"], ["rest", "REST API"], ["llm", "LLM"]] },
        { p: "purpose", l: "What the app uses it for", t: "text", wide: true },
        { p: "tools", l: "Tools", t: "multi", other: true, wide: true, opts: (d, key) => toolsFor(key),
          hint: "tick exactly what the app calls; * in the box = the whole server, for the app's own role only" },
        { p: "credentialRef", l: "Credential", t: "text", hint: "platform, vault:<path>#<key> or file:<path>" },
        { p: "forwardIdentity", l: "Customer's external system", t: "bool",
          hint: "Sends the verified user (x-magertron-acting-for) and agent (x-magertron-subject). Leave off for vendors." } ] } ] },

  { key: "controls", title: "Controls", help: "Limits, holds for a person's approval, PII handling and money.",
    onEnter: () => loadMagertron(),
    groups: [
      { title: "Limits", list: "controls.limits", item: "Limit", fields: [
        { p: "id", l: "Id", t: "text" }, { p: "description", l: "What it limits", t: "text" },
        { p: "server", l: "Server", t: "select", other: true, opts: connNames, onchange: () => render() },
        { p: "tool", l: "Tool", t: "select", other: true, opts: connTools },
        { p: "field", l: "Argument", t: "text", hint: "e.g. quantity" },
        { p: "op", l: "Rule", t: "select", opts: [["", "— choose —"], ["<=", "at most (<=)"], ["<", "less than (<)"], [">=", "at least (>=)"],
          [">", "more than (>)"], ["==", "exactly (==)"], ["in", "one of (in)"], ["not-in", "none of (not-in)"]] },
        { p: "value", l: "Value", t: "json", hint: "e.g. 100 or [\"BUY\",\"SELL\"]" },
        { p: "policyRef", l: "…or existing policy that enforces it", t: "text" } ] },
      { title: "Holds (a person approves first)", list: "controls.holds", item: "Hold", fields: [
        { p: "id", l: "Id", t: "text" }, { p: "reason", l: "Reason shown to the approver", t: "text" },
        { p: "server", l: "Server", t: "select", other: true, opts: connNames, onchange: () => render() },
        { p: "tool", l: "Tool", t: "select", other: true, opts: connTools },
        { p: "when", l: "Hold the call when", t: "when", wide: true },
        { p: "approvers", l: "Approvers", t: "tags", hint: "empty = anyone with Deploy on the namespace" },
        { p: "ttlSeconds", l: "Expires after (seconds)", t: "num" } ] },
      { title: "PII", fields: [
        { p: "controls.pii.action", l: "Action", t: "select", opts: [["", "— choose —"], ["allow", "Allow"], ["redact", "Redact (names become [PERSON])"], ["block", "Block the call"]] },
        { p: "controls.pii.appliesTo", l: "Applies to", t: "multi", other: true, opts: connNames } ] },
      { title: "Budgets", list: "controls.budgets", item: "Budget", fields: [
        { p: "server", l: "LLM server", t: "select", other: true, opts: llmServers }, { p: "description", l: "Description", t: "text" },
        { p: "dailyUsd", l: "Per day (USD)", t: "num" }, { p: "monthlyUsd", l: "Per month (USD)", t: "num" },
        { p: "policyRef", l: "…or existing cap", t: "text", wide: true } ] } ] },

  { key: "functionality", title: "What it does", help: "Screens and agents, in plain words. This becomes the spec a developer or coding agent builds against.",
    groups: [
      { title: "Summary", fields: [{ p: "functionality.summary", l: "Summary", t: "textarea", wide: true }] },
      { title: "Screens", list: "functionality.surfaces", item: "Screen", fields: [
        { p: "name", l: "Name", t: "text" }, { p: "does", l: "Does", t: "text" } ] },
      { title: "Agents", list: "functionality.agents", item: "Agent", fields: [
        { p: "name", l: "Name", t: "text" },
        { p: "cadenceMinutes", l: "Runs every (minutes)", t: "nums", hint: "e.g. 5, 10, 15" },
        { p: "onlyWhen", l: "Only when", t: "text", wide: true },
        { p: "may", l: "May", t: "lines", hint: "one per line" },
        { p: "mustNot", l: "Must not", t: "lines", hint: "one per line" },
        { p: "tools", l: "Tools it may call on its own", t: "lines", wide: true,
          hint: "server/tool, one per line - with nobody on the call, anything else is refused (enforced by the policy)" } ] } ] },

  { key: "checks", title: "Checks", help: "What must be proven before this Czarter is verified. Results go in the run log.",
    groups: [
      { title: "Checks", list: "checks", item: "Check", fields: [
        { p: "tests", l: "What it proves", t: "select", wide: true, opts: checkTargets, onchange: fillCheck,
          hint: "pick a limit or hold and the rest is written for you" },
        { p: "id", l: "Id", t: "text" },
        { p: "kind", l: "Kind", t: "select", opts: [["signin", "Sign-in"], ["exchange", "Token exchange"], ["allow", "Allowed call"],
          ["deny", "Refused call"], ["hold", "Held call"], ["cap", "Budget cap"]] },
        { p: "description", l: "Description", t: "text", wide: true },
        { p: "call", l: "Call", t: "json", wide: true, hint: "written for you from the control; edit only if you need a different test" },
        { p: "expect", l: "Expect", t: "text", wide: true } ] } ] },

  { key: "handover", title: "Handover", help: "What the customer gets when this is done.",
    groups: [
      { title: "Deliverables", fields: [
        { p: "handover.runbook", l: "Runbook", t: "bool" },
        { p: "handover.contract", l: "App contract (env, sign-in flow, routes, hold retry, spec)", t: "bool" },
        { p: "handover.terraform", l: "Terraform", t: "bool" } ] } ] },

  { key: "review", title: "Review & run", help: "Save, check the result, then apply one step at a time.", review: true },
];

// ---------- object paths ----------
function getP(o, p) { return p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o); }
function setP(o, p, v) {
  const ks = p.split("."); let a = o;
  ks.slice(0, -1).forEach(k => { if (a[k] == null || typeof a[k] !== "object") a[k] = {}; a = a[k]; });
  a[ks.at(-1)] = v;
}
// Drop empty values so optional fields stay out of the file.
function prune(v) {
  if (Array.isArray(v)) { const a = v.map(prune).filter(x => x !== undefined); return a.length ? a : undefined; }
  if (v && typeof v === "object") {
    const o = {}; for (const [k, x] of Object.entries(v)) { const y = prune(x); if (y !== undefined) o[k] = y; }
    return Object.keys(o).length ? o : undefined;
  }
  return (v === "" || v === null) ? undefined : v;
}
function cleanDoc() {
  const d = prune(clone(DOC)) || {};
  for (const k of ["connections", "checks"]) if (!d[k]) d[k] = [];
  if (!d.controls) d.controls = {};
  return d;
}

// ---------- fields ----------
function optsOf(f, key) {
  const raw = typeof f.opts === "function" ? f.opts(DOC, key) : (f.opts || []);
  return raw.map(o => Array.isArray(o) ? { v: o[0], l: o[1] } : { v: o, l: o });
}

function errorFor(key) {
  const slash = key.replace(/\./g, "/");
  const parent = slash.includes("/") ? slash.slice(0, slash.lastIndexOf("/")) : "(top)";
  const leaf = slash.split("/").at(-1);
  const e = ERRORS.find(e => e.path === slash || e.path.startsWith(slash + "/")
    || (e.path === parent && e.message === `'${leaf}' is a required property`));
  // Missing (a required value, an empty list that needs one) is "Required": the field is waiting
  // for input, not wrong. Anything else is a real mistake and keeps its message.
  if (e && laterErr(e)) return LATER;
  return !e ? "" : /is a required property$|should be non-empty|is too short$/.test(e.message) ? "Required" : e.message;
}
const NEED = "Required";
const LATER = "Filled in by Create app (step 8)";

// After each live check: update the messages under the fields without redrawing (keeps focus).
function refreshFieldErrors() {
  document.querySelectorAll("#form .f[data-fkey]").forEach(f => {
    const msg = errorFor(f.dataset.fkey), span = f.querySelector(".err");
    if (span) { span.textContent = msg; span.classList.toggle("need", msg === NEED); span.classList.toggle("later", msg === LATER); }
    f.classList.toggle("need", msg === NEED);
  });
}

function fieldHtml(f, val, key) {
  if (f.t === "server") {
    if (!MAG.servers.length) f = { ...f, t: "text", hint: "the server's name as Magertron™ shows it" };
    else {
      const it = getP(DOC, key.replace(/\.name$/, "")) || {};
      const ns = it.namespace || getP(DOC, "metadata.namespace");
      const known = MAG.servers.some(x => x.name === val && x.namespace === ns);
      f = { ...f, t: "select", other: true,
            opts: () => MAG.servers.map(x => [`${x.namespace}/${x.name}`, `${x.name}  ·  ${x.namespace}  ·  ${x.kind.toUpperCase()}${x.model ? "  ·  " + x.model : ""}`]) };
      if (val && known) val = `${ns}/${val}`;
    }
  }
  const id = "f_" + key.replace(/[^a-z0-9]/gi, "_");
  const msg = errorFor(key);
  const need = msg === NEED;
  const err = `<span class="err${need ? " need" : ""}${msg === LATER ? " later" : ""}">${esc(msg)}</span>`;
  const hint = f.hint ? `<span class="hint">${esc(f.hint)}</span>` : "";
  const cls = "f" + (f.wide ? " wide" : "") + (need ? " need" : "") + `" data-fkey="${esc(key)}`;
  const d = `data-key="${esc(key)}" data-t="${f.t}"`;
  let ctl;
  switch (f.t) {
    case "textarea": ctl = `<textarea id="${id}" ${d}>${esc(val)}</textarea>`; break;
    case "select": {
      const os = optsOf(f, key), cur = val ?? "", known = os.some(o => o.v === cur);
      const isOther = f.other && cur !== "" && !known;
      const list = (os.some(o => o.v === "") || known || isOther) ? os : [{ v: "", l: "— choose —" }, ...os];
      ctl = `<select id="${id}" ${d}>${list.map(o => `<option value="${esc(o.v)}"${o.v === cur && !isOther ? " selected" : ""}>${esc(o.l || "— choose —")}</option>`).join("")}`
        + (f.other ? `<option value="__other__"${isOther ? " selected" : ""}>Other…</option>` : "") + `</select>`
        + (f.other ? `<input type="text" class="othertext" data-other="${esc(key)}" placeholder="Describe it" value="${esc(isOther ? cur : "")}"${isOther ? "" : " hidden"}>` : "");
      break;
    }
    case "bool": return `<div class="${cls}"><label class="toggle"><input type="checkbox" ${d}${val ? " checked" : ""}> ${esc(f.l)}</label>${hint}${err}</div>`;
    case "multi": {
      const os = optsOf(f, key), cur = val || [], extra = cur.filter(x => !os.some(o => o.v === x));
      ctl = `<div class="checks">${os.map(o => `<label><input type="checkbox" ${d} value="${esc(o.v)}"${cur.includes(o.v) ? " checked" : ""}> ${esc(o.l)}</label>`).join("")}</div>`
        + (f.other ? `<input type="text" class="othertext" data-other="${esc(key)}" placeholder="${os.length ? "Others, comma-separated" : "Names, comma-separated"}" value="${esc(extra.join(", "))}">` : "");
      break;
    }
    case "tags": ctl = `<input type="text" id="${id}" ${d} value="${esc((val || []).join(", "))}">`; break;
    case "nums": ctl = `<input type="text" id="${id}" ${d} value="${esc((val || []).join(", "))}">`; break;
    case "lines": ctl = `<textarea id="${id}" ${d}>${esc((val || []).join("\n"))}</textarea>`; break;
    case "num": ctl = `<input type="number" id="${id}" ${d} value="${esc(val ?? "")}">`; break;
    case "when": {
      if (!whenSimple(val)) { ctl = `<textarea class="json" id="${id}" data-key="${esc(key)}" data-t="json">${esc(JSON.stringify(val))}</textarea>`; break; }
      const w = whenParts(val);
      ctl = `<div class="whenrow">
        <input type="text" id="${id}" ${d} data-part="arg" placeholder="argument, e.g. stake" value="${esc(w.arg)}">
        <select ${d} data-part="op">${[["above", "is above"], ["equals", "equals"], ["present", "is present"]]
          .map(([v, l]) => `<option value="${v}"${w.op === v ? " selected" : ""}>${l}</option>`).join("")}</select>
        <span class="wval"${w.op === "present" ? " hidden" : ""}><input type="text" ${d} data-part="val" placeholder="value, e.g. 500" value="${esc(w.val)}"></span>
      </div>`;
      break;
    }
    case "json": ctl = `<textarea class="json" id="${id}" ${d}>${esc(val === undefined || val === "" ? "" : JSON.stringify(val))}</textarea>`; break;
    default: ctl = `<input type="text" id="${id}" ${d} value="${esc(val ?? "")}">`;
  }
  return `<div class="${cls}"><label for="${id}">${esc(f.l)}</label>${ctl}${hint}${err}</div>`;
}

// A hold condition as three plain fields: argument, condition, value.
function whenParts(w) {
  w = w || {};
  if (w.argPresent) return { arg: w.argPresent, op: "present", val: "" };
  for (const [op, key] of [["equals", "argEquals"], ["above", "argAbove"]]) {
    const e = Object.entries(w[key] || {});
    if (e.length === 1) return { arg: e[0][0], op, val: e[0][1] };
  }
  return { arg: "", op: "above", val: "" };
}
function whenSimple(w) {
  w = w || {};
  const n = (w.argPresent ? 1 : 0) + Object.keys(w.argEquals || {}).length + Object.keys(w.argAbove || {}).length;
  return n <= 1;
}
function parseVal(s) {
  const t = String(s).trim();
  if (t === "true") return true; if (t === "false") return false;
  if (t !== "" && !isNaN(Number(t))) return Number(t);
  return t;
}

function readField(el) {
  const t = el.dataset.t;
  if (t === "when") {
    const k = CSS.escape(el.dataset.key), part = x => document.querySelector(`[data-key="${k}"][data-part="${x}"]`);
    const arg = part("arg").value.trim(), op = part("op").value, raw = part("val").value;
    part("val").closest(".wval").hidden = op === "present";
    if (!arg) return "";
    if (op === "present") return { argPresent: arg };
    if (raw.trim() === "") return "";
    return op === "above" ? { argAbove: { [arg]: Number(raw) } } : { argEquals: { [arg]: parseVal(raw) } };
  }
  if (t === "bool") return el.checked;
  if (t === "multi") {
    const k = CSS.escape(el.dataset.key);
    const picked = [...document.querySelectorAll(`[data-key="${k}"]:checked`)].map(x => x.value);
    const o = document.querySelector(`[data-other="${k}"]`);
    const extra = o ? o.value.split(",").map(x => x.trim()).filter(Boolean) : [];
    return [...picked, ...extra.filter(x => !picked.includes(x))];
  }
  if (t === "select" && el.value === "__other__") {
    const o = document.querySelector(`[data-other="${CSS.escape(el.dataset.key)}"]`);
    if (o) { o.hidden = false; return o.value; }
  }
  if (t === "select") {
    const o = document.querySelector(`[data-other="${CSS.escape(el.dataset.key)}"]`);
    if (o) o.hidden = true;
  }
  if (t === "tags") return el.value.split(",").map(s => s.trim()).filter(Boolean);
  if (t === "nums") return el.value.split(",").map(s => s.trim()).filter(Boolean).map(Number).filter(n => !isNaN(n));
  if (t === "lines") return el.value.split("\n").map(s => s.trim()).filter(Boolean);
  if (t === "num") return el.value === "" ? "" : Number(el.value);
  if (t === "json") {
    if (el.value.trim() === "") return "";
    try { el.classList.remove("bad"); return JSON.parse(el.value); }
    catch { el.classList.add("bad"); return undefined; }
  }
  return el.value;
}

function fieldDef(key) {
  for (const s of STAGES) for (const g of s.groups || []) for (const f of g.fields) {
    if (!g.list && f.p === key) return f;
    if (g.list && key.startsWith(g.list + ".") && key.endsWith("." + f.p)) return f;
  }
}

// The namespace follows the app name while it is empty or still the previous name,
// so a new or cloned Czarter gets its own namespace unless someone chose one.
let LAST_NAME = null;
function syncNamespace(v) {
  const ns = getP(DOC, "metadata.namespace") || "";
  if (!ns || ns === LAST_NAME) {
    setP(DOC, "metadata.namespace", v);
    const el = document.querySelector('[data-key="metadata.namespace"]');
    if (el) el.value = v;
  }
  LAST_NAME = v;
}

function onInput(e) {
  if (e.target.dataset && e.target.dataset.other) {
    const k = CSS.escape(e.target.dataset.other);
    const owner = document.querySelector(`select[data-key="${k}"]`) || document.querySelector(`[data-key="${k}"]`);
    if (owner) { onInput({ target: owner }); return; }
    // A pick-list with nothing to pick from yet (e.g. a server still to be built): the box is the whole value.
    setP(DOC, e.target.dataset.other, e.target.value.split(",").map(x => x.trim()).filter(Boolean));
    touch();
    return;
  }
  const el = e.target.closest("[data-key]"); if (!el) return;
  const v = readField(el); if (v === undefined) return;   // unparseable JSON: keep the old value
  setP(DOC, el.dataset.key, v);
  const f = fieldDef(el.dataset.key);
  if (f && f.onchange) f.onchange(v, el.dataset.key);
  touch();
}

// The owner is almost always one of the people who sign in (0.2.72). When Owner holds an email
// and Identity's "STS: people" is empty, add them; while that entry is still the one added here
// (nothing else typed into it), it follows Owner. Once edited by hand it is left alone.
let OWNER_PERSON = "";
function syncOwnerPerson(owner) {
  const email = String(owner || "").trim();
  const ok = /^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(email);
  const people = (getP(DOC, "identity.sts.people") || []).filter(x => x && String(x.email || x.name || "").trim());
  if (people.length !== (getP(DOC, "identity.sts.people") || []).length) setP(DOC, "identity.sts.people", people);
  const auto = people.length === 1 && OWNER_PERSON && people[0].email === OWNER_PERSON
    && Object.keys(people[0]).every(k => k === "email" || (k === "name" && !people[0].name));
  if (people.length === 0 && ok) { setP(DOC, "identity.sts.people", [{ email }]); OWNER_PERSON = email; }
  else if (auto) {
    if (ok) { people[0].email = email; OWNER_PERSON = email; }
    else { setP(DOC, "identity.sts.people", []); OWNER_PERSON = ""; }
  }
}

// The app's callback (0.2.74): when Identity is opened and Callback URLs is empty - any Czarter,
// old or new - ask the server for the first local port no Czarter uses and fill in the callback
// and, if empty, the logout URL. Nobody has to know which ports are taken.
let CB_FILLING = false;
async function fillCallback() {
  if (CB_FILLING || (getP(DOC, "identity.app.callbackUrls") || []).length) return;
  CB_FILLING = true;
  try {
    const { port } = await api("/api/free-port");
    if (!port || (getP(DOC, "identity.app.callbackUrls") || []).length) return;
    setP(DOC, "identity.app.callbackUrls", [`http://127.0.0.1:${port}/auth/callback`]);
    if (!(getP(DOC, "identity.app.logoutUrls") || []).length) setP(DOC, "identity.app.logoutUrls", [`http://127.0.0.1:${port}/`]);
    touch(); render();
  } catch { /* leave it for the person to type */ } finally { CB_FILLING = false; }
}

function syncExternalId(clientId) {
  const a = (((DOC.identity || {}).sts || {}).agent) || {};
  const ext = a.externalId || "";
  if (getP(DOC, "identity.idp.provider") === "auth0" && clientId && (!ext || ext.endsWith("@clients"))) {
    setP(DOC, "identity.sts.agent.externalId", clientId + "@clients");
    const el = document.querySelector('[data-key="identity.sts.agent.externalId"]');
    if (el) el.value = clientId + "@clients";
  }
}

// ---------- rendering ----------
// Always-visible summary of the Czarter, so the top-level facts are on screen on every stage.
function renderCtxBar() {
  const bar = $("#ctxbar");
  if (!DOC) { bar.hidden = true; return; }
  const g = p => getP(DOC, p) || "";
  const screens = (g("functionality.surfaces") || []).length, agents = (g("functionality.agents") || []).length;
  const builds = screens && agents ? "App + agent" : agents ? "Agent" : screens ? "App" : "";
  const role = (g("identity.sts.agent.roles") || [])[0] || "";
  const item = (label, val, mono) => `<span class="ci"><span class="cl">${label}</span>` +
    (val ? `<span class="cv${mono ? " mono" : ""}" title="${esc(String(val))}">${esc(String(val))}</span>` : `<span class="cv none">not set</span>`) + `</span>`;
  bar.innerHTML =
    `<span class="ci lead"><span class="cv big">${esc(g("metadata.name") || "Untitled Czarter")}</span>` +
      (builds ? `<span class="kindpill">${builds}</span>` : "") + `</span>` +
    item("Customer", g("metadata.tenant")) +
    item("Namespace", g("metadata.namespace"), true) +
    item("IdP app", g("identity.app.clientId"), true) +
    item("Agent", g("identity.sts.agent.serviceAccount"), true) +
    item("Role", role) +
    `<span class="ci dirty">${DIRTY ? "● unsaved" : ""}</span>`;
  bar.hidden = $("#wizard").hidden;
}

function render() {
  renderCtxBar();
  const s = STAGES[STAGE];
  $("#stageTitle").textContent = `${STAGE + 1}. ${s.title}`;
  $("#stageHelp").textContent = s.help;
  $("#prevBtn").disabled = STAGE === 0;
  $("#nextBtn").hidden = STAGE === STAGES.length - 1;
  renderSteps();
  if (s.review) return renderReview();
  if (s.onEnter && MAG.state === "idle") setTimeout(s.onEnter);
  if (s.key === "identity" && DOC && !(getP(DOC, "identity.app.callbackUrls") || []).length) setTimeout(fillCallback);
  if (fixLlmTools()) setTimeout(() => { touch(); render(); });
  // Owner set before 0.2.72 (or in a draft) never fired onchange, so people stayed empty (0.2.77):
  // fill it from Owner on any render while the list is empty.
  if (DOC && !(getP(DOC, "identity.sts.people") || []).some(x => x && String(x.email || "").trim()) && getP(DOC, "metadata.owner")) {
    const before = JSON.stringify(getP(DOC, "identity.sts.people") || []);
    syncOwnerPerson(getP(DOC, "metadata.owner"));
    if (JSON.stringify(getP(DOC, "identity.sts.people") || []) !== before) setTimeout(() => { touch(); render(); });
  }
  const note = typeof s.note === "function" ? s.note() : s.note;
  const many = s.groups.length > 1;
  const stageBtns = many ? `<div class="stagebtns"><button class="ghost tiny" data-gopen="${STAGE}">Expand all sections</button>
    <button class="ghost tiny" data-gclose="${STAGE}">Collapse all sections</button></div>` : "";
  const drafted = NOTES.length ? `<div class="note drafted"><b>Drafted from your description.</b><ul>${NOTES.map(n => `<li>${esc(n)}</li>`).join("")}</ul></div>` : "";
  $("#form").innerHTML = drafted + (note ? `<div class="note">${note}</div>` : "") + stageBtns + s.groups.map((g, gi) => {
    const gk = `${s.key}:${gi}`, folded = GFOLD.has(gk), n = groupErrors(g), ge = groupErrs(g);
    const head = (extra = "") => `<h3 class="ghead" data-gtoggle="${esc(gk)}"><span class="ititle"><span class="chev">${folded ? "▸" : "▾"}</span>${esc(g.title)}</span>
      <span class="iright">${extra}${badgeHtml(ge.filter(e => !laterErr(e)), ge.filter(laterErr), `data-gbadge="${gi}"`)}</span></h3>`;
    if (folded) return `<div class="group folded">${head()}</div>`;
    const gnote = g.note ? `<div class="note gnote">${g.note}</div>` : "";
    if (!g.list) return `<div class="group">${head()}${gnote}<div class="fields">${g.fields.map(f => fieldHtml(f, getP(DOC, f.p), f.p)).join("")}</div></div>`;
    const arr = getP(DOC, g.list) || [];
    const items = arr.map((it, i) => {
      const k = `${g.list}.${i}`, n = itemErrors(k), open = OPEN.has(k);
      const sum = it.description || it.purpose || it.reason || it.does || it.who || (g.linked ? [it.email, real(it.user)].filter(Boolean).join(" → ") : "") || "";
      return `<div class="item${open ? " open" : ""}">
        <div class="itemhead" data-toggle="${esc(k)}">
          <span class="ititle"><span class="chev">${open ? "▾" : "▸"}</span><b>${esc(g.item)} ${i + 1}${it.name || it.id ? " · " + esc(it.name || it.id) : ""}</b>
            ${!open && sum ? `<span class="isum">${esc(sum)}</span>` : ""}</span>
          <span class="iright">${n ? `<span class="badge">${n}</span>` : '<span class="badge ok">✓</span>'}
            <button class="ghost" data-remove="${esc(g.list)}" data-i="${i}">Remove</button></span></div>
        ${open ? itemBody(g, it, k) : ""}</div>`;
    }).join("");
    const all = arr.length > 1 ? `<span class="grpbtns"><button class="ghost tiny" data-openall="${esc(g.list)}">Expand all</button>
      <button class="ghost tiny" data-closeall="${esc(g.list)}">Collapse all</button></span>` : "";
    const waiting = !arr.length && n > 0;     // an empty list that needs one: waiting for input, like a field
    return `<div class="group">${head(all)}${gnote}<div class="items">${items || `<p class="muted${waiting ? " needbox" : ""}">${waiting ? "None yet - add at least one." : "None yet."}</p>`}</div>
      <button class="ghost add${waiting ? " need" : ""}" data-add="${esc(g.list)}">+ Add ${esc(g.item.toLowerCase())}</button></div>`;
  }).join("");
}

// A list item's fields (0.2.79). Fields marked adv fold under "More" - closed unless one holds a
// value or has an error - so a person is just Email and Name, and the account overrides only show
// when needed. A people list says how each person is linked, in words, instead of two empty boxes.
// Which Magertron™ account a person is (0.2.80): looked up from the email, picked with a click.
// One match is taken as is; several are offered; an administrator is never offered.
const MATCH = {};
function accountLine(it, k) {
  const email = String(it.email || "").trim().toLowerCase(), u = real(it.user);
  if (u) return `<div class="acct">Magertron™ account <b>${esc(u)}</b> <button class="ghost tiny" data-unpick="${esc(k)}">Change</button></div>`;
  if (!/^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i.test(email)) return "";
  const m = MATCH[email];
  if (m === undefined) {
    MATCH[email] = null;
    api(`/api/people/match?email=${encodeURIComponent(email)}`).then(r => { MATCH[email] = r; render(); }).catch(() => { MATCH[email] = { users: [] }; });
  }
  if (!m) return `<div class="acct muted">Looking up ${esc(email)}…</div>`;
  const ok = (m.users || []).filter(x => !x.admin);
  if (ok.length === 1) {
    setTimeout(() => { if (!real(getP(DOC, k + ".user"))) { setP(DOC, k + ".user", ok[0].username); touch(); render(); } });
    return `<div class="acct">Magertron™ account <b>${esc(ok[0].username)}</b></div>`;
  }
  if (!ok.length) return `<div class="acct">No Magertron™ account yet - one is created at setup.</div>`;
  return `<div class="acct">Which Magertron™ account? ${ok.map(x =>
    `<button class="ghost pick" data-pick="${esc(k)}" data-user="${esc(x.username)}">${esc(x.username)}${x.signsIn ? "" : " <span class=\"muted\">(never signed in)</span>"}</button>`).join(" ")}</div>`;
}

const real = v => (v && !String(v).startsWith("<link") ? String(v) : "");   // "<link by email>" = not linked yet
function itemBody(g, it, k) {
  const main = g.fields.filter(f => !f.adv), adv = g.fields.filter(f => f.adv);
  let out = `<div class="fields">${main.map(f => fieldHtml(f, it[f.p], `${k}.${f.p}`)).join("")}</div>`;
  if (g.linked) out += accountLine(it, k);
  if (adv.length) {
    const ak = `${k}:adv`, has = adv.some(f => real(it[f.p])) || adv.some(f => itemErrors(`${k}.${f.p}`));
    const open = OPEN.has(ak) || (has && !OPEN.has(ak + ":shut"));
    out += `<div class="advhead" data-advtoggle="${esc(ak)}" data-open="${open ? 1 : 0}"><span class="chev">${open ? "▾" : "▸"}</span>${g.linked ? "More - name the accounts yourself" : "More"}</div>`;
    if (open) out += `<div class="fields">${adv.map(f => fieldHtml(f, it[f.p], `${k}.${f.p}`)).join("")}</div>`;
  }
  return out;
}

function stageOfError(e) {
  const top = e.path.split("/")[0];
  const i = STAGES.findIndex(s => s.key === top);
  if (i >= 0) return i;
  const m = /'(\w+)' is a required property/.exec(e.message);
  const j = m ? STAGES.findIndex(s => s.key === m[1]) : -1;
  return j >= 0 ? j : STAGES.length - 1;
}

function renderSteps() {
  const real = STAGES.map(() => []), later = STAGES.map(() => []);
  ERRORS.forEach(e => (laterErr(e) ? later : real)[stageOfError(e)].push(e));
  $("#steps").innerHTML = STAGES.map((s, i) => `<button class="step${i === STAGE ? " on" : ""}" data-stage="${i}">
    <span><span class="n">${i + 1}</span>${esc(s.title)}</span>
    ${s.review ? "" : badgeHtml(real[i], later[i])}</button>`).join("");
}

// Review & run: one numbered checklist, top to bottom, each step with its status.
let STATUS = null;
async function loadStatus() {
  const name = getP(DOC, "metadata.name");
  try { STATUS = name ? await api(`/api/czarters/${encodeURIComponent(name)}/status`) : null; } catch { STATUS = null; }
  if (STAGES[STAGE].review) renderChecklist();
}

const pill = (kind, text) => `<span class="pill ${kind}">${esc(text)}</span>`;
const when = t => t ? new Date(t).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
// A server the SI has not built yet (0.2.82): Register servers recorded "no submission for X".
// A failed step whose every failure is about such a server is waiting, not broken.
let UNBUILT = [];
function waitPill(rows) {
  const bad = rows.filter(r => r && r.status === "failed");
  if (!UNBUILT.length || !bad.length || !bad.every(r => UNBUILT.some(n => `${r.step || ""} ${r.detail || ""}`.includes(n)))) return null;
  return pill("wait", `⏳ waiting for ${UNBUILT.join(", ")} to be built (step 7)`);
}
function runPill(rows) {
  rows = rows.filter(Boolean);
  const w = waitPill(rows); if (w) return w;
  if (!rows.length) return pill("todo", "not done yet");
  if (rows.some(r => r.status === "failed")) {
    const at = when(rows.map(r => r.done_at).sort().at(-1)), good = rows.filter(r => r.status !== "failed").length;
    // Some worked, some did not (e.g. grants on a server not deployed yet): say so, not just "failed".
    return good ? pill("warn", `${good} of ${rows.length} done · ${at}`) : pill("bad", `failed · ${at}`);
  }
  const last = rows.map(r => r.done_at).sort().at(-1);
  return pill("ok", `${rows.every(r => r.status === "passed") ? "passed" : "done"} · ${when(last)}`);
}

// A step counts as finished when it was applied ("done") or a later Verify confirmed it ("passed").
const OKS = { done: 1, passed: 1 };
function renderChecklist() {
  const st = STATUS || { runs: {} }, runs = st.runs || {};
  UNBUILT = Object.entries(runs).filter(([k, r]) => k.startsWith("register.") && r && r.status === "failed"
    && /no submission for/.test(r.detail || "")).map(([k]) => k.slice("register.".length));
  for (const [k, r] of Object.entries(runs)) if (r && !r.step) r.step = k;
  const scope = (DOC.connections || []).filter(x => x.tools && x.tools.length).map(x => runs[`controls.scope.${x.name}`]);
  const verifyRows = Object.values(runs).filter(r => r.done_by === "czarter-verify");
  const lastVerify = verifyRows.map(r => r.done_at).sort().at(-1);
  // Hand-over freshness. The last change = the Czarter's last save or the latest non-hand-over
  // run, whichever is newer. The pack is fresh when built after it; the zip and the Gitea push
  // when made after the pack (both are built from it).
  const tms = t => (t ? new Date(t).getTime() : 0) || 0;
  const lastChange = Math.max((st.czarter || 0) * 1000,
    ...Object.entries(runs).filter(([k]) => !k.startsWith("handover.")).map(([, r]) => tms(r?.done_at)));
  const packAt = (st.pack || 0) * 1000;
  const packFresh = !!st.pack && packAt + 1000 >= lastChange;
  const zipFresh = packFresh && runs["handover.zip"]?.status in OKS && tms(runs["handover.zip"].done_at) + 1000 >= packAt;
  const pushFresh = packFresh && runs["handover.push"]?.status in OKS && tms(runs["handover.push"].done_at) + 1000 >= packAt;
  const steps = [
    { n: 1, title: "Check the Czarter", text: "Every stage on the left needs a ✓.",
      status: ERRORS.length ? pill("bad", `${ERRORS.length} to fix`) : pill("ok", "complete"), done: !ERRORS.length },
    ...(AUTOMATED[getP(DOC, "identity.idp.provider")] ? [{ n: 0, title: `Create the app in ${AUTOMATED[getP(DOC, "identity.idp.provider")]}`,
      text: getP(DOC, "identity.idp.provider") === "keycloak"
        ? "Czartup creates the client in your Keycloak realm, stores its secret in Vault, fills in the Client ID and the agent's External ID, and puts the STS audience in its tokens. Then a sign-in flow with a one-time code for this app only. Each shows a dry run first."
        : "Czartup creates the application, stores its secret in Vault, fills in the Client ID, gives it access to the STS API and enables its sign-in connection. Then the MFA Action for this app only. Each shows a dry run first.",
      status: runPill([runs["identity.app"], runs["identity.mfa"]].filter(Boolean).length ? [runs["identity.app"], runs["identity.mfa"]] : []),
      btns: `<button data-act="apply-idp-app">Create app</button>` +
        (getP(DOC, "identity.people.mfa.required") === false ? "" :
          // MFA attaches to the app's client id, so it only makes sense once the app exists.
          (runs["identity.app"]?.status in OKS
            ? `<button data-act="apply-idp-mfa">Add MFA</button>`
            : `<button data-act="apply-idp-mfa" disabled title="Create the app first - MFA is attached to its client id">Add MFA</button>`)),
      done: runs["identity.app"]?.status in OKS &&
            (getP(DOC, "identity.people.mfa.required") === false || runs["identity.mfa"]?.status in OKS) }] : []),
    { n: 2, title: "Plan", text: "Writes the manual steps. Do any steps left for your IdP (and anything marked by hand) before going on.",
      status: st.plan ? pill("ok", `generated · ${when(st.plan * 1000)}`) : pill("todo", "not generated"),
      btns: `<button data-act="plan">Plan</button>`, done: !!st.plan },
    ...(st.token && st.token.hosted ? [{ n: 3, title: "Your session",
      text: "Czartup acts as you: every step below uses your own Magertron™ sign-in and is audited to you. No operator token.",
      status: st.token.valid ? pill("ok", `${st.token.user || "signed in"} · ${st.token.minutesLeft} min left`)
                             : pill("todo", "expired - sign in to the console again"),
      btns: "", live: true, done: true, aside: true }] : [{ n: 3, title: "Operator token", text: "A 15-minute token for the steps below. Minted for you when it is missing or about to run out; this button forces a new one.",
      status: st.token && st.token.valid ? pill("ok", `valid · ${st.token.minutesLeft} min left`) : pill("todo", st.token ? "expired" : "none yet"),
      // Not a step in the sequence: the token is minted automatically by every step below,
      // so this button is only a manual override and never lights up blue.
      btns: st.token && st.token.valid
        ? `<button class="ghost mint" data-act="mint" title="Token is fresh - click to mint another">Minted ✓</button>`
        : `<button class="ghost" data-act="mint">Mint new token</button>`, live: true, done: true, aside: true }]),
    { n: 4, title: "Agent service account", text: "Creates the app's own role (no permissions - its tools come from step 6) if it is missing, then the agent's identity in Magertron™ with its external ID and roles.",
      status: runPill([runs["sts.agent"]]), btns: `<button data-act="apply-agent">Apply</button>`, live: true,
      done: runs["sts.agent"]?.status in OKS },
    // After the SI builds and submits: not in the sequence (never locks what follows),
    // because the hand-over below must be reachable before anything is built.
    { n: 4.6, title: "Register servers", text: "After the SI submits: registers each server the app needs from its Inventory submission - external, in this namespace, Trusted upstream as the Czarter says - and approves it. Fixes the Trusted upstream setting on one already registered.",
      status: runPill(Object.keys(runs).filter(k => k.startsWith("register.")).map(k => runs[k])),
      btns: `<button data-act="apply-register">Apply</button>`, live: true, done: true, aside: true },
    { n: 4.7, title: "Approve tools", text: "Approves the new tool definitions of the tools this Czarter lists, on its own servers. Extra tools stay pending, and a definition that CHANGED after approval is never approved here - a person looks at it.",
      status: runPill([runs["tools.approve"]]), btns: `<button data-act="apply-tools">Apply</button>`, live: true, done: true, aside: true },
    { n: 5, title: "Tool grants", text: "Grants the app's own role exactly the tools you listed. Grants on a server that does not exist yet fail - run this again once it does.",
      status: runPill(scope), btns: `<button data-act="apply-grants">Apply</button><button class="ghost" data-act="grants">List grants</button>`, live: true,
      // Concluded once it has run: grants on a server that does not exist yet fail by design.
      done: scope.some(Boolean) },
    { n: 5.5, title: "People's access", text: "Gives each person the Czarter lists the app's own role - the one the tool grants go to. On a call made for a person, Magertron™ checks the person's roles too, so without it their calls are refused.",
      status: runPill([runs["people.roles"]]), btns: `<button data-act="apply-people">Apply</button>`, live: true,
      done: runs["people.roles"]?.status in OKS },
    ...(st.k8sPolicies ? [{ n: 5.7, title: "Install the app's policy", text: "Writes the app's own policy (limits, holds, what the agent may do alone) as its ConfigMap; OPA loads it within seconds. Czartup may write only app policies - the cluster refuses anything else.",
      status: runPill([runs["controls.policy.install"]]), btns: `<button data-act="apply-policy">Apply</button>`, live: true,
      done: runs["controls.policy.install"]?.status in OKS }] : []),
    { n: 5.8, title: "App icon", text: "A square PNG, 1024 x 1024 best. Register servers puts it on each of the app's own servers (never over an icon someone chose in the console); the hand-over pack carries it.",
      status: st.icon ? pill("ok", "set") : pill("todo", "none"),
      btns: `<label class="ghost filebtn">Choose PNG<input type="file" accept="image/png" data-icon hidden></label>`,
      live: true, done: true, aside: true },
    { n: 6, title: "Verify", text: "Read-only. Checks policy, people, agent, servers and that no broad grant undermines the tool lists."
        // What failed, on the card (0.2.84) - not just a count. A check about an unbuilt server is marked waiting.
        + (() => { const bad = verifyRows.filter(r => r.done_at === lastVerify && r.status === "failed");
            return bad.length ? `<ul class="vfail">${bad.map(r => { const w = UNBUILT.some(n => `${r.step} ${r.detail}`.includes(n));
              return `<li class="${w ? "wait" : "bad"}">${w ? "⏳" : "❌"} ${esc(r.detail || r.step)}</li>`; }).join("")}</ul>` : ""; })(),
      status: lastVerify ? runPill(verifyRows) : pill("todo", "not run"), btns: `<button data-act="verify">Verify</button>`, live: true,
      done: !!lastVerify },
    { n: 7, title: "Runbook", text: "The customer's runbook, every step with its status. Run it last, and again after any change.",
      status: st.runbook ? pill(st.runbook.verified ? "ok" : "todo", `${st.runbook.verified ? "verified" : "not yet verified"} · ${when(st.runbook.at * 1000)}`) : pill("todo", "not generated"),
      btns: `<button data-act="runbook">Runbook</button>`, done: !!st.runbook },
    { n: 8, title: "Hand over",
      // ⚠ The pack, zip and Gitea push are snapshots. Green only while nothing changed after
      // them (a save of the Czarter, or any step run) - otherwise blue: hand over again.
      text: `Builds the handover pack in standard formats - AGENTS.md, a GitHub Spec Kit spec, plan and tasks, a SERVER-SPEC for each server not deployed yet, the runbook and the policy draft. Then either download it as a versioned zip for an SI who works in their own repository, or push it to a private repo (GitHub: a branch and a pull request) with one issue per open step. Hand over again after any change: the zip version goes up, and in the repo done steps close their issues.`,
      status: (() => {
        const z = runs["handover.zip"], g = runs["handover.push"], bits = [];
        if (zipFresh) bits.push(`zip ${(z.detail || "").split(" ")[0]} · ${when(z.done_at)}`);
        if (pushFresh) bits.push(`in the repo · ${when(g.done_at)}`);
        if (bits.length) return pill("ok", bits.join(" · "));
        if (packFresh) return pill("todo", `pack built · ${when(st.pack * 1000)}`);
        if (st.pack || z?.status in OKS || g?.status in OKS) return pill("todo", "out of date - changed since the last hand-over");
        return pill("todo", "not handed over");
      })(),
      btns: `<button data-act="handover-pack">Build pack</button>` +
        // Zip or Gitea - either one hands it over, so once one is done the other is an option, not the next step.
        (st.pack ? `<span class="kebabwrap"><button class="ghost" data-menu="openin" title="Copy the pack to its own folder and open it in the SI's tool">Open in ▾</button>
          <span class="menu" hidden>
            <button class="menuitem" data-act="handover-open-cursor">Cursor</button>
            <button class="menuitem" data-act="handover-open-code">VS Code</button>
            <button class="menuitem" data-act="handover-open-claude">Claude Code</button>
            <button class="menuitem" data-act="handover-open-devin">Devin <span class="muted">(steps)</span></button>
          </span></span>` : "") +
        (st.pack ? `<button data-act="handover-zip"${pushFresh && !zipFresh ? ' class="ghost"' : ""}>Download zip</button>` +
                   `<button data-act="apply-handover"${zipFresh && !pushFresh ? ' class="ghost"' : ""}>Push to Repo</button>` +
                   `<button class="ghost" data-act="repo-check" title="Check Czartup can reach the repo host with its token">Test connection</button>`
                 : `<button data-act="handover-zip" disabled title="Build the pack first">Download zip</button>` +
                   `<button data-act="apply-handover" disabled title="Build the pack first">Push to Repo</button>`),
      section: "Handover", done: zipFresh || pushFresh },
  ];
  steps.forEach((x, i) => { x.n = i + 1; });
  // One step at a time: a step's main buttons stay grey until the step before it is done.
  // Read-only helpers (ghost buttons such as List grants) stay available.
  // Three looks per button: grey = locked (finish the step before), blue = do this next,
  // green with a check = already done (click again to re-run).
  const did = {
    "apply-idp-app": runs["identity.app"]?.status in OKS,
    "apply-idp-mfa": runs["identity.mfa"]?.status in OKS,
    "plan": !!st.plan,
    "apply-agent": runs["sts.agent"]?.status in OKS,
    // Green only when nothing failed: "8 of 10" or "2 of 3 granted" is not finished - it stays blue.
    "apply-grants": scope.some(Boolean) && scope.every(r => !r || r.status !== "failed"),
    "apply-people": runs["people.roles"]?.status in OKS,
    "apply-policy": runs["controls.policy.install"]?.status in OKS,
    "apply-register": Object.keys(runs).some(k => k.startsWith("register.")) && Object.keys(runs).filter(k => k.startsWith("register.")).every(k => runs[k]?.status in OKS),
    "apply-tools": runs["tools.approve"]?.status in OKS,
    "verify": !!lastVerify && verifyRows.every(r => r.status !== "failed"),
    "runbook": !!st.runbook,
    "handover-pack": packFresh,
    "apply-handover": pushFresh,
    "handover-zip": zipFresh,
  };
  steps.forEach(x => {
    if (!x.btns) return;
    x.btns = x.btns.replace(/<button (?![^>]*class="ghost)([^>]*)data-act="([^"]+)"([^>]*)>([^<]*)<\/button>/g,
      (m, a, act, b, label) => did[act] && !/\bdisabled\b/.test(a + b)
        ? `<button class="did" ${a}data-act="${act}"${b} title="Done - click to run it again">${label} ✓</button>` : m);
  });
  steps.forEach((x, i) => {
    if (i > 0) x.done = x.done && steps[i - 1].done;   // a step only counts once everything before it does
    if (i === 0 || steps[i - 1].done || !x.btns) return;
    const wait = `Finish step ${steps[i - 1].n} (${steps[i - 1].title}) first`;
    x.btns = x.btns.replace(/<button (?![^>]*class="ghost")(?![^>]*\bdisabled\b)/g, `<button disabled title="${esc(wait)}" `);
  });
  const errs = ERRORS.length ? `<ul class="errlist">${ERRORS.map(e => `<li><code>${esc(e.path)}</code> ${esc(e.message)}</li>`).join("")}</ul>` : "";
  // Each step folds to its title. By default only the step to do next is open (and step 1
  // while it has errors); a step the person opened or folded stays that way.
  const next = steps.findIndex(x => !x.done);
  const isOpen = (x, i) => CSTEP.has(x.title) ? CSTEP.get(x.title) : (i === next || (i === 0 && ERRORS.length > 0));
  $("#checklist").innerHTML = steps.map((x, i) => `
    ${x.section ? `<div class="divider">${esc(x.section)}</div>` : x.live && !steps[i - 1].live ? `<div class="divider">Against Magertron™ - ${esc(MAG.api || "your Magertron™")}</div>` : ""}
    <div class="cstep${isOpen(x, i) ? "" : " folded"}">
      <div class="num">${x.n}</div>
      <div class="cbody"><div class="ctitle" data-cstep="${esc(x.title)}" data-open="${isOpen(x, i) ? 1 : 0}" title="Show or hide the details"><span class="cchev">${isOpen(x, i) ? "▾" : "▸"}</span>${esc(x.title)}</div><div class="muted ctext">${x.text}</div>${i === 0 ? errs : ""}</div>
      <div class="cside"><div class="cbtns">${x.btns || ""}</div><div class="cstatus">${x.status || ""}</div></div>
    </div>`).join("");
}

function renderReview() {
  const name = getP(DOC, "metadata.name") || "";
  $("#form").innerHTML = `
    <div class="group setupall"><div><b>Set up everything</b><div class="muted">Every step below, in order, with one combined
      dry run first. Steps already done are skipped where running them again would change something; it stops at the
      first failure that blocks the rest.</div></div><button id="setupAll">Set up everything</button></div>
    <div id="setupBox"></div>
    <div class="group checklist" id="checklist"></div>
    <div id="confirmBox"></div>
    <div class="group"><h3>Output</h3><pre id="out" class="out muted">Nothing run yet${name ? " for " + esc(name) : ""}.</pre><div id="md" class="docs"></div></div>`;
  renderChecklist();
  loadStatus();
  if (MAG.state === "idle") loadMagertron();
}

// ---------- Set up everything ----------
// The same /api/run actions the step buttons call, one after another - each keeps its own
// dry run, run-log row and output. HARD steps stop the run when they fail; SOFT steps
// (a server the SI has not submitted yet, grants on it, Verify) report and carry on, so
// the pack can still be built before anything exists.
// The sequence and its rules live on the server (POST /api/setup) - one copy for the page,
// the MCP tools and any IDE panel.
// First n lines of a step's output, escaped for the page.
const firstLines = (t, n) => esc(String(t || "").split("\n").slice(0, n).join("\n"));
let SETUP = null;   // the previewed sequence, until it runs or is dismissed

// A small "working" banner with the spinner, for actions with no box of their own on the page
// (Retire from the menu). working(text, name) shows it and marks that Czarter's row; working() hides it.
function working(text, name) {
  let el = document.getElementById("working");
  if (!el) { el = document.createElement("div"); el.id = "working"; el.setAttribute("role", "status"); document.body.append(el); }
  document.querySelectorAll(".row.busy").forEach(r => r.classList.remove("busy"));
  if (!text) { el.hidden = true; return; }
  el.innerHTML = '<span class="spin" aria-hidden="true"></span>' + esc(text);
  el.hidden = false;
  const btn = name && document.querySelector(`[data-menu="${CSS.escape(name)}"]`);
  if (btn) btn.closest(".row")?.classList.add("busy");
}
const SPIN = '<span class="spin" aria-hidden="true"></span>';
// The button shows it is working from the click on: the dry run and the run both take a while,
// and save() redraws this step first, so the button is looked up again each time.
function setupBusy(label) {
  const b = $("#setupAll");
  if (!b) return;
  b.disabled = !!label;
  b.innerHTML = label ? SPIN + esc(label) : "Set up everything";
}

async function setupPreview() {
  // ⚠ Look the box up every time: save() and loadStatus() redraw this step, and a box
  // grabbed earlier is no longer on screen - the result would go nowhere.
  const box = { set innerHTML(h) { const el = $("#setupBox"); if (el) el.innerHTML = h; } };
  const name = getP(DOC, "metadata.name");
  if (ERRORS.length) { box.innerHTML = `<div class="confirm"><span>Fix the ${ERRORS.length} item(s) in step 1 first.</span></div>`; return; }
  const working = `<div class="group"><b>${SPIN}Dry run of every step…</b><pre class="out" id="setupLog">This takes a moment.</pre></div>`;
  setupBusy("Checking every step…"); box.innerHTML = working;
  await save();
  setupBusy("Checking every step…"); box.innerHTML = working;      // save() redrew the step
  let r;
  try { r = await api("/api/setup", { name, mode: "preview" }); }
  catch (e) { setupBusy(""); box.innerHTML = `<div class="confirm"><span>Preview failed: ${esc(e.message)}</span></div>`; return; }
  setupBusy("");
  if (r.errors && r.errors.length) { box.innerHTML = `<div class="confirm"><span>${esc(r.output || "Fix the errors in step 1 first.")}</span></div>`; return; }
  const steps = r.steps || [];
  SETUP = steps;
  const blockedHard = steps.find(s => s.blocked && s.hard);
  box.innerHTML = `<div class="group"><h3>Set up everything - what will happen</h3>
    ${steps.map(s => `<details class="item"${s.blocked ? " open" : ""}><summary class="itemhead"><span class="ititle"><b>${
        s.blocked ? "⚠️" : s.nothing ? "✓" : s.depends ? "↪" : "→"} ${esc(s.label)}</b><span class="isum">${
        s.blocked ? (s.hard ? "blocked - stops here" : "will report and carry on")
        : s.nothing ? "nothing to do" : s.depends ? "depends on the steps before it - checked when it runs"
        : s.dry ? "changes below" : esc(s.preview)}</span></span></summary>
      <pre class="out">${firstLines(s.preview, 40)}</pre></details>`).join("")}
    <div class="confirm"><span>${blockedHard ? `"${esc(blockedHard.label)}" is blocked - fix that first.` : "Make all of these changes now? Each step is recorded as usual."}</span>
      <span class="actions"><button class="ghost" id="setupCancel">Not now</button>${blockedHard ? "" : '<button class="danger" id="setupGo">Make all these changes</button>'}</span></div></div>`;
}

async function setupRun() {
  // ⚠ Look the box up every time: save() and loadStatus() redraw this step, and a box
  // grabbed earlier is no longer on screen - the result would go nowhere.
  const box = { set innerHTML(h) { const el = $("#setupBox"); if (el) el.innerHTML = h; } };
  const name = getP(DOC, "metadata.name");
  SETUP = null;
  // Set up runs the SAVED Czarter (0.2.89): save first, so a fix on screen (the page's own repairs
  // included) is what gets set up - not the copy from before it.
  fixLlmTools();
  if (DIRTY) { try { await save(); } catch { /* the run reports what is wrong */ } }
  box.innerHTML = `<div class="group"><h3 id="setupHead">${SPIN}Setting up ${esc(name)}…</h3><pre class="out" id="setupLog">Starting…</pre></div>`;
  setupBusy("Setting up…");
  const show = j => {
    const log = $("#setupLog"), head = $("#setupHead");
    if (log && (j.lines || []).length) log.textContent = (j.lines || []).join("\n");
    if (head && j.done) head.textContent = `Set up ${name}: ${!j.setUp ? "stopped - see below"
      : (j.waiting || []).length ? `ready - waiting for ${j.waiting.join(", ")} to be built (step 7)` : "finished"}`;
  };
  let j;
  try { j = await api("/api/setup", { name, mode: "run" }); }
  catch (e) { j = { done: true, lines: ["❌ " + e.message] }; }
  show(j);
  // It runs on the server: poll its progress (the same run an IDE agent would see).
  while (!j.done) {
    await new Promise(res => setTimeout(res, 1500));
    try { j = await api(`/api/setup/${encodeURIComponent(name)}`); } catch (e) { j = { done: true, lines: [...(j.lines || []), "❌ " + e.message] }; }
    show(j);
  }
  setupBusy("");
  // Create app may have filled in the client ID: reload the Czarter.
  try { const d = await api(`/api/czarters/${encodeURIComponent(name)}`); DOC = d.doc; ERRORS = d.errors; DIRTY = false; } catch { /* keep */ }
  await loadStatus(); renderCtxBar();
  if (j.setUp) setupDoneDialog(name, j.waiting || [], j.check || [], j.build || null);
}

// Set up worked (0.2.86): say so, and give the next step as buttons. With servers still to build,
// the next step is the IDE - not a red Verify the person has to interpret.
// Push to IDE (0.2.93): Czartup makes a one-time link to the build kit and opens the IDE with it.
// The Magertron extension (Cursor, VS Code) catches cursor:// / vscode:// links, downloads the kit,
// writes it into a folder and opens it. Claude Code and Devin get the kit as a download for now.
// Open an app link (cursor://, vscode://) from inside the console's frame (0.2.94). A browser
// will not hand a custom-scheme link to the OS from an embedded frame, so it goes from the top
// window - same origin as the console - and only falls back to the frame's own link if that is refused.
function openAppLink(link) {
  if (window.top !== window) {
    try { window.top.location.href = link; return; } catch { /* a different origin: fall through */ }
    try { if (window.open(link, "_top")) return; } catch { /* sandboxed */ }
  }
  const a = document.createElement("a"); a.href = link; a.target = "_top"; a.rel = "noopener";
  document.body.appendChild(a); a.click(); a.remove();
}

async function pushToIde(d, name, ide) {
  const act_ = d.querySelector(".dact"), say = h => { let n = d.querySelector(".dpushed");
    if (!n) { n = document.createElement("div"); n.className = "dpushed"; act_.after(n); } n.innerHTML = h; };
  if (ide !== "cursor" && ide !== "code") { setTimeout(() => d.remove()); act(`handover-open-${ide}`); return; }
  let r;
  try { r = await api("/api/builds/push", { name, ide }); }
  catch (e) { say(`❌ ${esc(e.message)}`); return; }
  const kit = new URL(`api/kit/${r.ticket}`, location.href).href;
  const link = `${ide === "cursor" ? "cursor" : "vscode"}://magertron.magertron-submit/build?czarter=${encodeURIComponent(name)}&kit=${encodeURIComponent(kit)}`;
  openAppLink(link);
  const app = ide === "cursor" ? "Cursor" : "VS Code";
  say(`Opening ${app}… it asks where to put the build kit, then opens it.
    <span class="muted">Nothing opened? <a href="${esc(link)}" target="_top">Open ${app}</a> - it needs the Magertron™ extension 0.2 or later.</span>`);
}

function setupDoneDialog(name, waiting, check = [], build = null) {
  check = check.filter(c => !waiting.some(n => `${c.label} ${c.summary}`.includes(n)));
  document.querySelector(".donedlg")?.remove();
  const d = document.createElement("div");
  d.className = "donedlg";
  const srv = n => { const x = (DOC.connections || []).find(c => c.name === n) || {}, t = x.tools || [];
    return `<div class="dsrv"><span class="dsrv-ic">▤</span><div class="dsrv-main"><div class="dsrv-name">${esc(n)}</div>
      ${x.purpose || x.description ? `<div class="dsrv-desc">${esc(x.purpose || x.description)}</div>` : ""}</div>
      ${t.length ? `<span class="dsrv-n" title="${esc(t.join(", "))}">${t.length} tool${t.length > 1 ? "s" : ""}</span>` : ""}</div>`; };
  d.innerHTML = `<div class="donebox" role="dialog" aria-modal="true">
    <div class="donehead"><span class="dok">✓</span>${esc(name)}'s governance is set up</div>
    <div class="dbody">
    ${waiting.length
      ? `<div class="dsub">${waiting.length > 1 ? `${waiting.length} MCP servers still need` : "One MCP server still needs"} to be built.</div>
         ${waiting.map(srv).join("")}`
      : `<div class="dsub">${check.length ? `Set up, with ${check.length} thing${check.length > 1 ? "s" : ""} to fix.`
                                          : "Everything is in place and verified."}</div>`}
    ${check.length ? `<div class="donecheck"><b>Also look at:</b><ul>${check.map(c =>
        `<li>${esc(c.label)} - ${esc(c.summary)}</li>`).join("")}</ul></div>` : ""}
    <div class="dact">
      <a href="#" class="dlater" data-donedlg>${waiting.length ? "Later" : "Close"}</a>
      ${waiting.length ? `<select id="doneIde"><option value="cursor">Cursor</option><option value="code">VS Code</option>
        <option value="claude">Claude Code</option><option value="devin">Devin</option></select>
        <button id="donePushGo">Push to IDE</button>` : ""}
    </div></div></div>`;
  document.body.appendChild(d);
  d.addEventListener("click", async e => {
    if (e.target.id === "donePushGo") { pushToIde(d, name, d.querySelector("#doneIde").value); return; }
    if (e.target.classList.contains("donewho")) { e.preventDefault(); d.querySelector(".doneassign").hidden = false; e.target.hidden = true; return; }
    if (e.target.id === "doneReassign") {
      const who = (d.querySelector("#doneWho").value || "").trim(); if (!who) return;
      try { await api("/api/builds/assign", { id: e.target.dataset.bid, assignee: who });
        d.querySelector(".donesent b").textContent = who; e.target.textContent = "Sent ✓"; } catch { e.target.textContent = "Failed"; }
      return;
    }
    if (e.target.closest("[data-donedlg]")) e.preventDefault();
    if (e.target === d || e.target.closest("[data-donedlg]") || e.target.closest("[data-act]")) setTimeout(() => d.remove());
  });
}

// ---------- markdown (runbook view) ----------
function md(src) {
  const inline = s => esc(s).replace(/!\[([^\]]*)\]\((?:\.\.\/)?(docs\/[^)\s]+)\)/g, '<img class="shot" alt="$1" src="$2">')
    .replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/&lt;sub&gt;(.*?)&lt;\/sub&gt;/g, "<sub>$1</sub>");
  const out = []; let list = null, table = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } if (table) { out.push("</table>"); table = null; } };
  for (const raw of src.split("\n")) {
    const line = raw.replace(/\s+$/, "");
    let m;
    if ((m = /^(#{1,3}) (.*)/.exec(line))) { close(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
    else if ((m = /^(\d+)\. (.*)/.exec(line))) { if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; } out.push(`<li>${inline(m[2])}`); }
    else if ((m = /^- (.*)/.exec(line))) { if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
    else if (/^\|/.test(line)) {
      if (/^\|[-| ]+\|$/.test(line)) continue;
      if (!table) { close(); out.push("<table>"); table = true; }
      out.push("<tr>" + line.slice(1, -1).split("|").map(c => `<td>${inline(c.trim())}</td>`).join("") + "</tr>");
    }
    else if (/^\s{2,}\S/.test(line) && list) out.push(`<br>${inline(line.trim())}`);
    else if (line === "") { if (list !== "ol") close(); }
    else { close(); out.push(`<p>${inline(line)}</p>`); }
  }
  close();
  return out.join("\n");
}

// ---------- server calls ----------
// Hosted at <magertron>/czartup/, Czartup runs as the person signed in to the
// Magertron(TM) console: the console keeps its token in sessionStorage
// ("mcp.auth.token") and this page is on the same site, so it reads it and sends
// it with every call. Locally (run.sh) there is no token and none is needed.
// Paths are RELATIVE ("api/..."), so the page works at / and under /czartup/.
function consoleToken() {
  try { return sessionStorage.getItem("mcp.auth.token"); } catch { return null; }
}
function authHeaders(extra = {}) {
  const t = consoleToken();
  return t ? { ...extra, Authorization: "Bearer " + t } : extra;
}
function rel(path) { return String(path).replace(/^\/+/, ""); }
function signInBanner(msg) {
  let b = document.getElementById("signin");
  if (!b) {
    b = document.createElement("div"); b.id = "signin"; b.className = "signin";
    document.body.prepend(b);
  }
  b.innerHTML = `${esc(msg)} <a href="../">Open the Magertron™ console</a>, sign in, then open Czartup from there.`;
}
async function api(path, body) {
  const opts = body ? { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body) }
                    : { headers: authHeaders() };
  const r = await fetch(rel(path), opts);
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { signInBanner(j.error || "Sign in to Magertron™ first."); throw new Error(j.error || "Sign in to Magertron™ first."); }
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
// A download needs the same header, so it is fetched and handed to the browser as a file.
async function download(path) {
  const r = await fetch(rel(path), { headers: authHeaders() });
  if (!r.ok) throw new Error(`download failed: HTTP ${r.status}`);
  const blob = await r.blob();
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = rel(path).split("/").pop();
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// One place for save status: under the Save button and in the footer.
function saveStatus(text, bad = false) {
  const n = $("#saveNote"); n.textContent = text; n.classList.toggle("bad", bad);
  $("#saveState").textContent = text;
}

let checkTimer = null;
function touch() {
  DIRTY = true; saveStatus("Unsaved changes"); renderCtxBar();
  clearTimeout(checkTimer);
  checkTimer = setTimeout(async () => {
    try {
      ERRORS = (await api("/api/check", { doc: cleanDoc() })).errors; renderSteps(); refreshFieldErrors();
      if (STAGES[STAGE] && STAGES[STAGE].key === "review" && typeof renderChecklist === "function") renderChecklist();
      const st = STAGES[STAGE];
      document.querySelectorAll("[data-gbadge]").forEach(b => {
        const ge = groupErrs(st.groups[+b.dataset.gbadge]); setBadge(b, ge.filter(e => !laterErr(e)), ge.filter(laterErr));
      });
      document.querySelectorAll("[data-toggle]").forEach(h => {
        const n = itemErrors(h.dataset.toggle), b = h.querySelector(".iright .badge");
        if (b) { b.className = n ? "badge" : "badge ok"; b.textContent = n || "✓"; }
      });
    } catch { /* offline */ }
  }, 400);
}

async function save() {
  const doc = cleanDoc();
  const r = await api("/api/save", { doc });
  ERRORS = r.errors; DIRTY = false; NOTES = [];
  renderCtxBar();
  saveStatus("✓ Saved " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }));
  renderSteps();
  return r;
}

// A step's button shows it is working from the click until the step answers, and can't be
// pressed twice. Its buttons (and the dry run's "Make the change") are looked up each time,
// because save() and loadStatus() redraw them.
const BUSY = new Map();                 // button -> its label while busy
function stepBusy(action, on) {
  const btns = [...document.querySelectorAll(`[data-act="${action}"]`), ...(LAST_DRY === action ? [$("#doIt")] : [])].filter(Boolean);
  for (const b of btns) {
    if (on && !BUSY.has(b)) { BUSY.set(b, b.innerHTML); b.disabled = true; b.innerHTML = SPIN + b.textContent; }
    if (!on && BUSY.has(b)) { b.disabled = false; b.innerHTML = BUSY.get(b); BUSY.delete(b); }
  }
  if (!on) BUSY.forEach((label, b) => { if (!b.isConnected) BUSY.delete(b); });
}

async function act(action, yes = false) {
  const out = $("#out"), mdBox = $("#md");
  const name = getP(DOC, "metadata.name");
  const running = () => { const o = $("#out"); if (o) { o.classList.remove("muted"); o.innerHTML = SPIN + (yes ? "Making the change…" : "Running…"); } };
  stepBusy(action, true); running();
  try {
    if (action !== "mint") await save();
    stepBusy(action, true); running(); mdBox.innerHTML = "";      // save() may have redrawn them
    const r = await api("/api/run", { name, action, yes });
    stepBusy(action, false);
    out.textContent = r.output || (r.ok ? "Done." : "Failed.");
    // Generated documents, each folded like a list item; the first one opens.
    const panel = (title, file, body, open) => `<details class="item doc"${open ? " open" : ""}>
        <summary class="itemhead"><span class="ititle"><span class="chev"></span><b>${esc(title)}</b>
        <span class="isum">${esc(file)}</span></span></summary><div class="docbody">${body}</div></details>`;
    if (r.markdown) {
      out.textContent = "Runbook generated.";
      mdBox.innerHTML = panel("Runbook", r.file, `<div class="md">${md(r.markdown)}</div>`, true);
    }
    if (r.files) {
      out.textContent = r.packed ? (r.output || "Pack built.") : "Plan generated. Missing values show as <to fill in>.";
      mdBox.innerHTML = r.files.map((f, i) => panel(f.title, f.file,
        f.kind === "markdown" ? `<div class="md">${md(f.content)}</div>` : `<pre class="out">${esc(f.content)}</pre>`, i === 0)).join("");
    }
    if (r.download) {
      // Hand the zip to the browser: it lands in your Downloads folder.
      try { await download(r.download); out.textContent = (r.output || "") + "\n\nSaved to your Downloads folder."; }
      catch (e) { out.textContent = (r.output || "") + "\n\n" + e.message; }
    }
    loadStatus();
    if (yes && action.startsWith("apply-idp")) {
      try { const d = await api(`/api/czarters/${encodeURIComponent(name)}`); DOC = d.doc; ERRORS = d.errors; DIRTY = false; renderSteps(); renderChecklist(); renderCtxBar(); }
      catch { /* keep the page as it is */ }
    }
    const box = $("#confirmBox");
    if (action.startsWith("apply-") && !yes && r.ok && /dry run/.test(r.output || "") && !/nothing to do|0 new grant/.test(r.output || "")) {
      LAST_DRY = action;
      const where = action === "apply-handover" ? "Push it to the repo now?"
        : action.startsWith("apply-idp") ? `Make this change in ${AUTOMATED[getP(DOC, "identity.idp.provider")] || "the IdP"} now?`
        : "Make this change in Magertron™ now?";
      box.innerHTML = `<div class="confirm"><span>That was a dry run. ${where}</span>
        <span class="actions"><button class="ghost" id="cancelDry">Not now</button><button class="danger" id="doIt">Make the change</button></span></div>`;
    } else { box.innerHTML = ""; LAST_DRY = null; }
  } catch (e) { stepBusy(action, false); out.textContent = "Error: " + e.message; }
}

// ---------- start, load, templates ----------
function blank() {
  return {
    apiVersion: "czarter.magertron.com/v1", kind: "Czarter",
    metadata: { name: "", tenant: "", namespace: "mcp-prod", owner: "" },
    identity: {
      idp: { provider: "auth0", domain: "", audience: "https://sts.magertron.com" },
      people: { signIn: "authorization_code_pkce", promptLogin: true, mfa: { required: true, factors: ["otp"], scope: "this-app-only" } },
      app: { type: "regular-web", clientId: "", grantTypes: ["authorization_code", "refresh_token", "client_credentials"], callbackUrls: [], secretRef: "" },
      sts: { people: [], agent: { serviceAccount: "", externalId: "", roles: [] } },
    },
    connections: [], controls: {}, checks: [],
    handover: { runbook: true, contract: true, terraform: false },
  };
}
function blankHere() { const d = blank(); useHere(d.identity); return d; }

// A template keeps the shape of the app (connections, controls, checks, what it does)
// and drops everything that belongs to one customer.
//
// In a trial (an install with its own IdP) a template is a ready-to-run sample instead: its
// company IS the trial's, its servers are already running in its namespace and its people are
// created in the trial's Keycloak. So only what the IdP fills in (Client ID, External ID) is
// blanked, and the owner is whoever is signed in - nothing to type before Set up everything.
function fromSample(doc) {
  const d = clone(doc), m = d.metadata || {}, i = d.identity;
  let name = m.name || m.namespace || "app";
  for (let n = 2; SAVED.includes(name); n++) name = `${m.name}-${n}`;
  d.metadata = { ...m, name, owner: ME.email || ME.user || "", authors: [] };
  useHere(i);
  const ns = m.namespace || name;
  i.app.clientId = "";
  i.app.secretRef = `vault:secret/${ns}/${IDP_HERE.provider === "keycloak" ? "keycloak" : "auth0"}#client_secret`;
  i.sts.agent.externalId = "";
  // Each person by email and name only: setup creates them in the IdP and in Magertron (their
  // Magertron name comes from their name - "Dana Ortiz" is dana.ortiz, as the holds expect).
  i.sts.people = (i.sts.people || []).filter(x => x && x.email).map(x => ({ email: x.email, ...(x.name ? { name: x.name } : {}) }));
  if (i.people.mfa && IDP_HERE.provider === "keycloak") i.people.mfa.how = "client authentication flow with OTP";
  return d;
}

function fromTemplate(doc) {
  if (IDP_HERE) return fromSample(doc);
  const d = clone(doc);
  // In a trial (an install with its own IdP) the template's servers are already running in the
  // template's namespace, so it is kept: whatever the app is called, it finds them.
  d.metadata = { name: "", tenant: "", namespace: IDP_HERE ? (d.metadata?.namespace || "") : "",
                 template: d.metadata?.template, owner: "", description: d.metadata?.description };
  const i = d.identity;
  i.idp.domain = ""; i.app.clientId = ""; i.app.callbackUrls = []; i.app.logoutUrls = []; i.app.secretRef = "";
  i.sts.people = []; i.sts.agent.serviceAccount = ""; i.sts.agent.externalId = ""; i.sts.agent.roles = [];
  if (i.people.mfa) i.people.mfa.how = "";
  useHere(i);
  (d.controls?.holds || []).forEach(h => { h.approvers = []; });   // approvers are the old customer's people
  return d;
}

let NOTES = [];   // what a drafted Czarter still needs from the person (Describe it)
async function open(doc) {
  DOC = doc; STAGE = 0; DIRTY = false; LAST_DRY = null; OPEN.clear(); CSTEP.clear(); NOTES = [];
  LAST_NAME = (doc && doc.metadata && doc.metadata.name) || null;
  $("#home").hidden = true; $("#wizard").hidden = false;
  saveStatus("");
  try { ERRORS = (await api("/api/check", { doc: cleanDoc() })).errors; } catch { ERRORS = []; }
  render();
}

async function home() {
  if (DIRTY && !confirm("Leave without saving?")) return;
  $("#wizard").hidden = true; $("#home").hidden = false; $("#ctxbar").hidden = true;
  const [saved, templates, here] = await Promise.all([api("/api/czarters"), api("/api/templates"),
                                                      api("/api/idp-default").catch(() => ({}))]);
  IDP_HERE = here && here.provider ? here : null;
  SAVED = (saved || []).map(s => s.name);
  // In a trial: which templates are ready to run - every MCP server they list already registered in
  // the template's namespace (the trial's sample servers). The others still need their servers built.
  let live = new Set();
  if (IDP_HERE) {
    try { const r = await api("/api/magertron/servers"); live = new Set((r.servers || []).map(x => `${x.namespace}/${x.name}`)); } catch { /* no list: none ready */ }
  }
  const ready = t => !!IDP_HERE && (t.doc.connections || []).filter(x => x.kind !== "llm")
    .every(x => live.has(`${x.namespace || t.doc.metadata?.namespace}/${x.name}`)) && (t.doc.connections || []).length > 0;
  // Ready-to-run templates first: in a trial, that is the one to start with.
  const order = templates.map((t, i) => i).sort((a, b) => ready(templates[b]) - ready(templates[a]));
  $("#templates").innerHTML = order.map(i => { const t = templates[i], co = t.doc.metadata?.tenant, go = ready(t);
    return `<button class="card${go ? " ready" : ""}" data-tpl="${i}"><b>${co ? esc(co) + " · " : ""}${esc(t.template || t.id)}</b>${go ? '<span class="readytag">Ready to run</span>' : ""}
    <span>${esc(t.doc.metadata?.description || "")} ${go
      ? `Everything is filled in and its MCP servers are already running here: press Set up everything, then Verify.`
      : `Connections, controls and checks filled in; ${IDP_HERE ? `sign-in from this install's ${esc(AUTOMATED[IDP_HERE.provider] || IDP_HERE.provider)}; its servers are built from the hand-over pack.` : "your customer's identity left blank."}`}</span></button>`; }).join("");
  $("#templates").onclick = e => { const b = e.target.closest("[data-tpl]"); if (b) open(fromTemplate(templates[+b.dataset.tpl].doc)); };
  $("#saved").innerHTML = saved.length ? saved.map(s => `<div class="row"><span><b>${esc(s.name)}</b>
      <span class="muted"> · ${esc(s.tenant || "")}${s.template ? " · " + esc(s.template) : ""}</span></span>
      <span>${s.valid ? '<span class="okline">✓ valid</span>' : '<span class="muted">incomplete</span>'}
      <button class="ghost" data-clone="${esc(s.name)}">Clone</button>
      <button class="ghost" data-open="${esc(s.name)}">Open</button>
      <span class="kebabwrap"><button class="ghost kebab" data-menu="${esc(s.name)}" title="More" aria-label="More actions for ${esc(s.name)}">⋮</button>
        <span class="menu" hidden><button class="menuitem danger-text" data-delete="${esc(s.name)}">Retire…</button></span></span></span></div>`).join("")
    : '<p class="muted">None yet.</p>';
}

// ---------- Use Czartup from your IDE ----------
async function ideConnect(apply) {
  const box = $("#ideBox"), st = $("#ideStatus");
  // Spinner, and no second click while it works (registering + waiting for tools takes a while).
  const busy = on => document.querySelectorAll("#ideCheck, #ideGo").forEach(b => { b.disabled = on; });
  st.innerHTML = `<span class="spin" aria-hidden="true"></span>${apply ? "Connecting… this can take half a minute" : "Checking…"}`;
  busy(true);
  let r;
  try { r = await api("/api/connect-ide", { apply }); }
  catch (e) { st.textContent = ""; busy(false); box.innerHTML = `<pre class="out">❌ ${esc(e.message)}</pre>`; return; }
  st.textContent = ""; busy(false);
  const todo = (r.lines || []).some(l => l.startsWith("would "));
  // One blue button at a time: while "Connect now" is offered, Connect steps back; once
  // connected it becomes "Check again".
  const btn = $("#ideCheck");
  btn.classList.toggle("ghost", !!(r.dry && todo) || !!r.ok);
  btn.textContent = r.ok && !todo ? "Check again" : "Connect";
  const cfg = JSON.stringify(r.config || {}, null, 2);
  box.innerHTML = `<pre class="out">${esc((r.lines || []).join("\n"))}</pre>` +
    (r.dry && todo ? `<div class="confirm"><span>Make these changes?</span><span class="actions"><button id="ideGo">Connect now</button></span></div>` : "") +
    (r.ok && !todo && !(r.lines || []).some(l => /^(⚠️|❌)/.test(l)) ? `<p><b>Add this to your IDE's MCP settings</b> (Cursor: <code>.cursor/mcp.json</code>; Claude Code: <code>.mcp.json</code>; VS Code: <code>.vscode/mcp.json</code> under "servers").
      No token goes in the file: on first use the IDE opens your browser, you sign in to Magertron™ and click <b>Allow</b>.</p>
      <pre class="out" id="ideCfg">${esc(cfg)}</pre><button class="ghost" id="ideCopy">Copy</button>` : "");
}
// ---------- Use Czartup from your terminal: this Magertron's own address in the login line ----------
{ const b = document.getElementById("cliBase"); if (b) b.textContent = location.origin; }
document.addEventListener("click", e => {
  if (e.target.id === "cliCopy") navigator.clipboard?.writeText($("#cliCmd").textContent).then(() => { e.target.textContent = "Copied"; });
});

document.addEventListener("click", e => {
  if (e.target.id === "ideCheck") ideConnect(false);
  else if (e.target.id === "ideGo") ideConnect(true);
  else if (e.target.id === "ideCopy") navigator.clipboard?.writeText($("#ideCfg").textContent).then(() => { e.target.textContent = "Copied"; });
});

// ---------- wiring ----------
document.addEventListener("input", e => { if ($("#wizard").contains(e.target)) onInput(e); });
document.addEventListener("change", e => { if ($("#wizard").contains(e.target) && (e.target.type === "checkbox" || e.target.tagName === "SELECT")) onInput(e); });
document.addEventListener("click", e => {
  if (!e.target.closest(".kebabwrap")) document.querySelectorAll(".menu").forEach(x => { x.hidden = true; });
}, true);
document.addEventListener("click", e => {
  const h = e.target.closest("[data-cstep]");
  if (!h) return;
  CSTEP.set(h.dataset.cstep, h.dataset.open !== "1");
  renderChecklist();
});
document.addEventListener("click", async e => {
  const gh = e.target.closest("[data-gtoggle]");
  if (gh && !e.target.closest("button")) { const k = gh.dataset.gtoggle; GFOLD.has(k) ? GFOLD.delete(k) : GFOLD.add(k); render(); return; }
  const pk = e.target.closest("[data-pick]");
  if (pk) { setP(DOC, pk.dataset.pick + ".user", pk.dataset.user); setP(DOC, pk.dataset.pick + ".idpSubject", ""); touch(); render(); return; }
  if (e.target.dataset && e.target.dataset.unpick) {
    const k = e.target.dataset.unpick; setP(DOC, k + ".user", ""); setP(DOC, k + ".idpSubject", "");
    const em = String(getP(DOC, k + ".email") || "").trim().toLowerCase(); delete MATCH[em];
    touch(); render(); return;
  }
  const adv = e.target.closest("[data-advtoggle]");
  if (adv) {
    const k = adv.dataset.advtoggle;
    if (adv.dataset.open === "1") { OPEN.delete(k); OPEN.add(k + ":shut"); } else { OPEN.add(k); OPEN.delete(k + ":shut"); }
    render(); return;
  }
  const head = e.target.closest("[data-toggle]");
  if (head && !e.target.closest("button")) {
    const k = head.dataset.toggle; OPEN.has(k) ? OPEN.delete(k) : OPEN.add(k); render(); return;
  }
  const t = e.target.closest("button"); if (!t) return;
  if (t.dataset.stage) { STAGE = +t.dataset.stage; render(); }
  else if (t.dataset.add) {
    const a = getP(DOC, t.dataset.add) || []; a.push({}); setP(DOC, t.dataset.add, a);
    [...OPEN].filter(k => k.startsWith(t.dataset.add + ".")).forEach(k => OPEN.delete(k));   // new one open, the rest folded
    OPEN.add(`${t.dataset.add}.${a.length - 1}`); touch(); render();
  }
  else if (t.dataset.remove) {
    const a = getP(DOC, t.dataset.remove); a.splice(+t.dataset.i, 1);
    [...OPEN].filter(k => k.startsWith(t.dataset.remove + ".")).forEach(k => OPEN.delete(k)); touch(); render();
  }
  else if (t.dataset.gopen !== undefined) { const k = STAGES[STAGE].key; [...GFOLD].filter(x => x.startsWith(k + ":")).forEach(x => GFOLD.delete(x)); render(); }
  else if (t.dataset.gclose !== undefined) { const st = STAGES[STAGE]; st.groups.forEach((_, i) => GFOLD.add(`${st.key}:${i}`)); render(); }
  else if (t.dataset.openall) { (getP(DOC, t.dataset.openall) || []).forEach((_, i) => OPEN.add(`${t.dataset.openall}.${i}`)); render(); }
  else if (t.dataset.closeall) { [...OPEN].filter(k => k.startsWith(t.dataset.closeall + ".")).forEach(k => OPEN.delete(k)); render(); }
  else if (t.dataset.act) { document.querySelectorAll(".menu").forEach(x => { x.hidden = true; }); act(t.dataset.act); }
  else if (t.dataset.mag) loadMagertron(true);
  else if (t.dataset.clone || t.id === "cloneBtn") {
    const from = t.dataset.clone || getP(DOC, "metadata.name");
    if (t.id === "cloneBtn" && DIRTY) { try { await save(); } catch (err) { saveStatus("Not saved: " + err.message, true); return; } }
    const to = (prompt(`Name for the copy of "${from}" (lowercase letters, digits and dashes):`, `${from}-copy`) || "").trim();
    if (!to) return;
    try { const r = await api("/api/clone", { from, to }); DIRTY = false; await open(r.doc); saveStatus(`✓ Cloned from ${from}`); }
    catch (err) { alert("Not cloned: " + err.message); }
  }
  else if (t.dataset.menu) {
    const m = t.parentElement.querySelector(".menu"), was = !m.hidden;
    document.querySelectorAll(".menu").forEach(x => { x.hidden = true; });
    m.hidden = was;
  }
  else if (t.dataset.delete) {
    const n = t.dataset.delete;
    document.querySelectorAll(".menu").forEach(x => { x.hidden = true; });
    // Retire = the same API the IDE and the CLI use: preview first, change nothing until OK.
    const cut = s => s.length > 1800 ? s.slice(0, 1800) + "\n…" : s;
    let pv;
    working(`Checking what retiring ${n} would remove…`, n);
    try { pv = await api("/api/retire", { name: n, remove_idp: true }); }
    catch (err) { working(); alert("Could not preview retiring " + n + ": " + err.message); return; }
    working();
    if (!pv.ok) { alert(cut(pv.output || "Could not preview the retire.")); return; }
    if (!confirm(`Retire "${n}"?\n\n${cut(pv.output.replace(/\n*Nothing was changed\.\s*$/, ""))}\n\nOK removes these from Magertron™ and the IdP, and moves the Czarter to the trash. People's accounts stay.`)) return;
    working(`Retiring ${n}…`, n);
    try {
      const r = await api("/api/retire", { name: n, confirm: true, remove_idp: true });
      working();
      alert(cut(r.output || (r.ok ? "Retired." : "Could not retire.")));
    } catch (err) { working(); alert("Not retired: " + err.message); }
    home();
  }
  else if (t.dataset.open) { const r = await api(`/api/czarters/${encodeURIComponent(t.dataset.open)}`); open(r.doc); }
  else if (t.id === "doIt" && LAST_DRY) act(LAST_DRY, true);
  else if (t.id === "setupAll") setupPreview();
  else if (t.id === "setupGo") setupRun();
  else if (t.id === "setupCancel") { SETUP = null; $("#setupBox").innerHTML = ""; }
  else if (t.id === "cancelDry") { $("#confirmBox").innerHTML = ""; LAST_DRY = null; }
});
$("#prevBtn").onclick = () => { if (STAGE > 0) { STAGE--; render(); } };
$("#nextBtn").onclick = () => { if (STAGE < STAGES.length - 1) { STAGE++; render(); } };
$("#saveBtn").onclick = async () => { saveStatus("Saving…"); try { await save(); if (STAGES[STAGE].review) render(); } catch (e) { saveStatus("Not saved: " + e.message, true); } };
$("#toHome").onclick = home;
$("#newBlank").onclick = () => open(blankHere());
// "Describe it": the server asks a model THROUGH Magertron(TM), as you, for a draft;
// it opens unsaved, like a template, with what is left for you listed on top.
$("#newDescribe").onclick = () => { $("#describeBox").hidden = false; $("#newDescribe").hidden = true; $("#describeText").focus(); };
$("#describeCancel").onclick = () => { $("#describeBox").hidden = true; $("#newDescribe").hidden = false; $("#describeStatus").textContent = ""; };
$("#describeGo").onclick = async () => {
  const text = $("#describeText").value.trim(), go = $("#describeGo"), st = $("#describeStatus");
  if (text.length < 15) { st.textContent = "A sentence or two, please."; return; }
  go.disabled = true; st.textContent = "Drafting through Magertron™ - this takes up to a minute…";
  try {
    const r = await api("/api/describe", { text });
    await open(r.doc);
    DIRTY = true;
    NOTES = r.notes || [];
    saveStatus(`Drafted by ${r.model} - not saved yet. Check every step, then Save.`);
    render();
  } catch (e) { st.textContent = "Could not draft it: " + e.message; }
  finally { go.disabled = false; }
};
window.addEventListener("beforeunload", e => { if (DIRTY) { e.preventDefault(); e.returnValue = ""; } });

api("/api/status").then(s => {
  ME = { user: s.user || "", email: s.email || "" };
  if (s.version) { $("#ver").textContent = "v" + s.version; $("#verfloat").textContent = "Czartup v" + s.version; }
  $("#apiInfo").textContent = "Magertron™: " + s.api + (s.user ? `  ·  ${s.user}` : "");
  if (s.hosted && !consoleToken()) signInBanner("Czartup runs as you, and you are not signed in here.");
}).catch(() => {});
home().catch(() => {});   // no session yet: the sign-in banner already says what to do

// App icon (a file input, not a button): read it, send it base64; the server checks it is a square PNG.
document.addEventListener("change", async e => {
  const inp = e.target.closest("input[data-icon]");
  if (!inp || !inp.files || !inp.files[0]) return;
  const out = $("#out"), name = getP(DOC, "metadata.name");
  const b64 = await new Promise((ok, bad) => { const r = new FileReader();
    r.onload = () => ok(String(r.result).split(",")[1] || ""); r.onerror = bad; r.readAsDataURL(inp.files[0]); });
  try { const r = await api("/api/icon", { name, image_base64: b64 });
        out.classList.remove("muted"); out.textContent = r.output || (r.ok ? "Icon saved." : "Not saved."); }
  catch (err) { out.textContent = err.message; }
  inp.value = ""; loadStatus();
});
