#!/usr/bin/env python3
"""Czarter apply: plan, verify (read-only) and apply (one step, dry run by default).

  plan    Generate what the Czarter needs into generated/<name>/:
            policy.rego    the app's own policy module (OPA 1.0): limits, holds, the agent alone
            <name>-configmap.yaml  that module as the ConfigMap the administrator applies
            verify.sql     read-only queries for the STS mappings
            manual.md      the steps a person still does by hand (IdP, server registration)
  verify  Check the live setup, read-only, and print passed/failed for each step:
            policy         the app's module is in OPA and current, and OPA really refuses / holds
                           (live decisions when CZARTUP_OPA_URL is set; else the --policy file)
            STS mappings   users.external_id rows exist (psql through kubectl, like pg())
            --api URL      also: each connection is registered (GET /api/v1/servers/{ns}/{name})
                           and the agent's service account is live (GET /api/v1/service-accounts),
                           the same routes seeding.cpp uses. Operator JWT from --token-file.
          --record adds the results to the run log, so runbook.py shows them.

  python3 apply.py plan   examples/tidewater.czarter.yaml
  python3 apply.py verify examples/tidewater.czarter.yaml
  python3 apply.py verify examples/tidewater.czarter.yaml --record docs/legacy/daytrader.runs.json

  apply   Make ONE change, through the same API the console uses. Dry run unless --yes.
            --step sts.agent   create the agent's service account with its external ID
                               (POST /api/v1/service-accounts); skips it if it already exists
            --step grants      grant the agent's first role exactly the tools each connection lists
                               (POST /api/v1/tool-rbac/servers/{ns}/{name}/permissions); 409 = already there

  python3 apply.py apply examples/tidewater.czarter.yaml --step sts.agent --api https://app.magertron.com
  python3 apply.py apply examples/tidewater.czarter.yaml --step sts.agent --api https://app.magertron.com --yes

Checks that send real calls (a held order, a denied order) are not run here.
"""
import time
import argparse, datetime, json, os, pathlib, re, subprocess, sys
import yaml
from jsonschema import Draft202012Validator

HERE = pathlib.Path(__file__).resolve().parent
# Without a live OPA (CZARTUP_OPA_URL), Verify reads the app's module from this file instead
# (its .rego or -configmap.yaml).
POLICY = os.environ.get("CZARTUP_POLICY", "policy.rego")
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
VIOLATES = {"<=": ">", "<": ">=", ">=": "<", ">": "<=", "==": "!="}


TODO = "<to fill in>"


def cns(c, x):
    """The namespace a connection's server lives in."""
    return x.get("namespace") or c["metadata"]["namespace"]


WHOAMI = "whoami"


def tools_of(x):
    """The tools a connection needs. A Trusted upstream also exposes `whoami` (returns the
    identity headers it received), so Verify and the SI can prove identity reaches it."""
    t = list(x.get("tools", []))
    if x.get("forwardIdentity") and WHOAMI not in t and "*" not in t:
        t.append(WHOAMI)
    return t


def draft_fill(c):
    """Placeholders for what an unfinished Czarter is missing, so plan can still write
    the manual steps (that is how you find out what to create in the IdP)."""
    def need(path, default):
        node = c
        ks = path.split(".")
        for k in ks[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        if node.get(ks[-1]) in (None, "", []):
            node[ks[-1]] = default
    for path, default in [
        ("metadata.name", "draft"), ("metadata.namespace", TODO),
        ("identity.idp.provider", "auth0"), ("identity.idp.domain", TODO), ("identity.idp.audience", TODO),
        ("identity.people.mfa.required", False),
        ("identity.app.type", "regular-web"), ("identity.app.clientId", TODO), ("identity.app.grantTypes", [TODO]),
        ("identity.app.callbackUrls", [TODO]), ("identity.app.secretRef", TODO),
        ("identity.sts.agent.serviceAccount", TODO), ("identity.sts.agent.externalId", TODO),
        ("identity.sts.agent.roles", [TODO]),
    ]:
        need(path, default)
    c["identity"]["sts"].setdefault("people", [])
    for p_ in c["identity"]["sts"]["people"]:
        p_.setdefault("idpSubject", TODO); p_.setdefault("user", TODO)
    c.setdefault("connections", [])
    for x in c["connections"]:
        x.setdefault("name", TODO); x.setdefault("kind", "mcp"); x.setdefault("purpose", TODO)
    c.setdefault("controls", {})
    for h in c["controls"].get("holds", []):
        h.setdefault("when", {"argPresent": TODO}); h.setdefault("reason", TODO); h.setdefault("ttlSeconds", 300)
        h.setdefault("server", TODO); h.setdefault("tool", TODO); h.setdefault("id", TODO)
    for l in c["controls"].get("limits", []):
        l.setdefault("id", TODO); l.setdefault("description", TODO); l.setdefault("server", TODO); l.setdefault("tool", TODO)
        if "field" not in l and "policyRef" not in l:
            l["policyRef"] = TODO
    return c


def load(path, draft=False):
    c = yaml.safe_load(pathlib.Path(path).expanduser().read_text())
    schema = json.loads((HERE / "schema" / "czarter.schema.json").read_text())
    errs = list(Draft202012Validator(schema).iter_errors(c))
    if errs and draft:
        print(f"draft: {len(errs)} thing(s) still to fill in - shown as {TODO} in the output")
        return draft_fill(c)
    if errs:
        for e in errs:
            print(f"{'/'.join(map(str, e.path)) or '(top)'}: {e.message}", file=sys.stderr)
        sys.exit("Czarter is not valid - run validate.py")
    for p_ in c["identity"]["sts"].get("people") or []:     # given by email, not linked yet
        p_.setdefault("user", UNLINKED); p_.setdefault("idpSubject", UNLINKED)
    return c


UNLINKED = "<link by email>"   # People's access fills it in from the person's email


def rid(*parts):
    return "czarter_" + "_".join(re.sub(r"[^a-z0-9]+", "_", p.lower()).strip("_") for p in parts)


def q(v):
    return json.dumps(v)


# ---------- plan ----------

OPA_NS = os.environ.get("CZARTUP_OPA_NAMESPACE", "mcp-system")   # where kube-mgmt looks for app policies
OPA_URL = os.environ.get("CZARTUP_OPA_URL", "")                   # live OPA, for Verify (e.g. http://mcp-orchestrator-opa:8181)


def pkg(c):
    """The app's own Rego package: magertron.apps.<name>. Underscores only - a valid identifier."""
    return "magertron.apps." + re.sub(r"[^a-z0-9]+", "_", c["metadata"]["name"].lower()).strip("_")


def cm_name(c):
    return f"czarter-{c['metadata']['name']}-policy"


def app_namespaces(c):
    """Every namespace this app's rules may act in: its own and each connection's.
    The base policy only counts this app's rules for calls in these namespaces."""
    return sorted({c["metadata"]["namespace"]} | {cns(c, x) for x in c.get("connections", [])})


def rego(c):
    """The app's policy as its OWN module (package magertron.apps.<name>), loaded into OPA from its own
    ConfigMap. The base policy (the chart's magertron-apps.rego) collects deny / hold_deny / hold_request
    from every app, and only for calls in that app's namespaces - so this module cannot override the
    decision, touch another app's rules, or reach outside its namespaces."""
    name, ctl = c["metadata"]["name"], c.get("controls", {})
    out = [f"# Generated by Czartup from the Czarter '{name}'. Do not edit by hand - edit the Czarter and",
           f"# get the hand-over pack again. Installed as the ConfigMap {cm_name(c)} (kubectl apply -f).",
           f"package {pkg(c)}", "",
           "# The base policy counts this app's rules only for calls in these namespaces.",
           f"namespaces := {q(app_namespaces(c))}", ""]

    for l in ctl.get("limits", []):
        if "field" not in l:
            out.append(f"# limit '{l['id']}' is enforced by existing policy: {l['policyRef']}")
            continue
        f, op, v = l["field"], l["op"], l["value"]
        if op in VIOLATES:
            bad = f"input.arguments.{f} {VIOLATES[op]} {q(v)}"
        elif op == "in":
            bad = f"not input.arguments.{f} in {q(v)}"
        else:  # not-in
            bad = f"input.arguments.{f} in {q(v)}"
        out += [f"deny contains {q(limit_msg(l))} if {{",
                f"\tinput.namespace == {q(server_ns(c, l['server']))}",
                f"\tinput.server == {q(l['server'])}",
                f"\tinput.tool == {q(l['tool'])}",
                f"\t{bad}",
                "}", ""]

    holds = ctl.get("holds", [])
    for h in holds:
        r = hold_rule(h)
        conds = [f"input.namespace == {q(server_ns(c, h['server']))}",
                 f"input.server == {q(h['server'])}", f"input.tool == {q(h['tool'])}"]
        if "argPresent" in h["when"]:
            a = h["when"]["argPresent"]
            conds += [f"is_string(input.arguments.{a})", f"input.arguments.{a} != \"\""]
        for k, v in h["when"].get("argEquals", {}).items():
            conds.append(f"input.arguments.{k} == {q(v)}")
        for k, v in h["when"].get("argAbove", {}).items():
            conds += [f"is_number(input.arguments.{k})", f"input.arguments.{k} > {q(v)}"]
        out += [f"{r} if {{"] + [f"\t{x}" for x in conds] + ["}", "",
                f"deny contains {q(h['reason'])} if {r}",
                f"hold_deny contains {q(h['reason'])} if {r}", ""]
    out += agent_alone_rego(c)
    # hold_request has one value per decision: chain the holds with else, first match wins.
    for i, h in enumerate(holds):
        body = q({"reason": h["reason"], "approvers": h.get("approvers", []), "ttl_seconds": h["ttlSeconds"]})
        out.append(("hold_request := " if i == 0 else "else := ") + body + f" if {hold_rule(h)}")
    return "\n".join(out).rstrip() + "\n"


def limit_msg(l):
    return f"{l['description']} ({l['field']} {l['op']} {json.dumps(l['value'])})"


def hold_rule(h):
    return "hold_" + re.sub(r"[^a-z0-9]+", "_", h["id"].lower()).strip("_")


def configmap(c):
    """The ConfigMap that carries the app's module. kube-mgmt (beside OPA) loads every ConfigMap in
    its namespace with the label openpolicyagent.org/policy=rego. The chart never creates this one,
    so a helm upgrade never touches it; deleting it removes the app's rules."""
    name = c["metadata"]["name"]
    body = "".join(f"    {ln}\n" if ln else "\n" for ln in rego(c).rstrip("\n").split("\n"))
    return (f"# {name}'s policy - limits, holds and what the agent may do alone. Install or update:\n"
            f"#   kubectl apply -f {name}-configmap.yaml\n"
            f"# Remove the app's rules: kubectl delete configmap {cm_name(c)} -n {OPA_NS}\n"
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            f"  name: {cm_name(c)}\n  namespace: {OPA_NS}\n  labels:\n"
            "    openpolicyagent.org/policy: rego\n"
            f"    magertron.io/czarter: {json.dumps(name)}\n"
            f"data:\n  {name}.rego: |\n{body}")


def configmap_obj(c):
    """The app's policy ConfigMap as an object (for the Kubernetes API) - the same content as configmap()."""
    name = c["metadata"]["name"]
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": cm_name(c), "namespace": OPA_NS,
                         "labels": {"openpolicyagent.org/policy": "rego", "magertron.io/czarter": name}},
            "data": {f"{name}.rego": rego(c)}}


# ---------- Install the app's policy (setup step policy.install) ----------
# Czartup writes ONLY its own app-policy ConfigMaps. The chart gives its service account a Role for
# ConfigMaps in this namespace AND a ValidatingAdmissionPolicy that refuses anything else it tries:
# a name other than czarter-*, a missing label, or Rego that is not package magertron.apps.<name>.
# Without that policy (Kubernetes < 1.30) the chart grants nothing and this step is not offered.
K8S = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
K8S_POLICIES = os.environ.get("CZARTUP_K8S_POLICIES") == "1"


