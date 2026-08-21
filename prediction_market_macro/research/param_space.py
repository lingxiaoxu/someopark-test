"""research/param_space.py — per-module parameter spaces, sized to the data.

Two things this module exists to prevent, both measured on the live db 2026-08-04.

**1. Width spent on parameters that cannot move the output.**
claims.py declared five parameters and `predict()` read one. research/param_grid.py built
3 x 2 x 2 x 2 = 24 sets on top of that and the 24 collapsed to THREE distinct predictions,
each duplicated eight times. Nothing raised; the walk-forward argmin ran over 24 columns of
which 21 were copies. So a declared parameter is not a parameter until it is demonstrated
to change a prediction, and `live_keys()` demonstrates it per series, on real events,
before the grid is built. The dead ones are dropped and REPORTED — never dropped silently.

That check is per SERIES, not per module, because several modules are two models behind one
entry point and the split is invisible from the outside:
    cpi.py     gas_* and food_drift reach KXCPI / KXCPIYOY only; the core branch never
               calls _gas_effect, so those keys are dead for KXCPICORE / KXCPICOREYOY.
    energy.py  fut_* reach KXWTIW / KXNATGASW; aaa_* reach KXAAAGASW. Never both.
Hard-coding that table would rot the moment a branch moves. Measuring it cannot.

**2. More candidates than the history can distinguish.**
Selecting argmin over K candidates on n observations is a maximum of K noisy means, and it
is biased low by roughly sigma*sqrt(2*ln(K)/n) even when every candidate is identical. The
usable event counts here are small and very uneven — KXWTIW has 142, KXGDP has 1 — so a
flat "200 sets per market" would be pure table lookup at the thin end. `max_sets()` inverts
the bound: given n, the largest K whose selection noise stays inside SELECTION_TOLERANCE.
It is a sizing rule, not a significance test; the deflation that decides ADOPTION is
research/dsr.py.

Counts that drive the sizing (settled + model-runnable, 2026-08-04):
    KXWTIW 142  KXAAAGASW 70  KXJOBLESSCLAIMS 49  KXCPICOREYOY 43  KXCPI 43
    KXCPICORE 42  KXU3 42  KXCPIYOY 41  KXPAYROLLS 38  KXFED 28  KXPCECORE 22
    KXNATGASW 18  KXGDP 1  KXFEDDECISION 1
The last two cannot support selection at all and must keep running on defaults.
"""
from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta

# Selection noise we are willing to accept from the argmin itself, in units of the
# per-event Brier sd. 0.35 keeps the search bias visibly below the effect sizes that have
# actually moved this system (the cpi/0.2.0 second-lag change was ~0.5 sd on the same
# scale). Raise it and the grid gets wider and the winner gets luckier.
SELECTION_TOLERANCE = 0.35
MIN_EVENTS = 12          # below this, no selection at all — run the defaults


def max_sets(n_events: int, tolerance: float = SELECTION_TOLERANCE) -> int:
    """Largest grid width whose argmin bias stays within `tolerance` sd on n events.

    Inverts bias ~ sqrt(2*ln(K)/n): K <= exp(n * tolerance^2 / 2). Clamped to [1, 512].
    The growth is exponential in n, which is the honest shape — doubling the history buys
    a lot more search than doubling the grid buys accuracy.
    """
    if n_events < MIN_EVENTS:
        return 1
    k = math.exp(n_events * tolerance * tolerance / 2.0)
    return int(max(1, min(512, math.floor(k))))


def live_keys(conn, series: str, predict, keys: dict, events: list,
              max_events: int = 8) -> tuple[list[str], list[str]]:
    """Split `keys` into (live, dead) for THIS series by perturbing each one.

    keys:   {param_name: a probe value that must change the prediction}
    events: [(asof, period)] — real settled events, newest first.

    A key counts as live the moment one event moves, so a probe only has to bite on a
    single branch. Dead means it moved nothing anywhere: either it belongs to the other
    branch of a two-model module, or it is not wired up at all. Both are reasons to keep
    it out of the grid; telling them apart is a human's job, which is why the caller is
    expected to report the dead list rather than swallow it.
    """
    live, dead = [], []
    evs = events[:max_events]
    for key, probe in keys.items():
        moved = False
        for asof, period in evs:
            try:
                a = predict(conn, asof, period, series)
                b = predict(conn, asof, period, series, params={key: probe})
            except Exception:                                    # noqa: BLE001
                continue                    # unrunnable event tells us nothing either way
            if a.inputs != b.inputs or repr(a.dist) != repr(b.dist):
                moved = True
                break
        (live if moved else dead).append(key)
    return live, dead


def settled_events(conn, series: str, limit: int | None = None,
                   before: datetime | None = None) -> list[tuple[datetime, str]]:
    """[(asof, period_key)] for settled events, NEWEST FIRST, asof = close - 1h.

    `before` bounds the read for walk-forward use: an event is admitted only if it closed
    strictly before that instant, so a caller replaying day T cannot see day T+1's settle.
    """
    from prediction_market_macro.util.periods import kalshi_period_to_key
    rows = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct DESC", (series,)
    ).fetchall()
    out = []
    for r in rows:
        key = kalshi_period_to_key(r["period"])
        if not key or not r["ct"]:
            continue
        close = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
        if before is not None and close >= before:
            continue
        out.append((close - timedelta(hours=1), key))
        if limit and len(out) >= limit:
            break
    return out


