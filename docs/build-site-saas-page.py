#!/usr/bin/env python3
"""Build docs/solutions-saas.html - "Across SaaS platforms": Magertron(TM) as the neutral
control plane in front of ServiceNow, Salesforce, Workday and internal systems.

The page is solutions-crm.html's shell (head, top bar, footer, Try it now form,
scripts) with new content: a hero, the architecture drawing, the four kinds of
call, who does what, example policy packs, why neutral, and a call to action.
Its own styles are scoped under .xs, so nothing else on the site changes.

  --site PATH   the site folder (default ~/GetHub/orchestrator/docs)
  --force       rebuild an existing solutions-saas.html

Dry run by default (prints what it takes and changes); --apply writes the page.
Afterwards: patch-site-nav-final.py --apply puts it in the Solutions menu.
"""
import pathlib, re, sys

APPLY = "--apply" in sys.argv
SITE = pathlib.Path(sys.argv[sys.argv.index("--site") + 1] if "--site" in sys.argv
                    else "~/GetHub/orchestrator/docs").expanduser()
SRC = SITE / "solutions-crm.html"
OUT = SITE / "solutions-saas.html"
if OUT.exists() and "--force" not in sys.argv:
    sys.exit("solutions-saas.html already exists - add --force to rebuild it")
s = SRC.read_text(encoding="utf-8")

TITLE = "Across SaaS Platforms - Magertron™ AI Orchestrator"
DESC = ("For enterprises that need to govern AI calls across their SaaS estate and won't let one "
        "vendor's control plane police the others: one neutral control plane, in your cluster.")

HERO = """<section class="hero">
  <div class="container">
    <h1 style="font-size: clamp(34px, 5vw, 56px);">One control plane over every SaaS agent<span class="sol-sub" style="display: block; font-size: 0.42em; margin-top: 14px;"><em>For enterprises that won't let one vendor police the others</em></span></h1>
    <p class="lede" style="max-width: 760px; margin: 18px auto 0;">Magertron™ AI Orchestrator is the neutral layer between your agents and every platform they use. It runs in your own cluster and sits in the path of every agent call into ServiceNow, Salesforce, Workday and your internal systems: one set of your rules, one identity for the agent and the person it acts for, one audit trail, and every vendor's usage metered against its contract.</p>
  </div>
</section>
"""