def _kube(method, path, body=None, ctype="application/json"):
    import ssl, urllib.request, urllib.error
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    ctx = ssl.create_default_context(cafile=str(K8S / "ca.crt"))
    req = urllib.request.Request(f"https://{host}:{port}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + (K8S / "token").read_text().strip(),
                                          "Content-Type": ctype, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def apply_policy(c, api, yes):
    """Create or update the app's own policy ConfigMap; kube-mgmt loads it into OPA within seconds."""
    global LAST_RESULTS
    if not K8S_POLICIES:
        return "skipped", ("this Magertron does not let Czartup install policies (needs the chart's admission policy, "
                           f"Kubernetes 1.30+) - the admin applies policy/{c['metadata']['name']}-configmap.yaml")
    want = configmap_obj(c)
    name, ns = want["metadata"]["name"], want["metadata"]["namespace"]
    st, cur = _kube("GET", f"/api/v1/namespaces/{ns}/configmaps/{name}")
    if st not in (200, 404):
        raise RuntimeError(f"could not read {ns}/{name} (HTTP {st}: {(cur or {}).get('message', '')[:160]})")
    # A ConfigMap once put in by hand with `kubectl apply` keeps an old copy of itself in this
    # annotation; Czartup's server-side apply never refreshes it, so it goes stale and misleads.
    # OPA never reads it: remove it whenever it is there.
    LAC = "kubectl.kubernetes.io/last-applied-configuration"
    stale = st == 200 and LAC in ((cur.get("metadata") or {}).get("annotations") or {})

    def drop_stale():
        if stale and yes:
            _kube("PATCH", f"/api/v1/namespaces/{ns}/configmaps/{name}",
                  {"metadata": {"annotations": {LAC: None}}}, ctype="application/merge-patch+json")
    same = st == 200 and (cur.get("data") or {}) == want["data"] and \
        all((cur.get("metadata", {}).get("labels") or {}).get(k) == v for k, v in want["metadata"]["labels"].items())
    ctl = c.get("controls", {})
    what = (f"{len(ctl.get('limits', []))} limit(s), {len(ctl.get('holds', []))} hold(s)"
            + (f", the agent alone: {len(alone_tools(c))} tool(s)" if alone_tools(c) is not None else ""))
    if same:
        drop_stale()
        LAST_RESULTS = [("controls.policy.install", "done", f"{name} is current ({what})")]
        return "done", f"nothing to do - {name} is installed and current"
    verb = "update" if st == 200 else "create"
    if not yes:
        print(f"would {verb} the ConfigMap {ns}/{name}: {what} - kube-mgmt loads it into OPA within seconds")
        return None, f"dry run - would {verb} {name}"
    # Server-side apply: create or update in one call, owned by Czartup.
    st, out = _kube("PATCH", f"/api/v1/namespaces/{ns}/configmaps/{name}?fieldManager=czartup&force=true",
                    want, ctype="application/apply-patch+yaml")
    ok = st in (200, 201)
    if ok:
        drop_stale()
    why = "" if ok else f" (HTTP {st}: {(out or {}).get('message', '')[:240]})"
    LAST_RESULTS = [("controls.policy.install", "done" if ok else "failed",
                     (f"{verb}d {ns}/{name}: {what}" if ok else f"could not {verb} {ns}/{name}{why}"))]
    print(("✅ " if ok else "❌ ") + LAST_RESULTS[0][2])
    return ("done" if ok else "failed"), LAST_RESULTS[0][2]


def alone_tools(c):
    """The tools the Czarter's agent may call on its own (no person on the call): the union
    of functionality.agents[].tools. None when no agent says - nothing is enforced then."""
    ags = (c.get("functionality") or {}).get("agents") or []
    if not any("tools" in a for a in ags):
        return None
    return sorted({t for a in ags for t in a.get("tools", [])})


def alone_msg(c):
    sa = c["identity"]["sts"]["agent"]["serviceAccount"]
    return f"%s/%s: {sa} may not call this on its own - only with a person on the call (Czarter '{c['metadata']['name']}')"


def agent_alone_rego(c):
    """The agent's service account, calling with no person on the call, is refused every tool
    its agents do not list. Calls made FOR a person (input.on_behalf_of set) are untouched -
    the app acts for staff with this same identity, and those calls need the staff's tools.
    One agent identity per Czarter (identity.sts.agent), so the lists are joined."""
    allowed = alone_tools(c)
    if allowed is None:
        return []
    sa = c["identity"]["sts"]["agent"]["serviceAccount"]
    return [f"# The agent ({sa}) with no person on the call may call only these (functionality.agents[].tools).",
            f"agent_alone_tools := {q(allowed)}", "",
            "agent_alone if not input.on_behalf_of",
            "agent_alone if input.on_behalf_of == \"\"", "",
            "deny contains msg if {",
            f"\tinput.subject == {q(sa)}",
            "\tagent_alone",
            "\tnot concat(\"/\", [input.server, input.tool]) in agent_alone_tools",
            f"\tmsg := sprintf({q(alone_msg(c))}, [input.server, input.tool])",
            "}", ""]


def server_ns(c, server):
    """The namespace a server lives in: its connection's own namespace, else the Czarter's."""
    for x in c.get("connections", []):
        if x.get("name") == server:
            return x.get("namespace") or c["metadata"]["namespace"]
    return c["metadata"]["namespace"]


def sql(c):
    s = c["identity"]["sts"]
    out = ["-- Read-only. Each query should return 1.", ""]
    for p in s["people"]:
        out.append(f"SELECT count(*) AS {rid('person', p['user'])} FROM users WHERE external_id = '{p['idpSubject']}';")
    out.append(f"SELECT count(*) AS {rid('agent', s['agent']['serviceAccount'])} FROM users WHERE external_id = '{s['agent']['externalId']}';")
    return "\n".join(out) + "\n"


def manual(c):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from runbook import PROVIDER, APP_TYPE, GRANT, KIND
    i, m = c["identity"], c["metadata"]
    prov = PROVIDER.get(i["idp"]["provider"], i["idp"]["provider"])
    lines = [f"# Manual steps for {m['name']}", "",
             "Not automated yet. Log each one in the run log when done.", "",
             f"## In {prov} (`{i['idp']['domain']}`)", ""]
    lines.append(f"- `identity.app` - create a {APP_TYPE.get(i['app'].get('type', 'regular-web'), i['app'].get('type'))}; "
                 f"its client ID goes into the Czarter: `{i['app']['clientId']}`")
    lines.append(f"- `identity.grants` - {'; '.join(GRANT.get(g, g) for g in i['app']['grantTypes'])}")
    lines.append(f"- `identity.callbacks` - {', '.join(i['app']['callbackUrls'])}")
    if i["people"].get("connections"):
        lines.append(f"- `identity.connections` - enable only {', '.join(i['people']['connections'])} for this app")
    if i["people"]["mfa"]["required"]:
        how = i["people"]["mfa"].get("how") or "require MFA for this app only"
        lines.append(f"- `identity.mfa` - {how[:1].upper() + how[1:]}")
    if "client_credentials" in i["app"]["grantTypes"]:
        lines.append(f"- `identity.sts-access` - APIs → `{i['idp']['audience']}` → Application Access: turn on Client Access for this app")
    lines.append(f"- `identity.secret` - store the client secret at `{i['app']['secretRef']}`")
    for p_ in i["sts"]["people"]:
        lines.append(f"- create the user for `{p_['user']}`; their IdP subject goes into the Czarter: `{p_['idpSubject']}`")
    lines += ["", "## In Magertron™", ""]
    a = i["sts"]["agent"]
    own = a["roles"][0]
    if not own.lower().startswith(("system:", "mcp consumer", "llm consumer", "api consumer")):
        lines.append(f"- `sts.role` - create the role `{own}` in namespace `{m['namespace']}`, with no permissions yet")
    for x in c["connections"]:
        xns = cns(c, x)
        if x.get("namespace") and xns != m["namespace"]:
            lines.append(f"- `connections.{x['name']}` - nothing to deploy: use the existing {KIND.get(x['kind'], x['kind'])} `{x['name']}` in `{xns}`")
        else:
            lines.append(f"- `connections.{x['name']}` - register the {KIND.get(x['kind'], x['kind'])} `{x['name']}` in `{xns}`")
    lines.append(f"- `sts.agent` - service account `{a['serviceAccount']}`, external ID `{a['externalId']}`, roles {', '.join(a['roles'])}"
                 " (or Apply: agent service account)")
    for p_ in i["sts"]["people"]:
        lines.append(f"- `sts.person.{p_['user']}` - set external ID `{p_['idpSubject']}` on `{p_['user']}` (not an admin)")
    if any(x.get("tools") for x in c["connections"]):
        lines.append(f"- grants - give `{own}` exactly the listed tools (or Apply: tool grants)")
    return "\n".join(lines) + "\n"


def plan(c, outdir):
    d = pathlib.Path(outdir).expanduser() / c["metadata"]["name"]
    d.mkdir(parents=True, exist_ok=True)
    for fn, text in (("policy.rego", rego(c)), (f"{c['metadata']['name']}-configmap.yaml", configmap(c)),
                     ("verify.sql", sql(c)), ("manual.md", manual(c))):
        (d / fn).write_text(text)
        print(f"wrote {d / fn}")


# ---------- verify ----------

def psql(query):
    pods = subprocess.run(["kubectl", "get", "pod", "-o", "name"], capture_output=True, text=True)
    if pods.returncode:
        raise RuntimeError(pods.stderr.strip() or "kubectl get pod failed")
    pod = next((p.split("/", 1)[1] for p in pods.stdout.split()
                if "mcp-orchestrator-postgres-" in p and "inventory" not in p), None)
    if not pod:
        raise RuntimeError("no mcp-orchestrator-postgres pod in the current namespace")
    r = subprocess.run(["kubectl", "exec", pod, "--", "psql", "-U", "mcp", "-d", "mcp_platform", "-tAc", query],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


class Api:
    """Read-only calls to the orchestrator, the same routes seeding.cpp uses.
    The operator token is read from a file and never printed."""

    def __init__(self, base, token_file):
        import urllib.request  # noqa: F401  (stdlib only)
        self.base = base.rstrip("/")
        # Hosted Czartup passes the signed-in person's own console token, per request.
        if os.environ.get("CZARTER_TOKEN"):
            self.token = os.environ["CZARTER_TOKEN"].strip()
            return
        p = pathlib.Path(token_file).expanduser()
        if not p.exists():
            raise RuntimeError(f"operator token file not found: {p}")
        if p.stat().st_mode & 0o077:
            raise RuntimeError(f"{p} is readable by others - run: chmod 600 {p}")
        self.token = p.read_text().strip()

    last_error = ""

    def headers(self):
        # A named User-Agent: Cloudflare refuses Python's default one with a 403.
        return {"Authorization": "Bearer " + self.token, "User-Agent": "magertron-czarter/1",
                "Accept": "application/json"}

    @staticmethod
    def why(e):
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return " ".join(body.split())[:300]

    def get(self, path):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            self.last_error = self.why(e)
            return e.code, None

    def get_bytes(self, path):
        """(status, raw body) - for answers that are not JSON, such as an icon."""
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, headers={**self.headers(), "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def post(self, path, body):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={**self.headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            self.last_error = self.why(e)
            try:
                return e.code, json.loads(self.last_error or "null")
            except ValueError:
                return e.code, None

    def put(self, path, body):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="PUT",
                                     headers={**self.headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            self.last_error = self.why(e)
            try:
                return e.code, json.loads(self.last_error or "null")
            except ValueError:
                return e.code, None

    def delete(self, path):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, method="DELETE", headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                try:
                    return r.status, json.loads(raw or b"null")
                except ValueError:
                    return r.status, None
        except urllib.error.HTTPError as e:
            self.last_error = self.why(e)
            return e.code, None

    def patch(self, path, body):
        import urllib.request, urllib.error
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="PATCH",
                                     headers={**self.headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            self.last_error = self.why(e)
            try:
                return e.code, json.loads(self.last_error or "null")
            except ValueError:
                return e.code, None

    @staticmethod
    def items(j):
        if isinstance(j, list):
            return j
        if isinstance(j, dict) and isinstance(j.get("items"), list):
            return j["items"]
        return []


def mcp_call(base, token, ns, server, tool, arguments=None, timeout=30):
    """One MCP tools/call through the Magertron gateway: initialize, initialized, call.
    Returns (http_status, result_or_error_text). Reads plain JSON or an SSE stream."""
    import urllib.request, urllib.error
    url = f"{base.rstrip('/')}/mcp/{ns}/{server}/"
    # A named User-Agent: Cloudflare refuses Python's default one (error 1010), as in Api.headers.
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
           "Accept": "application/json, text/event-stream", "User-Agent": "magertron-czartup/1"}

    def post(body, sid=None):
        h = dict(hdr, **({"Mcp-Session-Id": sid} if sid else {}))
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode(errors="replace")
                return r.status, r.headers.get("Mcp-Session-Id") or sid, raw
        except urllib.error.HTTPError as e:
            return e.code, sid, e.read().decode(errors="replace")

    def message(raw):
        raw = raw.strip()
        if raw.startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():          # SSE: the last data: line carries the reply
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if "result" in msg or "error" in msg:
                    return msg
        return {}

    st, sid, raw = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "czartup-verify", "version": "1"}}})
    if st != 200:
        return st, raw[:200]
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    st, sid, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments or {}}}, sid)
    if st != 200:
        return st, raw[:200]
    msg = message(raw)
    if "error" in msg:
        return st, str(msg["error"].get("message", msg["error"]))[:200]
    res = msg.get("result") or {}
    if res.get("structuredContent") is not None:
        return st, res["structuredContent"]
    for part in res.get("content") or []:
        if part.get("type") == "text":
            try:
                return st, json.loads(part["text"])
            except ValueError:
                return st, part["text"]
    return st, res


def identity_checks(c, api):
    """For every Trusted upstream: (1) the server record really has forward_identity on,
    (2) a live whoami through the gateway, as the caller, reaches the server with
    x-magertron-subject filled in. The SI's own check covers the person (acting_for)."""
    out = []
    base = os.environ.get("CZARTUP_PUBLIC_API") or api.base
    for x in c["connections"]:
        if not x.get("forwardIdentity"):
            continue
        xns, name = cns(c, x), x["name"]
        st, j = api.get(f"/api/v1/servers/{xns}/{name}")
        if st != 200 or not isinstance(j, dict):
            continue   # connections.<name> already says it is not registered
        if not j.get("forward_identity"):
            out.append((f"identity.{name}", "failed", f"Trusted upstream is OFF on {xns}/{name} - the server never learns "
                                                      "who the user is. Run Register servers (it corrects the setting)"))
            continue
        hst, r = mcp_call(base, api.token, xns, name, WHOAMI)
        if hst == 200 and isinstance(r, dict) and r.get("subject"):
            out.append((f"identity.{name}", "passed", f"{name} received x-magertron-subject = {r['subject']} through the gateway"))
        elif hst == 200 and isinstance(r, dict):
            out.append((f"identity.{name}", "failed", f"{name} answered whoami but received NO x-magertron-subject - "
                                                      "the gateway is not forwarding identity to it"))
        elif hst in (401, 403) or (isinstance(r, str) and "permission" in r.lower()):
            out.append((f"identity.{name}", "skipped", f"Trusted upstream is on; the live whoami needs a caller allowed to "
                                                       f"use {name} (HTTP {hst}) - the SI's identity check covers it"))
        elif hst == 421 or (isinstance(r, str) and "Invalid Host header" in r):
            out.append((f"identity.{name}", "failed", host_refused(api, xns, name)))
        else:
            out.append((f"identity.{name}", "failed", f"whoami on {xns}/{name} through the gateway: HTTP {hst} {r if isinstance(r, str) else ''}".strip()
                                                      + (" - does the server have a whoami tool? (SERVER-SPEC)" if hst == 200 else "")))
    return out


API_BASE_FOR_STS = [os.environ.get("CZARTER_API", "https://app.magertron.com")]
READ_ONLY = re.compile(r"^(get|list|search|read|find|lookup|describe|show|check|whoami)(_|$)", re.I)


