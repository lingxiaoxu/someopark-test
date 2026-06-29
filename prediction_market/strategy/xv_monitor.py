"""Cross-venue price monitor (plan 13, 08) — live, READ-ONLY.

Pulls live order books from each venue, aligns them to the same real-world
entity (canonical team_id), and runs the cross-venue math in `cross_venue.py`
+ the relative-value edge in `edge.py`:
  * per-entity de-vigged probability on each tradable venue;
  * cross-venue spread between venues;
  * relative value vs our model `p_model` (where is a venue mispriced?);
  * locked arbitrage net_lock when ≥2 TRADABLE venues quote the same outcome.

Right now the cleanest live overlap is the **champion** market: Polymarket
Global lists all 48 teams in real time; we compare its de-vigged prices to our
model `p_champion`. Kalshi (also a champion venue, tradable) plugs in via the
same `QuoteSource` interface; Polymarket US has no outright champion market yet
(match/group only — documented extension in plan 13).

Nothing here can place an order: Global is geoblocked and `venue_guard` blocks
it; Kalshi/US reads need no order rights. This is a monitor, not an executor.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import canonical_team_name, load_prior, team_id
from prediction_market.model.strength import build_strength
from prediction_market.model.tournament import simulate
from prediction_market.strategy import devig as devig_mod
from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader

_WIN_RE = re.compile(r"will\s+(.+?)\s+win", re.IGNORECASE)


@dataclass(frozen=True)
class Quote:
    venue: str
    yes_bid: float | None
    yes_ask: float | None

    @property
    def consensus(self) -> float | None:
        """Best single-price read: mid if two-sided, else whichever side exists."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2.0
        return self.yes_ask if self.yes_ask is not None else self.yes_bid


# ── Quote sources (one per venue) ────────────────────────────────────────────
def _team_from_market(m: dict) -> str | None:
    """Extract a canonical team_id from a Global champion market."""
    raw = m.get("groupItemTitle") or ""
    if not raw:
        q = m.get("question") or ""
        match = _WIN_RE.search(q)
        raw = match.group(1) if match else ""
    if not raw:
        return None
    tid = team_id(canonical_team_name(raw))
    return tid


def global_champion_quotes(reader: PolymarketGlobalReader | None = None) -> dict[str, Quote]:
    """{team_id: Quote} for the Global `world-cup-winner` event (live)."""
    reader = reader or PolymarketGlobalReader()
    events = reader.search_events("world-cup", limit=300) or reader.search_events("world cup", limit=300)
    if not events:
        return {}
    ev = events[0]
    out: dict[str, Quote] = {}
    for m in ev.get("markets", []):
        tid = _team_from_market(m)
        if not tid:
            continue
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            toks = json.loads(toks)
        if not toks:
            continue
        try:
            ob = reader.get_book(toks[0])
        except Exception:
            continue
        out[tid] = Quote(
            venue="poly_global",
            yes_bid=float(ob.yes_bid) if ob.yes_bid is not None else None,
            yes_ask=float(ob.yes_ask) if ob.yes_ask is not None else None,
        )
    return out


def kalshi_champion_quotes() -> dict[str, Quote]:
    """{team_id: Quote} for the Kalshi `KXMENWORLDCUP-26` event (tradable venue)."""
    from prediction_market.venues.kalshi.discovery import KalshiDiscovery

    d = KalshiDiscovery()
    out: dict[str, Quote] = {}
    for ref in d.champion_markets():
        if not ref.real_entity_id:
            continue
        try:
            ob = d.orderbook(ref.ticker)
        except Exception:
            continue
        out[ref.real_entity_id] = Quote(
            venue="kalshi",
            yes_bid=float(ob.yes_bid) if ob.yes_bid is not None else None,
            yes_ask=float(ob.yes_ask) if ob.yes_ask is not None else None,
        )
    return out


def model_champion_probs(n_sims: int = 50_000, seed: int = 1) -> dict[str, float]:
    """Our model's p_champion per team (fair reference, not a market quote)."""
    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=n_sims, seed=seed)
    return res.p_champion


# ── De-vig + comparison ──────────────────────────────────────────────────────
def devig_event(quotes: dict[str, Quote], method: str = "shin") -> dict[str, float]:
    """De-vig a mutually-exclusive event's consensus prices into probabilities.

    Champion is 48-way exclusive → asks sum > 1; we normalise (Shin for the
    long-tail favourite-longshot bias). Teams with no quote are dropped.
    """
    ids = [tid for tid, q in quotes.items() if q.consensus and q.consensus > 0]
    if not ids:
        return {}
    prices = [quotes[t].consensus for t in ids]
    p = devig_mod.devig(prices, method=method)
    return {tid: float(p[i]) for i, tid in enumerate(ids)}


