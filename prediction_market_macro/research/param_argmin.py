"""research/param_argmin.py — daily per-market argmin re-selection (user policy, 2026-08-11).

The user's standing instruction: every morning, re-run the per-market parameter search
over the trailing 75 days of PnL and REPLACE the production parameters with each
market's argmin. This module is that instruction as code. It deliberately does NOT
apply `dsr.select`: the DSR-gated selector (`param_select`) measured the same searches
as unadoptable (every market below MIN_OBS=12) and its objection stands on record in
README §E — the user chose raw argmin anyway, and this module says so in every row it
writes rather than pretending the deflation happened.

Mechanics:
  spaces    per-module designed grids (wider than param_space.CANDIDATES — these are
            the 2026-08-11 study spaces), live-key probed on events before the window
  scoring   pnl_score.score_matrix over quotable events in the trailing WINDOW_DAYS —
            the PROD rule (hybrid stream, exits on), realised dollars
  cadence   fingerprint-cached like param_select: a market rescored only when a new
            scoreable event has settled, so most days most markets cost one SELECT
  adoption  argmin params -> param_select.set_manual (history-preserving rows;
            `param_select.history` is the change log, exported to the frontend)
  approx    per-event replay, NOT full-portfolio sim — the measured caveat from the
            cross-product study (WTIW −13.9% vs −27.7%); the brute sweep
            (docs/PLAN_BRUTE_SWEEP.md) is the periodic ground-truth check.
"""
from __future__ import annotations

import importlib
import itertools
import json
import math
from datetime import datetime, timedelta, timezone

from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.research import pnl_score as _ps
from prediction_market_macro.research.param_select import (manual_params, set_manual)
from prediction_market_macro.research.param_space import live_keys, settled_events
from prediction_market_macro.research.param_wf import MODULE_OF, _predict_fn
from prediction_market_macro.util.periods import kalshi_period_to_key

WINDOW_DAYS = 75

# ── sample gate (2026-08-21) ────────────────────────────────────────────────────
# The policy above is unchanged: raw argmin, no DSR deflation. What this adds is a
# FLOOR on how wide a search that argmin is allowed to run when the sample is thin.
#
# The argmin of K trials on n events overstates the winner by about sqrt(2*ln K / n)
# per-event sd even when every set is identical, so K and n are not independent knobs.
# Measured on the live db 2026-08-20, before this gate:
#
#     KXAAAGASW        n=11  K=7    0.59 sd      KXFED       n=2  K=10   1.52 sd
#     KXWTIW/NATGASW   n=10  K=21   0.78 sd      KXCPI(YOY)  n=3  K=82   1.71 sd
#     KXJOBLESSCLAIMS  n=10  K=91   0.95 sd      KXCPICORE   n=3  K=109  1.77 sd
#                                                KXU3        n=2  K=82   2.10 sd
#                                                KXPAYROLLS  n=2  K=97   2.14 sd
#
# The weekly markets settle ~weekly, so 75 days is 10-11 events and they sit under 1 sd.
# The monthly ones settle once a month: 2-3 events against 82-109 candidate sets. A
# concrete instance — KXPAYROLLS on 2026-08-21 would have turned pnl -1.84 into +8.11
# picking from 97 sets on 2 events. Nearly all of that +9.95 is the bias above.
#
# TOLERANCE is the ceiling on that bias, in per-event sd. Inverting gives K <= exp(n*t^2/2).
# 1.0 is deliberately looser than param_space.SELECTION_TOLERANCE (0.35): this lane was
# chosen to be less conservative than the DSR one and the gate is not a back-door revert
# of that choice. It only removes searches so wide the winner is unidentifiable:
#
#     n=2  -> K<=2      n=3  -> K<=4      n=10 -> K<=148     n=11 -> K<=244
#
# so the four weekly markets are untouched (their CAP already binds well below 148) and
# the monthly ones narrow to a couple of sets — at n=2 to defaults only — until they have
# the history to support a search. Nothing is adopted on their behalf in the meantime;
# whatever was adopted before stays until it is rescored or cleared.
#
# NOTE: param_space.max_sets is the same inversion but hard-returns 1 below MIN_EVENTS=12,
# which is DSR-lane policy and would shut this lane down entirely. Hence a local function.
ARGMIN_TOLERANCE = 1.0


