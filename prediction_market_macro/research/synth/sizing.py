"""§5b-2 (a) — the sizing harness: what does independence mis-state about portfolio risk?

This module MEASURES; it changes no position size. §5d registered the warning that gates
any adoption: the real same-frequency variance ratio is 0.883, i.e. a correct joint law
licenses MORE risk than independent sizing on those pairs, and a fidelity argument alone
is not allowed to loosen a risk limit. Any sizing change built on these numbers needs its
own preregistration; this file exists so that registration has a harness to cite.

The object measured: across the n_paths generated worlds, series' settlement quantities
in the SAME world share the joint draw (PR-20/24/26), while a shuffled pairing destroys
exactly that alignment and nothing else — the same permutation-floor logic xpanel_dep.py
used on real data. The ratio

    var(portfolio | true pairing) / E_shuffles[ var(portfolio | shuffled pairing) ]

is 1.0 under independence by construction, matches the real-data ratio to the extent the
coupling is faithful, and is the number a portfolio-level lambda would be calibrated
against. n_paths=8 makes any single reading noisy; the shuffle expectation is exact-ish
(all pairings averaged) but the true-pairing variance has ~8 samples, so `reps` bootstrap
bands are reported and MUST travel with the point estimate wherever it is quoted.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

# settlement quantity per series: (table, id, log-increment on W-SAT sampling)
_WEEKLY_SINKS = {
    "KXJOBLESSCLAIMS": ("fred_obs", "ICSA"),
    "KXWTIW": ("fut_daily", "CL"),
    "KXNATGASW": ("fut_daily", "NG"),
}


def _weekly_series(db: Path, kind: str, ident: str, after: str) -> pd.Series:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        if kind == "fred_obs":
            rows = conn.execute(
                "SELECT event_time, value FROM fred_obs WHERE sid=? AND event_time>?"
                " ORDER BY event_time", (ident, after)).fetchall()
            s = pd.Series({pd.Timestamp(t): v for t, v in rows})
        else:
            rows = conn.execute(
                "SELECT event_time, close FROM fut_daily WHERE root=? AND event_time>?"
                " ORDER BY event_time", (ident, after)).fetchall()
            s = pd.Series({pd.Timestamp(t): v for t, v in rows}).resample("W-SAT").last()
    finally:
        conn.close()
    return s.dropna()


def weekly_increments(root: Path, after: str) -> tuple[np.ndarray, list[str], int]:
    """(n_series, n_paths, n_weeks) log-increments of the weekly settlement quantities,
    aligned on the common W-SAT index across every world of every series."""
    per = {}
    for series, (kind, ident) in _WEEKLY_SINKS.items():
        worlds = sorted((root / series).glob(f"world_{series}_*.db"))
        if not worlds:
            raise FileNotFoundError(f"no worlds for {series} under {root}")
        per[series] = [_weekly_series(w, kind, ident, after) for w in worlds]
    idx = None
    for ss in per.values():
        for s in ss:
            idx = s.index if idx is None else idx.intersection(s.index)
    names = list(per)
    arr = np.array([[s.loc[idx].to_numpy(float) for s in per[n]] for n in names])
    return np.diff(np.log(arr), axis=2), names, len(idx) - 1


def independence_mispricing(root: Path, after: str, *, reps: int = 2000,
                            seed: int = 0) -> dict:
    """The (a) precondition number: true-pairing vs shuffled-pairing portfolio variance.

    Portfolio = equal-weight sum of per-series standardized weekly increments (each
    series scaled to unit variance first, so the ratio reads correlation structure and
    not whichever series happens to be noisiest).
    """
    inc, names, n_weeks = weekly_increments(root, after)
    n_series, n_paths, _ = inc.shape
    sd = inc.std(axis=(1, 2), keepdims=True)
    z = inc / np.where(sd > 0, sd, 1.0)
    true_var = float(z.sum(axis=0).var())
    rng = np.random.default_rng(seed)
    shuf = []
    for _ in range(reps):
        zs = np.stack([z[i, rng.permutation(n_paths)] for i in range(n_series)])
        shuf.append(float(zs.sum(axis=0).var()))
    shuf = np.array(shuf)
    boot = []
    for _ in range(reps):
        pick = rng.integers(0, n_paths, n_paths)
        boot.append(float(z[:, pick].sum(axis=0).var())
                    / float(np.mean(shuf)))
    boot = np.array(boot)
    return {"series": names, "n_paths": n_paths, "n_weeks": n_weeks,
            "true_var": true_var, "indep_var": float(shuf.mean()),
            "ratio": true_var / float(shuf.mean()),
            "ratio_p05": float(np.quantile(boot, 0.05)),
            "ratio_p95": float(np.quantile(boot, 0.95)),
            "real_reference": "xpanel_dep same-frequency mean var ratio 0.883 (§5d)"}
