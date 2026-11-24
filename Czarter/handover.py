#!/usr/bin/env python3
"""Step 9 - hand the Czarter over to the people (and coding agents) who finish the app.

  pack   writes the handover pack in standard formats:
           AGENTS.md            brief + guardrails, read by Codex, Cursor, Copilot, Devin...
           CLAUDE.md            imports AGENTS.md for Claude Code
           .specify/memory/constitution.md, specs/001-<name>/{spec,plan,tasks}.md
                                GitHub Spec Kit layout, so any spec-driven tool picks it up
           servers/<srv>/SERVER-SPEC.md   one per server that is not deployed yet
           policy/<name>.rego   the app's own policy module: limits, holds, the agent alone
           policy/<name>-configmap.yaml  that module as the ConfigMap the admin applies (kubectl apply -f)
           RUNBOOK.md, czarter.yaml, .env.example, README.md, .gitignore
  zip    the same pack as a versioned zip (<app>-handover-<date>-v<N>.zip) for an SI who works in
         their own repository; the version goes up only when the Czarter changed
  check  Test connection: can Czartup reach the repo host with its token (CZARTUP_REPO picks it)
  push   puts the pack in a private repo named after the app - GitHub (default): on a branch
         czartup/<app> with a pull request; Gitea: one commit on main - and files
         one issue per open runbook step, labelled by trade; closes issues whose step is
         now done. Dry run unless --yes.

The Gitea token is read from Vault (default vault:secret/czartup/gitea#token, override
with CZARTUP_GITEA_TOKEN) and never printed. Gitea URL: CZARTUP_GITEA
(default http://192.168.1.162:3000).

  python3 handover.py pack czarters/playbook-auto.czarter.yaml --runs czarters/playbook-auto.runs.json
  python3 handover.py push czarters/playbook-auto.czarter.yaml --runs ... [--yes] [--record RUNS]
"""
import argparse, base64, datetime, json, os, pathlib, sys, urllib.error, urllib.request

import yaml

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import runbook  # noqa: E402

# The address the BUILDERS use - always the public one. Hosted Czartup talks to the
# orchestrator in-cluster (CZARTER_API), but the pack must never carry that address.
GATEWAY = (os.environ.get("CZARTUP_PUBLIC_API") or os.environ.get("CZARTER_API", "https://app.magertron.com")).rstrip("/")
GITEA = os.environ.get("CZARTUP_GITEA", "http://192.168.1.162:3000").rstrip("/")
TOKEN_REF = os.environ.get("CZARTUP_GITEA_TOKEN", "vault:secret/czartup/gitea#token")
# Push to Repo (0.2.96): where the build kit goes - github (default) or gitea.
REPO = (os.environ.get("CZARTUP_REPO") or "github").strip().lower()
GITHUB_API = (os.environ.get("CZARTUP_GITHUB_API") or "https://api.github.com").rstrip("/")   # GitHub Enterprise: https://HOST/api/v3
GITHUB_WEB = (os.environ.get("CZARTUP_GITHUB_WEB") or ("https://github.com" if "api.github.com" in GITHUB_API
              else GITHUB_API.replace("/api/v3", ""))).rstrip("/")
GITHUB_OWNER = (os.environ.get("CZARTUP_GITHUB_OWNER") or "").strip()     # an org; empty = the token's own user
GITHUB_TOKEN_REF = os.environ.get("CZARTUP_GITHUB_TOKEN", "vault:secret/czartup/github#token")
OK = ("done", "passed")

# Which trade finishes each runbook section - becomes the issue label.
TRADE = {"Identity setup": ("identity", "1d76db"), "Token exchange (STS)": ("magertron", "5319e7"),
         "Routing": ("server", "0e8a16"), "Policy and money controls": ("policy", "d93f0b"),
         "Checks": ("qa", "fbca04")}


def ns_of(c, x):
    return x.get("namespace") or c["metadata"]["namespace"]


def mcp_url(c, x):
    return f"{GATEWAY}/mcp/{ns_of(c, x)}/{x['name']}/"


def policy_args(c):
    """(server, tool) -> {argument: why} - the argument names the policy reads."""
    out, ctl = {}, c.get("controls", {})
    for l in ctl.get("limits", []):
        if l.get("field"):
            out.setdefault((l["server"], l["tool"]), {})[l["field"]] = f"limit `{l['id']}`: {l['field']} {l['op']} {json.dumps(l['value'])}"
    for h in ctl.get("holds", []):
        w = h["when"]
        keys = ([w["argPresent"]] if "argPresent" in w else []) + list(w.get("argEquals", {})) + list(w.get("argAbove", {}))
        for k in keys:
            out.setdefault((h["server"], h["tool"]), {})[k] = f"hold `{h['id']}`"
    for k in c.get("checks", []):
        cl = k.get("call") or {}
        for a in (cl.get("arguments") or {}):
            out.setdefault((cl.get("server"), cl.get("tool")), {}).setdefault(a, f"check `{k['id']}`")
    return out


def arg_types(c):
    """(server, tool, argument) -> Python type name, from the values the Czarter compares it with."""
    out, ctl = {}, c.get("controls", {})
    def put(srv, tool, k, v):
        t = "bool" if isinstance(v, bool) else "float" if isinstance(v, (int, float)) else "str"
        out.setdefault((srv, tool, k), t)
    for l in ctl.get("limits", []):
        if l.get("field") is not None and "value" in l:
            v = l["value"][0] if isinstance(l["value"], list) and l["value"] else l["value"]
            put(l["server"], l["tool"], l["field"], v)
    for h in ctl.get("holds", []):
        for k, v in h["when"].get("argEquals", {}).items():
            put(h["server"], h["tool"], k, v)
        for k in h["when"].get("argAbove", {}):
            put(h["server"], h["tool"], k, 0.0)
    for k in c.get("checks", []):
        cl = k.get("call") or {}
        for a, v in (cl.get("arguments") or {}).items():
            put(cl.get("server"), cl.get("tool"), a, v)
    return out


def owner(c, log, sec, sid):
    """Who closes a runbook step: the customer's Magertron™ admin, or the SI building the app."""
    if sec == "Routing" and sid.startswith("connections."):
        name = sid.split(".", 1)[1]
        if any(x["name"] == name for x in missing_servers(c, log)):
            return "SI builds, admin registers"
        return "Magertron admin"
    if sec == "Checks":
        return "SI (QA)"
    return "Magertron admin"


WHOAMI = "whoami"


def tools_of(x):
    """The tools a connection needs; a Trusted upstream also exposes `whoami` (see apply.tools_of)."""
    t = list(x.get("tools", []))
    if x.get("forwardIdentity") and WHOAMI not in t and "*" not in t:
        t.append(WHOAMI)
    return t


def missing_servers(c, log):
    """Connections in the app's own namespace that Verify could not find (or never checked)."""
    own = c["metadata"]["namespace"]
    return [x for x in c["connections"]
            if ns_of(c, x) == own and x.get("kind") != "llm"
            and log.get(f"connections.{x['name']}", {}).get("status") not in OK]


# ---------- the files ----------

def guardrails(c):
    """(title, rule, sub-lines) the builders work inside - enforced by Magertron™, not by their code."""
    a, ctl = c["identity"]["sts"]["agent"], c.get("controls", {})
    tools = []
    for x in c["connections"]:
        if tools_of(x):
            tools.append(f"`{x['name']}` ({ns_of(c, x)}): " + ", ".join(f"`{t}`" for t in tools_of(x)))
        elif x.get("kind") == "llm":
            tools.append(f"`{x['name']}` ({ns_of(c, x)}): the LLM, through its LLM Consumer role")
    g = [("Every call goes through the gateway",
          "Every call to a tool, API or LLM goes through the Magertron™ gateway. Never call a vendor directly.", []),
         ("Separate services",
          "The app and each server it uses run as separate services. The app reaches a server only through its "
          "gateway address - never in-process, by import, or on the server's own host and port. "
          "Anything else skips the limits, holds and audit.", []),
         ("Only the granted tools",
          f"The agent is `{a['serviceAccount']}`. It may call only these tools; anything else is refused with 403:", tools)]
    for l in ctl.get("limits", []):
        rule = f"`{l['field']} {l['op']} {json.dumps(l['value'])}`" if l.get("field") else f"policy `{l['policyRef']}`"
        g.append((f"Limit: {l['description']}",
                  f"`{l['server']}.{l['tool']}` is refused by policy unless {rule} holds ({l['description']}).", []))
    for h in ctl.get("holds", []):
        g.append((f"Hold: {h['reason']}",
                  f"`{h['server']}.{h['tool']}` can be **held** for a human ({h['reason']}). The gateway answers 403 with "
                  f"`x-magertron-hold-id`; once approved, retry the identical call with that header. Holds expire after {h['ttlSeconds']} s.", []))
    for b in ctl.get("budgets", []):
        g.append((f"Budget on {b['server']}",
                  f"Spend on `{b['server']}` is capped by Magertron™ ({b['description']}). Expect 403 when the cap is reached; show it, do not retry in a loop.", []))
    if ctl.get("pii"):
        g.append(("PII is handled by the platform", f"PII on calls is handled by Magertron™: {ctl['pii']['action']}.", []))
    g += [("No secrets in code",
           "Secrets are never in code, config files, logs, tickets or chat. Read them from the references in `.env.example` (Vault or platform).", []),
          ("Never widen access",
           "Do not widen access: no `*` grants, no extra roles for the agent, no direct vendor keys. Ask the Czarter owner to change the Czarter instead.", [])]
    return g


