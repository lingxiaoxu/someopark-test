"""Deep exit-timing + hybrid-strategy research over the real milestone price paths.

Answers, PIT, on settled matches with REAL recorded venue prices:
  1. For the VALUE picks and the ARGMAX picks, compare hold-to-FT vs:
       * every fixed-time exit (T15..T75)
       * CONDITIONAL lock: sell at the first milestone where our side ≥ L¢
       * stop-and-lock: sell if ≥ L¢ (take profit) OR ≤ S¢ (cut loss)
       * an ORACLE upper bound (sell at the path maximum — look-ahead, ceiling only)
  2. The HYBRID: when the decision model SKIPS (no edge), bet the argmax side instead,
     held-to-FT vs with a conditional lock — does it add or destroy money?

Everything is point-in-time for the PICK (model features cut at kickoff); the exit
rules use only prices AT or BEFORE the exit milestone (no look-ahead, except the
explicitly-labelled ORACLE bound).
"""
from __future__ import annotations

from dataclasses import replace

from prediction_market.config.config import CONFIG
from prediction_market.strategy.decision_model import SideQuote, decide

_SIDES = ("home", "draw", "away")
_MILES = ("PRE", "T15", "T30", "HT", "T60", "T75", "FT")
_MID = ("T15", "T30", "HT", "T60", "T75")          # exitable in-play milestones
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


def _bid_c(row, side):
    """Price we'd SELL our side at this milestone (hit the bid; fallback ask), ¢."""
    if row is None:
        return None
    for p in ("kalshi", "poly"):
        b = row[f"{p}_{side}_bid"]
        if b is not None:
            return b * 100.0
    for p in ("kalshi", "poly"):
        a = row[f"{p}_{side}_ask"]
        if a is not None:
            return a * 100.0
    return None


def _pnl_settle(entry_c, won):
    return (100.0 - entry_c) if won else -entry_c


def _pnl_exit(entry_c, exit_c):
    return exit_c - entry_c


def research():
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
    cfgm = CONFIG.model
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, round, kickoff_ts "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    ms = {}
    for r in conn.execute("SELECT * FROM milestone_snapshot"):
        ms[(r["fixture_api_id"], r["milestone"])] = r

    # collect per-bet records: {pick, entry_c, won, path:[(mile, sell_c)...], result}
    value_recs, argmax_recs, skip_argmax_recs = [], [], []
    for f in fixtures:
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        pre = ms.get((f["api_id"], "PRE"))
        if not (hi and ai) or pre is None:
            continue
        ko = is_knockout(f["round"])
        sm = build_strength_live(conn, prior, cfgm)
        if cfgm.oppadj_def_weight or cfgm.oppadj_off_weight:
            sm = replace(sm, adj=altdata_index(conn, sm.ratings, as_of=f["kickoff_ts"]))
        mp = price_match(sm, hi, ai, knockout=ko)
        p = apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=ko)
        model = {"home": p[0], "draw": p[1], "away": p[2]}
        result = "home" if f["home_goals"] > f["away_goals"] else ("draw" if f["home_goals"] == f["away_goals"] else "away")
        quotes = _quotes(pre)
        try:
            fi = form_index(conn, as_of=f["kickoff_ts"])
            form = {"home_z": fi[hi].form_z if hi in fi else None, "away_z": fi[ai].form_z if ai in fi else None}
        except Exception:
            form = None

        def rec_for(side):
            qq = quotes.get(side)
            if not qq or qq.ask is None:
                return None
            entry_c = qq.ask * 100.0
            path = [(mile, _bid_c(ms.get((f["api_id"], mile)), side)) for mile in _MID]
            return {"side": side, "entry_c": entry_c, "won": side == result,
                    "path": [(mi, c) for mi, c in path if c is not None]}

        # value pick
        d = decide(model, quotes, calib_confidence=0.25, form=form, gate_open=True)
        if d.side is not None and d.price_cents:
            r = rec_for(d.side)
            if r:
                value_recs.append(r)
        else:
            # skipped → record the argmax for the HYBRID experiment
            am = max(_SIDES, key=lambda s: model[s])
            r = rec_for(am)
            if r:
                skip_argmax_recs.append(r)
        # argmax pick (always, for the argmax exit study)
        am = max(_SIDES, key=lambda s: model[s])
        r = rec_for(am)
        if r:
            argmax_recs.append(r)
    return value_recs, argmax_recs, skip_argmax_recs


