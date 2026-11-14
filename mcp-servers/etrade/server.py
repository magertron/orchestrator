#!/usr/bin/env python3
"""etrade-mcp — an MCP server for one person's E*TRADE account, meant to sit
behind the Magertron™ gateway so every order is authorized, policy-checked,
audited and charged back like any other agent call.

Python standard library only. Speaks MCP over streamable HTTP, stateless JSON
(POST /mcp), like market-mock.

HOW E*TRADE SIGN-IN WORKS (OAuth 1.0a, their rules)
  1. etrade_login_start  -> a link to E*TRADE's own login page
  2. the person logs in there and E*TRADE shows a short verification code
  3. etrade_login_finish(verifier=CODE) -> connected
  The connection ends at midnight US Eastern (E*TRADE's rule) and goes idle
  after 2 hours without calls; this server renews it every 90 minutes while
  connected, so in practice you sign in once a day. Tokens live in memory
  only: a pod restart means signing in again.

SAFETY
  ETRADE_ENV=sandbox (default) uses E*TRADE's sandbox: canned answers, no money.
  ETRADE_ENV=live talks to your real account, and place_order still refuses
  unless ETRADE_ALLOW_LIVE_ORDERS=1. Orders must be previewed first, and
  place_order must repeat the previewed order exactly (no bait-and-switch
  between what was approved and what is sent). ETRADE_MAX_ORDER_USD is a last
  backstop; the real limits belong in Magertron policy (OPA).

SETTINGS (environment)
  ETRADE_ENV                sandbox | live                    (default sandbox)
  ETRADE_CONSUMER_KEY       E*TRADE consumer key    (from the Secret Magertron mounts as env)
  ETRADE_CONSUMER_SECRET    E*TRADE consumer secret
  ETRADE_KEY_DIR            fallback: dir holding consumer_key, consumer_secret  (/etc/etrade)
  ETRADE_ALLOW_LIVE_ORDERS  1 to allow place_order on live      (default 0)
  ETRADE_MAX_ORDER_USD      refuse orders estimated above this  (default 5000)
  MCP_BEARER_FILE           if set, every request must carry "Authorization:
                            Bearer <contents>" — the egress credential you give
                            Magertron, so nothing else in the cluster can call this
  PORT                      8080
  ETRADE_API_BASE / ETRADE_OAUTH_BASE / ETRADE_AUTHORIZE_URL   test overrides only
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.2"
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]

ENV = os.environ.get("ETRADE_ENV", "sandbox").strip().lower()
if ENV not in ("sandbox", "live"):
    raise SystemExit("ETRADE_ENV must be sandbox or live")
API = os.environ.get("ETRADE_API_BASE") or ("https://api.etrade.com" if ENV == "live" else "https://apisb.etrade.com")
OAUTH = os.environ.get("ETRADE_OAUTH_BASE") or "https://api.etrade.com"      # same host for sandbox and live
AUTHORIZE = os.environ.get("ETRADE_AUTHORIZE_URL") or "https://us.etrade.com/e/t/etws/authorize"
KEY_DIR = pathlib.Path(os.environ.get("ETRADE_KEY_DIR", "/etc/etrade"))
ALLOW_LIVE = os.environ.get("ETRADE_ALLOW_LIVE_ORDERS") == "1"
MAX_USD = float(os.environ.get("ETRADE_MAX_ORDER_USD", "5000") or 5000)
BEARER_FILE = os.environ.get("MCP_BEARER_FILE", "")

def _read(p):
    try:
        return pathlib.Path(p).read_text().strip()
    except OSError:
        return ""

def consumer():
    # Magertron mounts a server's credential Secret as env; files are the fallback.
    k = os.environ.get("ETRADE_CONSUMER_KEY", "").strip() or _read(KEY_DIR / "consumer_key")
    s = os.environ.get("ETRADE_CONSUMER_SECRET", "").strip() or _read(KEY_DIR / "consumer_secret")
    if not k or not s:
        raise Fail("E*TRADE consumer key/secret not set (ETRADE_CONSUMER_KEY / ETRADE_CONSUMER_SECRET)")
    return k, s

class Fail(Exception):
    """An error the caller should see as the tool's answer."""

