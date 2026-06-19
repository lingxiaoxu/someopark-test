"""Double-bet / double-chance research (PIT, per-minute paths).

User idea: in matches where we're confident in BOTH the draw AND one side, back both
pre-match, and trade OUT of the draw once it spikes (e.g. after an equalizer). Effectively
'lay the third outcome' + dynamically lock the draw leg. Must fire RARELY (high conviction).

We test, on settled matches:
  * condition for 'confident in both': model P(fav side) ≥ A and P(draw) ≥ D and the
    THIRD outcome P(other) ≤ X (i.e. confident it's NOT the longshot) — favourite-longshot
    says that cheap third side is over-priced, so backing the other two is underpriced.
  * payoff of double-chance held to FT vs the single value bet
  * payoff of dynamically selling the DRAW leg when it reaches a lock level on the path
Strictly PIT model; real PRE prices for entry; per-minute Poly path for the draw exit.
"""
from __future__ import annotations

from dataclasses import replace

from prediction_market.config.config import CONFIG

_S = ("home", "draw", "away")
_FIN = ("FT", "AET", "PEN")


def _pre_quotes(row):
    out = {}
    for s in _S:
        ka, pa = row[f"kalshi_{s}_ask"], row[f"poly_{s}_ask"]
        cands = [x for x in (ka, pa) if x is not None]
        out[s] = min(cands) * 100.0 if cands else None
    return out


def _draw_path(conn, fid):
    return [(r["rel_min"], r["price"] * 100.0) for r in conn.execute(
        "SELECT rel_min, price FROM price_tick WHERE fixture_api_id=? AND side='draw' ORDER BY ts", (fid,))]


def run(A=0.40, D=0.27, X=0.22, draw_lock=70):
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import is_knockout, price_match_calibrated
    from prediction_market.model.probability_calibration import load_calibration
    from prediction_market.model.squad_strength import build_strength_live

    conn = store.init_db(); prior = load_prior(); cal = load_calibration()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    name = {t.team_id: t.name for t in prior.teams}
    pre = {r["fixture_api_id"]: r for r in conn.execute("SELECT * FROM milestone_snapshot WHERE milestone='PRE'")}
    fx = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FIN))), _FIN).fetchall()

    cands = []
    for f in fx:
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        pr = pre.get(f["api_id"])
        if not (hi and ai) or pr is None:
            continue
        ko = is_knockout(f["round"])
        sm = build_strength_live(conn, prior, CONFIG.model, as_of=f["kickoff_ts"], xg_form=True)
        mp = price_match_calibrated(sm, hi, ai, knockout=ko, cal=cal)
        model = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
        result = "home" if f["home_goals"] > f["away_goals"] else ("draw" if f["home_goals"] == f["away_goals"] else "away")
        q = _pre_quotes(pr)
        # the non-draw side we're confident in = the higher of home/away model prob
        fav = "home" if model["home"] >= model["away"] else "away"
        other = "away" if fav == "home" else "home"
        # condition: confident in BOTH fav and draw, and the THIRD (other) is unlikely
        if not (model[fav] >= A and model["draw"] >= D and model[other] <= X):
            continue
        if q[fav] is None or q["draw"] is None:
            continue
        cands.append({"fid": f["api_id"], "fav": fav, "other": other, "result": result,
                      "q": q, "model": model,
                      "label": f"{name.get(hi,hi)} {f['home_goals']}-{f['away_goals']} {name.get(ai,ai)}"})

    print("=" * 72)
    print(f"DOUBLE-BET candidates: P(fav)≥{A}, P(draw)≥{D}, P(third)≤{X}  →  {len(cands)} matches")
    print("=" * 72)
    dc_hold = dc_dyn = single = 0.0
    for c in cands:
        q, res, fav = c["q"], c["result"], c["fav"]
        ef, ed = q[fav], q["draw"]
        # single value bet (back the cheaper-edge side; here just the fav for comparison)
        single += (100.0 - ef) if res == fav else -ef
        # double-chance held to FT: $1 on fav + $1 on draw (cost ef+ed cents per pair, but
        # we account each contract independently at $1 stake → pays 100 if its side hits)
        fav_pnl = (100.0 - ef) if res == fav else -ef
        draw_pnl_hold = (100.0 - ed) if res == "draw" else -ed
        dc_hold += fav_pnl + draw_pnl_hold
        # dynamic: sell the DRAW leg if its path reaches draw_lock; else settle
        path = [(m, p) for m, p in _draw_path(conn, c["fid"]) if 1 <= m <= 125]
        sold = next((p for m, p in path if p >= draw_lock), None)
        draw_pnl_dyn = (sold - ed) if sold is not None else draw_pnl_hold
        dc_dyn += fav_pnl + draw_pnl_dyn
        tag = f"sold draw@{sold:.0f}" if sold is not None else f"draw settle {'WON' if res=='draw' else 'lost'}"
        print(f"  {c['label'][:34]:34} fav={fav}({ef:.0f}¢) draw({ed:.0f}¢) res={res:4} | {tag}")
    n = len(cands)
    if n:
        print(f"\n  single-bet (fav only)          PnL {single:+.0f}¢  ({single/n:+.1f}¢/match)")
        print(f"  double-chance hold-to-FT       PnL {dc_hold:+.0f}¢  ({dc_hold/n:+.1f}¢/match, 2 contracts)")
        print(f"  double-chance + sell draw≥{draw_lock}¢  PnL {dc_dyn:+.0f}¢  ({dc_dyn/n:+.1f}¢/match)")
    return cands


if __name__ == "__main__":
    run()
