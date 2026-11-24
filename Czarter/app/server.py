#!/usr/bin/env python3
"""Magertron Czartup(TM) - a standalone local app that writes and applies Czarters.

Serves the wizard on 127.0.0.1 only, keeps each Czarter as a YAML file in
../czarters/, and runs the Czarter tools (validate, plan, verify, apply,
runbook, mint-operator) when you press their buttons.

  python3 app/server.py            # http://127.0.0.1:8791
  CZARTER_PORT=8792 CZARTER_API=https://app.magertron.com python3 app/server.py
"""
import hashlib, json, os, pathlib, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator

APP = pathlib.Path(__file__).resolve().parent
ROOT = APP.parent
STATIC = APP / "static"
# HOSTED: running in the cluster behind <magertron>/czartup/. Every API call then carries
# the signed-in person's console token, and Czartup acts as that person.
HOSTED = os.environ.get("CZARTUP_HOSTED") == "1"
STORE = pathlib.Path(os.environ.get("CZARTUP_STORE") or (ROOT / "czarters"))
EXAMPLES = ROOT / "examples"
SCHEMA = json.loads((ROOT / "schema" / "czarter.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
PORT = int(os.environ.get("CZARTER_PORT", "8791"))
API = os.environ.get("CZARTER_API", "https://app.magertron.com")
# What the page shows as "Magertron(TM): ..." - hosted, API is the in-cluster address.
PUBLIC_API = os.environ.get("CZARTUP_PUBLIC_API", API)
BIND = os.environ.get("CZARTUP_BIND", "0.0.0.0" if HOSTED else "127.0.0.1")
CALLER = threading.local()          # .token / .user for the request being served
_SEEN = {}                           # sha256(token) -> (expires_at, user): tokens Magertron accepted
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
STORE.mkdir(parents=True, exist_ok=True)


# This Czartup's version: the one serverInfo states below (the release script reads it there too),
# so the page, the MCP server and the image tag can never disagree.
VERSION = (re.search(r'"serverInfo":\s*\{[^}]*"version":\s*"([0-9.]+)"', pathlib.Path(__file__).read_text()) or [None, "?"])[1]

# Filled in by "Create the app in Auth0 / Keycloak" (Review & run, step 2), so not required before it.
CREATE_APP_FILLS = {("identity/app", "clientId"), ("identity/sts/agent", "externalId")}
AUTOMATED_IDPS = {"auth0": "Auth0", "keycloak": "Keycloak"}     # Czartup creates the app in these


def secret_dir(provider):
    """Where Create app keeps an app's client secret: secret/<namespace>/<this>."""
    return "keycloak" if provider == "keycloak" else "auth0"


_IDP_DEFAULT = {"at": 0.0, "value": None}


def idp_default():
    """This install's own IdP, when it has one Czartup should use for new apps: Vault
    secret/czartup/idp (provider, domain, audience). A trial tenant gets its Keycloak there from
    trialctl. Read at most once a minute; nothing there = no default."""
    if time.time() - _IDP_DEFAULT["at"] < 60:
        return _IDP_DEFAULT["value"]
    val = None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import idp_auth0
        d = idp_auth0.vault_read(os.environ.get("CZARTUP_IDP_DEFAULT", "secret/czartup/idp"))
        if d.get("provider") and d.get("domain"):
            val = {k: str(d.get(k) or "").strip() for k in ("provider", "domain", "audience")}
    except Exception:  # noqa: BLE001 - no Vault, nothing there: no default
        val = None
    _IDP_DEFAULT.update(at=time.time(), value=val)
    return val


def errors_of(doc):
    out = [{"path": "/".join(map(str, e.path)) or "(top)", "message": e.message}
           for e in sorted(VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.path)))]
    if ((doc or {}).get("identity") or {}).get("idp", {}).get("provider") in AUTOMATED_IDPS:
        out = [e for e in out
               if not any(e["path"] == where and e["message"] == f"'{leaf}' is a required property"
                          for where, leaf in CREATE_APP_FILLS)]
    return out


def path_for(name):
    if not SLUG.match(name or ""):
        raise ValueError("name must be lowercase letters, digits and dashes")
    return STORE / f"{name}.czarter.yaml"


def runs_for(name):
    return STORE / f"{name}.runs.json"


def tool(args, timeout=120):
    env = dict(os.environ)
    tok = getattr(CALLER, "token", None)
    if tok:
        env["CZARTER_TOKEN"] = tok      # apply.py / handover.py act as the signed-in person
    r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    return {"ok": r.returncode == 0, "code": r.returncode, "output": (r.stdout + r.stderr).strip()}


# ---------- the live Magertron(TM): which servers exist, and their tools ----------
TOKEN_FILE = pathlib.Path("~/.czarter/operator.jwt").expanduser()


