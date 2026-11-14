# etrade — E*TRADE MCP server (Magertron™-managed)

Brokerage tools for one person's E*TRADE account: accounts, balance, positions, quotes, preview / place / cancel orders. Starts in E*TRADE's **sandbox** (sample data, no money).

| Env | Default | |
|---|---|---|
| `ETRADE_CONSUMER_KEY`, `ETRADE_CONSUMER_SECRET` | — | from the server's credential Secret |
| `ETRADE_ENV` | `sandbox` | `live` = your real account |
| `ETRADE_ALLOW_LIVE_ORDERS` | `0` | `1` lets `place_order` move real money |
| `ETRADE_MAX_ORDER_USD` | `5000` | last-resort backstop; real limits go in OPA |

**Replicas: 1.** The E\*TRADE session lives in memory; a second pod would have no session.

Daily sign-in: call `etrade_login_start`, log in on E\*TRADE's page, then call `etrade_login_finish` with the code it shows. DayTrader's Trade tab does this for you.

Orders: `preview_order` first, then `place_order` with the `preview_id` and the same fields. A preview can be used once, expires after 15 minutes, and an order is never retried.
