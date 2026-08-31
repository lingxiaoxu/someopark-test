"""Live in-play export for the frontend (the "In-Play Arbitrage" artifact data).

For EVERY currently-live fixture, emits the full intra-game view the desk needs:
  * live state-dependent model 3-way (minute + score + red cards + xG shading),
    fair draw, P(over 2.5), expected remaining goals;
  * the live score / minute / red cards / per-team xG;
  * ALL in-play opportunities for that match (from strategy/inplay_arb.find_opportunities):
    cross-venue lock-arb, relative value vs Kalshi/Poly US, and tactics
    (draw take-profit, convergence take-profit, xG momentum).

Writes data/output/inplay_live.json. Read-only (market data only; no orders).
Run per-minute during matches:  python -m prediction_market_soccer.ops.inplay_export --loop 30
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.venues.kalshi.market_data import KalshiMarketData as _KMD

_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")

# Match period from API-Football status. The 90' THREE-WAY market settles at the regulation
# whistle, so anything past regulation (extra time / penalties) means the 90' result is DECIDED.
_ET_STATUS = ("ET", "BT")          # extra time (BT = break before ET)
_PENS_STATUS = ("P", "PEN")        # penalty shootout in progress


def _period_for(status: str) -> str:
    """'reg' | 'et' | 'pens' from status_short. Mirrors inplay_export_advance._period_for so the
    two products agree on when regulation is over (kept local to keep the 3-way path standalone)."""
    if status in _PENS_STATUS:
        return "pens"
    if status in _ET_STATUS:
        return "et"
    return "reg"


def _match_hedge(conn, fx, pick_name, lam_h, lam_a, gh, ga, minute, lp, prices, *, knockout=False):
    """Hedge suggestion for a live match, or None when not applicable.

    Scenario (user's core case): a directional side (home/away) is NOW leading, so
    the draw contract has cheapened and buying draw is a protection leg. The held
    side is the CURRENT LEADER (not the pre-match favourite), so an upset in progress
    is covered too. We surface the break-even hedge (the headline) + a 3-state payoff
    matrix from strategy.inplay_hedge — the single source of the quant math (no
    re-derivation in the frontend). Read-only; reference size = 10 contracts.
    """
    from prediction_market_soccer.strategy import inplay_hedge as ih
    from prediction_market_soccer.util.pricing import to_cents

    if minute <= 0:
        return None
    # Hedge OUR pre-match DIRECTIONAL position — the side we actually recommended (value pick
    # vs the PRE de-vigged market), NOT just whoever is leading now. Shown REGARDLESS of the
    # current score (our pick leading / level / behind); the label notes the state. This keeps
    # the box tied to what we actually hold (e.g. we backed Morocco @79¢; even at 2-2 or with
    # Morocco trailing, the box shows our Morocco position + how to hedge it).
    from prediction_market_soccer.model.inplay import live_match_prob
    pm = live_match_prob(lam_h, lam_a, 0, 0, 0)  # pre-match probs (value pick + entry fallback)
    pre_row = conn.execute(
        "SELECT * FROM milestone_snapshot WHERE fixture_api_id=? AND milestone='PRE'",
        (fx["api_id"],)).fetchone()

    our_pick, entry_c = None, None
    if pre_row is not None:
        pm3 = {"home": pm.p_home, "draw": pm.p_draw, "away": pm.p_away}
        ask = {}
        for s in ("home", "draw", "away"):
            for v in ("poly", "kalshi"):
                try:
                    a = pre_row[f"{v}_{s}_ask"]
                except (KeyError, IndexError):
                    a = None
                if a is not None:
                    ask[s] = a
                    break
        tot = sum(ask.values())
        if tot:
            edges = {s: pm3[s] - ask[s] / tot for s in ask}  # model − de-vigged market
            cand = max(edges, key=edges.get)
            if edges[cand] > 0:                              # positive value → that's our bet
                our_pick = cand
                entry_c = to_cents(ask[cand])

    # Need a DIRECTIONAL position (home/away) to draw-hedge. No value bet, or a draw bet → none.
    if our_pick not in ("home", "away"):
        return None
    pick = our_pick
    if entry_c is None:
        entry_c = to_cents(pm.p_home if pick == "home" else pm.p_away)

    # Current state of OUR position relative to the score — drives the title/summary wording.
    if (pick == "home" and gh > ga) or (pick == "away" and ga > gh):
        lead_state = "leading"
    elif gh == ga:
        lead_state = "level"
    else:
        lead_state = "behind"

    # The whole three-sided book, not just the draw. Six hedge shapes are solved below
    # and three of them (partial cash-out, lay, dutching) need prices this function was
    # never handing over: it built Quotes(draw_ask=…) alone, leaving both other asks and
    # all three bids None, so those three could not price on ANY match — the "not
    # available right now" they reported was structural, not a property of the market.
    # One venue for all three sides: mixing venues would price a basket that cannot be
    # bought as one. Buying the hedge pays the ASK (the draw leg previously used the mid,
    # which understated what the protection costs).
    book: dict[str, dict] = {}
    for v in ("kalshi", "poly_us"):
        blk = (prices or {}).get(v) or {}
        sides = {s: blk.get(s) for s in ("home", "draw", "away")}
        if all(sides[s] and sides[s].get("ask_c") is not None for s in sides):
            book = sides
            break
    def _px(side: str, field: str):
        s = book.get(side) or {}
        return s.get(field)
    draw_c = _px("draw", "ask_c")
    if draw_c is None:                      # no full book — model fair for the draw leg
        blk = next((((prices or {}).get(v) or {}).get("draw") for v in ("kalshi", "poly_us")
                    if ((prices or {}).get(v) or {}).get("draw")), None)
        draw_c = (blk or {}).get("ask_c") or (blk or {}).get("mid_c") or to_cents(lp.p_draw)
    if entry_c is None or draw_c is None or draw_c >= 100.0:
        return None

    SHARES = 10.0
    pos = ih.Position(shares=SHARES, entry_c=float(entry_c), side=pick)
    quotes = ih.Quotes(draw_ask=float(draw_c), minute=minute, score=f"{gh}-{ga}",
                       home_ask=_px("home", "ask_c"), away_ask=_px("away", "ask_c"),
                       home_bid=_px("home", "bid_c"), draw_bid=_px("draw", "bid_c"),
                       away_bid=_px("away", "bid_c"))
    be = ih.break_even_b(pos, quotes, hedge_side="draw")
    if be.b is None:
        return None
    full = ih.full_hedge_b(pos, quotes, hedge_side="draw")
    bs = sorted({0.0, round(be.b, 2)} | ({round(full, 2)} if full is not None else set()))
    matrix = [r.as_dict() for r in ih.payoff_matrix(pos, quotes, "draw", bs=bs)]
    be_row = be.payoff.as_dict() if be.payoff else None
    return {
        "held_side": pick,
        "held_team": pick_name,
        "shares_ref": SHARES,            # payoff numbers are per this many contracts
        "entry_c": round(float(entry_c), 1),
        "draw_c": round(float(draw_c), 1),
        "break_even_b": round(be.b, 2),
        "full_hedge_b": (round(full, 2) if full is not None else None),
        "profit_if_win_c": (round(be_row[pick], 1) if be_row else None),  # held side still wins
        "payoff": matrix,                # rows: b=0 / break-even / full hedge
        "knockout": knockout,            # KO: a 90' draw → extra time (frontend adds the caveat)
        "lead_state": lead_state,        # leading / level / behind — our pick vs the score
        # The module solves six OTHER hedge shapes that never reached the desk: the
        # break-even and full hedge answer "how do I stop losing", while these answer
        # "what is the best worst case" (maximin), "how do I take part of it off"
        # (partial cash-out), "can I lock a guaranteed return across all three states"
        # (dutching) and "how do I synthesise a sell" (lay). Each is None when the
        # quotes on hand cannot support it, so an absent row means "not available now",
        # not "not implemented".
        "alternatives": _hedge_alternatives(ih, pos, quotes, pick),
        "note_key": "hedge.protectLeading",
    }


def _has_nonfinite(v) -> bool:
    """True if any float inside is NaN/inf (invalid JSON once serialised)."""
    import math
    if isinstance(v, float):
        return not math.isfinite(v)
    if isinstance(v, dict):
        return any(_has_nonfinite(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(_has_nonfinite(x) for x in v)
    return False


def _hedge_alternatives(ih, pos, quotes, pick) -> dict:
    """The hedge shapes beyond break-even/full, each guarded so one solver that cannot
    price with today's quotes never removes the others.

    Every result is serialised with ``dataclasses.asdict`` rather than a hand-written
    projection: the first version of this function guessed at ``.as_dict()`` and
    ``.worst`` accessors that do not exist, and because the guard swallowed the
    AttributeError, five of the six solvers silently returned nothing while the code
    read as if they were wired. The guard now records WHY a solver produced nothing,
    so a shape error can never masquerade as "not available right now" again.
    """
    import dataclasses

    out: dict = {}
    problems: dict = {}

    def _try(name, fn):
        try:
            r = fn()
        except Exception as e:   # noqa: BLE001 — one solver must not sink the others
            problems[name] = f"{type(e).__name__}: {e}"
            return
        if r is None:
            return
        v = dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r
        if _has_nonfinite(v):
            # dutch_lock with only one side quoted returns basket_c = NaN. Bare NaN is
            # not valid JSON — the browser's JSON.parse rejects the whole file, so a
            # single unpriceable solver would blank the entire in-play card.
            problems[name] = "not priceable with the quotes on hand (non-finite result)"
            return
        out[name] = v

    _try("maximin", lambda: ih.maximin_hedge(pos, quotes, hedge_side="draw"))
    _try("delta_neutral", lambda: ih.delta_neutral_b(pos, quotes, hedge_side="draw"))
    _try("draw_protection", lambda: ih.hedge_draw_protection(pos, quotes))
    _try("partial_cashout_half", lambda: ih.partial_cashout(pos, quotes, 0.5))
    _try("dutch_lock", lambda: ih.dutch_lock(quotes))
    _try("lay", lambda: ih.lay_hedge(pos, quotes))
    if problems:
        out["_unavailable"] = problems
    return out


def build(conn=None, *, with_venues: bool = True) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.squad_strength import build_strength_live
    from prediction_market_soccer.strategy.inplay_arb import find_opportunities

    conn = conn or store.init_db()
    name, zh = {}, {}
    for r in conn.execute("SELECT DISTINCT club_id, name, zh FROM club_registry"):
        name[r["club_id"]] = r["name"]
        zh[r["club_id"]] = r["zh"] or ""
    # Per-competition models behind a StrengthModel-shaped facade (club ratings are
    # per-league; the in-play consumers stay unchanged) — plan §2.2/§3.0.
    from prediction_market_soccer.config.leagues import active, by_api_id, caps_dict, caps_for, stage_of
    from prediction_market_soccer.ingest.soccer_ingest import leg_of
    from prediction_market_soccer.model.strength_cache import composite_live_strength
    sm = composite_live_strength(conn)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    _lids = tuple(c.api_football_id for c in active())
    live = conn.execute(
        "SELECT league_id, api_id, home_api_id, away_api_id, home_goals, away_goals, elapsed, "
        "status_short, round, raw_json "
        "FROM fixture WHERE status_short IN ({}) AND league_id IN ({}) "
        "AND kickoff_ts >= strftime('%Y-%m-%dT%H:%M:%S','now','-10 hours') "
        "ORDER BY elapsed DESC".format(",".join("?" * len(_LIVE)), ",".join("?" * len(_lids))),
        (*_LIVE, *_lids)).fetchall()

    # All opportunities once (grouped by fixture), with live venue quotes if available.
    quote_sources = {}
    if with_venues:
        try:
            from prediction_market_soccer.jobs.live_poller import _live_quote_sources
            quote_sources = _live_quote_sources(conn)
        except Exception:
            quote_sources = {}
    scan_error = None
    try:
        opps = find_opportunities(conn=conn, sm=sm, quote_sources=quote_sources)
    except Exception as e:   # noqa: BLE001 — a scanner crash must not kill the export
        # ...but it must not look like a quiet market either. An empty list with no
        # trace is indistinguishable from "nothing to trade", and that is how a crash
        # inside the scanner could run for days unnoticed.
        opps = []
        scan_error = f"{type(e).__name__}: {e}"
        import traceback
        print(f"[inplay] opportunity scan FAILED — no signals this cycle: {scan_error}")
        traceback.print_exc()
    # Per-contract ¢ on every opportunity (market/fair/edge → ¢), ADD ONLY.
    from prediction_market_soccer.util.pricing import to_cents, model_cents, quote_to_cents
    by_fixture: dict[int, list] = {}
    for o in opps:
        o = {**o, "market_c": to_cents(o.get("market")), "fair_c": to_cents(o.get("fair")),
             "edge_c": to_cents(o.get("edge"))}
        by_fixture.setdefault(o["fixture_id"], []).append(o)

    matches = []
    for fx in live:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            continue
        minute = fx["elapsed"] or 0
        # Stoppage / added time — DISPLAY ONLY. API-Football pins status.elapsed at 45/90 during
        # added time and carries the +N minutes in status.extra. That field is already persisted in
        # raw_json, so we surface it here without any schema/ingest change (purely to render "90+4'").
        stoppage = None
        try:
            _st = (json.loads(fx["raw_json"]).get("fixture") or {}).get("status") or {}
            _ex = _st.get("extra")
            stoppage = int(_ex) if _ex not in (None, 0) else None
        except Exception:
            stoppage = None
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
            "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fx["api_id"],))}
        reds = {r["team_api_id"]: r["n"] for r in conn.execute(
            "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
            "AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id", (fx["api_id"],))}
        rh, ra = reds.get(fx["home_api_id"], 0), reds.get(fx["away_api_id"], 0)
        # C1/§3.0: stage + caps from the registry. Club semantics: the HOME side keeps
        # its real home advantage on every fixture; only a neutral final drops it.
        comp = by_api_id(fx["league_id"])
        leg, agg = leg_of(conn, fx["api_id"])
        cp = caps_for(comp.key, fx["round"], leg=leg) if comp else None
        stage = stage_of(comp.key, fx["round"]) if comp else None
        knockout = bool(cp and cp.ko_draw_semantics)
        lam_h, lam_a = sm.pair_lambdas(hi, ai, neutral=bool(cp and cp.neutral))
        lp = live_match_prob(lam_h, lam_a, minute, gh, ga, red_home=rh, red_away=ra,
                             injury_time=float(stoppage or 0),   # ⑤ real added time extends the window
                             xg_home=xg.get(fx["home_api_id"]), xg_away=xg.get(fx["away_api_id"]))
        # Live venue quotes (¢) per side for this fixture, alongside model-implied ¢.
        prices = {"model_c": model_cents({"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away})}
        for v, fn in quote_sources.items():
            try:
                q = fn(fx["api_id"])
            except Exception:
                q = None
            if q:
                prices[v] = quote_to_cents(q)
        # Confidence tier (high/medium/low) on EVERY opportunity — the validated
        # effectiveness rules (plan 20-22), so the desk sees which signals to trust
        # without any signal being dropped. Read-only annotation.
        # `knockout` (computed above) also feeds the confidence/hedge layers: KO games run
        # lower-scoring with more 90' draws (→ extra time). See inplay_confidence.
        fixture_opps = by_fixture.get(fx["api_id"], [])
        try:
            from prediction_market_soccer.strategy import inplay_confidence as ic
            ctx = ic.match_context(hi, ai, name.get(hi, hi), name.get(ai, ai), gh, ga, minute,
                                   model={"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away},
                                   knockout=knockout)
            for _o in fixture_opps:
                ic.annotate(_o, ctx)
        except Exception:
            pass
        # Hedge suggestion (protect a leading directional position) — None when N/A.
        try:
            hedge = _match_hedge(conn, fx, None, lam_h, lam_a, gh, ga, minute, lp, prices,
                                 knockout=knockout)
            if hedge is not None:
                hedge["held_team"] = name.get(hi, hi) if hedge["held_side"] == "home" else name.get(ai, ai)
        except Exception:
            hedge = None
        # 90' market settled once the match passes regulation (extra time / penalties). In a
        # knockout the 3-way settles on 90', so once status flips to ET/BT/P the result is
        # DECIDED: no new entry, no event read, and not even a "lock the draw" exit makes sense
        # (the draw has already won — it pays $1, so "sell to lock against a late goal" is moot).
        # The market is fully LOCKED — drop every opportunity and the hedge; the UI shows a
        # "90' settled — regulation locked" note instead. The minute keeps climbing (shown as
        # ET / penalties). The live 2-way ADVANCE product stays active through ET+pens
        # (see inplay_export_advance).
        period = _period_for(fx["status_short"])
        if period != "reg":
            fixture_opps = []
            hedge = None
        # Live shootout tally (scored per side) for the UI header — same source as the advance
        # export; None outside pens. The 3-way market is settled here, but both tabs share the
        # header so the penalty score should read consistently whichever tab is open.
        shootout = None
        if period == "pens":
            sc = {"home": 0, "away": 0}
            for r in conn.execute(
                "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
                "AND comments='Penalty Shootout' AND detail='Penalty' GROUP BY team_api_id", (fx["api_id"],)):
                if r["team_api_id"] == fx["home_api_id"]:
                    sc["home"] = r["n"]
                elif r["team_api_id"] == fx["away_api_id"]:
                    sc["away"] = r["n"]
            shootout = sc
        matches.append({
            "fixture_id": fx["api_id"],
            "league": comp.key if comp else None,
            "league_zh": comp.zh if comp else "",
            "caps": caps_dict(cp, stage, agg=agg) if cp else None,
            "status": fx["status_short"],
            "period": period,
            "shootout": shootout,
            "minute": minute,
            "stoppage": stoppage,   # display-only: +N added minutes (None when not in stoppage)
            "score": f"{gh}-{ga}",
            "reds": f"{rh}-{ra}",
            "home": {"id": hi, "name": name.get(hi, hi), "zh": zh.get(hi, "")},
            "away": {"id": ai, "name": name.get(ai, ai), "zh": zh.get(ai, "")},
            "model": {
                "home": round(lp.p_home, 4), "draw": round(lp.p_draw, 4), "away": round(lp.p_away, 4),
                "over_2_5": round(float(lp.p_over_total.get(2.5, 0.0)), 4),
                "exp_remaining_goals": round(lp.exp_remaining_goals, 3),
            },
            "xg": {"home": xg.get(fx["home_api_id"]), "away": xg.get(fx["away_api_id"])},
            "prices": prices,
            "opportunities": fixture_opps,
            "hedge": hedge,
        })

    return {"ts": datetime.now(timezone.utc).isoformat(), "n_live": len(matches),
            # Present ONLY when the scanner crashed. Its absence is what lets a reader
            # trust that "no opportunities" means the market was quiet.
            **({"scan_error": scan_error} if scan_error else {}),
            # Series Kalshi refused this cycle. An empty board with no note reads as
            # "the market was quiet"; this is what distinguishes it from "we were not
            # allowed to look".
            **({"venue_blind": dict(_KMD.unavailable)} if _KMD.unavailable else {}),
            "matches": matches}


def graft_advance(doc: dict, adv_doc: dict | None) -> dict:
    """Attach each live tie's 2-way advance block to its 3-way row, in place.

    SoccerMatchCard switches the whole card to the advance product when the user picks
    that lens, and it needs `m.advance.model` to do it — but it only OFFERS the lens
    when `m.caps.advance` is set. The in-play export was setting caps.advance (a real
    two-leg tie does carry the market) while emitting no `advance` key at all, so in the
    live view the Advances toggle appeared and then changed nothing when clicked. The
    2-way numbers existed the whole time, in the SEPARATE inplay_live_advance.json that
    the frontend has no fetcher for.

    The block is shaped exactly like upcoming_export's, using upcoming_export's own
    helpers, so a tie prices identically before kickoff and during the match.
    """
    if not adv_doc or not adv_doc.get("matches"):
        return doc
    from prediction_market_soccer.ops.upcoming_export import (
        _best_buy_edge_2way, _venue_devig_2way)
    by_fid = {m["fixture_id"]: m for m in adv_doc["matches"]}
    for row in doc.get("matches", []):
        a = by_fid.get(row["fixture_id"])
        if a is None:
            continue
        model = {"home": a["model"]["home"], "away": a["model"]["away"]}
        prices = a.get("prices") or {}
        kalshi, poly = prices.get("kalshi"), prices.get("poly_us")
        theta = CONFIG.risk.min_net_edge          # same gate the 3-way rows use
        best = None
        for q, venue in ((kalshi, "kalshi"), (poly, "poly_us")):
            e = _best_buy_edge_2way(model, q, venue, theta)
            if e and (best is None or e["net_edge"] > best["net_edge"]):
                best = e
        row["advance"] = {
            "model": {**model, "cents": prices.get("model_c")},
            "kalshi": ({**kalshi, "devig": _venue_devig_2way(kalshi)} if kalshi else None),
            "poly_us": ({**poly, "devig": _venue_devig_2way(poly)} if poly else None),
            "edge": {"best": best} if best else None,
            # Timing/decider split is what the live 2-way card adds over the pre-match one.
            "legs": {k: a["model"][k] for k in
                     ("p_reg_decides", "p_et_decides", "p_pens_decides") if k in a["model"]},
            "opportunities": a.get("opportunities") or [],
            "hedge_advance": a.get("hedge_advance"),
        }
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(description="Export live in-play view for the frontend")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="re-export every N seconds (0 = once)")
    ap.add_argument("--no-venues", action="store_true", help="skip live venue quotes (model + tactics only)")
    args = ap.parse_args()

    def _go():
        doc = build(with_venues=not args.no_venues)
        CONFIG.paths.ensure()
        (CONFIG.paths.output / "inplay_live.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        n_opp = sum(len(m["opportunities"]) for m in doc["matches"])
        print(f"inplay_live.json: {doc['n_live']} live match(es), {n_opp} opportunities")
        for m in doc["matches"]:
            print(f"  {m['home']['name']} {m['score']} {m['away']['name']} @{m['minute']}{('+' + str(m['stoppage'])) if m.get('stoppage') else ''}'  "
                  f"model H{m['model']['home']:.2f}/D{m['model']['draw']:.2f}/A{m['model']['away']:.2f}  "
                  f"{len(m['opportunities'])} opps")
        return doc

    if args.loop:
        interval = max(args.loop, CONFIG.soccer.ttl_live)
        while True:
            try:
                doc = _go()
            except Exception as e:
                print(f"[warn] export failed: {e}")
                doc = {"n_live": 0}
            time.sleep(interval if doc["n_live"] else max(300, interval * 10))
    else:
        _go()


if __name__ == "__main__":
    main()
