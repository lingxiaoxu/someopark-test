"""research/param_wf.py — walk-forward parameter selection, scored on outcomes.

The piece that connects `param_space` (how wide may the grid be) to `dsr` (did the winner
earn it): it produces the per-event score matrix both of them need, and replays selection
forward one day at a time.

**Three objectives, because the obvious one is the wrong one.**
The first version of this module scored candidates on `brier` — the mean squared error over
every leg of the ladder. That answers "do these parameters forecast better". It is not what
the strategy is paid for: the hybrid touches exactly ONE leg per event (the structure
`decide()` opens, else the favourite the argmax leg buys), while a CPI event has 10-20 legs.
A candidate can sharpen the far tails, win on mean Brier, and never move a bet.

    brier   mean per-leg Brier over every settled leg. Widest sample (581 events back to
            2022-12-23, no market price needed) and the least decision-relevant.
    banded  the same, restricted to legs whose MODEL fair lands in [0.10, 0.90] — the
            price window both streams can actually trade (`decide`'s `min_leg_price`
            floor, and the argmax leg's own band). Same 581-event sample, market-free,
            but it stops rewarding accuracy on legs no one may buy.
    pnl     realised dollars of the live hybrid rule, via `pnl_score`. The real objective.
            Needs a stored candle, which caps the sample at 63 events across all 14 series
            and at most 11 for any one of them, against `dsr.MIN_OBS` = 12.

`pnl` is the target and `banded` is its powered proxy; the proxy is only licensed by
measuring their agreement on the events where both exist, never by assuming it. All three
are LOSSES — lower is better — so `pnl` is carried as negative dollars and the argmin and
the paired DSR statistic work unchanged across all of them.

**Why this does not reuse `param_grid._release_universe`.**
That builder requires a stored market candle for every leg, because it scores the model and
the market side by side. Reasonable for the adoption gate; fatal for selection. Market
candles only exist from 2026-05-22, so it admits 61 events across all 14 series — and
`run_grid` has in fact never returned a result on this db, it returns
`{"n": 10, "error": "not enough scored releases (need 16)"}`. But choosing parameters needs
only the model's prediction and the realised outcome. Dropping the market requirement takes
the usable history from 61 events to 581, back to 2022-12-23. The market price is still
required to DECIDE whether to ship a winner — that gate is unchanged and lives elsewhere.

**Why the matrix is built once and then masked, rather than refitted each day.**
An event's score is evaluated at its own `close - 1h`, so it does not depend on the day the
simulation has reached. Selection at day D is then just a mask: average over the columns
whose event closed strictly before D. This is both ~60x cheaper and strictly PIT — day D
cannot see an event that settles at D or later, and the score of an event that settled in
2024 is the same number whether it is read on 6/1 or 7/31.

**Where the lookahead risk actually is, and what is done about it.**
Not in the scores — in the GRID. Which parameters are live, and how many sets the data can
support, are decided by `param_space.build_grid`, which reads events to probe. If that read
included the evaluation window it would be choosing the search space with knowledge of the
window. So the grid is built once from events closing strictly before `window_start` and
then frozen for the whole replay. Freezing also makes the three arms comparable: they
differ only in HOW they pick from the grid, never in what is in it.
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.model.common import Categorical, grid_pmf, leg_fair
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.research import dsr as _dsr
from prediction_market_macro.research.param_space import (build_grid, settled_events)
from prediction_market_macro.util.periods import kalshi_period_to_key

MODULE_OF = {"KXJOBLESSCLAIMS": "claims", "KXCPI": "cpi", "KXCPIYOY": "cpi",
             "KXCPICORE": "cpi", "KXCPICOREYOY": "cpi", "KXPCECORE": "pce",
             "KXPAYROLLS": "payrolls", "KXU3": "u3", "KXFED": "fed",
             "KXFEDDECISION": "fed", "KXWTIW": "energy", "KXNATGASW": "energy",
             "KXAAAGASW": "energy", "KXGDP": "gdp"}


def _predict_fn(series: str):
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])

    def call(conn, asof, key, series=series, params=None):
        return fn(conn, asof, key, series=series, params=params)
    return call


def event_legs(conn, series: str, tok: str) -> list[dict]:
    """Settled legs for one event. `suffix` is the ticker tail Categorical series key on."""
    out = []
    for l in conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, tok)).fetchall():
        out.append({"ticker": l["ticker"], "strike": l["floor_strike"],
                    "cap_strike": l["cap_strike"],
                    "strike_type": l["strike_type"] or "greater_or_equal",
                    "result": l["result"],
                    "suffix": l["ticker"].rsplit("-", 1)[-1]})
    return out


# The price window both live streams can trade: `decision.GATES['min_leg_price']` is the
# 0.10 penny-lottery floor, and `_place_argmax` requires 0.10 <= cost <= 0.90. Legs whose
# model fair sits outside it are never bought, so accuracy on them is not worth anything.
BAND_LO, BAND_HI = 0.10, 0.90


def _leg_fairs(pred, series: str, legs: list[dict]) -> list[float | None]:
    """Model probability per leg — the same object the market quotes, for both shapes."""
    if isinstance(pred.dist, Categorical):
        probs = dict(pred.dist.probs)
        return [probs.get(l["suffix"]) for l in legs]
    pmf = grid_pmf(pred.dist, REGISTRY[series].round_rule)
    return [leg_fair(pmf, l["strike_type"], l["strike"], l["cap_strike"])
            if l["strike"] is not None else None for l in legs]


def brier(pred, series: str, legs: list[dict], band: bool = False) -> float | None:
    """Mean per-leg Brier of the model against the realised outcome.

    Leg-averaged rather than distribution-wide so that Categorical and ladder series land
    on the same scale — every series is scored on the same object the market quotes, which
    is what makes a cross-series comparison mean anything.

    `band=True` keeps only the legs whose model fair lands in the tradeable price window,
    which is the powered stand-in for the PnL objective. The unbanded form averages over
    all 10-20 legs of a ladder while the strategy touches exactly one, so a candidate can
    win it by sharpening tails nobody may buy. Returns None when no leg is in band —
    the model claimed nothing tradeable, which is an absence of evidence about the
    parameters, not a score of zero.
    """
    if not legs:
        return None
    fairs = _leg_fairs(pred, series, legs)
    tot = n = 0.0
    for f, l in zip(fairs, legs):
        if f is None:
            continue
        if band and not (BAND_LO <= float(f) <= BAND_HI):
            continue
        tot += (float(f) - (1.0 if l["result"] == "yes" else 0.0)) ** 2
        n += 1
    return tot / n if n else None


def scored_universe(conn, series: str, before: datetime | None = None) -> list[dict]:
    """[{tok, key, asof, close, legs}] oldest first — settled, no market price required."""
    rows = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct", (series,)).fetchall()
    out = []
    for r in rows:
        key = kalshi_period_to_key(r["period"])
        if not key or not r["ct"]:
            continue
        close = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
        if before is not None and close >= before:
            continue
        legs = event_legs(conn, series, r["period"])
        if legs:
            out.append({"tok": r["period"], "key": key, "close": close,
                        "asof": close - timedelta(hours=1), "legs": legs})
    return out


def score_matrix(conn, series: str, grid: list[dict], universe: list[dict],
                 log=None, band: bool = False) -> tuple[list[dict], list[list[float]]]:
    """(kept_events, [[brier per set] per kept event]).

    An event is kept only when EVERY set in the grid scores it. Partial rows would let a
    set be compared against a different event sample than its rivals, which is exactly the
    bias the paired test in `dsr` exists to remove.
    """
    fn = _predict_fn(series)
    kept, mat = [], []
    for ev in universe:
        row = []
        for p in grid:
            try:
                pred = fn(conn, ev["asof"], ev["key"], params=(p or None))
                b = brier(pred, series, ev["legs"], band=band)
            except Exception:                                    # noqa: BLE001
                b = None
            if b is None:
                row = None
                break
            row.append(b)
        if row is None:
            continue
        kept.append(ev)
        mat.append(row)
    if log:
        what = "banded-scored" if band else "scored"
        log(f"  {series}: {what} {len(kept)}/{len(universe)} events x {len(grid)} sets")
    return kept, mat


OBJECTIVES = ("brier", "banded", "pnl")


def build_matrix(conn, series: str, grid: list[dict], objective: str,
                 before: datetime, log=None) -> tuple[list[dict], list[list[float]]]:
    """Dispatch to the right universe and loss. Every row is a LOSS — lower is better.

    `pnl` is carried as NEGATIVE dollars so that the argmin arm, the paired difference in
    `dsr`, and the aggregation in `run` all work without a per-objective sign branch. A
    sign branch is exactly the kind of thing that gets one case right and the other
    backwards, and a backwards PnL selection would look like a working search that
    reliably picks the worst set.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}, want one of {OBJECTIVES}")
    if objective == "pnl":
        from prediction_market_macro.research import pnl_score
        uni = [{**e, "key": kalshi_period_to_key(e["tok"]), "close": e["close_ts"]}
               for e in pnl_score.quotable_events(conn, series, before=before)]
        uni = [e for e in uni if e["key"]]
        kept, mat, _det = pnl_score.score_matrix(conn, series, grid, uni, log=log)
        return kept, [[-v for v in row] for row in mat]
    uni = scored_universe(conn, series, before=before)
    return score_matrix(conn, series, grid, uni, log=log, band=(objective == "banded"))


