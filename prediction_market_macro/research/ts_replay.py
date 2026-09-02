"""research/ts_replay.py — PIT walk-forward replay of the Chronos-2 shadow (§7-bis).

Answers one question: did the 0.2.0 rebuild (encoding, AAA anchor, first-print vintage,
covariates) actually make the shadow better, or does it only look better?

METHOD — "one more day of information per step". For each settled event, asof marches
forward one calendar day at a time toward the close. At every asof, every variant is
rebuilt from scratch against reads filtered `knowledge_time <= asof`, and is scored on the
SAME leg universe as the market baseline. Nothing is carried between steps, so a variant
cannot benefit from a later step's data.

LEAKAGE CONTROL — three independent guards, all asserted rather than assumed:
  1. Every read in model/ts_covariates filters knowledge_time <= asof.
  2. `data_horizon <= asof` is checked on every single pred (not just on DB write, which
     is where common.pred_to_row asserts it) and a violation aborts the run.
  3. The close-vs-release hazard: for series whose book closes AFTER the settling print is
     public, close-1h would hand the model the very number it is scored on. asof is
     stepped behind `_settle_release_ts`, reusing research/backtest's exact-mapping
     implementation (a fuzzy window manufactures leaks — see its docstring).

ADMISSIBILITY — read this before quoting any number here as grounds for promotion.
Chronos-2's pretraining corpus contains public macro series (WTI, Henry Hub, ICSA,
retail gasoline). A historical replay is therefore CONTAMINATED as evidence of forecast
skill, and §7-bis deliberately requires a live-forward window instead. What this replay
IS admissible for is the engineering question: 0.1.0 vs 0.2.0 is a comparison of two
ENCODINGS of the same model's output on the same data, so a difference between them is
attributable to the code change and not to memorised history. Use it to decide whether the
rebuild is sound; use the live shadow gate to decide whether to promote.

Run:  conda run -n someopark_run python -m prediction_market_macro.research.ts_replay \\
          --db /tmp/macro_ro.db --events 8 --days 5 --out /tmp/ts_replay.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from prediction_market_macro.config.registry import REGISTRY, effective_strike_type
from prediction_market_macro.model import ts_foundation as tf
from prediction_market_macro.model.common import Empirical, grid_pmf, leg_fair
from prediction_market_macro.research.backtest import _market_leg_prob, _settle_release_ts
from prediction_market_macro.util.periods import kalshi_period_to_key

# 0.1.0's encoding, reproduced exactly: ask for 99 levels (78 of which chronos-2
# fabricates by interpolating its 21 trained ones) and hand the vector to Empirical as
# though it were 99 samples. Kept here, in the research tree, purely as the A/B control —
# the production module no longer contains it.
_OLD_LEVELS = list(np.arange(0.01, 1.0, 0.01))


class Leak(RuntimeError):
    """A pred consumed data published after its own asof."""


def _variants(series: str) -> list[str]:
    v = ["v010", "v020"]
    if series in ("KXWTIW", "KXNATGASW"):
        v.append("vol")
    return v


def _predict(conn, variant: str, asof: datetime, key: str, series: str):
    """Build one variant's pmf-ready dist. Raises on any PIT violation."""
    if variant == "v010":
        task, s, horizons, meta, h = None, None, None, None, None
        task, s, horizons, meta = tf._build_task(conn, series, asof)
        h = tf._steps_to(key, s.index[-1], tf._TARGETS[series].step)
        tf._attach_future(conn, series, asof, task, s.index[-1], h)
        q_list, _ = tf._pipeline().predict_quantiles(
            [task], prediction_length=h, quantile_levels=_OLD_LEVELS)
        vals = np.asarray(q_list[0])[0, h - 1, :]
        # 0.1.0 also rounded to 4dp before constructing Empirical
        dist = Empirical(tuple(np.round(vals, 4).tolist()))
        horizon = max(x for x in horizons if x)
        inputs = {"ctx_len": meta["ctx_len"]}
    else:
        fn = tf.predict if variant == "v020" else tf.predict_vol_bridge
        p = fn(conn, asof, key, series)
        dist, inputs = p.dist, p.inputs
        horizon = p.data_horizon.isoformat()
        if p.data_horizon > p.asof:
            raise Leak(f"{series}/{key}/{variant}: horizon {p.data_horizon} > asof {asof}")
    hz = datetime.fromisoformat(horizon)
    if hz > asof:
        raise Leak(f"{series}/{key}/{variant}: horizon {hz} > asof {asof}")
    return dist, inputs


