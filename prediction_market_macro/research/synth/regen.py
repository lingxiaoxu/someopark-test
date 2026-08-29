"""research/synth/regen.py — keep the synthetic sample current, and store it (S7).

`param_argmin` may not import a generator. The morning pass has to finish in the minutes
before the board trades, and loading torch to re-derive a sample that changes on a monthly
timescale would be paying a daily cost for a weekly quantity. So the work is split:

    this job (weekly)   generate worlds -> score the grid -> write `synth_scores`
    param_argmin (daily)  read per-candidate means -> blend at the measured lambda

The two halves are joined by `set_hash`, and the join is checked rather than assumed:
`_objective` refuses to blend a grid this job did not fully cover, and says so. That makes
the failure mode of a drifted grid "today's argmin is real-sample-only", which is exactly
the pre-S6 behaviour, rather than a comparison of candidates on different samples.

## Which markets

The monthly ones, derived from the panel frequency rather than tabulated. That is not a
cost-saving choice, it is the scope of the problem: the weekly markets settle 10-11 times
in a 75-day window and the gate never bites on them, so a synthetic sample would buy them
nothing they do not already have. The weekly series are still GENERATED — by
`synth/calibrate.py`, on request — because they are the only place lambda can be measured
against a real sample at all. Measurement and production are different jobs on purpose.

## Which cutoff

`now`, so the paths run FORWARD from today's macro state. The alternative — splicing at the
start of the trailing real window, which is what `calibrate` does — produces synthetic
events that overlay the real evaluation sample, and that is right for measuring agreement
because the two are then comparable. It is wrong for production: the parameters being
chosen will be used on the next month, the user's requirement is that the synthetic draws
sit close to "当前环境和当前这个赌注" (the current environment and the current bet's own
numbers), and conditioning the generator on today's anchor is what delivers that. Every
generated event is therefore genuinely out-of-sample — it has not happened yet.

## What is written where

    macro.db  synth_runs    one row per (series, build) — provenance, never the data
              synth_scores  one row per candidate — the mean the daily lane reads
    data/synth/<series>/     the world dbs themselves, gitignored and regenerable

The worlds are kept rather than deleted after scoring. They are the only way to re-score a
grid that drifted mid-week without paying for generation again, and `rescore_latest` exists
for exactly that. Previous runs' worlds are removed when a new run succeeds, so the
footprint is one generation deep, not one per week.
"""
from __future__ import annotations

import json
import shutil
import sqlite3

import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prediction_market_macro.research import param_argmin as PA
from prediction_market_macro.research.synth import book as B
from prediction_market_macro.research.synth import build as BD
from prediction_market_macro.research.synth import generator as G
from prediction_market_macro.research.synth import panel as P
from prediction_market_macro.research.synth import worlds as W

UTC = timezone.utc

# How long a donor book may be reused. The book is the empirical distribution of real market
# quotes conditioned on how surprising the outcome was (§5b); it moves with the venue's
# liquidity and its ladder conventions, not with the macro state, so it is stable on a much
# longer timescale than the worlds it prices. Re-measuring costs a full pass over every
# quotable event in the db, which is why it is cached at all.
DONOR_MAX_AGE_DAYS = 30

# Paths per series. Eight is what §5 measured lambda on; the sample it produces is
# `n_paths * (horizon - 1)` events, so eight monthly paths is ~88 synthetic settlements.
N_PATHS = 8


def targets() -> list[str]:
    """The monthly generatable series — the ones the sample gate actually binds on."""
    return [s for s, st in BD.SETTLES.items() if P.PANELS[st.panel].freq == "MS"]


def _root(s) -> Path:
    return Path(s.db_path).parent / "synth"


def donors(src: sqlite3.Connection, root: Path, *, now: datetime, log=None) -> list[B.Donor]:
    """The donor book, measured once and cached. Pooled across ALL series on purpose.

    A donor is a real (surprise, book) pair. Pooling them is what lets a monthly market with
    three settlements a quarter be priced from a book measured on thousands of real quotes,
    and §5b measured the descriptor that indexes them — standardized surprise — to travel
    across series far better than the raw levels do.
    """
    path = root / "donors.json"
    if path.exists():
        age = now - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if age.days <= DONOR_MAX_AGE_DAYS:
            got = B.load(path)
            if log:
                log(f"  donors: {len(got)} reused (book is {age.days}d old)")
            return got
    got = B.measure(src, log=log)
    B.save(got, path)
    if log:
        log(f"  donors: {len(got)} measured")
    return got


