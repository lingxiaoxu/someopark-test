"""Season-market ¢ quotes per competition — club edition (TRANSFORM_PLAN §2.2).

Kalshi champion/top-N/relegation series come from the LEAGUE REGISTRY
(KXPREMIERLEAGUE-27, KXEPLTOP4, KXUCLTOP8 …). The three price-selection helpers
are kept VERBATIM from the WC module — they are the hard-won part:

  * ``_kalshi_champ_cents`` prefers last-traded (a no-hoper has no YES sellers,
    so ask=$1 is not its value);
  * ``_real_price`` handles thin CROSSED books (stale 1¢ ask under a 60¢ bid);
  * ``_reach_market_cents`` is SETTLEMENT-AWARE first (a finalized market is
    worth its result, not its stale last trade).

Polymarket side: per-comp season events exist on Poly Global (epl-2027-champion…);
slug resolution is a Phase-3b hook — ``poly_c`` stays None until wired.
"""
from __future__ import annotations

from prediction_market_soccer.config.leagues import active, get
from prediction_market_soccer.util.pricing import to_cents
from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _sub_team(m: dict) -> str:
    """Team name from yes_sub_title, defensively stripping "Reg Time: " (verbatim WC)."""
    sub = (m.get("yes_sub_title", "") or "").strip()
    if sub.lower().startswith("reg time:"):
        sub = sub.split(":", 1)[1].strip()
    return sub


def _real_price(ask, bid, last):
    """Sane executable mark for a thin contract (verbatim WC — crossed-book rule):
    well-formed book → ask; crossed/capped → live bid; else last trade; else None."""
    def _ok(x):
        return x is not None and 0.0 < x < 1.0
    a, b, l = _num(ask), _num(bid), _num(last)
    if _ok(a) and (b is None or a >= b):
        return a
    if _ok(b):
        return b
    if _ok(l):
        return l
    return None


def _market_cents(m: dict) -> float | None:
    """¢ mark for one per-team season market — SETTLEMENT-AWARE first (verbatim WC)."""
    status = str(m.get("status") or "").lower()
    if status in ("finalized", "settled", "determined"):
        res = str(m.get("result") or "").lower()
        if res == "yes":
            return 100.0
        if res == "no":
            return 0.0
        return None
    price = _real_price(m.get("yes_ask_dollars"), m.get("yes_bid_dollars"),
                        m.get("last_price_dollars"))
    return to_cents(price) if price is not None else None


def _champ_mark(m: dict) -> float | None:
    """Champion-market mark: prefer LAST-TRADED, then bid, then a real (<$1) ask
    (verbatim WC ``_kalshi_champ_cents`` preference order)."""
    ask = _num(m.get("yes_ask_dollars"))
    bid = _num(m.get("yes_bid_dollars"))
    last = _num(m.get("previous_price_dollars")) or _num(m.get("last_price_dollars"))
    price = last
    if price is None:
        price = bid if bid else (ask if (ask is not None and ask < 0.99) else None)
    return to_cents(price) if price is not None else None


def season_cents(comp_key: str, family: str, *, d: KalshiDiscovery | None = None) -> dict[str, float]:
    """{club_id: ¢} for one season family ('champion'/'top4'/'relegation'/'top8'/
    'ro16'/…) of one competition. Champion uses the last-traded preference;
    everything else the settlement-aware thin-book mark."""
    d = d or KalshiDiscovery(comp_key)
    series = d.comp.kalshi.get(family)
    if not series:
        return {}
    out: dict[str, float] = {}
    for ev in d.md.list_events(series, status="open"):
        for m in ev.get("markets", []):
            cid = d._resolve(_sub_team(m), scope="global")
            if not cid:
                continue
            c = _champ_mark(m) if family == "champion" else _market_cents(m)
            if c is not None:
                out[cid] = c
    return out


def champion_cents(comp_key: str) -> dict[str, dict]:
    """{club_id: {'kalshi_c': ¢, 'poly_c': None}} for one competition's title market."""
    kc = season_cents(comp_key, "champion")
    return {cid: {"kalshi_c": c, "poly_c": None} for cid, c in kc.items()}


def champion_cents_all() -> dict[str, dict[str, dict]]:
    """{comp_key: {club_id: {'kalshi_c','poly_c'}}} across every enabled comp.
    Failure-tolerant per comp — one venue hiccup never blanks the rest."""
    out: dict[str, dict] = {}
    for comp in active():
        try:
            cc = champion_cents(comp.key)
            if cc:
                out[comp.key] = cc
        except Exception as e:  # noqa: BLE001
            print(f"[champion_cents:{comp.key}] skipped ({type(e).__name__}: {e})")
    return out


def season_odds_cents(comp_key: str) -> dict[str, dict[str, float]]:
    """{family: {club_id: ¢}} for every season family the registry lists for this
    comp (champion/top4/relegation/last/top8/ro16/ro8/ro4/finalist) — the venue
    side of season_odds_export."""
    d = KalshiDiscovery(comp_key)
    fams = [f for f in ("champion", "top4", "relegation", "last", "top", "top8",
                        "ro16", "ro8", "ro4", "finalist") if d.comp.kalshi.get(f)]
    out: dict[str, dict[str, float]] = {}
    for f in fams:
        try:
            cents = season_cents(comp_key, f, d=d)
            if cents:
                out[f] = cents
        except Exception as e:  # noqa: BLE001
            print(f"[season_cents:{comp_key}:{f}] skipped ({type(e).__name__}: {e})")
    return out


if __name__ == "__main__":
    for k in ("epl", "ucl"):
        cc = champion_cents(k)
        top = sorted(cc.items(), key=lambda kv: -(kv[1]["kalshi_c"] or 0))[:6]
        print(f"— {k} champion ¢ (Kalshi): " +
              ", ".join(f"{c} {v['kalshi_c']:.0f}¢" for c, v in top))