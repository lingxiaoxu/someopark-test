"""Plan 03 research — cross-venue funding differential arb (Kalshi ↔ OKX).

Structure: long Kalshi perp X + short OKX perp X (or flipped). The price legs
track the SAME underlying, so price P&L ≈ cancels; the book collects the
funding DIFFERENTIAL between venues. Selection uses name-level streak
persistence (the "BCH 36/36 negative" pattern) instead of rank baskets.

⚠️ COMPLIANCE (stated up front, carried into every artifact): the plans default
to Kalshi-only. OKX blocks US customers from TRADING (its public data is open,
its order flow is not). For this user the offshore leg is very likely NOT
legally/operationally available — this module QUANTIFIES the economics anyway
(a) to size what is being left on the table and (b) to calibrate the
Kalshi-only fallback (§4), which hedges the carry name with a correlated
Kalshi perp instead of the same-asset offshore perp.

Funding-sign conventions (single-sourced with costs.funding_payment):
  holder P&L per cycle = −rate × position. Long collects NEGATIVE rates,
  short collects POSITIVE rates.
  Pair "long Kalshi + short OKX" daily P&L per $1/leg:
      (−Σ kalshi_rates_that_day) + (+Σ okx_rates_that_day)
  Grids differ (Kalshi 04/12/20 UTC, OKX 00/08/16 UTC) → align on daily sums.

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_strategies.funding_carry.research_cross_venue
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.loader import load_funding, load_offshore, load_perp_candles
from crypto_trading.crypto_common.trade_stats import newey_west_tstat

logger = logging.getLogger(__name__)

CYCLES_PER_YEAR = 365 * 3
PAIRS = {  # Kalshi ticker -> OKX symbol
    "KXBTCPERP": "BTCUSDT", "KXETHPERP": "ETHUSDT", "KXSOLPERP": "SOLUSDT",
    "KXXRPPERP": "XRPUSDT", "KXDOGEPERP": "DOGEUSDT", "KXBCHPERP": "BCHUSDT",
    "KXLTCPERP": "LTCUSDT", "KXLINKPERP": "LINKUSDT", "KXNEARPERP": "NEARUSDT",
    "KXSUIPERP": "SUIUSDT",
}
# fees per fill, fraction of notional (Kalshi maker measured; OKX taker standard)
KALSHI_FEE = 0.0005
OKX_FEE = 0.0005


# ── data prep ───────────────────────────────────────────────────────────────

def daily_funding(rates: pd.Series) -> pd.Series:
    """Cycle rates → daily sum (UTC date), aligning the two venues' grids."""
    return rates.groupby(rates.index.floor("1D")).sum()


def load_pair(kalshi_ticker: str) -> tuple[pd.Series, pd.Series] | None:
    """(kalshi_daily, okx_daily) on the COMMON window, or None if missing."""
    try:
        k = load_funding(kalshi_ticker)["funding_rate"]
        o = load_offshore("funding", PAIRS[kalshi_ticker])["funding_rate"]
    except (FileNotFoundError, KeyError):
        return None
    kd, od = daily_funding(k), daily_funding(o)
    common = kd.index.intersection(od.index)
    if len(common) < 20:
        return None
    return kd[common], od[common]


# ── §1 differential table ───────────────────────────────────────────────────

def sign_consistency(rates: pd.Series) -> float:
    nz = rates[rates != 0]
    if len(nz) == 0:
        return 0.0
    pos = (nz > 0).mean()
    return float(max(pos, 1 - pos))


def differential_table(end: pd.Timestamp | None = None) -> pd.DataFrame:
    """``end``: optional cutoff — rank pairs on data strictly BEFORE it (PIT
    universe selection); None ranks on the full sample (descriptive only)."""
    rows = []
    for kt, os_ in PAIRS.items():
        pair = load_pair(kt)
        if pair is None:
            continue
        kd, od = pair
        if end is not None:
            kd, od = kd[kd.index < end], od[od.index < end]
        if len(kd) < 5:
            continue
        k_ann = float(kd.mean() * 365)
        o_ann = float(od.mean() * 365)
        # best direction: long-Kalshi/short-OKX collects (o − k); flipped collects (k − o)
        diff_lk = o_ann - k_ann          # long Kalshi + short OKX
        direction = "long_K_short_O" if diff_lk >= 0 else "short_K_long_O"
        try:
            k_cycles = load_funding(kt)["funding_rate"]
            o_cycles = load_offshore("funding", PAIRS[kt])["funding_rate"]
        except FileNotFoundError:
            continue
        rows.append({
            "kalshi": kt, "okx": os_, "days": len(kd),
            "kalshi_ann_pct": k_ann * 100, "okx_ann_pct": o_ann * 100,
            "differential_ann_pct": abs(diff_lk) * 100, "direction": direction,
            "kalshi_sign_consistency": sign_consistency(k_cycles),
            "okx_sign_consistency": sign_consistency(o_cycles),
        })
    df = pd.DataFrame(rows).sort_values("differential_ann_pct", ascending=False)
    return df.reset_index(drop=True)