def _mag_get(path, token):
    import urllib.request, urllib.error
    req = urllib.request.Request(API.rstrip("/") + path, headers={
        "Authorization": "Bearer " + token, "User-Agent": "magertron-czarter/1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def _items(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ("items", "servers", "tools"):
            if isinstance(j.get(k), list):
                return j[k]
    return []


def _jwt_claims(token):
    """The token's claims - display only; Magertron(TM) is the judge of whether it is valid."""
    import base64
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def _jwt_sub(token):
    return _jwt_claims(token).get("sub") or ""


def caller_ok(token):
    """Hosted: accept a console token only if Magertron(TM) accepts it for an admin call
    (listing roles - the same permission Czartup's grant step needs). Cached for 60 s."""
    if not token:
        return False, "Sign in to Magertron™ first."
    key = hashlib.sha256(token.encode()).hexdigest()
    hit = _SEEN.get(key)
    if hit and hit[0] > time.time():
        return True, hit[1]
    st, _ = _mag_get("/api/v1/roles", token)
    if st == 200:
        _SEEN[key] = (time.time() + 60, _jwt_sub(token))
        return True, _SEEN[key][1]
    if st == 401:
        return False, "Your Magertron™ session has expired."
    if st == 403:
        return False, "Czartup needs a Magertron™ admin - your account cannot manage roles."
    return False, f"Magertron™ did not answer the sign-in check (HTTP {st})."


def _token(force=False):
    if HOSTED:
        return getattr(CALLER, "token", "") or ""
    if force or not TOKEN_FILE.exists():
        r = tool(["mint-operator.py"])
        if not r["ok"]:
            raise RuntimeError("could not mint an operator token: " + r["output"][-200:])
    return TOKEN_FILE.read_text().strip()


def _labels(row):
    lab = row.get("labels") or {}
    if isinstance(lab, str):
        try:
            lab = json.loads(lab)
        except ValueError:
            lab = {}
    return lab if isinstance(lab, dict) else {}


def _kind(row):
    """Magertron(TM) marks LLM servers with labels.kind = "llm"."""
    k = str(_labels(row).get("kind", "")).lower()
    if k in ("llm", "rest", "mcp"):
        return k
    t = " ".join(str(row.get(x, "")) for x in ("kind", "type", "server_type", "protocol")).lower()
    return "llm" if "llm" in t else "rest" if "rest" in t else "mcp"


def _row_tools(row):
    tl = row.get("tools") or []
    if isinstance(tl, str):
        try:
            tl = json.loads(tl)
        except ValueError:
            tl = []
    return [t.get("name") if isinstance(t, dict) else t for t in tl if (t.get("name") if isinstance(t, dict) else t)]


def people_match(email):
    """The Magertron users a person's email could be (0.2.80): username, email or SSO email equal to it.
    The page shows them to pick from, so nobody types a username or an IdP subject."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return {"email": e, "users": []}
    tok, users, page = _token(), [], 1
    while page <= 50:
        st, j = _mag_get(f"/api/v1/users?page={page}&size=200", tok)
        if st in (401, 403) and page == 1:
            tok = _token(force=True); st, j = _mag_get(f"/api/v1/users?page={page}&size=200", tok)
        if st != 200:
            raise RuntimeError(f"GET /api/v1/users returned HTTP {st}")
        items = j.get("items") if isinstance(j, dict) else j
        if not items:
            break
        users += [u for u in items if isinstance(u, dict)]
        if isinstance(j, dict) and j.get("total") is not None and len(users) >= int(j["total"]):
            break
        page += 1
    out = []
    for u in users:
        if e not in {str(v).strip().lower() for v in (u.get("username"), u.get("email"), u.get("sso_email")) if v}:
            continue
        roles = [str(r.get("name") or r.get("id") if isinstance(r, dict) else r) for r in u.get("roles") or []]
        out.append({"username": u.get("username"), "signsIn": bool(u.get("external_id")),
                    "admin": any("admin" in r.lower() for r in roles), "roles": len(roles)})
    out.sort(key=lambda x: (x["admin"], not x["signsIn"], x["username"] or ""))
    return {"email": e, "users": out}


def magertron_servers():
    """Every registered server with its tool names, read with the operator token
    (minted automatically when missing or expired)."""
    tok = _token()
    st, servers = _mag_get("/api/v1/servers", tok)
    if st in (401, 403):
        tok = _token(force=True)
        st, servers = _mag_get("/api/v1/servers", tok)
    if st != 200:
        raise RuntimeError(f"GET /api/v1/servers returned HTTP {st}")
    out = []
    for r in _items(servers):
        name = r.get("name") or r.get("server_name")
        if not name:
            continue
        ns = r.get("namespace") or r.get("namespace_") or ""
        out.append({"name": name, "namespace": ns, "kind": _kind(r),
                    "model": str(_labels(r).get("model", ""))})
    tools = {}
    for r in _items(servers):
        name = r.get("name") or r.get("server_name")
        if name:
            for t in _row_tools(r):
                tools.setdefault(f"{r.get('namespace') or r.get('namespace_') or ''}/{name}", set()).add(t)
    st, j = _mag_get("/api/v1/inventory/tools", tok)
    if st == 200:
        for t in _items(j):
            k = f"{t.get('server_namespace', '')}/{t.get('server_name', '')}"
            n = t.get("name") or t.get("tool_name")
            if n:
                tools.setdefault(k, set()).add(n)
    return {"api": PUBLIC_API, "servers": sorted(out, key=lambda x: (x["namespace"], x["name"])),
            "tools": {k: sorted(v) for k, v in tools.items()}}


BRIEF = ("I'm the system integrator for this customer. Read README.md and AGENTS.md, then tell me your "
         "plan to build this app in one day. Don't write code yet.")


def open_pack(name, target):
    """Copy the built pack into its own working folder (never over an existing one - that is
    the SI's work in progress) and open it in the SI's tool: cursor, code, claude or devin."""
    import shutil, datetime
    src = STORE / "generated" / name / "handover"
    if not (src / "AGENTS.md").exists():
        return {"ok": False, "output": "Build the pack first."}
    base = pathlib.Path(os.environ.get("CZARTUP_WORKDIR", "~/si-work")).expanduser()
    dest, n = base / name, 1
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M')
    while dest.exists():
        dest = base / (f"{name}-{stamp}" + (f"-{n}" if n > 1 else ""))
        n += 1
    base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    spawn = lambda argv: subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          start_new_session=True)
    brief = f"\n\nIn its agent (Plan mode first), start with:\n{BRIEF}"
    if target in ("cursor", "code"):
        label = {"cursor": "Cursor", "code": "VS Code"}[target]
        if not shutil.which(target):
            return {"ok": False, "output": f"Copied the pack to {dest}, but the '{target}' command is not on the PATH - "
                                           f"open that folder in {label} yourself."}
        spawn([target, str(dest)])
        return {"ok": True, "output": f"Opened {dest} in {label}." + brief}
    if target == "claude":
        if not shutil.which("claude"):
            return {"ok": False, "output": f"Copied the pack to {dest}, but Claude Code ('claude') is not on the PATH. "
                                           "Install Claude Code, then run 'claude' in that folder."}
        run = f'cd "{dest}" && claude; exec bash'
        for term, argv in (("gnome-terminal", ["gnome-terminal", f"--working-directory={dest}", "--", "bash", "-lc", run]),
                           ("konsole", ["konsole", "--workdir", str(dest), "-e", "bash", "-lc", run]),
                           ("xfce4-terminal", ["xfce4-terminal", f"--working-directory={dest}", "-x", "bash", "-lc", run]),
                           ("kitty", ["kitty", "--directory", str(dest), "bash", "-lc", run]),
                           ("alacritty", ["alacritty", "--working-directory", str(dest), "-e", "bash", "-lc", run]),
                           ("x-terminal-emulator", ["x-terminal-emulator", "-e", "bash", "-lc", run]),
                           ("xterm", ["xterm", "-e", "bash", "-lc", run])):
            if shutil.which(term):
                spawn(argv)
                return {"ok": True, "output": f"Opened a terminal in {dest} running Claude Code - it reads CLAUDE.md, "
                                              "which loads AGENTS.md." + brief}
        return {"ok": False, "output": f"Copied the pack to {dest}, but found no terminal to open. Run: cd {dest} && claude"}
    if target == "devin":
        return {"ok": True, "output": (
            f"Copied the pack to {dest}. Devin works in its own cloud from a GitHub or GitLab repo, so:\n\n"
            f"1. Put the folder in a PRIVATE repo, e.g. with the GitHub CLI:\n"
            f"   cd {dest} && git init -b main && git add . && git commit -m \"Handover pack\" && "
            f"gh repo create {name}-handover --private --source . --push\n"
            "2. In Devin, connect that repo and start a session on it.\n"
            "3. Put the app's secrets in Devin's own Secrets settings - the pack holds references only.\n"
            f"4. Give Devin the brief:\n{BRIEF}\n\n"
            "Or send the SI the zip (Download zip) and let them choose their repo.")}
    return {"ok": False, "output": f"Unknown target {target}."}


def delete_czarter(name):
    """Move a Czarter and everything Czartup made for it (run log, runbook, generated plan and
    handover pack) into czarters/.trash/<name>-<time>/. Nothing in the IdP or in Magertron™ is
    touched - the app, the agent, its role and grants stay until someone removes them there."""
    import datetime, shutil
    f = path_for(name)
    if not f.exists():
        return {"ok": False, "error": f"no Czarter named {name}"}
    dest = STORE / ".trash" / f"{name}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in (f, runs_for(name), STORE / f"{name}-runbook.md", STORE / "generated" / name):
        if src.exists():
            shutil.move(str(src), str(dest / src.name))
            moved.append(src.name)
    return {"ok": True, "moved": moved, "trash": str(dest)}


def retire_app(nm, yes=False, keep_servers=False, idp=False):
    """Retire: preview (yes=False) or remove what setup created for this app, then move its
    Czarter to the trash. One path for the IDE, the CLI and the page's Retire button.
    Returns (text, ok)."""
    f = path_for(nm) if nm else None
    if not nm or not f.exists():
        return f"no Czarter named {nm}", False
    others = set()
    for g in STORE.glob("*.czarter.yaml"):
        if g.resolve() == f.resolve():
            continue
        try:
            others.add(((yaml.safe_load(g.read_text()) or {}).get("metadata") or {}).get("namespace") or "")
        except yaml.YAMLError:
            pass
    if not HOSTED:
        minted = ensure_token()
        if minted and not minted["ok"]:
            return "Could not mint the operator token:\n" + minted["output"], False
    r = tool(["apply.py", "retire", str(f), "--api", API, "--other-namespaces", ",".join(sorted(o for o in others if o))]
             + (["--yes"] if yes else []) + (["--keep-servers"] if keep_servers else []) + (["--idp"] if idp else []),
             timeout=180)
    out = r.get("output", "")
    if yes and r.get("ok"):
        d = delete_czarter(nm)
        out += f"\n\nThe Czarter {nm} is in the trash ({d.get('trash', '')}) - Czartup no longer lists it."
    return out.strip(), bool(r.get("ok"))


# ---------- Self-heal after an upgrade ----------
# A new Czartup version can bring tools Magertron(TM) has never approved, and the gateway refuses
# them (403 tool-approval) until someone presses Connect now. Instead, the first admin who reaches
# Czartup after it starts (the page, an IDE, the CLI) carries it: Czartup re-probes itself and
# approves its OWN tool definitions, as that admin, audited to them. Only when Czartup is already
# registered - it never registers itself unasked. Once per process; a failure retries in 5 minutes.
_HEAL = {"done": False, "next": 0.0, "lines": []}
_HEAL_LOCK = threading.Lock()


# ---------- The standard demo's servers ----------
# Every Magertron with Czartup runs Tidewater Marina's three sample MCP servers (chart: samples.enabled,
# on by default), so the small-business-app template is ready to run out of the box. The chart runs
# them; registering a server takes an admin, so the first admin who reaches Czartup registers them -
# external, Trusted upstream on, approved, with their icons - exactly as trialctl does for a trial.
# Only servers that are actually running (they answer at their in-cluster address) and not yet
# registered. Never changes one that is already registered.
SAMPLE_NS = os.environ.get("CZARTUP_SAMPLES_NS", "tidewater")
SAMPLE_SERVERS = ("tidewater-slips", "tidewater-fuel", "tidewater-notices")
_SAMPLES = {"done": False, "next": 0.0}


def _sample_running(name):
    import ssl, urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE        # a self-signed sample, in-cluster
    try:
        with urllib.request.urlopen(f"https://{name}.{SAMPLE_NS}.svc.cluster.local:8443/healthz",
                                    timeout=4, context=ctx) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - not deployed here
        return False


def seed_samples(tok):
    """Register the sample servers that run here and are not registered yet. Returns (lines, finished):
    finished when all are registered, or none runs at all (samples.enabled=false). Otherwise - some
    still starting on a fresh install - it is tried again on a later visit."""
    import base64
    L, registered, absent = [], 0, 0
    for n in SAMPLE_SERVERS:
        st, _ = _mag_get(f"/api/v1/servers/{SAMPLE_NS}/{n}", tok)
        if st == 200:
            registered += 1
            continue                                # registered (by trialctl, a person, or before)
        if st in (401, 403):
            return L + ["samples: this sign-in may not register servers - an admin's first visit does it"], False
        if not _sample_running(n):
            absent += 1
            continue                                # not deployed here, or still starting
        st, j = _mag_post("/api/v1/servers", tok, {
            "type": "external", "name": n, "namespace": SAMPLE_NS,
            "endpoint_url": f"https://{n}.{SAMPLE_NS}.svc.cluster.local:8443/mcp", "upstream_path": "/mcp",
            "auth_type": "none", "credential_mode": "centralized", "routing_mode": "v3",
            "upstream_http_version": "http1", "tls_min_version": "tls1_2", "forward_identity": True,
            "description": "Tidewater Marina sample server (invented data) - the standard demo."}, timeout=60)
        if st not in (200, 201, 409):
            L.append(f"samples: could not register {SAMPLE_NS}/{n} (HTTP {st}): {str(j)[:160]}")
            continue
        icon = ROOT / "examples" / "icons" / f"{n}.png"
        if icon.exists():
            _mag_put(f"/api/v1/servers/{SAMPLE_NS}/{n}/icon", tok, {"image_base64": base64.b64encode(icon.read_bytes()).decode()})
        st, _ = _mag_post(f"/api/v1/servers/{SAMPLE_NS}/{n}/approve", tok, {}, timeout=60)
        _mag_post(f"/api/v1/servers/{SAMPLE_NS}/{n}/probe", tok, {}, timeout=60)
        registered += 1
        L.append(f"samples: registered {SAMPLE_NS}/{n} (Trusted upstream on){', approved' if st in (200, 201, 204) else ''}")
    return L, registered == len(SAMPLE_SERVERS) or absent == len(SAMPLE_SERVERS)


def _self_heal(tok):
    if not HOSTED or not tok or (_HEAL["done"] and _SAMPLES["done"]):
        return
    with _HEAL_LOCK:
        if (_HEAL["done"] and _SAMPLES["done"]) or time.time() < _HEAL["next"]:
            return
        _HEAL["next"] = time.time() + 300

    def run():
        CALLER.token, CALLER.user = tok, _jwt_sub(tok)
        if not _SAMPLES["done"]:
            try:
                lines, finished = seed_samples(tok)
                _SAMPLES["done"] = finished
                if lines:
                    print("czartup samples: " + " | ".join(lines), file=sys.stderr, flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"czartup samples failed: {e}", file=sys.stderr, flush=True)
        if _HEAL["done"]:
            return
        try:
            st, _ = _mag_get(f"/api/v1/servers/{MCP_NS}/czartup", tok)
            if st != 200:
                return                              # not connected yet: Connect now does the first one
            r = connect_ide(apply=True)
            _HEAL["lines"] = r.get("lines", [])
            if r.get("ok"):
                _HEAL["done"] = True
            print("czartup self-heal: " + " | ".join(_HEAL["lines"]), file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"czartup self-heal failed: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=run, daemon=True).start()


def fresh_identity(doc, old, new):
    """A clone is a new app: it must not reuse the source's IdP app, its secret or its agent link.
    Clear the client ID and the agent's external ID (Create app fills both in), point the secret at
    the clone's own Vault path, and rename the app's own role when it was named after the source."""
    idn = doc.get("identity") or {}
    app = idn.get("app") or {}
    if app.get("clientId"):
        app["clientId"] = ""
    ref = app.get("secretRef") or ""
    if ref.startswith("vault:"):
        key = ref.split("#", 1)[1] if "#" in ref else "client_secret"
        app["secretRef"] = f"vault:secret/{new}/{secret_dir((idn.get('idp') or {}).get('provider'))}#{key}"
    agent = (idn.get("sts") or {}).get("agent") or {}
    if agent.get("externalId"):
        agent["externalId"] = ""
    roles = agent.get("roles") or []
    title = lambda n: " ".join(w.capitalize() for w in n.split("-"))
    if roles and roles[0] == title(old):
        roles[0] = title(new)


def retarget_clone(doc, old, new):
    """A clone gets its own agent: '<old>-x' becomes '<new>-x', and every piece of free text
    (descriptions, reasons, expectations, the agent's purpose) that named the old agent is updated,
    so a check never describes an agent the clone does not have."""
    agent = ((doc.get("identity") or {}).get("sts") or {}).get("agent") or {}
    sa = agent.get("serviceAccount") or ""
    if not sa:
        return
    new_sa = new + sa[len(old):] if sa.startswith(old) else f"{new}-{sa}"
    if new_sa == sa:
        return
    agent["serviceAccount"] = new_sa
    # Free text can name the agent from any EARLIER generation of the clone chain
    # (playbook-settler -> playbook-v2-settler -> ...), so match any "<prefix><role-suffix>"
    # token, not just the last name. The role suffix is what follows the Czarter name.
    suffix = sa[len(old):] if sa.startswith(old) else ""
    words = [re.escape(sa)] + ([r"[a-z0-9][a-z0-9-]*" + re.escape(suffix)] if suffix.startswith("-") and len(suffix) > 2 else [])
    pat = re.compile(r"(?<![A-Za-z0-9_-])(?:" + "|".join(words) + r")(?![A-Za-z0-9_-])")

    def walk(x, key=None):
        if isinstance(x, dict):
            return {k: walk(v, k) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v, key) for v in x]
        if isinstance(x, str) and key in ("description", "reason", "expect", "purpose", "summary", "does", "name") \
                and key != "serviceAccount":
            return pat.sub(new_sa, x)
        return x
    for k in ("checks", "controls", "functionality", "connections", "handover"):
        if k in doc:
            doc[k] = walk(doc[k])


def czarter_status(name):
    """What the checklist on Review & run shows: token age, latest run-log row per step,
    and when the plan and runbook were last generated."""
    import time
    out = {"token": None, "runs": {}, "plan": None, "runbook": None, "pack": None, "czarter": None,
           "k8sPolicies": os.environ.get("CZARTUP_K8S_POLICIES") == "1",
           "icon": (STORE / f"{name}.icon.png").exists()}
    cf = path_for(name)
    if cf.exists():
        out["czarter"] = int(cf.stat().st_mtime)   # last save: a pack older than this is out of date
    if HOSTED:
        # No operator token here: the "token" is the caller's console session.
        tok = getattr(CALLER, "token", "") or ""
        exp = _jwt_claims(tok).get("exp")
        left = (exp - time.time()) if isinstance(exp, (int, float)) else 15 * 60
        out["token"] = {"minutesLeft": max(0, int(left // 60)), "valid": bool(tok) and left > 0,
                        "hosted": True, "user": getattr(CALLER, "user", "") or _jwt_sub(tok)}
    elif TOKEN_FILE.exists():
        left = 15 * 60 - (time.time() - TOKEN_FILE.stat().st_mtime)
        out["token"] = {"minutesLeft": max(0, int(left // 60)), "valid": left > 0}
    rf = runs_for(name)
    if rf.exists():
        for r in sorted(json.loads(rf.read_text() or "[]"), key=lambda r: r.get("done_at", "")):
            out["runs"][r["step"]] = {k: r.get(k) for k in ("status", "done_at", "done_by", "detail")}
    pf = STORE / "generated" / name / "manual.md"
    if pf.exists():
        out["plan"] = int(pf.stat().st_mtime)
    rb = STORE / f"{name}-runbook.md"
    if rb.exists():
        txt = rb.read_text()
        out["runbook"] = {"at": int(rb.stat().st_mtime), "verified": "**verified**" in txt}
    hp = STORE / "generated" / name / "handover" / "AGENTS.md"
    if hp.exists():
        out["pack"] = int(hp.stat().st_mtime)
    return out


def run_action(name, action, yes=False):
    f = str(path_for(name))
    if not pathlib.Path(f).exists():
        return {"ok": False, "output": "Save the Czarter first."}
    runs = str(runs_for(name))
    api = ["--api", API]
    minted = None
    if HOSTED and action == "mint":
        return {"ok": True, "output": f"Czartup acts as you ({getattr(CALLER, 'user', '') or 'signed in'}) - "
                                      "no separate operator token is needed here."}
    if HOSTED and action.startswith("handover-open"):
        # Nothing can be launched on the SI's machine from the cluster: hand them the zip.
        r = _run_action(name, "handover-zip", f, runs, api, yes)
        r["output"] = (r.get("output", "") + "\n\nUnzip it and open the folder in Cursor, VS Code, "
                       "Claude Code or Devin, then say \"Build\".").strip()
        return r
    if not HOSTED and action in ("verify", "apply-policy", "apply-scope", "apply-agent", "apply-grants", "apply-people", "apply-register", "apply-tools", "grants"):
        minted = ensure_token()
        if minted and not minted["ok"]:
            return {"ok": False, "output": "Could not mint the operator token:\n" + minted["output"]}
    r = _run_action(name, action, f, runs, api, yes)
    if minted:
        r["output"] = "Minted a fresh operator token.\n\n" + r.get("output", "")
    return r


def ensure_token(min_left=120):
    """Mint the operator token when it is missing or has under 2 minutes left.
    Returns None when the current one is fine, else the mint result."""
    import time
    if TOKEN_FILE.exists() and 15 * 60 - (time.time() - TOKEN_FILE.stat().st_mtime) > min_left:
        return None
    return tool(["mint-operator.py"])


def _run_action(name, action, f, runs, api, yes):
    if action == "validate":
        return tool(["validate.py", f])
    if action == "plan":
        r = tool(["apply.py", "plan", f, "--draft", "--out", str(STORE / "generated")])
        if r["ok"]:
            d = STORE / "generated" / name
            r["files"] = [{"title": t, "file": str(d / fn), "kind": k, "content": (d / fn).read_text()}
                          for t, fn, k in (("Manual steps", "manual.md", "markdown"),
                                           ("Policy (OPA)", "policy.rego", "code"),
                                           ("Checks (SQL, read-only)", "verify.sql", "code"))
                          if (d / fn).exists()]
        return r
    if action == "mint":
        return tool(["mint-operator.py"])
    if action == "verify":
        return tool(["apply.py", "verify", f, *api, "--record", runs])
    if action in ("apply-idp-app", "apply-idp-mfa"):
        step = {"apply-idp-app": "idp.app", "apply-idp-mfa": "idp.mfa"}[action]
        r = tool(["apply.py", "apply", f, "--step", step, "--record", runs] + (["--yes"] if yes else []), timeout=180)
        return r
    if action in ("apply-agent", "apply-grants", "apply-people", "apply-register", "apply-tools", "apply-policy", "apply-scope"):
        step = {"apply-agent": "sts.agent", "apply-grants": "grants", "apply-people": "people.roles",
                "apply-policy": "policy.install", "apply-scope": "policy.scope",
                "apply-register": "servers.register", "apply-tools": "tools.approve"}[action]
        r = tool(["apply.py", "apply", f, "--step", step, *api, "--record", runs] + (["--yes"] if yes else []))
        r["output"] = re.sub(r"re-run with --yes to (create it|make them)", "click Make the change", r["output"])
        return r
    if action == "grants":
        return tool(["apply.py", "grants", f, *api])
    if action == "handover-pack":
        out = STORE / "generated" / name / "handover"
        r = tool(["handover.py", "pack", f, "--runs", runs, "--out", str(out), "--record", runs])
        if r["ok"]:
            show = [("Build brief and guardrails", "AGENTS.md"), ("Spec (Spec Kit)", f"specs/001-{name}/spec.md"),
                    ("Tasks (Spec Kit)", f"specs/001-{name}/tasks.md"), ("Constitution", ".specify/memory/constitution.md")]
            show += [(f"Server spec: {d.name}", f"servers/{d.name}/SERVER-SPEC.md")
                     for d in sorted((out / "servers").glob("*")) if (d / "SERVER-SPEC.md").exists()]
            show += [("Environment (references only)", ".env.example"), ("Policy (the app's module)", f"policy/{name}.rego"),
                     ("Policy ConfigMap (kubectl apply -f)", f"policy/{name}-configmap.yaml")]
            r["files"] = [{"title": t, "file": str(out / fn), "content": (out / fn).read_text(),
                           "kind": "markdown" if fn.endswith(".md") else "code"} for t, fn in show if (out / fn).exists()]
            r["packed"] = True
        return r
    if action == "handover-zip":
        r = tool(["handover.py", "zip", f, "--runs", runs, "--out-dir", str(STORE / "generated" / name / "exports"), "--record", runs])
        m = re.search(r"^file: (.+\.zip)$", r.get("output", ""), re.M)
        if r["ok"] and m:
            fn = pathlib.Path(m.group(1)).name
            r["download"] = f"api/download/{name}/{fn}"
            r["output"] = "\n".join(l for l in r["output"].splitlines() if not l.startswith("file: "))
        return r
    if action.startswith("handover-open"):
        return open_pack(name, action.split("-")[-1] if action.count("-") >= 2 else "cursor")
    if action == "apply-handover":
        r = tool(["handover.py", "push", f, "--runs", runs, "--record", runs] + (["--yes"] if yes else []), timeout=180)
        return r
    if action == "repo-check":                 # Test connection (0.2.96): the repo host and its token
        return tool(["handover.py", "check"], timeout=60)
    if action == "runbook":
        out = STORE / f"{name}-runbook.md"
        args = ["runbook.py", f, "-o", str(out)]
        if pathlib.Path(runs).exists():
            args[2:2] = ["--runs", runs]
        r = tool(args)
        if r["ok"]:
            r["markdown"] = out.read_text()
            r["file"] = str(out)
        return r
    return {"ok": False, "output": f"unknown action {action}"}


# ---------- "Describe it": a draft Czarter from a sentence ----------
# The model is called THROUGH Magertron(TM) (its LLM route): governed, metered and
# audited like any other call. It drafts the SHAPE of the app; everything that
# belongs to one customer is blanked afterwards, as for a template, and the draft
# opens unsaved for a person to review. Nothing is applied.
#
# ⚠ WHOSE TOKEN. The LLM proxy refuses a person's token outright (rest_server.cpp,
# "LLM-access lockdown (a)"): only a service account spends a vendor key, and people
# reach models through an assistant that authenticates as one. For this ONE call
# Czartup is that assistant: it uses the console assistant's service account
# (mcp-assistant-sa, role system:llm-consumer - it can call a model and nothing else),
# mounted read-only from the Secret <release>-assistant (key MAG_SA_JWT). A mounted
# Secret file follows rotation; an env var would keep a revoked token.
# Only a signed-in administrator reaches describe() (authorize() runs first), and the
# pod log names who asked. Every OTHER call Czartup makes is still the person's own.
DESCRIBE_LLM = os.environ.get("CZARTUP_LLM", "mcp-prod/haiku-anthropic-llm")
DESCRIBE_MODEL = os.environ.get("CZARTUP_LLM_MODEL", "")
LLM_TOKEN_FILE = os.environ.get("CZARTUP_LLM_TOKEN_FILE", "/var/run/magertron/llm/token")


def _llm_token():
    """(token, via_service_account). The assistant's SA token when mounted, else the caller's."""
    try:
        with open(LLM_TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t, True
    except OSError:
        pass
    return _token(), False


def _mag_post(path, token, body, timeout=150):
    import urllib.request, urllib.error
    req = urllib.request.Request(API.rstrip("/") + path, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + token, "User-Agent": "magertron-czarter/1",
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:300]}


DESCRIBE_RULES = """You write Czarters: the YAML document Magertron Czartup uses to set up one governed app or agent.
Return ONE YAML document and nothing else - no prose, no code fences.

Rules:
- Follow the JSON Schema exactly (field names, nesting, enums). Use the example only for style.
- metadata: name = a short lowercase slug (letters, digits, dashes) from the company or app; tenant = the company;
  namespace = the same slug; description = one sentence; owner = "".
- identity: provider auth0, audience https://sts.magertron.com, people sign in with authorization_code_pkce,
  promptLogin true, MFA required (otp, this-app-only). Leave every customer-specific value EMPTY: idp.domain "",
  app.clientId "", callbackUrls [], secretRef "", agent.externalId "". sts.people: one {email, name} per person the
  description names (never invent people); [] when it names none - setup links each email to its accounts and
  creates a missing Magertron user (with the app's role). people.createMissing: true ONLY when the user says the
  app may create people in the IdP too (a demo or trial); otherwise leave it out.
- agent: serviceAccount = "<name>-<role>" (e.g. harborline-reorder); roles = [<the app's role> ] plus "LLM Consumer"
  if it uses an LLM. The app's role is SHARED: staff get it and the agent's service account holds it, so name it for
  the app and its people, never after the agent - "<Company> Staff" (e.g. "Harborline Staff"), not "Reorder Agent".
  What the agent may do ALONE is limited by agents[].tools, not by the role name.
- connections:
  * Use an EXISTING server from the "registered" list when one fits: same name, namespace and kind; list only tools
    it really has.
  * A system the app needs that is NOT registered (usually the customer's own system): a new connection named
    "<name>-<thing>", kind mcp, in the app's own namespace, with snake_case tool names and arguments implied by the
    controls. If it is the customer's OWN system (not a vendor), set forwardIdentity: true.
  * An LLM ONLY when the description says the app uses AI, a model or an LLM to write, summarise or chat. "Suggests",
    "recommends", "drafts" or "checks" on their own do NOT mean an LLM - plain code does those. No LLM: no LLM
    connection and no "LLM Consumer" role.
- controls: every limit and every approval in the description.
  * a limit: {id, description, server, tool, field, op, value} - e.g. field amount, op "<=", value 5000.
  * an approval: a hold {id, server, tool, when: {argEquals: {<field>: <value>}}, reason, approvers, ttlSeconds: 900}.
    approvers = the people who decide it, if the description names them: each one ALSO in sts.people, and written as
    the Magertron username setup gives them - first.last from their name in lowercase (Sam Lee -> sam.lee); [] =
    anyone with Deploy on the namespace. Never the agent, never the person whose request is held - Magertron refuses both.
  * the fields used here MUST be arguments of that tool, and the tool MUST be in that connection's tools.
  * a money cap over time ("budget", "cap", "$100 a month", "spend no more than") is a BUDGET, never a limit:
    {server, description, monthlyUsd or dailyUsd} on the connection that spends (usually the LLM). A limit is only a
    rule on ONE call's arguments.
- Every action the description says people or the agent take (book, record, check out, waive, draft...) needs a
  tool on a connection - writes as well as reads. Staff who "book slips" need a book_slip tool.
- functionality: summary, surfaces (what people see), agents (name, cadence, onlyWhen, may, mustNot, tools). An agent
  that runs on a schedule acts with nobody signed in; mustNot always includes approving its own holds.
  * tools = the ONLY tools the agent may call on its own, as "server/tool" from connections. Reads plus what its "may"
    needs; never a tool its "mustNot" forbids (an agent that drafts orders gets draft_order, not create_order). An agent
    that READS something gets a read tool for it (get_/list_), never one that writes, files, reports or changes it - if
    the connection has no read tool for it, add one (e.g. get_bike_condition beside report_bike_condition). The app
    still uses every connection tool when it acts FOR a person; this list only limits the agent alone.
- checks: person sign-in (signin), agent exchange (exchange), one deny per limit (call with a value over the
  limit), one hold per approval (call that triggers it).
- handover: {runbook: true, contract: true, terraform: false}.
"""


def describe(text):
    """A draft Czarter from a description. Returns {doc, errors, notes} - not saved."""
    text = (text or "").strip()
    if len(text) < 15:
        raise ValueError("describe the app in a sentence or two")
    if len(text) > 4000:
        raise ValueError("keep the description under 4000 characters")
    try:
        servers, tools = magertron_servers(), {}
        servers, tools = servers["servers"], servers["tools"]
    except Exception:  # noqa: BLE001 - a draft without the live list is still useful
        servers, tools = [], {}
    registered = [{"name": x["name"], "namespace": x["namespace"], "kind": x["kind"],
                   "tools": tools.get(f"{x['namespace']}/{x['name']}", [])[:40]} for x in servers]
    schema = json.dumps(json.loads((ROOT / "schema" / "czarter.schema.json").read_text()), separators=(",", ":"))
    example = (EXAMPLES / "tidewater.czarter.yaml").read_text()
    llm_ns, llm_name = DESCRIBE_LLM.split("/", 1)
    model = DESCRIBE_MODEL or next((x.get("model") for x in servers if x["name"] == llm_name and x.get("model")), "")
    body = {"messages": [
                {"role": "system", "content": DESCRIBE_RULES},
                {"role": "user", "content": "JSON Schema:\n" + schema + "\n\nExample (style only):\n" + example
                 + "\n\nRegistered in this Magertron:\n" + json.dumps(registered)
                 + "\n\nThe app to describe:\n" + text}],
            "max_tokens": 4000, "temperature": 0}
    if model:
        body["model"] = model
    tok, via_sa = _llm_token()
    who = getattr(CALLER, "user", None) or "local operator"
    print(f"describe: draft for {who} via {'the assistant service account' if via_sa else 'their own token'}",
          flush=True)
    st, j = _mag_post(f"/api/v1/llm/{llm_ns}/{llm_name}/v1/chat/completions", tok, body)
    if st == 403 and not via_sa and isinstance(j, dict) and j.get("error") == "llm_requires_service_account":
        raise RuntimeError("Magertron only lets a service account call a model, and this install does not give "
                           "Czartup one yet: the chart mounts the assistant's service account into Czartup "
                           f"(file {LLM_TOKEN_FILE}). Upgrade the chart, or start from a template or Enter Manually.")
    if st != 200:
        why = (j or {}).get("error") if isinstance(j, dict) else None
        if isinstance(why, dict):
            why = why.get("message")
        raise RuntimeError(f"the model ({DESCRIBE_LLM}) answered HTTP {st}" + (f": {str(why)[:200]}" if why else ""))
    try:
        content = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("the model's answer was not a chat completion")
    content = re.sub(r"^\s*```(?:ya?ml)?\s*|\s*```\s*$", "", content.strip())
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise RuntimeError(f"the model's draft was not valid YAML ({str(e)[:120]}) - try again or reword")
    if not isinstance(doc, dict):
        raise RuntimeError("the model's draft was not a Czarter - try again or reword")
    notes = sanitize_draft(doc)
    return {"doc": doc, "errors": errors_of(doc), "notes": notes, "model": f"{DESCRIBE_LLM}" + (f" ({model})" if model else "")}


def free_app_port(start: int = 8791, end: int = 8899) -> int:
    """The first local app port (127.0.0.1/localhost) no saved Czarter or example uses (0.2.73)."""
    used = set()
    for d in (STORE, EXAMPLES):
        for f in d.glob("*.czarter.yaml"):
            try:
                urls = ((yaml.safe_load(f.read_text()) or {}).get("identity", {}).get("app", {}) or {}).get("callbackUrls") or []
            except (yaml.YAMLError, OSError):
                continue
            for u in urls:
                m = re.match(r"https?://(?:127\.0\.0\.1|localhost):(\d+)", str(u))
                if m:
                    used.add(int(m.group(1)))
    return next((p for p in range(start, end + 1) if p not in used), start)


def sanitize_draft(doc):
    """Blank what belongs to one customer (as for a template) and say what is left to do."""
    doc["apiVersion"], doc["kind"] = "czarter.magertron.com/v1", "Czarter"
    m = doc.setdefault("metadata", {})
    m["owner"] = ""
    name = re.sub(r"[^a-z0-9-]+", "-", str(m.get("name") or "").lower()).strip("-")[:40] or "draft"
    base, n = name, 2
    while path_for(name).exists():
        name, n = f"{base}-{n}", n + 1
    m["name"] = name
    m["namespace"] = re.sub(r"[^a-z0-9-]+", "-", str(m.get("namespace") or name).lower()).strip("-") or name
    i = doc.setdefault("identity", {})
    idp, app, sts = i.setdefault("idp", {}), i.setdefault("app", {}), i.setdefault("sts", {})
    here = idp_default()
    if here:                                  # this install has its own IdP (a trial's Keycloak): use it
        idp.update(here)
        if here["provider"] != "auth0":
            i.setdefault("people", {}).pop("connections", None)
    # Where Create app stores the new app's secret - the same default a clone gets.
    # The app's sign-in callback (0.2.73): every Czarter so far runs its app locally on its own
    # 127.0.0.1:87xx port, so take the first port no saved Czarter or example uses yet, from 8791
    # (8790 is DayTrader's). The SI changes it to where the app really runs.
    port = free_app_port()
    app.update({"clientId": "", "callbackUrls": [f"http://127.0.0.1:{port}/auth/callback"],
                "logoutUrls": [f"http://127.0.0.1:{port}/"],
                "secretRef": f"vault:secret/{name}/{secret_dir(idp.get('provider'))}#client_secret"})
    # Keep the people the DESCRIPTION named (0.2.47) - only {email, name}; setup links or creates them.
    # Clearing the list here dropped every named approver from the draft.
    keep = []
    for x in sts.get("people") or []:
        if isinstance(x, dict) and "@" in str(x.get("email") or ""):
            keep.append({"email": str(x["email"]).strip(), **({"name": str(x["name"]).strip()} if x.get("name") else {})})
    sts["people"] = keep
    sts.setdefault("agent", {})["externalId"] = ""
    notes = []
    # The IdP is usually the same for every app here: take it from a saved Czarter.
    if not idp.get("domain"):
        for f in sorted(STORE.glob("*.czarter.yaml"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                other = (yaml.safe_load(f.read_text()) or {}).get("identity", {}).get("idp", {})
            except yaml.YAMLError:
                continue
            if other.get("provider") == idp.get("provider", "auth0") and other.get("domain"):
                idp["domain"] = other["domain"]
                notes.append(f"Identity: IdP domain {other['domain']} taken from {f.name.removesuffix('.czarter.yaml')} - check it.")
                break
    notes.append(("Identity: people " + ", ".join(x.get("name") or x["email"] for x in keep) + " - setup links or creates them. "
                  if keep else "Identity: add the people who sign in. ")
                 + f"Callback http://127.0.0.1:{port}/auth/callback is the first free local port - change it to where the app will run. "
                 + "Create app fills in the client ID.")
    # The owner signs in (0.2.77): a draft with an Owner email and no STS people gets the owner.
    own = str(((doc.get("metadata") or {}).get("owner")) or "").strip()
    sts = ((doc.setdefault("identity", {}) if isinstance(doc.get("identity"), dict) else {}).setdefault("sts", {}))
    if isinstance(sts, dict) and not any(isinstance(x, dict) and str(x.get("email") or "").strip() for x in sts.get("people") or []) and re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", own, re.I):
        sts["people"] = [{"email": own}]
        notes.append(f"Identity: added the owner {own} to STS people.")
    # An agent tool its connection does not list (0.2.84): a model name drafted from memory
    # (claude-3-5-haiku) on an LLM connection that offers others. Use the connection's own model;
    # on any other connection, add the tool to it (the SI builds it).
    cx = {x.get("name"): x for x in doc.get("connections") or [] if isinstance(x, dict)}
    for ag in ((doc.get("functionality") or {}).get("agents") or []):
        if not isinstance(ag, dict) or not isinstance(ag.get("tools"), list):
            continue
        fixed = []
        for t in ag["tools"]:
            srv, _, tool = str(t).partition("/")
            x = cx.get(srv)
            have = x.get("tools") if x and isinstance(x.get("tools"), list) else None
            if have is None or not tool or tool in have:
                fixed.append(t); continue
            if x.get("kind") == "llm":
                if have:
                    fixed.append(f"{srv}/{have[0]}")
                    notes.append(f"What it does: {ag.get('name', 'the agent')} uses {srv}/{have[0]} - {tool} is not a model {srv} offers.")
                else:
                    fixed.append(t)     # the page fills the model in from the registered server (0.2.85)
            else:
                have.append(tool); fixed.append(t)
                notes.append(f"Connections: added {tool} to {srv} - {ag.get('name', 'the agent')} uses it.")
        ag["tools"] = list(dict.fromkeys(fixed))
    # A money cap drafted as a limit (0.2.76): a "limit" with no tool, or one whose id or text says
    # budget/cap and per day/month, is really a budget. Move it, keeping the server and the amount.
    ctl0 = doc.get("controls") if isinstance(doc.get("controls"), dict) else None
    if ctl0 is not None:
        keep_l, moved = [], []
        llm = next((x.get("name") for x in doc.get("connections") or [] if isinstance(x, dict) and x.get("kind") == "llm"), "")
        for it in ctl0.get("limits") or []:
            if not isinstance(it, dict):
                continue
            txt = f"{it.get('id', '')} {it.get('description', '')}".lower()
            looks = bool(re.search(r"budget|\bcap\b|spend", txt)) and bool(re.search(r"month|daily|\bday\b|per (?:week|year)", txt))
            if not it.get("tool") or looks:
                amt = it.get("value") if isinstance(it.get("value"), (int, float)) else None
                if amt is None:
                    m_ = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", txt)
                    amt = float(m_.group(1).replace(",", "")) if m_ else None
                b = {"server": it.get("server") or llm, "description": it.get("description") or "Spending cap"}
                if amt is not None:
                    b["dailyUsd" if re.search(r"daily|\bday\b", txt) and "month" not in txt else "monthlyUsd"] = amt
                moved.append(b)
            else:
                keep_l.append(it)
        if moved:
            ctl0["limits"] = keep_l
            ctl0["budgets"] = (ctl0.get("budgets") or []) + moved
            notes.append("Controls: moved " + ", ".join(b["description"] for b in moved)
                         + " from Limits to Budgets - a money cap over time is a budget. Check the server and amount.")
    # A tool a limit or a hold names must be on its server (0.2.53): drafts named the held tool
    # (waive_late_fee) without listing it, so the policy would guard a tool nobody could call.
    conns = {x.get("name"): x for x in doc.get("connections") or [] if isinstance(x, dict)}
    ctl = doc.get("controls") or {}
    for item in (ctl.get("limits") or []) + (ctl.get("holds") or []):
        x = conns.get((item or {}).get("server"))
        t = (item or {}).get("tool")
        if x is not None and t and isinstance(x.get("tools"), list) and t not in x["tools"]:
            x["tools"].append(t)
            notes.append(f"Connections: added {t} to {x['name']} - the {'hold' if 'when' in item else 'limit'} "
                         f"{item.get('id', '')} uses it.")
    new = [x for x in doc.get("connections") or [] if x.get("namespace") in (None, m["namespace"]) and x.get("kind") == "mcp"]
    if new:
        notes.append("Connections: " + ", ".join(x.get("name", "?") for x in new)
                     + " will be built by the SI from the hand-over pack - check the tool names and arguments.")
    notes.append("Drafted by a model: check every step before you save.")
    return notes

# ---------- Set up everything (server side) ----------
# ⚠ ONE copy of the sequence, here. The page, the MCP tools and any IDE panel all call it,
# so they can never drift apart. Same steps, same rules as the page had:
#   dry  - previewed with a dry run;  hard - a failure stops everything after it.
# A preview whose dry run fails AFTER an earlier step that will change something is marked
# "depends" (e.g. People's access needs the role Agent service account creates).
SETUP_JOBS = {}                       # name -> {"done", "ok", "lines", "started", "by"}
_SETUP_LOCK = threading.Lock()
_NOTHING = re.compile(r"nothing to do|0 new grant|nothing to approve", re.I)
_BOOKKEEPING = re.compile(r"^(recorded \d+ results? in |wrote /)")


def _get(d, dotted, default=None):
    for k in dotted.split("."):
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _load(name):
    f = path_for(name)
    if not f.exists():
        raise ValueError(f"no Czarter named {name}")
    return yaml.safe_load(f.read_text()) or {}


def setup_steps(name):
    doc = _load(name)
    runs = czarter_status(name)["runs"]
    ok = lambda k: (runs.get(k) or {}).get("status") in ("done", "passed")
    who = AUTOMATED_IDPS.get(_get(doc, "identity.idp.provider"))
    mfa = _get(doc, "identity.people.mfa.required") is not False
    L = []
    # Always on for an automated IdP: once the app exists the step only finishes what is missing -
    # above all callback/logout URLs added to the Czarter after the app was created (the builder
    # learns the app's address later). Skipping it once "done" left those out of the IdP.
    if who:
        L.append({"act": "apply-idp-app", "dry": True, "hard": True,
                  "label": f"Update the app in {who}" if ok("identity.app") else f"Create the app in {who}"})
    if who and mfa and not ok("identity.mfa"):
        L.append({"act": "apply-idp-mfa", "label": "Add MFA for this app", "dry": ok("identity.app"), "hard": True,
                  "note": "" if ok("identity.app") else "dry run after the app exists"})
    L += [{"act": "plan", "label": "Plan", "hard": True},
          {"act": "apply-agent", "label": "Agent service account", "dry": True, "hard": True},
          {"act": "apply-register", "label": "Register servers", "dry": True},
          {"act": "apply-tools", "label": "Approve tools", "dry": True},
          {"act": "apply-grants", "label": "Tool grants", "dry": True},
          {"act": "apply-people", "label": "People's access", "dry": True, "hard": True}]
    if os.environ.get("CZARTUP_K8S_POLICIES") == "1":
        # The admin's yes to setup is the approval - no kubectl, no files to hand around.
        L.append({"act": "apply-policy", "label": "Install the app's policy", "dry": True, "hard": True})
    # The app's namespace asks External Policy (0.2.44) - else its limits and holds never run.
    L.append({"act": "apply-scope", "label": "Ask External Policy about the app's namespace", "dry": True})
    L += [{"act": "verify", "label": "Verify"},
          {"act": "runbook", "label": "Runbook"},
          {"act": "handover-pack", "label": "Build the hand-over pack"}]
    return L


def _summary(out, ok):
    lines = [l for l in (out or "").split("\n") if l.strip() and not _BOOKKEEPING.match(l.strip())]
    return lines[-1].strip() if lines else ("done" if ok else "failed")


def setup_preview(name):
    """Dry run of every step: what would change. Changes nothing."""
    errs = errors_of(_load(name))
    if errs:
        return {"ok": False, "errors": errs, "steps": [],
                "output": f"Fix the {len(errs)} validation error(s) first."}
    steps, changes_before = setup_steps(name), False
    for st in steps:
        if not st.get("dry"):
            st["preview"] = st.get("note") or "will run"
            continue
        try:
            r = run_action(name, st["act"], yes=False)
            st["preview"] = "\n".join(l for l in (r.get("output") or "").split("\n")
                                      if not re.match(r"^recorded \d+ results? in ", l)).strip()
            st["blocked"] = not r.get("ok")
        except Exception as e:  # noqa: BLE001
            st["preview"], st["blocked"] = f"Error: {e}", True
        st["nothing"] = bool(_NOTHING.search(st["preview"]))
        if st["blocked"] and changes_before:
            st["blocked"], st["depends"] = False, True
        if not st["blocked"] and not st["nothing"]:
            changes_before = True
    hard = next((x["label"] for x in steps if x.get("blocked") and x.get("hard")), None)
    return {"ok": hard is None, "steps": steps, "blocked_hard": hard}


_TOTAL = re.compile(r"^❌ (servers\.register|grants|tools\.approve|people\.roles): ")
_NOT_BUILT = re.compile(r"no submission for (\S+) in Inventory yet")


def _waits_on(out, unbuilt):
    """True when every failure in a step's output is about a server nobody has built yet."""
    bad = [l.strip() for l in (out or "").split("\n") if l.strip().startswith("❌")]
    # A step's own total ("❌ grants: 0 granted, ... 11 not granted") names no server: judge the lines it totals.
    bad = [l for l in bad if not _TOTAL.match(l)]
    return bool(unbuilt) and bool(bad) and all(any(n in l for n in unbuilt) for l in bad)


def _setup_worker(name, token, user):
    CALLER.token, CALLER.user = token, user
    job = SETUP_JOBS[name]
    unbuilt = []      # the app's own servers the SI has not built and submitted yet (0.2.81)
    try:
        for st in setup_steps(name):
            job["lines"].append(f"… {st['label']}")
            try:
                r = run_action(name, st["act"], yes=st["act"].startswith("apply-"))
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "output": str(e)}
            out = r.get("output") or ""
            if st["act"] == "apply-register":
                unbuilt = sorted(set(_NOT_BUILT.findall(out)))
            # Not built yet is not an error: it is the next step, and it is the SI's. Say so.
            if not r.get("ok") and _waits_on(out, unbuilt):
                job["lines"][-1] = f"⏳ {st['label']}: waiting for {', '.join(unbuilt)} to be built"
                job["steps"].append({"label": st["label"], "ok": True, "waiting": True, "hard": False,
                                     "summary": f"waiting for {', '.join(unbuilt)}"})
                continue
            mark = "✅" if r.get("ok") else ("❌" if st.get("hard") else "⚠️")
            summ = _summary(out, r.get("ok"))
            if not r.get("ok") and unbuilt:
                # Some failures are the unbuilt server, some are not (0.2.88): show a real one, never the 404s.
                real = [l.strip() for l in out.split("\n") if l.strip().startswith("❌") and not _TOTAL.match(l.strip())
                        and not any(n in l for n in unbuilt)]
                if real:
                    summ = real[0]
            job["lines"][-1] = f"{mark} {st['label']}: {summ}"
            job["steps"].append({"label": st["label"], "ok": bool(r.get("ok")), "hard": bool(st.get("hard")),
                                 "summary": summ})
            if not r.get("ok") and st.get("hard"):
                job["lines"] += ["", f'Stopped: "{st["label"]}" failed - the steps after it need it. '
                                     "Fix it and run Set up everything again."]
                job["ok"] = False
                return
        job["ok"] = all(x["ok"] for x in job["steps"])
        # Every step that must work did (0.2.86): the page says "set up", and lists what to look at.
        job["setUp"] = True
        job["check"] = [{"label": x["label"], "summary": x["summary"]} for x in job["steps"] if not x["ok"]]
        if unbuilt:
            # The server still to build goes to the IDE whatever else Verify found (0.2.89).
            job["waiting"] = unbuilt
            try:
                job["build"] = post_build(name, unbuilt, job.get("assignee") or user or "", user or "")
            except Exception as e:  # noqa: BLE001
                job["lines"].append(f"⚠️ could not post the build request: {e}")
            job["lines"] += ["", "Czartup™ is paused. Next: Push to your IDE to build and submit back to Magertron™ for review, "
                                 "staging and deploy. Then Review and Run again in Czartup™."]
        else:
            job["lines"] += ["", "Done. Each step's own result is in the checklist."]
    except Exception as e:  # noqa: BLE001
        job["lines"].append(f"❌ {e}")
        job["ok"] = False
    finally:
        job["done"] = True


# ---------- Build requests (0.2.87): Czartup pushes the build to the developer's IDE ----------
# When setup ends with servers nobody has built, Czartup posts a build request for them, assigned
# to a Magertron user. The IDE pulls it: the Magertron extension's "Builds waiting for you" list,
# or the my_builds / start_build MCP tools. When the server is submitted and an admin has approved
# it, the next admin request to Czartup finishes the setup - nobody comes back to press a button.
BUILDS_FILE = STORE / "builds.json"
_BUILDS_LOCK = threading.Lock()
_READY_STATES = {"approved", "staged", "registered", "active", "promoted"}


def _builds():
    try:
        return json.loads(BUILDS_FILE.read_text())
    except Exception:  # noqa: BLE001
        return []


def _save_builds(b):
    BUILDS_FILE.write_text(json.dumps(b, indent=2))


def post_build(name, servers, assignee, by=""):
    """One open build request per Czarter: a new one replaces an open one for the same app."""
    with _BUILDS_LOCK:
        b = [x for x in _builds() if not (x["czarter"] == name and x["status"] in ("open", "started"))]
        d = _load(name)
        conns = {x.get("name"): x for x in d.get("connections") or [] if isinstance(x, dict)}
        req = {"id": f"{name}-{int(time.time())}", "czarter": name, "status": "open", "assignee": assignee or by,
               "by": by, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "servers": [{"name": n, "purpose": (conns.get(n) or {}).get("purpose", ""),
                            "tools": (conns.get(n) or {}).get("tools", [])} for n in servers]}
        b.append(req)
        _save_builds(b)
        return req


def my_builds(user):
    return [x for x in _builds() if x["status"] in ("open", "started") and (not user or x.get("assignee") in (user, "", None))]


def start_build(bid, user):
    with _BUILDS_LOCK:
        b = _builds()
        x = next((y for y in b if y["id"] == bid), None)
        if not x:
            raise ValueError(f"no build request {bid}")
        x["status"], x["started"], x["startedBy"] = "started", time.strftime("%Y-%m-%dT%H:%M:%S%z"), user or ""
        _save_builds(b)
        return x


# Push to IDE (0.2.93): a one-time ticket for the build kit, put in an IDE link
# (cursor://magertron.magertron-submit/build?kit=...). The extension downloads the kit with the
# ticket alone - no token on the developer's machine. 15 minutes, a few uses (a retry is fine).
KIT_TICKETS = {}


def push_kit(name, ide):
    r = run_action(name, "handover-pack")
    if not r.get("ok"):
        raise RuntimeError(r.get("output") or "could not build the kit")
    import secrets as _s
    t = _s.token_urlsafe(24)
    now = time.time()
    for k in [k for k, v in KIT_TICKETS.items() if v["exp"] < now]:
        KIT_TICKETS.pop(k, None)
    KIT_TICKETS[t] = {"name": name, "exp": now + 900, "uses": 0, "by": getattr(CALLER, "user", "") or ""}
    return {"ticket": t, "name": name, "ide": ide}


def kit_files(ticket):
    v = KIT_TICKETS.get(ticket)
    if not v or v["exp"] < time.time() or v["uses"] >= 5:
        raise PermissionError("this link has expired - push it again from Czartup")
    v["uses"] += 1
    import base64
    root = STORE / "generated" / v["name"] / "handover"
    files = [{"path": str(p.relative_to(root)), "base64": base64.b64encode(p.read_bytes()).decode()}
             for p in sorted(root.rglob("*")) if p.is_file()]
    icon = STORE / f"{v['name']}.icon.png"
    if icon.exists():
        files.append({"path": "assets/icon.png", "base64": base64.b64encode(icon.read_bytes()).decode()})
    return {"czarter": v["name"], "files": files,
            "open": next((f["path"] for f in files if f["path"].endswith("tasks.md")), "AGENTS.md")}


_FINISH = {"next": 0.0}


def _finish_builds(tok):
    """Once a minute, on an authorised request: a build whose servers are submitted and approved
    in Magertron (or already registered) gets Set up everything again, as that caller."""
    if not tok or time.time() < _FINISH["next"]:
        return
    _FINISH["next"] = time.time() + 60
    todo = [x for x in _builds() if x["status"] in ("open", "started")]
    if not todo:
        return

    def run():
        CALLER.token, CALLER.user = tok, _jwt_sub(tok)
        from urllib.parse import quote
        for x in todo:
            try:
                ns = (_load(x["czarter"]).get("metadata") or {}).get("namespace", "")
                ready = True
                for srv in x["servers"]:
                    st, _ = _mag_get(f"/api/v1/servers/{ns}/{quote(srv['name'])}", tok)
                    if st == 200:
                        continue
                    st, j = _mag_get(f"/api/v1/inventory/servers?limit=50&q={quote(srv['name'])}", tok)
                    rows = (j.get("items") if isinstance(j, dict) else j) if st == 200 else []
                    if not any(isinstance(r, dict) and r.get("name") == srv["name"]
                               and str(r.get("state", "")).lower() in _READY_STATES for r in rows or []):
                        ready = False
                        break
                if not ready:
                    continue
                job = setup_run(x["czarter"], wait=600)
                if job.get("setUp") and not job.get("waiting"):
                    with _BUILDS_LOCK:
                        b = _builds()
                        for y in b:
                            if y["id"] == x["id"]:
                                y["status"], y["finished"] = "done", time.strftime("%Y-%m-%dT%H:%M:%S%z")
                        _save_builds(b)
            except Exception as e:  # noqa: BLE001
                print(f"czartup build {x.get('id')}: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=run, daemon=True).start()


def _fix_llm_tools(name):
    """An LLM connection lists its server's model, and the agent uses that model (0.2.90) -
    done here, on the saved Czarter, so it holds for the page, the IDE and the CLI alike."""
    try:
        d = _load(name)
        reg = {(x["name"], x["namespace"]): x.get("model") for x in magertron_servers().get("servers", [])
               if x.get("kind") == "llm" and x.get("model")}
    except Exception:  # noqa: BLE001
        return
    ns0, changed = (d.get("metadata") or {}).get("namespace"), False
    for x in d.get("connections") or []:
        if not isinstance(x, dict) or x.get("kind") != "llm":
            continue
        model = reg.get((x.get("name"), x.get("namespace") or ns0))
        if not model:
            continue
        if not x.get("tools"):
            x["tools"], changed = [model], True
        for ag in ((d.get("functionality") or {}).get("agents") or []):
            if not isinstance(ag, dict) or not isinstance(ag.get("tools"), list):
                continue
            new = []
            for t in ag["tools"]:
                srv, _, tool = str(t).partition("/")
                new.append(f"{srv}/{x['tools'][0]}" if srv == x["name"] and tool and tool not in x["tools"] else t)
            new = list(dict.fromkeys(new))
            if new != ag["tools"]:
                ag["tools"], changed = new, True
    if changed:
        path_for(name).write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=100))


def setup_run(name, wait=45):
    """Make every change. Runs in the background; waits up to `wait` seconds, then returns
    what has happened so far - poll setup_job() (the page) or czarter_status (MCP)."""
    _fix_llm_tools(name)
    errs = errors_of(_load(name))
    if errs:
        return {"ok": False, "done": True, "errors": errs,
                "lines": [f"Fix the {len(errs)} validation error(s) first."]}
    with _SETUP_LOCK:
        cur = SETUP_JOBS.get(name)
        if cur and not cur["done"]:
            return dict(cur, note="already running")
        SETUP_JOBS[name] = {"done": False, "ok": None, "lines": [], "steps": [],
                            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            "by": getattr(CALLER, "user", "") or ""}
        t = threading.Thread(target=_setup_worker, daemon=True,
                             args=(name, getattr(CALLER, "token", None), getattr(CALLER, "user", None)))
        t.start()
    t.join(timeout=wait)
    return setup_job(name)


def setup_job(name):
    j = SETUP_JOBS.get(name)
    return dict(j) if j else {"done": True, "ok": None, "lines": [], "steps": [], "none": True}


# ---------- Czartup as an MCP server ----------
# POST /mcp: MCP over HTTP (JSON-RPC, JSON replies - no stream). Behind Magertron(TM) it is
# registered with credential_mode=passthrough, so the caller's own Magertron token arrives in
# Authorization and Czartup acts as that person - exactly as for the web page (authorize()).
MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MCP_OPEN = {"initialize", "ping", "tools/list", "prompts/list"}   # discovery: no token needed
MCP_INSTRUCTIONS = (
    "Czartup creates and configures governed apps and agents on Magertron(TM). Use these tools "
    "WHENEVER the user wants to build an app or agent that signs people in, calls company systems or "
    "MCP servers, spends money, needs limits or approvals, or acts on someone's behalf - do NOT "
    "hand-write sign-in, policy, credentials or tool grants. Flow: describe_app (draft from a "
    "sentence) -> show the draft and save_czarter once the user agrees -> setup_preview -> "
    "setup_run with confirm=true once the user agrees -> get_handover and write its files into the "
    "repository -> build what AGENTS.md says. verify shows what is still missing at any time. "
    "If the user gives an icon for the app, set_icon. Every result ends with Next: - follow it.")

# annotations (MCP 2025-06-18): readOnlyHint tells IDEs which tools only read, so they can run
# those without asking and still ask before setup_run - the one tool that changes the platform
# (and save_czarter, which writes a Czarter). Hints, not security: the gateway and Czartup decide.
MCP_TOOLS = [
    {"name": "describe_app", "title": "Draft a Czarter", "annotations": {"title": "Draft a Czarter", "readOnlyHint": True, "idempotentHint": False, "openWorldHint": False},
     "description": "Draft a Czarter (the declaration of a governed app or agent) from a plain-language "
                    "description. Use first whenever the user asks to build an app or agent on Magertron. "
                    "Returns the draft as YAML and what is left to fill in. Saves nothing.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "What the app should do, who uses it, its limits and approvals."}},
                     "required": ["text"]}},
    {"name": "list_czarters", "title": "List Czarters", "annotations": {"title": "List Czarters", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}, "description": "List the saved Czarters, and whether each is valid.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_czarter", "title": "Read a Czarter", "annotations": {"title": "Read a Czarter", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}, "description": "Read one Czarter as YAML, with its validation errors. To a developer, 'the Czarter' means the Czarter AND "
                    "its hand-over pack: when they ask to get, update or follow the Czarter (or one of its sections - "
                    "approvals, identity, limits, the agent), call get_handover too, write its files, and change the code to "
                    "match the named AGENTS.md section. The YAML says what; AGENTS.md says how.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "save_czarter", "title": "Save a Czarter", "annotations": {"title": "Save a Czarter", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "description": "Save a Czarter (YAML). Refuses to replace an existing one unless overwrite is true. "
                    "Only save what the user has seen and agreed to.",
     "inputSchema": {"type": "object", "properties": {"yaml": {"type": "string"}, "overwrite": {"type": "boolean"}},
                     "required": ["yaml"]}},
    {"name": "czarter_status", "title": "Czarter status", "annotations": {"title": "Czarter status", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
     "description": "Where a Czarter stands: each step's latest result, the plan, runbook and hand-over "
                    "pack, and a Set up everything run in progress.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "setup_preview", "title": "Preview Set up everything", "annotations": {"title": "Preview Set up everything", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
     "description": "Dry run of Set up everything: what each step would change in the identity provider and "
                    "Magertron. Changes nothing. Show it to the user before setup_run.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "setup_run", "title": "Run Set up everything", "annotations": {"title": "Run Set up everything", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
     "description": "Make every change Set up everything previewed (identity app, agent account, servers, "
                    "tool approvals and grants, people's access), then verify and build the hand-over pack. "
                    "Needs confirm=true - only after the user agreed to the preview. Long runs continue in the "
                    "background: check czarter_status.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "confirm": {"type": "boolean"}},
                     "required": ["name", "confirm"]}},
    {"name": "verify", "title": "Verify", "annotations": {"title": "Verify", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
     "description": "Read-only checks: policy, people, agent, servers, tools, identity. Lists what is missing.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_handover", "title": "Get the hand-over pack", "annotations": {"title": "Get the hand-over pack", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
     "description": "Build the hand-over pack and return its files (AGENTS.md, specs, SERVER-SPECs, policy "
                    "draft, .env.example). Write each file at its path, relative to the repository root, "
                    "then build what AGENTS.md says.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
]

MCP_TOOLS += [
    {"name": "my_builds", "title": "Builds waiting for me",
     "annotations": {"title": "Builds waiting for me", "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
     "description": "The MCP servers Czartup has asked this developer to build: one entry per app, with each server's "
                    "purpose and tools. Use when the user asks what to build, or opens a new workspace.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "start_build", "title": "Start a build",
     "annotations": {"title": "Start a build", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "description": "Start a build request from my_builds: marks it started and returns its build kit (the hand-over "
                    "pack's files). Write each file at its path in an empty folder, then build what AGENTS.md says.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}]

MCP_TOOLS.append(
    {"name": "retire", "title": "Retire an app",
     "annotations": {"title": "Retire an app", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
     "description": "Retire an app: remove what setup created for it - its policy, its namespace from External Policy "
                    "(unless another app uses it), its role's tool grants, the role from its people (the people stay), "
                    "the agent's service account, the role, and its own servers - then move the Czarter to the trash. "
                    "ALWAYS call it first with confirm=false and show the user the list; call with confirm=true only "
                    "after they say yes. keep_servers=true leaves the servers registered. remove_idp=true also removes "
                    "the app's application in its IdP (Auth0 or Keycloak), its MFA and its client secret in Vault - only what Czartup "
                    "made for this app. People's IdP accounts always stay.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "the Czarter"},
         "confirm": {"type": "boolean", "description": "false = preview (default); true = do it"},
         "keep_servers": {"type": "boolean"},
         "remove_idp": {"type": "boolean", "description": "also remove the app from its IdP - Auth0 or Keycloak (and its secret from Vault)"}},
         "required": ["name"]}})

MCP_TOOLS.append(
    {"name": "set_icon", "title": "Set the app's icon",
     "annotations": {"title": "Set the app's icon", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "description": "Give the app its icon: a square PNG, 1024 x 1024 best (e.g. the user says 'use assets/logo.png as "
                    "the icon' - read the file and pass it base64). Stored with the Czarter; the next setup_run puts it "
                    "on each of the app's own servers in Magertron (never over an icon a person chose there), and the "
                    "hand-over pack carries it. server: only that server gets this one.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "the Czarter"},
         "image_base64": {"type": "string", "description": "the PNG, base64"},
         "server": {"type": "string", "description": "optional: one of the app's servers"}},
         "required": ["name", "image_base64"]}})

# The verbs (0.2.40): each one is an MCP prompt, so IDEs list them as slash commands - the developer
# picks /czartup-setup instead of remembering wording. NAME is optional: the repository's
# HANDOVER-VERSION names its Czarter.
_WHICH = ("the Czarter {n}" , "this repository's Czarter (its name is metadata.name in czarter.yaml here; if there is none, "
                              "list_czarters and ask me which)")
_NAME_ARG = {"name": "name", "description": "The Czarter (optional: this repository's)", "required": False}
VERBS = {
    "czartup-new": ("Create a governed app or agent with Czartup",
                    [{"name": "description", "description": "What the app should do", "required": True}], None),
    "czartup-setup": ("Preview, then make Magertron match the Czarter", [_NAME_ARG],
                      "Run Czartup setup for {which}: call setup_preview and show me what it would change. Run "
                      "setup_run with confirm=true only after I say yes. Then tell me the Next: step."),
    "czartup-build": ("Write the hand-over pack and build the app from it", [_NAME_ARG],
                      "Build {which}: call get_handover, write every file into this repository at its path, then "
                      "build what AGENTS.md says - stopping wherever it says the Magertron admin takes over."),
    "czartup-update": ("Get the updated Czarter and change the code to match", [_NAME_ARG,
                       {"name": "section", "description": "Optional: one AGENTS.md section, e.g. approvals", "required": False}],
                      "Update to the latest version of {which}: call get_czarter and get_handover, write the pack's files over the "
                      "old ones, and change the code to match AGENTS.md{section}. Tell me what you changed."),
    "czartup-verify": ("Prove the app against the live platform", [_NAME_ARG],
                       "Run Czartup verify for {which}. Show how many checks passed and failed; for each failure, "
                       "what to fix and who does it (me in Cursor, or the Magertron admin)."),
    "czartup-approvals": ("Build or fix the app's Approvals screen", [_NAME_ARG],
                          "Follow the Approvals section of {which} exactly: call get_handover, "
                          "write the pack, and make the app's Approvals screen, Resubmit and messages match "
                          "AGENTS.md and reference/approvals.py."),
    "czartup-retire": ("Retire an app: remove what setup created", [_NAME_ARG],
                       "Retire {which}: call retire with confirm=false and remove_idp=true (unless I say to keep the IdP app) and show me the list of what would be removed. "
                       "Call retire with confirm=true only after I say yes."),
    "czartup-status": ("Where the app stands, and what comes next", [_NAME_ARG],
                       "Call czarter_status for {which}. Tell me in a few lines what is done, what failed, and the "
                       "one next step."),
}
MCP_PROMPTS = [{"name": k, "description": d, "arguments": a} for k, (d, a, _) in VERBS.items()]


def prompt_text(name, args):
    """The message a verb sends, with its arguments filled in."""
    if name == "czartup-new":
        desc = args.get("description", "")
        return (f"Build this as a governed app on Magertron(TM): {desc}\n\nUse the Czartup tools: describe_app, "
                "show me the draft and save it once I agree, then setup_preview, then setup_run with "
                "confirm=true once I agree, then get_handover - write its files into this repository - and "
                "build what AGENTS.md says. Do not hand-write sign-in, policy, credentials or grants.")
    n = (args.get("name") or "").strip()
    which = _WHICH[0].format(n=n) if n else _WHICH[1]
    sec = (args.get("section") or "").strip()
    return VERBS[name][2].format(which=which, section=f" (its {sec} section)" if sec else "")


def _txt(t):
    return {"type": "text", "text": t}


# ---------- "Next:" (backlog 50) ----------
# Every tool result ends with what comes next, worked out from the Czarter's own state, so an IDE
# can walk someone who has never seen Czartup through the whole path.
_ADMIN_POLICY = "STOP - admin: install the app's policy with `kubectl apply -f policy/{n}-configmap.yaml` (from get_handover), then call verify."


def next_hint(name, failed_rows=None):
    """One line: what to do next for this Czarter."""
    try:
        doc = _load(name)
    except Exception:  # noqa: BLE001
        return "describe_app to draft one, or list_czarters to see what is saved."
    if errors_of(doc):
        return "fix the validation errors above, then save_czarter with overwrite=true."
    job = setup_job(name)
    if not job.get("none") and not job.get("done"):
        return "setup is still running - call czarter_status in a minute."
    st = czarter_status(name)
    runs = st["runs"]
    ok = lambda k: (runs.get(k) or {}).get("status") in ("done", "passed")
    if not any(k.startswith(("identity.", "sts.", "register.", "tools.", "grants", "people.")) for k in runs):
        return "setup_preview, show it to the user, then setup_run with confirm=true once they agree."
    rows = failed_rows if failed_rows is not None else \
        [(k, v.get("detail") or "") for k, v in runs.items() if v.get("status") in ("failed", "partial")]
    reg = [k for k, _ in rows if k.startswith(("register.", "connections."))]
    if reg:
        if not st.get("pack"):
            return "get_handover, write its files into the repository, then Build the servers (AGENTS.md, step 1)."
        return ("STOP - the servers are not registered yet: submit each server.json (right-click > Magertron: Submit), "
                "then the Magertron admin runs setup again (setup_preview, then setup_run) to register them.")
    pol = [k for k, _ in rows if k.startswith(("controls.policy", "controls.collector", "controls.limit.",
                                               "controls.hold.", "controls.agent-alone"))
           and ".gateway" not in k]   # the live gateway check says its own fix (token, grant, policy)
    if pol:
        if any(k == "controls.collector" for k, _ in rows):
            return "STOP - admin: this Magertron install's OPA does not collect app policies yet - upgrade the chart, then verify."
        if os.environ.get("CZARTUP_K8S_POLICIES") == "1":     # Czartup installs it itself (0.2.42)
            return "run setup (setup_preview, then setup_run once the user agrees) - it installs the app's policy - then verify."
        return _ADMIN_POLICY.format(n=name)
    if rows:
        k, d = rows[0]
        return f"fix `{k}`: {d[:240]}" + (" - then verify." if d else " Then verify.")
    if not st.get("pack"):
        return "get_handover, write its files into the repository, then Build what AGENTS.md says."
    if not ok("controls.policy"):
        return "verify - it checks the policy, servers, grants and identity against the live platform."
    return "run the checks in czarter.yaml as live calls through the gateway (AGENTS.md); when they pass, the app is done."


def save_icon(nm, image_b64, srv=""):
    """(message, ok). The app's icon (or one server's), checked to be a square PNG, stored beside the Czarter."""
    import base64, binascii
    doc = _load(nm)
    if srv and srv not in {x.get("name") for x in doc.get("connections") or []}:
        return f"{nm} has no server named {srv}.", False
    try:
        data = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return "That is not base64.", False
    sys.path.insert(0, str(ROOT))
    import apply as _ap
    why = _ap.check_icon(data)
    if why:
        return f"Not used: the icon is {why}. A square PNG, 1024 x 1024, works best.", False
    (STORE / (f"{nm}.{srv}.icon.png" if srv else f"{nm}.icon.png")).write_bytes(data)
    return (f"Saved the icon for {nm}" + (f" (server {srv} only)" if srv else "") + f" - {len(data) // 1024} KB. "
            "Register servers puts it on the app's servers."), True


def _with_next(blocks, line):
    """Append the Next: line to the first text block."""
    for b in blocks:
        if b.get("type") == "text":
            b["text"] = b["text"].rstrip() + f"\n\nNext: {line}"
            break
    return blocks


def mcp_tool(name, a):
    """(content blocks, is_error) for one tools/call - each ending with Next: (backlog 50)."""
    a = a or {}
    blocks, err = _mcp_tool(name, a)
    nm = a.get("name") or ""
    try:
        if name == "describe_app":
            line = ("fix what the error says, or ask the user, then describe_app again." if err else
                    "show the user the draft and what is still to do; save_czarter once they agree.")
        elif name == "list_czarters":
            line = "get_czarter for one of them, or describe_app to draft a new one."
        elif name == "save_czarter":
            doc = yaml.safe_load(a.get("yaml") or "") if not err else None
            nm = ((doc or {}).get("metadata") or {}).get("name", "") if isinstance(doc, dict) else ""
            line = next_hint(nm) if nm else "fix what the error says, then save_czarter again."
        elif name == "setup_preview":
            line = ("fix the blocked step it names, then setup_preview again." if err else
                    "ask the user; on yes, setup_run with confirm=true.")
        elif name == "verify":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            failed = [(m.group(1), m.group(2)) for m in re.finditer(r"^❌ ([^:\s]+): (.*)$", text, re.M)]
            line = next_hint(nm, failed) if nm else "verify needs the Czarter's name."
        elif name == "get_handover" and not err:
            line = ("write every file at its path, then follow AGENTS.md from 'Start here' - it stops "
                    "at each step that belongs to the Magertron admin.")
        elif name == "retire":
            line = None        # the app is going away: its old setup hints would only mislead
        elif name == "set_icon":
            line = ("fix what the error says and try again." if err else
                    "setup_preview, then setup_run once the user agrees - Register servers puts the icon on the app's servers.")
        else:
            line = next_hint(nm) if nm else None
    except Exception:  # noqa: BLE001 - a hint never breaks a tool
        line = None
    return (_with_next(blocks, line) if line else blocks), err


def _mcp_tool(name, a):
    if name == "describe_app":
        try:
            d = describe(a.get("text", ""))
        except (ValueError, RuntimeError) as e:
            return [_txt(str(e))], True
        y = yaml.safe_dump(d["doc"], sort_keys=False, allow_unicode=True, width=100)
        notes = "\n".join(f"- {n}" for n in d.get("notes") or [])
        errs = "\n".join(f"- {e['path']}: {e['message']}" for e in d.get("errors") or [])
        return [_txt(f"Draft Czarter (NOT saved) - drafted by {d.get('model')}.\n\nStill to do:\n{notes}"
                     + (f"\n\nValidation:\n{errs}" if errs else "") + f"\n\n```yaml\n{y}```")], False
    if name == "list_czarters":
        rows = []
        for f in sorted(STORE.glob("*.czarter.yaml")):
            d = yaml.safe_load(f.read_text()) or {}
            rows.append(f"- {f.name.removesuffix('.czarter.yaml')} ({'valid' if not errors_of(d) else 'has errors'})")
        return [_txt("\n".join(rows) or "No Czarters saved yet.")], False
    if name == "get_czarter":
        d = _load(a.get("name", ""))
        errs = errors_of(d)
        return [_txt((f"{len(errs)} validation error(s):\n" + "\n".join(f"- {e['path']}: {e['message']}" for e in errs) + "\n\n" if errs else "")
                     + "```yaml\n" + yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=100) + "```"
                     + "\n\nHow to build it is in the hand-over pack, not here: if the user asked to update or follow the "
                       "Czarter, call get_handover, write its files, and make the code match AGENTS.md.")], False
    if name == "save_czarter":
        try:
            doc = yaml.safe_load(a.get("yaml") or "")
        except yaml.YAMLError as e:
            return [_txt(f"Not valid YAML: {e}")], True
        if not isinstance(doc, dict):
            return [_txt("Not a Czarter.")], True
        nm = (doc.get("metadata") or {}).get("name", "")
        f = path_for(nm)
        if f.exists() and not a.get("overwrite"):
            return [_txt(f"A Czarter named {nm} exists already - pass overwrite=true to replace it.")], True
        f.write_text("# Czarter - written by Magertron Czartup(TM) (MCP)\n"
                     + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
        errs = errors_of(doc)
        return [_txt(f"Saved {nm}." + (f" {len(errs)} validation error(s) to fix before setup:\n"
                                        + "\n".join(f"- {e['path']}: {e['message']}" for e in errs) if errs else
                                        " Valid."))], False
    if name == "czarter_status":
        nm = a.get("name", "")
        _load(nm)
        st, job = czarter_status(nm), setup_job(nm)
        L = [f"{k}: {v.get('status')} ({(v.get('done_at') or '')[:16]})"
             + (f" - {v['detail']}" if v.get("detail") else "") for k, v in st["runs"].items()]
        L.append(f"plan: {'generated' if st.get('plan') else 'not yet'}; runbook: "
                 f"{'yes' if st.get('runbook') else 'not yet'}; hand-over pack: {'built' if st.get('pack') else 'not yet'}")
        if not job.get("none"):
            L += ["", "Set up everything: " + ("finished" if job["done"] else "RUNNING"), *job["lines"]]
        return [_txt("\n".join(L))], False
    if name == "setup_preview":
        r = setup_preview(a.get("name", ""))
        if r.get("errors"):
            return [_txt(r["output"] + "\n" + "\n".join(f"- {e['path']}: {e['message']}" for e in r["errors"]))], True
        L = []
        for x in r["steps"]:
            tag = ("BLOCKED" if x.get("blocked") else "nothing to do" if x.get("nothing")
                   else "depends on the steps before it" if x.get("depends") else "will change")
            L.append(f"## {x['label']} - {tag}\n{(x.get('preview') or '')[:1500]}")
        head = (f'"{r["blocked_hard"]}" is blocked - fix that first; setup_run would stop there.'
                if r.get("blocked_hard") else "Ready. Ask the user, then call setup_run with confirm=true.")
        return [_txt(head + "\n\n" + "\n\n".join(L))], bool(r.get("blocked_hard"))
    if name == "setup_run":
        if a.get("confirm") is not True:
            return [_txt("Not run: needs confirm=true, after the user has agreed to setup_preview.")], True
        r = setup_run(a.get("name", ""))
        tail = "" if r.get("done") else "\n\nStill running - call czarter_status for progress."
        return [_txt("\n".join(r.get("lines") or []) + tail)], r.get("done") and r.get("ok") is False
    if name == "verify":
        r = run_action(a.get("name", ""), "verify")
        return [_txt(r.get("output") or "")], not r.get("ok")
    if name == "my_builds":
        b = my_builds(getattr(CALLER, "user", "") or "")
        if not b:
            return [_txt("Nothing to build: no build requests are waiting for you.")], False
        return [_txt("\n".join(f"- {x['id']} ({x['status']}): app {x['czarter']} - build "
                                + "; ".join(f"{s_['name']} ({s_.get('purpose') or 'MCP server'}; {len(s_.get('tools') or [])} tools)"
                                            for s_ in x["servers"]) for x in b)
                     + "\nStart one with start_build(id).")], False
    if name == "start_build":
        try:
            x = start_build(a.get("id", ""), getattr(CALLER, "user", "") or "")
        except ValueError as e:
            return [_txt(str(e))], True
        blocks, err = _mcp_tool("get_handover", {"name": x["czarter"]})
        if not err:
            blocks[0]["text"] = (f"Build request {x['id']} started: build " + ", ".join(s_["name"] for s_ in x["servers"])
                                 + ". When each server is done, submit it to Magertron with the Magertron extension "
                                   "(right-click > Magertron: Submit). An admin reviews and approves it, and Czartup "
                                   "connects it - nothing else to do.\n\n" + blocks[0]["text"])
        return blocks, err
    if name == "get_handover":
        nm = a.get("name", "")
        r = run_action(nm, "handover-pack")
        if not r.get("ok"):
            return [_txt(r.get("output") or "could not build the pack")], True
        root = STORE / "generated" / nm / "handover"
        files = [p for p in sorted(root.rglob("*")) if p.is_file()]
        blocks = [_txt(f"Hand-over pack for {nm}: {len(files)} files. Write each one at its path relative to "
                       "the repository root (create folders as needed), then follow AGENTS.md.\n"
                       + "\n".join(f"- {p.relative_to(root)}" for p in files))]
        icon = STORE / f"{nm}.icon.png"
        if icon.exists():
            import base64
            blocks[0]["text"] += "\n- assets/icon.png (the app's icon)"
            blocks.append({"type": "resource", "resource": {"uri": f"czartup://{nm}/handover/assets/icon.png",
                           "mimeType": "image/png", "blob": base64.b64encode(icon.read_bytes()).decode()}})
        for p in files:
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            blocks.append({"type": "resource", "resource": {
                "uri": f"czartup://{nm}/handover/{p.relative_to(root)}",
                "mimeType": "text/markdown" if p.suffix == ".md" else "text/plain", "text": text}})
        return blocks, False
    if name == "retire":
        out, ok = retire_app((a.get("name") or "").strip(), bool(a.get("confirm")), bool(a.get("keep_servers")),
                             bool(a.get("remove_idp")))
        if ok and not a.get("confirm"):
            out += "\n\nNothing was changed."
        return [_txt(out)], not ok
    if name == "set_icon":
        msg, ok = save_icon(a.get("name", ""), a.get("image_base64") or "", (a.get("server") or "").strip())
        return [_txt(msg)], not ok
    return [_txt(f"unknown tool {name}")], True


def mcp_handle(msg):
    """One JSON-RPC message -> reply dict, or None for a notification."""
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if mid is None:
        return None                              # notifications/initialized and friends
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    try:
        if method == "initialize":
            v = params.get("protocolVersion")
            return ok({"protocolVersion": v if v in MCP_VERSIONS else MCP_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                       "serverInfo": {"name": "czartup", "title": "Magertron Czartup", "version": "0.2.96"},
                       "instructions": MCP_INSTRUCTIONS})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": MCP_TOOLS})
        if method == "tools/call":
            try:
                content, err = mcp_tool(params.get("name", ""), params.get("arguments"))
            except ValueError as e:
                content, err = [_txt(str(e))], True
            except subprocess.TimeoutExpired:
                content, err = [_txt("timed out")], True
            return ok({"content": content, "isError": bool(err)})
        if method == "prompts/list":
            return ok({"prompts": MCP_PROMPTS})
        if method == "prompts/get":
            pn = params.get("name")
            if pn not in VERBS:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown prompt"}}
            return ok({"description": VERBS[pn][0], "messages": [{
                "role": "user", "content": _txt(prompt_text(pn, params.get("arguments") or {}))}]})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)[:500]}}

# ---------- Connect Czartup to IDEs (it registers ITSELF, as the signed-in admin) ----------
# The gateway only routes https://, so Czartup is registered at its public address
# (<magertron>/czartup/mcp - the same route the page uses). credential_mode=passthrough:
# the caller's own Magertron token reaches Czartup, which acts as them (authorize()).
# ⚠ Passthrough of a platform token is right ONLY because Czartup is first-party, in the
# cluster, and already receives that token from the page - never a pattern for others.
MCP_NS = os.environ.get("CZARTUP_MCP_NAMESPACE", "mcp-prod")
MCP_URL = os.environ.get("CZARTUP_MCP_URL", PUBLIC_API.rstrip("/") + "/czartup/mcp")
MCP_TOOL_NAMES = None   # filled from MCP_TOOLS below


def _czartup_tool_names(tok):
    """Every tool name Magertron holds a definition for on mcp-system/czartup, in any state."""
    from urllib.parse import quote
    seen, cursor = set(), None
    for _ in range(20):
        st, j = _mag_get(f"/api/v1/inventory/tools?server_namespace={quote(MCP_NS)}&server_name=czartup"
                         + (f"&cursor={quote(cursor)}" if cursor else ""), tok)
        if st != 200 or not isinstance(j, dict):
            raise RuntimeError(f"could not read Czartup's tool definitions (HTTP {st})")
        seen |= {d.get("tool_name") for d in (j.get("items") or [])
                 if d.get("server_namespace") == MCP_NS and d.get("server_name") == "czartup"}
        cursor = j.get("next_cursor")
        if not cursor:
            break
    return seen


def _pending_czartup_tools(tok):
    from urllib.parse import quote
    out, cursor = [], None
    for _ in range(20):
        st, j = _mag_get("/api/v1/inventory/tools?status=pending" + (f"&cursor={quote(cursor)}" if cursor else ""), tok)
        if st != 200 or not isinstance(j, dict):
            raise RuntimeError(f"could not read tool definitions (HTTP {st})")
        out += [d for d in (j.get("items") or [])
                if d.get("server_namespace") == MCP_NS and d.get("server_name") == "czartup"]
        cursor = j.get("next_cursor")
        if not cursor:
            break
    return out


ICON = STATIC / "magertron-icon.png"   # the Magertron(TM) logo - Czartup's icon in the console


def _mag_status(path, token):
    """HTTP status only - for answers that are not JSON (an icon is an image)."""
    import urllib.request, urllib.error
    req = urllib.request.Request(API.rstrip("/") + path, method="GET", headers={
        "Authorization": "Bearer " + token, "User-Agent": "magertron-czarter/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


# Logos earlier Czartup versions uploaded (sha256 of the bytes). Connect replaces one of THESE with
# the current logo; any other icon was chosen by a person in the console and is left alone.
OWN_OLD_ICONS = {"ba314e2e066c8a1015d53961f9ba173ce9cd50e5edc58b332ae99c6592d36938",   # 0.2.10 wide logo
                 "56515564dd5eb126e7c0b59d73f3684dcdc05512231994a8a8e1e1da415437b4",  # 0.2.12 dark square tile
                 "c715e83f46063491c6ac34767bcc0f3e30c528ab77e09ba865c87ff5168dc948",  # 0.2.13 slate tile, 128px
                 "aafbd16d0e6fc49360cff5282a3c60621fc73dcc68b5285ea665d7e565ee8da7"}   # 0.2.14 slate tile, 1024px


def _mag_bytes(path, token):
    import urllib.request, urllib.error
    req = urllib.request.Request(API.rstrip("/") + path, method="GET", headers={
        "Authorization": "Bearer " + token, "User-Agent": "magertron-czarter/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:  # noqa: BLE001
        return 0, b""


def _mag_put(path, token, body):
    import urllib.request, urllib.error
    req = urllib.request.Request(API.rstrip("/") + path, data=json.dumps(body).encode(), method="PUT", headers={
        "Authorization": "Bearer " + token, "User-Agent": "magertron-czarter/1",
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _ensure_icon(tok, apply, L):
    """Czartup's server shows the Magertron logo in the console - set at Connect, never by hand.
    Only when it has none: an icon someone chose in the console is never replaced."""
    import base64
    if not ICON.exists():
        return
    import hashlib
    st, cur = _mag_bytes(f"/api/v1/servers/{MCP_NS}/czartup/icon", tok)
    if st == 200:
        if hashlib.sha256(cur).hexdigest() not in OWN_OLD_ICONS or cur == ICON.read_bytes():
            return                          # the current logo, or an icon a person chose
        if not apply:
            L.append("would update its icon (the Magertron logo, full-size)")
            return
    elif not apply:
        L.append("would set its icon (the Magertron logo)")
        return
    st = _mag_put(f"/api/v1/servers/{MCP_NS}/czartup/icon", tok,
                  {"image_base64": base64.b64encode(ICON.read_bytes()).decode()})
    L.append("✅ icon set (the Magertron logo)" if st in (200, 201, 204)
             else f"⚠️ could not set its icon (HTTP {st}) - set it on the server's page")


def ide_config():
    url = PUBLIC_API.rstrip("/") + f"/mcp/{MCP_NS}/czartup/"
    # No token in the file: the IDE signs in itself (MCP OAuth - Magertron is the
    # authorization server). Its first call gets a 401 pointing at the sign-in;
    # the person signs in to the console and clicks Allow.
    return {"mcpServers": {"czartup": {"url": url}}}


def connect_ide(apply=False):
    """Register Czartup on this Magertron as an MCP server, approve it and its tools.
    Dry run unless apply. Idempotent: already done -> says so."""
    from urllib.parse import quote
    tok, L = _token(), []
    names = {t["name"] for t in MCP_TOOLS}
    if not MCP_URL.lower().startswith("https://"):
        return {"ok": False, "lines": [f"Czartup's public address is {MCP_URL} - the gateway only routes https://. "
                                       "Set czartup.publicUrl (or CZARTUP_MCP_URL) to an https address."]}
    st, cur = _mag_get(f"/api/v1/servers/{MCP_NS}/czartup", tok)
    if st in (401, 403):
        return {"ok": False, "lines": [f"Magertron refused (HTTP {st}) - registering a server needs an admin."]}
    exists = st == 200 and isinstance(cur, dict)
    if exists:
        mode = cur.get("credential_mode")
        state = str(cur.get("state") or cur.get("status") or "")
        L.append(f"✅ {MCP_NS}/czartup is registered at {cur.get('endpoint_url')}" + (f" (state: {state})" if state else ""))
        if state and state.lower() not in ("running", "ready", "approved", "healthy", "active", "deployed"):
            if not apply:
                L.append(f"would approve it (state is {state})")
            else:
                st2, o2 = _mag_post(f"/api/v1/servers/{MCP_NS}/czartup/approve", tok, {})
                why = (o2 or {}).get("error") if isinstance(o2, dict) else ""
                L.append("✅ approved" if st2 in (200, 201, 204) else
                         f"❌ approving it returned HTTP {st2}" + (f": {str(why)[:200]}" if why else "")
                         + " - see the server's page in the console (probe result)")
        if mode and mode != "passthrough":
            L.append(f"❌ its credential mode is '{mode}', not 'passthrough' - Czartup would not receive the caller's "
                     "token. Change it on the server's page (Edit), or delete it and connect again.")
    elif not apply:
        L.append(f"would register {MCP_NS}/czartup at {MCP_URL} (external, credential mode passthrough) and approve it")
    else:
        st, out = _mag_post("/api/v1/servers", tok, {
            "type": "external", "name": "czartup", "namespace": MCP_NS, "endpoint_url": MCP_URL,
            "auth_type": "none", "credential_mode": "passthrough", "routing_mode": "v3",
            "upstream_http_version": "http1", "tls_min_version": "tls1_2", "forward_identity": False})
        if st not in (200, 201):
            err = (out or {}).get("error") if isinstance(out, dict) else ""
            return {"ok": False, "lines": [f"❌ could not register {MCP_NS}/czartup (HTTP {st}{': ' + str(err)[:200] if err else ''})"]}
        L.append(f"✅ registered {MCP_NS}/czartup at {MCP_URL} (credential mode passthrough)")
        st2, o2 = _mag_post(f"/api/v1/servers/{MCP_NS}/czartup/approve", tok, {})
        why = (o2 or {}).get("error") if isinstance(o2, dict) else ""
        L.append("✅ approved" if st2 in (200, 201, 204) else
                 f"❌ approving it returned HTTP {st2}" + (f": {str(why)[:200]}" if why else "")
                 + " - press Connect again in a minute (the gateway may still be probing it)")
        _ensure_icon(tok, True, L)
    if exists:
        _ensure_icon(tok, apply, L)
    elif not apply:
        L.append("would set its icon (the Magertron logo)")
    # After an upgrade Czartup has tools Magertron has never seen (0.2.21 added set_icon): the gateway
    # lists a server's tools only when it probes it, so re-probe Czartup itself, then approve below.
    probed = False
    if exists:
        try:
            known = _czartup_tool_names(tok)
        except RuntimeError as e:
            known = None
            L.append(f"⚠️ {e}")
        new = sorted(names - known) if known is not None else []
        if new and not apply:
            L.append(f"would re-probe {MCP_NS}/czartup - {len(new)} new tool(s) Magertron has not seen: {', '.join(new)}")
        elif new:
            st3, _ = _mag_post(f"/api/v1/servers/{MCP_NS}/czartup/probe", tok, {})
            probed = st3 in (200, 201, 202, 204)
            L.append(f"✅ re-probed {MCP_NS}/czartup for {len(new)} new tool(s): {', '.join(new)}" if st3 in (200, 201, 202, 204)
                     else f"❌ re-probing {MCP_NS}/czartup returned HTTP {st3} - use Re-probe on its page in the console")
    # Tool definitions appear once the gateway has listed Czartup's tools: wait briefly.
    pend = []
    if exists or apply:
        for i in range(10 if apply and (probed or not exists) else 1):   # new definitions take a moment
            try:
                pend = _pending_czartup_tools(tok)
            except RuntimeError as e:
                L.append(f"⚠️ {e}"); break
            if pend or not apply:
                break
            time.sleep(3)
        mine = [d for d in pend if d.get("tool_name") in names]
        other = [d for d in pend if d.get("tool_name") not in names]
        if not apply:
            if mine:
                L.append(f"would approve {len(mine)} Czartup tool definition(s): {', '.join(sorted(d['tool_name'] for d in mine))}")
        else:
            ok = 0
            for d in mine:
                s2, _ = _mag_post(f"/api/v1/inventory/tools/{quote(str(d['id']), safe='')}/approve", tok, {})
                ok += s2 in (200, 201, 204)
            if mine:
                L.append(f"{'✅' if ok == len(mine) else '⚠️'} approved {ok} of {len(mine)} tool definition(s)")
            elif not exists:
                L.append("❌ no tool definitions yet - the gateway lists them shortly after approval; press Connect again in a minute")
        for d in other:
            L.append(f"⚠️ left pending: {d.get('tool_name')} (not a Czartup tool - review it in Inventory > Tool Approvals)")
    if exists and not pend and not any(x.startswith(("❌", "would ", "✅ re-probed")) for x in L):
        L.append("✅ nothing to do - Czartup is connected")
    return {"ok": not any(x.startswith("❌") for x in L), "lines": L, "config": ide_config(),
            "dry": not apply}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        # The Magertron(TM) console shows Czartup inside its Czartup panel (an iframe, same site).
        # The console enforces Cross-Origin-Embedder-Policy, and a page it frames must carry one too
        # or the browser refuses to show it. frame-ancestors 'self': only that console may frame us.
        self.send_header("Cross-Origin-Embedder-Policy", "credentialless")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        super().end_headers()

    def send(self, code, body, ctype="application/json", filename=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def authorize(self, p):
        """Hosted: every /api/ call except status runs as the caller, or not at all."""
        CALLER.token = CALLER.user = None
        if not HOSTED or not (p.startswith("/api/") or p.rstrip("/") == "/mcp") or p == "/api/status" \
                or p.startswith("/api/kit/"):       # the one-time ticket in the path is the credential
            return True
        h = self.headers.get("Authorization") or ""
        tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
        ok, who = caller_ok(tok)
        if not ok:
            self.send(401, {"error": who})
            return False
        CALLER.token, CALLER.user = tok, who
        _self_heal(tok)                         # first admin after an upgrade approves Czartup's new tools
        _finish_builds(tok)                     # a build that was submitted and approved finishes its setup
        return True

    def do_GET(self):
        p = urlparse(self.path).path
        if not self.authorize(p):
            return
        try:
            if p == "/api/status":
                h = self.headers.get("Authorization") or ""
                tok = h[7:].strip() if h.lower().startswith("bearer ") else ""
                cl = _jwt_claims(tok) if tok else {}
                return self.send(200, {"api": PUBLIC_API, "store": "" if HOSTED else str(STORE),
                                       "hosted": HOSTED, "user": cl.get("sub") or "", "email": cl.get("email") or "",
                                       "version": VERSION})
            if p == "/api/czarters":
                items = []
                for f in sorted(STORE.glob("*.czarter.yaml")):
                    d = yaml.safe_load(f.read_text()) or {}
                    m = d.get("metadata", {})
                    items.append({"name": f.name.removesuffix(".czarter.yaml"), "tenant": m.get("tenant"),
                                  "template": m.get("template"), "valid": not errors_of(d)})
                return self.send(200, items)
            if p == "/api/magertron/servers":
                try:
                    return self.send(200, magertron_servers())
                except Exception as e:  # noqa: BLE001
                    return self.send(200, {"error": str(e), "servers": [], "tools": {}})
            if p == "/api/idp-default":
                return self.send(200, idp_default() or {})
            if p.startswith("/api/kit/"):
                try:
                    return self.send(200, kit_files(p.split("/")[3]))
                except PermissionError as e:
                    return self.send(410, {"error": str(e)})
            if p == "/api/builds":
                return self.send(200, {"builds": my_builds(getattr(CALLER, "user", "") or "")})
            if p == "/api/people/match":
                from urllib.parse import parse_qs
                try:
                    return self.send(200, people_match((parse_qs(urlparse(self.path).query).get("email") or [""])[0]))
                except Exception as e:  # noqa: BLE001
                    return self.send(200, {"error": str(e), "users": []})
            if p == "/api/free-port":
                return self.send(200, {"port": free_app_port()})
            if p == "/api/templates":
                out = []
                for f in sorted(EXAMPLES.glob("*.czarter.yaml")):
                    d = yaml.safe_load(f.read_text())
                    out.append({"id": f.name.removesuffix(".czarter.yaml"),
                                "template": d.get("metadata", {}).get("template"), "doc": d})
                return self.send(200, out)
            if p.startswith("/api/setup/"):
                return self.send(200, setup_job(p.split("/")[3]))
            if p.rstrip("/") == "/mcp":
                return self.send(405, {"error": "POST JSON-RPC to /mcp (no event stream)"})
            if p.startswith("/api/czarters/") and p.endswith("/status"):
                return self.send(200, czarter_status(p.split("/")[3]))
            if p.startswith("/api/czarters/"):
                f = path_for(p.rsplit("/", 1)[1])
                if not f.exists():
                    return self.send(404, {"error": "not found"})
                d = yaml.safe_load(f.read_text())
                return self.send(200, {"doc": d, "errors": errors_of(d)})
            if p.startswith("/api/download/"):
                # /api/download/<czarter>/<zip> - only zips Czartup exported for that Czarter
                _, _, _, name, fn = p.split("/", 4)
                path_for(name)                                  # validates the name
                d = (STORE / "generated" / name / "exports").resolve()
                f = (d / fn).resolve()
                if f.parent != d or f.suffix != ".zip" or not f.is_file():
                    return self.send(404, b"not found", "text/plain")
                return self.send(200, f.read_bytes(), "application/zip", filename=f.name)
            if p.startswith("/docs/"):
                docs = (ROOT / "docs").resolve()
                f = (ROOT / p.lstrip("/")).resolve()
                if docs not in f.parents or not f.is_file():
                    return self.send(404, b"not found", "text/plain")
                return self.send(200, f.read_bytes(), {".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream"))
            name = "index.html" if p in ("/", "") else p.lstrip("/")
            f = (STATIC / name).resolve()
            if STATIC not in f.parents or not f.is_file():
                return self.send(404, b"not found", "text/plain")
            ctype = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                     ".svg": "image/svg+xml", ".png": "image/png"}.get(f.suffix, "application/octet-stream")
            return self.send(200, f.read_bytes(), ctype + "; charset=utf-8")
        except ValueError as e:
            return self.send(400, {"error": str(e)})

    def mcp(self, p="/mcp"):
        try:
            msg = self.body()
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        batch = isinstance(msg, list)
        # ⚠ Discovery (initialize, ping, tools/list, prompts/list, notifications) needs no token:
        # Magertron's probe and tool discovery call without one, and it only describes the tools.
        # Anything that DOES something (tools/call, prompts/get) runs as the caller, or not at all.
        methods = [m.get("method", "") for m in (msg if batch else [msg]) if isinstance(m, dict)]
        if not all(m in MCP_OPEN or m.startswith("notifications/") for m in methods):
            if not self.authorize(p):
                return
        else:
            CALLER.token = CALLER.user = None
        replies = [r for r in (mcp_handle(m) for m in (msg if batch else [msg]) if isinstance(m, dict)) if r]
        if not replies:
            self.send_response(202); self.send_header("Content-Length", "0"); self.end_headers()
            return
        data = json.dumps(replies if batch else replies[0]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if not batch and isinstance(msg, dict) and msg.get("method") == "initialize":
            self.send_header("Mcp-Session-Id", hashlib.sha256(os.urandom(16)).hexdigest()[:32])
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.rstrip("/") != "/mcp" and not self.authorize(p):
            return
        return self.send(200 if p.rstrip("/") == "/mcp" else 404, {})

    def do_POST(self):
        p = urlparse(self.path).path
        if p.rstrip("/") == "/mcp":
            return self.mcp(p)        # decides per method whether a token is needed
        if not self.authorize(p):
            return
        try:
            b = self.body()
            if p.startswith("/api/builds/") and p.endswith("/start"):
                try:
                    x = start_build(p.split("/")[3], getattr(CALLER, "user", "") or "")
                except ValueError as e:
                    return self.send(404, {"error": str(e)})
                r = run_action(x["czarter"], "handover-zip")
                return self.send(200, {"build": x, "download": r.get("download"), "output": r.get("output", "")})
            if p == "/api/builds/push":
                try:
                    return self.send(200, push_kit(b.get("name", ""), b.get("ide", "cursor")))
                except Exception as e:  # noqa: BLE001
                    return self.send(400, {"error": str(e)})
            if p == "/api/builds/assign":
                with _BUILDS_LOCK:
                    bl = _builds()
                    for y in bl:
                        if y["id"] == b.get("id"):
                            y["assignee"] = b.get("assignee", "")
                    _save_builds(bl)
                return self.send(200, {"ok": True})
            if p == "/api/setup":
                nm = b.get("name", "")
                if b.get("mode") == "run":
                    return self.send(200, setup_run(nm, wait=2))
                return self.send(200, setup_preview(nm))
            if p == "/api/check":
                return self.send(200, {"errors": errors_of(b.get("doc", {}))})
            if p == "/api/connect-ide":
                try:
                    return self.send(200, connect_ide(apply=bool(b.get("apply"))))
                except RuntimeError as e:
                    return self.send(400, {"error": str(e)})
            if p == "/api/describe":
                try:
                    return self.send(200, describe(b.get("text", "")))
                except (ValueError, RuntimeError) as e:
                    return self.send(400, {"error": str(e)})
            if p == "/api/save":
                doc = b.get("doc", {})
                name = (doc.get("metadata") or {}).get("name", "")
                f = path_for(name)
                f.write_text("# Czarter - written by Magertron Czartup(TM)\n"
                             + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
                return self.send(200, {"saved": str(f), "errors": errors_of(doc)})
            if p == "/api/delete":
                return self.send(200, delete_czarter(b.get("name", "")))
            if p == "/api/retire":
                out, ok = retire_app((b.get("name") or "").strip(), bool(b.get("confirm")), bool(b.get("keep_servers")),
                                     bool(b.get("remove_idp")))
                return self.send(200, {"ok": ok, "output": out})
            if p == "/api/clone":
                src, dst = path_for(b.get("from", "")), path_for(b.get("to", ""))
                if not src.exists():
                    return self.send(404, {"error": "no such Czarter"})
                if dst.exists():
                    return self.send(409, {"error": f"a Czarter named {b['to']} already exists"})
                doc = yaml.safe_load(src.read_text())
                old_name = (doc.get("metadata") or {}).get("name") or b["from"]
                doc.setdefault("metadata", {})["name"] = b["to"]
                retarget_clone(doc, old_name, b["to"])
                fresh_identity(doc, old_name, b["to"])
                # A clone is a new app: its own namespace, named after it (editable on Project).
                doc["metadata"]["namespace"] = b["to"]
                # The clone starts fresh: no run log, no runbook - nothing has been done for it yet.
                dst.write_text(f"# Czarter - cloned from {b['from']} by Magertron Czartup(TM)\n"
                               + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
                return self.send(200, {"doc": doc, "errors": errors_of(doc)})
            if p == "/api/icon":
                msg, ok = save_icon(b.get("name", ""), b.get("image_base64") or "")
                return self.send(200, {"ok": ok, "output": msg})
            if p == "/api/run":
                return self.send(200, run_action(b.get("name", ""), b.get("action", ""), bool(b.get("yes"))))
            return self.send(404, {"error": "not found"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except subprocess.TimeoutExpired:
            return self.send(200, {"ok": False, "output": "timed out"})


if __name__ == "__main__":
    print(f"Magertron Czartup on http://{BIND}:{PORT}  (store: {STORE}, API: {API}"
          + (", hosted: acts as the signed-in person" if HOSTED else "") + ")", flush=True)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