def start_here(c, log):
    """The order of work and the stop points, so a bare "Build" is enough."""
    miss = [x["name"] for x in missing_servers(c, log)]
    fwd = {x["name"] for x in c["connections"] if x.get("forwardIdentity")}
    ns = c["metadata"]["namespace"]
    L = ["## Start here (when you are told \"Build\" or \"Go\")", "",
         "Work in this order. **Stop at each STOP**, tell the person exactly what they must do, and wait for them "
         "to say it is done. Never script, guess or skip a STOP - those are Magertron admin steps.", ""]
    n = 1
    if miss:
        names = ", ".join(f"`{x}`" for x in miss)
        L += [f"{n}. **Choose the app's address now, before anything else** (0.2.56): a port nothing else uses "
              "(check with `ss -ltn`) and its sign-in callback, e.g. `http://127.0.0.1:<port>/auth/callback`, logout "
              "`http://127.0.0.1:<port>/`. Put both in the Czarter (`identity.app.callbackUrls` and `logoutUrls`, "
              "save_czarter with overwrite) - the setup run at the STOP below then adds them to the IdP with everything "
              "else, so sign-in works the first time and nobody is asked about them.",
              f"{n}a. **Build the server(s): {names}.** Follow `servers/<name>/SERVER-SPEC.md` exactly "
              "(HTTPS on `0.0.0.0`, path `/mcp`, the exact tool names and fields, no policy limits in the server). "
              "Run each one as a **service** (`deploy/systemd/<name>.service`), not in an IDE terminal - a restarted IDE "
              "would stop it. Then smoke-test `initialize` and `tools/list` locally.",
              f"{n+1}. **Fill in `servers/<name>/server.json`** with this machine's `https://` address.",
              f"{n+2}. **STOP - admin.** Tell the person to: get a service-account token from the admin and run "
              "`mcpctl login <magertron-url> --token <token>`; right-click `server.json` → **Magertron: Submit**; "
              f"then have the Magertron admin run, in Czartup: **Register servers** (registers it in `{ns}`"
              + (", Trusted upstream on" if fwd & set(miss) else "")
              + "), **Approve tools**, **Tool grants**, **People's access** and "
              + ("**Install the app's policy** - all in one Set up everything run."
                 if os.environ.get("CZARTUP_K8S_POLICIES") == "1" else
                 f"install the app's policy: `kubectl apply -f policy/{c['metadata']['name']}-configmap.yaml` "
                 "(its own ConfigMap - never an edit to a shared policy file)."),
              f"{n+3}. **Confirm through the gateway:** `initialize` on each server's gateway URL returns 200. "
              "A 404 means it is not registered yet - go back to the STOP."]
        n += 4
    L += [f"{n}. **Build the app** (sign-in, STS exchange, screens, agents) against the gateway URLs only. "
          f"Run it as a service too (`deploy/systemd/{c['metadata']['name']}-app.service`).",
          "   - **Sign-in callback:** use the address you put in the Czarter in step 1 - the IdP accepts only the "
          "callback and logout URLs in `czarter.yaml`. Only if you must change it: update the Czarter (save_czarter with "
          "overwrite) and run setup - Czartup adds them to the IdP app. Never ask the admin to edit the IdP by hand.",
          *(["   - **Secrets from Vault:** read `IDP_CLIENT_SECRET_REF` from `VAULT_ADDR`, verifying its certificate with "
             "`VAULT_CACERT` - the Vault address and CA this Magertron install uses (ask the admin for the CA file). Use the "
             "address the certificate names, not 127.0.0.1 unless it is named. Never turn verification off "
             "(no `VAULT_SKIP_VERIFY`, no `verify=False`)."]
            if str(c["identity"]["app"].get("secretRef", "")).startswith("vault:") else []),
          f"{n+1}. **Run the checks in `czarter.yaml` as live gateway calls.** If one fails because a platform step is "
          "still open (see *Who does what*), **STOP - admin**: name the step and wait.",
          *([f"   **Plus the identity check** for each Trusted upstream ("
              + ", ".join(f"`{x['name']}`" for x in c["connections"] if x.get("forwardIdentity"))
              + f"): signed in as a person, call `{WHOAMI}` through the gateway - `acting_for` must be that person and "
              "`subject` the app's service account. Empty `acting_for` means Magertron is not forwarding the person: **STOP - admin**."]
            if any(x.get("forwardIdentity") for x in c["connections"]) else []),
          f"{n+2}. **STOP - done.** Ask the person to run **Verify** in Czartup. Done when it is all green.", ""]
    return L


def agents_md(c, log):
    m, f, idn = c["metadata"], c.get("functionality") or {}, c["identity"]
    a, ppl = idn["sts"]["agent"], idn["people"]
    L = [f"# AGENTS.md - {m['name']}", "",
         f"This repository was handed over from the Magertron™ Czarter `{m['name']}` (customer `{m['tenant']}`, "
         f"namespace `{m['namespace']}`). `czarter.yaml` is the source of truth; this file is the brief for whoever builds it - "
         "a person or a coding agent.", ""]
    L += start_here(c, log)
    L += ["## What to build", "", f.get("summary") or m.get("description") or "See czarter.yaml.", ""]
    for s in f.get("surfaces", []):
        L.append(f"- **Screen: {s['name']}** - {s['does']}")
    for ag in f.get("agents", []):
        cad = f", every {'/'.join(map(str, ag['cadenceMinutes']))} min" if ag.get("cadenceMinutes") else ""
        when = f", only when {ag['onlyWhen']}" if ag.get("onlyWhen") else ""
        L.append(f"- **Agent: {ag['name']}**{cad}{when}. May: {'; '.join(ag['may'])}. Must not: {'; '.join(ag['mustNot'])}."
                 + (f" On its own (nobody on the call) it may call only: {', '.join(ag['tools']) or 'nothing'} - "
                    "the gateway refuses anything else, so build it to act FOR a person when it needs more."
                    if "tools" in ag else ""))
    L += ["", "Full spec, plan and tasks: `specs/001-" + m["name"] + "/` (GitHub Spec Kit layout).", "",
          "## Guardrails (enforced by Magertron™ - your code cannot get around them)", ""]
    for _, rule, subs in guardrails(c):
        L.append(f"- {rule}")
        L += [f"  - {x}" for x in subs]
    L += ["", "## Identity and calls", "",
          f"1. **The person signs in** at `{idn['idp']['domain']}` with authorization code + PKCE"
          + (", `prompt=login`" if ppl.get("promptLogin") else "")
          + (", MFA required" if ppl.get("mfa", {}).get("required") else "") + f". Audience `{idn['idp']['audience']}`.",
          f"2. **The app gets its own token** with client credentials (client ID `{idn['app']['clientId']}`; the secret from `IDP_CLIENT_SECRET_REF`).",
          f"3. **Exchange both at the Magertron™ STS**: `POST {GATEWAY}/api/v1/token/exchange` with "
          "`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, the person's token as `subject_token` and the app's as `actor_token` "
          "(both `urn:ietf:params:oauth:token-type:jwt`). The result acts as "
          f"`{a['serviceAccount']}` on behalf of the signed-in person.",
          "4. **Call through the gateway** with that token:"]
    for x in c["connections"]:
        if x.get("kind") == "llm":
            L.append(f"   - `{x['name']}`: `{GATEWAY}/api/v1/llm/{ns_of(c, x)}/{x['name']}/v1/chat/completions` (OpenAI-compatible)")
        else:
            L.append(f"   - `{x['name']}`: `{mcp_url(c, x)}` (MCP: initialize, then tools/call with `Mcp-Session-Id`)")
    unattended = [ag["name"] for ag in f.get("agents", []) if ag.get("cadenceMinutes") or ag.get("onlyWhen")]
    if unattended:
        L += ["", "## Agents that run with nobody signed in", "",
              f"{', '.join(unattended)} run{'s' if len(unattended) == 1 else ''} on a schedule, so there is no person token. "
              f"{'It gets' if len(unattended) == 1 else 'They get'} **the app's own client-credentials token** and "
              f"exchange{'s' if len(unattended) == 1 else ''} it at the STS **on its own**: `subject_token` = the app's token, "
              "**no `actor_token`** (the gateway accepts only Magertron-signed tokens, never the IdP's directly). "
              f"The result acts as `{a['serviceAccount']}` alone. Their reach is exactly the agent's own tools."]
    op = open_steps_of(c, log)
    if op:
        L += ["", "## Who does what", "",
              "Open steps belong to two people. **Do not script the Magertron admin steps** - grants, policy, "
              "budgets, PII and identity settings are done by the customer's Magertron admin in Czartup and the console.", "",
              "| Step | Owner |", "|---|---|"]
        L += [f"| {title} (`{sid}`) | {owner(c, log, sec, sid)} |" for sec, sid, title, _ in op]
    fwd = [x["name"] for x in c["connections"] if x.get("forwardIdentity")]
    if fwd:
        L += ["", "## Who the user is", "",
              f"{', '.join(f'`{n}`' for n in fwd)} receive{'s' if len(fwd) == 1 else ''} the verified user from the gateway in "
              "`x-magertron-acting-for` (and the agent in `x-magertron-subject`). The admin must turn on **Trusted upstream** "
              "for each when registering it. Servers never take the user from a tool argument."]
    L += [""] + approvals_md(c)
    L += ["## Definition of done", "",
          "- Every open issue in this repository is closed.",
          "- **Verify is all green** in Czartup (it re-checks identity, servers, grants and policy against the live platform).",
          "- The checks in `czarter.yaml` pass as live calls **through the gateway**. A check that must be refused passes "
          "only on **HTTP 403 from the Magertron gateway**; a check that must be held only on 403 with `x-magertron-hold-id`. "
          "A tool result such as `ok: false` from the server itself does not count - it means the gateway let the call through.", ""]
    miss = missing_servers(c, log)
    if miss:
        L += ["## Servers to build", ""] + [f"- `{x['name']}` - see `servers/{x['name']}/SERVER-SPEC.md`" for x in miss] + [""]
    return "\n".join(L)