@dataclass
class XVRow:
    team_id: str
    name: str
    p_model: float
    p_global: float | None           # Global de-vigged (read-only reference)
    p_kalshi: float | None           # Kalshi de-vigged (TRADABLE venue)
    kalshi_ask: float | None         # raw tradable Kalshi YES ask
    rel_value_kalshi: float | None   # p_model - p_kalshi_devig (actionable signal)
    rel_value_global: float | None   # p_model - p_global_devig
    xv_spread: float | None          # |p_kalshi - p_global| cross-venue (dis)agreement


def compare_champion(*, n_sims: int = 50_000, devig_method: str = "shin",
                     include_kalshi: bool = True) -> list[XVRow]:
    prior = load_prior()
    name_of = {t.team_id: t.name for t in prior.teams}
    p_model = model_champion_probs(n_sims=n_sims)
    gq = global_champion_quotes()
    g_devig = devig_event(gq, method=devig_method)
    kq = kalshi_champion_quotes() if include_kalshi else {}
    k_devig = devig_event(kq, method=devig_method)

    rows: list[XVRow] = []
    for tid in p_model:
        pg = g_devig.get(tid)
        pk = k_devig.get(tid)
        kq_t = kq.get(tid)
        rows.append(XVRow(
            team_id=tid, name=name_of.get(tid, tid),
            p_model=round(p_model[tid], 5),
            p_global=round(pg, 5) if pg is not None else None,
            p_kalshi=round(pk, 5) if pk is not None else None,
            kalshi_ask=round(kq_t.yes_ask, 5) if kq_t and kq_t.yes_ask is not None else None,
            rel_value_kalshi=round(p_model[tid] - pk, 5) if pk is not None else None,
            rel_value_global=round(p_model[tid] - pg, 5) if pg is not None else None,
            xv_spread=round(abs(pk - pg), 5) if (pk is not None and pg is not None) else None,
        ))
    # Rank by the actionable signal (model vs the tradable Kalshi price), falling
    # back to the Global divergence where Kalshi is missing.
    rows.sort(key=lambda r: -(abs(r.rel_value_kalshi) if r.rel_value_kalshi is not None
                              else abs(r.rel_value_global) if r.rel_value_global is not None else -1))
    return rows


def us_match_quotes(slug: str) -> dict[str, float] | None:
    """Polymarket US single-match 3-way prices for a fwc-* event (None if no markets).

    US match markets populate near kickoff; until then events have 0 markets and
    this returns None. Plugs into the four-way comparison when live.
    """
    import os

    from polymarket_us import PolymarketUS
    try:
        c = PolymarketUS(key_id=os.environ["PMUS_KEY_ID"], secret_key=os.environ["PMUS_SECRET"])
        ev = c.events.retrieve_by_slug(slug)
    except Exception:
        return None
    out: dict[str, float] = {}
    for m in ev.get("markets", []):
        try:
            bbo = c.markets.bbo(m["slug"])
            ask = bbo.get("bestAsk") if isinstance(bbo, dict) else None
            if ask is not None:
                out[(m.get("groupItemTitle") or m.get("question") or m["slug"])] = float(ask)
        except Exception:
            continue
    return out or None


