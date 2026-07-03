"""Market-data source layer for the long-term tracker.

Three concerns live here:

* :class:`LegResult` — the shared result envelope every scoring leg returns
  (technical / fundamental / analyst / news). It exists once here and is
  imported by the other leg modules.
* Price history (ALG-2) — :func:`fetch_ohlcv` pulls OHLCV bars from yfinance
  with retry/backoff and a same-day on-disk cache so reruns are network-free.
* Analyst + news + earnings (ALG-3) — thin Finnhub free-tier wrappers behind a
  single mockable ``_finnhub_get`` HTTP seam, plus their scoring rubric.

Design principle: a leg never fabricates a neutral ``0`` on failure. The
``status`` field always says *why* a score is or isn't present:

* ``ok``             — a real score was computed.
* ``not_applicable`` — the leg doesn't apply (e.g. analyst leg on an ETF).
* ``no_data``        — the leg applies but the data was missing / errored.
"""

from __future__ import annotations

import datetime as _dt
import os
import pickle
import time
from dataclasses import dataclass, field
from typing import Any

from .. import db

# Optional heavy deps. Imported lazily-tolerant so that importing this module
# (e.g. for LegResult) never hard-fails in an environment without them; the
# functions that need them raise a clear error only when actually called.
try:  # pragma: no cover - import guard
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore

try:  # pragma: no cover - import guard
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore

try:  # pragma: no cover - import guard
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


# ---------------------------------------------------------------------------
# Shared result envelope
# ---------------------------------------------------------------------------
@dataclass
class LegResult:
    """Structured outcome of one scoring leg.

    ``score`` is on a -2..+2 scale and is ``None`` whenever ``status`` is not
    ``ok``. ``summary`` carries a small dict of the salient signals for the AI
    synthesis step; ``detail`` is a human-readable note (also used to explain
    why a leg produced no score).
    """

    status: str  # 'ok' | 'not_applicable' | 'no_data'
    score: float | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


# ---------------------------------------------------------------------------
# ALG-2: price history (yfinance) with retry + same-day on-disk cache
# ---------------------------------------------------------------------------
_CACHE_DIR = os.path.join(".cache", "longterm")


def _cache_path(yf_symbol: str, period: str) -> str:
    today = _dt.date.today().isoformat()
    # Keep the key filesystem-safe.
    safe = "".join(c if c.isalnum() else "_" for c in f"{yf_symbol}_{period}")
    return os.path.join(_CACHE_DIR, f"{safe}_{today}.pkl")