def constitution(c):
    m = c["metadata"]
    L = [f"# {m['name']} Constitution", "",
         "Generated from the Magertron™ Czarter. These principles override any plan or task; "
         "change them only by changing `czarter.yaml`.", "", "## Core Principles", ""]
    for i, (title, rule, subs) in enumerate(guardrails(c), 1):
        L += [f"### {i}. {title}", "", rule] + [f"- {x}" for x in subs] + [""]
    L += ["## Governance", "", f"Owner: {m['owner']}. Verify in Czartup is the acceptance gate.", ""]
    return "\n".join(L)


def approvers_text(c, h):
    """Who may decide a hold, in words."""
    return (", ".join(f"`{a}`" for a in h.get("approvers") or []) or
            f"anyone with Deploy on `{server_ns(c, h['server'])}`") + " - never the person it holds, never the agent"


def server_ns(c, server):
    x = next((x for x in c["connections"] if x["name"] == server), None)
    return ns_of(c, x) if x else c["metadata"]["namespace"]


def approvals_md(c):
    """AGENTS.md: the approvals screen, when the Czarter has holds. Approving happens in the customer's own
    app, through the Magertron holds API, with the approver's own token - Magertron records the decision and
    lets the retried call through, and refuses a decision by the person the hold is for or by the agent."""
    holds = (c.get("controls") or {}).get("holds") or []
    if not holds:
        return []
    ns = sorted({server_ns(c, h["server"]) for h in holds})
    L = ["## Approvals screen (the app has holds)", "",
         "Some calls are **held** for a person's approval. Build an **Approvals** screen in the app - approvers decide "
         "there, in this app, not in the Magertron™ console:", "",
         "| Hold | Who may approve |", "|---|---|"]
    L += [f"| `{h['server']}/{h['tool']}` - {h['reason']} | {approvers_text(c, h)} |" for h in holds]
    L += ["", "How:", "",
          f"- List: `GET $MAGERTRON_URL/api/v1/holds` with the signed-in person's everyday token (the agent acting for "
          "them, from their STS exchange) - never the agent's token alone, never a second token, never logged or shown. Keep the ones for namespace "
          + ", ".join(f"`{n}`" for n in ns) + ". Show who asked (`on_behalf_of`), the tool, the reason, the state and "
          "when it expires; pending only by default, with a *show all* toggle.",
          "- Decide: `POST $MAGERTRON_URL/api/v1/holds/<hold_id>/approve` or `/decline`, body `{\"note\": \"...\"}` "
          "(optional), with the **same everyday token** (the agent acting for the signed-in person). Magertron judges "
          "the decision by the **person** on that token - the agent is only the channel - so the app needs no second "
          "token. Show Magertron's answer, and say *Approved* / *Declined* on success: a 403 such as \"a hold cannot be "
          "decided by the agent or the person it holds\" is the control working (nobody approves their own request) - "
          "show it, don't hide the button.",
          "- The person who asked: their screen shows *waiting for approval* (the hold's reason and `expires_at`, never "
          "the raw response). On the Approvals screen, an **approved** row they asked for has a **Resubmit** button. "
          "Resubmit takes the server, tool and arguments **from the hold itself** (`GET $MAGERTRON_URL/api/v1/holds/<id>`, "
          "never from the app's memory, so a restart loses nothing), opens a **fresh MCP session** to that server "
          "(initialize), and sends the call **once** with `x-magertron-hold-id: <id>`. **Never retry it automatically**: "
          "one approval lets exactly one call through, and a retry after a transport error is held again. Without "
          "Resubmit an approval does nothing.",
          "- Every action shows its result next to its button: *Approved*, *Declined*, *Created* (or the tool's own "
          "result), or Magertron's message as it is. A 401 says *Your sign-in has ended - sign in again*. Never fail "
          "silently.",
          "- Never let the app or the agent decide a hold itself - Magertron refuses it, and the Czarter forbids it.",
          "- `reference/approvals.py` is a working reference for all of the above. **Adapt it** to this app's language "
          "and framework; keep the token rule, the header and the retry exactly.", ""]
    return L


APPROVALS_REFERENCE = """\"\"\"Reference: an app's approvals, the way Magertron(TM) expects them (from the Czarter's hand-over pack).

ADAPT this to the app's language and framework - do not copy it blindly. What must stay exactly as here:
  - every call uses the signed-in person's EVERYDAY token: the STS exchange of their IdP token
    (subject_token) WITH the app's own token (actor_token). There is no second token for approving;
  - a held call answers HTTP 403 with the header x-magertron-hold-id: show "waiting for approval" -
    never the raw response. Magertron keeps the call's exact arguments on the hold;
  - approve / decline: POST /api/v1/holds/<id>/approve|decline, body {"note": "..."} (optional);
  - once approved, Resubmit: read the hold, open a fresh MCP session, send the identical call ONCE with
    the header x-magertron-hold-id: <id>. Never retry automatically;
  - every action shows its result beside its button; a 401 says "Your sign-in has ended - sign in again";
  - show Magertron's answer as it is. A 403 "a hold cannot be decided by the agent or the person it
    holds" means someone tried to approve their own request - the control working;
  - never log or show a token.
\"\"\"
import json, urllib.error, urllib.request

MAGERTRON = "https://app.magertron.com"      # from MAGERTRON_URL in .env
UA = "your-app/1"                           # a named User-Agent: Cloudflare refuses Python's default one


def _call(method, path, bearer, body=None):
    req = urllib.request.Request(MAGERTRON + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + bearer, "Accept": "application/json",
                                          "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def list_holds(everyday_token, namespace, show_all=False):
    \"\"\"What the Approvals screen shows: who asked, the tool, why, the state, when it expires.\"\"\"
    st, doc = _call("GET", "/api/v1/holds", everyday_token)
    if st != 200:
        return st, (doc or {}).get("error", "")
    rows = []
    for h in (doc or {}).get("holds") or []:
        if h.get("namespace") != namespace or (not show_all and h.get("state") != "pending"):
            continue
        rows.append({"id": h.get("hold_id"), "who": h.get("on_behalf_of") or h.get("subject"),
                     "tool": f"{h.get('server')}/{h.get('tool')}", "reason": h.get("reason"),
                     "state": h.get("state"), "expires": h.get("expires_at")})
    return 200, rows


def decide(everyday_token, hold_id, approve, note=""):
    \"\"\"Approve or decline as the signed-in person. Returns (ok, text to show).\"\"\"
    st, doc = _call("POST", f"/api/v1/holds/{hold_id}/{'approve' if approve else 'decline'}",
                    everyday_token, {"note": note[:500]} if note else {})
    if 200 <= st < 300:
        return True, "Approved" if approve else "Declined"
    return False, (doc or {}).get("error") or f"Magertron answered {st}"


def get_hold(everyday_token, hold_id):
    \"\"\"One hold, as Magertron stored it - with the call's server, tool and exact arguments.\"\"\"
    return _call("GET", f"/api/v1/holds/{hold_id}", everyday_token)


def resubmit(hold_id, everyday_token, open_session, call_tool):
    \"\"\"The Resubmit button: on an APPROVED row, shown to the person who asked.
    - takes server, tool and arguments FROM THE HOLD (survives an app restart);
    - opens a FRESH MCP session to that server (open_session = the app's initialize);
    - sends the call ONCE with x-magertron-hold-id (call_tool must add the header when given hold_id).
    Never retry: one approval lets exactly one call through.\"\"\"
    st, h = get_hold(everyday_token, hold_id)
    if st != 200:
        return False, (h or {}).get("error") or f"Magertron answered {st}"
    if h.get("state") != "approved":
        return False, f"This request is {h.get('state')}, not approved."
    session = open_session(h["server"], everyday_token)
    try:
        return True, call_tool(h["server"], h["tool"], h.get("arguments") or {}, everyday_token,
                               session=session, hold_id=hold_id)
    except Exception as e:  # noqa: BLE001 - show it, do not retry
        return False, str(e)
"""