# ── the three arms ───────────────────────────────────────────────────────────────

def pick_default(*_a, **_k) -> tuple[int, dict]:
    """Arm 1 — ship what is registered. The control everything is measured against."""
    return 0, {"mode": "default"}


def pick_argmin(cols: dict, default_key: str, **_k) -> tuple[int, dict]:
    """Arm 2 — trailing argmin, the selection rule `param_grid.run_grid` uses today.

    No deflation of any kind. Included so the cost of the deflation is measurable rather
    than asserted: this arm is what the system would do if #112 had not been written.

    The one thing it does share with the DSR arm is that it cannot pick on zero history —
    otherwise the first OOS event would be decided by `min()` over empty lists. That is a
    definitional floor, not a deflation, so it does not soften what this arm is measuring.
    """
    if not cols[default_key]:
        return default_key, {"mode": "argmin", "chosen": default_key,
                             "reason": "no trailing history yet"}
    best = min(cols, key=lambda k: sum(cols[k]) / len(cols[k]))
    return best, {"mode": "argmin", "chosen": best}


def pick_dsr(cols: dict, default_key: str, adopt_p: float = _dsr.ADOPT_P,
             **_k) -> tuple[int, dict]:
    """Arm 3 — argmin, then made to justify itself against the width of the search."""
    rep = _dsr.select(cols, default_key, adopt_p=adopt_p)
    return rep["chosen"], rep