def _read_cache(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except Exception:
        return None


def _write_cache(path: str, df) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(df, fh)
    except Exception:
        # Cache is best-effort; never let a cache write break a fetch.
        pass


def _download_ohlcv(yf_symbol: str, period: str):
    """Single yfinance download attempt. Isolated so tests can monkeypatch it."""
    if yf is None:  # pragma: no cover
        raise RuntimeError("yfinance is not installed")
    ticker = yf.Ticker(yf_symbol)
    df = ticker.history(period=period, auto_adjust=True)
    return df


def fetch_ohlcv(yf_symbol: str, period: str = "1y", *, retries: int = 3):
    """Fetch OHLCV bars for ``yf_symbol`` as a pandas DataFrame, or ``None``.

    * A same-day on-disk cache in ``.cache/longterm/`` keyed on symbol+period+
      date means repeated runs on the same day never hit the network.
    * Network fetches retry up to ``retries`` times with exponential backoff.

    Returns ``None`` if every attempt fails or the result is empty.
    """
    if not yf_symbol:
        return None

    path = _cache_path(yf_symbol, period)
    cached = _read_cache(path)
    if cached is not None and getattr(cached, "empty", True) is False:
        return cached

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            df = _download_ohlcv(yf_symbol, period)
            if df is not None and not df.empty:
                _write_cache(path, df)
                return df
            # Empty frame: treat as a soft failure worth one retry.
            last_err = ValueError("empty OHLCV frame")
        except Exception as exc:  # network / parsing / rate-limit
            last_err = exc
        if attempt < retries - 1:
            time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, ...

    db.log_event(
        "info",
        symbol=yf_symbol,
        status="no_data",
        detail=f"fetch_ohlcv failed after {retries} tries: {last_err}",
    )
    return None


# ---------------------------------------------------------------------------
# ALG-3: Finnhub free-tier — analyst, news, earnings
# ---------------------------------------------------------------------------
_FINNHUB_BASE = "https://finnhub.io/api/v1"

# Free tier allows 60 calls/min. We keep a tiny module-level throttle: at most
# one call per (60/60)=1.0s is overkill, so we target ~55/min to stay safe.
_MIN_INTERVAL = 60.0 / 55.0
_last_call_ts = 0.0


def _throttle() -> None:
    global _last_call_ts
    now = time.monotonic()
    wait = _MIN_INTERVAL - (now - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()


def _finnhub_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Single mockable HTTP seam for all Finnhub calls.

    Returns parsed JSON, or ``None`` on any error / missing key. The API token
    is injected here and is NEVER logged.
    """
    token = db.get_setting("lt_finnhub_key", "") or ""
    if not token:
        return None
    if requests is None:  # pragma: no cover
        return None

    q = dict(params or {})
    q["token"] = token
    _throttle()
    try:
        resp = requests.get(f"{_FINNHUB_BASE}{path}", params=q, timeout=15)
        if resp.status_code != 200:
            # Log without the token — reconstruct a safe URL description.
            db.log_event(
                "info",
                status="no_data",
                detail=f"finnhub {path} -> HTTP {resp.status_code}",
            )
            return None
        return resp.json()
    except Exception as exc:
        db.log_event("info", status="no_data", detail=f"finnhub {path} error: {exc}")
        return None


def _is_scoreable_symbol(finnhub_symbol: str | None, instrument_type: str) -> bool:
    """Analyst/news legs only apply to US equities with a Finnhub symbol."""
    return bool(finnhub_symbol) and instrument_type == "equity"


# ---- Analyst leg ----------------------------------------------------------
def fetch_analyst(finnhub_symbol: str) -> dict[str, Any] | None:
    """Fetch recommendation trends + price target for ``finnhub_symbol``.

    Returns ``{"recommendations": [...], "price_target": {...}}`` or ``None``.
    """
    recs = _finnhub_get("/stock/recommendation", {"symbol": finnhub_symbol})
    target = _finnhub_get("/stock/price-target", {"symbol": finnhub_symbol})
    if not recs and not target:
        return None
    return {"recommendations": recs or [], "price_target": target or {}}


def analyst_score(
    payload: dict[str, Any] | None,
    *,
    finnhub_symbol: str | None,
    instrument_type: str,
    current_price: float | None = None,
) -> LegResult:
    """Score analyst sentiment to -2..+2.

    Components:
      * net upgrades trend over the most recent 2 months (strongBuy/buy vs
        strongSell/sell shift),
      * consensus buy/sell ratio in the latest period,
      * price-target upside vs ``current_price``.
    """
    if not _is_scoreable_symbol(finnhub_symbol, instrument_type):
        return LegResult(
            status="not_applicable",
            detail="analyst leg applies only to US equities with a Finnhub symbol",
        )
    if not payload:
        return LegResult(status="no_data", detail="no analyst data returned")

    recs = payload.get("recommendations") or []
    target = payload.get("price_target") or {}
    if not recs and not target:
        return LegResult(status="no_data", detail="empty analyst payload")

    summary: dict[str, Any] = {}
    score = 0.0
    have_signal = False

    # Finnhub recommendation rows are newest-first, each with strongBuy/buy/
    # hold/sell/strongSell and a period (YYYY-MM-DD).
    if recs:
        have_signal = True
        latest = recs[0]
        sb = latest.get("strongBuy", 0) or 0
        b = latest.get("buy", 0) or 0
        h = latest.get("hold", 0) or 0
        s = latest.get("sell", 0) or 0
        ss = latest.get("strongSell", 0) or 0
        bullish = sb + b
        bearish = s + ss
        total = bullish + h + bearish
        ratio = (bullish - bearish) / total if total else 0.0
        summary["consensus_ratio"] = round(ratio, 3)
        summary["latest_counts"] = {
            "strongBuy": sb, "buy": b, "hold": h, "sell": s, "strongSell": ss,
        }
        # Consensus ratio contributes up to +/-1.
        score += max(-1.0, min(1.0, ratio * 1.5))

        # Net-upgrade trend: compare latest bullish-minus-bearish to prior.
        if len(recs) >= 2:
            prev = recs[1]
            prev_net = (
                (prev.get("strongBuy", 0) or 0) + (prev.get("buy", 0) or 0)
                - (prev.get("sell", 0) or 0) - (prev.get("strongSell", 0) or 0)
            )
            cur_net = bullish - bearish
            delta = cur_net - prev_net
            summary["net_upgrade_delta"] = delta
            if delta > 0:
                score += 0.5
            elif delta < 0:
                score -= 0.5

    # Price-target upside.
    tgt_mean = target.get("targetMean") if target else None
    if tgt_mean and current_price:
        have_signal = True
        try:
            upside = (float(tgt_mean) - float(current_price)) / float(current_price)
            summary["target_upside_pct"] = round(upside * 100, 2)
            if upside >= 0.20:
                score += 1.0
            elif upside >= 0.05:
                score += 0.5
            elif upside <= -0.10:
                score -= 1.0
            elif upside < 0:
                score -= 0.5
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if not have_signal:
        return LegResult(status="no_data", detail="no usable analyst signals")

    score = max(-2.0, min(2.0, score))
    return LegResult(status="ok", score=round(score, 3), summary=summary,
                     detail="analyst score from consensus, trend, and target upside")


# ---- News leg -------------------------------------------------------------
def fetch_news(
    finnhub_symbol: str | None,
    *,
    instrument_type: str = "equity",
    since_ts: float | None = None,
) -> LegResult:
    """Fetch recent company news headlines from Finnhub ``/company-news``.

    ``since_ts`` is a unix timestamp of the previous successful run; defaults
    to 72h back. On success the ``summary`` carries a list of raw headline
    dicts ``{headline, source, datetime, url}`` for the AI step (news isn't
    numerically scored here). Returns a :class:`LegResult`.
    """
    if not _is_scoreable_symbol(finnhub_symbol, instrument_type):
        return LegResult(
            status="not_applicable",
            detail="news leg applies only to US equities with a Finnhub symbol",
        )

    now = _dt.datetime.now(_dt.timezone.utc)
    if since_ts is None:
        start = now - _dt.timedelta(hours=72)
    else:
        start = _dt.datetime.fromtimestamp(since_ts, tz=_dt.timezone.utc)

    data = _finnhub_get(
        "/company-news",
        {
            "symbol": finnhub_symbol,
            "from": start.date().isoformat(),
            "to": now.date().isoformat(),
        },
    )
    if data is None:
        return LegResult(status="no_data", detail="company-news request failed")

    headlines = []
    for item in data or []:
        headlines.append(
            {
                "headline": item.get("headline"),
                "source": item.get("source"),
                "datetime": item.get("datetime"),
                "url": item.get("url"),
            }
        )
    return LegResult(
        status="ok",
        summary={"count": len(headlines), "headlines": headlines},
        detail=f"{len(headlines)} headlines since {start.date().isoformat()}",
    )


# ---- Earnings calendar ----------------------------------------------------
def fetch_earnings_calendar(
    finnhub_symbol: str | None,
    *,
    instrument_type: str = "equity",
    horizon_days: int = 14,
) -> _dt.date | None:
    """Return the next earnings date within ``horizon_days``, or ``None``.

    Feeds the earnings-freeze veto in later scoring. ETF / missing symbol
    simply returns ``None`` (no earnings for those).
    """
    if not _is_scoreable_symbol(finnhub_symbol, instrument_type):
        return None

    today = _dt.date.today()
    to = today + _dt.timedelta(days=horizon_days)
    data = _finnhub_get(
        "/calendar/earnings",
        {
            "symbol": finnhub_symbol,
            "from": today.isoformat(),
            "to": to.isoformat(),
        },
    )
    if not data:
        return None

    rows = data.get("earningsCalendar") if isinstance(data, dict) else None
    if not rows:
        return None

    dates: list[_dt.date] = []
    for row in rows:
        raw = row.get("date")
        if not raw:
            continue
        try:
            d = _dt.date.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if today <= d <= to:
            dates.append(d)
    return min(dates) if dates else None
