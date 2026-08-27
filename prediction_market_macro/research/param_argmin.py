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

import hashlib
import importlib
import itertools
import json
import math
from dataclasses import dataclass
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


def sample_cap(n_events: float, tolerance: float = ARGMIN_TOLERANCE) -> int:
    """Widest `width` (the cross-product, EXCLUDING the default row) the sample supports.

    `build` returns [{}] + product(...), so the number of trials the argmin actually
    chooses among is width+1; the bias bound is inverted on that trial count and the
    default row given back, which is why this returns k_max-1 rather than k_max.
    Returns 0 when the sample supports no search at all — the caller must then ship
    defaults rather than pick.

    `n_events` is a float rather than a count because the synthetic sample enters it
    DISCOUNTED — `n_eff = n_real + lambda * n_synth` (S6) — and 2 + 0.07*96 is not an
    integer. The bound itself never cared: `sqrt(2 ln K / n)` is the standard deviation of
    a mean over n draws and is defined for any positive n.
    """
    if n_events <= 0:
        return 0
    k_max = math.floor(math.exp(n_events * tolerance * tolerance / 2.0))
    return max(0, min(4096, k_max - 1))

# ── the synthetic sample (S6, PLAN_DFM_SYNTH) ───────────────────────────────────
# The DFM generator produces worlds that resemble the current environment and the current
# bet, and every candidate set is replayed on them. That sample cannot enter `n` at face
# value — a synthetic event is not a real one — so it enters DISCOUNTED:
#
#     n_eff = n_real + lambda * n_synth
#
# `lambda` is measured, not chosen (`synth/calibrate.py`), and is read from `synth_lambda`
# rather than hard-coded so a re-measurement takes effect without a code change.
#
# **Nothing here imports the generator.** The daily lane reads per-candidate MEANS that a
# separate, slower job (S7) has already written to `synth_scores`; torch never loads in the
# morning pass. The cost of that separation is that the two must agree on which grid was
# scored, which is what `grid_hash` is for — a grid the job did not score is not blended, it
# is skipped and logged.
SYNTH_MAX_AGE_DAYS = 45


def set_hash(params: dict) -> str:
    """A parameter set's identity, stable across dict ordering and float formatting."""
    return hashlib.sha1(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def grid_hash(grid: list[dict]) -> str:
    """The identity of an ORDERED grid. Order matters because index 0 is the default row
    that every improvement is measured against."""
    return hashlib.sha1("|".join(set_hash(p) for p in grid).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SynthSample:
    """The synthetic half of the sample: one mean per candidate, plus its exchange rate."""
    run_id: str
    lam: float
    n_synth: int
    built_ts: datetime
    means: dict[str, float]          # set_hash -> mean PnL per synthetic event

    @property
    def weight(self) -> float:
        return self.lam * self.n_synth


def synth_lambda(conn, series: str) -> tuple[float, dict]:
    """The most recent measured lambda: the series' own if present, else the pooled '*'.

    A per-series row is preferred where it exists because the exchange rate is a property
    of how well the generator models THAT market. The pooled row is the fallback and is
    deliberately the min of the per-series lower bounds, so a market that was never
    measured — every monthly one, which is the whole reason the gate binds — inherits the
    worst case rather than an average it has no claim to.
    """
    # A db that predates S7 has no such table. That is an absent optional input, not a
    # failure — but it is reported by name rather than caught as an exception, because a
    # swallowed OperationalError would read identically to a typo in the query.
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN"
        " ('synth_lambda','synth_runs','synth_scores')")}
    if len(have) < 3:
        return 0.0, {"source": None,
                     "note": f"synthetic store not initialised (missing "
                             f"{sorted({'synth_lambda', 'synth_runs', 'synth_scores'} - have)})"}
    for key in (series, "*"):
        row = conn.execute("SELECT * FROM synth_lambda WHERE series=?"
                           " ORDER BY measured_ts DESC LIMIT 1", (key,)).fetchone()
        if row:
            return float(row["lam"]), {"source": key, "measured_ts": row["measured_ts"],
                                       "lam_point": row["lam_point"],
                                       "lam_lo": row["lam_lo"], "lam_hi": row["lam_hi"]}
    return 0.0, {"source": None, "note": "no lambda measured — synthetic sample unused"}