def spec_md(c):
    m, f = c["metadata"], c.get("functionality") or {}
    L = [f"# Feature Specification: {m['name']}", "",
         f"**Feature Branch**: `001-{m['name']}`  ", f"**Created**: {datetime.date.today().isoformat()}  ",
         "**Status**: Handed over  ", f"**Input**: Magertron™ Czarter `{m['name']}`", "",
         "## User Scenarios & Testing *(mandatory)*", ""]
    n = 0
    for s in f.get("surfaces", []):
        n += 1
        L += [f"### User Story {n} - {s['name']}", "", s["does"] + ".", "",
              "**Acceptance Scenarios**:", "",
              f"1. **Given** a signed-in person, **When** they open {s['name']}, **Then** it {s['does'][0].lower() + s['does'][1:]}, "
              "with every call made through the Magertron™ gateway.", ""]
    for h in (c.get("controls") or {}).get("holds") or []:
        n += 1
        L += [f"### User Story {n} - Approve: {h['reason']}", "",
              f"A call to `{h['server']}/{h['tool']}` is held until an approver decides ({approvers_text(c, h)}).", "",
              "**Acceptance Scenarios**:", "",
              "1. **Given** a person's request is held, **When** they look, **Then** they see it waiting for approval "
              "and when it expires.",
              "2. **Given** an approver opens Approvals, **When** they approve, **Then** the requester's identical "
              "resubmission goes through.",
              "3. **Given** the person who asked, **When** they try to approve their own request, **Then** Magertron™ "
              "refuses it (403) and the screen shows why.", ""]
    for ag in f.get("agents", []):
        n += 1
        L += [f"### User Story {n} - {ag['name']} agent", "",
              f"The agent may: {'; '.join(ag['may'])}.", "", "**Acceptance Scenarios**:", ""]
        for j, mn in enumerate(ag["mustNot"], 1):
            L.append(f"{j}. **Given** the agent is running, **When** it tries to {mn}, **Then** it is stopped.")
        L.append("")
    L += ["### Edge Cases", "",
          "- A call is refused by policy (403): show the reason, do not retry.",
          "- A call is held (403 with `x-magertron-hold-id`): show the hold's `message` and when it expires (`expires_at`), "
          "as waiting for approval - never the raw response. Keep `hold_id`; once approved, retry the identical call with "
          "the header.",
          "- A budget cap is reached: show it, stop the loop.", "",
          "## Requirements *(mandatory)*", "", "### Functional Requirements", ""]
    reqs = [f"System MUST provide the {s['name']} screen: {s['does']}." for s in f.get("surfaces", [])]
    reqs += [f"The {ag['name']} agent MUST NOT {mn}." for ag in f.get("agents", []) for mn in ag["mustNot"]]
    reqs += [rule + (" " + "; ".join(subs) + "." if subs else "") for _, rule, subs in guardrails(c)]
    L += [f"- **FR-{i:03d}**: {r}" for i, r in enumerate(reqs, 1)]
    L += ["", "### Key Entities", "", f"- **Person**: signs in at the customer's IdP; acts as their Magertron™ user.",
          f"- **Agent**: `{c['identity']['sts']['agent']['serviceAccount']}`, the app's own identity.",
          "- **Connections**: " + ", ".join(f"`{x['name']}`" for x in c["connections"]) + ".", "",
          "## Success Criteria *(mandatory)*", "", "### Measurable Outcomes", ""]
    L += [f"- **SC-{i:03d}**: [{k['kind']}] {k['description']} - expect: {k['expect']}." for i, k in enumerate(c.get("checks", []), 1)]
    L.append(f"- **SC-{len(c.get('checks', [])) + 1:03d}**: Verify in Czartup is all green.")
    return "\n".join(L) + "\n"


def plan_md(c):
    m, idn = c["metadata"], c["identity"]
    L = [f"# Implementation Plan: {m['name']}", "",
         f"**Branch**: `001-{m['name']}` | **Spec**: [spec.md](./spec.md)", "",
         "## Summary", "", (c.get("functionality") or {}).get("summary") or m.get("description", ""), "",
         "## Technical Context", "",
         f"**Identity**: {runbook.PROVIDER.get(idn['idp']['provider'], idn['idp']['provider'])} tenant `{idn['idp']['domain']}`, "
         f"audience `{idn['idp']['audience']}`, joined at the Magertron™ STS.  ",
         f"**Gateway**: `{GATEWAY}` - namespace `{m['namespace']}`.  ",
         "**Dependencies**: " + ", ".join(f"{x['name']} ({runbook.KIND.get(x['kind'], x['kind'])})" for x in c["connections"]) + ".  ",
         "**Language/Framework**: builder's choice.  ", "",
         "## Constitution Check", "", "See `.specify/memory/constitution.md`. Every principle is enforced by the platform; "
         "the plan must not depend on breaking one (no direct vendor calls, no wider grants, no secrets in code).", "",
         "## Project Structure", "", "Builder's choice. Keep `czarter.yaml`, `AGENTS.md` and `specs/` at the root.", ""]
    return "\n".join(L)


def tasks_md(c, log, open_steps):
    m = c["metadata"]
    L = [f"# Tasks: {m['name']}", "", "**Input**: `specs/001-" + m["name"] + "/` and `czarter.yaml`.  ",
         "Platform steps are tracked as issues in this repository (`[step id]` in the title). "
         "`[P]` = can run in parallel.", "", "## Phase 1: Setup", "",
         "- [ ] T001 Copy `.env.example` to `.env` and fill in the references (never the secret values)",
         "- [ ] T002 [P] Read `AGENTS.md` and the constitution", ""]
    t = 3
    if open_steps:
        L += ["## Phase 2: Platform steps still open (blocking)", "",
              "Owner in brackets. Magertron admin steps are done in Czartup and the console - not scripted by the SI.", ""]
        for sec, sid, title, _ in open_steps:
            L.append(f"- [ ] T{t:03d} [P] [{owner(c, log, sec, sid)}] {title} - step `{sid}`")
            t += 1
        L.append("")
    L += ["## Phase 3: The app", ""]
    for s in (c.get("functionality") or {}).get("surfaces", []):
        L.append(f"- [ ] T{t:03d} [P] Build {s['name']}: {s['does']}"); t += 1
    for ag in (c.get("functionality") or {}).get("agents", []):
        L.append(f"- [ ] T{t:03d} Build the {ag['name']} agent within its May / Must not lists"); t += 1
    L.append(f"- [ ] T{t:03d} Sign-in and STS exchange as in AGENTS.md; handle refused, held and capped calls"); t += 1
    if (c.get("controls") or {}).get("holds"):
        L.append(f"- [ ] T{t:03d} Build the Approvals screen (AGENTS.md: list and decide holds with the approver's own token)"); t += 1
    L += ["", "## Phase 4: Proof", "",
          f"- [ ] T{t:03d} Run every check in `czarter.yaml` as a live call", f"- [ ] T{t + 1:03d} Verify in Czartup is all green", ""]
    return "\n".join(L)


def systemd_unit(desc, workdir, cmd, env=None):
    """A systemd USER unit: runs without a terminal or an IDE, restarts on failure. %h = the home folder."""
    return "\n".join([
        "# Install: copy to ~/.config/systemd/user/, set the paths and command, then",
        "#   systemctl --user daemon-reload && systemctl --user enable --now <this file's name>",
        "# Logs: journalctl --user -u <this file's name> -f",
        "[Unit]", f"Description={desc}", "After=network-online.target", "",
        "[Service]", f"WorkingDirectory=%h/<repo>/{workdir}".replace("/.", ""), f"ExecStart={cmd}",
        *([f"EnvironmentFile=%h/<repo>/{env}"] if env else []),
        "Restart=on-failure", "RestartSec=3", "",
        "[Install]", "WantedBy=default.target", ""])


def systemd_readme(c, servers):
    n = c["metadata"]["name"]
    units = [f"{s}.service" for s in servers] + [f"{n}-app.service"]
    return "\n".join([
        f"# Run {n} as services", "",
        "The servers and the app run as **systemd user services**: they start without a terminal, keep running when "
        "the IDE closes or restarts, and restart on failure. Anything started only from an IDE terminal stops with it.", "",
        "1. In each `.service` file, replace `<repo>` with this repository's folder (relative to your home) and, for the "
        "app, `<the command that starts the app>`.",
        "2. Install and start them:", "", "```",
        "mkdir -p ~/.config/systemd/user", *[f"cp deploy/systemd/{u} ~/.config/systemd/user/" for u in units],
        "systemctl --user daemon-reload", *[f"systemctl --user enable --now {u}" for u in units], "```", "",
        "3. To keep them running after you log out: `loginctl enable-linger $USER` (once).", "",
        "Check: `systemctl --user status " + " ".join(units) + "`. Logs: `journalctl --user -u <name> -f`.", "",
        "Docker Compose is fine instead - one service per server plus the app, `restart: unless-stopped`.", ""])


