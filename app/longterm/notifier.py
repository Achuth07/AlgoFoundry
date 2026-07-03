"""WhatsApp notifier via CallMeBot (ALG-6).

Composes a single, compact WhatsApp summary for a whole long-term run and
delivers it through the free CallMeBot relay. Design constraints:

* One message per run — never one-per-holding — with an *alerts-first* layout
  so anything needing human attention (SELL, manual review / override,
  drawdown review, or missing data) is impossible to miss.
* Best-effort delivery: an unset phone/key is a no-op (logged ``info``), and an
  HTTP failure is logged ``error`` and swallowed. :func:`send_whatsapp` never
  raises out of the pipeline.
* The CallMeBot API key is NEVER written to a log event or an exception message.
"""

from __future__ import annotations

from urllib.parse import quote

from .. import db

try:  # pragma: no cover - import guard
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore

_CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"

# Keep well under WhatsApp's practical limit; CallMeBot truncates very long
# messages, so we truncate the per-holding section ourselves with a marker.
_MAX_CHARS = 3500

# Flags / labels that promote a holding into the alerts section.
_ALERT_FLAGS = {"manual_review", "drawdown_review"}


def _first_sentence(text: str | None) -> str:
    """Return the first sentence of a rationale (up to the first period)."""
    if not text:
        return ""
    text = str(text).strip()
    # Split on the first ". " so decimals like "+0.4" aren't cut.
    for sep in (". ", ".\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text


def _fmt_composite(value) -> str:
    try:
        return f"{float(value):+.1f}"
    except (TypeError, ValueError):
        return "n/a"


def _flags_of(v: dict) -> list[str]:
    raw = v.get("review_flags") or ""
    return [f.strip() for f in str(raw).split(",") if f.strip()]


def _alert_for(v: dict) -> str | None:
    """Return a short alert label if the verdict needs attention, else None."""
    label = (v.get("label") or "").upper()
    dq = (v.get("data_quality") or "").lower()
    flags = _flags_of(v)
    override = bool(v.get("override_flag"))

    if dq == "no_data":
        return "NO_DATA"
    if label == "SELL":
        return "SELL"
    if override or "manual_review" in flags:
        return "REVIEW"
    if "drawdown_review" in flags:
        return "DRAWDOWN"
    return None


def compose_summary(verdicts: list[dict], run_date) -> str:
    """Build ONE WhatsApp message summarising a whole run.

    Layout: a header with the date and BUY/HOLD/SELL counts; an *alerts*
    section first (SELL / review / drawdown / no-data); then compact
    per-holding lines. The per-holding section is truncated with an
    "…and N more" marker to stay under ~3500 chars.
    """
    verdicts = list(verdicts or [])
    counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
    for v in verdicts:
        label = (v.get("label") or "").upper()
        if label in counts:
            counts[label] += 1

    header = (
        f"AlgoFoundry LT — {run_date} — {len(verdicts)} holdings: "
        f"{counts['BUY']} BUY / {counts['HOLD']} HOLD / {counts['SELL']} SELL"
    )

    # ---- Alerts section (first) -----------------------------------------
    alert_lines: list[str] = []
    for v in verdicts:
        alert = _alert_for(v)
        if not alert:
            continue
        sym = v.get("symbol") or "?"
        reason = _first_sentence(v.get("rationale"))
        line = f"! {sym} {alert}"
        if reason:
            line += f" — {reason}"
        alert_lines.append(line)

    parts: list[str] = [header]
    if alert_lines:
        parts.append("")
        parts.append("ALERTS:")
        parts.extend(alert_lines)

    # ---- Per-holding compact lines --------------------------------------
    holding_lines: list[str] = []
    for v in verdicts:
        sym = v.get("symbol") or "?"
        label = (v.get("label") or "?").upper()
        comp = _fmt_composite(v.get("composite"))
        reason = _first_sentence(v.get("rationale"))
        line = f"{sym} {label} ({comp})"
        if reason:
            line += f" — {reason}"
        holding_lines.append(line)

    fixed = "\n".join(parts) + ("\n\nHOLDINGS:\n" if holding_lines else "")
    budget = _MAX_CHARS - len(fixed)

    kept: list[str] = []
    used = 0
    for i, line in enumerate(holding_lines):
        # Reserve room for a possible "…and N more" marker.
        remaining = len(holding_lines) - len(kept)
        marker = f"\n…and {remaining} more" if remaining > 1 else ""
        if used + len(line) + 1 + len(marker) > budget and kept:
            dropped = len(holding_lines) - len(kept)
            kept.append(f"…and {dropped} more")
            break
        kept.append(line)
        used += len(line) + 1

    message = fixed + "\n".join(kept)
    return message[:_MAX_CHARS]


def send_whatsapp(message: str) -> bool:
    """Send ``message`` via CallMeBot. Returns True on success, else False.

    An unset phone/key is not an error: we log an ``info`` event and return
    False. HTTP failures log an ``error`` event and return False. The API key
    is never logged. This function never raises.
    """
    phone = (db.get_setting("lt_callmebot_phone", "") or "").strip()
    key = (db.get_setting("lt_callmebot_key", "") or "").strip()

    if not phone or not key:
        db.log_event("info", action="notifier", status="skipped",
                     detail="notifier not configured (phone/key unset)")
        return False

    if requests is None:  # pragma: no cover
        db.log_event("error", action="notifier",
                     detail="requests library not installed")
        return False

    url = (
        f"{_CALLMEBOT_URL}?phone={quote(phone)}"
        f"&text={quote(message)}&apikey={quote(key)}"
    )
    try:
        resp = requests.get(url, timeout=20)
    except Exception as exc:  # transport failure; never includes the key
        db.log_event("error", action="notifier",
                     detail=f"whatsapp send failed: {exc}")
        return False

    if resp.status_code != 200:
        db.log_event("error", action="notifier", status="failed",
                     detail=f"whatsapp send HTTP {resp.status_code}")
        return False

    db.log_event("info", action="notifier", status="sent",
                 detail="whatsapp summary delivered")
    return True
