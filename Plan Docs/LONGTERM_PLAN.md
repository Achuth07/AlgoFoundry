AlgoFoundry — Long-Term Portfolio Tracker (Trading 212)
=========================================================

Linear project: https://linear.app/whatcyber/project/long-term-portfolio-tracker-trading-212-3a9020d12f85
(team: AlgoFoundry, issues ALG-1 through ALG-10)

## 1. What this is

A second, independent subproduct inside the AlgoFoundry app, sitting alongside the existing
TradingView → Interactive Brokers swing bridge. It manages **long-term, buy-and-hold positions**
held on a **separate broker — Trading 212** — so the long-term book never touches the swing book's
broker, risk gating, or kill switch.

Every day, for every long-term holding, the system will:

1. Pull the live position from Trading 212.
2. Gather technicals, analyst ratings, and recent news.
3. Run an AI synthesis pass against a defined SOP.
4. Output a **BUY / SELL / HOLD** verdict + rationale — shown in a new dashboard tab and pushed
   as a WhatsApp summary.

**Phase 1 is advisory only.** Nothing is auto-executed. You read the verdict and place any trade
yourself in the Trading 212 app. The architecture is laid out so Trading 212 order execution
(their API supports it in beta) can be bolted on later without a rework — mirroring the confirmation
step, position caps, and kill switch the swing bridge already has.

## 2. Confirmed decisions

| Decision | Choice | Why |
|---|---|---|
| Broker | Trading 212 Public API (beta) | Official docs at docs.trading212.com/api; separate demo/live environments; read positions today, order placement available in beta for later |
| Execution scope | Advisory now, phase-2 auto-execute | Lower risk to start; architecture leaves room to add execution |
| Data budget | Free tier only | yfinance (price/technicals) + Finnhub free tier (news, recommendation trends); FMP ($19/mo) noted as a Phase 3 upgrade if free analyst data proves too thin |
| AI research engine | OpenRouter, free models | DeepSeek R1 (free) as primary reasoning model, `openrouter/free` router as fallback; custom SOP prompt, own integration — not the bigdata.com skill |
| WhatsApp delivery | CallMeBot | Free, personal-use HTTP GET API; sends only to your own number — fits a single-user tool with zero ongoing cost |

## 3. SOP — daily scoring rubric

Every holding gets three independent leg scores, each on a **-2 to +2** scale, computed the same
way every day so results are comparable over time:

- **Technical score** — trend (price vs 50/200 SMA and their slope), momentum (RSI(14), MACD
  cross), volatility/drawdown-from-52wk-high. Computed with `pandas-ta` over `yfinance` OHLCV
  history.
- **Analyst score** — net recent upgrades/downgrades, consensus rating, and price-target
  upside/downside vs current price, from Finnhub's free recommendation-trend and price-target
  endpoints.
- **News/sentiment score** — classification of material news (earnings, guidance, M&A, regulatory,
  leadership changes) from Finnhub company-news headlines, judged by the AI step below.

**Composite score** = weighted sum of the three legs, normalized, mapped to threshold bands:

```
composite <= -threshold_sell   →  SELL
-threshold_sell < composite < threshold_buy  →  HOLD
composite >= threshold_buy     →  BUY (add)
```

Weights and thresholds live in config (`longterm_config`), not hardcoded — tune them after the
paper-run validation period (ALG-10).

**Where the AI fits in:** the OpenRouter model receives the technicals summary, analyst score, and
raw news headlines for one holding, and returns (a) a 2-4 sentence human-readable rationale, (b)
the news/sentiment score, (c) an "override candidate" flag if its qualitative read strongly
disagrees with the rule-based composite. The **final label is bounded by the rule-based score** —
the AI can flag disagreement for manual review, but can't unilaterally flip a call. This keeps the
system auditable and stops one persuasive headline from swinging a verdict on its own.

## 4. Architecture

Extends the existing FastAPI / Jinja2+HTMX / SQLite app with a new `app/longterm/` module:

```
app/longterm/
├── t212.py          # Trading 212 API client — auth, fetch positions, demo/live switch
├── data_sources.py  # yfinance OHLCV + Finnhub news/analyst pulls
├── technicals.py    # pandas-ta indicator computation → technical score
├── ai_research.py   # OpenRouter client + SOP prompt → rationale + news score + override flag
├── scoring.py        # composite scoring + BUY/SELL/HOLD decision bands
├── notifier.py       # CallMeBot WhatsApp summary sender
└── scheduler.py       # daily job orchestration (APScheduler)
```

Pipeline order: **T212 holdings → technicals → analyst/news → AI synthesis → composite scoring →
persist → WhatsApp send.**

**Database** (extends `app/db.py`, new tables only — existing swing tables untouched):

- `longterm_holdings_snapshot` — daily position snapshot (date, symbol, qty, avg price, current
  price, P&L).
- `longterm_verdicts` — full history of every daily call (date, symbol, all three leg scores,
  composite, label, rationale, override flag) — this is what lets you measure call accuracy over
  time.
- `longterm_config` — API keys (Trading 212, Finnhub, OpenRouter, CallMeBot), scoring
  weights/thresholds, schedule time.