def compare_matches(*, limit: int = 8) -> list[dict]:
    """Single-match cross-venue: model W/D/L vs sharp bookmaker de-vig (+ US when live).

    Reads upcoming fixtures + ingested odds from the store. The bookmaker de-vig
    is the sharp reference (validates the model); US/Kalshi single-match plug in
    when their markets populate.
    """
    from prediction_market.ingest import store
    from prediction_market.model.match_pricing import is_knockout, price_match_calibrated
    from prediction_market.model.probability_calibration import load_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = store.init_db()
    prior = load_prior()
    name_of = {t.team_id: t.name for t in prior.teams}
    # Use the SAME live model the rest of the system uses (squad/form/FC/opponent-adj
    # blends + xG-form + post-hoc calibration) so the Divergence view's model 3-way
    # matches the Match-Pricing / upcoming card exactly — not the bare FIFA-only raw
    # model. xg_form=True mirrors upcoming_export.py (without it the two views diverge).
    sm = build_strength_live(conn, prior, xg_form=True)
    cal = load_calibration()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    # Venue advance-market discovery (knockout 2-way), for the "Advances" view's
    # market side. Failure-tolerant — a venue that can't load just yields no advance.
    kd = pd_ = None
    try:
        from prediction_market.venues.kalshi.discovery import KalshiDiscovery
        kd = KalshiDiscovery()
    except Exception:
        kd = None
    try:
        from prediction_market.venues.polymarket_us.discovery import PolymarketUSDiscovery
        pd_ = PolymarketUSDiscovery()
    except Exception:
        pd_ = None

    def _devig2(asks):
        if any(a is None for a in asks):
            return None
        from prediction_market.strategy.devig import devig
        p = devig(asks, method="multiplicative")
        return {"home": round(float(p[0]), 3), "away": round(float(p[1]), 3)}

    rows = conn.execute(
        "SELECT f.api_id, f.home_api_id, f.away_api_id, f.round, f.kickoff_ts, "
        "       AVG(o.p_home) bh, AVG(o.p_draw) bd, AVG(o.p_away) ba "
        "FROM fixture f JOIN match_odds o ON o.fixture_api_id = f.api_id "
        "WHERE f.status_short='NS' GROUP BY f.api_id ORDER BY f.kickoff_ts LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        ko = is_knockout(r["round"])
        # 3-way: price the 90' market identically to upcoming_export (knockout=False with
        # host_neutral=ko) so the Model-vs-Market and Today's-Prediction views agree.
        mp = price_match_calibrated(sm, hi, ai, knockout=False, host_neutral=ko, cal=cal)
        row = {
            "home": name_of.get(hi, hi), "away": name_of.get(ai, ai),
            "knockout": ko,
            "model": {"home": round(mp.p_home, 3), "draw": round(mp.p_draw, 3), "away": round(mp.p_away, 3)},
            "book_devig": {"home": round(r["bh"], 3), "draw": round(r["bd"], 3), "away": round(r["ba"], 3)},
            "edge_vs_book": {"home": round(mp.p_home - r["bh"], 3),
                             "draw": round(mp.p_draw - r["bd"], 3),
                             "away": round(mp.p_away - r["ba"], 3)},
            "us": None,  # populated by us_match_quotes when US match markets are live
            "advance": None,
        }
        # 2-way advance block (knockout only): model_advance computed IDENTICALLY to
        # upcoming_export (price_match knockout=True → p_home_advance), market = venue
        # advance de-vig (Kalshi preferred, else Poly US). So selecting "Advances" here
        # and in Today's-Prediction yields the same model number for the same match.
        if ko:
            mp_adv = price_match_calibrated(sm, hi, ai, knockout=True, cal=cal)
            pha = mp_adv.p_home_advance
            if pha is not None:
                model_adv = {"home": round(pha, 3), "away": round(1.0 - pha, 3)}
                ka = pa = None
                et_date = None
                try:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    et_date = (datetime.fromisoformat(r["kickoff_ts"])
                               .astimezone(ZoneInfo("America/New_York")).date().isoformat())
                except Exception:
                    et_date = None
                if kd is not None:
                    try:
                        ka = kd.advance_quotes(hi, ai)
                    except Exception:
                        ka = None
                if pd_ is not None and et_date:
                    try:
                        pa = pd_.advance_quotes(hi, ai, et_date)
                    except Exception:
                        pa = None
                k_dv = _devig2([(ka.get(s) or {}).get("ask") for s in ("home", "away")]) if ka else None
                p_dv = _devig2([(pa.get(s) or {}).get("ask") for s in ("home", "away")]) if pa else None
                market_adv = k_dv or p_dv   # prefer Kalshi's de-vig as the market reference
                row["advance"] = {
                    "model": model_adv,
                    "market_devig": market_adv,
                    "market_source": ("kalshi" if k_dv else ("poly_us" if p_dv else None)),
                    "edge_vs_market": ({s: round(model_adv[s] - market_adv[s], 3) for s in ("home", "away")}
                                       if market_adv else None),
                }
        out.append(row)
    return out


def write_report(rows: list[XVRow]) -> str:
    CONFIG.paths.ensure()
    payload = {
        "market": "champion",
        "venues": {"model": True, "poly_global": True, "kalshi": True, "poly_us": False},
        "note": "Kalshi = tradable; Global = read-only reference (geoblock). "
                "rel_value_kalshi = p_model - p_kalshi_devig is the actionable signal. "
                "xv_spread = |kalshi - global| (cross-venue agreement). Lock-arb needs two "
                "TRADABLE venues + settlement equivalence (US has no champion market yet).",
        "rows": [r.__dict__ for r in rows],
    }
    out = CONFIG.paths.output / "xv_champion.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Persist the cross-venue spreads to the store (plan 05 §2) — best-effort.
    try:
        from datetime import datetime, timezone

        from prediction_market.ingest import store
        conn = store.init_db()
        store.persist_xv_spread(conn, datetime.now(timezone.utc).isoformat(), "champion",
                                [r.__dict__ for r in rows])
    except Exception as e:
        print(f"[warn] xv_spread persistence skipped: {e}")
    return str(out)


if __name__ == "__main__":
    rows = compare_champion()
    print("Cross-venue CHAMPION: model vs Kalshi (tradable) vs Polymarket Global (ref), de-vigged")
    print(f"{'team':<15}{'p_model':>8}{'p_kalshi':>9}{'p_global':>9}{'rv_kalshi':>10}{'xv_spr':>8}")
    for r in rows[:12]:
        k = f"{r.p_kalshi:.3f}" if r.p_kalshi is not None else " n/a"
        g = f"{r.p_global:.3f}" if r.p_global is not None else " n/a"
        rv = f"{r.rel_value_kalshi:+.3f}" if r.rel_value_kalshi is not None else " n/a"
        xs = f"{r.xv_spread:.3f}" if r.xv_spread is not None else " n/a"
        print(f"{r.name:<15}{r.p_model:>8.3f}{k:>9}{g:>9}{rv:>10}{xs:>8}")
    print("\nwrote", write_report(rows))