# ── exit-rule evaluators ──────────────────────────────────────────────────────
def hold_ft(recs):
    return sum(_pnl_settle(r["entry_c"], r["won"]) for r in recs)


def fixed_exit(recs, mile):
    tot = 0.0
    for r in recs:
        c = dict(r["path"]).get(mile)
        tot += _pnl_exit(r["entry_c"], c) if c is not None else _pnl_settle(r["entry_c"], r["won"])
    return tot


def cond_lock(recs, L):
    """Sell at the FIRST in-play milestone where our side ≥ L¢; else hold to FT."""
    tot = 0.0
    for r in recs:
        sold = None
        for mi, c in r["path"]:
            if c >= L:
                sold = c; break
        tot += _pnl_exit(r["entry_c"], sold) if sold is not None else _pnl_settle(r["entry_c"], r["won"])
    return tot


def stop_and_lock(recs, L, S):
    """Sell at first milestone with price ≥ L (take profit) OR ≤ S (cut loss); else FT."""
    tot = 0.0
    for r in recs:
        sold = None
        for mi, c in r["path"]:
            if c >= L or c <= S:
                sold = c; break
        tot += _pnl_exit(r["entry_c"], sold) if sold is not None else _pnl_settle(r["entry_c"], r["won"])
    return tot


def oracle_max(recs):
    """Look-ahead UPPER BOUND: sell at the path maximum (or settle if that's best)."""
    tot = 0.0
    for r in recs:
        best_exit = max([c for _, c in r["path"]], default=None)
        ft = _pnl_settle(r["entry_c"], r["won"])
        tot += max(ft, (best_exit - r["entry_c"]) if best_exit is not None else ft)
    return tot


def main():
    value, argmax, skip_am = research()
    def line(name, pnl, n):
        print(f"  {name:<28}{pnl:>8.2f}  ({pnl/n*100:+.1f}% avg ROI, n={n})")

    for label, recs in (("VALUE picks", value), ("ARGMAX picks", argmax)):
        print("=" * 60)
        print(f"{label}  (n={len(recs)})")
        print("=" * 60)
        line("hold-to-FT", hold_ft(recs), len(recs))
        for mi in _MID:
            line(f"fixed exit @ {mi}", fixed_exit(recs, mi), len(recs))
        for L in (70, 80, 85, 90, 95):
            line(f"cond lock ≥ {L}¢", cond_lock(recs, L), len(recs))
        for L, S in ((85, 10), (90, 8), (80, 15)):
            line(f"lock≥{L} / stop≤{S}", stop_and_lock(recs, L, S), len(recs))
        line("ORACLE max (look-ahead)", oracle_max(recs), len(recs))
        print()

    print("=" * 60)
    print(f"HYBRID: bet ARGMAX on the {len(skip_am)} SKIPPED (no-edge) matches")
    print("=" * 60)
    line("argmax hold-to-FT", hold_ft(skip_am), len(skip_am))
    for L in (80, 85, 90):
        line(f"argmax cond lock ≥ {L}¢", cond_lock(skip_am, L), len(skip_am))
    for L, S in ((85, 12), (90, 10)):
        line(f"argmax lock≥{L}/stop≤{S}", stop_and_lock(skip_am, L, S), len(skip_am))
    line("argmax ORACLE max", oracle_max(skip_am), len(skip_am))


if __name__ == "__main__":
    main()
