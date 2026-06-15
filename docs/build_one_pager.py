# Magertron One-Pager — build script (the editable source for the PDF layout)
#
# USAGE:  cd into this folder, then:  python3 build_one_pager.py
# Requires: pip install reportlab pillow
# Needs these files alongside it: favicon.png, dashboard.png, chargeback.png
# Outputs: Magertron_One_Pager.pdf
#
# COMMON EDITS:
#   - Body copy: search for p1, p2 (intro paragraphs) and the bullets list (PROOF section)
#   - Feature lines: the feats list (Govern / Cap the spend / etc.)
#   - Colors: BLUE / DARK / GRAY constants near the top
#   - Section spacing: the  y -= NN  lines after each header rule
#   - Icon size: icon_h variable in the header block

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Image as RLImage
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from PIL import Image

BLUE = HexColor("#2563EB")
DARK = HexColor("#111827")
GRAY = HexColor("#4B5563")
LGRAY = HexColor("#6B7280")

W, H = letter
LM, RM = 0.9*inch, 0.9*inch
CW = W - LM - RM

c = canvas.Canvas("Magertron_One_Pager.pdf", pagesize=letter)

def style(name, size, leading, color=DARK, bold=False, italic=False, space=0):
    font = "Helvetica"
    if bold and italic: font = "Helvetica-BoldOblique"
    elif bold: font = "Helvetica-Bold"
    elif italic: font = "Helvetica-Oblique"
    return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading,
                          textColor=color, spaceAfter=space, alignment=TA_LEFT)

def para(text, st, x, y, w):
    p = Paragraph(text, st)
    pw, ph = p.wrap(w, 1000)
    p.drawOn(c, x, y - ph)
    return y - ph

def rule(y, x=LM, w=CW, color=BLUE, lw=1.6):
    c.setStrokeColor(color); c.setLineWidth(lw)
    c.line(x, y, x+w, y)

y = H - 1.35*inch

# Eyebrow
c.setFillColor(BLUE)
# Magertron icon badge
icon_h = 13
icon_w = icon_h * (96/64)
c.drawImage("favicon.png", LM, y-2.5, width=icon_w, height=icon_h, mask='auto')
tx = LM + icon_w + 7
c.setFont("Helvetica-Bold", 10.5)
c.drawString(tx, y, "MAGERTRON\u2122")
mw = c.stringWidth("MAGERTRON\u2122", "Helvetica-Bold", 10.5)
c.setFont("Helvetica-Bold", 8.5)
c.drawString(tx+mw+6, y+0.6, "MCP ORCHESTRATOR")
y -= 33
# Title
c.setFont("Helvetica-Bold", 23); c.setFillColor(DARK)
c.drawString(LM, y, "The Spend Plane for the Agent Economy")
y -= 20
# Subtitle
c.setFont("Helvetica", 10.5); c.setFillColor(GRAY)
c.drawString(LM, y, "Governance, compliance and cost control for AI agents and MCP server usage in the enterprise.")
y -= 16
rule(y, lw=2.2); y -= 18

body = style("body", 9.8, 13, GRAY)
def lead(text):
    return f'<font name="Helvetica-Bold" color="#111827">{text}</font>'

p1 = (lead("Every integration wave needs a control plane.") +
      " SOAP had app servers. REST had API gateways. gRPC and GraphQL had the service mesh. "
      "The technology underneath changes \u2014 deploy services, route traffic, govern who can call what, "
      "audit everything \u2014 but the operational shape never does. MCP is the newest wave, and its "
      "control plane doesn\u2019t exist yet.")
y = para(p1, body, LM, y, CW); y -= 10

p2 = (lead("And this wave moves money.") +
      " Agents now transact through MCP servers that bill on usage, and tens of thousands of them turn "
      "spend unpredictable fast. Developers stand up shadow servers with no audit trail or lifecycle, "
      "agents call enterprise systems with no per-tool access control, and no tool today gives a CFO a "
      "budget lever. Governance and cost are converging into one missing layer \u2014 orchestration is that layer.")
y = para(p2, body, LM, y, CW); y -= 36

