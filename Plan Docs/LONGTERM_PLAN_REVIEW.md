# Long-Term Portfolio Tracker — Ticket Review & Revised Implementation Plan

Reviewed: 2026-07-02 · Scope: Linear project *Long-Term Portfolio Tracker (Trading 212)*, ALG-1 … ALG-11, against the current AlgoFoundry codebase.

Overall the project is well-scoped: advisory-only Phase 1, rule-bounded AI, free-tier data, validation gate before trusting verdicts. The issues below are correctness and sequencing problems, not direction changes.

---

## 1. Problems found

### P1 — Missing ticket: instrument/symbol mapping (the biggest real-world gap)
Trading 212 returns its own tickers (`AAPL_US_EQ`, `VUAG_EQ`, LSE-listed instruments, fractional shares, GBP/EUR-denominated ISAs). yfinance wants `AAPL` / `VUAG.L`; **Finnhub free tier only covers US-listed symbols** for news and analyst data. Nothing in ALG-1/2/3 maps T212 instruments to data-provider symbols, currency, or instrument type. Without this, the pipeline breaks on the first non-US holding.

Related: **ETFs**. Long-term T212 portfolios typically hold ETFs — which have **no analyst ratings and no company news**. Two of the three scoring legs are undefined for them. Must be handled by design, not discovered in the paper run.

→ New ticket **ALG-12**: mapping layer (T212 ticker → yfinance/Finnhub symbol, currency, type equity/ETF) with a cached `longterm_instruments` table and a manual-override column for unmappable tickers.

### P2 — OpenRouter free-tier numbers in the tickets are wrong
ALG-4/ALG-11 assume "~20 req/min, 200 req/day". Verified today against OpenRouter docs: free-variant models are **20 req/min and 50 req/day** — 1000/day only after a one-time purchase of $10 in credits. Consequences:

- The ALG-11 eval harness (5+ models × 3–5 samples × retries) can burn a full day's quota in one session.
- Daily runs are fine at ~10–20 holdings, but retries + fallback + manual reruns will brush the cap.

Options: (a) batch several holdings per request, (b) one-time $10 top-up → 1000/day, (c) both. Cheap insurance either way.

### P3 — `OPENROUTER_API` key is *not* in `.env`
The project doc says it's already set. The actual `.env` contains only `ALGOFOUNDRY_*` vars. Small, but it's a stated precondition of ALG-11 and it's false — needs adding before any model eval.

### P4 — Rationale is generated before the verdict exists
Flow per the SOP: AI produces rationale + news score → *then* composite is computed → label assigned. The rationale can therefore contradict the final label (AI writes a bullish story, composite lands on SELL). Fix in ALG-4/ALG-5:

1. AI call returns **news/sentiment score + extracted key facts** (structured JSON only).
2. Composite + label computed deterministically.
3. Rationale rendered *after* labeling, bounded by the final label — template assembled from the leg summaries + AI key facts (no second LLM call needed).

This also cuts LLM usage roughly in half and makes rationale hallucination impossible by construction.

### P5 — No degraded-data policy
What happens when Finnhub has no analyst data (ETFs, small caps), yfinance rate-limits, or the LLM call fails? Currently unspecified anywhere. Proposal (goes in ALG-5):

- Missing leg → reweight remaining legs, mark verdict `partial_data` with which legs were present.
- LLM failure → verdict still produced from technical + analyst legs, rationale = template only.
- Total data failure for a symbol → verdict `NO_DATA`, surfaced in dashboard + WhatsApp, never silently skipped.

### P6 — `longterm_config` duplicates existing infrastructure
`app/db.py` already has a `settings` key-value table with defaults, casts, and the Basic-auth settings UI pattern. A parallel `longterm_config` table means two config stores, two UIs, two code paths. → Drop it; use `lt_`-prefixed keys in the existing `settings` table (`lt_t212_api_key`, `lt_weights_technical`, `lt_schedule_time`, …). ALG-8 shrinks to two tables.

### P7 — Dependency graph is over-serialized
Current chain forces ~9 sequential steps. False edges:

| Edge | Why it's false |
|---|---|
| ALG-8 → blocks → ALG-1 | T212 client needs an API key from settings (table already exists), not the new tables |
| ALG-1 → blocks → ALG-2, ALG-3 | Data pulls only need ticker symbols; develop/test against hardcoded symbols, integrate later |
| ALG-6 → blocks → ALG-7 | Notifier is a leaf; scheduler ships with it stubbed |
| ALG-5 → blocks → ALG-9 | Dashboard can be built against schema + seed rows |

True new edge: **ALG-12 blocks ALG-2 and ALG-3** (they consume mapped symbols). Fixing these cuts the critical path to 4 waves (§3).

### P8 — `pandas-ta` is a liability
Unmaintained upstream; known numpy≥2 breakage. Needed indicators (SMA 50/200 + slope, RSI-14, MACD, ATR, drawdown-from-52wk-high) are ~40 lines of plain pandas. → Drop the dependency, compute in `technicals.py` directly, unit-test against known values.