def server_spec(c, x):
    args = policy_args(c)
    L = [f"# SERVER-SPEC: {x['name']}", "",
         f"Build this {runbook.KIND.get(x['kind'], x['kind'])} and register it in Magertron™ as "
         f"`{ns_of(c, x)}/{x['name']}`. Purpose: {x['purpose']}.", "",
         "## Tools - names must match exactly", "",
         "The agent is granted these names one by one; a renamed tool is simply not reachable.", "",
         "| Tool | Arguments the policy reads | Why |", "|---|---|---|"]
    for t in tools_of(x):
        if t == WHOAMI and x.get("forwardIdentity"):
            L.append(f"| `{WHOAMI}` | - | Identity check: returns the identity headers it received (see below) |")
            continue
        need = args.get((x["name"], t), {})
        L.append(f"| `{t}` | " + (", ".join(f"`{k}`" for k in need) or "-") + " | " + ("; ".join(need.values()) or "-") + " |")
    types = arg_types(c)
    L += ["", "Types: " + (", ".join(f"`{k}` {v}" for (sv, _, k), v in sorted(types.items()) if sv == x["name"]) or "-") + ".",
          "", "**An argument the policy reads must keep exactly that name and type.** If `stake` is called "
          "`amount`, the limit on it silently stops applying.", "",
          "**Do not copy policy limits into the server** (for example `stake <= 1000`). Magertron enforces them at the "
          "gateway; if the server refuses too, the deny check cannot tell who refused, and a missing policy goes unnoticed.", "",
          ] + identity_section(c, x) + [
          "## Credentials", "", f"`{x.get('credentialRef', 'platform')}` - injected by Magertron™ at the gateway. "
          "The server never holds the caller's or a vendor's key in code.", "",
          "## Hosting - the gateway must reach it over HTTPS", "",
          "- Run it as its **own service**, separate from the app.",
          "- Listen on `0.0.0.0` (all interfaces), not `127.0.0.1`, on a port the Magertron gateway can reach.",
          "- **Pick a port nothing else uses** - other apps' servers may run on this machine. Check first "
          "(`ss -ltn | grep :<port>` prints nothing when it is free), never reuse a port from another project, and give "
          "the app itself its own port too (its sign-in callback URL carries that port, so change both together).",
          "- Serve **HTTPS**. The gateway drops external servers with an `http://` address. A self-signed certificate is "
          "fine on a private address; for a public host, use a real certificate, or give the admin your CA certificate "
          "(the public file, never the key) for **Trusted CA**.",
          "- The MCP endpoint path is `/mcp`.",
          "- **Run it as a service that survives restarts**, never only in an IDE terminal: closing the IDE stops "
          f"everything it started. Use `deploy/systemd/{x['name']}.service` from this pack (systemd user unit, restarts "
          "on failure) or a Docker Compose service.",
          "- **Allowed hosts:** MCP SDK servers refuse an unknown `Host` header (HTTP 421 / \"Invalid Host header\"). "
          "Read the allowed host from **`server.json`** (the address you submit, which is the one Magertron™ registers and "
          "the gateway calls), as the skeleton below does - never type it a second time, and never only in a shell "
          "variable, which is lost on the next restart.",
          "- **Keep tool definitions stable.** Names, descriptions and argument schemas must not change between restarts. "
          "A changed definition is a NEW definition: Magertron™ refuses it until an admin approves it again "
          "(\"tool definition is new and not yet approved\").", "",
          "## Register it", "",
          f"1. Put the server's HTTPS address in `server.json` (next to this file) - replace `<host>` and `<port>`.",
          "2. Get a Magertron identity from the customer's admin: a **service-account token** with the narrowest role "
          "that can submit servers. Log in once: `mcpctl login https://<magertron> --token \"$(cat <token-file>)\"`.",
          "3. Install the Magertron submit extension (Cursor: magertron.com/cursor, VS Code: magertron.com/vscode), "
          "right-click `server.json` → **Magertron: Submit**. Submitting does not deploy it.",
          f"4. The Magertron admin runs, in Czartup: **Register servers** (registers `{x['name']}` in `{ns_of(c, x)}` at "
          "your HTTPS address" + (", Trusted upstream on" if x.get("forwardIdentity") else "") + "), **Approve tools**, "
          "**Tool grants**, **People's access**, then **Verify**.", "",
          "## Done when", "", f"- It appears in Magertron™ under `{ns_of(c, x)}` with the tools above.",
          "- Czartup **Tool grants** grants them with no failures.",
          f"- An `initialize` through `{mcp_url(c, x)}` answers 200 (not 404 - a 404 means the gateway has no route).",
          "- Verify shows it green.", "", "## Skeleton (Python MCP SDK)", "",
          "Names and argument types below are fixed; the bodies are yours. API names vary between SDK versions - "
          "check yours.", "", "```python",
          "import uvicorn", "from mcp.server.fastmcp import FastMCP", "",
          "from mcp.server.transport_security import TransportSecuritySettings", "",
          "# The gateway calls this server at the address in server.json - the one you submit and Magertron",
          "# registers - and the SDK refuses any other Host (HTTP 421). Read it from there, so the two can never differ.",
          "import json, pathlib", "from urllib.parse import urlparse",
          '_url = json.loads((pathlib.Path(__file__).parent / "server.json").read_text())["remotes"][0]["url"]',
          "ALLOWED = TransportSecuritySettings(enable_dns_rebinding_protection=True,",
          '                                   allowed_hosts=[urlparse(_url).netloc, "localhost:*", "127.0.0.1:*"])',
          f'mcp = FastMCP("{x["name"]}", transport_security=ALLOWED)', ""]
    if x.get("forwardIdentity"):
        L.insert(L.index("from mcp.server.fastmcp import FastMCP"), "from mcp.server.fastmcp import Context")
    for t in tools_of(x):
        if t == WHOAMI and x.get("forwardIdentity"):
            L += ["@mcp.tool()", f"def {WHOAMI}(ctx: Context) -> dict:",
                  '    """Identity check: what the Magertron gateway told this server about the caller."""',
                  "    h = ctx.request_context.request.headers",
                  '    return {"subject": h.get("x-magertron-subject", ""), "acting_for": h.get("x-magertron-acting-for", "")}', ""]
            continue
        need = list(args.get((x["name"], t), {}))
        types = arg_types(c)
        sig = ", ".join(f"{k}: {types.get((x['name'], t, k), 'str')}" for k in need)
        L += ["@mcp.tool()", f"def {t.replace('-', '_')}({sig}) -> dict:", f'    """{t}"""', "    raise NotImplementedError", ""]
    L += ['if __name__ == "__main__":',
          "    # HTTPS on all interfaces, so the Magertron gateway can reach it.",
          '    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=8443,',
          '                ssl_certfile="server.crt", ssl_keyfile="server.key")', "```", "",
          "A self-signed certificate for a private address:", "", "```",
          'openssl req -x509 -newkey rsa:2048 -nodes -keyout server.key -out server.crt -days 90 '
          '-subj "/CN=<host>" -addext "subjectAltName=IP:<host-ip>"', "```", ""]
    return "\n".join(L)


def identity_section(c, x):
    """Who is calling, as the server sees it."""
    if x.get("forwardIdentity"):
        return ["## Who is calling - read it from the gateway, never from arguments", "",
                "This is the customer's external system (not a vendor), so the admin marks it **Trusted upstream** and Magertron™ adds, "
                "on every allowed call, overwriting anything the client sent:", "",
                "| Header | Holds |", "|---|---|",
                "| `x-magertron-acting-for` | the person the call is for (e.g. the signed-in player); empty when an agent acts alone |",
                "| `x-magertron-subject` | the verified caller - the agent's service account |", "",
                "Use `x-magertron-acting-for` as the user (the player, the account holder). Never take the user from a tool "
                "argument - a client could name anyone. When it is empty, only agent-level work is allowed "
                "(e.g. settling finished bets).", "",
                f"### Identity check - `{WHOAMI}`", "",
                f"Also expose a read-only tool `{WHOAMI}` that returns exactly the two headers it received: "
                '`{"subject": <x-magertron-subject>, "acting_for": <x-magertron-acting-for>}` (empty strings when absent). '
                "Nothing else - no tokens, no other headers. It is how Czartup's Verify and your own check prove the "
                "identity really arrives; without it a broken forward looks like an app bug (\"no player\").", ""]
    return ["## Who is calling", "",
            "Magertron™ does **not** tell this server who is calling (it is treated as a vendor). If it must act per "
            "user, ask the Czarter owner to set `forwardIdentity: true` on this connection.", ""]


def server_json(c, x):
    """MCP Registry-style manifest for the Magertron submit extension; the SI fills in where it runs."""
    # _meta (MCP Registry: vendor keys in reverse-DNS form) carries the Czarter's decisions,
    # so the console can pre-fill the External server form from the submission. The console
    # treats forwardIdentity as a REQUEST and checks the fingerprint against the Czarter on
    # record: the SI controls this file, the Czarter decides trust.
    return json.dumps({"name": x["name"], "description": x.get("purpose", ""), "version": "1.0.0",
                       "remotes": [{"type": "streamable-http", "url": "https://<host>:<port>/mcp"}],
                       "_meta": {"com.magertron/czarter": {
                           "czarter": c["metadata"]["name"], "namespace": ns_of(c, x),
                           "forwardIdentity": bool(x.get("forwardIdentity")),
                           "fingerprint": fingerprint(c)}}}, indent=2) + "\n"


def env_example(c):
    m, idn = c["metadata"], c["identity"]
    L = ["# Copy to .env. References only - never the secret values.",
         f"MAGERTRON_URL={GATEWAY}", f"MAGERTRON_NAMESPACE={m['namespace']}",
         f"STS_URL={GATEWAY}/api/v1/token/exchange",
         f"IDP_PROVIDER={idn['idp']['provider']}", f"IDP_DOMAIN={idn['idp']['domain']}",
         # The issuer the app discovers OIDC from: Keycloak's domain IS its realm's issuer URL.
         "IDP_ISSUER=" + (idn["idp"]["domain"].rstrip("/") if idn["idp"]["provider"] == "keycloak"
                          else f"https://{idn['idp']['domain']}/"),
         f"IDP_AUDIENCE={idn['idp']['audience']}",
         f"IDP_CLIENT_ID={idn['app']['clientId']}", f"IDP_CLIENT_SECRET_REF={idn['app'].get('secretRef', '')}",
         f"IDP_CALLBACK_URL={(idn['app'].get('callbackUrls') or [''])[0]}"]
    if str(idn["app"].get("secretRef", "")).startswith("vault:"):
        # The Vault this install uses - the address its certificate names - and where its CA file is on
        # your machine. Never skip TLS verification.
        L += [f"VAULT_ADDR={os.environ.get('VAULT_ADDR', '')}", "VAULT_CACERT=  # path to the Vault CA certificate (ask the admin)"]
    for x in c["connections"]:
        key = x["name"].upper().replace("-", "_")
        L.append(f"{key}_URL=" + (f"{GATEWAY}/api/v1/llm/{ns_of(c, x)}/{x['name']}/v1/chat/completions" if x.get("kind") == "llm" else mcp_url(c, x)))
    return "\n".join(L) + "\n"