def _store(conn, series: str, built: BD.BuildResult, grid: list[dict],
           kept_s: list, mat_s: list[list[float]], *, now: datetime,
           n_real: int, keep_runs: int = 3) -> str:
    """Write one run and its per-candidate means, then prune older runs for this series."""
    run_id = f"{series}_{now.strftime('%Y%m%dT%H%M%S')}"
    ncol = len(grid)
    means = [sum(row[j] for row in mat_s) / len(mat_s) for j in range(ncol)]
    sds = []
    for j in range(ncol):
        mu = means[j]
        var = sum((row[j] - mu) ** 2 for row in mat_s) / max(len(mat_s) - 1, 1)
        sds.append(var ** 0.5)
    conn.execute(
        "INSERT OR REPLACE INTO synth_runs(run_id, series, cutoff_ts, splice_ts, built_ts,"
        " n_paths, n_events, grid_hash, meta_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (run_id, series, built.cutoff.isoformat(), built.splice.isoformat(),
         now.isoformat(), len(built.worlds), len(kept_s), PA.grid_hash(grid),
         json.dumps({"coverage": built.coverage, "anchor": str(built.anchor),
                     "n_generated": built.n_synth, "n_scored": len(kept_s),
                     "n_real_at_build": n_real,
                     "worlds": [str(p) for p in built.worlds],
                     "generator": built.meta.get("generator", {}),
                     "panel": built.meta.get("panel")}, default=str)))
    for j, p in enumerate(grid):
        conn.execute(
            "INSERT OR REPLACE INTO synth_scores(run_id, set_hash, set_json, set_idx,"
            " n_events, mean_pnl, sd_pnl) VALUES(?,?,?,?,?,?,?)",
            (run_id, PA.set_hash(p), json.dumps(p, sort_keys=True), j, len(kept_s),
             float(means[j]), float(sds[j])))
    stale = [r[0] for r in conn.execute(
        "SELECT run_id FROM synth_runs WHERE series=? ORDER BY built_ts DESC LIMIT -1"
        " OFFSET ?", (series, keep_runs))]
    for rid in stale:
        conn.execute("DELETE FROM synth_scores WHERE run_id=?", (rid,))
        conn.execute("DELETE FROM synth_runs WHERE run_id=?", (rid,))
    conn.commit()
    return run_id


def one(conn, src: sqlite3.Connection, series: str, *, root: Path, book: list[B.Donor],
        now: datetime, n_paths: int = N_PATHS, seed: int = 0,
        paths=None, log=None) -> dict:
    """Generate, score and store one series. `conn` is live macro.db; `src` is the snapshot.

    What gets scored is the LADDER UNION, not today's grid. The morning lane's grid depends
    on `n_eff = n_real + lambda * n_synth`, and both of those move — a settlement lands, or
    lambda is re-measured — between this job running and the sample being read. Scoring one
    grid would then leave `_objective` refusing to blend the very sample this job paid to
    generate, and refusing it silently-but-logged, which is the least useful place for a
    correct refusal. `PA.grid_ladder` enumerates every grid the cap ladder can produce and
    their union covers all of them.

    The union is not the widest grid. Narrowing drops whole KEYS, so `{"vol_window": 26}`
    and `{"vol_window": 26, "clip": 0.15}` are different dicts with different `set_hash`es
    and a narrow grid is not a subset of a wide one. Measured on KXPAYROLLS: rungs of width
    1/3/4/5/9/13/17/33/49/97 union to 222, a 2.3x premium over scoring the widest alone.
    That is the price of the sample being usable at whatever lambda turns out to be, paid
    once a week in a 3am job rather than every morning before the board trades.
    """
    say = log or (lambda *_a, **_k: None)
    lo = now - timedelta(days=PA.WINDOW_DAYS)
    out_dir = root / series
    prev = sorted(out_dir.glob("world_*.db")) if out_dir.exists() else []

    built = BD.build(src, series, now, donors=book, out_dir=out_dir,
                     n_paths=n_paths, seed=seed, paths=paths, log=say)
    say(f"  {series}: {built.n_synth} synthetic events from {len(built.worlds)} paths")
    # Worlds from the previous run, now superseded. Removed only after the new ones exist,
    # so a crash mid-generation leaves the old sample in place rather than none at all.
    for old in prev:
        if old not in built.worlds:
            old.unlink(missing_ok=True)

    grids, union = PA.grid_ladder(conn, series, lo)
    say(f"  {series}: {len(grids)} reachable grid(s), "
        f"{'/'.join(str(len(g)) for g in grids)} wide -> union {len(union)} candidates")
    if len(union) < 2:
        return {"series": series, "stored": False, "n_synth": built.n_synth,
                "reason": "the widest reachable grid is defaults-only — no candidate for a "
                          "synthetic sample to rank"}
    kept_s, mat_s = BD.score_matrix(built.events, union, log=say)
    if len(kept_s) < 3:
        return {"series": series, "stored": False, "n_synth": built.n_synth,
                "reason": f"only {len(kept_s)}/{built.n_synth} synthetic events replay on "
                          "every candidate — too few to average"}
    lam, lrep = PA.synth_lambda(conn, series)
    run_id = _store(conn, series, built, union, kept_s, mat_s, now=now,
                    n_real=len(PA.universe(conn, series, now)))
    return {"series": series, "stored": True, "run_id": run_id, "k": len(union),
            "grids": [len(g) for g in grids], "n_synth": len(kept_s),
            "n_generated": built.n_synth, "lam": lam, "lam_source": lrep.get("source"),
            "weight": round(lam * len(kept_s), 2), "coverage": built.coverage}