# ── candidate values per parameter ───────────────────────────────────────────────
# Each entry is (probe_value_for_liveness, [candidate values including the default]).
# The default MUST appear in its own candidate list: the grid has to be able to return
# "the current model" as the winner, otherwise selection can only ever move away from it.
CANDIDATES: dict[str, dict[str, tuple]] = {
    "claims": {
        "level_weights": ((0.0, 0.0, 0.0, 1.0),
                          [(0.1, 0.2, 0.3, 0.4), (0.25, 0.25, 0.25, 0.25),
                           (0.0, 0.0, 0.3, 0.7)]),
        "seasonal_years": (3, [6, 10]),
        "vol_window": (8, [13, 26]),
        "sigma_floor": (0.50, [0.015, 0.02]),
        "seasonal_clip": (0.0, [0.15, 0.25]),
    },
    "cpi": {
        "w_last": (0.2, [0.4, 0.5]),          # §29.6: the RMSE grid is FLAT over 0.3..0.5,
                                              # so this is here to be shown harmless, not
                                              # to be tuned. Two values, both inside it.
        "mean_window": (6, [9, 12, 18]),
        "mad_window": (36, [12, 18, 24]),
        "sigma_floor": (0.9, [0.04, 0.06]),
        "horizon_widen": (0.9, [0.05, 0.10, 0.15]),
        "gas_weight": (0.10, [0.025, 0.031, 0.037]),
        "rb_passthrough": (0.05, [0.40, 0.55, 0.70]),
        "food_drift": (0.5, [0.0, 0.03, 0.05]),
    },
    "pce": {
        "bridge_window": (40, [48, 60, 72]),
        "resid_floor": (0.9, [0.03, 0.04, 0.05]),
    },
    "payrolls": {
        "base_months": (9, [3, 6]),
        "w_base": (0.1, [0.5, 0.6, 0.75]),
        "jobs_per_claim": (8.0, [1.0, 2.0, 3.0]),
        # payrolls/0.2.0 searches the SHAPE and the SCALING of the mixture, not absolute
        # widths. sigma_core/sigma_tail are gone because they no longer set the width —
        # and note they would have LOOKED live if left here, since predict() echoes a
        # retired key into inputs and live_keys() reads inputs. The grid asks how much to
        # trust the rolling estimate, not what number to pin.
        "sigma_window": (3, [18, 24, 36]),
        "sigma_mult": (3.0, [0.9, 1.0, 1.15]),
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
        "w_dgs2": (0.95, [0.20, 0.30]),
        "w_rule": (0.95, [0.10, 0.15, 0.25]),
        # shrink_halflife_m is deliberately absent: at the close-1h scoring asof the
        # meeting is imminent, so lambda is 0 and the parameter cannot move ANY scored
        # prediction. It matters only for far-dated contracts, which this scoring cannot
        # see. Including it would be width with a guaranteed-zero payoff.
    },
    "energy": {
        "fut_vol_window": (60, [10, 20, 40]),
        "fut_pool_bars": (200, [750, 1500, 3000]),
        "aaa_sig_w_window": (8, [26, 52, 104]),
        "aaa_sig_w_floor": (0.9, [0.005, 0.01]),
        "aaa_min_fit": (99999, [10, 16]),
        "aaa_resid_floor": (0.9, [0.015, 0.02, 0.03]),
        "aaa_proxy_inflation": (6.0, [1.25, 1.5, 1.75]),
        "aaa_fresh_days": (0, [3, 7]),
    },
    "gdp": {},        # n=1. No selection is possible and none is offered.
}


def build_grid(conn, series: str, module: str, predict, n_events: int,
               log=None) -> tuple[list[dict], dict]:
    """Live-filtered, size-capped candidate grid for one series.

    Returns (grid, report). The report carries what was dropped and why — width lost to
    dead keys and width lost to the size cap are different problems and are counted
    separately. Nothing here is allowed to shrink the grid quietly.
    """
    spec = CANDIDATES.get(module, {})
    report = {"series": series, "module": module, "n_events": n_events,
              "cap": max_sets(n_events), "dead_keys": [], "dropped_keys": [],
              "n_sets": 1, "full_product": 1}
    if not spec or n_events < MIN_EVENTS:
        report["reason"] = ("no candidates for module" if not spec else
                            f"only {n_events} events (< {MIN_EVENTS}) — defaults only")
        return [{}], report

    events = settled_events(conn, series, limit=8)
    probes = {k: v[0] for k, v in spec.items()}
    live, dead = live_keys(conn, series, predict, probes, events)
    report["dead_keys"] = dead

    # Spend the cap on the parameters with the most candidates first: a key with 3 values
    # buys more search than one with 2, and dropping it costs less than halving two others.
    ordered = sorted(live, key=lambda k: -len(spec[k][1]))
    report["full_product"] = math.prod(len(spec[k][1]) for k in ordered) if ordered else 1
    chosen, width = [], 1
    for k in ordered:
        w = len(spec[k][1])
        if width * w > report["cap"]:
            report["dropped_keys"].append(k)
            continue
        chosen.append(k)
        width *= w
    grid = [dict(zip(chosen, combo))
            for combo in itertools.product(*[spec[k][1] for k in chosen])] or [{}]
    report["n_sets"] = len(grid)
    report["live_keys"] = chosen
    if log:
        log(f"{series}: {len(grid)} sets from {len(chosen)} live params "
            f"(cap {report['cap']} on n={n_events}); "
            f"dead={dead or 'none'}; dropped_for_size={report['dropped_keys'] or 'none'}")
    return grid, report