def sample_cap(n_events: int, tolerance: float = ARGMIN_TOLERANCE) -> int:
    """Widest `width` (the cross-product, EXCLUDING the default row) the sample supports.

    `build` returns [{}] + product(...), so the number of trials the argmin actually
    chooses among is width+1; the bias bound is inverted on that trial count and the
    default row given back, which is why this returns k_max-1 rather than k_max.
    Returns 0 when the sample supports no search at all — the caller must then ship
    defaults rather than pick.
    """
    if n_events <= 0:
        return 0
    k_max = math.floor(math.exp(n_events * tolerance * tolerance / 2.0))
    return max(0, min(4096, k_max - 1))

SPACES: dict[str, dict[str, tuple]] = {
    "claims": {
        "level_weights": ((0.0, 0.0, 0.0, 1.0),
                          [(0.1, 0.2, 0.3, 0.4), (0.25, 0.25, 0.25, 0.25),
                           (0.0, 0.0, 0.3, 0.7), (0.0, 0.1, 0.3, 0.6),
                           (0.05, 0.15, 0.3, 0.5)]),
        "seasonal_years": (3, [6, 10, 15]),
        "seasonal_clip": (0.0, [0.15, 0.25]),
        "vol_window": (8, [13, 26, 52]),
    },
    "cpi": {
        "w_last": (0.2, [0.3, 0.4, 0.5]),
        "mean_window": (6, [9, 12, 18]),
        "mad_window": (36, [12, 18, 24]),
        "sigma_floor": (0.9, [0.04, 0.06]),
        "horizon_widen": (0.9, [0.05, 0.10]),
        "gas_weight": (0.10, [0.025, 0.031, 0.037]),
        "rb_passthrough": (0.05, [0.40, 0.55, 0.70]),
        "food_drift": (0.5, [0.0, 0.03, 0.05]),
        "gas_sigma_unobs": (0.5, [0.06, 0.08]),
    },
    "pce": {
        "bridge_window": (24, [36, 48, 60, 72]),
        "resid_floor": (0.9, [0.03, 0.04, 0.05, 0.06]),
    },
    "payrolls": {
        "base_months": (12, [3, 6]),
        "w_base": (0.1, [0.4, 0.5, 0.6, 0.75]),
        "jobs_per_claim": (8.0, [1.0, 2.0, 3.0]),
        "claims_clip": (10_000, [100_000, 150_000, 200_000]),
        # 0.2.0: absolute widths retired — the mixture scale now tracks the model's own
        # residuals, so what is searchable is the window, the multiplier and the shape.
        "sigma_window": (3, [18, 24, 36]),
        "sigma_mult": (3.0, [0.85, 0.9, 1.0, 1.15]),
        "tail_mult": (8.0, [2.0, 2.55, 3.0]),
        "w_tail": (0.6, [0.15, 0.2, 0.3]),
    },
    "u3": {
        "hist_months": (36, [120, 180, 240]),
        "laplace": (20.0, [0.25, 0.5, 1.0]),
        "tilt_frac": (0.9, [0.0, 0.2, 0.35]),
        "tilt_threshold": (1.0, [5000, 8000, 12000]),
    },
    "fed": {
        "w_ff": (0.05, [0.40, 0.50, 0.60]),
        "w_market": (0.95, [0.25, 0.35, 0.45]),
        "w_dgs2": (0.95, [0.20, 0.30, 0.40]),
        "w_rule": (0.95, [0.10, 0.15, 0.25]),
    },
    "energy": {
        "fut_vol_window": (60, [5, 10, 20, 40, 60]),
        "fut_pool_bars": (200, [375, 750, 1500, 3000]),
        "aaa_sig_w_window": (8, [26, 52, 104]),
        "aaa_sig_w_floor": (0.9, [0.005, 0.01]),
        "aaa_min_fit": (99999, [10, 16]),
        "aaa_resid_floor": (0.9, [0.015, 0.02, 0.03]),
        "aaa_proxy_inflation": (6.0, [1.25, 1.5, 1.75]),
        "aaa_fresh_days": (0, [3, 7]),
        "aaa_trend_damp": (0.05, [0.25, 0.5, 0.75]),
    },
}
CAP = {"KXJOBLESSCLAIMS": 100, "KXCPI": 110, "KXCPICORE": 110, "KXCPIYOY": 110,
       "KXCPICOREYOY": 110, "KXPCECORE": 20, "KXPAYROLLS": 120, "KXU3": 90,
       "KXFED": 90, "KXWTIW": 20, "KXNATGASW": 20, "KXAAAGASW": 110}