def run(conn, s, *, series: list[str] | None = None, now: datetime | None = None,
        n_paths: int = N_PATHS, seed: int = 0, log=print) -> dict:
    """The weekly pass over every monthly market. Returns {series: status}."""
    now = now or datetime.now(UTC)
    root = _root(s)
    root.mkdir(parents=True, exist_ok=True)
    # Generation reads from a SNAPSHOT, never from the live connection. `build` hands the
    # world connection to real prediction models, and a model that ever learned to write
    # would then write into macro.db. It writes into a copy instead, by construction.
    snap = W.snapshot(s.db_path, root / "snapshot.db")
    src = sqlite3.connect(snap)
    src.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    try:
        book = donors(src, root, now=now, log=log)
        for name in (series or targets()):
            try:
                r = one(conn, src, name, root=root, book=book, now=now,
                        n_paths=n_paths, seed=seed, log=log)
            except Exception as exc:                                   # noqa: BLE001
                # One market's generator failing is not a reason to leave the other six
                # without a sample; the daily lane already treats an absent run as
                # "real-sample-only" and will simply keep doing that for this one.
                out[name] = f"FAIL {type(exc).__name__}: {str(exc)[:160]}"
                if log:
                    log(f"  {name}: {out[name]}")
                continue
            out[name] = (f"ok run={r['run_id']} n_synth={r['n_synth']} k={r['k']} "
                         f"weight={r['weight']}" if r["stored"]
                         else f"skipped: {r['reason']}")
    finally:
        src.close()
    return out


WEEKLY_SERIES: dict[str, str] = {
    "KXJOBLESSCLAIMS": "claims_weekly",
    "KXWTIW": "energy_weekly",
    "KXNATGASW": "energy_weekly",
}


