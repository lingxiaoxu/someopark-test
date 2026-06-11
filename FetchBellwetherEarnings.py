"""
FetchBellwetherEarnings.py — maintain the forward earnings calendar for the
semiconductor bellwethers (NVDA, AVGO).

The event-risk overlay's trigger #2 (bellwether contagion) needs to know, ahead
of time, WHEN NVDA / AVGO report — so it can watch the reaction day (D / D+1) for
a < -4.5% close.  The system's existing ``price_data/earnings_cache.json`` is
SEC-filing based (backward, lagged), so it is NOT usable for a *forward* trigger.

This script keeps a small forward calendar:
  - historical reaction dates: seeded from earnings_cache.json (2022+)
  - next scheduled date: yfinance ``Ticker(t).calendar['Earnings Date']`` (forward)

Output: price_data/macro/earnings_bellwether/bellwether_earnings.json

yfinance is unofficial and can break silently → on failure we ALERT loudly and
keep the last-good file (the trigger then falls back to price-only detection).

Usage:
    python FetchBellwetherEarnings.py            # refresh NVDA + AVGO
    python FetchBellwetherEarnings.py NVDA       # one ticker
"""

import os
import sys
import json
from datetime import date

BELLWETHERS = ["NVDA", "AVGO"]

_ROOT = os.path.dirname(os.path.abspath(__file__))
EARNINGS_CACHE = os.path.join(_ROOT, "price_data", "earnings_cache.json")
OUT_DIR = os.path.join(_ROOT, "price_data", "macro", "earnings_bellwether")
OUT_PATH = os.path.join(OUT_DIR, "bellwether_earnings.json")


def _alert(msg: str) -> None:
    """Loud failure alert (stderr + stdout) — mirrors UpdateMasterPerformance."""
    banner = "!" * 70
    for stream in (sys.stderr, sys.stdout):
        print(f"\n{banner}\n[BELLWETHER ALERT] {msg}\n{banner}", file=stream)


def _next_earnings_date(ticker: str):
    """Next scheduled earnings date via yfinance .calendar (forward), with the
    same randomized 1–3s rate-limit retry as the SMH-holdings fetch
    (EventRiskDetector.retry_yf). None when no date is scheduled; raises after
    all retries fail (caller keeps last-good)."""
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from EventRiskDetector import retry_yf

    def _pull():
        import yfinance as yf
        return yf.Ticker(ticker).calendar

    cal = retry_yf(_pull, f"{ticker} .calendar")
    ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not ed:
        return None
    d = ed[0] if isinstance(ed, list) else ed
    return str(d)


def _hist_reaction_dates(ticker: str, since: str = "2022-01-01"):
    """Historical reaction dates from earnings_cache (filing-based, for backtest seed)."""
    try:
        ec = json.load(open(EARNINGS_CACHE))["symbols"]
        return sorted({e["earnings_date"] for e in ec.get(ticker, [])
                       if e.get("earnings_date", "") >= since})
    except Exception:
        return []


def update_bellwether_calendar(tickers=None, quiet: bool = False) -> dict:
    """Refresh the bellwether forward calendar. Keeps last-good on yfinance failure."""
    tickers = tickers or BELLWETHERS
    os.makedirs(OUT_DIR, exist_ok=True)

    # Load existing (so a per-ticker failure keeps the last good entry).
    existing = {}
    if os.path.exists(OUT_PATH):
        try:
            existing = json.load(open(OUT_PATH))
        except Exception:
            existing = {}

    out = {
        "_source": "earnings_cache(history, filing-based) + yfinance .calendar(next, forward)",
        "_note": "reaction dates; trigger pairs with a price check (D/D+1 close < -4.5%)",
        "fetched_at": date.today().isoformat(),
    }
    failures = []
    for t in tickers:
        hist = _hist_reaction_dates(t)
        nxt = None
        try:
            nxt = _next_earnings_date(t)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{t}: {e!r}")
            # keep last-good next if available
            nxt = (existing.get(t) or {}).get("next_forward")
        prev = (existing.get(t) or {}).get("reaction_dates", [])
        all_dates = sorted(set(hist) | set(prev) | ({nxt} if nxt else set()))
        out[t] = {"reaction_dates": all_dates, "next_forward": nxt}
        if not quiet:
            print(f"  {t}: history {len(hist)} + next={nxt} -> {len(all_dates)} dates")

    if failures:
        _alert("yfinance .calendar failed for: " + "; ".join(failures)
               + " — kept last-good next dates; trigger #2 falls back to price-only.")

    json.dump(out, open(OUT_PATH, "w"), indent=2)
    status = "ok" if not failures else "partial"
    return {"status": status, "failures": failures, "path": OUT_PATH,
            "tickers": {t: out[t]["next_forward"] for t in tickers}}


if __name__ == "__main__":
    tks = sys.argv[1:] or None
    res = update_bellwether_calendar(tks)
    print(f"[BELLWETHER] {res['status']}: next = {res['tickers']}  -> {res['path']}")