MARKETS = list(CAP)


def build(conn, series: str, window_start: datetime,
          n_events: int | None = None) -> tuple[list[dict], dict]:
    """Live-key-filtered grid, default at index 0. Probe events close BEFORE the
    window so grid design never sees the evaluation sample (the grid75 protocol).

    `n_events` is the size of the sample the grid will be SCORED on. Only its count is
    used — never an outcome — so the grid75 protocol still holds: the design cannot be
    tuned to the evaluation, it can only be made narrower because the evaluation is small.
    Passing None keeps the pre-2026-08-21 behaviour (the static CAP alone) and is what
    the study scripts use to reproduce the original spaces.
    """
    spec = SPACES.get(MODULE_OF[series], {})
    if not spec:
        return [{}], {"note": "no space"}
    cap = CAP[series]
    scap = None
    if n_events is not None:
        scap = sample_cap(n_events)
        cap = min(cap, scap)
    pre = settled_events(conn, series, limit=8, before=window_start)
    fn = _predict_fn(series)
    live, dead = live_keys(conn, series, fn, {k: v[0] for k, v in spec.items()}, pre)
    ordered = sorted(live, key=lambda k: -len(spec[k][1]))
    chosen, width, dropped = [], 1, []
    for k in ordered:
        w = len(spec[k][1])
        if width * w > cap:
            dropped.append(k)
            continue
        chosen.append(k)
        width *= w
    rep = {"live": chosen, "dead": dead, "dropped_for_cap": dropped,
           "cap": cap, "sample_cap": scap, "n_events_for_cap": n_events}
    if not chosen:                      # no live keys → defaults only, no dup {} row
        rep.update({"live": [], "n_sets": 1})
        # a thin sample is a different situation from a model with no live keys, and the
        # two must not read the same in the log — one waits for history, one is a bug.
        if scap is not None and scap < min((len(v[1]) for v in spec.values()), default=1):
            rep["reason"] = (f"sample too thin: {n_events} events support at most "
                             f"{scap + 1} trials, narrowest key has "
                             f"{min(len(v[1]) for v in spec.values())} values")
        return [{}], rep
    grid = [{}] + [dict(zip(chosen, c))
                   for c in itertools.product(*[spec[k][1] for k in chosen])]
    rep["n_sets"] = len(grid)
    return grid, rep


def _fingerprint(conn, series: str, now: datetime) -> str:
    lo = now - timedelta(days=WINDOW_DAYS)
    evs = [e for e in _ps.quotable_events(conn, series, before=now)
           if e["close_ts"] >= lo]
    # model version is part of the key: a model bump (e.g. cpi/0.2.0 -> 0.3.0 nowcast
    # anchor) changes every replayed score, and an events-only fingerprint would keep
    # serving the OLD model's argmin until the next settle happened to land.
    ver = getattr(importlib.import_module(SERIES_DISPATCH[series][0]), "VERSION", "?")
    return (f"{len(evs)}:{max((e['close_ts'].isoformat() for e in evs), default='-')}"
            f":{ver}")


def _last_log(conn, series: str):
    return conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='param_argmin' AND series=?"
        " ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()