**Dashboard**: new "Long-Term" tab, sibling to the existing Trading / Event Log / Settings tabs —
holdings table with today's verdict badge and expandable rationale, a settings sub-section for the
new API keys and weights, and a history view per symbol.

**Scheduler**: one daily APScheduler job in the existing FastAPI process, running after US market
close (configurable), plus a manual "Run now" button on the dashboard for testing. Idempotent per
day so a manual rerun doesn't double-send WhatsApp messages.

## 5. Roadmap

- **Phase 1** (this project, ALG-1 → ALG-9): advisory pipeline, dashboard tab, WhatsApp summary.
- **Phase 1.5** (ALG-10): 2-4 week paper-run validation — track every verdict, sanity-check a
  sample weekly, tune weights/thresholds, confirm WhatsApp isn't spammy. Document the accuracy/
  behavior bar that has to be hit before Phase 2 is even considered.
- **Phase 2**: Trading 212 order execution wired into the verdict engine, with a confirmation step,
  position caps, and a kill switch — mirroring the safeguards already built into the swing bridge.
- **Phase 3**: upgrade to Financial Modeling Prep (~$19/mo) for richer analyst grades and
  price-target consensus if the Finnhub free tier proves too sparse; build a call-accuracy
  tracking/backtesting view on top of `longterm_verdicts`.

## 6. Linear breakdown

| Issue | Title | Priority | Blocked by |
|---|---|---|---|
| ALG-8 | SQLite schema: long-term holdings, verdicts, config | High | — |
| ALG-1 | Trading 212 API client: auth + fetch long-term holdings | High | ALG-8 |
| ALG-2 | Market data + technical indicator pipeline | High | ALG-1 |
| ALG-3 | Analyst ratings + news ingestion (Finnhub free tier) | High | ALG-1 |
| ALG-11 | Evaluate & select OpenRouter free LLM model for SOP analysis | High | — |
| ALG-4 | AI research synthesis via OpenRouter (SOP prompt) | Urgent | ALG-2, ALG-3, ALG-11 |
| ALG-5 | Composite scoring + BUY/SELL/HOLD decision engine | Urgent | ALG-2, ALG-3, ALG-4 |
| ALG-6 | WhatsApp daily summary via CallMeBot | Medium | ALG-5 |
| ALG-9 | New "Long-Term" dashboard tab | High | ALG-8, ALG-5 |
| ALG-7 | Daily scheduler / job orchestration | High | ALG-1, ALG-2, ALG-3, ALG-4, ALG-5, ALG-6 |
| ALG-10 | Paper-run validation of the SOP before relying on it | Medium | ALG-7, ALG-9 |

### Build order (topological)

1. **ALG-8** — DB schema (foundation, nothing else can persist without it)
2. **ALG-11** — LLM model evaluation (independent, can run in parallel with ALG-8/ALG-1)
3. **ALG-1** — Trading 212 client (needs config storage from ALG-8)
4. **ALG-2 / ALG-3** — technicals and analyst/news legs (both need the holdings list from ALG-1, can run in parallel with each other)
5. **ALG-4** — AI synthesis (needs both data legs + the chosen model from ALG-11)
6. **ALG-5** — composite scoring/decision engine (needs all three leg scores)
7. **ALG-6 / ALG-9** — WhatsApp notifier and dashboard tab (both need verdicts from ALG-5; can run in parallel)
8. **ALG-7** — scheduler that wires the whole pipeline together (needs everything above except the dashboard)
9. **ALG-10** — paper-run validation (needs the scheduler running and the dashboard to review results)

`OPENROUTER_API` is already set in `.env` — ALG-11 and ALG-4 both reuse it, no new provisioning needed.

## 7. Open items / assumptions to sanity-check before building

- Trading 212's public API is in **beta** — confirm your account has API access enabled and check
  the current rate limits before relying on it for a daily job.
- Finnhub's free tier has request-rate limits; the daily job should batch/pace calls across your
  holdings rather than firing all requests at once.
- CallMeBot's free tier is personal-use only and sends to a single number (yours) — exactly what's
  needed here, but if you ever want the summary sent to someone else too, that requires a different
  approach (Twilio or Meta Cloud API).
- OpenRouter free models carry a shared rate limit (~20 req/min, 200 req/day) — fine for a
  once-daily run over a normal-sized portfolio, but worth monitoring if the holding list grows large.

## Sources

- [Trading 212 Public API](https://docs.trading212.com/api)
- [Trading 212 Public API Docs (Redoc)](https://t212public-api-docs.redoc.ly/)
- [CallMeBot — Free WhatsApp API](https://www.callmebot.com/blog/free-api-whatsapp-messages/)
- [WhatsApp Business API Pricing 2026](https://www.uptail.ai/blog/whatsapp-business-api-pricing-2026-what-it-costs-and-how-billing-works)
- [FMP: Best APIs for analyst revisions/ratings](https://site.financialmodelingprep.com/education/analyst/best-apis-for-tracking-analyst-revisions-upgrades-downgrades-and-rating-trends)
- [Free AI Models on OpenRouter](https://openrouter.ai/collections/free-models)
- [Free LLM API in 2026 — OpenRouter](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)
