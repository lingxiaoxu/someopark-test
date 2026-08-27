"""Model-aware (not price-only) exit timing on the per-minute price_tick paths.

The crude rules failed because they looked ONLY at the pick's own price. The Oracle
(+621¢ ≈ 3× hold) says the value is there — we just need a smarter SIGNAL. The idea:
at each minute compute the LIVE model fair value of the pick (current score + minutes
left, pre-match λ — the same double-Poisson the desk uses) and SELL when the MARKET
price overshoots that fair value by a margin (the market over-reacted → lock it), but
HOLD while the market still UNDER-prices us (the value hasn't been realised).

We also test: sell-on-overshoot only AFTER a favourable goal event; and a combined
'overshoot OR near-certain (≥95¢)' rule. PIT: pre-match λ from the as_of strength model;
score reconstructed from fixture_event; market from price_tick. No look-ahead (ex ORACLE).
"""
from __future__ import annotations

from prediction_market_soccer.config.config import CONFIG

_S = ("home", "draw", "away")
_FIN = ("FT", "AET", "PEN")


def _score_timeline(conn, fid, home_api, away_api):
    """sorted [(minute, home_goals, away_goals)] cumulative from fixture_event goals."""
    evs = conn.execute(
        "SELECT minute, team_api_id, detail FROM fixture_event WHERE fixture_api_id=? AND type='Goal' "
        "ORDER BY minute, seq", (fid,)).fetchall()
    tl = []
    gh = ga = 0
    for e in evs:
        if (e["detail"] or "") == "Missed Penalty":
            continue
        own = (e["detail"] or "") == "Own Goal"
        scorer_home = (e["team_api_id"] == home_api) ^ own   # own goal credits the other side
        if scorer_home:
            gh += 1
        else:
            ga += 1
        tl.append((e["minute"] or 0, gh, ga))
    return tl


def _score_at(tl, minute):
    gh = ga = 0
    for m, h, a in tl:
        if m <= minute:
            gh, ga = h, a
        else:
            break
    return gh, ga


