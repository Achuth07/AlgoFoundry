# AlgoFoundry

A self-hosted bridge that turns **TradingView alerts** into **Interactive Brokers**
orders, with a web GUI for position sizing, IBKR connection setup, monitoring,
and a kill switch.

```
TradingView indicator (alert) ──HTTPS POST──▶ AlgoFoundry webhook
                                                   │
                                          position-aware gating
                                                   │
                                            ib_async ──▶ IB Gateway / TWS ──▶ IBKR
```

The strategy is **long-only swing trading on the 4-hour timeframe**:

- **Entry (buy):** unfiltered ATR trailing-stop flip to long (`pine/entry_indicator.pine`).
- **Exit (sell):** take-profit on a strong bearish close after an RSI overbought
  cross-under (`pine/exit_indicator.pine`).

The **bridge owns position state** (it reads live positions from IBKR on every
signal): a BUY is acted on only if you're flat in that symbol, a SELL only if you
hold it. That's why the Pine exit signal is stateless — see the note in the exit
script.

## Components

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app: `/webhook` (public, secret-auth) + web GUI (Basic-auth) |
| `app/trading.py` | Signal gating: buy-if-flat / sell-if-holding, kill switch, caps |
| `app/broker.py` | `ib_async` wrapper in a dedicated thread/loop; orders, positions, sizing |
| `app/db.py` | SQLite settings (key-value) + event log |
| `app/models.py` | Webhook payload schema |
| `app/templates/` | HTMX dashboard |
| `pine/` | TradingView entry & exit indicators with `alert()` calls |

## Setup

### 1. Install
```bash
cd AlgoFoundry
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit ALGOFOUNDRY_USER / PASSWORD
```

### 2. Run IB Gateway (paper first)
- Install **IB Gateway**, log into your **paper** account.
- Configure → API → Settings: enable *ActiveX and Socket Clients*, socket port
  **4002** (paper) / **4001** (live), add `127.0.0.1` to trusted IPs.
- For unattended operation, run it under **IBC** (https://github.com/IbcAlpha/IBC)
  so it auto-logs-in and survives the daily restart.

### 3. Start AlgoFoundry
```bash
./run.sh      # serves the GUI on http://127.0.0.1:8000
```
Open the GUI (log in with the credentials from `.env`), set your sizing/ports,
click **Connect**. Flip **Trading enabled** ON only when you're ready.

### 4. Expose the webhook (HTTPS)
TradingView needs a public HTTPS URL. Do **not** expose the GUI publicly — only
`/webhook`. Two clean options:
- **Cloudflare Tunnel** from the box running AlgoFoundry (no open ports), or
- A **VPS** with **Caddy** in front for automatic TLS.

Then IP-allowlist TradingView's published webhook source IPs at the proxy, and
keep the shared `secret` (shown in the GUI) in every alert payload.

### 5. TradingView alerts
- Add both indicators from `pine/` to your 4H chart; paste the **same secret**
  into each indicator's "Webhook secret" input (matches the GUI value).
- Create an alert on each: condition = **"Any alert() function call"**,
  **Once Per Bar Close**, Webhook URL = `https://YOUR-DOMAIN/webhook`.
- Webhooks require a **paid TradingView plan** (Plus or higher).

## Webhook payload
```json
{ "action": "buy",  "symbol": "AAPL", "secret": "YOUR_SECRET" }
{ "action": "sell", "symbol": "AAPL", "secret": "YOUR_SECRET" }
```
Optional `"qty": 10` overrides the GUI sizing. `symbol` can be `{{ticker}}`.

## Sizing modes (GUI)
- **Fixed shares** — buy exactly N shares.
- **Fixed dollars** — buy `floor(dollars / price)` shares (default).
- **% of equity** — buy `floor(NetLiq * pct% / price)` shares.

All capped by **Max $ / position** and **Max positions**.

## Safety notes
- Start on **paper** (port 4002) and leave it running for a few weeks before live.
- The **kill switch** sets `trading_enabled = false` instantly; new signals are
  rejected (existing positions are not auto-closed — use Manual → Flatten).
- One IBKR username = one API session. Give the bot its own login / dedicated
  client ID so it doesn't fight a manual TWS session.
- This is trading software you operate at your own risk; verify every behaviour
  on paper before risking capital. Not financial advice.

## Roadmap / known follow-ups
- You mentioned fixing the Pine exit gate yourself — the version here already
  removes the `isLong` dependency; adjust to taste.
- Consider adding: bracket/stop-loss orders, Telegram/email fill notifications,
  and a daily reconcile-on-startup log.
