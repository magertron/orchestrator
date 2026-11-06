#!/usr/bin/env python3
"""Add docs/llms.txt - a plain-text map of magertron.com for AI assistants and crawlers
(the llmstxt.org format: title, one-line summary, short notes, then links by section).

Only pages that exist in the docs folder are listed; missing ones are reported and left out.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)

Dry run by default (prints the file); --apply writes docs/llms.txt. Refuses to overwrite
an existing llms.txt unless you add --force.
"""
import pathlib, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
BASE = "https://magertron.com/"
out = SITE / "llms.txt"
if out.exists() and "--force" not in sys.argv:
    sys.exit("docs/llms.txt already exists - add --force to replace it")

HEAD = """# Magertron™ AI Orchestrator

> Magertron is a self-hosted, Kubernetes-native control plane for enterprise AI: the governance and spend layer between AI agents and the MCP tools, LLMs, APIs and event streams they call. Every call carries the agent and the person it acts for, is checked against policy inline, metered against real vendor contracts, and written to an append-only audit log.

Key facts:
- Governs four kinds of traffic in one policy plane: MCP tool calls, LLM inference, REST APIs, and event streams (Kafka, ZeroMQ).
- Identity: per-call agent + user identity, SSO, SCIM, revocable service accounts, RFC 8693 token exchange.
- Policy: per-tool grants by role and namespace, human approval holds with no self-approval, tool drift detection, optional external policy decision point (OPA).
- Cost: per-call, per-token and per-value rating against vendor contracts, budgets, and chargeback by department, user or agent.
- Audit: append-only record of every decision, refusals included; OpenTelemetry traces.
- Runs in the customer's own cluster, including air-gapped. Installed with a Helm chart; CLI and Terraform provider available.
- Pricing is a flat platform fee, not a tax on usage: Free ($0, up to 20 MCP servers), Pro ($499/month billed annually), Business ($30,000/year), Enterprise (contact us).
- Commercial software, not open source; the installer, chart, CLI and client libraries are Apache-2.0.
- Made by Magertron, LLC, South Florida. Contact: curtis@magertron.com
"""

SECTIONS = [
    ("Platform", [
        ("index.html", "AI Orchestrator", "Overview: governed surfaces, how it works, install"),
        ("data-reach.html", "AI Data Reach", "Which agents can reach which data, Unity Catalog-compatible"),
        ("agentczar.html", "AgentCzar", "Policy pack protecting bank customers' accounts from agent-driven fraud"),
        ("pricing.html", "Pricing", "Free, Pro, Business and Enterprise tiers"),
        ("cost.html", "Cost & Reporting", "Spend dashboards, contracts, budgets and chargeback"),
        ("security.html", "Security", "Securing API, streaming, MCP and LLM traffic inside the enterprise"),
        ("compliance.html", "Compliance", "Audit evidence and compliance posture"),
    ]),
    ("Solutions", [
        ("solutions-banking.html", "Banking", "Agents acting on bank accounts, with approvals and limits"),
        ("solutions-accounts-payable.html", "Accounts payable", "Maker-checker when the maker is an AI agent"),
        ("solutions-crm.html", "Sales & CRM", "Agents that abide by the deal desk"),
        ("solutions-network-operations.html", "Network operations", "Network agents with an operator on every change"),
    ]),
    ("Developers", [
        ("developers.html", "Developers", "Overview for agent and MCP server developers"),
        ("developer-portal.html", "Developer Portal", "Discover approved tools, try them, request a token"),
        ("api/v1/", "API documentation", "REST API reference"),
        ("vscode/", "VS Code", "Using Magertron from VS Code"),
        ("cursor/", "Cursor", "Using Magertron from Cursor"),
        ("claude-code/", "Claude Code", "Using Magertron from Claude Code"),
        ("devin/", "Devin", "Using Magertron from Devin"),
    ]),
]
EXTERNAL = [
    ("Optional", [
        ("https://github.com/magertron/orchestrator", "GitHub: orchestrator", "Helm chart, installer and docs"),
        ("https://github.com/magertron/agent-examples", "GitHub: agent examples", "Example agents calling through Magertron"),
    ]),
]

lines, missing = [HEAD], []
for title, pages in SECTIONS:
    rows = []
    for path, name, desc in pages:
        p = SITE / path
        if (p / "index.html").exists() if path.endswith("/") else p.exists():
            rows.append(f"- [{name}]({BASE}{'' if path == 'index.html' else path}): {desc}")
        else:
            missing.append(path)
    if rows:
        lines.append(f"## {title}\n\n" + "\n".join(rows) + "\n")
for title, links in EXTERNAL:
    lines.append(f"## {title}\n\n" + "\n".join(f"- [{n}]({u}): {d}" for u, n, d in links) + "\n")
text = "\n".join(lines)

print(text)
for m in missing:
    print(f"(left out - not in the docs folder: {m})")
if APPLY:
    out.write_text(text, encoding="utf-8")
    print(f"\nwrote {out}  -> will be live at {BASE}llms.txt after you push")
else:
    print("\ndry run - re-run with --apply to write")