def run_weekly(conn, s, *, now: datetime | None = None, n_paths: int = N_PATHS,
               seed: int = 0, log=print) -> dict:
    """The coupled weekly pass (#214, PR-20): one joint draw, three series' worlds.

    The default `run` loop is monthly-only by construction (`targets()` filters on freq),
    so the weekly series were only ever generated by explicit calls — independently, which
    is exactly the defect §5d measured: world `i` of KXJOBLESSCLAIMS and world `i` of
    KXWTIW/KXNATGASW never shared a draw, and every cross-series correlation in the sample
    was 0 by construction. This pass fits both weekly panels once, draws them JOINTLY
    through `G.sample_coupled` with the frozen `G.WEEKLY_COUPLING`, and hands each series
    its panel's slice of the SAME coupled draw via `build(paths=...)`.

    The fit is deliberately the one PR-20's calibration ran on — `GenConfig` defaults with
    `fit_local(k=120)` — NOT `build`'s per-series `epochs=1500`: the frozen rhos are only
    valid together with the fit regime whose attenuation slope they were solved against.

    KXWTIW and KXNATGASW receive the identical energy paths, which is not new: their
    separate builds already produced identical paths (same panel, same seed, same cutoff);
    the prepass preserves that while adding the cross-panel correlation.
    """
    now = now or datetime.now(UTC)
    root = _root(s)
    root.mkdir(parents=True, exist_ok=True)
    snap = W.snapshot(s.db_path, root / "snapshot.db")
    src = sqlite3.connect(snap)
    src.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    try:
        book = donors(src, root, now=now, log=log)
        prep = {}
        for pname in ("claims_weekly", "energy_weekly"):
            pdata = P.build(src, pname, now)
            anchor = pdata.inc.index[-1]
            c_raw = P.condition_row(pdata.levels, pdata.inc, pdata.spec, anchor)
            gen = G.Generator.fit_local(pdata, G.GenConfig(panel=pname, seed=seed + 7),
                                        c_raw, k=120)
            prep[pname] = (pdata, gen, c_raw, pdata.levels.loc[anchor])
        (pc, gc, cc, ac) = prep["claims_weekly"]
        (pe, ge, ce, ae) = prep["energy_weekly"]
        inc_c, inc_e = G.sample_coupled(gc, ge, cc, ce, n_paths,
                                        rho=G.WEEKLY_COUPLING, seed=seed + 3,
                                        ar_phi_b=G.PANEL_AR.get("energy_weekly"))
        lv = {"claims_weekly": P.integrate_paths(inc_c, ac, pc.spec, gc.lattice),
              "energy_weekly": P.integrate_paths(inc_e, ae, pe.spec, ge.lattice)}
        if log:
            log(f"  coupled prepass: rho={G.WEEKLY_COUPLING} n_paths={n_paths}")
        for name, pname in WEEKLY_SERIES.items():
            try:
                r = one(conn, src, name, root=root, book=book, now=now,
                        n_paths=n_paths, seed=seed, paths=lv[pname], log=log)
            except Exception as exc:                                   # noqa: BLE001
                out[name] = f"FAIL {type(exc).__name__}: {str(exc)[:160]}"
                if log:
                    log(f"  {name}: {out[name]}")
                continue
            out[name] = (f"ok run={r['run_id']} n_synth={r['n_synth']} k={r['k']} "
                         f"weight={r['weight']}" if r["stored"]
                         else f"skipped: {r['reason']}")
    finally:
        src.close()
    return out


PINNED_MONTHLY: dict[str, tuple[str, str, str]] = {
    # monthly panel -> (pinned column, hub weekly panel, hub column). PR-26 (#214).
    "labor_monthly": ("claims", "claims_weekly", "claims"),
    "inflation_monthly": ("gas_retail", "energy_weekly", "gas_retail"),
}


def _month_means(levels_real, fwd_w, drawn, n_paths):
    """(year, month) -> per-path month-mean weekly levels, plus which months are FULLY
    drawn. The completeness rule is the registered one: every W-SAT of the CALENDAR month
    must be present (real or drawn); a month truncated at the horizon is incomplete. The
    pin domain (PR-26) is the fully-drawn months only — a pin built from real weeks is a
    cross-path constant, and PR-25 measured what that does to the panel's variance."""
    import pandas as pd
    have, drawn_stamps = {}, set()
    for t, v in levels_real.items():
        have[pd.Timestamp(t)] = np.full(n_paths, float(v))
    for wi, t in enumerate(fwd_w):
        have[pd.Timestamp(t)] = drawn[:, wi]
        drawn_stamps.add(pd.Timestamp(t))
    out, fully_drawn = {}, set()
    for ym in {(t.year, t.month) for t in have}:
        start = pd.Timestamp(ym[0], ym[1], 1)
        stamps = pd.date_range(start, start + pd.offsets.MonthEnd(1), freq="W-SAT")
        if len(stamps) and all(t in have for t in stamps):
            out[ym] = np.mean([have[t] for t in stamps], axis=0)
            if all(t in drawn_stamps for t in stamps):
                fully_drawn.add(ym)
    return out, fully_drawn