def agent_token(c):
    """The agent's own token, the way it runs at night: the app's client credentials from
    Vault (identity.app.secretRef), client_credentials at the IdP. Never printed or stored."""
    import urllib.request, urllib.error
    from idp_auth0 import _split, vault_read
    i = c["identity"]
    path, key = _split(i["app"].get("secretRef", ""))
    secret = str(vault_read(path).get(key or "client_secret") or "").strip()
    if not secret:
        raise RuntimeError(f"no client secret at vault:{path}#{key or 'client_secret'}")
    if i["idp"]["provider"] == "keycloak":
        # Keycloak: the realm's token endpoint, form-encoded; the audience comes from the client's
        # audience mapper (Create app adds it). Reached where Czartup reaches Keycloak - inside a
        # trial tenant that is the in-cluster address; the token's issuer is the public one either way.
        import urllib.parse, idp_keycloak
        base, realm = idp_keycloak.realm_of(i["idp"]["domain"])
        try:
            base = (vault_read(_split(idp_keycloak.MGMT_REF)[0]).get("url") or base).strip().rstrip("/")
        except Exception:  # noqa: BLE001 - no automation credentials: the issuer's own address
            pass
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": i["app"]["clientId"],
                                       "client_secret": secret}).encode()
        req = urllib.request.Request(f"{base}/realms/{urllib.parse.quote(realm, safe='')}/protocol/openid-connect/token",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "magertron-czartup/1"})
    else:
        body = json.dumps({"grant_type": "client_credentials", "client_id": i["app"]["clientId"],
                           "client_secret": secret, "audience": i["idp"]["audience"]}).encode()
        base = os.environ.get("CZARTUP_AUTH0_BASE") or f"https://{i['idp']['domain']}"
        req = urllib.request.Request(base + "/oauth/token", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "magertron-czartup/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            idp_tok = json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"the IdP refused the app's client credentials (HTTP {e.code}): {Api.why(e)[:150]}")
    # The gateway takes only Magertron-signed tokens: exchange the app's token at the STS on its
    # own (subject_token, no actor_token) - exactly what the app's agent_bearer() does.
    sts = (os.environ.get("CZARTUP_PUBLIC_API") or API_BASE_FOR_STS[0]).rstrip("/") + "/api/v1/token/exchange"
    req = urllib.request.Request(sts, method="POST", data=json.dumps({
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": idp_tok, "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "magertron-czartup/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read()).get("access_token") or ""
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"the Magertron STS refused the app's token (HTTP {e.code}): {Api.why(e)[:150]}")
    if not tok:
        raise RuntimeError("the Magertron STS answered without an access_token")
    return tok


def agent_alone_gateway(c, api):
    """The agent on its own, through the gateway (0.2.29): with its own token and nobody on the
    call, one of its own tools is answered and a tool that is not its own is refused - the same
    call the nightly agent makes, so the developer never has to stage it by hand.
    Only read-only tools (get_/list_/...) are called, so the check never changes anything."""
    allowed = alone_tools(c)
    if not allowed or "client_credentials" not in c["identity"]["app"].get("grantTypes", []):
        return []
    step = "controls.agent-alone.gateway"
    sa = c["identity"]["sts"]["agent"]["serviceAccount"]
    known = sorted(f"{x['name']}/{t}" for x in c.get("connections", []) if x.get("kind") != "llm" for t in tools_of(x))
    ns_of = {x["name"]: cns(c, x) for x in c.get("connections", [])}
    own = [t for t in allowed if t in known and READ_ONLY.match(t.split("/", 1)[1])]
    other = [t for t in known if t not in allowed and READ_ONLY.match(t.split("/", 1)[1])
             and t.split("/", 1)[1] != WHOAMI]          # whoami is the platform's identity probe, not an app tool
    if not own and not other:
        return [(step, "skipped", f"{sa}'s tools all change something, so Czartup does not call them for a check - "
                                  "the policy decisions above cover it")]
    API_BASE_FOR_STS[0] = api.base
    try:
        tok = agent_token(c)
    except Exception as e:  # noqa: BLE001
        return [(step, "failed", f"could not get {sa}'s own token: {e}")]
    base = os.environ.get("CZARTUP_PUBLIC_API") or api.base
    out, msg_of = [], lambda srv, tool: alone_msg(c) % (srv, tool)
    if other:
        srv, tool = other[0].split("/", 1)
        st, r = mcp_call(base, tok, ns_of[srv], srv, tool)
        text = r if isinstance(r, str) else json.dumps(r)[:200]
        if st == 403 and "may not call this on its own" in text:
            out.append((step, "passed", f"the gateway refuses {sa} on its own on {srv}/{tool} (HTTP 403) - not one of its tools"))
        else:
            out.append((step, "failed", f"{sa} on its own called {srv}/{tool}, which is not one of its tools, and the gateway "
                                        f"did not refuse it (HTTP {st}: {text[:150]}) - install the policy again"))
    if own:
        srv, tool = own[0].split("/", 1)
        st, r = mcp_call(base, tok, ns_of[srv], srv, tool)
        text = r if isinstance(r, str) else json.dumps(r)[:200]
        if st == 200:
            out.append((step + ".allowed", "passed", f"{sa} on its own reached its own tool {srv}/{tool} through the gateway"))
        elif "may not call this on its own" in text:
            out.append((step + ".allowed", "failed", f"the gateway refuses {sa} its own tool {srv}/{tool} - install the policy again"))
        elif st == 421 or "Invalid Host header" in text:
            out.append((step + ".allowed", "failed", host_refused(api, ns_of[srv], srv)))
        else:
            out.append((step + ".allowed", "failed", f"{sa} on its own could not reach {srv}/{tool} (HTTP {st}: {text[:150]}) - "
                                                     "check its role has that tool (Apply: tool grants) and the tool is approved"))
    return out


def approver_checks(c, api):
    """Approvals (0.2.37): every listed approver of every hold must be a real person who can decide -
    a Magertron user (matched like Magertron matches: username, email, SSO email or the email's local
    part), linked to the IdP (external_id), holding the app's role. A name that matches nobody means
    the hold can never be approved. And the holds API must answer."""
    holds = (c.get("controls") or {}).get("holds") or []
    if not holds:
        return []
    out = []
    st, _ = api.get("/api/v1/holds")
    out.append(("approvals.api", "passed" if st == 200 else "failed",
                "the holds API answers" if st == 200 else
                f"GET /api/v1/holds returned HTTP {st} - the app's Approvals screen will not work"))
    users, _ = users_by_name(api)
    try:
        app_role = resolve_roles(api, [c["identity"]["sts"]["agent"]["roles"][0]])[0]
    except RuntimeError:
        app_role = None

    def match(name):
        n = name.strip().lower()
        # A user whose username IS the name is the one meant - even when another user shares the
        # same email (dana.ortiz and dana.ortiz2 both at dana.ortiz@...). Only a name that is not a
        # username is matched by email.
        exact = [u for uname, u in users.items() if uname.lower() == n]
        if exact:
            return exact
        hit = []
        for uname, u in users.items():
            keys = {uname.lower()}
            for e in (u.get("email"), u.get("sso_email")):
                if e:
                    keys |= {e.lower(), e.lower().split("@", 1)[0]}
            if n in keys:
                hit.append(u)
        return hit

    for h in holds:
        for a in h.get("approvers") or []:
            step = f"approvals.{h['id']}.{a}"
            hit = match(a)
            if len(hit) != 1:
                out.append((step, "failed", (f"approver {a} matches no Magertron user - this hold can never be approved by them"
                                             if not hit else f"approver {a} matches {len(hit)} users ("
                                             + ", ".join(u["username"] for u in hit) + ") - name one")))
                continue
            u = hit[0]
            if not u.get("external_id"):
                out.append((step, "failed", f"approver {a} ({u['username']}) is not linked to the IdP - run setup (People's access)"))
            elif app_role and app_role not in role_ids(u):
                out.append((step, "failed", f"approver {a} ({u['username']}) does not have {app_role}, so cannot sign in "
                                            "to the app to decide - run setup (People's access)"))
            else:
                out.append((step, "passed", f"{a} is {u['username']}: linked, has the app's role, may decide {h['server']}/{h['tool']}"))
    return out


def verify_api(c, api):
    """Returns (results, steps whose database result this replaces)."""
    res, ns, replaces = [], c["metadata"]["namespace"], set()

    res += identity_checks(c, api)
    res += approver_checks(c, api)

    # Tool definitions still waiting for approval: every call to them is refused with
    # "tool definition is new and not yet approved", which looks like an app bug.
    try:
        want = {(cns(c, x), x["name"]): set(tools_of(x)) for x in c["connections"] if tools_of(x)}
        waiting = {}
        for d in _tool_defs(api, "pending") + _tool_defs(api, "drifted"):
            k = (d.get("server_namespace"), d.get("server_name"))
            if k in want and d.get("tool_name") in want[k]:
                waiting.setdefault(k, []).append(f"{d['tool_name']} ({d.get('status')})")
        for (xns, srv), ts in sorted(waiting.items()):
            res.append((f"tools.{srv}", "failed", f"{len(ts)} tool definition(s) on {xns}/{srv} not approved: "
                                                  f"{', '.join(sorted(ts))} - run Approve tools"))
    except RuntimeError as e:
        res.append(("tools.approved", "failed", str(e)))

    # The Czarter's limits and holds only run if External Policy is asked about its
    # namespace at all. Out of scope, every deny and hold check passes straight through.
    if c.get("controls", {}).get("limits") or c.get("controls", {}).get("holds"):
        st, j = api.get("/api/v1/governance/pdp")
        if st == 200 and isinstance(j, dict):
            scope = str(j.get("namespaces") or "*").strip()
            names = {n.strip() for n in scope.split(",") if n.strip()}
            if not j.get("configured", True):
                res.append(("policy.scope", "failed", "External Policy is not configured - limits and holds are not enforced"))
            elif scope == "*" or ns in names:
                res.append(("policy.scope", "passed", f"External Policy is asked about {ns} ({scope})"))
            else:
                res.append(("policy.scope", "failed",
                            f"{ns} is not in External Policy's namespaces ({scope}) - its limits and holds are never checked. "
                            + ("Run setup (it adds the namespace)" if j.get("namespaces_editable") else
                               f"Add it (install.sh --pdp-namespaces \"{scope},{ns}\")")))
        else:
            res.append(("policy.scope", "failed", f"could not read External Policy (GET /api/v1/governance/pdp returned HTTP {st})"))

    for x in c["connections"]:
        step = f"connections.{x['name']}"
        xns = cns(c, x)
        st, j = api.get(f"/api/v1/servers/{xns}/{x['name']}")
        if st == 200 and isinstance(j, dict):
            port = j.get("cluster_port", 0)
            res.append((step, "passed", f"registered in {xns}" + (f", cluster port {port}" if port else ", no cluster port (remote server or route not added)")))
        else:
            res.append((step, "failed", f"GET /api/v1/servers/{xns}/{x['name']} returned HTTP {st}"))

    a = c["identity"]["sts"]["agent"]
    st, orgs = api.get("/api/v1/orgs")
    org = next((o.get("id") for o in api.items(orgs) if o.get("slug") == "default"), None) if st == 200 else None
    if not org:
        res.append(("sts.agent", "failed", f"could not find the default organization (HTTP {st})"
                    + (f" - server said: {api.last_error}" if api.last_error else "")))
        return res, replaces
    st, j = api.get(f"/api/v1/service-accounts?organization_id={org}")
    if st != 200:
        res.append(("sts.agent", "failed", f"could not list service accounts (HTTP {st})"))
        return res, replaces
    row = next((r for r in api.items(j)
                if a["serviceAccount"] in (r.get("subject"), r.get("name")) and not r.get("revoked_at")), None)
    if not row:
        res.append(("sts.agent", "failed", f"no live service account named {a['serviceAccount']}"))
    elif "external_id" in row and row["external_id"] != a["externalId"]:
        res.append(("sts.agent", "failed", f"{a['serviceAccount']} has external ID {row['external_id']}, Czarter says {a['externalId']}"))
    elif "external_id" in row:
        res.append(("sts.agent", "passed", f"{a['serviceAccount']} is live with the right external ID"))
        replaces.add("sts.agent")
    else:
        # The API does not show the external ID: the account is live, the database row decides.
        print(f"   api: {a['serviceAccount']} is live; its external ID is checked in the database")
    if row:
        res += verify_scope(c, api, row)
        res += agent_alone_gateway(c, api)
    return res, replaces


def verify_scope(c, api, row):
    """Least privilege: the Czarter's tool list only limits the agent if no role it
    holds has a wildcard ALLOW on that server. One result per connection that lists tools."""
    ns, a, out = c["metadata"]["namespace"], c["identity"]["sts"]["agent"], []
    held = row.get("role_ids") or row.get("roles")
    if not held:
        try:
            held = resolve_roles(api, a["roles"])
        except RuntimeError as e:
            return [(f"controls.scope.{x['name']}", "failed", str(e)) for x in c["connections"] if x.get("tools")]
    held = [h.get("id") if isinstance(h, dict) else h for h in held]
    try:
        app_role = resolve_roles(api, [a["roles"][0]])[0]
    except RuntimeError:
        app_role = None
    for x in c["connections"]:
        if not tools_of(x):
            continue
        step = f"controls.scope.{x['name']}"
        st, j = api.get(f"/api/v1/tool-rbac/servers/{cns(c, x)}/{x['name']}/permissions")
        if st != 200:
            out.append((step, "failed", f"could not read permissions (HTTP {st})")); continue
        rows = api.items(j) or (j.get("permissions") if isinstance(j, dict) else []) or []
        wide = sorted({r.get("role_id") for r in rows
                       if r.get("effect") == "ALLOW" and "*" in str(r.get("tool_pattern", ""))
                       and r.get("role_id") in held and r.get("role_id") != app_role})
        if wide and "*" in x["tools"]:
            out.append((step, "passed", f"the Czarter allows every tool on {x['name']}, so "
                        + ", ".join(f"{w} *" for w in wide) + " adds nothing"))
        elif wide and x.get("kind") == "llm" and _llm_model(api, cns(c, x), x["name"]) in x["tools"]:
            # An LLM server serves one model (0.2.91): LLM Consumer's * reaches only that model, which the
            # Czarter lists - nothing extra is allowed. Its spend is held by the budget, not the tool list.
            out.append((step, "passed", f"{x['name']} serves only {_llm_model(api, cns(c, x), x['name'])}, "
                        "which the Czarter lists"))
        elif wide:
            out.append((step, "failed", f"tool list not enforced on {x['name']}: "
                        + ", ".join(f"{w} has *" for w in wide)))
        else:
            out.append((step, "passed", f"agent limited to the {len(tools_of(x))} listed tools on {x['name']}"))
    return out


def host_refused(api, ns, name):
    """For an HTTP 421 "Invalid Host header" (0.2.95): the server itself refused the gateway's Host.
    Says exactly which host:port to allow - the address Magertron registered for it."""
    from urllib.parse import quote, urlparse
    try:
        st, j = api.get(f"/api/v1/servers/{ns}/{quote(name)}")
        u = urlparse(str((j or {}).get("endpoint_url") or (j or {}).get("url") or "")) if st == 200 else None
        hp = f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}" if u and u.hostname else "<host>:<port>"
    except Exception:  # noqa: BLE001
        hp = "<host>:<port>"
    return (f"{name} itself refused the gateway's Host header (HTTP 421) - add \"{hp}\" to the server's allowed "
            "hosts (SERVER-SPEC: Allowed hosts) and restart it. Nothing to change in Magertron.")


def _llm_model(api, ns, name):
    """The one model an LLM server serves (its "model" label), or "" when it does not say."""
    from urllib.parse import quote
    st, j = api.get(f"/api/v1/servers/{ns}/{quote(name)}")
    if st != 200 or not isinstance(j, dict):
        return ""
    lab = j.get("labels") or {}
    if isinstance(lab, str):
        try:
            lab = json.loads(lab)
        except ValueError:
            lab = {}
    return str((lab or {}).get("model") or j.get("model") or "")


def opa_call(method, path, body=None):
    """One call to the live OPA (CZARTUP_OPA_URL). Returns the parsed JSON."""
    import urllib.request
    req = urllib.request.Request(OPA_URL.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def installed_module(c, policy_path):
    """(text of this app's module as OPA has it, where it came from) - or (None, why not)."""
    want = f"package {pkg(c)}\n"
    if OPA_URL:
        try:
            mods = opa_call("GET", "/v1/policies").get("result", [])
        except Exception as e:  # noqa: BLE001
            return None, f"could not reach OPA at {OPA_URL}: {e}"
        hit = next((m for m in mods if want in (m.get("raw") or "")), None)
        return (hit["raw"], f"OPA ({hit['id']})") if hit else (None, "not loaded in OPA")
    p = pathlib.Path(policy_path).expanduser()
    if not p.exists():
        return None, f"no live OPA (CZARTUP_OPA_URL) and no file {p}"
    text = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        text = "".join(str(v) for d in yaml.safe_load_all(text) if d for v in (d.get("data") or {}).values())
    return (text, p.name) if want in text else (None, f"not in {p.name}")


def _nest(field, val):
    out = cur = {}
    parts = field.split(".")
    for k in parts[:-1]:
        cur[k] = {}
        cur = cur[k]
    cur[parts[-1]] = val
    return out


def _violating(op, v):
    """A value that breaks `field op v`, or None when there is no safe one to try."""
    num = isinstance(v, (int, float)) and not isinstance(v, bool)
    if op == "<=" and num: return v + 1
    if op == "<" and num: return v
    if op == ">=" and num: return v - 1
    if op == ">" and num: return v
    if op == "==": return v + 1 if num else f"{v}-czartup-verify"
    if op == "in": return "czartup-verify-not-listed"
    if op == "not-in" and v: return v[0]
    return None


def decide(inp):
    """OPA's decision for one call, exactly as Magertron asks for it."""
    path = os.environ.get("CZARTUP_OPA_DECISION", "magertron/decision")
    return opa_call("POST", f"/v1/data/{path}", {"input": inp}).get("result") or {}


def verify(c, policy_path, api=None):
    """Policy: is THIS app's module installed and current - and, against the live OPA, does it
    actually refuse and hold what the Czarter says (real decisions, not a text search)."""
    res = []  # (step, status, detail)
    ctl, name = c.get("controls", {}), c["metadata"]["name"]
    fix = ("run setup (it installs the app's policy)" if K8S_POLICIES else
           f"kubectl apply -f policy/{name}-configmap.yaml (from the hand-over pack)")
    mod, where = installed_module(c, policy_path)
    if mod is None:
        res.append(("controls.policy", "failed", f"{name}'s policy is {where} - {fix}"))
    elif mod.strip() != rego(c).strip():
        res.append(("controls.policy", "failed", f"{name}'s policy in {where} is out of date with the Czarter - {fix}"))
    else:
        res.append(("controls.policy", "passed", f"{name}'s policy is installed and current ({where})"))
    live = bool(OPA_URL) and mod is not None
    if live:
        try:
            coll = opa_call("GET", "/v1/data/magertron/apps_collector").get("result")
        except Exception:  # noqa: BLE001
            coll = None
        if not coll:
            live = False
            res.append(("controls.collector", "failed", "OPA has the app's module but the base policy does not collect app "
                        "rules (magertron-apps.rego is missing) - upgrade the chart; nothing below is enforced"))
    nf = res[0][2]

    def run(step, inp, ok, good, bad):
        try:
            d = decide(inp)
        except Exception as e:  # noqa: BLE001
            res.append((step, "failed", f"could not ask OPA: {e}")); return
        res.append((step, "passed" if ok(d) else "failed", good if ok(d) else f"{bad} (OPA said {json.dumps(d)[:300]})"))

    person = {"subject": "czartup-verify", "on_behalf_of": "czartup-verify", "roles": []}
    for h in ctl.get("holds", []):
        step = f"controls.hold.{h['id']}"
        args = {}
        if "argPresent" in h["when"]:
            args.update(_nest(h["when"]["argPresent"], "czartup-verify"))
        for k, v in h["when"].get("argEquals", {}).items():
            args.update(_nest(k, v))
        for k, v in h["when"].get("argAbove", {}).items():
            args.update(_nest(k, v + 1))
        if not live:
            res.append((step, "failed" if mod is None or hold_rule(h) not in mod else "passed",
                        nf if mod is None else f"hold rule present in {where} (not tried against a live OPA)"))
            continue
        run(step, {**person, "namespace": server_ns(c, h["server"]), "server": h["server"], "tool": h["tool"], "arguments": args},
            lambda d: d.get("allow") is False and "hold" in d and h["reason"] in d.get("reasons", [])
                      and sorted((d.get("hold") or {}).get("approvers") or []) == sorted(h.get("approvers") or []),
            f"OPA holds {h['server']}/{h['tool']} for approval: {h['reason']}"
            + (f" (approvers: {', '.join(h['approvers'])})" if h.get("approvers") else ""),
            f"OPA did not hold {h['server']}/{h['tool']} with the Czarter's approvers ({', '.join(h.get('approvers') or []) or 'none'})"
            " - run setup (Install the app's policy)")

    for l in ctl.get("limits", []):
        step = f"controls.limit.{l['id']}"
        if "field" not in l:
            if IDENT.match(l["policyRef"]):
                res.append((step, "skipped", f"enforced by existing policy '{l['policyRef']}' - not tried here"))
            else:
                res.append((step, "skipped", f"cannot check '{l['policyRef']}' automatically - confirm by hand"))
            continue
        bad = _violating(l["op"], l["value"])
        if not live or bad is None:
            ok_ = mod is not None and q(limit_msg(l)) in mod
            res.append((step, "passed" if ok_ else "failed", nf if mod is None else
                        (f"limit present in {where} (not tried against a live OPA)" if ok_ else f"limit missing in {where}")))
            continue
        run(step, {**person, "namespace": server_ns(c, l["server"]), "server": l["server"], "tool": l["tool"],
                   "arguments": _nest(l["field"], bad)},
            lambda d: d.get("allow") is False and limit_msg(l) in d.get("reasons", []),
            f"OPA refuses {l['server']}/{l['tool']} with {l['field']}={json.dumps(bad)}: {l['description']}",
            f"OPA did not refuse {l['server']}/{l['tool']} with {l['field']}={json.dumps(bad)}")

    # The agent on its own (no person on the call): only its listed tools. An agent that runs
    # with nobody signed in and lists none may call EVERY tool its role holds - including the
    # ones its "must not" list names - so that fails, for every Czarter, old ones included.
    ags = (c.get("functionality") or {}).get("agents") or []
    if ags:
        step = "controls.agent-alone"
        allowed = alone_tools(c)
        known = {f"{x['name']}/{t}" for x in c.get("connections", []) for t in tools_of(x)}
        if allowed is None:
            res.append((step, "failed", "no agent lists the tools it may call on its own - with nobody on the call it "
                        "may use every tool its role is granted, whatever its 'must not' list says. Add "
                        "functionality.agents[].tools (server/tool), then install the policy again"))
        elif [t for t in allowed if t not in known and not t.endswith("/*")]:
            res.append((step, "failed", "the agent's own tools name tools no connection lists: " +
                        ", ".join(t for t in allowed if t not in known and not t.endswith("/*"))))
        elif not live:
            ok_ = mod is not None and f"agent_alone_tools := {q(allowed)}" in mod
            res.append((step, "passed" if ok_ else "failed", nf if mod is None else
                        (f"agent-alone rule present in {where} (not tried against a live OPA)" if ok_
                         else f"the agent-alone rule is missing or out of date in {where} - {fix}")))
        else:
            sa = c["identity"]["sts"]["agent"]["serviceAccount"]
            ns_of = {x["name"]: cns(c, x) for x in c.get("connections", [])}
            refused = sorted(known - set(allowed))
            alone = {"subject": sa, "on_behalf_of": "", "roles": []}
            if refused:
                srv, tool = refused[0].split("/", 1)
                msg = alone_msg(c) % (srv, tool)
                run(step, {**alone, "namespace": ns_of[srv], "server": srv, "tool": tool, "arguments": {}},
                    lambda d: d.get("allow") is False and msg in d.get("reasons", []),
                    f"OPA refuses {sa} alone on {srv}/{tool}; on its own it may call only {len(allowed)} tool(s)",
                    f"OPA let {sa} call {srv}/{tool} with nobody on the call")
            exact = [t for t in allowed if not t.endswith("/*")]
            if exact:
                srv, tool = exact[0].split("/", 1)
                msg = alone_msg(c) % (srv, tool)
                run(step + ".allowed", {**alone, "namespace": ns_of.get(srv, c["metadata"]["namespace"]),
                                         "server": srv, "tool": tool, "arguments": {}},
                    lambda d: msg not in d.get("reasons", []),
                    f"{sa} alone may still call its own tool {srv}/{tool}",
                    f"OPA refuses {sa} its own tool {srv}/{tool}")

    if any(r[0] == "controls.collector" for r in res):    # present, but nothing applies it
        res = [(st_, "failed", "not enforced - the base policy does not collect app rules (upgrade the chart)")
               if st_.startswith(("controls.hold.", "controls.limit.", "controls.agent-alone")) and v == "passed" else (st_, v, d)
               for st_, v, d in res]

    s = c["identity"]["sts"]
    pairs = [(f"sts.person.{x['user']}", x["idpSubject"]) for x in s["people"]]
    if api is not None:
        # Through the Magertron(TM) API - no database access (hosted Czartup has none).
        # sts.agent is checked by verify_api, which replaces this row.
        users, st = users_by_name(api)
        try:
            app_role = resolve_roles(api, [s["agent"]["roles"][0]])[0]
        except RuntimeError:
            app_role = None
        rest = []
        for x, (step, ext) in zip(s["people"], pairs):
            if UNLINKED in (x["user"], ext):
                res.append((step, "failed", f"{x.get('email', x['user'])} is not linked yet - run People's access"))
                continue
            u = users.get(x["user"])
            if u is not None and "external_id" in u:
                ok = (u.get("external_id") or "") == ext
                has = app_role is None or app_role in role_ids(u)
                res.append((step, "passed" if ok and has else "failed",
                            (f"{x['user']} has external_id {ext}" if ok else
                             f"{x['user']} has external_id {u.get('external_id') or '(none)'}, the Czarter says {ext}")
                            + ("" if has else f"; missing role {app_role} - run People's access")))
            elif users and u is None:
                res.append((step, "failed", f"no Magertron user {x['user']}"))
            else:
                rest.append((step, ext))
        pairs = rest
        if pairs and os.environ.get("CZARTER_TOKEN"):
            res += [(step, "failed", f"could not read the user through the API (HTTP {st})") for step, _ in pairs]
            return res
    else:
        pairs.append(("sts.agent", s["agent"]["externalId"]))
    for step, ext in pairs:
        try:
            n = psql(f"SELECT count(*) FROM users WHERE external_id = '{ext.replace(chr(39), chr(39) * 2)}'")
            res.append((step, "passed" if n == "1" else "failed", f"users with external_id {ext}: {n}"))
        except Exception as e:  # noqa: BLE001
            res.append((step, "failed", f"could not query: {e}"))
    return res


def record(res, runs_path, by="czarter-verify"):
    p = pathlib.Path(runs_path).expanduser()
    runs = json.loads(p.read_text()) if p.exists() else []
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    runs += [{"step": s, "status": st, "done_by": by, "done_at": now, "detail": d}
             for s, st, d in res if st != "skipped"]
    p.write_text(json.dumps(runs, indent=2) + "\n")
    print(f"recorded {sum(1 for r in res if r[1] != 'skipped')} results in {p}")


# ---------- apply (one step at a time; dry run unless --yes) ----------

SA_TTL_DAYS = 365


def default_org(api):
    st, orgs = api.get("/api/v1/orgs")
    org = next((o.get("id") for o in api.items(orgs) if o.get("slug") == "default"), None) if st == 200 else None
    if not org:
        raise RuntimeError(f"could not find the default organization (HTTP {st})"
                           + (f" - server said: {api.last_error}" if api.last_error else ""))
    return org


def resolve_roles(api, wanted):
    """Czarter lists roles by name or id; the API needs ids."""
    st, j = api.get("/api/v1/roles")
    if st != 200:
        raise RuntimeError(f"could not list roles (GET /api/v1/roles returned HTTP {st}) - "
                           "send me the roles route and I will fix this")
    rows = api.items(j)
    ids, missing = [], []
    for w in wanted:
        r = next((r for r in rows if w in (r.get("id"), r.get("name"))), None)
        if not r:
            # With two orchestrator pods, a role just created on one pod is not in the
            # other pod's list until it reloads. Ask for it by id a few times: each
            # request can land on either pod, so one usually reaches the pod that has it.
            import time
            for attempt in range(4):
                for cand in dict.fromkeys([role_id_for(w), w]):
                    from urllib.parse import quote
                    st2, one = api.get("/api/v1/roles/" + quote(cand, safe=""))
                    if st2 == 200 and isinstance(one, dict) and one.get("id"):
                        r = one
                        break
                if r:
                    break
                time.sleep(1)
        (ids.append(r["id"]) if r else missing.append(w))
    if missing:
        raise RuntimeError("no such role: " + ", ".join(missing))
    return ids


def role_id_for(name):
    """The id a new role gets, as the console makes them: "custom:" plus its name as a
    slug ("Playbook Auto" -> "custom:playbook-auto"). An id without the prefix is
    stored but never loaded back by the orchestrator, so the role vanishes."""
    return "custom:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def ensure_role(c, api, yes):
    """Create the app's own role (the agent's first role) if it does not exist.
    Like the built-in Agent role it has NO permissions: tool access comes only from
    the per-server grants (step 'grants'), so the role's reach is exactly the tool list.
    Returns None when the role exists, else a note (and records sts.role)."""
    global LAST_RESULTS
    own = c["identity"]["sts"]["agent"]["roles"][0]
    st, j = api.get("/api/v1/roles")
    if st != 200:
        raise RuntimeError(f"could not list roles (GET /api/v1/roles returned HTTP {st})")
    ensure_role.pending = False
    if any(own in (r.get("id"), r.get("name")) for r in api.items(j)):
        return None
    rid_ = role_id_for(own)
    if rid_ == "custom:":
        raise RuntimeError(f"cannot make a role id from '{own}' - rename the role")
    body = {"id": rid_, "name": own,
            "description": f"The app's own role for the Czarter '{c['metadata']['name']}'. "
                           "No permissions: its tools come from per-server grants.",
            "permissions": []}
    ensure_role.pending = not yes
    if not yes:
        print(f"would POST /api/v1/roles (role '{own}' does not exist yet) with:")
        print(json.dumps(body, indent=2))
        return f"would create role {own}"
    st, out = api.post("/api/v1/roles", body)
    if st not in (200, 201):
        err = (out or {}).get("error") if isinstance(out, dict) else api.last_error
        raise RuntimeError(f"could not create role '{own}' (HTTP {st})" + (f": {err}" if err else ""))
    LAST_RESULTS = [("sts.role", "done", f"created role {own} (id {rid_}), no permissions - tools come from grants")]
    return f"created role {own}"


def apply_agent(c, api, yes):
    """Create the agent's service account with its external ID, the same call the console makes."""
    a, name = c["identity"]["sts"]["agent"], c["metadata"]["name"]
    org = default_org(api)
    st, j = api.get(f"/api/v1/service-accounts?organization_id={org}")
    if st != 200:
        raise RuntimeError(f"could not list service accounts (HTTP {st})")
    row = next((r for r in api.items(j)
                if a["serviceAccount"] in (r.get("subject"), r.get("name")) and not r.get("revoked_at")), None)
    if row:
        ext = row.get("external_id")
        if ext and ext != a["externalId"]:
            return "failed", f"{a['serviceAccount']} already exists with external ID {ext}; Czarter says {a['externalId']} - not changed"
        # Repair the account's roles if they differ from the Czarter (e.g. it was
        # created with a role id the orchestrator never loaded).
        ensure_role(c, api, yes)
        if not yes and ensure_role.__dict__.get("pending"):
            want = [role_id_for(a["roles"][0])] + resolve_roles(api, a["roles"][1:])
        else:
            want = resolve_roles(api, a["roles"])
        have = row.get("role_ids") or row.get("roles") or []
        have = [h.get("id") if isinstance(h, dict) else h for h in have]
        if sorted(have) == sorted(want):
            return "done", f"{a['serviceAccount']} already exists with the right roles - nothing to do"
        body = {"role_ids": want}
        if not yes:
            print(f"would PATCH /api/v1/service-accounts/{row.get('id')} (roles now {have}) with:")
            print(json.dumps(body, indent=2))
            return None, "dry run - re-run with --yes to make them"
        st, out = api.patch(f"/api/v1/service-accounts/{row.get('id')}", body)
        if st not in (200, 204):
            err = (out or {}).get("error") if isinstance(out, dict) else api.last_error
            return "failed", f"could not update {a['serviceAccount']}'s roles (HTTP {st})" + (f": {err}" if err else "")
        return "done", f"{a['serviceAccount']} already existed - roles set to {', '.join(want)}"

    role_note = ensure_role(c, api, yes)
    if role_note is not None and not yes:
        print()
    if not yes and role_note:
        # The role is not there yet, so its id cannot be looked up: show its future id.
        roles = [role_id_for(a["roles"][0])] + resolve_roles(api, a["roles"][1:])
    else:
        roles = resolve_roles(api, a["roles"])
    body = {"name": a["serviceAccount"], "subject": a["serviceAccount"],
            "description": f"Agent identity for the Czarter '{name}'.",
            "role_ids": roles, "organization_id": org,
            "external_id": a["externalId"], "ttl_seconds": SA_TTL_DAYS * 24 * 3600}
    if not yes:
        print("would POST /api/v1/service-accounts with:")
        print(json.dumps(body, indent=2))
        return None, "dry run - re-run with --yes to create it"

    st, out = api.post("/api/v1/service-accounts", body)
    if st not in (200, 201) or not isinstance(out, dict):
        err = (out or {}).get("error") if isinstance(out, dict) else None
        return "failed", f"create returned HTTP {st}" + (f": {err}" if err else "")
    # The response carries a Magertron token for the account. The app does not use it
    # (it signs in with its IdP client credentials), but it is a live credential:
    # keep it private instead of printing or dropping it.
    tok = out.get("token")
    where = ""
    if tok:
        try:
            d = pathlib.Path("~/.czarter").expanduser(); d.mkdir(mode=0o700, parents=True, exist_ok=True)
            f = d / f"{a['serviceAccount']}.jwt"
            f.touch(mode=0o600, exist_ok=True); f.chmod(0o600); f.write_text(tok + "\n")
            where = f"; its Magertron token is in {f} (not used by the app - revoke it if you never need it)"
        except OSError as e:
            where = f"; could not save its Magertron token ({e}) - rotate the account if you ever need one"
    return "done", f"created {a['serviceAccount']} (id {out.get('id', '?')}), external ID {a['externalId']}, expires in {SA_TTL_DAYS} days{where}"


def apply_grants(c, api, yes):
    """Grant the agent's first role exactly the tools each connection lists
    (POST /api/v1/tool-rbac/servers/{ns}/{name}/permissions, as seeding.cpp does).
    Grants the role already has are skipped (read first), and a 409 counts as already there."""
    ns, name = c["metadata"]["namespace"], c["metadata"]["name"]
    role = resolve_roles(api, [c["identity"]["sts"]["agent"]["roles"][0]])[0]
    plan = [(x["name"], t) for x in c["connections"] for t in tools_of(x)]
    nsof = {x["name"]: cns(c, x) for x in c["connections"]}
    if not plan:
        return "skipped", "no connection lists tools"
    existing = {}
    for srv in {srv for srv, _ in plan}:
        st, j = api.get(f"/api/v1/tool-rbac/servers/{nsof[srv]}/{srv}/permissions")
        rows = (api.items(j) or (j.get("permissions") if isinstance(j, dict) else []) or []) if st == 200 else []
        existing[srv] = {r.get("tool_pattern") for r in rows if r.get("role_id") == role and r.get("effect") == "ALLOW"}
    todo = [(srv, t) for srv, t in plan if t not in existing.get(srv, set())]
    if not yes:
        print(f"would grant role {role} ({len(plan) - len(todo)} already there):")
        for srv, t in todo:
            print(f"  {nsof[srv]}/{srv}: {t}")
        return None, f"dry run - {len(todo)} new grant(s); re-run with --yes to make them"
    made, had, bad = 0, 0, []
    per = {}   # server -> [made, had, failed]
    for srv, t in plan:
        p_ = per.setdefault(srv, [0, 0, 0])
        if t in existing.get(srv, set()):
            had += 1; p_[1] += 1
            continue
        st, out = api.post(f"/api/v1/tool-rbac/servers/{nsof[srv]}/{srv}/permissions", {
            "role_id": role, "pattern_type": "WILDCARD", "tool_pattern": t, "effect": "ALLOW",
            "reason": f"Czarter '{name}': the agent uses this tool."})
        if st in (200, 201):
            made += 1; p_[0] += 1
        elif st == 409:
            had += 1; p_[1] += 1
        else:
            p_[2] += 1
            why = "server not found - deploy it, then run this again" if st == 404 else \
                  f"HTTP {st}" + (f" ({api.last_error[:80]})" if api.last_error else "")
            bad.append((srv, t, why))
    # One line per server, so the grants that worked are as visible as the ones that did not.
    for srv, (m, h, f) in per.items():
        tools = [t for s_, t in plan if s_ == srv]
        if f:
            whys = sorted({w for s_, _, w in bad if s_ == srv})
            print(f"❌ {nsof[srv]}/{srv}: {f} not granted ({', '.join(t for s_, t, _ in bad if s_ == srv)}) - {'; '.join(whys)}")
        else:
            print(f"✅ {nsof[srv]}/{srv}: {len(tools)} tool(s) granted ({m} new, {h} already there)")
    detail = f"{made} granted, {had} already there" + (f", {len(bad)} not granted (see above)" if bad else "")
    global LAST_RESULTS
    LAST_RESULTS = [(f"controls.scope.{srv}", "failed" if f else "done",
                     f"{m} granted, {h} already there" + (f", {f} failed" if f else ""))
                    for srv, (m, h, f) in per.items()]
    return ("partial" if bad and (made or had) else "failed" if bad else "done"), detail


def show_grants(c, api):
    """Read-only: list the tool permissions on each connection, as the platform stores them."""
    ns = c["metadata"]["namespace"]
    for x in c["connections"]:
        st, j = api.get(f"/api/v1/tool-rbac/servers/{cns(c, x)}/{x['name']}/permissions")
        if st != 200:
            print(f"{x['name']}: HTTP {st}" + (f" - {api.last_error[:400]}" if api.last_error else ""))
            continue
        rows = api.items(j) or (j.get("permissions") if isinstance(j, dict) else []) or []
        print(f"{x['name']}: {len(rows)} permission(s)")
        seen = {}
        for r in rows:
            k = (r.get("role_id"), r.get("tool_pattern"), r.get("effect"))
            seen[k] = seen.get(k, 0) + 1
        for (role, pat, eff), n in sorted(seen.items(), key=lambda kv: tuple(str(v) for v in kv[0])):
            print(f"  {eff or '?':5} {role or '?':28} {pat or '?'}" + (f"   x{n} DUPLICATE" if n > 1 else ""))


def users_by_name(api):
    """Every Magertron user by username. GET /users/{name} does not return external_id
    or the full role list; the LIST does, so page through it once."""
    users, st, page = {}, 0, 1
    while page <= 50:
        st, j = api.get(f"/api/v1/users?page={page}&size=200")
        items = (j.get("items") if isinstance(j, dict) else j) if st == 200 else None
        if not items:
            break
        for u in items:
            if isinstance(u, dict) and u.get("username"):
                users[u["username"]] = u
        if isinstance(j, dict) and j.get("total") is not None and len(users) >= int(j["total"]):
            break
        page += 1
    return users, st


def role_ids(u):
    """A user's role ids, whether the API lists them as strings or as {id, name} objects."""
    out = []
    for r in u.get("roles") or []:
        out.append(r.get("id") if isinstance(r, dict) else r)
    return [r for r in out if r]


def apply_people(c, api, yes):
    """Give each person the Czarter lists the app's own role - the role the tool grants
    go to. On a call made for a person, Magertron checks the PERSON's roles as well as
    the agent's, so without it every call they make fails with "on-behalf-of principal
    ... is not permitted". Adds the role; never removes one."""
    global LAST_RESULTS
    s = c["identity"]["sts"]
    try:
        role = resolve_roles(api, [s["agent"]["roles"][0]])[0]
    except RuntimeError:
        # The app's role is created by the Agent step, earlier in the same setup run. A preview runs
        # before it exists: show what will happen with the role's name, instead of failing (0.2.47).
        if yes:
            raise
        role = s["agent"]["roles"][0]
    users, st = users_by_name(api)
    if st not in (200, 0) and not users:
        raise RuntimeError(f"could not list users (GET /api/v1/users returned HTTP {st})")
    rows, todo = link_people(c, api, users, yes, role), []
    for x in s["people"]:
        if x["user"] == UNLINKED:
            continue                        # link_people said why
        u = users.get(x["user"])
        if u is None:
            rows.append((f"people.role.{x['user']}", "failed", f"no Magertron user {x['user']}"))
        elif role in role_ids(u):
            rows.append((f"people.role.{x['user']}", "done", f"{x['user']} already has {role}"))
        else:
            todo.append((x["user"], role_ids(u)))
    if not s["people"]:
        return "skipped", "the Czarter lists no people"
    if not yes:
        print(f"would add role {role} to:" if todo else f"everyone already has {role}")
        for name, have in todo:
            print(f"  {name} (has: {', '.join(have) or 'no roles'})")
        for step, st_, d in rows:
            print(f"{ {'done': '✅', 'pending': '·'}.get(st_, '❌') } {d}")
        if not todo and not any(r[1] == "pending" for r in rows):
            LAST_RESULTS = rows
            bad = [r for r in rows if r[1] == "failed"]
            return ("failed" if bad else "done"), (f"nothing to do - everyone listed already has {role}" if not bad
                                                    else f"{len(bad)} person(s) are not Magertron users")
        return None, f"dry run - {len(todo)} person(s) to give {role}, " \
                     f"{sum(r[1] == 'pending' for r in rows)} to link"
    from urllib.parse import quote
    for name, have in todo:
        st, out = api.put(f"/api/v1/users/{quote(name, safe='')}/roles", {"roles": have + [role]})
        ok = st in (200, 201, 204)
        rows.append((f"people.role.{name}", "done" if ok else "failed",
                     f"added {role} to {name}" if ok else
                     f"could not add {role} to {name} (HTTP {st}" + (f": {api.last_error[:80]}" if api.last_error else "") + ")"))
    for step, st_, d in rows:
        print(f"{'✅' if st_ == 'done' else '❌'} {d}")
    LAST_RESULTS = rows
    bad = [r for r in rows if r[1] == "failed"]
    good = [r for r in rows if r[1] == "done"]
    return ("partial" if bad and good else "failed" if bad else "done"), \
           f"{len(good)} of {len(rows)} have {role}"


def _find_user_by_email(users, email):
    """Magertron users whose username, email or SSO email is this email (case does not matter)."""
    e = email.strip().lower()
    return [u for name, u in users.items()
            if e in {str(v).strip().lower() for v in (name, u.get("email"), u.get("sso_email")) if v}]


AUTOMATED_IDPS = ("auth0", "keycloak")
PROV_NAME = {"auth0": "Auth0", "keycloak": "Keycloak", "okta": "Okta", "entra": "Entra ID"}


def idp_module(c):
    """The module that automates this Czarter's IdP: idp_auth0 or idp_keycloak (None: by hand)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    prov = (c.get("identity") or {}).get("idp", {}).get("provider")
    if prov == "keycloak":
        import idp_keycloak
        return idp_keycloak
    if prov == "auth0":
        import idp_auth0
        return idp_auth0
    return None


def _idp_users_by_email(c, email):
    """[(user_id, connection, verified)] for this email in the Czarter's IdP (Auth0 or Keycloak)."""
    import idp_auth0, urllib.parse
    i = c["identity"]
    if i["idp"]["provider"] == "keycloak":
        return idp_module(c).users_by_email(c, email)
    if i["idp"]["provider"] != "auth0":
        raise RuntimeError("people by email is automated for Auth0 and Keycloak only - give idpSubject and user instead")
    a0 = idp_auth0.Auth0(i["idp"]["domain"])
    a0.need(idp_auth0.SCOPES_PEOPLE, "look people up by email")
    _, j = a0.call("GET", "/api/v2/users-by-email?email=" + urllib.parse.quote(email))
    allowed = set(i["people"].get("connections") or [])
    out = []
    for u in j or []:
        conn = ((u.get("identities") or [{}])[0]).get("connection", "")
        if allowed and conn not in allowed:
            continue                        # this app cannot sign them in through that connection
        out.append((u.get("user_id", ""), conn, bool(u.get("email_verified"))))
    return out


def _new_username(users, email, name=""):
    """The new Magertron user's name (0.2.46): from the person's NAME when there is one ("Sam Lee" ->
    sam.lee), so an address like curtis+sam@... never becomes the name in every audit row; else the
    email's local part without any +tag (jane.rivera); else the email. Never one that is taken."""
    import unicodedata

    def slug(t):
        t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower().replace("'", "")
        t = re.sub(r"[^a-z0-9.\-]+", ".", t.strip())
        return re.sub(r"\.{2,}", ".", t).strip(".-")
    base = slug(name) or slug(email.split("@", 1)[0].split("+", 1)[0]) or email.strip().lower()
    if base not in users:
        return base
    for i in range(2, 100):                 # sam.lee is taken: sam.lee2, never someone else's name
        if f"{base}{i}" not in users:
            return f"{base}{i}"
    return email.strip().lower()


def _create_magertron_user(api, users, email, role, name=""):
    """A Magertron user for a person who has none, WITH the app's role (never a user with no roles).
    They sign in through the IdP, so the password is random and never shown or kept."""
    import secrets
    name = _new_username(users, email, name)
    st, out = api.post("/api/v1/users", {"username": name, "email": email, "roles": [role],
                                          "password": secrets.token_urlsafe(32) + "Aa1!"})
    if st not in (200, 201):
        raise RuntimeError(f"could not create the Magertron user {name} (HTTP {st}"
                           + (f": {api.last_error[:100]}" if api.last_error else "") + ")")
    users[name] = {"username": name, "email": email, "roles": [role], "external_id": ""}
    return name


def link_people(c, api, users, yes, role=None):
    """People's access, first half: make sure each person's IdP account and Magertron user are the same
    person (users.external_id = their IdP subject). A person given by email is looked up in both: exactly
    one match each, or it stops and asks - never a guess. An external_id that is already set to something
    else is never overwritten. Writes what it found back into the Czarter (user, idpSubject), keeping the email."""
    from urllib.parse import quote
    rows, linked = [], {}                  # linked: index -> (user, idpSubject) to write back - only real links
    for idx, x in enumerate(c["identity"]["sts"]["people"]):
        email, who = x.get("email", ""), x.get("email") or x["user"]
        step = f"people.link.{who}"
        was = (x["user"], x["idpSubject"])
        todo = []                          # what a dry run would create for this person
        create_mg = False
        if x["user"] == UNLINKED:
            hit = _find_user_by_email(users, email)
            if len(hit) != 1:
                # Several users share the email (test accounts), or none has it: a person whose name IS an
                # existing username, exactly ("mags"), is that user (0.2.54). The Czarter names them so.
                byname = users.get(str(x.get("name") or "").strip())
                if byname:
                    hit = [byname]
            if len(hit) > 1:
                rows.append((step, "failed", f"{len(hit)} Magertron users have the email {email} ("
                                             + ", ".join(u["username"] for u in hit) + ") - name the right one as user"))
                continue
            if hit:
                x["user"] = hit[0]["username"]
            elif not role:
                rows.append((step, "failed", f"no Magertron user has the email {email}")); continue
            else:
                create_mg = True
                todo.append(f"create the Magertron user {_new_username(users, email, x.get('name', ''))} with {role}")
        made_in_idp = False
        # Already linked in Magertron (0.2.52): its external_id IS the person's IdP account - use it.
        # Looking the email up again found two accounts (password and Google) and asked which.
        if x["idpSubject"] == UNLINKED and x["user"] != UNLINKED:
            have = (users.get(x["user"]) or {}).get("external_id") or ""
            if have:
                x["idpSubject"] = have
        if x["idpSubject"] == UNLINKED:
            try:
                ids = _idp_users_by_email(c, email)
            except Exception as e:  # noqa: BLE001
                rows.append((step, "failed", f"could not look {email} up in the IdP: {e}")); x["user"], x["idpSubject"] = was; continue
            if not ids and c["identity"]["people"].get("createMissing"):
                if not yes:
                    todo.append(f"create {email} in the IdP (they get a set-your-password email)")
                else:
                    try:
                        made = idp_module(c).create_person(c, email, x.get("name", ""))
                    except Exception as e:  # noqa: BLE001
                        rows.append((step, "failed", f"could not create {email} in the IdP: {e}"))
                        x["user"], x["idpSubject"] = was; continue
                    sub, conn = made[0], made[1]
                    told = made[2] if len(made) > 2 else "Auth0 emailed them a link to set their password"
                    ids, made_in_idp = [(sub, conn, True)], True     # created by Czartup: only the inbox owner can sign in
                    rows.append((f"people.idp.{who}", "done", f"created {email} in the IdP ({conn}); {told}"))
            if todo and not yes:
                rows.append((step, "pending", "would " + ", ".join(todo) + f", and link them"))
                x["user"], x["idpSubject"] = was; continue
            if len(ids) != 1:
                rows.append((step, "failed", (f"no IdP account with the email {email} that this app can sign in - create it in the IdP, or set "
                              "identity.people.createMissing: true so Czartup creates it" if not ids else
                             f"{len(ids)} IdP accounts have the email {email}: "
                             + "; ".join(f"{sub} ({conn}{', verified' if v else ', NOT verified'})" for sub, conn, v in ids)
                             + " - set idpSubject to the one this person signs in with")))
                continue
            if not ids[0][2] and not made_in_idp:
                # An unverified email proves nothing about who owns the account: never link it.
                rows.append((step, "failed", f"{email}'s IdP account ({ids[0][0]}) is not verified - verify it in the "
                                             "IdP, then run People's access again"))
                x["user"], x["idpSubject"] = was
                continue
            x["idpSubject"] = ids[0][0]
        if todo and not yes:
            rows.append((step, "pending", "would " + ", ".join(todo) + ", and link them"))
            x["user"], x["idpSubject"] = was; continue
        if create_mg:
            try:
                x["user"] = _create_magertron_user(api, users, email, role, x.get("name", ""))
            except RuntimeError as e:
                rows.append((step, "failed", str(e))); x["user"], x["idpSubject"] = was; continue
            rows.append((f"people.user.{who}", "done", f"created the Magertron user {x['user']} with {role}"))
        u = users.get(x["user"])
        if u is None:
            rows.append((step, "failed", f"no Magertron user {x['user']}")); continue
        have = u.get("external_id") or ""
        if have == x["idpSubject"]:
            rows.append((step, "done", f"{x['user']} is {x['idpSubject']} in the IdP"))
            linked[idx] = (x["user"], x["idpSubject"])
        elif have:
            rows.append((step, "failed", f"{x['user']} is already linked to {have}, not {x['idpSubject']} - "
                                         "change it in the console if that is intended; Czartup never overwrites it"))
        elif not yes:
            rows.append((step, "pending", f"would link {x['user']} to the IdP account {x['idpSubject']}"))
        else:
            st, _ = api.put(f"/api/v1/users/{quote(x['user'], safe='')}/external-id", {"external_id": x["idpSubject"]})
            ok = st in (200, 201, 204)
            if ok:
                u["external_id"] = x["idpSubject"]
                linked[idx] = (x["user"], x["idpSubject"])
            else:
                x["user"], x["idpSubject"] = was
            rows.append((step, "done" if ok else "failed",
                         f"linked {x['user']} to {x['idpSubject']}" if ok else f"could not link {x['user']} (HTTP {st})"))
    # Write back only links that are real now (done), and only where the Czarter named the person by email.
    fill = {i: v for i, v in linked.items() if c["identity"]["sts"]["people"][i].get("email")}
    # A hold's approvers name people by the username their name gives (Dana Ortiz -> dana.ortiz). When
    # that name already belonged to someone else, the person got another one (dana.ortiz2) - and the
    # hold must follow them, or it would name the other person (0.2.66).
    moved = {}
    for i, (user, _) in fill.items():
        x = c["identity"]["sts"]["people"][i]
        natural = _new_username({}, x.get("email", ""), x.get("name", ""))
        if natural != user and natural in users and users[natural].get("email", "").lower() != x.get("email", "").lower():
            moved[natural] = user
    if moved:
        for h in (c.get("controls") or {}).get("holds") or []:
            h["approvers"] = [moved.get(a, a) for a in h.get("approvers") or []]
        for old, new in moved.items():
            rows.append((f"people.approver.{new}", "done", f"the holds name {new}, not {old}: {old} is another person"))
    if (fill or moved) and yes and CZARTER_FILE:
        doc = yaml.safe_load(pathlib.Path(CZARTER_FILE).read_text())
        for i, (user, sub) in fill.items():
            d = doc["identity"]["sts"]["people"][i]
            if d.get("user") == user and d.get("idpSubject") == sub:
                continue
            d["user"], d["idpSubject"] = user, sub
        for h in (doc.get("controls") or {}).get("holds") or []:
            h["approvers"] = [moved.get(a, a) for a in h.get("approvers") or []]
        pathlib.Path(CZARTER_FILE).write_text("# Czarter - written by Magertron Czartup(TM)\n"
                                              + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
    return rows


def _inventory_urls(api, name):
    """The https addresses Inventory holds for a server of this name (SI submissions)."""
    from urllib.parse import quote
    st, j = api.get(f"/api/v1/inventory/servers?limit=50&q={quote(name)}")
    items = (j.get("items") if isinstance(j, dict) else j) if st == 200 else None
    if items is None:
        raise RuntimeError(f"could not read Inventory (GET /api/v1/inventory/servers returned HTTP {st})")
    rows = [r for r in items if isinstance(r, dict) and r.get("name") == name and r.get("endpoint_url")
            and r.get("state") not in ("dismissed", "deprecated")]
    return sorted({r["endpoint_url"] for r in rows})


def _register_servers(c, api, yes):
    """Register the servers the SI built, from their Inventory submission: external, in the
    Czarter's namespace, Trusted upstream exactly as the Czarter says - then approve.
    A server already registered with the wrong Trusted upstream setting is corrected (a trust
    change, so it is approved again). Only the app's own namespace; never an LLM or a vendor
    connection in another namespace."""
    global LAST_RESULTS
    from urllib.parse import quote
    own = c["metadata"]["namespace"]
    rows, plan = [], []
    for x in c["connections"]:
        if cns(c, x) != own or x.get("kind") == "llm":
            continue
        name, fwd = x["name"], bool(x.get("forwardIdentity"))
        step = f"register.{name}"
        st, cur = api.get(f"/api/v1/servers/{own}/{quote(name)}")
        if st == 200 and isinstance(cur, dict):
            if bool(cur.get("forward_identity")) == fwd:
                rows.append((step, "done", f"{own}/{name} is registered (Trusted upstream {'on' if fwd else 'off'})"))
            else:
                plan.append(("fix", x, cur.get("endpoint_url") or ""))
            continue
        try:
            urls = _inventory_urls(api, name)
        except RuntimeError as e:
            rows.append((step, "failed", str(e))); continue
        https = [u for u in urls if u.lower().startswith("https://")]
        if not urls:
            rows.append((step, "failed", f"no submission for {name} in Inventory yet - the SI submits its server.json "
                                         "(right-click > Magertron: Submit), then run this again"))
        elif not https:
            rows.append((step, "failed", f"{name} was submitted at {urls[0]} - the gateway only routes https://; "
                                         "ask the SI to serve it over HTTPS and submit again"))
        elif len(https) > 1:
            rows.append((step, "failed", f"{name} has {len(https)} different submitted addresses ({', '.join(https)}) - "
                                         "dismiss the stale ones in Inventory, then run this again"))
        else:
            plan.append(("new", x, https[0]))
    if not plan:
        LAST_RESULTS = rows
        bad = [r for r in rows if r[1] == "failed"]
        for _, st_, d in rows:
            print(f"{'✅' if st_ == 'done' else '❌'} {d}")
        if not rows:
            return "skipped", "no server of this app's own namespace to register"
        return ("failed" if bad and len(bad) == len(rows) else "partial" if bad else "done"), \
               ("nothing to do - " if not bad else "") + f"{len(rows) - len(bad)} of {len(rows)} registered"
    if not yes:
        for kind, x, url in plan:
            on = "on" if x.get("forwardIdentity") else "off"
            if kind == "new":
                print(f"would register {own}/{x['name']} at {url} as an external server, Trusted upstream {on}, and approve it")
            else:
                print(f"would turn Trusted upstream {on} on {own}/{x['name']} (a trust change) and approve it again")
        for _, st_, d in rows:
            print(f"{'✅' if st_ == 'done' else '❌'} {d}")
        return None, f"dry run - {len(plan)} server(s) to register or correct"
    for kind, x, url in plan:
        name, fwd, step = x["name"], bool(x.get("forwardIdentity")), f"register.{x['name']}"
        if kind == "new":
            st, out = api.post("/api/v1/servers", {
                "type": "external", "name": name, "namespace": own, "endpoint_url": url,
                "auth_type": "none", "credential_mode": "centralized", "routing_mode": "v3",
                "upstream_http_version": "http1", "tls_min_version": "tls1_2", "forward_identity": fwd})
            what = f"registered {own}/{name} at {url}"
        else:
            st, out = api.put(f"/api/v1/servers/{own}/{quote(name)}/external", {"forward_identity": fwd})
            what = f"turned Trusted upstream {'on' if fwd else 'off'} on {own}/{name}"
        if st not in (200, 201):
            rows.append((step, "failed", f"could not {('register' if kind == 'new' else 'update')} {own}/{name} "
                                         f"(HTTP {st}" + (f": {api.last_error[:100]}" if api.last_error else "") + ")"))
            continue
        st2, _ = api.post(f"/api/v1/servers/{own}/{quote(name)}/approve", {})
        if st2 in (200, 201, 204):
            rows.append((step, "done", f"{what}" + (f", Trusted upstream {'on' if fwd else 'off'}" if kind == "new" else "") + ", approved"))
        else:
            rows.append((step, "partial", f"{what}, but approving it returned HTTP {st2}"
                                          + (f" ({api.last_error[:80]})" if api.last_error else "") + " - approve it in the console"))
    for _, st_, d in rows:
        print({"done": "✅", "partial": "⚠️"}.get(st_, "❌") + f" {d}")
    LAST_RESULTS = rows
    bad = [r for r in rows if r[1] != "done"]
    return ("done" if not bad else "failed" if len(bad) == len(rows) else "partial"), \
           f"{len(rows) - len(bad)} of {len(rows)} registered and approved"


def apply_register(c, api, yes):
    """Register servers, then give the app's servers the app's icon (if the Czarter has one)."""
    global LAST_RESULTS
    st, detail = _register_servers(c, api, yes)
    rows = app_icons(c, api, yes)
    if rows:
        LAST_RESULTS = list(LAST_RESULTS or []) + rows
        done = sum(1 for r in rows if r[1] == "done")
        if done:
            detail += f"; icon on {done} server(s)"
    return st, detail


# ---------- the app's icon (backlog 51) ----------
# Stored beside the Czarter: <name>.icon.png (the app), <name>.<server>.icon.png (one server's own).
# <name>.icons.json records the sha256 of every icon Czartup set, per server, so it replaces only
# its OWN earlier icon - never one a person chose in the console.
CZARTER_FILE = None   # set by main(): where the Czarter (and so its icon) lives


def icon_paths(name, folder=None):
    d = pathlib.Path(folder or (pathlib.Path(CZARTER_FILE).parent if CZARTER_FILE else "."))
    return d / f"{name}.icon.png", d / f"{name}.icons.json"


def icon_for(c, server, folder=None):
    """The icon bytes for one server: its own override, else the app's. None when there is none."""
    name = c["metadata"]["name"]
    app, _ = icon_paths(name, folder)
    own = app.with_name(f"{name}.{server}.icon.png")
    for f in (own, app):
        if f.exists():
            return f.read_bytes()
    return None


def check_icon(data):
    """None when data is a square PNG of at least 256 px (1024 x 1024 recommended); else why not."""
    import struct
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return "not a PNG"
    w, h = struct.unpack(">II", data[16:24])
    if w != h:
        return f"not square ({w} x {h})"
    if w < 256:
        return f"too small ({w} x {h}) - 1024 x 1024 is best"
    if len(data) > 2_000_000:
        return f"too large ({len(data) // 1024} KB) - keep it under 2 MB"
    return None


def app_icons(c, api, yes):
    """Set the app's icon on each of its own servers that is registered - new or already there."""
    import base64, hashlib
    from urllib.parse import quote
    name, own = c["metadata"]["name"], c["metadata"]["namespace"]
    _, rec_f = icon_paths(name)
    rec = json.loads(rec_f.read_text()) if rec_f.exists() else {}
    rows, changed = [], False
    for x in c["connections"]:
        if cns(c, x) != own or x.get("kind") == "llm":
            continue
        srv, step = x["name"], f"icon.{x['name']}"
        img = icon_for(c, srv)
        if img is None:
            continue
        want = hashlib.sha256(img).hexdigest()
        st, _ = api.get(f"/api/v1/servers/{own}/{quote(srv)}")
        if st != 200:
            continue                                   # not registered yet - the next run sets it
        st, cur = api.get_bytes(f"/api/v1/servers/{own}/{quote(srv)}/icon")
        have = hashlib.sha256(cur).hexdigest() if st == 200 and cur else None
        if have == want:
            continue
        if have and have != rec.get(srv):
            print(f"⏭ {own}/{srv} has an icon someone chose in the console - left as it is")
            continue
        if not yes:
            print(f"would set the app's icon on {own}/{srv}")
            continue
        st, _ = api.put(f"/api/v1/servers/{own}/{quote(srv)}/icon", {"image_base64": base64.b64encode(img).decode()})
        if st in (200, 201, 204):
            rec[srv], changed = want, True
            rows.append((step, "done", f"icon set on {own}/{srv}"))
            print(f"✅ icon set on {own}/{srv}")
        else:
            rows.append((step, "failed", f"could not set the icon on {own}/{srv} (HTTP {st})"))
            print(f"⚠️ could not set the icon on {own}/{srv} (HTTP {st}) - set it on the server's page")
    if changed:
        rec_f.write_text(json.dumps(rec, indent=1) + "\n")
    return rows


def _tool_defs(api, status):
    """Inventory tool definitions with this status, all pages."""
    from urllib.parse import quote
    out, cursor = [], None
    for _ in range(20):
        st, j = api.get(f"/api/v1/inventory/tools?status={status}" + (f"&cursor={quote(cursor)}" if cursor else ""))
        if st != 200 or not isinstance(j, dict):
            raise RuntimeError(f"could not read tool definitions (GET /api/v1/inventory/tools returned HTTP {st})")
        out += j.get("items") or []
        cursor = j.get("next_cursor")
        if not cursor:
            break
    return out


def apply_tools(c, api, yes):
    """Approve the NEW tool definitions of the tools this Czarter lists, on its own servers.
    Never: a tool the Czarter does not list (left pending for a person to look at), or a
    DRIFTED definition - one that changed after it was approved, the rug pull this exists for."""
    global LAST_RESULTS
    want = {(cns(c, x), x["name"]): set(tools_of(x)) for x in c["connections"] if tools_of(x)}
    pending = _tool_defs(api, "pending")
    drifted = _tool_defs(api, "drifted")
    # A server registered a moment ago (the step before this one) has no tool definitions yet: the
    # gateway learns them when it probes the server. Probe now and wait, so its tools are approved in
    # THIS run instead of needing setup again (0.2.51).
    if yes:
        from urllib.parse import quote as _q
        def seen():
            out = set()
            for st_ in ("pending", "drifted", "approved"):
                try:
                    out |= {(d.get("server_namespace"), d.get("server_name"), d.get("tool_name"))
                            for d in _tool_defs(api, st_)}
                except RuntimeError:          # a status this Magertron does not list - skip it
                    pass
            return out
        known = seen()
        late = [k for k, tools in want.items() if any((k[0], k[1], t) not in known for t in tools)
                and k[0] == c["metadata"]["namespace"]]
        for ns_, srv in late:
            api.post(f"/api/v1/servers/{_q(ns_, safe='')}/{_q(srv, safe='')}/probe", {})
        deadline = time.time() + 30
        while late and time.time() < deadline:
            time.sleep(2)
            known = seen()
            late = [k for k in late if any((k[0], k[1], t) not in known for t in want[k])]
        pending = _tool_defs(api, "pending")
        drifted = _tool_defs(api, "drifted")
    mine = lambda d: (d.get("server_namespace"), d.get("server_name")) in want
    todo = [d for d in pending if mine(d) and d.get("tool_name") in want[(d["server_namespace"], d["server_name"])]]
    extra = [d for d in pending if mine(d) and d.get("tool_name") not in want[(d["server_namespace"], d["server_name"])]]
    drift = [d for d in drifted if mine(d)]
    notes = [f"left pending: {d['server_namespace']}/{d['server_name']} {d['tool_name']} (not in the Czarter - "
             "review it in Inventory > Tool Approvals)" for d in extra]
    notes += [f"DRIFTED, not approved: {d['server_namespace']}/{d['server_name']} {d['tool_name']} changed after it "
              "was approved - review the change in Inventory > Tool Approvals" for d in drift]
    if not todo:
        for n in notes:
            print(f"⚠️ {n}")
        LAST_RESULTS = [("tools.approve", "partial" if drift else "done",
                         "nothing to approve" + (f"; {len(drift)} drifted need a person" if drift else ""))]
        return LAST_RESULTS[0][1], LAST_RESULTS[0][2]
    if not yes:
        print(f"would approve {len(todo)} new tool definition(s) the Czarter lists:")
        for d in todo:
            print(f"  {d['server_namespace']}/{d['server_name']}: {d['tool_name']}")
        for n in notes:
            print(f"⚠️ {n}")
        return None, f"dry run - {len(todo)} definition(s) to approve"
    from urllib.parse import quote
    ok, bad = 0, []
    for d in todo:
        st, _ = api.post(f"/api/v1/inventory/tools/{quote(str(d['id']), safe='')}/approve", {})
        if st in (200, 201, 204):
            ok += 1
            print(f"✅ approved {d['server_namespace']}/{d['server_name']} {d['tool_name']}")
        else:
            bad.append(d)
            print(f"❌ {d['server_namespace']}/{d['server_name']} {d['tool_name']}: HTTP {st}")
    for n in notes:
        print(f"⚠️ {n}")
    detail = f"{ok} approved" + (f", {len(bad)} failed" if bad else "") + (f"; {len(drift)} drifted need a person" if drift else "")
    st = "failed" if bad and not ok else "partial" if bad or drift else "done"
    LAST_RESULTS = [("tools.approve", st, detail)]
    return st, detail


LAST_RESULTS = None   # set by a step that records several run-log rows

def apply_scope(c, api, yes):
    """External Policy is asked about the app's namespaces (0.2.44): without that, every limit,
    hold and agent-alone rule of the app is skipped - the PDP is never consulted for its calls.
    Adds them through POST /api/v1/governance/pdp/namespaces/{ns} (Magertron 4.2.89+); never
    removes one."""
    global LAST_RESULTS
    ctl = c.get("controls") or {}
    if not (ctl.get("limits") or ctl.get("holds") or alone_tools(c) is not None):
        return "skipped", "the Czarter has no limits, holds or agent-alone rules - External Policy is not needed"
    st, j = api.get("/api/v1/governance/pdp")
    if st != 200 or not isinstance(j, dict):
        return "failed", f"could not read External Policy (GET /api/v1/governance/pdp returned HTTP {st})"
    if not j.get("configured", True):
        return "failed", "External Policy is not configured on this Magertron - install with --enable-opa"
    scope = str(j.get("namespaces") or "*").strip()
    have = {n.strip() for n in scope.split(",") if n.strip()}
    want = sorted(app_namespaces(c))
    todo = [] if scope == "*" else [n for n in want if n not in have]
    if not todo:
        LAST_RESULTS = [("policy.scope", "done", f"External Policy is asked about {', '.join(want)} ({scope})")]
        return "done", f"nothing to do - External Policy already covers {', '.join(want)}"
    if not j.get("namespaces_editable"):
        return "failed", (f"{', '.join(todo)} not in External Policy's namespaces ({scope}), and this Magertron cannot "
                          f"change them live - install.sh --pdp-namespaces \"{scope},{','.join(todo)}\"")
    if not yes:
        print(f"would add {', '.join(todo)} to External Policy's namespaces ({scope})")
        return None, f"dry run - would add {', '.join(todo)}"
    from urllib.parse import quote
    rows = []
    for n in todo:
        st, out = api.post(f"/api/v1/governance/pdp/namespaces/{quote(n, safe='')}", {})
        ok = st in (200, 201)
        rows.append(("policy.scope", "done" if ok else "failed",
                     f"added {n} to External Policy's namespaces" if ok else
                     f"could not add {n} (HTTP {st}" + (f": {api.last_error[:400]}" if api.last_error else "") + ")"))
    LAST_RESULTS = rows
    bad = [r for r in rows if r[1] == "failed"]
    for _, st_, d in rows:
        print(("✅ " if st_ == "done" else "❌ ") + d)
    return ("failed" if bad else "done"), "; ".join(r[2] for r in rows)


def unregister_server(api, ns, n):
    """Unregister a server. One with billing records can't be deleted without destroying its billed
    usage (Magertron refuses, and the history is an audit record): it is disabled instead - no call
    reaches it any more - and its billing stays. Returns True, a message for what was done, or False."""
    from urllib.parse import quote
    st, _ = api.delete(f"/api/v1/servers/{ns}/{quote(n)}")
    if st in (200, 204, 404):
        return True
    if "billing record" in (api.last_error or ""):
        api.last_error = ""
        st2, _ = api.post(f"/api/v1/servers/{ns}/{quote(n)}/disable", {})
        if st2 in (200, 201, 204, 409):
            return (f"disabled the server {ns}/{n} - it has billing history, which Magertron keeps, so it is "
                    "not deleted; no call reaches it any more")
        return False
    return False


def registered_by_czartup(name):
    """The servers Czartup registered for this Czarter, from its run log (<name>.runs.json beside
    it): the register step's "registered <ns>/<server> at <url>" rows. None when there is no log
    to tell (a Czarter retired from somewhere else) - then every server of its namespace counts."""
    if not CZARTER_FILE:
        return None
    f = pathlib.Path(CZARTER_FILE).expanduser().with_name(f"{name}.runs.json")
    if not f.exists():
        return None
    try:
        runs = json.loads(f.read_text())
    except ValueError:
        return None
    out = set()
    for r in runs if isinstance(runs, list) else []:
        st, d = str(r.get("step", "")), str(r.get("detail", ""))
        if st.startswith("register.") and r.get("status") == "done" and d.startswith("registered "):
            out.add(st[len("register."):])
    return out


def retire(c, api, yes, keep_servers=False, other_namespaces=(), idp=False):
    """Retire an app (0.2.55): remove what setup created for it, in the safe order, so nothing of it is
    left running or able to act. Preview by default; --yes to do it. Never touches another app's things:
      1. the app's policy ConfigMap (OPA drops its rules within seconds);
      2. its namespace from External Policy - unless another Czarter still uses that namespace;
      3. the app role's tool grants on its servers;
      4. the app role from each of its people (the people themselves stay - they may use other apps);
      5. the agent's service account;
      6. the app's role;
      7. its own servers (the app's namespace only, and only those its setup registered) - unless --keep-servers.
    With idp (0.2.59), first: the app's Auth0 application, its MFA Action and its client secret in Vault -
    only what Czartup made for this Czarter (idp_auth0.retire_plan). The people's IdP accounts always stay.
    The Czarter itself is removed by Czartup once this succeeds."""
    from urllib.parse import quote
    name, ns = c["metadata"]["name"], c["metadata"]["namespace"]
    s = c["identity"]["sts"]
    rows, plan = [], []

    # 0. the IdP (--idp): first, so no one can sign in to the app or get its agent a token while the rest goes
    idp_err = None
    if idp:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import idp_auth0
        mod = idp_module(c) or idp_auth0          # anything else: idp_auth0 says "remove it by hand"
        who = "Keycloak" if mod is not idp_auth0 else "Auth0"
        try:
            p2, notes = mod.retire_plan(c)
            plan += [("idp", w, f) for w, f in p2]
            rows += [("retire.idp", "done", n) for n in notes]
        except (idp_auth0.Stop, KeyError, TypeError) as e:
            idp_err = f"{who} was not changed: {e}"
            rows.append(("retire.idp", "pending", idp_err))

    # 1. policy
    cm = cm_name(c)
    if K8S_POLICIES:
        st, _ = _kube("GET", f"/api/v1/namespaces/{OPA_NS}/configmaps/{cm}")
        if st == 200:
            plan.append(("policy", f"delete the policy ConfigMap {OPA_NS}/{cm}",
                         lambda: _kube("DELETE", f"/api/v1/namespaces/{OPA_NS}/configmaps/{cm}")[0] in (200, 202, 404)))
    else:
        rows.append(("retire.policy", "pending", f"admin: kubectl delete configmap {cm} -n {OPA_NS}"))

    # 2. External Policy namespace
    st, j = api.get("/api/v1/governance/pdp")
    scope = str((j or {}).get("namespaces") or "*") if st == 200 and isinstance(j, dict) else "*"
    if scope != "*" and ns in {n.strip() for n in scope.split(",")}:
        if ns in other_namespaces:
            rows.append(("retire.scope", "done", f"{ns} stays in External Policy - another Czarter uses it"))
        elif (j or {}).get("namespaces_editable"):
            plan.append(("scope", f"remove {ns} from External Policy's namespaces",
                         lambda: api.delete(f"/api/v1/governance/pdp/namespaces/{quote(ns, safe='')}")[0] in (200, 204)))

    # 3. grants, 6. role
    role = None
    try:
        role = resolve_roles(api, [s["agent"]["roles"][0]])[0]
    except (RuntimeError, IndexError, KeyError):
        rows.append(("retire.role", "done", "the app's role does not exist - nothing granted to it"))
    if role:
        for x in c.get("connections") or []:
            if x.get("kind") == "llm":
                continue
            st, j = api.get(f"/api/v1/tool-rbac/servers/{cns(c, x)}/{x['name']}/permissions")
            perms = (api.items(j) or (j.get("permissions") if isinstance(j, dict) else []) or []) if st == 200 else []
            mine = [r for r in perms if r.get("role_id") == role and r.get("id")]
            if mine:
                ids = [r["id"] for r in mine]
                plan.append(("grants", f"revoke {len(ids)} tool grant(s) of {role} on {x['name']}",
                             lambda ids=ids: all(api.delete(f"/api/v1/tool-rbac/permissions/{quote(str(i), safe='')}")[0]
                                                 in (200, 204, 404) for i in ids)))

    # 4. people keep their accounts, lose the app's role
    if role:
        users, _ = users_by_name(api)
        for x in s.get("people") or []:
            u = users.get(x.get("user")) if x.get("user") not in (None, UNLINKED) else None
            if not u:
                hit = _find_user_by_email(users, x.get("email", ""))
                u = hit[0] if len(hit) == 1 else None
            if u and role in role_ids(u):
                keep = [r for r in role_ids(u) if r != role]
                un = u["username"]
                plan.append(("people", f"take {role} from {un} (the user stays)",
                             lambda un=un, keep=keep: api.put(f"/api/v1/users/{quote(un, safe='')}/roles",
                                                              {"roles": keep})[0] in (200, 201, 204)))

    # 5. agent
    org = default_org(api)
    st, j = api.get(f"/api/v1/service-accounts?organization_id={org}")
    sa = next((r for r in (api.items(j) if st == 200 else []) or []
               if s["agent"]["serviceAccount"] in (r.get("subject"), r.get("name")) and not r.get("revoked_at")), None)
    if sa and sa.get("id"):
        plan.append(("agent", f"delete the agent's service account {s['agent']['serviceAccount']}",
                     lambda: api.delete(f"/api/v1/service-accounts/{quote(str(sa['id']), safe='')}")[0] in (200, 204, 404)))
    if role:
        plan.append(("role", f"delete the role {role}",
                     lambda: api.delete(f"/api/v1/roles/{quote(role, safe='')}")[0] in (200, 204, 404)))

    # 7. own servers - only those THIS Czarter registered. A server that was already registered when
    # setup ran (a trial's sample servers, a platform server, another team's) is someone else's: its
    # run log says "... is registered", not "registered ... at ...", and it stays.
    mine = registered_by_czartup(name)
    for x in c.get("connections") or []:
        if cns(c, x) != ns or x.get("kind") == "llm":
            continue
        st, _ = api.get(f"/api/v1/servers/{ns}/{quote(x['name'])}")
        if st == 200:
            if keep_servers:
                rows.append(("retire.servers", "done", f"kept the server {ns}/{x['name']} (--keep-servers)"))
            elif mine is not None and x["name"] not in mine:
                rows.append(("retire.servers", "done", f"kept the server {ns}/{x['name']}: it was registered before "
                                                       f"{name}'s setup ran, so it is not {name}'s to remove"))
            else:
                plan.append(("servers", f"unregister the server {ns}/{x['name']} (or disable it, if it has billing history)",
                             lambda n=x["name"]: unregister_server(api, ns, n)))

    left = ("left in place: the people's IdP accounts - they may use other apps" if idp else
            f"left in place: the {PROV_NAME.get(c['identity']['idp'].get('provider'), 'IdP')} app {name} "
            f"({c['identity']['app'].get('clientId') or 'no client ID'}) and the "
            "people's IdP accounts - add --idp to remove the app from the IdP too")
    if not yes:
        print(f"Retiring {name} would:" if plan else f"Nothing of {name} is left on the platform.")
        for _, what, _f in plan:
            print(f"  - {what}")
        for _, st_, d in rows:
            print(f"  {'·' if st_ == 'pending' else '✓'} {d}")
        print(f"  ({left})")
        return None, f"dry run - {len(plan)} change(s)"
    bad = 0
    for key, what, do in plan:
        try:
            ok = do()
        except Exception as e:  # noqa: BLE001
            ok, what = False, f"{what} ({e})"
        if isinstance(ok, str):                 # done, but not as planned: say what was done instead
            ok, what = True, ok
        rows.append((f"retire.{key}", "done" if ok else "failed", what if ok else f"could not {what}"
                     + (f": {api.last_error[:400]}" if api.last_error else "")))
        bad += not ok
        print(("✅ " if ok else "❌ ") + rows[-1][2])
    if idp_err:                                 # asked to clear Auth0 and could not: not finished, keep the Czarter
        print(f"❌ {idp_err} - fix it and retire again with --idp")
        bad += 1
    print(f"ℹ️ {left}")
    return ("failed" if bad else "done"), (f"{len(plan) - bad} of {len(plan)} removed" + (f", {bad} failed" if bad else ""))


APPLY_STEPS = {"policy.install": apply_policy, "policy.scope": apply_scope, "sts.agent": apply_agent, "grants": apply_grants, "people.roles": apply_people,
               "servers.register": apply_register, "tools.approve": apply_tools}


def apply_idp(a):
    """The IdP steps work on an unfinished Czarter: they are what fills in the Client ID."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import idp_auth0
    path = pathlib.Path(a.czarter).expanduser()
    c = yaml.safe_load(path.read_text())
    mod = idp_module(c) or idp_auth0          # anything else: idp_auth0 answers "skipped"

    def save(doc):
        path.write_text("# Czarter - written by Magertron Czartup(TM)\n"
                        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
    try:
        if a.step == "idp.app":
            st, detail, rows = mod.apply_app(c, a.yes, save)
        else:
            st, detail, rows = mod.apply_mfa(c, a.yes)
    except (idp_auth0.Stop, KeyError, TypeError) as e:
        st, detail, rows = "failed", (f"the Czarter is missing {e}" if isinstance(e, KeyError) else str(e)), []
    print(f"{ {'done': '✅', 'failed': '❌', 'skipped': '⏭', 'partial': '⚠️'}.get(st, '·') } {a.step}: {detail}")
    if st and a.record:
        # Always record the step's OWN row too (0.2.43): a run that failed before any sub-row
        # (e.g. Vault sealed) wrote only "idp.app" - and a later success that writes only sub-rows
        # never replaced it, so "Next:" kept pointing at a failure that was long fixed.
        rows = list(rows or [])
        if not any(r[0] == a.step for r in rows):
            rows.append((a.step, st, detail))
        record(rows, a.record, by="czartup-idp")
    return 1 if st == "failed" else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("plan"); a1.add_argument("czarter"); a1.add_argument("--out", default=str(HERE / "generated"))
    a1.add_argument("--draft", action="store_true", help="allow an unfinished Czarter; missing values become placeholders")
    a2 = sub.add_parser("verify"); a2.add_argument("czarter"); a2.add_argument("--policy", default=POLICY)
    a2.add_argument("--record", metavar="RUNS_JSON")
    a2.add_argument("--api", metavar="CONTROL_URL",
                    help="also check servers and the service account through the orchestrator API, e.g. https://app.magertron.com")
    a2.add_argument("--token-file", default="~/.czarter/operator.jwt",
                    help="operator JWT for --api (chmod 600, never printed)")
    a3 = sub.add_parser("apply"); a3.add_argument("czarter")
    a3.add_argument("--step", required=True, choices=sorted(APPLY_STEPS) + ["idp.app", "idp.mfa"])
    a3.add_argument("--api", metavar="CONTROL_URL", help="needed for the Magertron steps")
    a3.add_argument("--token-file", default="~/.czarter/operator.jwt")
    a3.add_argument("--yes", action="store_true", help="make the change (default: dry run)")
    a3.add_argument("--record", metavar="RUNS_JSON")
    a5 = sub.add_parser("retire"); a5.add_argument("czarter")
    a5.add_argument("--api", required=True, metavar="CONTROL_URL")
    a5.add_argument("--token-file", default="~/.czarter/operator.jwt")
    a5.add_argument("--yes", action="store_true", help="remove it (default: preview)")
    a5.add_argument("--keep-servers", action="store_true", help="leave the app's servers registered")
    a5.add_argument("--other-namespaces", default="", help="namespaces other Czarters use (comma-separated)")
    a5.add_argument("--idp", action="store_true", help="also remove the app's IdP application (Auth0 or Keycloak), its MFA and its Vault secret")
    a4 = sub.add_parser("grants"); a4.add_argument("czarter")
    a4.add_argument("--api", required=True, metavar="CONTROL_URL")
    a4.add_argument("--token-file", default="~/.czarter/operator.jwt")
    a = ap.parse_args()
    if a.cmd == "apply" and a.step.startswith("idp."):
        sys.exit(apply_idp(a))
    global CZARTER_FILE
    CZARTER_FILE = a.czarter
    c = load(a.czarter, draft=getattr(a, "draft", False))
    if a.cmd == "retire":
        try:
            st, detail = retire(c, Api(a.api, a.token_file), a.yes, a.keep_servers,
                                {n for n in a.other_namespaces.split(",") if n}, idp=a.idp)
        except RuntimeError as e:
            st, detail = "failed", str(e)
        print(f"{ {'done': '✅', 'failed': '❌'}.get(st, '·') } retire: {detail}")
        sys.exit(1 if st == "failed" else 0)
    if a.cmd == "grants":
        try:
            show_grants(c, Api(a.api, a.token_file))
        except RuntimeError as e:
            sys.exit(f"❌ {e}")
        return
    if a.cmd == "apply":
        if not a.api:
            sys.exit("--api is needed for this step")
        try:
            st, detail = APPLY_STEPS[a.step](c, Api(a.api, a.token_file), a.yes)
        except RuntimeError as e:
            st, detail = "failed", str(e)
        if a.step == "sts.agent" and LAST_RESULTS:
            for s_, st_, d_ in LAST_RESULTS:
                print(f"✅ {s_}: {d_}")
            rows = LAST_RESULTS + ([(a.step, st, detail)] if st else [])
        else:
            # Per-item rows (people.role.mags, register.x, controls.scope.y) AND the step's own
            # line - the checklist marks a step done by its own line (people.roles, ...).
            rows = list(LAST_RESULTS or [])
            if st and not any(r[0] == a.step for r in rows):
                rows.append((a.step, st, detail))
        print(f"{ {'done': '✅', 'failed': '❌', 'skipped': '⏭', 'partial': '⚠️'}.get(st, '·') } {a.step}: {detail}")
        if st and a.record:
            record(rows, a.record, by="czarter-apply")
        sys.exit(1 if st == "failed" else 0)
    if a.cmd == "plan":
        plan(c, a.out)
        return
    res = verify(c, a.policy, Api(a.api, a.token_file) if a.api else None)
    if a.api:
        try:
            api_res, replaces = verify_api(c, Api(a.api, a.token_file))
            res = [r for r in res if r[0] not in replaces] + api_res
        except RuntimeError as e:
            print(f"❌ api: {e}")
            sys.exit(1)
    mark = {"passed": "✅", "failed": "❌", "skipped": "⏭"}
    for s, st, d in res:
        print(f"{mark[st]} {s}: {d}")
    if a.record:
        record(res, a.record)
    sys.exit(1 if any(st == "failed" for _, st, _ in res) else 0)


if __name__ == "__main__":
    main()