ARMS = {"default": pick_default, "argmin": pick_argmin, "dsr": pick_dsr}


def _replay_arms(kept: list[dict], mat: list[list[float]], n_sets: int,
                 window_start: datetime, adopt_p: float, objective: str) -> dict | None:
    """Run the three arms forward over the events that close inside the window.

    At each event the arms choose using ONLY the rows strictly above it, then are scored on
    that event's row. The chosen set never sees the event it is scored on, in any arm. The
    caller is responsible for handing over `kept`/`mat` sorted oldest first — everything
    here reads `range(i)` as "the past" and a mis-sorted matrix would leak silently.
    """
    oos = [i for i, e in enumerate(kept) if e["close"] >= window_start]
    if not oos:
        return None
    out = {"n_train_max": len(kept) - len(oos), "n_oos": len(oos),
           "arms": {}, "events": []}
    per_arm_scores = {a: [] for a in ARMS}
    per_arm_picks = {a: [] for a in ARMS}
    for i in oos:
        cols = {j: [mat[t][j] for t in range(i)] for j in range(n_sets)}
        ev_row = {"period": kept[i]["key"], "close": kept[i]["close"].isoformat(),
                  "n_train": i}
        if kept[i].get("series"):
            ev_row["series"] = kept[i]["series"]
        for arm, fnpick in ARMS.items():
            j, rep = fnpick(cols, 0, adopt_p=adopt_p) if arm != "default" else (0, {})
            per_arm_scores[arm].append(mat[i][j])
            per_arm_picks[arm].append(j)
            ev_row[arm] = {"set": j, "loss": round(mat[i][j], 6)}
            if arm == "dsr" and rep.get("adopted"):
                ev_row[arm]["dsr_p"] = rep.get("dsr_p")
        out["events"].append(ev_row)
    for arm in ARMS:
        s = per_arm_scores[arm]
        picks = per_arm_picks[arm]
        out["arms"][arm] = {
            # `loss` is the objective's own unit: Brier for brier/banded, NEGATIVE
            # dollars for pnl. `pnl` is spelled out alongside so a reader of the JSON
            # never has to remember the sign convention to know who won.
            "loss": round(sum(s) / len(s), 6),
            "pnl": round(-sum(s), 4) if objective == "pnl" else None,
            "n_moved": sum(1 for p in picks if p != 0),
            "distinct_sets": len(set(picks)),
        }
    return out