CSS = r""".xs{--bg:#0e1422;--paper:#121a2c;--ink:#e7ebf3;--muted:#9aa4b8;--line:#26314b;
  --navy:#1a2440;--navy-2:#24314f;--on-navy:#eef1f6;--on-navy-muted:#a9b3c6;
  --accent:#f29a4a;--accent-soft:#3a2617;--ok:#4cc98a;--hold:#f2b84b;--stop:#ff7a7a;
  --vendor:#182136;--vendor-line:#3a4664;
  --display:"IBM Plex Sans Condensed","Arial Narrow",Arial,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  color:var(--ink);font:15.5px/1.6 var(--body)}
.xs .xs-wrap{max-width:1180px;margin:0 auto;padding:8px 24px 64px;display:flex;flex-direction:column;gap:44px}
.xs *{box-sizing:border-box}
@media (max-width:640px){.xs .xs-wrap{padding:8px 16px 48px}}
.xs h2{font:700 26px/1.15 var(--display);margin:0 0 6px;text-wrap:balance}
.xs h3{font:600 17px/1.3 var(--body);margin:0 0 4px}
.xs p{margin:0}
.xs .sub{color:var(--muted);max-width:68ch}
.xs .figure{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px 18px 10px}
.xs .scroll{overflow-x:auto}
.xs svg.arch{display:block;min-width:980px;width:100%;height:auto}
.xs .figure figcaption{font-size:13px;color:var(--muted);padding:8px 4px 4px;display:flex;flex-wrap:wrap;gap:6px 18px}
.xs .key{display:inline-flex;align-items:center;gap:6px}
.xs .key i{display:inline-block;width:22px;height:0;border-top:2px solid var(--accent)}
.xs .key i.dash{border-top:2px dashed var(--vendor-line)}
.xs .t-col{font:600 11px var(--body);letter-spacing:.12em;text-transform:uppercase;fill:var(--muted)}
.xs .box{fill:var(--paper);stroke:var(--line);stroke-width:1.2}
.xs .box-v{fill:var(--vendor);stroke:var(--vendor-line);stroke-width:1.2}
.xs .mag{fill:var(--navy)}
.xs .tile{fill:var(--navy-2)}
.xs .gw{fill:var(--accent)}
.xs .t-h{font:600 14px var(--body);fill:var(--ink)}
.xs .t-s{font:400 12px var(--body);fill:var(--muted)}
.xs .t-mh{font:700 17px var(--display);fill:var(--on-navy)}
.xs .t-mt{font:600 13px var(--body);fill:var(--on-navy)}
.xs .t-ms{font:400 11.5px var(--body);fill:var(--on-navy-muted)}
.xs .t-gw{font:600 12px var(--mono);fill:#1a0f06}
.xs .flow{stroke:var(--accent);stroke-width:2;fill:none}
.xs .flow-in{stroke:var(--accent);stroke-width:2;fill:none;stroke-dasharray:7 5}
.xs .vend{stroke:var(--vendor-line);stroke-width:1.6;fill:none;stroke-dasharray:4 4}
.xs .arrow{fill:var(--accent)}
.xs .arrow-v{fill:var(--vendor-line)}
.xs .paths{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
.xs .path{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:16px 18px;display:flex;flex-direction:column;gap:8px;min-width:0}
.xs .path .tag{font:500 12px var(--mono);color:var(--muted)}
.xs .path ul{margin:0;padding-left:18px;color:var(--muted);font-size:14.5px}
.xs .path li{margin:2px 0}
.xs .path li b{color:var(--ink);font-weight:600}
.xs .out{display:inline-block;font:600 11px var(--mono);padding:2px 8px;border-radius:999px;margin-right:4px;border:1px solid}
.xs .o-ok{color:var(--ok);border-color:currentColor}
.xs .o-hold{color:var(--hold);border-color:currentColor}
.xs .o-stop{color:var(--stop);border-color:currentColor}
.xs .o-none{color:var(--muted);border-color:currentColor}
.xs .tbl{overflow-x:auto;background:var(--paper);border:1px solid var(--line);border-radius:12px}
.xs table{border-collapse:collapse;width:100%;min-width:640px;font-size:14.5px}
.xs th, .xs td{text-align:left;padding:11px 16px;border-bottom:1px solid var(--line);vertical-align:top}
.xs th{font:600 12px var(--body);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);background:var(--bg)}
.xs tr:last-child td{border-bottom:0}
.xs td:first-child{font-weight:600;white-space:nowrap}
.xs .packs{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
.xs .pack{border-top:3px solid var(--accent);background:var(--paper);border-radius:0 0 12px 12px;padding:14px 18px;min-width:0;
  border-left:1px solid var(--line);border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.xs .pack ul{margin:6px 0 0;padding-left:0;list-style:none;display:flex;flex-direction:column;gap:7px;font-size:14.5px}
.xs .pack li{display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:start}
.xs .why{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px}
.xs .why div{border-left:2px solid var(--accent);padding-left:14px}
.xs .why p{color:var(--muted);font-size:14.5px}
.xs .note{font-size:13.5px;color:var(--muted);border-top:1px solid var(--line);padding-top:16px}
.xs .cta-a{display:inline-block;padding:11px 20px;border-radius:10px;font:600 15px var(--body);text-decoration:none;border:1px solid var(--accent)}
.xs .cta-a.primary{background:var(--accent);color:#1a0f06}
.xs .cta-a.ghost{color:var(--accent);background:transparent}
.xs .cta-a:focus-visible{outline:2px solid var(--on-navy);outline-offset:2px}"""
BODY = r"""<div class="xs"><div class="xs-wrap">
<figure class="figure" style="margin:0">
<div class="scroll">
<svg class="arch" viewBox="0 0 1120 640" role="img" aria-labelledby="archTitle archDesc">
  <title id="archTitle">Magertron in front of ServiceNow, Salesforce and other systems</title>
  <desc id="archDesc">People and agents on the left call through Magertron in the middle, which checks identity, policy, holds and cost, then reaches ServiceNow's MCP server, Salesforce, other SaaS, internal systems, LLMs and event streams on the right. Each vendor's own governance tool governs inside its platform and receives Magertron's traces.</desc>
  <defs>
    <marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="arrow"/></marker>
    <marker id="ahv" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="arrow-v"/></marker>
  </defs>

  <!-- column labels -->
  <text x="20" y="24" class="t-col">Who acts</text>
  <text x="330" y="24" class="t-col">In your cluster</text>
  <text x="830" y="24" class="t-col">What they reach</text>

  <!-- left: who acts -->
  <rect x="20" y="40" width="240" height="66" rx="10" class="box"/>
  <text x="36" y="66" class="t-h">People</text>
  <text x="36" y="86" class="t-s">sign in with your IdP (Okta, Entra)</text>

  <rect x="20" y="122" width="240" height="84" rx="10" class="box"/>
  <text x="36" y="148" class="t-h">Your own agents</text>
  <text x="36" y="168" class="t-s">Agent SDK, LangGraph, in-house</text>
  <text x="36" y="186" class="t-s">copilots: Claude, Cursor, Codex</text>

  <rect x="20" y="222" width="240" height="66" rx="10" class="box"/>
  <text x="36" y="248" class="t-h">Partner-built agents</text>
  <text x="36" y="268" class="t-s">from your SIs and software vendors</text>

  <rect x="20" y="304" width="240" height="84" rx="10" class="box-v"/>
  <text x="36" y="330" class="t-h">SaaS agents calling out</text>
  <text x="36" y="350" class="t-s">Agentforce, Now Assist reaching</text>
  <text x="36" y="368" class="t-s">your internal systems</text>

  <!-- middle: Magertron -->
  <rect x="330" y="40" width="430" height="420" rx="14" class="mag"/>
  <text x="352" y="72" class="t-mh">Magertron™ AI Orchestrator</text>
  <text x="352" y="92" class="t-ms">self-hosted · one install per enterprise · namespaces per team</text>

  <rect x="352" y="108" width="386" height="40" rx="8" class="gw"/>
  <text x="545" y="133" text-anchor="middle" class="t-gw">gateway: every call checked inline</text>

  <rect x="352" y="160" width="188" height="86" rx="8" class="tile"/>
  <text x="366" y="184" class="t-mt">Identity</text>
  <text x="366" y="204" class="t-ms">the agent and the person</text>
  <text x="366" y="220" class="t-ms">it acts for, on every call</text>
  <text x="366" y="236" class="t-ms">(token exchange, SCIM)</text>

  <rect x="550" y="160" width="188" height="86" rx="8" class="tile"/>
  <text x="564" y="184" class="t-mt">Policy</text>
  <text x="564" y="204" class="t-ms">grants per tool and role,</text>
  <text x="564" y="220" class="t-ms">plus policy packs per system</text>
  <text x="564" y="236" class="t-ms">(OPA, yours or bundled)</text>

  <rect x="352" y="256" width="188" height="86" rx="8" class="tile"/>
  <text x="366" y="280" class="t-mt">Holds</text>
  <text x="366" y="300" class="t-ms">risky calls wait for a</text>
  <text x="366" y="316" class="t-ms">named approver; no</text>
  <text x="366" y="332" class="t-ms">self-approval</text>

  <rect x="550" y="256" width="188" height="86" rx="8" class="tile"/>
  <text x="564" y="280" class="t-mt">Cost</text>
  <text x="564" y="300" class="t-ms">every call rated against</text>
  <text x="564" y="316" class="t-ms">the vendor's contract;</text>
  <text x="564" y="332" class="t-ms">caps by team and agent</text>

  <rect x="352" y="352" width="386" height="86" rx="8" class="tile"/>
  <text x="366" y="376" class="t-mt">Audit and traces</text>
  <text x="366" y="396" class="t-ms">every decision, refusals included, with who acted for whom.</text>
  <text x="366" y="412" class="t-ms">One trace per agent session (OpenTelemetry), exported to your</text>
  <text x="366" y="428" class="t-ms">SIEM, observability stack and the vendors' own consoles.</text>

  <!-- right: what they reach -->
  <rect x="830" y="40" width="270" height="70" rx="10" class="box"/>
  <text x="846" y="66" class="t-h">ServiceNow MCP server</text>
  <text x="846" y="86" class="t-s">Action Fabric: incidents, changes,</text>
  <text x="846" y="102" class="t-s">approvals, catalog requests</text>

  <rect x="830" y="122" width="270" height="70" rx="10" class="box"/>
  <text x="846" y="148" class="t-h">Salesforce MCP and APIs</text>
  <text x="846" y="168" class="t-s">accounts, opportunities, quotes;</text>
  <text x="846" y="184" class="t-s">Agentforce actions billed per use</text>

  <rect x="830" y="204" width="270" height="56" rx="10" class="box"/>
  <text x="846" y="228" class="t-h">Workday · SAP · M365 · Slack</text>
  <text x="846" y="246" class="t-s">and any other SaaS with MCP or REST</text>

  <rect x="830" y="272" width="270" height="56" rx="10" class="box"/>
  <text x="846" y="296" class="t-h">Your internal APIs and MCP servers</text>
  <text x="846" y="314" class="t-s">ERP, billing, data warehouse</text>

  <rect x="830" y="340" width="270" height="56" rx="10" class="box"/>
  <text x="846" y="364" class="t-h">LLM providers</text>
  <text x="846" y="382" class="t-s">Anthropic, OpenAI, your own models</text>

  <rect x="830" y="408" width="270" height="52" rx="10" class="box"/>
  <text x="846" y="431" class="t-h">Event streams</text>
  <text x="846" y="448" class="t-s">Kafka, ZeroMQ topics</text>

  <!-- flows in -->
  <path d="M260 73 C 295 73, 300 128, 350 128" class="flow" marker-end="url(#ah)"/>
  <path d="M260 164 C 300 164, 305 128, 350 128" class="flow" marker-end="url(#ah)"/>
  <path d="M260 255 C 305 255, 305 128, 350 128" class="flow" marker-end="url(#ah)"/>
  <path d="M260 346 C 310 346, 305 128, 350 128" class="flow-in" marker-end="url(#ah)"/>

  <!-- flows out -->
  <path d="M740 128 C 790 128, 790 75, 828 75" class="flow" marker-end="url(#ah)"/>
  <path d="M740 128 C 790 128, 790 157, 828 157" class="flow" marker-end="url(#ah)"/>
  <path d="M740 128 C 795 128, 790 232, 828 232" class="flow" marker-end="url(#ah)"/>
  <path d="M740 128 C 800 128, 790 300, 828 300" class="flow" marker-end="url(#ah)"/>
  <path d="M740 128 C 805 128, 790 368, 828 368" class="flow" marker-end="url(#ah)"/>
  <path d="M740 128 C 810 128, 790 434, 828 434" class="flow" marker-end="url(#ah)"/>

  <!-- vendor governance band -->
  <rect x="330" y="512" width="770" height="104" rx="12" class="box-v"/>
  <text x="352" y="540" class="t-h">Inside each platform: the vendor's own governance</text>
  <text x="352" y="562" class="t-s">ServiceNow AI Control Tower governs what happens inside ServiceNow; Salesforce's trust and Agentforce</text>
  <text x="352" y="580" class="t-s">controls govern inside Salesforce. Agents built and run entirely inside a vendor never cross Magertron.</text>
  <text x="352" y="598" class="t-s">Magertron governs the calls between platforms, and sends its traces and audit to those consoles.</text>

  <path d="M1105 75 C 1112 75, 1112 300, 1112 505" class="vend" marker-end="url(#ahv)"/>
  <path d="M545 440 L545 505" class="vend" marker-end="url(#ahv)"/>
  <text x="556" y="482" class="t-s">traces and audit out</text>
</svg>
</div>
<figcaption>
  <span class="key"><i></i> governed call through Magertron</span>
  <span class="key"><i style="border-top-style:dashed"></i> a SaaS agent calling your systems, governed on the way in</span>
  <span class="key"><i class="dash"></i> vendor governance and exported traces</span>
</figcaption>
</figure>

<section>
  <h2>Four kinds of call</h2>
  <p class="sub" style="margin-bottom:16px">What happens depends on who is calling and where the call goes.</p>
  <div class="paths">
    <div class="path">
      <span class="tag">your agent → ServiceNow</span>
      <h3>Agents you built, on ServiceNow's MCP server</h3>
      <ul>
        <li>Every call carries <b>the agent and the person</b> it acts for.</li>
        <li>Your rules apply on top of ServiceNow's: <b>closing a P1 waits for the on-call engineer</b>.</li>
        <li>No lock-in to Agent Studio: the same agent also works on Salesforce and Workday.</li>
      </ul>
      <p><span class="out o-ok">ALLOW</span><span class="out o-hold">HOLD</span><span class="out o-stop">DENY</span></p>
    </div>
    <div class="path">
      <span class="tag">your agent → Salesforce</span>
      <h3>Agents on a platform that bills per action</h3>
      <ul>
        <li>Every call into Salesforce is <b>counted and priced</b> against your contract.</li>
        <li><b>Caps by team, user or agent</b> stop a looping agent before the invoice does.</li>
        <li>Deal-desk rules: discounts over a limit wait for the manager.</li>
      </ul>
      <p><span class="out o-ok">ALLOW</span><span class="out o-hold">HOLD</span><span class="out o-stop">CAP</span></p>
    </div>
    <div class="path">
      <span class="tag">SaaS agent → your systems</span>
      <h3>Vendor agents reaching inside</h3>
      <ul>
        <li>Your ERP, billing and data are registered in Magertron as MCP or REST servers.</li>
        <li>An Agentforce or Now Assist agent calling them gets the <b>same identity, policy and audit</b> as your own agents.</li>
      </ul>
      <p><span class="out o-ok">ALLOW</span><span class="out o-hold">HOLD</span><span class="out o-stop">DENY</span></p>
    </div>
    <div class="path">
      <span class="tag">vendor agent → same vendor</span>
      <h3>Entirely inside one platform</h3>
      <ul>
        <li>An Agent Studio agent working only on ServiceNow data never leaves ServiceNow.</li>
        <li>That is the vendor's control tower's job. Magertron is not in that path, and doesn't claim to be.</li>
      </ul>
      <p><span class="out o-none">VENDOR-GOVERNED</span></p>
    </div>
  </div>
</section>

<section>
  <h2>Who does what</h2>
  <p class="sub" style="margin-bottom:16px">Magertron doesn't replace the vendors' governance. It covers what no single vendor can see.</p>
  <div class="tbl"><table>
    <thead><tr><th></th><th>Inside one vendor (Control Tower, Agentforce controls)</th><th>Across vendors (Magertron)</th></tr></thead>
    <tbody>
      <tr><td>Scope</td><td>That vendor's platform, and what it chooses to connect to</td><td>Every agent call that crosses a boundary, to any vendor or internal system</td></tr>
      <tr><td>Where it runs</td><td>The vendor's cloud</td><td>Your cluster, including air-gapped</td></tr>
      <tr><td>Whose rules</td><td>Configured in that vendor's console</td><td>One set of policies, kept in your git, applied to all of them</td></tr>
      <tr><td>Identity</td><td>The vendor's user model</td><td>Your IdP: the agent plus the person it acts for, the same everywhere</td></tr>
      <tr><td>Cost</td><td>The vendor's own usage reports</td><td>Every vendor's usage side by side, rated against contracts, with caps</td></tr>
      <tr><td>Audit</td><td>Per platform</td><td>One trail across platforms, exported to the vendors' consoles too</td></tr>
    </tbody>
  </table></div>
</section>

<section>
  <h2>Policy packs per system</h2>
  <p class="sub" style="margin-bottom:16px">Like the Factory and AgentCzar packs: the customer supplies the facts as data, the pack supplies the rules. Examples of what each would hold.</p>
  <div class="packs">
    <div class="pack"><h3>ServiceNow pack</h3><ul>
      <li><span class="out o-hold">HOLD</span><span>Closing or downgrading a P1 incident, for the on-call engineer</span></li>
      <li><span class="out o-hold">HOLD</span><span>A change request outside its change window, for the CAB</span></li>
      <li><span class="out o-stop">DENY</span><span>Bulk reassignment of more than 50 tickets in one call</span></li>
    </ul></div>
    <div class="pack"><h3>Salesforce pack</h3><ul>
      <li><span class="out o-hold">HOLD</span><span>A discount beyond the rep's authority, for the deal desk</span></li>
      <li><span class="out o-stop">DENY</span><span>Exporting more than 1,000 contacts in one call</span></li>
      <li><span class="out o-stop">CAP</span><span>A team's monthly Agentforce and API spend</span></li>
    </ul></div>
    <div class="pack"><h3>Workday pack</h3><ul>
      <li><span class="out o-hold">HOLD</span><span>Any compensation change, for the HR business partner</span></li>
      <li><span class="out o-stop">DENY</span><span>Reading salary data for anyone outside the manager's org</span></li>
      <li><span class="out o-ok">ALLOW</span><span>Time-off balances and org charts</span></li>
    </ul></div>
  </div>
</section>

<section>
  <h2>Why neutral matters</h2>
  <div class="why" style="margin-top:12px">
    <div><h3>The referee isn't a player</h3><p>A company doesn't want ServiceNow deciding what agents may do in Salesforce, or the reverse. One neutral layer applies the company's own rules to both.</p></div>
    <div><h3>Agents stay portable</h3><p>Built against Magertron, an agent reaches ServiceNow, Salesforce and internal systems the same way. Switching or adding a vendor doesn't mean rebuilding governance.</p></div>
    <div><h3>Metered platforms need a meter</h3><p>When SaaS bills per agent action, finance needs every vendor's usage in one place, rated against contracts and capped before month-end.</p></div>
  </div>
</section>

<section class="xs-cta" style="display:flex;flex-wrap:wrap;gap:14px;align-items:center">
  <a href="#" onclick="openDemoModal(event)" class="cta-a primary">Try it now</a>
  <a href="developers.html" class="cta-a ghost">For developers</a>
</section>
<p class="note">ServiceNow's MCP server, Action Fabric and the expanded AI Control Tower were announced at Knowledge 2026; Salesforce prices Agentforce per action. Pack rules shown are examples of what a policy pack holds.</p>
</div></div>
"""