def rescore(conn, series: str, now: datetime, log=None) -> dict | None:
    lo = now - timedelta(days=WINDOW_DAYS)
    # the universe is built FIRST so the grid can be sized to it (see `sample_cap`).
    # Only len(uni) crosses over; no outcome does.
    uni = [{**e, "key": kalshi_period_to_key(e["tok"]), "close": e["close_ts"]}
           for e in _ps.quotable_events(conn, series, before=now)
           if e["close_ts"] >= lo]
    uni = [e for e in uni if e["key"]]
    if not uni:
        return None
    grid, grep_ = build(conn, series, lo, n_events=len(uni))
    if len(grid) < 2:
        # Not an error and not "no sample": the sample exists, it is just too small to
        # tell K candidates apart. Returned as a result so `daily` can log the reason
        # instead of silently looking like a market with no data.
        return {"grid": grid, "grid_report": grep_, "n_events": len(uni),
                "best_idx": 0, "best_params": {}, "pnl_best": None,
                "pnl_default": None, "gated": True}
    kept, mat, _det = _ps.score_matrix(conn, series, grid, uni, log=log)
    if not kept:
        return None
    totals = [sum(row[j] for row in mat) for j in range(len(grid))]
    best = max(range(len(grid)), key=lambda j: totals[j])
    return {"grid": grid, "grid_report": grep_, "n_events": len(kept),
            "best_idx": best, "best_params": grid[best],
            "pnl_best": round(totals[best], 2), "pnl_default": round(totals[0], 2)}


def daily(conn, now: datetime | None = None, log=print) -> dict:
    """The morning pass. Returns {series: status}."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for series in MARKETS:
        fp = _fingerprint(conn, series, now)
        last = _last_log(conn, series)
        if last:
            m = json.loads(last["metrics_json"] or "{}")
            if m.get("fingerprint") == fp:
                out[series] = "cached"
                continue
        r = rescore(conn, series, now, log=None)
        if r is None:
            out[series] = "no sample"
            continue
        cur = manual_params(conn, series, now)
        cur_params = cur[0] if cur else {}
        # A gated market adopts NOTHING — and in particular does not adopt `{}` over an
        # existing row. The gate is a bar on new selection, not a retro-revert of a
        # decision the user already made; undoing those is a separate, explicit act.
        changed = (not r.get("gated")) and r["best_params"] != cur_params
        if changed:
            set_manual(conn, series, r["best_params"],
                       note=(f"daily argmin {now.date()}: window {WINDOW_DAYS}d, "
                             f"n_events={r['n_events']}, sets={len(r['grid'])}, "
                             f"pnl {r['pnl_default']:+.2f} -> {r['pnl_best']:+.2f}. "
                             "Raw argmin per standing user policy 2026-08-11; the "
                             "DSR gate's objection (n below MIN_OBS) is on record. "
                             f"Search width capped at {r['grid_report'].get('cap')} "
                             f"by the {r['n_events']}-event sample gate."))
        conn.execute(
            "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
            " metrics_json, created_ts) VALUES('param_argmin',?,?,?,?,?)",
            (f"argmin:{series}:{now.isoformat()}", series, f"{WINDOW_DAYS}d",
             json.dumps({"fingerprint": fp, "n_events": r["n_events"],
                         "n_sets": len(r["grid"]), "best_idx": r["best_idx"],
                         "best_params": r["best_params"],
                         "pnl_default": r["pnl_default"],
                         "pnl_best": r["pnl_best"], "adopted_change": changed,
                         "gated": bool(r.get("gated")),
                         "grid_report": r["grid_report"]}),
             now.isoformat()))
        conn.commit()
        if r.get("gated"):
            out[series] = (f"GATED n={r['n_events']} "
                           f"(cap {r['grid_report'].get('sample_cap')}) — "
                           f"{'holding ' + json.dumps(cur_params)[:40] if cur_params else 'defaults'}")
        else:
            out[series] = ("ADOPTED " + json.dumps(r["best_params"])[:60]) if changed \
                else "unchanged"
        if log:
            log(f"  param_argmin {series}: {out[series]}")
    return out


def main():
    from pathlib import Path
    from prediction_market_macro.ingest.store import connect
    db = Path(__file__).resolve().parent.parent / "data" / "macro.db"
    print(json.dumps(daily(connect(db)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