# ── §2 streak-persistence rule ──────────────────────────────────────────────

def nonzero_streaks(rates: pd.Series) -> pd.Series:
    """At each cycle: length of the current same-sign NONZERO streak (zeros are
    skipped — they neither extend nor break; sign change resets)."""
    streak = 0
    sign = 0
    out = []
    for r in rates:
        if r == 0 or np.isnan(r):
            out.append(streak * sign)   # zero: carry the SIGNED streak, don't extend
            continue
        s = 1 if r > 0 else -1
        streak = streak + 1 if s == sign else 1
        sign = s
        out.append(streak * sign)       # signed streak
    return pd.Series(out, index=rates.index)


def streak_rule_backtest(kalshi_ticker: str, *, enter_k: int, exit_n: int = 2,
                         fee_amortized: bool = True,
                         start: pd.Timestamp | None = None) -> dict | None:
    """Enter the pair when Kalshi's nonzero same-sign streak ≥ enter_k; hold
    while it persists; exit after exit_n opposite-sign nonzero cycles.
    Position direction collects the Kalshi side (streak sign negative →
    long Kalshi; positive → short Kalshi), with the opposite OKX leg.
    P&L aligned daily. Fees: 2 fills/leg/round-trip on both venues.
    ``start``: accrue P&L only from this date (streak state may warm up on
    earlier cycles — that is past information, PIT-safe). Used so the backtest
    window is disjoint from the universe-selection window.
    """
    pair = load_pair(kalshi_ticker)
    if pair is None:
        return None
    kd, od = pair
    if start is not None:
        kd, od = kd[kd.index >= start], od[od.index >= start]
        if len(kd) < 5:
            return None
    k_cycles = load_funding(kalshi_ticker)["funding_rate"]
    streaks = nonzero_streaks(k_cycles)
    # daily last streak value (decision at day end, position for next day — PIT)
    streak_daily = streaks.groupby(streaks.index.floor("1D")).last().reindex(kd.index).ffill()

    in_pos = False
    pos_sign = 0                        # +1 = short Kalshi (collect positive), −1 = long Kalshi
    opp_count = 0
    recs = []
    entries = 0
    prev_day_streak = streak_daily.shift(1)     # PIT: decide on yesterday's streak
    for day in kd.index:
        s = prev_day_streak.get(day, 0)
        if not in_pos:
            if abs(s) >= enter_k:
                in_pos = True
                pos_sign = 1 if s > 0 else -1
                opp_count = 0
                entries += 1
        else:
            # exit check on yesterday's info
            if s != 0 and np.sign(s) != pos_sign:
                opp_count += 1
                if opp_count >= exit_n:
                    in_pos = False
            else:
                opp_count = 0
        if in_pos:
            # pos_sign −1: long Kalshi (+short OKX): pnl = −k + o
            # pos_sign +1: short Kalshi (+long OKX): pnl = +k − o
            pnl = (kd[day] - od[day]) * pos_sign
            recs.append({"day": day, "pnl": pnl, "dir": pos_sign})
    if not recs:
        return {"ticker": kalshi_ticker, "enter_k": enter_k, "days_in": 0}
    df = pd.DataFrame(recs).set_index("day")
    gross_daily = df["pnl"]
    rt_fee = 2 * KALSHI_FEE + 2 * OKX_FEE       # per $1/leg round trip
    total_fees = entries * rt_fee
    net_total = float(gross_daily.sum()) - total_fees
    days_in = len(df)
    nw = newey_west_tstat(gross_daily)
    return {
        "ticker": kalshi_ticker, "enter_k": enter_k, "exit_n": exit_n,
        "entries": entries, "days_in": days_in, "days_total": len(kd),
        "gross_total": float(gross_daily.sum()), "fees_total": total_fees,
        "net_total": net_total,
        "ann_on_1x_leg_pct": net_total / max(days_in, 1) * 365 * 100,
        "ann_on_2x_capital_pct": net_total / max(days_in, 1) * 365 * 100 / 2,
        "nw_t_gross_daily": nw["t_nw"], "n_days": nw["n"],
        "series": gross_daily,
    }


# ── §3 residual basis risk ──────────────────────────────────────────────────