def die(m):
    sys.exit(f"ABORT: {m} - nothing written")

a = s.find('<section class="hero"')
f = s.find("<footer")
if a < 0 or f < 0 or f < a:
    die("solutions-crm.html has no <section class=\"hero\"> before its <footer>")
removed = s[a:f]
page = s[:a] + HERO + BODY + "\n" + s[f:]

n = 0
page, k = re.subn(r"<title>.*?</title>", f"<title>{TITLE}</title>", page, count=1, flags=re.S); n += k
page, k = re.subn(r'(<meta name="description" content=")[^"]*(")', lambda m: m.group(1) + DESC + m.group(2), page, count=1); n += k
page = re.sub(r'(<link rel="canonical" href=")[^"]*(")', r"\1https://magertron.com/solutions-saas.html\2", page, count=1)
page = re.sub(r'(<meta property="og:url" content=")[^"]*(")', r"\1https://magertron.com/solutions-saas.html\2", page, count=1)
page = re.sub(r'(<meta property="og:title" content=")[^"]*(")', lambda m: m.group(1) + TITLE + m.group(2), page, count=1)
page = re.sub(r'(<meta property="og:description" content=")[^"]*(")', lambda m: m.group(1) + DESC + m.group(2), page, count=1)
if page.count("</head>") != 1:
    die("not exactly one </head>")
fonts = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@600;700'
         '&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">\n')
page = page.replace("</head>", fonts + '<style id="xsaas">\n' + CSS + "\n</style>\n</head>", 1)

print(f"from {SRC.name}: kept head, top bar, footer, form and scripts")
print(f"replaced {len(removed):,} B of CRM content with the SaaS page ({len(HERO) + len(BODY):,} B)")
print(f"title/description set: {'yes' if n == 2 else 'title only - no meta description found'}")
if APPLY:
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}")
else:
    print("dry run - re-run with --apply to write")
