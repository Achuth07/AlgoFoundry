<div align="center">

# AlgoFoundry

**A self-hosted bridge that turns TradingView alerts into Interactive Brokers orders — with a web dashboard for sizing, connection setup, monitoring, and a one-click kill switch.**

</div>

---

## Overview

AlgoFoundry receives webhook alerts from TradingView indicators, applies
position-aware risk gating, and routes the resulting orders to Interactive
Brokers through [`ib_async`](https://github.com/ib-api-reloaded/ib_async). It runs
as a single FastAPI service with an HTMX dashboard — no cloud SaaS, no monthly
relay fees, full control of your own keys and execution.

```
TradingView indicator (alert)
        │  HTTPS POST (JSON + shared secret)
        ▼
AlgoFoundry  ──►  webhook auth  ──►  position-aware gating
        │
        ▼
   ib_async  ──►  IB Gateway / TWS (API mode)  ──►  Interactive Brokers
```

The **bridge is the source of truth for position state**. On every signal it reads
your live IBKR positions and acts accordingly:

- a **BUY** is executed only if you are currently *flat* in that symbol;
- a **SELL** is executed only if you currently *hold* that symbol.

Because state is reconciled against the broker on every signal, a restart or a
manual trade can't desync the system. This is also why the TradingView exit signal
is intentionally **stateless** — the indicator detects the take-profit condition,
and the bridge decides whether to act on it.

## Features

- **TradingView → IBKR automation** via a simple JSON webhook.
- **Web dashboard** (sidebar layout): Trading, Event Log, and Settings tabs.
- **Position-aware gating**: buy-if-flat / sell-if-holding, enforced server-side.
- **Three sizing modes**: fixed shares, fixed dollars, or percent of equity.
- **Risk guardrails**: per-position dollar cap, max concurrent positions,
  per-side enable/disable, and a global kill switch.
- **Live monitoring**: positions, open orders, account summary, and a full event
  log of every signal and order.
- **Manual execution**: buy / sell / flatten any symbol from the UI.
- **Secure by design**: secret-authenticated webhook + HTTP Basic-auth dashboard,
  intended to run behind a tunnel or VPN.
- **Persistent config**: all settings stored in SQLite, surviving restarts.

## Included strategy

The repository ships a reference **long-only swing strategy for the 4-hour
timeframe** as a single TradingView indicator,
[`pine/algofoundry_strategy.pine`](pine/algofoundry_strategy.pine), which emits
both signals:

| Signal | Logic |
|--------|-------|
| **Buy** | Unfiltered ATR trailing-stop flip to long (fires once on the flip) |
| **Take-profit (sell)** | Strong bearish close after an RSI overbought cross-under |

Signals **strictly alternate** — buy → take-profit → buy → take-profit — so you
get exactly one take-profit after each buy before the next buy can fire (the only
exit is the take-profit; there is no stop-loss). Both fire on **bar close**
(`alert.freq_once_per_bar_close`) to avoid intrabar repainting, and a single
"Any alert() function call" alert drives the whole strategy. The bridge
independently enforces buy-if-flat / sell-if-holding, so the two stay consistent
even if a webhook is missed. The bridge is strategy-agnostic, so you can point any
indicator that emits `buy`/`sell` webhooks at it.

An enhanced variant, [`pine/algofoundry_strategy_v2.pine`](pine/algofoundry_strategy_v2.pine),
adds an optional protective **stop-loss** exit, optional **trend / volume entry
filters**, a **re-entry cooldown**, and a richer webhook payload (`price`,
`reason`). With all toggles off it behaves exactly like v1.

A second, independent strategy —
[`pine/algofoundry_trend_pullback.pine`](pine/algofoundry_trend_pullback.pine) —
is a research-based **trend-pullback momentum** system for 4H US equities: it buys
pullbacks inside a confirmed 20/50/200 EMA uptrend, gated by an **ADX** strength
filter, triggered when **RSI** reclaims 50, with an **ATR stop**, an **R-multiple
take-profit**, and an optional trend-break exit.

## Tech stack

FastAPI · Jinja2 + HTMX · `ib_async` · SQLite · Pine Script v5

## Project structure

```
AlgoFoundry/
├── app/
│   ├── main.py          # FastAPI app: /webhook (public) + dashboard (Basic-auth)
│   ├── trading.py       # Signal gating: buy-if-flat / sell-if-holding, caps
│   ├── broker.py        # ib_async wrapper in a dedicated thread/loop
│   ├── db.py            # SQLite settings (key-value) + event log
│   ├── models.py        # Webhook payload schema
│   ├── templates/       # HTMX dashboard + fragments
│   └── static/          # Logo assets
├── pine/                # TradingView strategy indicator (entry + exit)
├── requirements.txt
├── run.sh
└── .env.example
```

## Prerequisites

- Python 3.10+
- An Interactive Brokers account with **IB Gateway** or **TWS** (start with paper).
- A **paid TradingView plan** (Plus or higher) — webhook alerts are not available
  on the free tier.

## Installation

```bash
git clone https://github.com/<your-username>/AlgoFoundry.git
cd AlgoFoundry
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit ALGOFOUNDRY_USER / ALGOFOUNDRY_PASSWORD
```

## Configuration

Runtime settings live in the dashboard (stored in SQLite). `.env` only holds
bootstrap values:

| Variable | Description |
|----------|-------------|
| `ALGOFOUNDRY_HOST` / `ALGOFOUNDRY_PORT` | Where the GUI/server binds (default `127.0.0.1:8000`) |
| `ALGOFOUNDRY_USER` / `ALGOFOUNDRY_PASSWORD` | Dashboard login credentials |
| `ALGOFOUNDRY_WEBHOOK_SECRET` | Optional fixed webhook secret (auto-generated if blank) |
| `ALGOFOUNDRY_DB` | SQLite database path |

## Usage

### 1. Run IB Gateway (paper first)

Log into your **paper** account, then under **Configure → Settings → API**:
enable *ActiveX and Socket Clients*, set the socket port (**4002** paper / **4001**
live for Gateway; 7497 / 7496 for TWS), and add `127.0.0.1` to trusted IPs. For
unattended operation, run it under [IBC](https://github.com/IbcAlpha/IBC) so it
auto-logs-in and survives the required daily restart.

### 2. Start AlgoFoundry

```bash
./run.sh        # dashboard at http://127.0.0.1:8000
```

Log in, set your ports and sizing in **Settings**, click **Connect**, then enable
**Trading** only when you're ready.

### 3. Expose the webhook over HTTPS

TradingView needs a public HTTPS endpoint. Expose **only** `/webhook` — never the
dashboard. Recommended options:

- **Cloudflare Tunnel** from the host running AlgoFoundry (no open inbound ports), or
- a **VPS** with **Caddy** in front for automatic TLS.

Additionally, IP-allowlist TradingView's published webhook source IPs at your
reverse proxy, and keep the shared secret in every alert payload.

### 4. Create TradingView alerts

Add `pine/algofoundry_strategy.pine` to your 4H chart and paste the shared secret
(shown in **Settings → Webhook**) into the indicator's *Webhook secret* input.
Create a **single** alert with condition **"Any alert() function call"**, frequency
**Once Per Bar Close**, and Webhook URL `https://YOUR-DOMAIN/webhook`. The one
alert handles both buy and sell.

## Webhook API

`POST /webhook`

```json
{ "action": "buy",  "symbol": "AAPL", "secret": "YOUR_SECRET" }
{ "action": "sell", "symbol": "AAPL", "secret": "YOUR_SECRET" }
```

`symbol` may use the TradingView placeholder `{{ticker}}`. An optional
`"qty": 10` overrides the dashboard sizing for that order.

## Sizing modes

| Mode | Shares ordered |
|------|----------------|
| Fixed shares | exactly *N* shares |
| Fixed dollars *(default)* | `floor(dollars / price)` |
| % of equity | `floor(NetLiquidation × pct% / price)` |

Every order is additionally capped by **Max $ / position** and **Max positions**.

## Security & safety

- Start on **paper** and run it for a while before trading live.
- The **kill switch** disables trading instantly; new signals are rejected
  (existing positions are *not* auto-closed — use **Manual → Flatten**).
- Bind the dashboard to localhost and reach it via a tunnel/VPN; expose only the
  webhook endpoint publicly.
- One IBKR username allows a single API session — give the bot its own login or a
  dedicated client ID so it doesn't conflict with a manual TWS session.

## Roadmap

- Bracket / stop-loss order support.
- Fill and error notifications (Telegram / email).
- Daily reconcile-on-startup logging.
- Optional Docker + IBC compose for fully unattended deployment.

## Disclaimer

AlgoFoundry is provided for educational purposes and is **not financial advice**.
Automated trading carries significant risk, including the loss of capital. You are
solely responsible for any orders placed through this software. Test thoroughly on
a paper account and use at your own risk.

## License

Released under the [MIT License](LICENSE).