def replay(conn, series: str, window_start: datetime, window_end: datetime,
           adopt_p: float = _dsr.ADOPT_P, log=None,
           objective: str = "brier") -> dict | None:
    """Replay all three arms over the OOS window for one series."""
    module = MODULE_OF[series]
    fn = _predict_fn(series)
    # ── grid frozen on pre-window data only (see module docstring) ──
    pre_n = len(settled_events(conn, series, before=window_start))
    grid, report = build_grid(conn, series, module, fn, pre_n, log=log)
    if len(grid) <= 1:
        if log:
            log(f"  {series}: no grid ({report.get('reason', 'width 1')}) — skipped")
        return None
    # DEFAULT_IX: the empty dict is the registered production model, and build_grid never
    # emits it, so it is prepended. Everything is differenced against index 0.
    grid = [{}] + grid
    kept, mat = build_matrix(conn, series, grid, objective, window_end, log=log)
    res = _replay_arms(kept, mat, len(grid), window_start, adopt_p, objective)
    if res is None:
        if log:
            log(f"  {series}: no events inside the window — skipped")
        return None
    return {"series": series, "module": module, "objective": objective,
            "n_sets": len(grid), "grid_report": report, **res}


# ── branch pools ─────────────────────────────────────────────────────────────────
# On the `pnl` objective the sample is capped by candle retention at 63 events, and no
# single series reaches `dsr.MIN_OBS` = 12 (the best are 11). Pooling is the only way to
# clear the floor today, and only one kind of pooling is legitimate.
#
# NOT by module. `param_space` documents that liveness is per-SERIES because two modules
# hide two branches: cpi's `gas_*`/`food_drift` are dead for the core series, and energy's
# `fut_*` reach WTIW/NATGAS while `aaa_*` reach AAAGAS — never both. Pooling across a
# branch boundary would difference against parameters that cannot move half the sample,
# which dilutes the paired edge toward zero rather than strengthening it.
#
# Within a branch it is sound: identical parameters, identical code path, identical
# $1-scale payoff, and the events stay independent observations of the same contrast. The
# pairing removes event difficulty; what is left is cross-series heterogeneity, which
# inflates sd(d) and therefore makes the test MORE conservative, never less.
#
# `probe` is the series whose events decide liveness and whose predict fn `build_grid`
# perturbs. It is the branch's high-count member, so the probe cannot fail for want of
# runnable events. The size cap is computed on the POOLED history, because that is the
# history the selection will actually run on.
POOLS = {
    "energy_fut": {"module": "energy", "probe": "KXWTIW",
                   "series": ["KXWTIW", "KXNATGASW"]},
}


def replay_pool(conn, pool: str, window_start: datetime, window_end: datetime,
                adopt_p: float = _dsr.ADOPT_P, log=None,
                objective: str = "brier") -> dict | None:
    """Replay the three arms over a branch pool — one grid, several series, one sample.

    Every constituent series is scored against the SAME grid and the rows are merged in
    close-time order, so the trailing window an arm sees at event i is "everything in the
    branch that had already settled", which is exactly what a daily selector would have.
    """
    spec = POOLS[pool]
    module, members = spec["module"], spec["series"]
    pre_n = sum(len(settled_events(conn, s, before=window_start)) for s in members)
    grid, report = build_grid(conn, spec["probe"], module,
                              _predict_fn(spec["probe"]), pre_n, log=log)
    if len(grid) <= 1:
        if log:
            log(f"  [{pool}]: no grid ({report.get('reason', 'width 1')}) — skipped")
        return None
    grid = [{}] + grid
    kept, mat = [], []
    per_series = {}
    for s in members:
        k, m = build_matrix(conn, s, grid, objective, window_end, log=log)
        per_series[s] = len(k)
        for e, row in zip(k, m):
            kept.append({**e, "series": s})
            mat.append(row)
    order = sorted(range(len(kept)), key=lambda i: kept[i]["close"])
    kept = [kept[i] for i in order]
    mat = [mat[i] for i in order]
    res = _replay_arms(kept, mat, len(grid), window_start, adopt_p, objective)
    if res is None:
        if log:
            log(f"  [{pool}]: no events inside the window — skipped")
        return None
    return {"series": f"pool:{pool}", "module": module, "objective": objective,
            "pooled": members, "n_scored_per_series": per_series,
            "n_sets": len(grid), "grid_report": report, **res}