def readme(c, log, open_steps, stamp=None):
    m = c["metadata"]
    L = [f"# {m['name']}", "", m.get("description") or (c.get("functionality") or {}).get("summary", ""), "",
         f"Handed over from the Magertron™ Czarter `{m['name']}` for {m['tenant']}.", "",
         "| Start here | |", "|---|---|", "| Build brief and guardrails | `AGENTS.md` |",
         f"| Spec, plan, tasks | `specs/001-{m['name']}/` |", "| Platform status | `RUNBOOK.md` |",
         "| Source of truth | `czarter.yaml` |", "| Policy | `policy/` - the admin installs it with `kubectl apply -f policy/<name>-configmap.yaml` |"]
    if missing_servers(c, log):
        L.append("| Servers to build | `servers/` (SERVER-SPEC.md and server.json) |")
        L += ["", "**Before you start:** ask the customer's Magertron admin for a service-account token to submit your "
              "server(s), and for the app's IdP client secret reference. You never need an admin login."]
    if stamp:
        L += ["", f"**Pack version {stamp['version']}** - {stamp['date']}, Czarter fingerprint `{stamp['hash']}`. "
              "If the Czarter owner sends a newer version, work from that one.", "",
              f"Open platform steps: **{len(open_steps)}** (Phase 2 of `specs/001-{m['name']}/tasks.md`). "
              "Done = every task ticked and Verify all green in Czartup against the current Czarter.", "",
              "Put this folder in your own repository and use your own tools: `AGENTS.md` for coding agents, "
              "`specs/` for Spec Kit (`/speckit.implement`), `servers/` for the servers to build.", "",
              "**With a coding agent:** open this folder and say **\"Build\"**. `AGENTS.md` (*Start here*) sets the order "
              "and stops at each step that needs the Magertron admin.", ""]
    else:
        L += ["", f"Open platform steps: **{len(open_steps)}** (one issue each). Done = all issues closed and Verify all green.", ""]
    return "\n".join(L)


def open_steps_of(c, log):
    return [s for s in runbook.steps(c) if log.get(s[1], {}).get("status") not in OK]


def build(c, runs, stamp=None):
    """path -> text for the whole pack."""
    original, c = c, runbook.with_people(c)   # generate from a copy; the pack's czarter.yaml is the original
    log = runbook.latest(runs)
    m, op = c["metadata"], open_steps_of(c, runbook.latest(runs))
    try:
        import apply
        rego, cmap = apply.rego(c), apply.configmap(c)
    except Exception as e:  # noqa: BLE001
        rego = cmap = f"# policy could not be generated: {e}\n"
    files = {
        "README.md": readme(c, log, op, stamp),
        "AGENTS.md": agents_md(c, log),
        "CLAUDE.md": "# Claude Code\n\n@AGENTS.md\n",
        **({"reference/approvals.py": APPROVALS_REFERENCE} if (c.get("controls") or {}).get("holds") else {}),
        ".specify/memory/constitution.md": constitution(c),
        f"specs/001-{m['name']}/spec.md": spec_md(c),
        f"specs/001-{m['name']}/plan.md": plan_md(c),
        f"specs/001-{m['name']}/tasks.md": tasks_md(c, log, op),
        "RUNBOOK.md": runbook.render(c, runs),
        f"policy/{m['name']}.rego": rego,
        f"policy/{m['name']}-configmap.yaml": cmap,
        "czarter.yaml": yaml.safe_dump(original, sort_keys=False, allow_unicode=True),
        ".env.example": env_example(c),
        ".gitignore": ".env\n*.secret\n*.key\n*.pem\n__pycache__/\nnode_modules/\n",
    }
    for x in missing_servers(c, log):
        files[f"servers/{x['name']}/SERVER-SPEC.md"] = server_spec(c, x)
        files[f"servers/{x['name']}/server.json"] = server_json(c, x)
        files[f"deploy/systemd/{x['name']}.service"] = systemd_unit(
            f"{x['name']} - MCP server for {m['name']}", f"servers/{x['name']}", "python3 server.py")
    files[f"deploy/systemd/{m['name']}-app.service"] = systemd_unit(
        f"{m['name']} app", ".", "<the command that starts the app>", env=".env")
    files["deploy/systemd/README.md"] = systemd_readme(c, [x["name"] for x in missing_servers(c, log)])
    if stamp:
        files["HANDOVER-VERSION"] = (f"czarter: {m['name']}\nversion: {stamp['version']}\ndate: {stamp['date']}\n"
                                     f"fingerprint: {stamp['hash']}\nopen_steps: {len(op)}\n")
    return files, op


def fingerprint(c, open_ids=()):
    """Short hash of the Czarter and of which steps are still open: the same pack content
    always gives the same fingerprint, a changed Czarter or a newly finished step a new one."""
    import hashlib
    return hashlib.sha256(json.dumps([c, sorted(open_ids)], sort_keys=True, default=str).encode()).hexdigest()[:8]


def cmd_zip(c, runs, out_dir):
    """A versioned zip for an SI who uses their own repository and tools.
    The version goes up only when the Czarter itself changed since the last zip."""
    import zipfile
    m = c["metadata"]
    h = fingerprint(c, [sid for _, sid, _, _ in open_steps_of(c, runbook.latest(runs))])
    prev = [r for r in runs if r.get("step") == "handover.zip"]
    last = prev[-1].get("detail", "") if prev else ""
    try:
        n = int(last.split()[0].lstrip("v")) if last else 0
    except ValueError:
        n = len(prev)
    version = n if (prev and f"· {h} ·" in last) else n + 1
    stamp = {"version": f"v{version}", "date": datetime.date.today().isoformat(), "hash": h}
    files, op = build(c, runs, stamp)
    out_dir = pathlib.Path(out_dir).expanduser(); out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{m['name']}-handover-{stamp['date']}-v{version}.zip"
    root = f"{m['name']}-handover-v{version}"
    with zipfile.ZipFile(out_dir / fname, "w", zipfile.ZIP_DEFLATED) as z:
        for p, text in sorted(files.items()):
            z.writestr(f"{root}/{p}", text)
    print(f"✅ handover.zip: {fname} - {len(files)} files, version v{version} (fingerprint {h}), {len(op)} open step(s)")
    print(f"file: {out_dir / fname}")
    return [("handover.zip", "done", f"v{version} · {h} · {fname}")]


def cmd_pack(c, runs, out):
    files, op = build(c, runs)
    out = pathlib.Path(out).expanduser()
    for p, text in files.items():
        f = out / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    print(f"✅ handover.pack: {len(files)} files in {out} ({len(op)} open platform step(s))")
    for p in sorted(files):
        print(f"   {p}")
    return files


# ---------- Gitea ----------

class Gitea:
    def __init__(self, base, token):
        self.base, self.token = base, token

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + "/api/v1" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "token " + self.token, "Accept": "application/json",
                                              "Content-Type": "application/json", "User-Agent": "magertron-czartup/1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"message": raw[:300]}

    def all(self, path):
        out, page = [], 1
        while True:
            st, j = self.call("GET", f"{path}{'&' if '?' in path else '?'}limit=50&page={page}")
            if st != 200 or not j:
                return out
            out += j
            if len(j) < 50:
                return out
            page += 1


def gitea_token():
    if os.environ.get("CZARTUP_GITEA_TOKEN_VALUE"):          # tests only
        return os.environ["CZARTUP_GITEA_TOKEN_VALUE"]
    import idp_auth0
    path, key = idp_auth0._split(TOKEN_REF)
    data = idp_auth0.vault_read(path)
    tok = (data.get(key or "token") or "").strip()
    if not tok:
        raise SystemExit(f"❌ handover.push: no Gitea token at {TOKEN_REF} - store one with write:repository and write:issue "
                         f"(and write:user to let Czartup create the repo):\n   vault kv put {path} {key or 'token'}=-")
    return tok