def read_synth(conn, series: str, now: datetime) -> tuple[SynthSample | None, dict]:
    """The freshest usable synthetic sample for `series`, or None and the reason why.

    Refusal is never silent and never partial. A stale run, a missing lambda and a run that
    scored a different grid are three different situations and the log says which.
    """
    lam, lam_rep = synth_lambda(conn, series)
    rep = {"lambda": lam, **lam_rep}
    if lam <= 0.0:
        rep["skipped"] = "lambda is zero — the synthetic sample carries no weight"
        return None, rep
    row = conn.execute("SELECT * FROM synth_runs WHERE series=?"
                       " ORDER BY built_ts DESC LIMIT 1", (series,)).fetchone()
    if not row:
        rep["skipped"] = "no synthetic run for this series"
        return None, rep
    built = datetime.fromisoformat(row["built_ts"])
    age = (now - built).days
    rep.update({"run_id": row["run_id"], "built_ts": row["built_ts"], "age_days": age,
                "n_synth": row["n_events"], "grid_hash": row["grid_hash"]})
    if age > SYNTH_MAX_AGE_DAYS:
        rep["skipped"] = f"run is {age}d old, limit {SYNTH_MAX_AGE_DAYS}d"
        return None, rep
    means = {r["set_hash"]: float(r["mean_pnl"]) for r in conn.execute(
        "SELECT set_hash, mean_pnl FROM synth_scores WHERE run_id=?", (row["run_id"],))}
    if not means:
        rep["skipped"] = "run exists but scored nothing"
        return None, rep
    return SynthSample(row["run_id"], lam, int(row["n_events"]), built, means), rep