# THE SOLUTION header
c.setFont("Helvetica-Bold", 12); c.setFillColor(DARK)
c.drawString(LM, y, "THE SOLUTION"); y -= 6
rule(y); y -= 22

# Two columns
col_w = 3.55*inch
img_x = LM + col_w + 0.3*inch
img_w = CW - col_w - 0.3*inch

sol_top = y
sub = style("sub", 10.5, 13.5, DARK, bold=True)
ty = para("Governance gets us in the door. Cost control makes us indispensable.", sub, LM, y, col_w); ty -= 6
desc = style("desc", 9.8, 13, GRAY)
ty = para("Magertron\u2122 runs in your own Kubernetes cluster, in the transaction path \u2014 enforcing policy and capping cost.", desc, LM, ty, col_w); ty -= 10

feats = [
    ("Govern", "design-time + run-time policy, default-deny."),
    ("Cap the spend", "per-server, per-tool, real-time."),
    ("Meter &amp; charge back", "metered, hard per-user limits."),
    ("Detect &amp; block", "rogue servers, in or out of perimeter."),
    ("Attribute the spend", "per user, per dept, point-in-time."),
]
fstyle = style("f", 9.8, 13.5, GRAY)
for name, rest in feats:
    txt = f'<font name="Helvetica-Bold" color="#111827">{name}</font> \u2014 {rest}'
    ty = para(txt, fstyle, LM, ty, col_w); ty -= 3
ty -= 4
cfo = style("cfo", 10, 13, BLUE, bold=True)
ty = para("The CFO\u2019s guardrail \u2014 not a kill switch.", cfo, LM, ty, col_w)

# Dashboard image (right col), top-aligned with subhead
dim = Image.open("dashboard.png"); ar = dim.height/dim.width
ih = img_w*ar
c.drawImage("dashboard.png", img_x, sol_top - ih, width=img_w, height=ih)
cap = style("cap", 8, 10.5, LGRAY, italic=True)
cy = para("Live AI spend, committed vs. pay-as-you-go, and per-vendor success.", cap, img_x, sol_top - ih - 4, img_w)

y = min(ty, cy) - 38

# PROOF header
c.setFont("Helvetica-Bold", 12); c.setFillColor(DARK)
c.drawString(LM, y, "PROOF"); y -= 6
rule(y); y -= 22

proof_top = y
# Chargeback image left
cim = Image.open("chargeback.png"); car = cim.height/cim.width
cimg_w = 2.75*inch
cih = cimg_w*car
# allow the image to run closer to the footer so it keeps full width
max_h = (proof_top) - (0.55*inch + 0.45*inch)
if cih > max_h:
    cih = max_h
    cimg_w = cih/car
c.drawImage("chargeback.png", LM, proof_top - cih, width=cimg_w, height=cih)
para("Chargeback by team and agent.", cap, LM, proof_top - cih - 4, cimg_w)

# Proof bullets right
px = LM + cimg_w + 0.35*inch
pw = CW - cimg_w - 0.35*inch
py = proof_top
hd = style("hd", 10.5, 13, DARK, bold=True)
py = para("Built and shipping \u2014 not a slideware promise.", hd, px, py, pw); py -= 9

bullets = [
    "Per-tool RBAC (default-deny), mTLS, SSO/SCIM, OCSF audit.",
    "BYOK across five auth protocols: DCR (RFC 7591), OAuth2 Client Credentials, OAuth2 Delegated, mTLS, Bearer.",
    "Validated with Okta, Splunk, Datadog, OIDC, SCIM.",
    "SOC 2 Type II in progress.",
]
bstyle = style("b", 9.8, 13.5, GRAY)
for b in bullets:
    # check mark
    c.setFont("Helvetica", 9.8); c.setFillColor(BLUE)
    c.drawString(px, py - 9.8, "\u2713")
    py = para(b, bstyle, px + 15, py, pw - 15); py -= 9

# Footer
fy = 0.55*inch
c.setFont("Helvetica", 8); c.setFillColor(LGRAY)
c.drawCentredString(W/2, fy, "Magertron\u2122, Inc.   \u00b7   Confidential   \u00b7   magertron.com")

c.save()
print("saved")