# ── US Eastern date (E*TRADE tokens die at midnight ET) ──────────────────────
def et_today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # no tz database in the image: EST is close enough for a date check
        return (dt.datetime.utcnow() - dt.timedelta(hours=5)).date()

# ── OAuth 1.0a (HMAC-SHA1) ───────────────────────────────────────────────────
def _enc(s):
    return urllib.parse.quote(str(s), safe="~-._")

def oauth_header(method, url, query, token="", token_secret="", extra=None):
    ck, cs = consumer()
    o = {"oauth_consumer_key": ck, "oauth_nonce": secrets.token_hex(16),
         "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(int(time.time())),
         "oauth_version": "1.0"}
    if token:
        o["oauth_token"] = token
    o.update(extra or {})
    params = sorted((_enc(k), _enc(v)) for k, v in list(o.items()) + list((query or {}).items()))
    base = "&".join([method.upper(), _enc(url), _enc("&".join(f"{k}={v}" for k, v in params))])
    key = f"{_enc(cs)}&{_enc(token_secret)}".encode()
    o["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth realm=\"\"," + ",".join(f'{_enc(k)}="{_enc(v)}"' for k, v in sorted(o.items()))

def _http(method, url, query=None, body=None, token="", token_secret="", extra=None):
    full = url + ("?" + urllib.parse.urlencode(query) if query else "")
    h = {"Authorization": oauth_header(method, url, query, token, token_secret, extra),
         "Accept": "application/json", "User-Agent": f"etrade-mcp/{VERSION}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(full, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise Fail(f"can't reach E*TRADE ({url.split('/')[2]}): {e.reason}")

def _etrade_error(status, text):
    try:
        j = json.loads(text)
        e = j.get("Error") or j.get("error") or {}
        msg = e.get("message") or e.get("Message") or text
    except ValueError:
        msg = text
    if status == 401:
        return Fail(f"E*TRADE refused the session ({str(msg)[:200]}). Sign in again: etrade_login_start.")
    return Fail(f"E*TRADE HTTP {status}: {str(msg)[:300]}")

# ── session (memory only) ─────────────────────────────────────────────────────
S = {"req_token": "", "req_secret": "", "token": "", "secret": "", "day": None,
     "connected_at": 0.0, "last_call": 0.0}
LOCK = threading.Lock()

def connected():
    if S["token"] and S["day"] != et_today():      # past midnight ET: E*TRADE has already expired it
        S.update(token="", secret="", day=None)
    return bool(S["token"])

def api(method, path, query=None, body=None):
    if not connected():
        raise Fail("Not signed in to E*TRADE. Call etrade_login_start, then etrade_login_finish with the code.")
    status, text = _http(method, API + path, query, body, S["token"], S["secret"])
    S["last_call"] = time.time()
    if status == 204 or not text.strip():
        return {}
    if status >= 400:
        raise _etrade_error(status, text)
    try:
        return json.loads(text)
    except ValueError:
        raise Fail(f"E*TRADE answered with something that isn't JSON: {text[:200]}")

def keepalive():
    """Renew every 90 min of quiet so the 2-hour idle rule never bites."""
    while True:
        time.sleep(300)
        with LOCK:
            if connected() and time.time() - max(S["last_call"], S["connected_at"]) > 90 * 60:
                st, tx = _http("GET", OAUTH + "/oauth/renew_access_token", token=S["token"], token_secret=S["secret"])
                if st < 400:
                    S["last_call"] = time.time()
                else:
                    S.update(token="", secret="", day=None)

# ── small helpers ─────────────────────────────────────────────────────────────
def a(args, k, default=None):
    v = args.get(k, default)
    return v.strip() if isinstance(v, str) else v

def need(args, *keys):
    miss = [k for k in keys if a(args, k) in (None, "")]
    if miss:
        raise Fail("missing argument(s): " + ", ".join(miss))

def num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None

def as_list(x):
    return x if isinstance(x, list) else ([x] if x else [])

ACTIONS = {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}
PRICE_TYPES = {"MARKET", "LIMIT"}
TERMS = {"GOOD_FOR_DAY", "GOOD_UNTIL_CANCEL", "IMMEDIATE_OR_CANCEL", "FILL_OR_KILL"}

def order_spec(args):
    need(args, "account_id_key", "symbol", "action", "quantity", "price_type")
    spec = {"account_id_key": a(args, "account_id_key"), "symbol": a(args, "symbol").upper(),
            "action": a(args, "action").upper(), "price_type": a(args, "price_type").upper(),
            "order_term": (a(args, "order_term") or "GOOD_FOR_DAY").upper()}
    q = num(a(args, "quantity"))
    if not q or q <= 0 or q != int(q):
        raise Fail("quantity must be a whole number of shares above 0")
    spec["quantity"] = int(q)
    if spec["action"] not in ACTIONS:
        raise Fail(f"action must be one of {sorted(ACTIONS)}")
    if spec["price_type"] not in PRICE_TYPES:
        raise Fail(f"price_type must be MARKET or LIMIT")
    if spec["order_term"] not in TERMS:
        raise Fail(f"order_term must be one of {sorted(TERMS)}")
    if spec["price_type"] == "LIMIT":
        lp = num(a(args, "limit_price"))
        if not lp or lp <= 0:
            raise Fail("limit_price is required for a LIMIT order")
        spec["limit_price"] = round(lp, 4)
    else:
        spec["limit_price"] = None
    if not spec["symbol"].replace(".", "").replace("-", "").isalnum() or len(spec["symbol"]) > 10:
        raise Fail("symbol doesn't look like a ticker")
    return spec

def order_body(spec, client_id):
    o = {"allOrNone": "false", "priceType": spec["price_type"], "orderTerm": spec["order_term"],
         "marketSession": "REGULAR",
         "Instrument": [{"Product": {"securityType": "EQ", "symbol": spec["symbol"]},
                         "orderAction": spec["action"], "quantityType": "QUANTITY",
                         "quantity": str(spec["quantity"])}]}
    if spec["limit_price"] is not None:
        o["limitPrice"] = str(spec["limit_price"])
    return {"orderType": "EQ", "clientOrderId": client_id, "Order": [o]}

PREVIEWS = {}            # previewId -> {spec, client_id, estimate, at}
PREVIEW_TTL = 15 * 60

# ── tools ────────────────────────────────────────────────────────────────────
def t_status(args):
    c = connected()
    return {"environment": ENV, "connected": c,
            "connected_since": dt.datetime.utcfromtimestamp(S["connected_at"]).isoformat() + "Z" if c else None,
            "expires": "midnight US Eastern" if c else None,
            "live_orders_allowed": ENV == "live" and ALLOW_LIVE, "max_order_usd": MAX_USD,
            "note": "Sandbox: E*TRADE returns canned sample data; no real money moves." if ENV == "sandbox" else
                    "LIVE account."}

def t_login_start(args):
    ck, _ = consumer()
    st, tx = _http("GET", OAUTH + "/oauth/request_token", extra={"oauth_callback": "oob"})
    if st >= 400:
        raise _etrade_error(st, tx)
    q = dict(urllib.parse.parse_qsl(tx))
    if not q.get("oauth_token"):
        raise Fail(f"E*TRADE didn't return a request token: {tx[:200]}")
    with LOCK:
        S["req_token"], S["req_secret"] = q["oauth_token"], q.get("oauth_token_secret", "")
    url = f"{AUTHORIZE}?{urllib.parse.urlencode({'key': ck, 'token': q['oauth_token']})}"
    return {"authorize_url": url, "environment": ENV,
            "next": "Open authorize_url, log in to E*TRADE and accept. E*TRADE shows a verification code; "
                    "call etrade_login_finish with verifier=<that code> within 5 minutes."}

def t_login_finish(args):
    need(args, "verifier")
    if not S["req_token"]:
        raise Fail("Start with etrade_login_start.")
    st, tx = _http("GET", OAUTH + "/oauth/access_token", token=S["req_token"], token_secret=S["req_secret"],
                   extra={"oauth_verifier": a(args, "verifier")})
    if st >= 400:
        raise Fail("E*TRADE didn't accept that code (it is single-use and expires in 5 minutes). "
                   "Run etrade_login_start again.")
    q = dict(urllib.parse.parse_qsl(tx))
    if not q.get("oauth_token"):
        raise Fail(f"E*TRADE didn't return an access token: {tx[:200]}")
    with LOCK:
        S.update(token=q["oauth_token"], secret=q.get("oauth_token_secret", ""), day=et_today(),
                 connected_at=time.time(), last_call=time.time(), req_token="", req_secret="")
    return {"connected": True, "environment": ENV, "expires": "midnight US Eastern"}

def t_logout(args):
    if connected():
        try:
            _http("GET", OAUTH + "/oauth/revoke_access_token", token=S["token"], token_secret=S["secret"])
        except Fail:
            pass
    with LOCK:
        S.update(token="", secret="", day=None)
    PREVIEWS.clear()
    return {"connected": False}

def t_accounts(args):
    j = api("GET", "/v1/accounts/list.json")
    accts = as_list(((j.get("AccountListResponse") or {}).get("Accounts") or {}).get("Account"))
    return {"accounts": [{"account_id_key": x.get("accountIdKey"), "account_id": x.get("accountId"),
                          "name": x.get("accountName") or x.get("accountDesc"), "type": x.get("accountType"),
                          "mode": x.get("accountMode"), "status": x.get("accountStatus"),
                          "institution": x.get("institutionType")} for x in accts]}

def t_balance(args):
    need(args, "account_id_key")
    j = api("GET", f"/v1/accounts/{urllib.parse.quote(a(args, 'account_id_key'))}/balance.json",
            {"instType": "BROKERAGE", "realTimeNAV": "true"})
    b = j.get("BalanceResponse") or {}
    c = b.get("Computed") or {}
    rt = c.get("RealTimeValues") or {}
    return {"account_id": b.get("accountId"), "account_type": b.get("accountType"),
            "total_value": num(rt.get("totalAccountValue")), "net_market_value": num(rt.get("netMv")),
            "cash_available": num(c.get("cashAvailableForInvestment")),
            "cash_buying_power": num(c.get("cashBuyingPower")), "margin_buying_power": num(c.get("marginBuyingPower")),
            "settled_cash": num(c.get("settledCashForInvestment"))}

def t_portfolio(args):
    need(args, "account_id_key")
    j = api("GET", f"/v1/accounts/{urllib.parse.quote(a(args, 'account_id_key'))}/portfolio.json")
    out = []
    for ap in as_list((j.get("PortfolioResponse") or {}).get("AccountPortfolio")):
        for p in as_list(ap.get("Position")):
            pr, qk = p.get("Product") or {}, p.get("Quick") or {}
            out.append({"symbol": pr.get("symbol") or p.get("symbolDescription"), "type": pr.get("securityType"),
                        "quantity": num(p.get("quantity")), "price_paid": num(p.get("pricePaid")),
                        "last": num(qk.get("lastTrade")), "market_value": num(p.get("marketValue")),
                        "total_cost": num(p.get("totalCost")), "total_gain": num(p.get("totalGain")),
                        "total_gain_pct": num(p.get("totalGainPct")), "days_gain": num(p.get("daysGain")),
                        "days_gain_pct": num(p.get("daysGainPct"))})
    return {"positions": out}

def t_orders(args):
    need(args, "account_id_key")
    q = {"count": "50"}
    if a(args, "status"):
        q["status"] = a(args, "status").upper()
    j = api("GET", f"/v1/accounts/{urllib.parse.quote(a(args, 'account_id_key'))}/orders.json", q)
    out = []
    for o in as_list((j.get("OrdersResponse") or {}).get("Order")):
        for d in as_list(o.get("OrderDetail")):
            for ins in as_list(d.get("Instrument")):
                out.append({"order_id": o.get("orderId"), "status": d.get("status"),
                            "placed": d.get("placedTime"), "symbol": (ins.get("Product") or {}).get("symbol"),
                            "action": ins.get("orderAction"), "quantity": num(ins.get("orderedQuantity")),
                            "filled": num(ins.get("filledQuantity")), "price_type": d.get("priceType"),
                            "limit_price": num(d.get("limitPrice")), "avg_price": num(ins.get("averageExecutionPrice"))})
    return {"orders": out}

def t_quote(args):
    need(args, "symbols")
    syms = ",".join(s.strip().upper() for s in str(a(args, "symbols")).split(",") if s.strip())[:200]
    j = api("GET", f"/v1/market/quote/{urllib.parse.quote(syms, safe=',')}.json")
    out = []
    for q in as_list((j.get("QuoteResponse") or {}).get("QuoteData")):
        al = q.get("All") or {}
        out.append({"symbol": (q.get("Product") or {}).get("symbol"), "last": num(al.get("lastTrade")),
                    "change": num(al.get("changeClose")), "change_pct": num(al.get("changeClosePercentage")),
                    "bid": num(al.get("bid")), "bid_size": num(al.get("bidSize")),
                    "ask": num(al.get("ask")), "ask_size": num(al.get("askSize")),
                    "open": num(al.get("open")), "high": num(al.get("high")), "low": num(al.get("low")),
                    "volume": num(al.get("totalVolume")), "time": q.get("dateTime"),
                    "time_utc": q.get("dateTimeUTC"), "status": q.get("quoteStatus")})
    return {"quotes": out}

def t_preview(args):
    spec = order_spec(args)
    cid = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(20))
    j = api("POST", f"/v1/accounts/{urllib.parse.quote(spec['account_id_key'])}/orders/preview.json",
            body={"PreviewOrderRequest": order_body(spec, cid)})
    r = j.get("PreviewOrderResponse") or {}
    ids = as_list(r.get("PreviewIds"))
    if not ids:
        raise Fail(f"E*TRADE returned no preview id: {json.dumps(j)[:300]}")
    pid = str(ids[0].get("previewId"))
    od = (as_list(r.get("Order")) or [{}])[0]
    est = num(od.get("estimatedTotalAmount"))
    msgs = [m.get("description") for m in as_list((od.get("messages") or {}).get("Message")) if m.get("description")]
    PREVIEWS[pid] = {"spec": spec, "client_id": cid, "estimate": est, "at": time.time()}
    for k in [k for k, v in PREVIEWS.items() if time.time() - v["at"] > PREVIEW_TTL]:
        PREVIEWS.pop(k, None)
    return {"preview_id": pid, "order": {k: v for k, v in spec.items() if k != "account_id_key"},
            "estimated_total": est, "estimated_commission": num(od.get("estimatedCommission")),
            "messages": msgs, "environment": ENV,
            "next": "To send it, call place_order with this preview_id and the SAME order fields. Valid 15 minutes."}

def t_place(args):
    need(args, "preview_id")
    spec = order_spec(args)
    pv = PREVIEWS.get(str(a(args, "preview_id")))
    if not pv or time.time() - pv["at"] > PREVIEW_TTL:
        raise Fail("Unknown or expired preview_id. Preview the order again.")
    if pv["spec"] != spec:
        diff = [k for k in spec if spec[k] != pv["spec"].get(k)]
        raise Fail(f"This order doesn't match what was previewed ({', '.join(diff)} differ). Preview it again.")
    if ENV == "live" and not ALLOW_LIVE:
        raise Fail("Live orders are switched off on this server (ETRADE_ALLOW_LIVE_ORDERS is not 1).")
    if pv["estimate"] is not None and pv["estimate"] > MAX_USD:
        raise Fail(f"Estimated ${pv['estimate']:,.2f} is above this server's backstop of ${MAX_USD:,.0f}.")
    body = order_body(spec, pv["client_id"])
    body["PreviewIds"] = [{"previewId": int(a(args, "preview_id")) if str(a(args, "preview_id")).isdigit() else a(args, "preview_id")}]
    # Never retried: a network error here may still have placed the order. Check list_orders instead.
    j = api("POST", f"/v1/accounts/{urllib.parse.quote(spec['account_id_key'])}/orders/place.json",
            body={"PlaceOrderRequest": body})
    PREVIEWS.pop(str(a(args, "preview_id")), None)
    r = j.get("PlaceOrderResponse") or {}
    ids = [str(x.get("orderId")) for x in as_list(r.get("OrderIds"))]
    od = (as_list(r.get("Order")) or [{}])[0]
    msgs = [m.get("description") for m in as_list((od.get("messages") or {}).get("Message")) if m.get("description")]
    return {"placed": bool(ids), "order_ids": ids, "order": {k: v for k, v in spec.items() if k != "account_id_key"},
            "messages": msgs, "environment": ENV}

def t_cancel(args):
    need(args, "account_id_key", "order_id")
    oid = a(args, "order_id")
    j = api("PUT", f"/v1/accounts/{urllib.parse.quote(a(args, 'account_id_key'))}/orders/cancel.json",
            body={"CancelOrderRequest": {"orderId": int(oid) if str(oid).isdigit() else oid}})
    r = j.get("CancelOrderResponse") or {}
    return {"cancelled": True, "order_id": r.get("orderId", oid), "time": r.get("cancelTime")}

ACCT = {"account_id_key": {"type": "string", "description": "accountIdKey from list_accounts"}}
ORDER = {**ACCT,
         "symbol": {"type": "string"},
         "action": {"type": "string", "enum": sorted(ACTIONS)},
         "quantity": {"type": "integer", "minimum": 1},
         "price_type": {"type": "string", "enum": sorted(PRICE_TYPES)},
         "limit_price": {"type": "number", "description": "required for LIMIT"},
         "order_term": {"type": "string", "enum": sorted(TERMS), "default": "GOOD_FOR_DAY"}}
RO = {"readOnlyHint": True, "openWorldHint": True}
TOOLS = {}
def add(name, desc, props, req, fn, ann):
    TOOLS[name] = {"name": name, "description": desc, "annotations": ann,
                   "inputSchema": {"type": "object", "properties": props, "required": list(req)}, "_h": fn}

add("etrade_status", "Is the E*TRADE session connected, sandbox or live, and the order backstop.", {}, (), t_status, RO)
add("etrade_login_start", "Begin the daily E*TRADE sign-in. Returns a link to E*TRADE's login page; the person "
    "logs in there and gets a verification code.", {}, (), t_login_start, {"readOnlyHint": False, "openWorldHint": True})
add("etrade_login_finish", "Finish sign-in with the verification code E*TRADE showed.",
    {"verifier": {"type": "string"}}, ("verifier",), t_login_finish, {"readOnlyHint": False, "openWorldHint": True})
add("etrade_logout", "End the E*TRADE session now.", {}, (), t_logout, {"readOnlyHint": False, "openWorldHint": True})
add("list_accounts", "The E*TRADE accounts of the signed-in person.", {}, (), t_accounts, RO)
add("get_balance", "Account value, cash and buying power.", ACCT, ("account_id_key",), t_balance, RO)
add("get_portfolio", "Positions with cost, market value and gain.", ACCT, ("account_id_key",), t_portfolio, RO)
add("list_orders", "Recent orders. status: OPEN, EXECUTED, CANCELLED, INDIVIDUAL_FILLS, CANCEL_REQUESTED, EXPIRED, REJECTED.",
    {**ACCT, "status": {"type": "string"}}, ("account_id_key",), t_orders, RO)
add("get_quote", "E*TRADE quotes for comma-separated symbols.", {"symbols": {"type": "string"}}, ("symbols",), t_quote, RO)
add("preview_order", "Preview a stock order: E*TRADE's estimated cost and warnings. Moves no money. Returns preview_id.",
    ORDER, ("account_id_key", "symbol", "action", "quantity", "price_type"), t_preview, RO)
add("place_order", "MOVES MONEY. Send a previewed stock order. Needs preview_id and the same order fields as the "
    "preview. Never retried automatically.", {**ORDER, "preview_id": {"type": "string"}},
    ("account_id_key", "preview_id", "symbol", "action", "quantity", "price_type"), t_place,
    {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
add("cancel_order", "Cancel an open order.", {**ACCT, "order_id": {"type": "string"}}, ("account_id_key", "order_id"),
    t_cancel, {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})

# ── MCP over HTTP (stateless JSON-RPC) ────────────────────────────────────────
def handle(msg):
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if mid is None:
        return None
    def ok(result): return {"jsonrpc": "2.0", "id": mid, "result": result}
    def err(code, text): return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "etrade-mcp", "version": VERSION},
                   "instructions": f"E*TRADE brokerage ({ENV}). Sign in daily with etrade_login_start/finish. "
                                   "Orders: preview_order, then place_order with the same fields."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{k: v for k, v in t.items() if k != "_h"} for t in TOOLS.values()]})
    if method == "tools/call":
        t = TOOLS.get(params.get("name", ""))
        if not t:
            return err(-32602, f"unknown tool: {params.get('name')}")
        try:
            data = t["_h"](params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(data, indent=2)}], "isError": False})
        except Fail as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # noqa: BLE001 — never leak a stack trace to the caller
            print(f"tool {t['name']} crashed: {type(e).__name__}: {e}", flush=True)
            return ok({"content": [{"type": "text", "text": f"internal error ({type(e).__name__})"}], "isError": True})
    return err(-32601, f"method not found: {method}")