def run(conn, window_start: datetime, window_end: datetime,
        adopt_p: float = _dsr.ADOPT_P, series: list[str] | None = None,
        log=print, objective: str = "brier", pools: list[str] | None = None) -> dict:
    """Replay every series, plus any branch pools asked for.

    Pools are reported ALONGSIDE their constituent series rather than replacing them, and
    they are excluded from the aggregate so the same event is never counted twice. A pool
    is a different sample of the same events, not additional evidence.
    """
    todo = series or [s for s in SERIES_DISPATCH if s in MODULE_OF]
    per, totals = {}, {a: [0.0, 0] for a in ARMS}
    pooled = {}
    for name in (pools or []):
        try:
            r = replay_pool(conn, name, window_start, window_end, adopt_p=adopt_p,
                            log=log, objective=objective)
        except Exception as e:                                   # noqa: BLE001
            log(f"  [{name}]: FAILED {type(e).__name__}: {e}")
            continue
        if not r:
            continue
        pooled[name] = r
        log(f"  [{name}]: n_oos={r['n_oos']} sets={r['n_sets']} " +
            "  ".join(f"{a}={r['arms'][a]['loss']:.5f}"
                      f"(moved {r['arms'][a]['n_moved']})" for a in ARMS))
    for s in todo:
        try:
            r = replay(conn, s, window_start, window_end, adopt_p=adopt_p, log=log,
                       objective=objective)
        except Exception as e:                                   # noqa: BLE001
            log(f"  {s}: FAILED {type(e).__name__}: {e}")
            continue
        if not r:
            continue
        per[s] = r
        for a in ARMS:
            totals[a][0] += r["arms"][a]["loss"] * r["n_oos"]
            totals[a][1] += r["n_oos"]
        log(f"  {s}: n_oos={r['n_oos']} sets={r['n_sets']} " +
            "  ".join(f"{a}={r['arms'][a]['loss']:.5f}"
                      f"(moved {r['arms'][a]['n_moved']})" for a in ARMS))
    agg = {a: round(v[0] / v[1], 6) if v[1] else None for a, v in totals.items()}
    out = {"window": [window_start.isoformat(), window_end.isoformat()],
           "objective": objective, "adopt_p": adopt_p,
           "n_oos_total": totals["default"][1],
           "aggregate_loss": agg, "per_series": per, "pools": pooled}
    if objective == "pnl":
        # total dollars, which is what the aggregate mean loss hides: a per-event mean
        # over 63 events is unreadable as a strategy result
        out["total_pnl"] = {a: round(-v[0], 4) for a, v in totals.items()}
    return out


def main():
    import argparse
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--adopt-p", type=float, default=_dsr.ADOPT_P)
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--objective", default="brier", choices=OBJECTIVES)
    ap.add_argument("--pools", nargs="*", default=[], choices=list(POOLS) or None)
    ap.add_argument("--out", default="/tmp/param_wf.json")
    a = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    ws = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    we = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
    res = run(conn, ws, we, adopt_p=a.adopt_p, series=a.series, objective=a.objective,
              pools=a.pools)
    open(a.out, "w").write(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["aggregate_loss"], indent=1))
    if "total_pnl" in res:
        print("total_pnl:", json.dumps(res["total_pnl"]))
    for name, r in res.get("pools", {}).items():
        print(f"pool {name}: n_oos={r['n_oos']} " +
              json.dumps({k: v["pnl"] if v.get("pnl") is not None else v["loss"]
                          for k, v in r["arms"].items()}))
    print("n_oos_total:", res["n_oos_total"], "->", a.out)


if __name__ == "__main__":
    main()