def residual_basis_vol(kalshi_ticker: str) -> dict | None:
    """Ann. vol of (Kalshi daily return − OKX daily return) — the pair's TRUE
    residual price risk (marks are different instruments on the same asset)."""
    try:
        kp = load_perp_candles(kalshi_ticker, "1h")["price_close"].dropna()
        op = load_offshore("klines_1h", PAIRS[kalshi_ticker])["close"].dropna()
    except FileNotFoundError:
        return None
    kd = kp.resample("1D").last().pct_change().dropna()
    od = op.resample("1D").last().pct_change().dropna()
    common = kd.index.intersection(od.index)
    if len(common) < 15:
        return None
    resid = (kd[common] - od[common])
    return {"resid_vol_ann_pct": float(resid.std() * np.sqrt(365)) * 100,
            "each_vol_ann_pct": float(kd[common].std() * np.sqrt(365)) * 100,
            "corr": float(kd[common].corr(od[common])), "n_days": len(common)}


# ── §4 Kalshi-only fallback ─────────────────────────────────────────────────

def kalshi_only_fallback(long_ticker: str = "KXBCHPERP",
                         hedge_ticker: str = "KXBTCPERP") -> dict | None:
    """Domesticated variant: long the collect-side name on Kalshi, hedge with a
    SHORT of a correlated Kalshi perp (BTC — which itself collects positive
    funding when shorted). Reports carry collected, residual price vol, and a
    naive carry-Sharpe = ann_carry / resid_vol."""
    try:
        lf = load_funding(long_ticker)["funding_rate"]
        hf = load_funding(hedge_ticker)["funding_rate"]
        lp = load_perp_candles(long_ticker, "1h")["price_close"].dropna()
        hp = load_perp_candles(hedge_ticker, "1h")["price_close"].dropna()
    except FileNotFoundError:
        return None
    carry_long = float(-daily_funding(lf).mean() * 365)     # long collects −rate
    carry_hedge = float(daily_funding(hf).mean() * 365)     # short collects +rate
    ld = lp.resample("1D").last().pct_change().dropna()
    hd = hp.resample("1D").last().pct_change().dropna()
    common = ld.index.intersection(hd.index)
    beta = float(np.cov(ld[common], hd[common])[0, 1] / np.var(hd[common]))
    resid = ld[common] - beta * hd[common]
    resid_vol = float(resid.std() * np.sqrt(365))
    unhedged_vol = float(ld[common].std() * np.sqrt(365))
    total_carry = carry_long + carry_hedge * beta            # hedge sized at beta
    return {
        "long": long_ticker, "hedge_short": hedge_ticker, "beta": beta,
        "carry_long_ann_pct": carry_long * 100,
        "carry_hedge_ann_pct": carry_hedge * 100,
        "total_carry_ann_pct": total_carry * 100,
        "unhedged_vol_ann_pct": unhedged_vol * 100,
        "residual_vol_ann_pct": resid_vol * 100,
        "carry_sharpe_unhedged": carry_long / unhedged_vol if unhedged_vol else 0,
        "carry_sharpe_hedged": total_carry / resid_vol if resid_vol else 0,
        "n_days": len(common),
    }


# ── orchestrator ────────────────────────────────────────────────────────────

def run_all(enter_ks=(6, 12, 24), selection_frac: float = 0.5) -> dict:
    out: dict = {}
    diff = differential_table()
    out["differential_table"] = diff.to_dict("records")   # full-sample, DESCRIPTIVE

    # PIT universe selection: rank pairs on the first ``selection_frac`` of the
    # sample, backtest strictly after. The old full-sample head(4) baked
    # selection bias into the headline annualized number.
    spans = [p[0].index for kt in PAIRS if (p := load_pair(kt)) is not None]
    cutoff = None
    if spans:
        lo = min(ix.min() for ix in spans)
        hi = max(ix.max() for ix in spans)
        cutoff = lo + (hi - lo) * selection_frac
    sel = differential_table(end=cutoff) if cutoff is not None else diff
    top = sel.head(4)["kalshi"].tolist()
    out["selection"] = {"cutoff": str(cutoff), "universe": top,
                        "note": "ranked on prefix window only; backtest starts at cutoff"}

    sweeps = []
    for kt in top:
        for K in enter_ks:
            r = streak_rule_backtest(kt, enter_k=K, start=cutoff)
            if r and r.get("days_in", 0) > 0:
                r.pop("series", None)
                sweeps.append(r)
    out["streak_sweep"] = sweeps
    out["n_trials"] = len(enter_ks) * len(top)

    out["residual_basis"] = {kt: residual_basis_vol(kt) for kt in top}
    out["kalshi_only_fallback"] = kalshi_only_fallback()
    out["compliance_note"] = ("OKX blocks US customers from trading; offshore leg "
                              "likely NOT available to this operator. Quantified for "
                              "sizing + to calibrate the Kalshi-only fallback.")
    return out


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    res = run_all()
    outdir = SIGNALS_DIR / "research"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S")
    (outdir / f"cross_venue_{stamp}.json").write_text(
        json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