def cmd_push_gitea(c, runs, yes):
    files, op = build(c, runs)
    m = c["metadata"]
    g = Gitea(GITEA, gitea_token())
    st, me = g.call("GET", "/user")
    if st != 200:
        return "failed", f"Gitea refused the token (HTTP {st}): {(me or {}).get('message', '')}", []
    org, repo = me["login"], m["name"]
    st, info = g.call("GET", f"/repos/{org}/{repo}")
    exists = st == 200
    if exists and not info.get("private"):
        return "failed", f"{org}/{repo} exists and is PUBLIC - not pushing a handover pack there", []
    # what changes
    changes = []
    for p, text in files.items():
        sha = None
        if exists and not info.get("empty"):
            s2, cur = g.call("GET", f"/repos/{org}/{repo}/contents/{urllib.request.quote(p)}")
            if s2 == 200 and isinstance(cur, dict):
                if base64.b64decode(cur.get("content") or "").decode("utf-8", "replace") == text:
                    continue
                sha = cur.get("sha")
        changes.append((p, text, sha))
    # A server that is deployed now no longer needs its SERVER-SPEC: remove it from the repo.
    if exists and not info.get("empty"):
        for x in c["connections"]:
            for p in (f"servers/{x['name']}/SERVER-SPEC.md", f"servers/{x['name']}/server.json"):
                if p in files:
                    continue
                s2, cur = g.call("GET", f"/repos/{org}/{repo}/contents/{urllib.request.quote(p)}")
                if s2 == 200 and isinstance(cur, dict) and cur.get("sha"):
                    changes.append((p, None, cur["sha"]))
    issues = g.all(f"/repos/{org}/{repo}/issues?state=all&type=issues") if exists else []
    by_step = {}
    for i in issues:
        t = i.get("title", "")
        if t.startswith("[") and "]" in t:
            by_step.setdefault(t[1:t.index("]")], i)
    open_ids = {sid for _, sid, _, _ in op}
    to_open = [(sec, sid, title, how) for sec, sid, title, how in op
               if sid not in by_step or by_step[sid].get("state") == "closed"]
    to_close = [i for sid, i in by_step.items() if i.get("state") == "open" and sid not in open_ids]

    if exists and not changes and not to_open and not to_close:
        return "done", f"{GITEA}/{org}/{repo} is up to date - nothing to do", []
    if not yes:
        print(f"would {'update' if exists else 'create'} PRIVATE repo {GITEA}/{org}/{repo}")
        print(f"would commit {len(changes)} file(s)" + (":" if changes else " - pack unchanged"))
        for p, text, sha in changes:
            print(f"   {'delete' if text is None else 'update' if sha else 'add   '} {p}")
        print(f"would open {len(to_open)} issue(s), one per open platform step:")
        for sec, sid, title, _ in to_open:
            print(f"   [{sid}] {title}   label: {TRADE.get(sec, ('platform',))[0]}")
        if to_close:
            print(f"would close {len(to_close)} issue(s) whose step is now done:")
            for i in to_close:
                print(f"   #{i['number']} {i['title']}")
        return None, "dry run - click Make the change", []

    rows = []
    if not exists:
        st, info = g.call("POST", "/user/repos", {"name": repo, "private": True, "default_branch": "main",
                                                   "description": f"Handover from the Magertron Czarter '{repo}' ({m['tenant']})"})
        if st not in (200, 201):
            msg = (info or {}).get("message", "")
            hint = (f" Create it by hand in Gitea (private, name {repo}, not initialized) and run this again."
                    if st == 403 else "")
            return "failed", f"could not create {org}/{repo} (HTTP {st}): {msg}.{hint}", []
        rows.append(("handover.repo", "done", f"created private repo {org}/{repo}"))
    if changes:
        body = {"message": f"Handover pack from Czartup ({datetime.date.today().isoformat()})",
                "files": [{"operation": "delete", "path": p, "sha": sha} if text is None else
                          dict(operation="update" if sha else "create", path=p,
                               content=base64.b64encode(text.encode()).decode(), **({"sha": sha} if sha else {}))
                          for p, text, sha in changes]}
        # One commit on the default branch; Gitea also accepts this on an empty repo (first commit).
        body["branch"] = (info or {}).get("default_branch") or "main"
        st, out = g.call("POST", f"/repos/{org}/{repo}/contents", body)
        if st not in (200, 201):
            return "failed", f"commit refused (HTTP {st}): {(out or {}).get('message', '')}", rows
    # labels
    have = {l["name"]: l["id"] for l in g.all(f"/repos/{org}/{repo}/labels")}
    for name, color in list(TRADE.values()) + [("platform", "c5def5")]:
        if name not in have:
            s3, lab = g.call("POST", f"/repos/{org}/{repo}/labels", {"name": name, "color": "#" + color})
            if s3 in (200, 201):
                have[name] = lab["id"]
    opened, reopened, closed, bad = 0, 0, 0, []
    for sec, sid, title, how in to_open:
        text = (f"**Owner: {owner(c, runbook.latest(runs), sec, sid)}**\n\n{how}\n\n---\nRunbook step `{sid}` ({sec}). "
                "Close this when Verify in Czartup shows the step green.\n"
                "Handed over by Magertron Czartup.")
        lab = have.get(TRADE.get(sec, ("platform",))[0])
        old = by_step.get(sid)
        if old:
            s4, _ = g.call("PATCH", f"/repos/{org}/{repo}/issues/{old['number']}", {"state": "open"})
            (bad.append(sid) if s4 not in (200, 201) else None); reopened += s4 in (200, 201)
            continue
        s4, _ = g.call("POST", f"/repos/{org}/{repo}/issues", {"title": f"[{sid}] {title}", "body": text,
                                                                 "labels": [lab] if lab else []})
        (bad.append(sid) if s4 not in (200, 201) else None); opened += s4 in (200, 201)
    for i in to_close:
        g.call("POST", f"/repos/{org}/{repo}/issues/{i['number']}/comments",
               {"body": "Czartup: this step is now done in the runbook - closing."})
        s5, _ = g.call("PATCH", f"/repos/{org}/{repo}/issues/{i['number']}", {"state": "closed"})
        closed += s5 in (200, 201)
    url = f"{GITEA}/{org}/{repo}"
    detail = (f"{url}: {len(changes)} file(s) committed, {opened} issue(s) opened"
              + (f", {reopened} reopened" if reopened else "") + (f", {closed} closed" if closed else "")
              + (f"; could not file: {', '.join(bad)}" if bad else ""))
    rows.append(("handover.push", "failed" if bad else "done", detail))
    return ("failed" if bad else "done"), detail, rows


# ---------- GitHub (0.2.96) ----------
# The build kit goes on a branch, czartup/<app>, as ONE commit, with a pull request into the
# default branch - never straight to main, never a force-push. Open runbook steps become issues,
# labelled by trade, and close themselves when the step is done (as on Gitea).