def run_joint(conn, s, *, now: datetime | None = None, n_paths: int = N_PATHS,
              seed: int = 0, log=print) -> dict:
    """The ten-series joint pass (PR-26): one coupled weekly draw, everything downstream.

    Order and provenance, because every piece was judged separately:
      1. weekly pair drawn ONCE through `sample_coupled` with the frozen PR-20 rho and
         PR-24's `ar_phi_b` — the three weekly series' worlds share draw i;
      2. labor/inflation drawn through `sample_pinned` with their PR-23 phi, the
         `claims`/`gas_retail` columns pinned to the weekly draw's month-means on the
         FULLY-DRAWN months (PR-26's domain — real-week months are deliberately not
         pinned, see PR-25's verdict);
      3. every series' worlds built from its panel's slice of that one joint draw via
         `build(paths=...)`.

    Replaces both `run` (whose whole target list is the labor/inflation series) and
    `run_weekly` in the refresh lane; both remain callable for explicit use.
    """
    import pandas as pd
    now = now or datetime.now(UTC)
    root = _root(s)
    root.mkdir(parents=True, exist_ok=True)
    snap = W.snapshot(s.db_path, root / "snapshot.db")
    src = sqlite3.connect(snap)
    src.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    try:
        book = donors(src, root, now=now, log=log)
        prep = {}
        for pname in ("claims_weekly", "energy_weekly"):
            pdata = P.build(src, pname, now)
            anchor = pdata.inc.index[-1]
            c_raw = P.condition_row(pdata.levels, pdata.inc, pdata.spec, anchor)
            gen = G.Generator.fit_local(pdata, G.GenConfig(panel=pname, seed=seed + 7),
                                        c_raw, k=120)
            prep[pname] = (pdata, gen, c_raw, pdata.levels.loc[anchor])
        (pc, gc, cc, ac) = prep["claims_weekly"]
        (pe, ge, ce, ae) = prep["energy_weekly"]
        inc_c, inc_e = G.sample_coupled(gc, ge, cc, ce, n_paths,
                                        rho=G.WEEKLY_COUPLING, seed=seed + 3,
                                        ar_phi_b=G.PANEL_AR.get("energy_weekly"))
        lv = {"claims_weekly": P.integrate_paths(inc_c, ac, pc.spec, gc.lattice),
              "energy_weekly": P.integrate_paths(inc_e, ae, pe.spec, ge.lattice)}
        fwd_w = P.forward_periods(pc.spec, pc.inc.index[-1], pc.spec.horizon)
        if log:
            log(f"  joint prepass: rho={G.WEEKLY_COUPLING} n_paths={n_paths}")

        for name, pname in WEEKLY_SERIES.items():
            try:
                r = one(conn, src, name, root=root, book=book, now=now,
                        n_paths=n_paths, seed=seed, paths=lv[pname], log=log)
                out[name] = (f"ok run={r['run_id']} n_synth={r['n_synth']} k={r['k']} "
                             f"weight={r['weight']}" if r["stored"]
                             else f"skipped: {r['reason']}")
            except Exception as exc:                               # noqa: BLE001
                out[name] = f"FAIL {type(exc).__name__}: {str(exc)[:160]}"
                if log:
                    log(f"  {name}: {out[name]}")

        for mpanel, (col, hub_panel, hub_col) in PINNED_MONTHLY.items():
            pdata = P.build(src, mpanel, now)
            anchor = pdata.inc.index[-1]
            c_raw = P.condition_row(pdata.levels, pdata.inc, pdata.spec, anchor)
            gen = G.Generator.fit_local(
                pdata, G.GenConfig(panel=mpanel, seed=seed + 7,
                                   ar_phi=G.PANEL_AR.get(mpanel)), c_raw, k=120)
            hub_p, hub_lv = prep[hub_panel][0], lv[hub_panel]
            hub_j = [c.name for c in hub_p.spec.gen_columns].index(hub_col)
            mm, _fully_drawn = _month_means(hub_p.levels[hub_col].dropna(), fwd_w,
                                           hub_lv[:, :, hub_j], n_paths)
            cols = [c.name for c in pdata.spec.gen_columns]
            J, d = cols.index(col), pdata.spec.d
            fwd_m = P.forward_periods(pdata.spec, anchor, pdata.spec.horizon)
            pins = {}
            for mi, t in enumerate(fwd_m):
                ym = (t.year, t.month)
                pm_ = t - pd.offsets.MonthBegin(1)
                # PR-28: ALL calendar-complete months. Fully-drawn pins carry cross-path
                # variance (PR-26's class); real/mixed pins condition on published truth
                # (the information-injection class PR-28 judged) — together the level
                # identity is exact from the anchor on.
                if ym in mm and (pm_.year, pm_.month) in mm:
                    pins[mi * d + J] = np.log(mm[ym]) - np.log(mm[(pm_.year, pm_.month)])
            inc_m = G.sample_pinned(gen, c_raw, pins, n_paths, seed=seed + 3)
            lv_m = P.integrate_paths(inc_m, pdata.levels.loc[anchor], pdata.spec,
                                     gen.lattice)
            if log:
                log(f"  {mpanel}: pinned {sorted(str(fwd_m[c // d].date()) for c in pins)}")
            for series, st in BD.SETTLES.items():
                if st.panel != mpanel:
                    continue
                try:
                    r = one(conn, src, series, root=root, book=book, now=now,
                            n_paths=n_paths, seed=seed, paths=lv_m, log=log)
                    out[series] = (f"ok run={r['run_id']} n_synth={r['n_synth']} "
                                   f"k={r['k']} weight={r['weight']}" if r["stored"]
                                   else f"skipped: {r['reason']}")
                except Exception as exc:                           # noqa: BLE001
                    out[series] = f"FAIL {type(exc).__name__}: {str(exc)[:160]}"
                    if log:
                        log(f"  {series}: {out[series]}")
    finally:
        src.close()
    return out


