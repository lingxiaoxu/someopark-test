"""Diagnose WHY the decision model bets draw so often, PIT over settled matches.

Reports, per match and in aggregate:
  * model draw prob vs de-vigged market draw prob (model over/under the market?)
  * model draw prob vs ACTUAL draw frequency (is the draw mass calibrated?)
  * how often decide() picks draw, and its win rate vs non-draw picks
  * net_edge by side (where the 'value' is coming from)
  * counterfactual: add a draw-specific extra theta → how many draws drop, and
    what happens to overall pick accuracy + draw/non-draw win rates.
"""
from __future__ import annotations

from dataclasses import replace

from prediction_market.config.config import CONFIG
from prediction_market.strategy.decision_model import SideQuote, decide
from prediction_market.strategy.edge import compute_edge

_SIDES = ("home", "draw", "away")
_FINISHED = ("FT", "AET", "PEN")


def _quotes(row):
    q = {}
    for s in _SIDES:
        asks = []
        if row[f"kalshi_{s}_ask"] is not None:
            asks.append((row[f"kalshi_{s}_ask"], "kalshi"))
        if row[f"poly_{s}_ask"] is not None:
            asks.append((row[f"poly_{s}_ask"], "poly_us"))
        if not asks:
            q[s] = SideQuote(); continue
        ask, ven = min(asks, key=lambda t: t[0])
        q[s] = SideQuote(ask=ask, devig=row[f"devig_{s}"], venue=ven)
    return q


def run(draw_extra_theta: float = 0.0):
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.altdata_adjust import altdata_index
    from prediction_market.model.form_strength import form_index
    from prediction_market.model.match_pricing import price_match, is_knockout
    from prediction_market.model.probability_calibration import load_calibration, apply_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = store.init_db()
    prior = load_prior()
    cal = load_calibration()
    cfg_model = CONFIG.model
    name = {t.team_id: t.name for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, round, kickoff_ts "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    ms = {}
    for r in conn.execute("SELECT * FROM milestone_snapshot WHERE milestone='PRE'"):
        ms[r["fixture_api_id"]] = r

    n = n_draw_actual = 0
    md_sum = dvd_sum = 0.0
    picks = {"draw": [], "other": []}
    rows = []
    for f in fixtures:
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        pre = ms.get(f["api_id"])
        if not (hi and ai) or pre is None:
            continue
        ko = is_knockout(f["round"])
        sm = build_strength_live(conn, prior, cfg_model)
        if cfg_model.oppadj_def_weight or cfg_model.oppadj_off_weight:
            sm = replace(sm, adj=altdata_index(conn, sm.ratings, as_of=f["kickoff_ts"]))
        mp = price_match(sm, hi, ai, knockout=ko)
        p = apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=ko)
        model = {"home": p[0], "draw": p[1], "away": p[2]}
        result = "home" if f["home_goals"] > f["away_goals"] else ("draw" if f["home_goals"] == f["away_goals"] else "away")
        n += 1
        n_draw_actual += result == "draw"
        md_sum += model["draw"]
        q = _quotes(pre)
        dvd = pre["devig_draw"]
        dvd_sum += dvd or 0.0

        # net_edge per side at baseline theta (+ optional draw extra theta)
        try:
            fi = form_index(conn, as_of=f["kickoff_ts"])
            form = {"home_z": fi[hi].form_z if hi in fi else None, "away_z": fi[ai].form_z if ai in fi else None}
        except Exception:
            form = None
        cfg_d = replace(CONFIG.decision, draw_extra_theta=draw_extra_theta) if hasattr(CONFIG.decision, "draw_extra_theta") else CONFIG.decision
        d = decide(model, q, cfg=cfg_d, calib_confidence=0.25, form=form, gate_open=True)
        edges = {}
        for s in _SIDES:
            if q[s].ask is not None:
                edges[s] = compute_edge(model[s], q[s].ask, k=CONFIG.risk.shrink_k,
                                        theta=CONFIG.risk.min_net_edge).net_edge
        pick = d.side
        if pick:
            (picks["draw"] if pick == "draw" else picks["other"]).append(pick == result)
        rows.append((f"{name.get(hi,hi)[:10]:10s} {f['home_goals']}-{f['away_goals']} {name.get(ai,ai)[:10]:10s}",
                     model, dvd, result, pick, edges))

    print(f"=== draw_extra_theta = {draw_extra_theta:.3f} ===")
    print(f"matches: {n}  actual draws: {n_draw_actual} ({n_draw_actual/n:.0%})")
    print(f"mean model P(draw): {md_sum/n:.3f}   mean de-vig market P(draw): {dvd_sum/n:.3f}   "
          f"(model − market = {(md_sum-dvd_sum)/n:+.3f})")
    nd, no = len(picks["draw"]), len(picks["other"])
    print(f"decide picks: DRAW {nd}/{nd+no} ({nd/(nd+no):.0%})  |  draw win-rate "
          f"{sum(picks['draw'])/nd:.0%}" if nd else "no draw picks", end="")
    print(f"   non-draw win-rate {sum(picks['other'])/no:.0%}" if no else "")
    # overall pick accuracy
    allp = picks["draw"] + picks["other"]
    print(f"overall pick win-rate: {sum(allp)/len(allp):.0%} over {len(allp)} bets")
    return rows


if __name__ == "__main__":
    import sys
    theta = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    rows = run(theta)
    print("\nper-match (model H/D/A | devigD | result | pick | edge H/D/A):")
    for label, model, dvd, result, pick, edges in rows:
        mk = lambda s: f"{model[s]:.2f}"
        ek = lambda s: f"{edges.get(s, float('nan')):+.2f}" if s in edges else "  -  "
        flag = " <DRAW" if pick == "draw" else ""
        print(f"  {label} | {mk('home')}/{mk('draw')}/{mk('away')} | dvD {dvd:.2f} | {result:4s} | "
              f"pick={pick or '-':4s} | e {ek('home')}/{ek('draw')}/{ek('away')}{flag}")