def replay_series(conn, series: str, max_events: int, days: int,
                  verbose: bool = True) -> dict:
    spec = REGISTRY[series]
    events = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period"
        " HAVING ct IS NOT NULL ORDER BY ct DESC LIMIT ?",
        (series, max_events)).fetchall()

    # brier[variant][lag_days][event_period] -> that event's mean Brier. Keyed by EVENT
    # rather than appended to a list because a lag with an empty book skips the append,
    # and positional lists would then silently misalign variant-vs-market pairs.
    brier: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(dict))
    mkt: dict[int, dict] = defaultdict(dict)
    dead: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    modes: dict[str, int] = defaultdict(int)
    # Per lag: settled legs offered vs legs that had a usable market bar and were
    # therefore scored. The market baseline defining the universe is the design and it
    # stays; counting it is what was missing. 80.4% of settled legs across the fourteen
    # series carry no two-sided quote at close−1h (PLAN_DFM_SYNTH.md §5e), and coverage
    # generally FALLS as the lag grows because earlier bars are rarer — so one variant
    # is compared across lags on shrinking, liquidity-selected books. Same universe per
    # lag for every variant, so the A/B is unaffected; the scope is not readable unless
    # it is reported.
    cov_settled: dict[int, int] = defaultdict(int)
    cov_scored: dict[int, int] = defaultdict(int)
    errors: list[str] = []
    event_ids: list[str] = []
    n_events = 0

    for ev in events:
        key = kalshi_period_to_key(ev["period"])
        if not key:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        release_ts = _settle_release_ts(conn, spec, key)
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        if not legs:
            continue
        used = False
        for lag in range(days):
            asof = close_ts - timedelta(hours=1) - timedelta(days=lag)
            # guard 3: never let asof reach past the settling print
            if release_ts is not None and asof >= release_ts:
                asof = release_ts - timedelta(seconds=1)

            # the market baseline defines the scored leg universe, identical for every
            # variant, so a variant cannot win by being scored on easier legs
            universe = []
            for l in legs:
                mp = _market_leg_prob(conn, l["ticker"], asof)
                if mp is None or l["floor_strike"] is None:
                    continue
                universe.append((l, mp))
            cov_settled[lag] += len(legs)
            cov_scored[lag] += len(universe)
            if not universe:
                continue

            pmfs = {}
            for variant in _variants(series):
                try:
                    dist, inputs = _predict(conn, variant, asof, key, series)
                    pmfs[variant] = grid_pmf(dist, spec.round_rule)
                    if variant == "v020" and "mode" in inputs:
                        modes[inputs["mode"]] += 1
                except Leak:
                    raise
                except Exception as e:                          # noqa: BLE001
                    errors.append(f"{series}/{key}/lag{lag}/{variant}: {type(e).__name__}: {e}")
            if not pmfs:
                continue

            acc = {v: [] for v in pmfs}
            mkt_acc, dead_acc = [], {v: 0 for v in pmfs}
            for l, mp in universe:
                out01 = 1.0 if l["result"] == "yes" else 0.0
                mkt_acc.append((mp - out01) ** 2)
                for v, pmf in pmfs.items():
                    f = leg_fair(pmf, effective_strike_type(series, l["strike_type"]),
                                 l["floor_strike"], l["cap_strike"])
                    acc[v].append((f - out01) ** 2)
                    if 0.02 <= mp <= 0.98 and (f <= 1e-9 or f >= 1 - 1e-9):
                        dead_acc[v] += 1
            for v in pmfs:
                brier[v][lag][ev["period"]] = float(np.mean(acc[v]))
                dead[v][lag].append(dead_acc[v])
            mkt[lag][ev["period"]] = float(np.mean(mkt_acc))
            used = True
        if used:
            n_events += 1
            event_ids.append(ev["period"])
            if verbose:
                print(f"  {series} {ev['period']:10s} legs={len(legs):3d} done",
                      flush=True)

    def agg(d):
        return {lag: round(float(np.mean(list(x.values()))), 5)
                for lag, x in sorted(d.items()) if x}

    def per_event(d):
        # event_period -> Brier, NOT a bare list. The bootstrap resamples event ids and
        # looks both variant and market up by the same id; a positional list would pair
        # them wrongly the moment one lag skips an event for an empty book.
        return {lag: {k: round(float(y), 6) for k, y in x.items()}
                for lag, x in sorted(d.items()) if x}

    return {
        "series": series, "n_events": n_events,
        "brier": {v: agg(brier[v]) for v in brier},
        "brier_market": agg(mkt),
        # per-EVENT series, kept so significance can be tested by resampling EVENTS
        # (the clustering unit — legs within an event share one outcome draw; pooling
        # legs would overstate n by ~15x and manufacture significance. Same lesson as
        # the isotonic-calibration cliff documented in research/eval.py).
        "per_event": {v: per_event(brier[v]) for v in brier},
        "per_event_market": per_event(mkt),
        "event_ids": event_ids,
        "n_scored": {lag: len(x) for lag, x in sorted(mkt.items())},
        # scored/settled per lag. Every Brier above is on this subsample, which is
        # selected on liquidity rather than at random; see §5e.
        "leg_coverage": {lag: round(cov_scored[lag] / cov_settled[lag], 4)
                         for lag in sorted(cov_settled) if cov_settled[lag]},
        "legs_settled": {lag: cov_settled[lag] for lag in sorted(cov_settled)},
        "legs_scored": {lag: cov_scored[lag] for lag in sorted(cov_scored)},
        "dead_on_live": {v: {lag: int(np.sum(x)) for lag, x in sorted(dead[v].items())}
                         for v in dead},
        "modes": dict(modes),
        "errors": errors[:20], "n_errors": len(errors),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--events", type=int, default=8)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--series", default=None, help="comma list; default all covered")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    series_list = a.series.split(",") if a.series else list(tf._TARGETS)

    out, t0 = {}, time.time()
    for s in series_list:
        print(f"== {s}", flush=True)
        out[s] = replay_series(conn, s, a.events, a.days)
    out["_meta"] = {"db": a.db, "events": a.events, "days": a.days,
                    "elapsed_s": round(time.time() - t0, 1),
                    "generated": datetime.now(timezone.utc).isoformat(),
                    "admissibility": "0.1.0-vs-0.2.0 is a clean encoding A/B; "
                                     "absolute-vs-market is contaminated by pretraining "
                                     "(see module docstring) and is NOT promotion evidence"}
    txt = json.dumps(out, indent=2)
    if a.out:
        with open(a.out, "w") as f:
            f.write(txt)
        print(f"\nwrote {a.out}")
    print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
