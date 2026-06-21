---
title: 'API Gateway or MCP Orchestrator? One Box or Two?'
description: 'Both ride HTTP, so teams assume one box should handle both. Here is the honest case for and against combining an API gateway with an MCP orchestrator — and the one question that actually decides it: what are you really trying to manage?'
pubDate: 'Jun 21 2026'
heroImageUrl: '/blog-banner.svg'
---

The question is showing up in real architecture reviews now. A team stands up its first MCP traffic — an internal agent calling tools, a feature built on the Model Context Protocol — and someone asks the reasonable thing: do we just put this behind the API gateway we already run, or is it separate?

Most of the bad answers come from stopping one step too early. The decision feels confusing because of a single fact: **both ride HTTP.** Your REST APIs go over HTTP; MCP, in its Streamable HTTP and SSE transports, goes over HTTP too. So the instinct is one front door, one box, done. That instinct is where the trouble starts.

We build one of these, so we'll make the case *against* our own product at least as hard as the case for it — this is a decision the architecture should make, not the vendor. By the end you won't have a verdict from me. You'll have a diagnostic you can run yourself, and it comes down to one question that isn't "which gateway." It's *what are you actually trying to manage?*

## Same transport, different protocol

HTTP is the transport — the envelope. What's *inside* the envelope is different. A REST API speaks REST over HTTP; MCP speaks JSON-RPC over HTTP. The shared transport is exactly why people conflate the two, and exactly why they shouldn't. What matters isn't the transport they have in common — it's the governable surface they don't.

An API gateway sees a method, a path, headers, and a body. It can authenticate, terminate TLS, rate-limit, route, reject an oversized payload — and yes, with the right plugins it can reach into the body too: extract a field, match a pattern, validate against a schema you hand it, transform the payload on the way through. What it *can't* do is reason about that body in domain terms it doesn't model. It can pull a string out of JSON, but it has no native concept of *a tool*, of *this principal's grant to that specific tool*, of *what this call costs*. It manipulates bytes; it doesn't understand them as MCP. The gateway governs the traffic, and can even rewrite the contents — but the meaning of that context lives in a domain it was never built to maintain.

An MCP orchestrator sees something different: a `tools/call` — a *named tool*, *typed arguments*, a *principal*, and the *schema* that tool was registered with. Because it sees those, it can do what the API gateway structurally cannot: authorize this principal to call this specific tool, attribute the call's *cost* to the tool and team that made it, validate arguments against the registered schema, and flag a tool whose description has been poisoned with instructions aimed at the calling model. None of that is bolted onto HTTP. It exists *only because* the protocol exposes the tool, the principal, and the schema.

> An API gateway governs **transport**. An MCP/AI orchestrator governs **semantics** — and one little surprise later in this blog. But overall, it's the same packets on the wire with different things to control.

Yes, both terminate HTTP. That's true and it's not the point. The overlap is the envelope, not the letter.

## A second difference: the orchestrator runs the logic

There's a second distinction, and it's one the REST world doesn't have to think about because it was settled twenty years ago.

An API gateway is pass-through by design. It sits in front of upstreams it doesn't own, applies policy, and forwards. It originates nothing. It holds no business logic of its own, and — this is the part that matters — it holds none of the *credentials* that logic needs to reach the systems behind it. That's deliberate. A gateway with a small blast radius is a gateway that doesn't run your code.

An MCP server is something that can be managed internally. It's a different kind of thing — not an upstream the plane forwards to, but effectively an *application*. It most often carries business logic. It holds the credentials to reach an internal database, an HR system, an ERP, a data warehouse. It does work, and to do that work it has to be trusted with access to the systems of record.

If that sounds familiar, it should. It's the same reason you ran applications in Tomcat or WebLogic instead of just exposing them through a web server. The app server existed because applications hold logic and credentials, and something has to host them, hold those secrets safely, manage their lifecycle, and broker their access to the databases and enterprise systems behind the firewall. A reverse proxy in front couldn't do that job, because that was never a proxy's job.

The MCP orchestrator is the application server re-imagined for the agentic AI architecture. Internal MCP servers execute inside the orchestrator as not only a protocol proxy, but also as hosted workloads bound for either internal and/or external destinations. The orchestrator deploys them, holds their credentials as referenced secrets, and governs their lifecycle. And those internal servers reach *outward* to external MCP servers and other agents to enrich what they do: an internal server running your business logic — which may itself be LLM-backed, making its own model-driven decisions — calls a vendor's hosted MCP server to pull in something it doesn't have locally. That single outbound call is where the whole model converges — it's where the vendor credential gets injected (the logic never holds the raw key), where the call gets metered (you're accounting for the enrichment), where the external tool's description gets scanned, and where per-tool authorization decides what your internal logic is even allowed to reach. Hosting, governance, and egress control meet on one request.

So the MCP orchestrator blends two roles the REST world keeps separate: it's a gateway *and* can dual as an application server. It governs traffic like the former and hosts logic and credentials like the latter, because the protocol's atomic unit — the MCP server — is both a thing you govern and a thing that has intelligence to route and/or choose to execute code. An API gateway can't fill that role, and not because it's missing a feature. It's the wrong shape. A proxy was never meant to be the place your applications live.

That blend is not free, and it's worth saying so. A pure pass-through gateway has a simpler failure model precisely *because* it doesn't run your logic — nothing it hosts can take it down, because it hosts nothing. The moment the orchestrator runs compute, its fate is coupled to the workloads it carries. That's a real cost. The honest claim isn't that blending is strictly better; it's that MCP makes the blend natural, the way deploying a servlet into an app server was natural — and that you should walk in knowing you've taken on an application server's responsibilities, not just a gateway's. But also know that you could use an MCP orchestrator purely as a proxy — and that is perfectly acceptable too.