def remeasure_weekly_lambda(conn, s, *, now: datetime | None = None, log=None) -> dict:
    """Re-measure the weekly series' lambda under the standing §6 rule, weekly (PR-27).

    The 2026-08-21 and 2026-08-29 measurements were both manual; without this step the
    weekly zeros could only ever be refreshed by someone remembering to. Per-series rows
    only (`pooled=False`): the '*' row the monthly markets read stays owned by the
    original measurement and the S5-WF machinery — a weekly-only re-pool would let three
    series' lower bounds rewrite a number seven other markets trade on.

    A series the measurement refuses (too few real events, grid too narrow) keeps its
    previous row — refusal is not evidence.
    """
    from prediction_market_macro.research.synth import calibrate as C
    now = now or datetime.now(UTC)
    root = _root(s)
    root.mkdir(parents=True, exist_ok=True)
    snap = W.snapshot(s.db_path, root / "snapshot.db")
    src = sqlite3.connect(snap)
    src.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    try:
        book = donors(src, root, now=now, log=log)
        results = []
        for series in WEEKLY_SERIES:
            try:
                r = C.run(src, series, now, donors=book,
                          out_dir=root / "lambda_worlds", log=log)
            except Exception as exc:                               # noqa: BLE001
                out[series] = f"FAIL {type(exc).__name__}: {str(exc)[:120]}"
                continue
            if r is None:
                out[series] = "refused (insufficient sample) — previous row stands"
            else:
                results.append(r)
                out[series] = f"lam_lo={max(r.lam_lo, 0.0):.4f} point={r.lam_point:.4f}"
        if results:
            C.persist(conn, results, now=now, log=log, pooled=False)
            conn.commit()
    finally:
        src.close()
    return out


