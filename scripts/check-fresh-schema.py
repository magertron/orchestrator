#!/usr/bin/env python3
"""Release pre-check: does the chart's schema build a NEW database? (2026-11-27)

Regression runs against an existing database, and Postgres runs the init scripts only
on an empty one - so a broken schema script ships green (4.2.20 to 4.3.61: every fresh
install had no audit_events). This renders the chart's postgres-init configmap, starts a
throwaway Postgres in Docker with the same image the chart uses, lets it run the three
scripts exactly as a new install does, and fails if any statement errors or the key
tables are missing. About 30 seconds. Nothing outside the container is touched.

Run on czar (before release.sh, or from its pre-checks):
  python3 ~/Downloads/check-fresh-schema.py
  python3 ~/Downloads/check-fresh-schema.py --chart ~/GetHub/orchestrator/helm/orchestrator --image postgres:16
Exit 0 = good, 1 = the schema is broken (the errors are printed), 2 = could not run.
"""
import argparse, pathlib, re, subprocess, sys, tempfile, time

# Tables the INIT SCRIPTS create. (merchant_keys, platform_config and a few more are added by
# the orchestrator when it starts, so a bare database without it does not have them.)
MUST_EXIST = ["audit_events", "users", "roles", "deployed_servers", "cost_contracts",
              "meter_events"]

ap = argparse.ArgumentParser()
ap.add_argument("--chart", default=str(pathlib.Path("~/GetHub/orchestrator/helm/orchestrator").expanduser()))
ap.add_argument("--image", default="", help="Postgres image; default: the one the chart renders")
ap.add_argument("--timeout", type=int, default=180)
a = ap.parse_args()


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def die(msg, code=2):
    print(msg); sys.exit(code)


sets = ["--set", "postgresql.enabled=true", "--set", "seed.adminPasswordHash=$2b$12$freshschemacheckxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"]
r = sh(["helm", "template", "fresh-check", a.chart, *sets])
if r.returncode != 0:
    die(f"helm template failed:\n{r.stderr[-800:]}")
rendered = r.stdout

# The init configmap's three files: literal blocks under data:, indented 4.
docs = rendered.split("\n---")
cm = next((d for d in docs if "kind: ConfigMap" in d and "-postgres-init" in d and "inventory" not in d.split("name:", 1)[1].split("\n", 1)[0]), None)
if not cm:
    die("no postgres-init ConfigMap in the rendered chart")
files, cur = {}, None
for line in cm.split("\n"):
    m = re.match(r"^  ([0-9][0-9]-[A-Za-z0-9_.-]+\.sql): \|", line)
    if m:
        cur = m.group(1); files[cur] = []; continue
    if cur is not None:
        if line.startswith("    ") or line == "":
            files[cur].append(line[4:] if line.startswith("    ") else "")
        else:
            cur = None
if len(files) < 3:
    die(f"expected 3 init files, found: {sorted(files)}")

image = a.image
if not image:
    imgs = re.findall(r"image:\s*\"?([^\s\"]*postgres[^\s\"]*)\"?", rendered)
    imgs = [i for i in imgs if "inventory" not in i]
    image = imgs[0] if imgs else "postgres:16"
print(f"image    {image}")
print(f"scripts  {', '.join(sorted(files))}")

with tempfile.TemporaryDirectory() as d:
    for name, lines in files.items():
        p = pathlib.Path(d) / name
        p.write_text("\n".join(lines).rstrip("\n") + "\n")
        p.chmod(0o644)
    pathlib.Path(d).chmod(0o755)
    name = f"fresh-schema-check-{int(time.time())}"
    r = sh(["docker", "run", "-d", "--name", name,
            "-e", "POSTGRES_USER=mcp", "-e", "POSTGRES_DB=mcp_platform", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
            "-v", f"{d}:/docker-entrypoint-initdb.d:ro", image])
    if r.returncode != 0:
        die(f"docker run failed:\n{r.stderr[-800:]}")
    try:
        logs, ready, t0 = "", False, time.time()
        while time.time() - t0 < a.timeout:
            logs = sh(["docker", "logs", name]).stdout + sh(["docker", "logs", name]).stderr
            state = sh(["docker", "inspect", "-f", "{{.State.Running}}", name]).stdout.strip()
            # Ready twice = the init server finished the scripts and the real server is up.
            if logs.count("database system is ready to accept connections") >= 2 or state != "true":
                ready = state == "true"; break
            time.sleep(2)
        errors = [l for l in logs.splitlines() if re.search(r"\bERROR\b|FATAL", l)]
        if errors or not ready:
            print("FAIL     the schema did not build a new database:")
            for l in (errors or logs.splitlines()[-15:])[:15]:
                print("   ", l)
            sys.exit(1)
        q = ("select string_agg(t, ',') from unnest(array[" + ",".join(f"'{t}'" for t in MUST_EXIST) +
             "]) t where to_regclass('public.' || t) is null")
        r = sh(["docker", "exec", name, "psql", "-U", "mcp", "-d", "mcp_platform", "-tAc", q])
        missing = r.stdout.strip()
        n = sh(["docker", "exec", name, "psql", "-U", "mcp", "-d", "mcp_platform", "-tAc",
                "select count(*) from pg_tables where schemaname='public'"]).stdout.strip()
        if r.returncode != 0 or missing:
            print(f"FAIL     missing tables: {missing or r.stderr.strip()}")
            sys.exit(1)
        print(f"OK       a new database builds clean: {n} tables, audit_events included")
    finally:
        sh(["docker", "rm", "-f", name])