def run():
    import json

    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.match_pricing import is_knockout
    from prediction_market_soccer.model.squad_strength import build_strength_live

    conn = store.init_db(); prior = load_prior()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rep = json.loads((CONFIG.paths.output / "performance_report.json").read_text())
    bl = [b for b in rep["bet_log"] if b.get("bet") and b.get("pick")]
    fxmap = {}
    for r in conn.execute("SELECT api_id, home_api_id, away_api_id, kickoff_ts, round FROM fixture"):
        fxmap[(cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"]))] = r

    recs = []
    for b in bl:
        r = fxmap.get((b.get("home_id"), b.get("away_id")))
        if not r:
            continue
        fid = r["api_id"]
        ticks = conn.execute(
            "SELECT rel_min, price FROM price_tick WHERE fixture_api_id=? AND side=? AND rel_min BETWEEN 1 AND 125 "
            "ORDER BY ts", (fid, b["pick"])).fetchall()
        if len(ticks) < 10:
            continue
        # Entry = the REAL price we paid (bet log entry_cents, cheapest venue) so hold-to-FT
        # reconciles with the reported PnL; exits use the Poly per-minute path.
        entry = b.get("entry_cents")
        if entry is None:
            pre = conn.execute("SELECT price FROM price_tick WHERE fixture_api_id=? AND side=? AND rel_min<=0 ORDER BY ts DESC LIMIT 1",
                               (fid, b["pick"])).fetchone()
            entry = (pre["price"] if pre else ticks[0]["price"]) * 100.0
        sm = build_strength_live(conn, prior, CONFIG.model, as_of=r["kickoff_ts"], xg_form=True)
        lam_h, lam_a = sm.pair_lambdas(b["home_id"], b["away_id"], knockout=is_knockout(r["round"]))
        tl = _score_timeline(conn, fid, r["home_api_id"], r["away_api_id"])
        path = []   # (minute, market_c, model_fair_c) for the picked side
        for t in ticks:
            mn = min(95, max(1, t["rel_min"]))
            gh, ga = _score_at(tl, mn)
            lp = live_match_prob(lam_h, lam_a, mn, gh, ga)
            fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[b["pick"]]
            path.append((t["rel_min"], t["price"] * 100.0, fair * 100.0))
        recs.append({"won": bool(b["won"]), "entry": entry, "path": path, "fid": fid,
                     "pick": b["pick"], "label": f"{b['home']} {b['score']} {b['away']}"})

    n = len(recs)

    def hold(r):
        return (100.0 - r["entry"]) if r["won"] else -r["entry"]

    def overshoot(r, margin, floor=0):
        """Sell when market ≥ model fair + margin (and market ≥ floor); else hold to FT."""
        for mn, mkt, fair in r["path"]:
            if mkt >= fair + margin and mkt >= floor:
                return mkt - r["entry"]
        return hold(r)

    def overshoot_or_lock(r, margin, lock):
        for mn, mkt, fair in r["path"]:
            if (mkt >= fair + margin) or (mkt >= lock):
                return mkt - r["entry"]
        return hold(r)

    print("=" * 66)
    print(f"MODEL-AWARE EXIT — {n} value picks (sell when market overshoots live model fair)")
    print("=" * 66)
    h = sum(hold(r) for r in recs)
    print(f"  {'hold-to-FT':<34}{h:>8.0f}¢  ({h/n:+.1f}¢/bet)")
    for m in (8, 12, 15, 20, 25, 30):
        v = sum(overshoot(r, m) for r in recs)
        print(f"  {'sell market ≥ fair + '+str(m)+'¢':<34}{v:>8.0f}¢  ({v/n:+.1f}¢/bet)")
    print("  -- overshoot OR near-certain lock --")
    for m, lk in ((15, 90), (20, 92), (12, 95)):
        v = sum(overshoot_or_lock(r, m, lk) for r in recs)
        print(f"  {'fair+'+str(m)+'  OR  ≥'+str(lk)+'¢':<34}{v:>8.0f}¢  ({v/n:+.1f}¢/bet)")
    # also: only sell overshoot in the LATE window (>=60') where reversion risk is real
    def overshoot_late(r, margin, from_min):
        for mn, mkt, fair in r["path"]:
            if mn >= from_min and mkt >= fair + margin:
                return mkt - r["entry"]
        return hold(r)
    print("  -- overshoot only late (reversion window) --")
    for m, fr in ((15, 60), (15, 70), (20, 75)):
        v = sum(overshoot_late(r, m, fr) for r in recs)
        print(f"  {'fair+'+str(m)+' from '+str(fr)+chr(39):<34}{v:>8.0f}¢  ({v/n:+.1f}¢/bet)")
    o = sum(max(hold(r), max((mkt for _, mkt, _ in r["path"]), default=0) - r["entry"]) for r in recs)
    print(f"  {'ORACLE (look-ahead max)':<34}{o:>8.0f}¢  ({o/n:+.1f}¢/bet)  <- ceiling")


if __name__ == "__main__":
    run()


def detail(margin=12):
    import json
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.match_pricing import is_knockout
    from prediction_market_soccer.model.squad_strength import build_strength_live
    conn = store.init_db(); prior = load_prior()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute("SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rep = json.loads((CONFIG.paths.output / "performance_report.json").read_text())
    bl = [b for b in rep["bet_log"] if b.get("bet") and b.get("pick")]
    fxmap = {}
    for r in conn.execute("SELECT api_id, home_api_id, away_api_id, kickoff_ts, round FROM fixture"):
        fxmap[(cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"]))] = r
    print(f"\n{'match':<34}{'pick':>5}{'entry':>6}{'won':>5}{'hold':>7}{'sold@':>7}{'smart':>7}")
    th=ts=0.0; nsold=saved=0
    for b in bl:
        r = fxmap.get((b.get("home_id"), b.get("away_id")))
        if not r: continue
        fid=r["api_id"]
        ticks=conn.execute("SELECT rel_min,price FROM price_tick WHERE fixture_api_id=? AND side=? AND rel_min BETWEEN 1 AND 125 ORDER BY ts",(fid,b["pick"])).fetchall()
        if len(ticks)<10: continue
        entry=b.get("entry_cents")
        sm=build_strength_live(conn,prior,CONFIG.model,as_of=r["kickoff_ts"],xg_form=True)
        lam_h,lam_a=sm.pair_lambdas(b["home_id"],b["away_id"],knockout=is_knockout(r["round"]))
        tl=_score_timeline(conn,fid,r["home_api_id"],r["away_api_id"])
        sold=None
        for t in ticks:
            mn=min(95,max(1,t["rel_min"])); gh,ga=_score_at(tl,mn)
            lp=live_match_prob(lam_h,lam_a,mn,gh,ga)
            fair={"home":lp.p_home,"draw":lp.p_draw,"away":lp.p_away}[b["pick"]]*100
            mkt=t["price"]*100
            if mkt>=fair+margin: sold=mkt; break
        won=bool(b["won"]); hold=(100-entry) if won else -entry
        smart=(sold-entry) if sold is not None else hold
        th+=hold; ts+=smart
        if sold is not None:
            nsold+=1
            if smart>hold: saved+=1
        print(f"  {(b['home']+' '+b['score']+' '+b['away'])[:32]:<32}{b['pick']:>5}{entry:>6.0f}{'Y' if won else 'N':>5}{hold:>+7.0f}{(sold if sold else 0):>7.0f}{smart:>+7.0f}")
    print(f"\n  hold {th:+.0f}¢  smart {ts:+.0f}¢  | sold {nsold}, of which improved {saved}")
