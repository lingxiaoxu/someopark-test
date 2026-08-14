"""Plan 02 fee-defeat research: can the validated gap_z edge be CONCENTRATED
enough to clear ~10bps round-trip maker fees?

Established facts this attacks: the perp-leg expression's gross edge is real
(zero-fee best cell +$9.79, 70% hit, ~3.7bps/round-trip) but every config loses
under projected fees. Five levers examined on the ~20d recorded strips+tape:

  1. TAIL CONCENTRATION — is forward convergence monotone in |gap_z|, and do
     extreme-z entries (beyond the old sweep's k=2.0) gross > 12bps?
  2. SETTLEMENT PINNING — near horizon close the event strip SETTLES; does
     time-to-close condition the convergence (settlement gravity)?
  3. EPISODES — group same-sign excursions; one trade per episode at the first
     HIGH-threshold crossing (causal): fewer, fatter trades?
  4. DOUBLE CONFIRMATION — gap sign agreeing with the spot-basis sign (two
     independent dislocation measures): bigger convergence?
  5. FEE SENSITIVITY — at what round-trip fee does the best honest subset
     break even? (Kalshi fee tiers drop with volume — the deploy threshold.)

Honesty bar: anything called "tradeable" uses the SAME fill-aware loop as the
strategy (maker entry vs real tape, queue=1, passive-then-cross exit,
unfavorable-touch crossing); signal-level tables are labeled descriptive
(overlapping forward windows — no significance claims). Deflation over the
number of fill-aware runs.

CLI: … -m crypto_trading.crypto_strategies.event_perp.research_fee_defeat
Artifacts: trading_signals/research/fee_defeat_<ts>.{json,md}
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import (build_basis_frame,
                                                 load_poll_market_stats,
                                                 load_poll_trades)
from crypto_trading.crypto_common.trade_stats import trade_significance_report
from crypto_trading.crypto_strategies.event_perp.backtest import SERIES_TO_PERP
from crypto_trading.crypto_strategies.event_perp.strategy import (EventPerpParams,
                                                                  _backtest_loop,
                                                                  build_gap_frame)

logger = logging.getLogger(__name__)

SERIES_LIST = ("KXBTC", "KXETH")
SERIES_TO_ASSET = {"KXBTC": "BTC", "KXETH": "ETH"}
FWD_WINDOWS_MIN = (30, 60, 120)
Z_BUCKETS = ((1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, np.inf))
TTC_BUCKETS = ((12.0, np.inf, ">12h"), (4.0, 12.0, "4-12h"),
               (1.0, 4.0, "1-4h"), (0.0, 1.0, "<1h"))
BASE_Z = 1.0          # rows below this are non-signal; excluded from all buckets
FEE_LINE_BPS = (0.0, 2.5, 5.0, 10.0)


# ── shared inputs ────────────────────────────────────────────────────────────

_CACHE: dict = {}


def series_inputs(series: str, zwin: int = 60):
    """(gap_frame+ttc, touch, trades, perp) built once per series."""
    if series in _CACHE:
        return _CACHE[series]
    perp = SERIES_TO_PERP[series]
    gap = build_gap_frame(series, zwin=zwin)
    if gap.empty:
        raise RuntimeError(f"no gap frame for {series}")
    close_ts = pd.to_datetime(gap["close_time"], utc=True, format="ISO8601")
    gap = gap.assign(ttc_h=(close_ts - gap.index).dt.total_seconds() / 3600.0)
    stats = load_poll_market_stats(perp)
    touch = (stats[["bid", "ask"]].dropna()
             .resample("1min", label="right", closed="right").last().ffill(limit=3))
    trades = load_poll_trades(perp).sort_index()
    _CACHE[series] = (gap, touch, trades, perp)
    return _CACHE[series]


# ── signal-level forward convergence (descriptive) ───────────────────────────

def _forward_spot(gap: pd.DataFrame, minutes: int) -> pd.Series:
    """Within-horizon perp_spot ≥`minutes` ahead (NaN when horizon ends first)."""
    out = np.full(len(gap), np.nan)
    pos = {ts: i for i, ts in enumerate(gap.index)}
    for _, g in gap.groupby("close_time", sort=False):
        ts = g["recv_ts"].to_numpy()
        spot = g["perp_spot"].to_numpy()
        j = np.searchsorted(ts, ts + minutes * 60.0, side="left")
        ok = j < len(ts)
        rows = [pos[t] for t in g.index]
        for r, jj, o in zip(rows, j, ok):
            if o:
                out[r] = spot[jj]
    return pd.Series(out, index=gap.index)


def conv_bps(gap: pd.DataFrame, minutes: int) -> pd.Series:
    """Signed forward convergence in bps: +ve = perp moved toward implied mean."""
    fwd = _forward_spot(gap, minutes)
    d = np.sign(gap["gap_z"])
    return 1e4 * d * (fwd / gap["perp_spot"] - 1.0)


def bucket_table(gap: pd.DataFrame, bucket_col: str, buckets, label: str) -> pd.DataFrame:
    rows = []
    convs = {m: conv_bps(gap, m) for m in FWD_WINDOWS_MIN}
    sig = gap["gap_z"].abs() >= BASE_Z
    for b in buckets:
        lo, hi, name = (b if len(b) == 3 else (b[0], b[1], f"[{b[0]},{b[1]})"))
        m = sig & (gap[bucket_col].abs() >= lo) & (gap[bucket_col].abs() < hi) \
            if bucket_col == "gap_z" else \
            sig & (gap[bucket_col] >= lo) & (gap[bucket_col] < hi)
        row = {"bucket": name, "n": int(m.sum())}
        for mins in FWD_WINDOWS_MIN:
            c = convs[mins][m].dropna()
            row[f"conv{mins}m_bps"] = round(float(c.mean()), 2) if len(c) else None
        rows.append(row)
    df = pd.DataFrame(rows)
    df.insert(0, "table", label)
    return df


# ── episodes (signal-level, causal first-crossing) ───────────────────────────

def episode_table(gap: pd.DataFrame, hi_k: float = 2.0) -> dict:
    """Same-sign |z|≥BASE_Z excursions within a horizon; enter at the FIRST
    |z|≥hi_k crossing (causal), measure move to episode end (z sign flip or
    |z|<BASE_Z or horizon end)."""
    grosses, lengths = [], []
    n_episodes = 0
    for _, g in gap.groupby("close_time", sort=False):
        z = g["gap_z"].to_numpy()
        spot = g["perp_spot"].to_numpy()
        i, n = 0, len(g)
        while i < n:
            if not np.isfinite(z[i]) or abs(z[i]) < BASE_Z:
                i += 1
                continue
            sgn = np.sign(z[i])
            j = i
            entry = None
            while j < n and np.isfinite(z[j]) and np.sign(z[j]) == sgn \
                    and abs(z[j]) >= BASE_Z:
                if entry is None and abs(z[j]) >= hi_k:
                    entry = j
                j += 1
            n_episodes += 1
            if entry is not None and j - 1 > entry:
                grosses.append(1e4 * sgn * (spot[j - 1] / spot[entry] - 1.0))
                lengths.append((g["recv_ts"].iloc[j - 1] - g["recv_ts"].iloc[entry]) / 60)
            i = j
    g = pd.Series(grosses)
    days = (gap["recv_ts"].max() - gap["recv_ts"].min()) / 86400
    return {"episodes_total": n_episodes, "traded (hit hi_k)": len(g),
            "per_day": round(len(g) / days, 2) if days else None,
            "mean_gross_bps": round(float(g.mean()), 2) if len(g) else None,
            "median_gross_bps": round(float(g.median()), 2) if len(g) else None,
            "hit": round(float((g > 0).mean()), 2) if len(g) else None,
            "mean_hold_min": round(float(np.mean(lengths)), 1) if lengths else None}


# ── double confirmation mask ─────────────────────────────────────────────────

def basis_agree_mask(gap: pd.DataFrame, series: str) -> np.ndarray:
    """True where gap sign agrees with the spot-basis sign (both say perp is
    cheap, or both say rich). gap>0 ⇔ perp cheap vs event book; b_t<0 ⇔ perp
    cheap vs spot composite."""
    perp, asset = SERIES_TO_PERP[series], SERIES_TO_ASSET[series]
    try:
        basis = build_basis_frame(perp, asset)["b_t"]
    except Exception as e:
        logger.warning("no basis frame for %s (%s) — mask all-False", series, e)
        return np.zeros(len(gap), dtype=bool)
    b = basis.reindex(gap.index, method="ffill", tolerance=pd.Timedelta(minutes=3))
    return ((gap["gap"] > 0) & (b < 0) | (gap["gap"] < 0) & (b > 0)).fillna(False).to_numpy()


# ── fill-aware conditioned runs (the honest, tradeable layer) ────────────────

def fill_run(series: str, *, entry_k: float, mask: np.ndarray | None,
             label: str, max_hold_min: int = 120,
             fee_scenario: str = "projected") -> dict:
    gap, touch, trades, perp = series_inputs(series)
    p = replace(EventPerpParams(), entry_k=entry_k, max_hold_min=max_hold_min)
    r = _backtest_loop(gap, touch, trades, p, ticker=perp,
                       fee_scenario=fee_scenario, entry_mask=mask)
    s = r["summary"]
    tp = r["trade_pnl"]
    out = {"label": label, "series": series, "entry_k": entry_k,
           "n": s["round_trips"], "fill_rate": round(s["fill_rate"], 2),
           "net": round(s["net_pnl"], 3), "hit": round(s["hit_rate"], 2)}
    if len(tp):
        gross_bps = 1e4 * tp["gross"] / (tp["entry_px"] * p.contracts)
        out["gross_bps_mean"] = round(float(gross_bps.mean()), 2)
        out["_gross_bps"] = gross_bps.tolist()
        out["_notional"] = float((tp["entry_px"] * p.contracts).mean())
    if len(tp) >= 5:
        rep = trade_significance_report(tp["net"], k=min(5, len(tp)), n_trials=10)
        out.update({"t_nw": round(rep["t_nw"], 2), "dsr": round(rep["dsr"], 3),
                    "significant": bool(rep["significant"])})
    return out


def fee_sensitivity(gross_bps: list[float]) -> dict:
    g = np.array(gross_bps)
    line = {f"net_bps@{f}bps": round(float(g.mean() - f), 2) for f in FEE_LINE_BPS}
    be = float(g.mean())
    line["breakeven_rt_fee_bps"] = round(be, 2)
    line["n"] = len(g)
    return line


# ── orchestration ────────────────────────────────────────────────────────────

def run_all() -> dict:
    report: dict = {"signal_level (descriptive — overlapping windows)": {},
                    "fill_aware (honest, n_trials=10)": [], "episodes": {},
                    "fee_sensitivity": {}}
    sig_tables = []
    for series in SERIES_LIST:
        gap, *_ = series_inputs(series)
        sig_tables.append(bucket_table(gap, "gap_z", Z_BUCKETS, f"{series} |z| buckets"))
        sig_tables.append(bucket_table(gap, "ttc_h", TTC_BUCKETS, f"{series} time-to-close"))
        agree = basis_agree_mask(gap, series)
        base = (gap["gap_z"].abs() >= 1.5).to_numpy()
        for name, m in (("agree", agree & base), ("disagree", ~agree & base)):
            c = conv_bps(gap, 60)[m].dropna()
            sig_tables.append(pd.DataFrame([{
                "table": f"{series} basis-{name}", "bucket": "|z|≥1.5", "n": int(len(c)),
                "conv60m_bps": round(float(c.mean()), 2) if len(c) else None}]))
        report["episodes"][series] = episode_table(gap)

    report["signal_level (descriptive — overlapping windows)"] = \
        pd.concat(sig_tables, ignore_index=True).to_dict("records")

    # fill-aware conditioned runs (10 total → deflation n_trials=10)
    runs = []
    for series in SERIES_LIST:
        gap, *_ = series_inputs(series)
        runs.append(fill_run(series, entry_k=2.5, mask=None, label="tail k2.5"))
        runs.append(fill_run(series, entry_k=3.0, mask=None, label="tail k3.0"))
        near = ((gap["ttc_h"] < 4.0).to_numpy())
        runs.append(fill_run(series, entry_k=1.5, mask=near, label="pin <4h k1.5"))
        vnear = ((gap["ttc_h"] < 1.0).to_numpy())
        runs.append(fill_run(series, entry_k=1.5, mask=vnear, label="pin <1h k1.5"))
        runs.append(fill_run(series, entry_k=1.5, mask=basis_agree_mask(gap, series),
                             label="basis-agree k1.5"))
    best = None
    for r in runs:
        gross = r.pop("_gross_bps", None)
        r.pop("_notional", None)
        if gross and (best is None or r.get("gross_bps_mean", -99) > best[1]):
            best = (r["label"] + " " + r["series"], r.get("gross_bps_mean", -99), gross)
    report["fill_aware (honest, n_trials=10)"] = runs
    if best:
        report["fee_sensitivity"] = {"best_subset": best[0], **fee_sensitivity(best[2])}
    return report


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    rep = run_all()
    out = SIGNALS_DIR / "research"
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (out / f"fee_defeat_{stamp}.json").write_text(json.dumps(rep, indent=1, default=str))
    md = ["# Plan 02 fee-defeat research", ""]
    md.append("## signal-level (descriptive)")
    md.append(pd.DataFrame(rep["signal_level (descriptive — overlapping windows)"]).to_string())
    md.append("\n## fill-aware (honest)")
    md.append(pd.DataFrame(rep["fill_aware (honest, n_trials=10)"]).to_string())
    md.append("\n## episodes")
    md.append(json.dumps(rep["episodes"], indent=1))
    md.append("\n## fee sensitivity (best subset)")
    md.append(json.dumps(rep["fee_sensitivity"], indent=1))
    (out / f"fee_defeat_{stamp}.md").write_text("\n".join(md))
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