SPACES: dict[str, dict[str, tuple]] = {
    "claims": {
        "level_weights": ((0.0, 0.0, 0.0, 1.0),
                          [(0.1, 0.2, 0.3, 0.4), (0.25, 0.25, 0.25, 0.25),
                           (0.0, 0.0, 0.3, 0.7), (0.0, 0.1, 0.3, 0.6),
                           (0.05, 0.15, 0.3, 0.5)]),
        "seasonal_years": (3, [6, 10, 15]),
        "seasonal_clip": (0.0, [0.15, 0.25]),
        "vol_window": (8, [13, 26, 52]),
        # #197/PR-11: `seasonal_estimator` is deliberately NOT here, and that is not the
        # #196 disease — claims/0.2.0 put the screen in DEFAULT_PARAMS, so every row this
        # lane scores (including the `{}` default row it has adopted since 2026-08-26)
        # already runs it. What is absent is the ability to search AWAY from it, and this
        # lane is the wrong place for that in both regimes the sample gate produces:
        #   thin  (n=3 -> sample_cap 3): the key is the second-widest at 4 values and is
        #         dropped before it is ever tried, so adding it changes nothing;
        #   thick (n=10 -> cap 100): 5*4*3 = 60 fits but 5*4*3*3 does not, so it EVICTS
        #         vol_window and seasonal_clip — two knobs this lane has actually adopted
        #         from (2026-08-19 took vol_window=13, seasonal_clip=0.15).
        # And the objective is realised dollars over ~10 events, which has no power to see
        # the +0.31 nats/event this parameter is about. `param_space.CANDIDATES` searches
        # it under DSR deflation on 52 events instead; that is the lane that can judge it.
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


def probe(conn, series: str, window_start: datetime) -> tuple[dict, list[str], list[str]]:
    """(space, live keys, dead keys) — the expensive half of `build`, done once.

    `live_keys` replays the model on probe events to find which knobs actually move its
    output, which is several model evaluations. `build` is called repeatedly with different
    sample sizes (the re-narrow loop, and `grid_ladder`), and every one of those calls used
    to redo this. Nothing in it depends on `n_events`.
    """
    space = SPACES.get(MODULE_OF[series], {})
    if not space:
        return {}, [], []
    pre = settled_events(conn, series, limit=8, before=window_start)
    fn = _predict_fn(series)
    live, dead = live_keys(conn, series, fn, {k: v[0] for k, v in space.items()}, pre)
    return space, live, dead


def build(conn, series: str, window_start: datetime,
          n_events: int | None = None, probed=None) -> tuple[list[dict], dict]:
    """Live-key-filtered grid, default at index 0. Probe events close BEFORE the
    window so grid design never sees the evaluation sample (the grid75 protocol).

    `n_events` is the size of the sample the grid will be SCORED on. Only its count is
    used — never an outcome — so the grid75 protocol still holds: the design cannot be
    tuned to the evaluation, it can only be made narrower because the evaluation is small.
    Passing None keeps the pre-2026-08-21 behaviour (the static CAP alone) and is what
    the study scripts use to reproduce the original spaces.

    `probed` is a cached `probe()` result. It changes nothing about the grid — same inputs,
    same output — and exists only so repeated calls do not replay the model each time.
    """
    spec, live, dead = probed if probed is not None else probe(conn, series, window_start)
    if not spec:
        return [{}], {"note": "no space"}
    cap = CAP[series]
    scap = None
    if n_events is not None:
        scap = sample_cap(n_events)
        cap = min(cap, scap)
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


def grid_ladder(conn, series: str, window_start: datetime,
                probed=None) -> tuple[list[dict], list[dict]]:
    """(every distinct grid the cap ladder can produce, the union of their candidates).

    The synthetic sample is generated weekly and read daily, and `_objective` will not blend
    a grid the generation job did not fully cover. So the job cannot simply score "today's
    grid" — by the time it is read, `n_eff` may have moved and the morning lane may be
    holding a NARROWER grid, whose members are not a subset of the wider one. Narrowing
    drops whole KEYS, so `{"vol_window": 26}` and `{"vol_window": 26, "clip": 0.15}` are
    different dictionaries and hash differently even when the second's clip is the default.

    The set of grids is small and enumerable, which is what makes covering all of them cheap
    rather than clever: `build` chooses keys greedily widest-first until the cap is reached,
    so as the cap rises the chosen set only ever grows, and there are at most one grid per
    key plus the empty one. Scoring their union costs about 1.5x the widest grid alone and
    makes the stored sample independent of whatever lambda turns out to be — which matters
    because lambda is still being measured, and a sample keyed to today's guess at it would
    be worthless the moment the measurement lands.
    """
    probed = probed if probed is not None else probe(conn, series, window_start)
    grids, seen, union, uhash = [], set(), [], set()
    n = 0.0
    while True:
        g, _ = build(conn, series, window_start, n_events=n, probed=probed)
        h = grid_hash(g)
        if h not in seen:
            seen.add(h)
            grids.append(g)
            for p in g:
                ph = set_hash(p)
                if ph not in uhash:
                    uhash.add(ph)
                    union.append(p)
        if sample_cap(n) >= CAP[series]:
            break
        n += 0.5
        if n > 40:                # sample_cap(40) is astronomically above any CAP
            break
    return grids, union


def _fingerprint(conn, series: str, now: datetime) -> str:
    lo = now - timedelta(days=WINDOW_DAYS)
    evs = [e for e in _ps.quotable_events(conn, series, before=now)
           if e["close_ts"] >= lo]
    # model version is part of the key: a model bump (e.g. cpi/0.2.0 -> 0.3.0 nowcast
    # anchor) changes every replayed score, and an events-only fingerprint would keep
    # serving the OLD model's argmin until the next settle happened to land.
    ver = getattr(importlib.import_module(SERIES_DISPATCH[series][0]), "VERSION", "?")
    # ...and so is the synthetic sample, for exactly the same reason. `regen` runs weekly
    # and lambda is measured on its own schedule; both change `n_eff`, hence the grid width
    # and the objective. On a monthly market a new settlement is up to 31 days away, so
    # without this the sample this job paid to generate would sit unread for a month — and
    # the day lambda first lands, NOTHING would rescore. `synth` is a cheap read (one row
    # from each of two indexed tables) on a path that already runs a `quotable_events` scan.
    syn = conn.execute("SELECT run_id FROM synth_runs WHERE series=?"
                       " ORDER BY built_ts DESC LIMIT 1", (series,)).fetchone()
    lam, _ = synth_lambda(conn, series)
    return (f"{len(evs)}:{max((e['close_ts'].isoformat() for e in evs), default='-')}"
            f":{ver}:{(syn['run_id'] if syn else '-')}:{lam:.4f}")


def _last_log(conn, series: str):
    return conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='param_argmin' AND series=?"
        " ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()


def universe(conn, series: str, now: datetime) -> list[dict]:
    """The quotable events in the trailing window, keyed. Only its COUNT sizes the grid."""
    uni = [{**e, "key": kalshi_period_to_key(e["tok"]), "close": e["close_ts"]}
           for e in _ps.quotable_events(conn, series, before=now)
           if e["close_ts"] >= now - timedelta(days=WINDOW_DAYS)]
    return [e for e in uni if e["key"]]


def resolve_grid(conn, series: str, lo: datetime, uni: list[dict],
                 synth_weight: float = 0.0, log=None, probed=None):
    """(grid, kept, mat, report) — the grid the sample can actually support, and its scores.

    Shared by the morning lane and by the regeneration job (`synth/regen.py`) precisely so
    the two cannot disagree about which grid was scored: the job stores per-candidate means
    under `set_hash`, and `_objective` refuses to blend a grid it does not fully cover. If
    the job resolved the grid by its own reasoning, every drift between the two would
    surface as a silent no-blend rather than as a bug.

    Narrowing only, never widening. A smaller grid may well keep MORE events (fewer sets to
    fail), but re-widening on that would make the sample size a function of the grid that
    the grid is a function of. Only the COUNT crosses over either way, never an outcome, so
    the grid75 protocol holds exactly as it did before.
    """
    probed = probed if probed is not None else probe(conn, series, lo)
    n_eff = len(uni) + synth_weight
    grid, rep = build(conn, series, lo, n_events=n_eff, probed=probed)
    rep["n_eff"] = round(n_eff, 2)
    if len(grid) < 2:
        return grid, [], [], rep
    kept, mat, _det = _ps.score_matrix(conn, series, grid, uni, log=log)
    if not kept:
        return grid, [], [], rep
    # ── the gate must bind on the sample the argmin RANKS on, not the one it was offered.
    # `quotable_events` and `event_pnl` do not agree on what is scoreable: an event the
    # live rule would not have traded at all — a skill-BLOCKED series, a disabled one —
    # is quotable and unscoreable, and `score_matrix` drops it for every candidate at once.
    # Measured 2026-08-21: KXJOBLESSCLAIMS has been blocked since 07-09, so its universe of
    # 10 is a scored sample of 4, and sizing the grid on 10 let 91 candidate sets be ranked
    # on 4 events — 1.50 sd of selection bias, above the 1.0 the gate exists to enforce and
    # worse than the KXPAYROLLS case that motivated it. Sizing on the universe was the
    # original shape of the gate and this is the hole in it.
    for _ in range(3):
        n_eff = len(kept) + synth_weight
        if len(grid) - 1 <= sample_cap(n_eff):
            break
        narrowed, rep = build(conn, series, lo, n_events=n_eff, probed=probed)
        rep.update({"n_eff": round(n_eff, 2), "renarrowed_from": len(grid),
                    "renarrow_reason": (f"{len(uni)} quotable but {len(kept)} scoreable "
                                        f"— resized on the scored sample")})
        if len(narrowed) >= len(grid):
            break
        grid = narrowed
        if len(grid) < 2:
            return grid, kept, mat, rep
        kept, mat, _det = _ps.score_matrix(conn, series, grid, uni, log=log)
        if not kept:
            return grid, [], [], rep
    return grid, kept, mat, rep


def rescore(conn, series: str, now: datetime, log=None) -> dict | None:
    lo = now - timedelta(days=WINDOW_DAYS)
    # the universe is built FIRST so the grid can be sized to it (see `sample_cap`).
    uni = universe(conn, series, now)
    if not uni:
        return None
    syn, srep = read_synth(conn, series, now)
    grid, kept, mat, grep_ = resolve_grid(
        conn, series, lo, uni, syn.weight if syn else 0.0, log=log)
    grep_["synth"] = srep
    if len(grid) < 2:
        # Not an error and not "no sample": the sample exists, it is just too small to
        # tell K candidates apart. Returned as a result so `daily` can log the reason
        # instead of silently looking like a market with no data.
        return {"grid": grid, "grid_report": grep_, "n_events": len(kept) or len(uni),
                "best_idx": 0, "best_params": {}, "pnl_best": None,
                "pnl_default": None, "gated": True}
    if not kept:
        return None
    totals = [sum(row[j] for row in mat) for j in range(len(grid))]
    obj, blended = _objective(totals, len(kept), grid, syn, srep)
    best = max(range(len(grid)), key=lambda j: obj[j])
    return {"grid": grid, "grid_report": grep_, "n_events": len(kept),
            "best_idx": best, "best_params": grid[best], "blended": blended,
            "pnl_best": round(totals[best], 2), "pnl_default": round(totals[0], 2)}


def _objective(totals: list[float], n_real: int, grid: list[dict],
               syn: SynthSample | None, srep: dict) -> tuple[list[float], bool]:
    """Per-event mean IMPROVEMENT over the default, real and synthetic pooled by weight.

    Improvement over index 0, not level, and that is the load-bearing choice. §5c measured
    the synthetic world to be uniformly EASIER to trade than reality (`mean|z_y|` 0.725 vs
    0.964), so synthetic PnL levels are inflated; a level shift common to every candidate
    cancels in the difference and only disagreement about WHICH set is better survives.

        obj[j] = (n_real * (mr[j] - mr[0]) + lambda * n_synth * (ms[j] - ms[0])) / n_eff

    At lambda = 0 this is `mr[j] - mr[0]`, whose argmax is the argmax of `totals` — the
    pre-S6 behaviour exactly, which is what makes an unmeasured or zero lambda a no-op
    rather than a silent change of objective.

    The synthetic side is used ONLY if it covers EVERY candidate in the grid. A partial
    blend would compare candidates on different samples, which is the same bias
    `score_matrix`'s all-sets keep rule exists to remove.
    """
    mr = [t / n_real for t in totals]
    base = [m - mr[0] for m in mr]
    if syn is None:
        return base, False
    hashes = [set_hash(p) for p in grid]
    missing = [h for h in hashes if h not in syn.means]
    if missing:
        srep["skipped"] = (f"synthetic run covers {len(hashes) - len(missing)}/"
                           f"{len(hashes)} of today's grid — blending a subset would "
                           "compare candidates on different samples")
        return base, False
    ms = [syn.means[h] for h in hashes]
    w_r, w_s = float(n_real), syn.weight
    srep["blend_weight_real"] = round(w_r / (w_r + w_s), 3)
    return [(w_r * base[j] + w_s * (ms[j] - ms[0])) / (w_r + w_s)
            for j in range(len(grid))], True


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
            grep_ = r["grid_report"]
            srep = grep_.get("synth", {})
            # A set chosen with synthetic evidence and one chosen without are different
            # decisions and the change log has to say which, because `param_select.history`
            # is exported to the frontend and is the record a human reads back.
            syn_note = (f" Blended with {srep.get('n_synth')} DFM synthetic events at "
                        f"lambda={srep.get('lambda'):.3f} (run {srep.get('run_id')}, "
                        f"{srep.get('age_days')}d old, real weight "
                        f"{srep.get('blend_weight_real')}); n_eff={grep_.get('n_eff')}."
                        ) if r.get("blended") else \
                       f" Real events only ({srep.get('skipped', 'no synthetic sample')})."
            set_manual(conn, series, r["best_params"],
                       note=(f"daily argmin {now.date()}: window {WINDOW_DAYS}d, "
                             f"n_events={r['n_events']}, sets={len(r['grid'])}, "
                             f"pnl {r['pnl_default']:+.2f} -> {r['pnl_best']:+.2f}. "
                             "Raw argmin per standing user policy 2026-08-11; the "
                             "DSR gate's objection (n below MIN_OBS) is on record. "
                             f"Search width capped at {grep_.get('cap')} "
                             f"by the {grep_.get('n_eff')}-event sample gate." + syn_note))
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
                         "blended": bool(r.get("blended")),
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