### P9 — Smaller fixes
- **Schema (ALG-8)**: `longterm_verdicts` should also store `price_at_verdict`, `model_used`, `prompt_version`, `raw_ai_response` (auditability + future accuracy measurement), with `UNIQUE(date, symbol)` for idempotent reruns.
- **Scheduler (ALG-7)**: APScheduler in-process is fine single-user, but document the single-worker constraint (`uvicorn --reload` / multi-worker double-fires jobs); persist `lt_last_run_date` for idempotency; T212 portfolio endpoint is rate-limited (~1 req/5s) — one call per run, no per-holding polling.
- **News window (ALG-3)**: fixed 24–48h lookback misses weekend/holiday news; look back to the previous successful run's timestamp instead.
- **yfinance (ALG-2)**: known to throttle/block; add retry with backoff + same-day on-disk cache (already hinted in ticket — make it an acceptance criterion).
- **T212 key scope (ALG-1)**: generate the key with portfolio-read scope only for Phase 1; least privilege now, execution scopes added in Phase 2.
- **Testing**: only ALG-5 mentions tests. Every ticket should carry acceptance criteria; pytest scaffolding lands in Wave 1 with ALG-8.

---

## 2. Ticket changes summary

| Ticket | Change |
|---|---|
| **ALG-12 (new)** | Instrument/symbol mapping + `longterm_instruments` table + ETF/instrument-type detection. Blocks ALG-2, ALG-3. |
| ALG-8 | Drop `longterm_config` (reuse `settings`); add verdict audit columns + `UNIQUE(date,symbol)`; add pytest scaffolding; no longer blocks ALG-1 |
| ALG-1 | Unblocked from ALG-8; add read-only key scope note; no longer blocks ALG-2/ALG-3 |
| ALG-2 | Replace pandas-ta with plain pandas; blocked by ALG-12 instead of ALG-1 |
| ALG-3 | Blocked by ALG-12; ETF/no-coverage handling; lookback-to-last-run window |
| ALG-4 | Correct rate limits (50/day free); JSON = news score + key facts only; rationale moves post-scoring; batching option |
| ALG-5 | Absorb degraded-data policy (P5) + rationale rendering (P4) |
| ALG-6 | No longer blocks ALG-7 |
| ALG-7 | Single-worker note, `lt_last_run_date`, T212 rate-limit note |
| ALG-9 | Unblocked from ALG-5 (build against seed data); add `partial_data`/`NO_DATA` display states |
| ALG-11 | Correct rate limits; budget the eval within 50 req/day (or after $10 top-up); precondition: add `OPENROUTER_API` to `.env` |

---

## 3. Revised build order

```
Wave 1 (parallel)   ALG-8  schema + pytest scaffold
                    ALG-12 symbol mapping            ← new
                    ALG-11 LLM eval  (after key added to .env)

Wave 2 (parallel)   ALG-1  T212 client
                    ALG-2  technicals (plain pandas)
                    ALG-3  analyst + news + earnings calendar
                    ALG-13 fundamentals leg (yfinance)   ← new
                    ALG-9  dashboard tab, seed data

Wave 3              ALG-4  AI synthesis (needs 2, 3, 11)
                    ALG-5  scoring + degraded policy + rationale rendering

Wave 4              ALG-7  orchestration + integration
                    ALG-6  WhatsApp notifier
                    ALG-9  wire dashboard to real data

Wave 5              ALG-10 paper-run validation (2–4 weeks)
```

Waves 1–2 are where the parallelism gain is; critical path is 8 → (12) → 2/3 → 4 → 5 → 7 → 10.

## 4. Decision-engine upgrades (second review pass, adopted)

The plain weighted-sum → bands design was upgraded in ALG-5, and a fourth scoring leg added:

- **ALG-13 (new): fundamentals leg** — P/E, revenue growth, margins, debt via yfinance (free, no new provider), -2..+2, refreshed weekly not daily. Composite is now 4 legs (-8..+8). Rationale: a long-term book had no valuation/quality signal at all, and this gives non-US equities a leg where Finnhub has no coverage.
- **Hysteresis** — labels only change when the composite crosses a band by a margin or holds the new zone ~2 days; kills BUY→HOLD→BUY flapping in the daily WhatsApp.
- **Asymmetric bands** — SELL band wider than BUY; buy-and-hold churns reluctantly.
- **Leg divergence** — tech +2 / analyst −2 must not average to a calm HOLD; high spread lowers confidence and auto-flags manual review.
- **Composite trend** — SELL requires low level *and* a declining 5–10 day composite slope; one bad day never triggers a SELL.
- **Veto rules (post-composite)** — earnings within N days freezes label changes (Finnhub earnings calendar added to ALG-3); M&A/regulatory headline → manual review; drawdown vs cost basis beyond `lt_max_drawdown_pct` → forced review.
- **Forward returns** — `fwd_return_7d/30d/90d` columns on verdicts (ALG-8), backfilled by a small job so ALG-10 measures hit rates per label instead of eyeballing.

Data-source verdict: yfinance + Finnhub is sufficient for Phase 1 once the fundamentals leg exists; more providers = more failure modes. FMP stays the Phase 3 upgrade path. OpenRouter $10 top-up done → 1000 free req/day.

## 5. Pipeline order per daily run (corrected)

```
T212 positions ──► symbol mapping ──► technicals (score)
                                  └─► analyst data (score)
                                  └─► news headlines ──► LLM: news score + key facts (JSON)
composite = w·(tech, analyst, news)  ──►  label (SELL/HOLD/BUY, bounded)
label + leg summaries + key facts    ──►  rationale (template, post-label)
persist verdict ──► dashboard ──► WhatsApp summary (best-effort)
```
