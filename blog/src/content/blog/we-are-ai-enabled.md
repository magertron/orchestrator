---
title: 'We Are AI-Enabled'
description: 'Enterprises are buying Claude Desktop and calling it an AI strategy. Meanwhile, shadow MCP servers are multiplying across their networks with no inventory, no governance, and credentials to internal systems. Here is why network scanning will not find them — and what will.'
pubDate: 'May 15 2026'
heroImageUrl: '/blog-banner.svg'
---

*-We are AI-enabled!!*

I've heard some version of that sentence from more enterprise IT leadership in the past six months than I can count. Because what they actually mean is: *we paid for Claude Desktop licenses, and our employees installed them.* What they think it means is: *we have an AI strategy.*

At the heart of the problem is that some IT directors and AI platform leadership teams think their subscription to Claude Desktop in the enterprise is what completes their company's AI agenda. *"We have had Claude for months now, and we are fully AI governed,"* they will say. But the gap couldn't be wider. Enterprises with tens of thousands of developers, and multiple organizational units across multiple geographies, will undoubtedly build — or attempt to build — their own self-serving MCP servers. These un-governed shadow processes are already rampant on the network, with no clear way to inventory each one nor identify the agents they serve without intrusive network scanning.

The shadow MCP server is the most dominant deployment model in practice. Some developer wraps their team's internal REST API into an MCP server, gets it running in 30 minutes, commits the binary or the npm package to their team's repo, and tells four colleagues *"hey, point Claude Desktop at this."* That's it. No k8s. No Helm chart. No orchestrator registration. No security review. No inventory entry. Multiply that by 50 dev teams in a 5,000-person company, and you have hundreds of MCP servers running under desks and on dev VMs that nobody has a list of. Some of them with credentials to internal systems.

## An approach to mitigate risk

Discovery as "scan the network" is the wrong mental model for MCP specifically. The better approach is **multi-source inventory aggregation**, where the network is one source among several:

![MCP multi-source inventory aggregation: four discovery sources feeding into the Magertron control plane, producing a unified inventory](/mcp-federation-diagram.svg)

The other sources could be:

**1. MCP host config files.** Authoritative. Trivially parseable. Just JSON. Every IDE plugin, every desktop client, every CLI tool that hosts MCP has one. An endpoint agent — or even just a Cursor/VS Code extension or a corp-distributed script run on login — can enumerate these in milliseconds and post them upstream. This is the single highest-value discovery source for MCP servers.

**2. Process and package inspection.** A lightweight host agent that:

- Lists running processes whose argv contains `mcp` or matches known MCP server binaries
- Reads `~/.npm`, `~/.cache/uv`, pip metadata for installed `mcp-server-*` packages

**3. Egress / proxy logs.** If the org has an outbound web proxy or service mesh, look for the MCP host clients calling out. `User-Agent: Claude.ai/Desktop` and similar fingerprints tell you which user on which machine is using which MCP host — which in turn tells you to go look at their config files.

But you can't avoid it forever. Comprehensive MCP discovery in a real enterprise requires an endpoint presence. There's no source/config-only path to it, and there are plenty of endpoint and MDM vendors that are far more suited to the job than any new tool you're going to roll out.

## So how does an enterprise actually become MCP-aware?

Not with a scanner. Not with a quarterly audit. Not with a Confluence page asking developers to *"please register your MCP servers here"* — which, having lived in enterprises, I can promise you nobody will do.

You become MCP-aware by accepting that this is fundamentally a **federation problem**, not a discovery problem. The data you need is already in the enterprise — scattered across MDM systems, endpoint security tools, egress proxies, SSO logs, internal package registries, source repos, and config files on every developer laptop. The job of an MCP governance plane isn't to find that data fresh. It's to **pull it together, dedupe it, correlate it, and turn it into an inventory you can actually act on.**

Adopt servers into a managed catalog. Flag the orphans. Watch the egress patterns. Alert on the unmanaged. Govern the ones that matter; deprecate the ones that don't. Take what the defense gives you.

The enterprises that figure this out in the next twelve months will treat MCP the way they eventually learned to treat shadow IT, shadow SaaS, and shadow data — as an ongoing inventory discipline, not a one-time scan. The ones that don't will spend the following twelve months explaining to their CISO how an AI agent exfiltrated half the customer database through an MCP server that nobody knew was running.

*"We have Claude Desktop"* is not an AI strategy.

*"We know every MCP server in our environment"* is the beginning of one.