class H(BaseHTTPRequestHandler):
    server_version = "etrade-mcp/" + VERSION
    def log_message(self, f, *args):   # never log headers or bodies: they carry account data
        if os.environ.get("QUIET") != "1":
            print(f"{self.address_string()} {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}", flush=True)
    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)
    def _authorized(self):
        if not BEARER_FILE:
            return True
        want = _read(BEARER_FILE)
        got = self.headers.get("Authorization", "")
        return bool(want) and hmac.compare_digest(got.encode(), f"Bearer {want}".encode())
    def do_GET(self):
        p = self.path.split("?")[0]
        # Every health path Magertron and Kubernetes probe: /health, /health/live,
        # /health/ready (Magertron), /healthz, /readyz (Kubernetes convention).
        if p.rstrip("/") in ("/health", "/health/live", "/health/ready", "/healthz", "/readyz", "/livez"):
            return self._send(200, json.dumps({"status": "ok", "environment": ENV,
                                               "connected": connected()}).encode())
        if p.rstrip("/") == "/mcp":
            return self._send(405, b'{"error":"use POST"}')
        self._send(404, b'{"error":"not found"}')
    def do_DELETE(self):
        self._send(405)
    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/mcp":
            return self._send(404, b'{"error":"not found"}')
        if not self._authorized():
            return self._send(401, b'{"error":"unauthorized"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        except Exception:
            return self._send(400, b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}')
        if isinstance(body, list):
            out = [r for r in (handle(m) for m in body if isinstance(m, dict)) if r is not None]
            return self._send(200, json.dumps(out).encode()) if out else self._send(202)
        if not isinstance(body, dict):
            return self._send(400, b'{"error":"bad request"}')
        res = handle(body)
        if res is None:
            return self._send(202)
        self._send(200, json.dumps(res).encode())

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=keepalive, daemon=True).start()
    print(f"etrade-mcp {VERSION}: {ENV} ({API}), {len(TOOLS)} tools on :{port}/mcp, "
          f"live orders {'ON' if ENV == 'live' and ALLOW_LIVE else 'off'}, backstop ${MAX_USD:,.0f}, "
          f"gateway bearer {'required' if BEARER_FILE else 'not required'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