## The honest case for one box

There are real reasons to put both behind a single piece of infrastructure, and you need them at full strength to make the right call.

**One operational surface.** Two gateways means two things to deploy, patch, monitor, and page on. For a small team that's a meaningful share of the budget, and "fewer moving parts" is a real virtue.

**One policy and identity domain.** When auth, limits, and audit live in one place, you reason about security once. Split them and you get two policy languages to keep coherent, two audit streams to correlate, two places a misconfiguration can hide.

**Shared edge concerns genuinely belong together.** TLS, DDoS protection, IP allowlisting, payload-size limits are protocol-agnostic. Duplicating them across two boxes is just two places to get it wrong.

And the strongest version, plainly: **for a greenfield team, at small scale, with incidental MCP traffic and no existing gateway, one box is the right call** — not a compromise. If that's you, the rest of this is interesting but not urgent.

## The honest case for two

The other side — where we land, so weigh it accordingly.

**The semantics degrade to the lowest common denominator.** Force MCP through a box built for opaque bodies and you don't get MCP governance with a REST accent — you lose the tool-level surface entirely, because the box has nowhere to put it. You'd be carrying MCP traffic you can't govern *as MCP*.

**Release cadence and blast radius.** MCP is young and moving fast — the spec churns, the threat model (tool poisoning, rug pulls, injection) is new and evolving. Couple that to your stable, load-bearing REST gateway and one of two bad things happens: MCP's churn destabilizes the gateway your whole API surface depends on, or the gateway's need for stability throttles MCP's iteration. Decoupled planes move at their own speeds and fail independently.

**The economics live at a different altitude.** Transport rate limiting — requests per second — is a different control from governing *spend*: dollars, credits, per-department budgets. A request can pass every transport check — well-formed, authenticated, under the limit — and still be one you must refuse, because the department is over budget or the tool has been flagged. The API gateway can't make that call. Not because it's poorly built, but because it doesn't know what a contract is, or a credit, or a tool.

## What are you actually trying to manage?

This is the only thing in the post I'd ask you to remember. Stop choosing a product. Name your problem.

> **Managing transport** — request rates, connections, payload sizes, TLS, who can *reach* the endpoint — that's an API gateway's job.
>
> **Managing logic, semantics and economics** — which principal may invoke which *tool*, what it *costs*, whether it's been *poisoned*, which budget it draws down — that's an MCP governance plane's job.

And the punchline isn't the self-serving one: **most teams are managing both.** That's not a reason to mash them into one box — it's why two composed planes is usually the answer. Two genuinely different problems are rarely best solved by one tool pretending they're the same.

**The precedent is consistent — and it has a reason.** gRPC didn't fold into REST gateways. Service meshes didn't collapse into API gateways. Streaming and event gateways became their own tier. None of that was an accident, and it wasn't fashion. Each time, the same force was at work: a protocol arrived carrying semantics the general-purpose gateway couldn't see, or behavior it wasn't built to run, and pushing it through the gateway anyway meant flattening it to the lowest common denominator the gateway understood. gRPC's streaming and method semantics didn't survive being treated as opaque HTTP. East-west service-to-service traffic, with its retries, mTLS, and load-aware routing, wanted a mesh, not a north-south edge proxy. Every time the cost of forcing the new protocol into the old box exceeded the cost of running a second plane, the industry ran the second plane. MCP is the agent tier's instance of exactly that pattern — distinct semantics, plus hosted logic the gateway was never meant to hold — and it resolves the same way, for the same reason.

**Lean toward one box** when you're greenfield, small, MCP is incidental, and simplicity matters more than governance depth right now. Frequently correct, no shame in it.

**Lean toward two planes** when you already run an API gateway, your MCP traffic carries real money or risk, you're at a scale where one component's failure shouldn't take the other down, or the two need to evolve at different speeds.

## How they compose

"Two planes" doesn't mean rip-and-replace. The common enterprise pattern puts the **MCP plane behind the API gateway**: your edge gateway keeps doing transport hygiene — TLS, coarse limits, DDoS — then forwards to the MCP plane, which applies tool-level governance and routes to the MCP server. You don't replace your gateway; you add the plane it was never built to hold. (Greenfield, with no REST surface to govern, the MCP plane is simply the front door itself.)

And the detail that defuses the turf war: the protocol routes the packet. REST goes to the REST gateway, MCP/JSON-RPC goes to the MCP plane — no ambiguous request both want to own. Composition, not competition.

One honest exception: the team with *neither* gateway that wants *one thing*. There's a real argument for a single box doing thin transport limits as a *convenience* — until that traffic carries real money or risk, at which point you've outgrown the one-box answer. That's growth, not a mistake. If you read this and concluded "not yet," that may be the most useful thing you take from it.

## The takeaway — what are you looking to manage?

The mistake isn't picking the wrong gateway or orchestrator. It's answering something other than "what am I trying to manage?" Transport, semantics, or — for most of you — both. And two clean planes are easier to run than one blurred one.

This is the thinking behind what we built: an MCP orchestrator, not another gateway — a plane that governs the traffic *and* houses the logic. If the diagnostic pointed you toward managing semantics and spend, that's the plane we live in. If it pointed you elsewhere, it did its job — which mattered more than the pitch.