class GitHub:
    def __init__(self, api, token):
        self.api, self.token = api, token

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.api + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "Content-Type": "application/json", "User-Agent": "magertron-czartup/1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw), dict(e.headers)
            except ValueError:
                return e.code, {"message": raw[:300]}, dict(e.headers)

    def all(self, path):
        out, page = [], 1
        while page <= 20:
            st, j, _ = self.call("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if st != 200 or not j:
                return out
            out += j
            if len(j) < 100:
                return out
            page += 1
        return out


def github_token():
    if os.environ.get("CZARTUP_GITHUB_TOKEN_VALUE"):          # tests only
        return os.environ["CZARTUP_GITHUB_TOKEN_VALUE"]
    import idp_auth0
    path, key = idp_auth0._split(GITHUB_TOKEN_REF)
    data = idp_auth0.vault_read(path)
    tok = (data.get(key or "token") or "").strip()
    if not tok:
        raise SystemExit(f"❌ handover.push: no GitHub token at {GITHUB_TOKEN_REF}. Make a fine-grained token with "
                         "Contents, Pull requests and Issues (read and write) and Administration (write, to create the "
                         f"repo), then store it:\n   vault kv put {path} {key or 'token'}=-")
    return tok


def _git_blob_sha(text):
    import hashlib
    b = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def cmd_push_github(c, runs, yes):
    files, op = build(c, runs)
    m = c["metadata"]
    g = GitHub(GITHUB_API, github_token())
    st, me, hdr = g.call("GET", "/user")
    if st != 200:
        return "failed", f"GitHub refused the token (HTTP {st}): {(me or {}).get('message', '')}", []
    org, repo = GITHUB_OWNER or me["login"], m["name"]
    branch = f"czartup/{repo}"
    web = f"{GITHUB_WEB}/{org}/{repo}"
    st, info, _ = g.call("GET", f"/repos/{org}/{repo}")
    exists = st == 200
    if exists and not info.get("private"):
        return "failed", f"{org}/{repo} exists and is PUBLIC - not pushing a build kit there", []

    # What the branch holds now (or main, before the first push): one tree read, compared by blob hash.
    base = (info or {}).get("default_branch") or "main"
    parent = tree_sha = None
    have = {}
    branch_exists = False
    if exists:
        s1, ref, _ = g.call("GET", f"/repos/{org}/{repo}/git/ref/heads/{branch}")
        branch_exists = s1 == 200
        if not branch_exists:
            s1, ref, _ = g.call("GET", f"/repos/{org}/{repo}/git/ref/heads/{base}")
        if s1 == 200:
            parent = ref["object"]["sha"]
            s2, cm, _ = g.call("GET", f"/repos/{org}/{repo}/git/commits/{parent}")
            tree_sha = (cm or {}).get("tree", {}).get("sha")
            s3, tr, _ = g.call("GET", f"/repos/{org}/{repo}/git/trees/{tree_sha}?recursive=1")
            have = {e["path"]: e["sha"] for e in (tr or {}).get("tree", []) if e.get("type") == "blob"}
    changes = [(p, text) for p, text in files.items() if have.get(p) != _git_blob_sha(text)]
    # A server that is deployed now no longer needs its SERVER-SPEC: remove it from the branch.
    for x in c["connections"]:
        for p in (f"servers/{x['name']}/SERVER-SPEC.md", f"servers/{x['name']}/server.json"):
            if p not in files and p in have:
                changes.append((p, None))

    issues = [i for i in g.all(f"/repos/{org}/{repo}/issues?state=all") if "pull_request" not in i] if exists else []
    by_step = {}
    for i in issues:
        t = i.get("title", "")
        if t.startswith("[") and "]" in t:
            by_step.setdefault(t[1:t.index("]")], i)
    open_ids = {sid for _, sid, _, _ in op}
    to_open = [(sec, sid, title, how) for sec, sid, title, how in op
               if sid not in by_step or by_step[sid].get("state") == "closed"]
    to_close = [i for sid, i in by_step.items() if i.get("state") == "open" and sid not in open_ids]

    if exists and branch_exists and not changes and not to_open and not to_close:
        return "done", f"{web} (branch {branch}) is up to date - nothing to do", []
    if not yes:
        print(f"would {'update' if exists else 'create'} PRIVATE repo {web}")
        print(f"would commit {len(changes)} file(s) to branch {branch} and open a pull request into {base}"
              + (":" if changes else " - kit unchanged"))
        for p, text in changes:
            print(f"   {'delete' if text is None else 'update' if p in have else 'add   '} {p}")
        print(f"would open {len(to_open)} issue(s), one per open platform step:")
        for sec, sid, title, _ in to_open:
            print(f"   [{sid}] {title}   label: {TRADE.get(sec, ('platform',))[0]}")
        if to_close:
            print(f"would close {len(to_close)} issue(s) whose step is now done:")
            for i in to_close:
                print(f"   #{i['number']} {i['title']}")
        return None, "dry run - click Make the change", []

    rows = []
    if not exists:
        body = {"name": repo, "private": True, "auto_init": True,
                "description": f"Build kit from the Magertron Czarter '{repo}' ({m.get('tenant', '')})"}
        st, info, _ = g.call("POST", "/user/repos" if org == me["login"] else f"/orgs/{org}/repos", body)
        if st not in (200, 201):
            return "failed", (f"could not create {org}/{repo} (HTTP {st}): {(info or {}).get('message', '')}. "
                              "Create it by hand (private), or give the token Administration: write."), []
        rows.append(("handover.repo", "done", f"created private repo {org}/{repo}"))
        base = info.get("default_branch") or "main"
        s1, ref, _ = g.call("GET", f"/repos/{org}/{repo}/git/ref/heads/{base}")
        if s1 != 200:
            return "failed", f"new repo {org}/{repo} has no {base} branch yet (HTTP {s1}) - push again in a minute", rows
        parent = ref["object"]["sha"]
        s2, cm, _ = g.call("GET", f"/repos/{org}/{repo}/git/commits/{parent}")
        tree_sha = cm["tree"]["sha"]
        changes = [(p, text) for p, text in files.items()]

    if changes:
        tree = [{"path": p, "mode": "100644", "type": "blob", "sha": None} if text is None else
                {"path": p, "mode": "100644", "type": "blob", "content": text} for p, text in changes]
        st, nt, _ = g.call("POST", f"/repos/{org}/{repo}/git/trees", {"base_tree": tree_sha, "tree": tree})
        if st not in (200, 201):
            return "failed", f"GitHub refused the files (HTTP {st}): {(nt or {}).get('message', '')}", rows
        st, cm, _ = g.call("POST", f"/repos/{org}/{repo}/git/commits",
                           {"message": f"Build kit from Czartup ({datetime.date.today().isoformat()})",
                            "tree": nt["sha"], "parents": [parent]})
        if st not in (200, 201):
            return "failed", f"GitHub refused the commit (HTTP {st}): {(cm or {}).get('message', '')}", rows
        if branch_exists:
            st, out, _ = g.call("PATCH", f"/repos/{org}/{repo}/git/refs/heads/{branch}", {"sha": cm["sha"], "force": False})
        else:
            st, out, _ = g.call("POST", f"/repos/{org}/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": cm["sha"]})
        if st not in (200, 201):
            return "failed", f"could not move branch {branch} (HTTP {st}): {(out or {}).get('message', '')}", rows

    # The pull request: one per app, reused on every push.
    pr_url = ""
    st, prs, _ = g.call("GET", f"/repos/{org}/{repo}/pulls?state=open&head={org}:{branch}")
    if st == 200 and prs:
        pr_url = prs[0].get("html_url", "")
    elif changes:
        st, pr, _ = g.call("POST", f"/repos/{org}/{repo}/pulls",
                           {"title": f"Czartup build kit: {repo}", "head": branch, "base": base,
                            "body": "The build kit for this app, from Magertron Czartup. Review it, merge it, and build "
                                    "what AGENTS.md says. Open platform steps are filed as issues."})
        if st in (200, 201):
            pr_url = pr.get("html_url", "")
        else:
            rows.append(("handover.pr", "failed", f"could not open the pull request (HTTP {st}): {(pr or {}).get('message', '')}"))

    # Labels and issues, as on Gitea.
    labels = {l["name"] for l in g.all(f"/repos/{org}/{repo}/labels")}
    for name, color in list(TRADE.values()) + [("platform", "c5def5")]:
        if name not in labels:
            s3, _, _ = g.call("POST", f"/repos/{org}/{repo}/labels", {"name": name, "color": color})
            if s3 in (200, 201):
                labels.add(name)
    opened, reopened, closed, bad = 0, 0, 0, []
    for sec, sid, title, how in to_open:
        text = (f"**Owner: {owner(c, runbook.latest(runs), sec, sid)}**\n\n{how}\n\n---\nRunbook step `{sid}` ({sec}). "
                "Close this when Verify in Czartup shows the step green.\nHanded over by Magertron Czartup.")
        lab = TRADE.get(sec, ("platform",))[0]
        old = by_step.get(sid)
        if old:
            s4, _, _ = g.call("PATCH", f"/repos/{org}/{repo}/issues/{old['number']}", {"state": "open"})
            (bad.append(sid) if s4 not in (200, 201) else None); reopened += s4 in (200, 201)
            continue
        s4, _, _ = g.call("POST", f"/repos/{org}/{repo}/issues", {"title": f"[{sid}] {title}", "body": text,
                                                                  "labels": [lab] if lab in labels else []})
        (bad.append(sid) if s4 not in (200, 201) else None); opened += s4 in (200, 201)
    for i in to_close:
        g.call("POST", f"/repos/{org}/{repo}/issues/{i['number']}/comments",
               {"body": "Czartup: this step is now done in the runbook - closing."})
        s5, _, _ = g.call("PATCH", f"/repos/{org}/{repo}/issues/{i['number']}", {"state": "closed"})
        closed += s5 in (200, 201)
    detail = (f"{pr_url or web}: {len(changes)} file(s) on {branch}, {opened} issue(s) opened"
              + (f", {reopened} reopened" if reopened else "") + (f", {closed} closed" if closed else "")
              + (f"; could not file: {', '.join(bad)}" if bad else ""))
    failed = bad or any(r[1] == "failed" for r in rows)
    rows.append(("handover.push", "failed" if failed else "done", detail))
    return ("failed" if failed else "done"), detail, rows


def cmd_check():
    """Test connection: can the token reach the owner, and create a repo, branch, PR and issues there?"""
    if REPO != "github":
        g = Gitea(GITEA, gitea_token())
        st, me = g.call("GET", "/user")
        return ("done", f"Gitea {GITEA}: signed in as {me['login']}") if st == 200 else \
               ("failed", f"Gitea {GITEA} refused the token (HTTP {st})")
    g = GitHub(GITHUB_API, github_token())
    st, me, hdr = g.call("GET", "/user")
    if st != 200:
        return "failed", f"GitHub refused the token (HTTP {st}): {(me or {}).get('message', '')}"
    org = GITHUB_OWNER or me["login"]
    lines = [f"signed in to GitHub as {me['login']}; build kits go to {GITHUB_WEB}/{org}/<app>"]
    scopes = hdr.get("X-OAuth-Scopes") or hdr.get("x-oauth-scopes")
    if scopes is not None and scopes.strip():          # a classic token says its scopes; fine-grained ones do not
        if "repo" not in [x.strip() for x in scopes.split(",")]:
            return "failed", lines[0] + f" - but the token lacks the repo scope (has: {scopes})"
    if org != me["login"]:
        s2, mem, _ = g.call("GET", f"/orgs/{org}/memberships/{me['login']}")
        if s2 != 200:
            return "failed", lines[0] + f" - but {me['login']} is not a member of {org} (HTTP {s2})"
        if (mem or {}).get("role") != "admin":
            lines.append(f"not an owner of {org}: create each app's repo there by hand, or ask an owner to allow it")
    return "done", "; ".join(lines)


def cmd_push(c, runs, yes):
    return cmd_push_github(c, runs, yes) if REPO == "github" else cmd_push_gitea(c, runs, yes)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    for n in ("pack", "push", "zip"):
        p = sub.add_parser(n)
        p.add_argument("czarter")
        p.add_argument("--runs")
        p.add_argument("--record", metavar="RUNS_JSON")
        if n == "pack":
            p.add_argument("--out", required=True)
        elif n == "zip":
            p.add_argument("--out-dir", required=True)
        else:
            p.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if a.cmd == "check":
        st, detail = cmd_check()
        print(f"{'✅' if st == 'done' else '❌'} repo.check: {detail}")
        sys.exit(0 if st == "done" else 1)
    c = yaml.safe_load(pathlib.Path(a.czarter).expanduser().read_text())
    runs = json.loads(pathlib.Path(a.runs).read_text()) if a.runs and pathlib.Path(a.runs).exists() else []
    if a.cmd == "pack":
        cmd_pack(c, runs, a.out)
        rows = [("handover.pack", "done", f"pack written to {a.out}")]
        st = "done"
    elif a.cmd == "zip":
        rows, st = cmd_zip(c, runs, a.out_dir), "done"
    else:
        st, detail, rows = cmd_push(c, runs, a.yes)
        print(f"{ {'done': '✅', 'failed': '❌'}.get(st, '·') } handover.push: {detail}")
    if st and a.record and rows:
        import apply
        apply.record(rows, a.record, by="czartup-handover")
    sys.exit(1 if st == "failed" else 0)


if __name__ == "__main__":
    main()