def portfolio_corr(conn, s, *, now: datetime | None = None, n_paths: int = 64,
                   seed: int = 0, log=None) -> dict:
    """The correlated-cluster matrix (PR-29): 10 series' settlement summaries, one joint draw.

    A dedicated `n_paths=64` pass through the production samplers — coupled+AR weekly,
    pinned monthly — WITHOUT writing worlds (draws cost seconds; §5f measured that 8 is
    hopeless and 64 resolves). Each series' summary is its settle column's total increment
    over its own horizon; the pairwise correlation across the 64 joint draws lands in
    `data/synth/portfolio_corr.json`, which `ops/risk.py` reads for the one-sided
    `per_corr_cluster_usd` cap. File absent -> cap inert; this function failing therefore
    degrades to today's behavior, never to a wrong number.
    """
    import pandas as pd
    now = now or datetime.now(UTC)
    root = _root(s)
    root.mkdir(parents=True, exist_ok=True)
    snap = W.snapshot(s.db_path, root / "snapshot.db")
    src = sqlite3.connect(snap)
    src.row_factory = sqlite3.Row
    try:
        prep = {}
        for pname in ("claims_weekly", "energy_weekly"):
            pdata = P.build(src, pname, now)
            anchor = pdata.inc.index[-1]
            c_raw = P.condition_row(pdata.levels, pdata.inc, pdata.spec, anchor)
            gen = G.Generator.fit_local(pdata, G.GenConfig(panel=pname, seed=seed + 7),
                                        c_raw, k=120)
            prep[pname] = (pdata, gen, c_raw)
        (pc, gc, cc), (pe, ge, ce) = prep["claims_weekly"], prep["energy_weekly"]
        inc_c, inc_e = G.sample_coupled(gc, ge, cc, ce, n_paths,
                                        rho=G.WEEKLY_COUPLING, seed=seed + 3,
                                        ar_phi_b=G.PANEL_AR.get("energy_weekly"))
        lv = {"claims_weekly": P.integrate_paths(
                  inc_c, pc.levels.loc[pc.inc.index[-1]], pc.spec, gc.lattice),
              "energy_weekly": P.integrate_paths(
                  inc_e, pe.levels.loc[pe.inc.index[-1]], pe.spec, ge.lattice)}
        fwd_w = P.forward_periods(pc.spec, pc.inc.index[-1], pc.spec.horizon)
        incs = {"claims_weekly": inc_c, "energy_weekly": inc_e}
        for mpanel, (col, hub_panel, hub_col) in PINNED_MONTHLY.items():
            pdata = P.build(src, mpanel, now)
            anchor = pdata.inc.index[-1]
            c_raw = P.condition_row(pdata.levels, pdata.inc, pdata.spec, anchor)
            gen = G.Generator.fit_local(
                pdata, G.GenConfig(panel=mpanel, seed=seed + 7,
                                   ar_phi=G.PANEL_AR.get(mpanel)), c_raw, k=120)
            hub_p = pc if hub_panel == "claims_weekly" else pe
            hub_j = [c.name for c in hub_p.spec.gen_columns].index(hub_col)
            mm, _fd = _month_means(hub_p.levels[hub_col].dropna(), fwd_w,
                                   lv[hub_panel][:, :, hub_j], n_paths)
            cols = [c.name for c in pdata.spec.gen_columns]
            J, d = cols.index(col), pdata.spec.d
            fwd_m = P.forward_periods(pdata.spec, anchor, pdata.spec.horizon)
            pins = {}
            for mi, t in enumerate(fwd_m):
                ym = (t.year, t.month)
                pm_ = t - pd.offsets.MonthBegin(1)
                if ym in mm and (pm_.year, pm_.month) in mm:
                    pins[mi * d + J] = np.log(mm[ym]) - np.log(mm[(pm_.year, pm_.month)])
            incs[mpanel] = G.sample_pinned(gen, c_raw, pins, n_paths, seed=seed + 3)
    finally:
        src.close()

    summaries, panels = {}, {}
    for series, st in BD.SETTLES.items():
        if st.panel not in incs:
            continue
        conn_p = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        conn_p.close()
        spec = P.PANELS[st.panel]
        j = [c.name for c in spec.gen_columns].index(st.column)
        summaries[series] = incs[st.panel][:, :, j].sum(axis=1)
        panels[series] = st.panel
    names = sorted(summaries)
    corr = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            corr[f"{a}|{b}"] = round(float(np.corrcoef(summaries[a],
                                                       summaries[b])[0, 1]), 4)
    out = {"measured_ts": now.isoformat(), "n_paths": n_paths, "seed": seed,
           "series": names, "panels": panels, "corr": corr}
    path = root / "portfolio_corr.json"
    path.write_text(json.dumps(out, indent=1))
    if log:
        log(f"  portfolio_corr: {len(names)} series, {len(corr)} pairs -> {path}")
    return out


