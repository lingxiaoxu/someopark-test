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
    """Champion-market mark: the last trade, CLAMPED INTO the live book.

    Two corrections to the inherited World Cup order. First, `previous_price_dollars`
    is the previous CLOSE and `last_price_dollars` is the actual last trade; taking the
    former first published a stale print (Fluminense 18¢ while the tape said 19¢).

    Second, a season-champion contract is thin and its last trade can be days old, so on
    its own it is not a price anyone can get. Clamping it into [bid, ask] is what makes
    the mark executable, and it is also the elimination guard this board was missing:
    a club knocked out in the round of 16 still shows an old 5¢ print while its book has
    collapsed to bid 0 / ask 1¢, and the clamp reports the 1¢ the market will actually
    pay rather than the memory of a trade made when it was still alive.

    A settled market is read from its result, not its book — the same discipline
    ``_market_cents`` already applies.
    """
    status = str(m.get("status") or "").lower()
    if status in ("finalized", "settled", "determined"):
        res = str(m.get("result") or "").lower()
        if res == "yes":
            return 100.0
        if res == "no":
            return 0.0
        return None
    ask = _num(m.get("yes_ask_dollars"))
    bid = _num(m.get("yes_bid_dollars"))
    last = _num(m.get("last_price_dollars")) or _num(m.get("previous_price_dollars"))
    # A well-formed book (bid <= ask, ask a real price rather than the $1 placeholder)
    # bounds the mark; a crossed or absent book leaves the trade to speak for itself.
    book_ok = (bid is not None and ask is not None and ask < 0.99 and bid <= ask)
    if last is not None:
        price = min(max(last, bid), ask) if book_ok else last
    elif book_ok:
        price = ask
    else:
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

_NAME_SUFFIXES = {"jr", "jnr", "junior", "sr", "snr", "ii", "iii", "iv",
                  "filho", "neto", "segundo"}


def _norm_person(name: str) -> str:
    """Surname key for matching a market subject to one of our players.

    Kalshi writes the display name its own way ("E. Haaland", "Erling Haaland",
    "Haaland"), so the join is on the LAST token, accent-folded — the same discipline
    fc_ingest uses for the FIFA-series roster join. Deliberately not fuzzy: a wrong
    player attached to a price is worse than no price (the WC iron rule — live paths
    match exactly, fuzzy is bootstrap-only).
    """
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    toks = [t for t in s.replace(".", " ").split() if len(t) > 1]
    # Drop generational suffixes before taking the last token: they are extremely
    # common in Brazilian and Argentine squads, and without this "Vinicius Jr" keys
    # on "jr" — which would collide every Junior in the competition onto one price.
    while len(toks) > 1 and toks[-1].lower().strip(".") in _NAME_SUFFIXES:
        toks.pop()
    return toks[-1].lower() if toks else ""


def topscorer_cents(comp_key: str) -> dict[str, float]:
    """{surname key: ¢} for a competition's SEASON top-scorer market.

    Kalshi carries these as their own series (KXUEFACLTOPGOAL for the Champions League);
    the registry has always listed the ticker while nothing fetched it, so the top-scorer
    board shipped as a pure model view with no market column. Returns {} when the
    competition has no such series registered OR when Kalshi has not opened it yet —
    which, measured 2026-08-27, is every one of them: all seven candidate series
    returned zero open events with the season barely underway. The column is therefore
    expected to be empty until they list, and lights up on its own when they do.
    """
    d = KalshiDiscovery(comp_key)
    series = d.comp.kalshi.get("topscorer")
    if not series:
        return {}
    out: dict[str, float] = {}
    for ev in d.md.list_events(series, status="open"):
        for m in ev.get("markets", []):
            key = _norm_person(_sub_team(m))
            c = _market_cents(m)
            if key and c is not None:
                out[key] = c
    return out