def lambda_board(conn, *, now: datetime | None = None) -> dict:
    """What lambda every monthly target would trade at TODAY, with its basis and age.

    Pure read, logged by the weekly refresh right after `weekly_synth_regen`. This exists
    because the lane's two refusal paths — "no lambda row" and "a lambda row that is zero" —
    print the same line in the daily log, which is precisely how the missing writer (§7c)
    went unnoticed. The weekly log therefore states the switch position explicitly: the
    effective lambda, whether it is per-series or pooled, whether it was measured or
    pre-registered, and how old the row is.
    """
    now = now or datetime.now(UTC)
    out: dict[str, dict] = {}
    for name in targets():
        lam, rep = PA.synth_lambda(conn, name)
        entry: dict = {"lambda": lam, "source": rep.get("source")}
        if rep.get("source") is not None:
            row = conn.execute(
                "SELECT measured_ts, detail_json FROM synth_lambda WHERE series=?"
                " ORDER BY measured_ts DESC LIMIT 1", (rep["source"],)).fetchone()
            entry["basis"] = json.loads(row["detail_json"]).get("basis")
            entry["age_days"] = (now - datetime.fromisoformat(row["measured_ts"])).days
        else:
            entry["note"] = rep.get("note")
        out[name] = entry
    return out


def rescore_latest(conn, s, series: str, *, now: datetime | None = None, log=print) -> dict:
    """Re-score the worlds already on disk against the current ladder, without regenerating.

    The repair for the one recoverable way this can go wrong: `live_keys` changes and the
    candidate SETS themselves move, so the stored run no longer covers any grid the morning
    can reach and `_objective` declines to blend. Generation is the expensive half and it is
    still valid — the worlds were conditioned on a macro state that is at most
    `SYNTH_MAX_AGE_DAYS` old, which is the same freshness the daily lane already accepts.
    Only the scoring needs redoing.

    Scores the ladder union for the same reason `one` does, which also makes the `grid_hash`
    short-circuit meaningful: the two jobs compute the same hash over the same object, so a
    match really does mean there is nothing to redo.
    """
    now = now or datetime.now(UTC)
    row = conn.execute("SELECT * FROM synth_runs WHERE series=? ORDER BY built_ts DESC"
                       " LIMIT 1", (series,)).fetchone()
    if not row:
        return {"series": series, "stored": False, "reason": "no run to re-score"}
    meta = json.loads(row["meta_json"])
    paths = [Path(p) for p in meta.get("worlds", [])]
    missing = [str(p) for p in paths if not p.exists()]
    if missing or not paths:
        return {"series": series, "stored": False,
                "reason": f"worlds are gone ({len(missing)}/{len(paths)} missing) — this "
                          "needs a full regeneration, not a re-score"}
    events = []
    for wp in paths:
        c = sqlite3.connect(wp)
        c.row_factory = sqlite3.Row
        try:
            plans = BD.ps.quotable_events(c, series)
        finally:
            c.close()
        for e in plans:
            key = BD.kalshi_period_to_key(e["tok"])
            if key:
                events.append(BD.SynthEvent(series, 0, e["tok"], key, e["close_ts"],
                                            0.0, 0.0, "?", str(wp)))
    lo = now - timedelta(days=PA.WINDOW_DAYS)
    grids, union = PA.grid_ladder(conn, series, lo)
    if len(union) < 2:
        return {"series": series, "stored": False, "grids": [len(g) for g in grids],
                "reason": "the widest reachable grid is defaults-only — nothing to rank"}
    if PA.grid_hash(union) == row["grid_hash"]:
        return {"series": series, "stored": False, "run_id": row["run_id"],
                "reason": "the stored run already covers every reachable grid"}
    grid = union
    kept_s, mat_s = BD.score_matrix(events, grid, log=log)
    if len(kept_s) < 3:
        return {"series": series, "stored": False,
                "reason": f"only {len(kept_s)} synthetic events replay on every candidate"}
    built = BD.BuildResult(
        series=series, cutoff=datetime.fromisoformat(row["cutoff_ts"]),
        splice=datetime.fromisoformat(row["splice_ts"]),
        anchor=meta.get("anchor"), worlds=paths, events=events,
        coverage=meta.get("coverage", {}), meta=meta)
    run_id = _store(conn, series, built, grid, kept_s, mat_s, now=now,
                    n_real=len(PA.universe(conn, series, now)))
    return {"series": series, "stored": True, "run_id": run_id, "k": len(grid),
            "grids": [len(g) for g in grids], "n_synth": len(kept_s),
            "rescored_from": row["run_id"]}
