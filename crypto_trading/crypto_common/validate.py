"""
validate.py — PASS/FAIL validation gates (Plan 01 §8, Plan 05 §8)
=================================================================
Pattern COPIED from qlib-main/semiconductor_strategy/validate.py (read-only
template): explicit win-criterion, `beats` matrix, PASS printout, exit code
0 on PASS / 1 on FAIL.

Crypto adaptations:
  * FAIL-SAFE: the gates consume WALK-FORWARD OOS artifacts + backtest
    summaries (they never re-run engines here). When a required input does
    not exist yet the verdict is FAIL with reason "insufficient data: …" —
    NEVER a vacuous PASS. (Template re-ran its engine inline; our gates are
    OOS-only per the plans, so artifacts are the contract.)
  * Annualization 252 → 365.
  * Two gates:
      basis_meanrev  (Plan 01 §8): OOS net Sharpe ≥ floor under PROJECTED
        fees AND realized-vs-modeled slippage ratio ≤ cap (only evaluable
        once live fills exist — until then it FAILS as insufficient unless
        --allow-no-live) AND maxDD within budget.
      perp_rotation  (Plan 05 §8): OOS beats BOTH benchmarks (KXBTCPERP-HODL
        + EW basket) on Sharpe AND CAGR under projected fees+funding+slippage,
        AND maxDD within budget.

Expected artifacts (the WF/backtest steps produce these):
  trading_signals/walk_forward/<strategy>_oos_equity.csv   columns: date,equity
    [+ optional benchmark columns btc_hodl,ew_basket for perp_rotation —
     otherwise benchmarks are rebuilt from recorded candles over the window]
  trading_signals/walk_forward/<strategy>_detail.json      WFResult.to_detail_dict()
  trading_signals/<strategy>/backtests/*.json              latest summary (fee scenario check)

CLI:
    … -m crypto_trading.crypto_common.validate --strategy basis_meanrev
    … -m crypto_trading.crypto_common.validate --strategy perp_rotation [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common.backtest.metrics import TRADING_DAYS

log = logging.getLogger("crypto.validate")

# Gate parameters (starting points — Plan 01/05 "floor"/"budget"; WF-calibrate)
BASIS_SHARPE_FLOOR = 0.5
BASIS_MAXDD_BUDGET = -0.15
BASIS_SLIPPAGE_RATIO_CAP = 1.5       # realized ≤ 1.5× modeled
ROTATION_MAXDD_BUDGET = -0.35
LIQ_SHARPE_FLOOR = 0.5
LIQ_MAXDD_BUDGET = -0.20
EVENT_IC_FLOOR = 0.05               # Plan 02 SIGNAL gate: OOS fair-value-gap IC
EVENT_MIN_FOLDS = 6                  # ≥ this many OOS folds with consistent sign


def _metrics(returns: pd.Series) -> dict:
    """Template's _metrics verbatim (PERIODS → 365)."""
    r = returns.dropna()
    if len(r) < 10:
        return {"cagr": float("nan"), "vol": float("nan"), "sharpe": float("nan"),
                "calmar": float("nan"), "max_dd": float("nan"), "total": float("nan")}
    total = (1 + r).prod()
    cagr = total ** (TRADING_DAYS / len(r)) - 1
    vol = r.std() * np.sqrt(TRADING_DAYS)
    sharpe = (r.mean() / r.std() * np.sqrt(TRADING_DAYS)) if r.std() > 0 else float("nan")
    eq = (1 + r).cumprod()
    max_dd = (eq / eq.cummax() - 1).min()
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else float("nan")
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "calmar": calmar,
            "max_dd": max_dd, "total": total - 1}


# ── artifact loading (fail-safe) ────────────────────────────────────────────

def _wf_dir() -> Path:
    return _config.SIGNALS_DIR / "walk_forward"


def _rel(p: Path) -> str:
    """Repo-relative path for messages; falls back to the name if outside repo."""
    try:
        return str(p.relative_to(_config.CRYPTO_ROOT))
    except ValueError:
        return p.name


def _load_oos_equity(strategy: str) -> tuple[pd.DataFrame | None, str]:
    p = _wf_dir() / f"{strategy}_oos_equity.csv"
    if not p.exists():
        return None, f"missing {_rel(p)}"
    df = pd.read_csv(p)
    date_col = "date" if "date" in df.columns else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], utc=True)
    df = df.set_index(date_col).sort_index()
    if "equity" not in df.columns or len(df) < 15:
        return None, f"{p.name}: needs ≥15 rows with an 'equity' column"
    return df, ""


def _load_wf_detail(strategy: str) -> tuple[dict | None, str]:
    p = _wf_dir() / f"{strategy}_detail.json"
    if not p.exists():
        return None, f"missing {_rel(p)}"
    try:
        return json.loads(p.read_text()), ""
    except Exception as e:
        return None, f"{p.name}: unreadable ({e})"


def _latest_backtest(strategy: str) -> dict | None:
    d = _config.SIGNALS_DIR / strategy / "backtests"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except Exception:
        return None


def _live_slippage_ratio(strategy: str) -> float | None:
    """Realized vs modeled slippage from live fills (None until live)."""
    try:
        from crypto_trading.crypto_common.reporting.ledger import load_fills
        fills = load_fills(strategy)
    except Exception:
        return None
    if fills is None or fills.empty or "decision_mid" not in fills.columns:
        return None
    f = fills.dropna(subset=["decision_mid", "price"])
    if len(f) < 20:
        return None
    signed = np.where(f["side"] == "buy", 1.0, -1.0)
    realized_bps = ((f["price"] - f["decision_mid"]) / f["decision_mid"]
                    * signed * 1e4)
    modeled_bps = 5.0     # engine's modeled slippage assumption (bps)
    return float(realized_bps.mean() / modeled_bps) if modeled_bps else None


def _benchmark_returns(window: pd.DatetimeIndex) -> dict[str, pd.Series]:
    """KXBTCPERP-HODL + EW basket daily returns over the OOS window."""
    out: dict[str, pd.Series] = {}
    try:
        from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT
        from crypto_trading.crypto_common.loader import load_perp_candles
        closes = {}
        for t in ACTIVE_PERPS_SNAPSHOT:
            try:
                c = load_perp_candles(t, "1d")
                closes[t] = (c.bid_close + c.ask_close) / 2
            except FileNotFoundError:
                continue
        panel = pd.DataFrame(closes)
        if panel.empty:
            return out
        panel = panel.reindex(window).ffill(limit=2)
        rets = panel.pct_change()
        if "KXBTCPERP" in rets.columns:
            out["btc_hodl"] = rets["KXBTCPERP"].dropna()
        out["ew_basket"] = rets.mean(axis=1, skipna=True).dropna()
    except Exception as e:
        log.warning(f"benchmark rebuild failed: {e}")
    return out


# ── gates ───────────────────────────────────────────────────────────────────

def validate_basis(*, allow_no_live: bool = False) -> dict:
    """Plan 01 §8 gate."""
    report: dict = {"strategy": "basis_meanrev", "gates": {}, "missing": []}
    oos, why = _load_oos_equity("basis_meanrev")
    if oos is None:
        report["missing"].append(f"walk-forward OOS equity ({why})")
    bt = _latest_backtest("basis_meanrev")
    if bt is None:
        report["missing"].append("backtest summary json")
    elif bt.get("fee_scenario") != "projected":
        report["gates"]["fee_scenario"] = {
            "ok": False, "detail": f"latest backtest ran fees={bt.get('fee_scenario')} "
                                   "— gate requires projected"}

    if report["missing"]:
        report["PASS"] = False
        report["reason"] = "insufficient data: " + "; ".join(report["missing"])
        return report

    rets = oos["equity"].pct_change().dropna()
    m = _metrics(rets)
    report["oos_metrics"] = m
    report["gates"]["oos_sharpe"] = {
        "ok": bool(m["sharpe"] >= BASIS_SHARPE_FLOOR),
        "detail": f"OOS net Sharpe {m['sharpe']:.2f} vs floor {BASIS_SHARPE_FLOOR} (projected fees)"}
    report["gates"]["max_dd"] = {
        "ok": bool(m["max_dd"] >= BASIS_MAXDD_BUDGET),
        "detail": f"OOS maxDD {m['max_dd']:.1%} vs budget {BASIS_MAXDD_BUDGET:.0%}"}

    ratio = _live_slippage_ratio("basis_meanrev")
    if ratio is None:
        if allow_no_live:
            report["gates"]["slippage"] = {
                "ok": True, "detail": "no live fills yet — WAIVED via --allow-no-live"}
        else:
            report["PASS"] = False
            report["reason"] = ("insufficient data: no live fills — realized-vs-modeled "
                                "slippage gate not evaluable (use --allow-no-live for a "
                                "paper-only verdict)")
            return report
    else:
        report["gates"]["slippage"] = {
            "ok": bool(ratio <= BASIS_SLIPPAGE_RATIO_CAP),
            "detail": f"realized/modeled slippage {ratio:.2f} vs cap {BASIS_SLIPPAGE_RATIO_CAP}"}

    report["PASS"] = all(g["ok"] for g in report["gates"].values())
    if not report["PASS"]:
        report["reason"] = "; ".join(f"{k}: {g['detail']}"
                                     for k, g in report["gates"].items() if not g["ok"])
    return report


def validate_perp_rotation() -> dict:
    """Plan 05 §8 gate (mirrors the AISS win criterion structure)."""
    report: dict = {"strategy": "perp_rotation", "gates": {}, "missing": []}
    oos, why = _load_oos_equity("perp_rotation")
    if oos is None:
        report["missing"].append(f"walk-forward OOS equity ({why})")
        report["PASS"] = False
        report["reason"] = "insufficient data: " + "; ".join(report["missing"])
        return report

    rets = oos["equity"].pct_change().dropna()
    strat = _metrics(rets)
    report["perp_rotation_metrics"] = strat

    # benchmarks: columns in the artifact win; else rebuild from candles
    benches: dict[str, pd.Series] = {}
    for col, name in (("btc_hodl", "btc_hodl"), ("ew_basket", "ew_basket")):
        if col in oos.columns:
            benches[name] = oos[col].pct_change().dropna()
    if len(benches) < 2:
        benches = {**_benchmark_returns(oos.index), **benches}
    hurdles = [b for b in ("btc_hodl", "ew_basket") if b in benches
               and len(benches[b].dropna()) >= 10]
    if len(hurdles) < 2:
        report["PASS"] = False
        report["reason"] = ("insufficient data: benchmark series unavailable "
                            f"(have {hurdles or 'none'}; need btc_hodl + ew_basket)")
        return report

    beats = {}
    for b in hurdles:
        bm = _metrics(benches[b])
        report[b] = bm
        beats[b] = {"sharpe": bool(strat["sharpe"] > bm["sharpe"]),
                    "cagr": bool(strat["cagr"] > bm["cagr"]),
                    "max_dd": bool(strat["max_dd"] > bm["max_dd"])}
    report["beats"] = beats
    report["gates"]["beat_benchmarks"] = {
        "ok": all(beats[b]["sharpe"] and beats[b]["cagr"] for b in hurdles),
        "detail": f"must beat BOTH {hurdles} on Sharpe AND CAGR (OOS, projected costs)"}
    report["gates"]["max_dd"] = {
        "ok": bool(strat["max_dd"] >= ROTATION_MAXDD_BUDGET),
        "detail": f"OOS maxDD {strat['max_dd']:.1%} vs budget {ROTATION_MAXDD_BUDGET:.0%}"}

    report["PASS"] = all(g["ok"] for g in report["gates"].values())
    if not report["PASS"]:
        report["reason"] = "; ".join(f"{k}: {g['detail']}"
                                     for k, g in report["gates"].items() if not g["ok"])
    return report


def validate_liq_reversion() -> dict:
    """Plan 04 §8 gate: OOS net Sharpe ≥ floor (projected fees, maker) + maxDD
    budget. FAIL-safe on missing artifacts."""
    report: dict = {"strategy": "liq_reversion", "gates": {}, "missing": []}
    oos, why = _load_oos_equity("liq_reversion")
    if oos is None:
        report["missing"].append(f"walk-forward OOS equity ({why})")
        report["PASS"] = False
        report["reason"] = "insufficient data: " + "; ".join(report["missing"])
        return report
    rets = oos["equity"].pct_change().dropna()
    m = _metrics(rets)
    report["oos_metrics"] = m
    report["gates"]["oos_sharpe"] = {
        "ok": bool(m["sharpe"] >= LIQ_SHARPE_FLOOR),
        "detail": f"OOS net Sharpe {m['sharpe']:.2f} vs floor {LIQ_SHARPE_FLOOR} "
                  "(projected fees, maker/liquidity-provision)"}
    report["gates"]["max_dd"] = {
        "ok": bool(m["max_dd"] >= LIQ_MAXDD_BUDGET),
        "detail": f"OOS maxDD {m['max_dd']:.1%} vs budget {LIQ_MAXDD_BUDGET:.0%}"}
    report["PASS"] = all(g["ok"] for g in report["gates"].values())
    if not report["PASS"]:
        report["reason"] = "; ".join(f"{k}: {g['detail']}"
                                     for k, g in report["gates"].items() if not g["ok"])
    return report


def validate_event_perp() -> dict:
    """Plan 02 SIGNAL gate (NOT P&L — the two-leg hedge/settlement is deferred).
    PASS iff the fair-value-gap dislocation IC holds out-of-sample: mean OOS IC ≥
    floor across ≥ MIN_FOLDS folds with a consistent (positive) sign."""
    report: dict = {"strategy": "event_perp", "gates": {}, "missing": [],
                    "note": "SIGNAL validation (OOS IC), not P&L — two-leg hedge deferred"}
    p = _wf_dir() / "event_perp_oos_ic.csv"
    if not p.exists():
        report["PASS"] = False
        report["reason"] = f"insufficient data: missing {p.name} (run wf --strategy event_perp)"
        return report
    df = pd.read_csv(p)
    ic = pd.to_numeric(df.get("oos_ic"), errors="coerce").dropna()
    n = len(ic)
    if n < EVENT_MIN_FOLDS:
        report["PASS"] = False
        report["reason"] = (f"insufficient data: only {n} OOS-IC folds "
                            f"(need ≥ {EVENT_MIN_FOLDS}; more strip days required)")
        return report
    mean_ic = float(ic.mean())
    frac_pos = float((ic > 0).mean())
    report["oos_ic"] = {"mean": round(mean_ic, 4), "n_folds": n,
                        "fraction_positive": round(frac_pos, 2)}
    report["gates"]["mean_oos_ic"] = {
        "ok": bool(mean_ic >= EVENT_IC_FLOOR),
        "detail": f"mean OOS IC {mean_ic:.3f} vs floor {EVENT_IC_FLOOR}"}
    report["gates"]["sign_consistency"] = {
        "ok": bool(frac_pos >= 0.6),
        "detail": f"{frac_pos:.0%} of folds positive (need ≥ 60%)"}
    report["PASS"] = all(g["ok"] for g in report["gates"].values())
    if not report["PASS"]:
        report["reason"] = "; ".join(f"{k}: {g['detail']}"
                                     for k, g in report["gates"].items() if not g["ok"])
    return report


def _print(report: dict) -> None:
    """Template's verdict printout shape."""
    print("=" * 78)
    print(f"VALIDATION GATE — {report['strategy']}")
    print("=" * 78)
    if report.get("note"):
        print(f"  ({report['note']})")
    for key in ("oos_metrics", "perp_rotation_metrics", "btc_hodl", "ew_basket"):
        m = report.get(key)
        if m:
            print(f"{key:24}: CAGR {m['cagr']:8.1%}  Sharpe {m['sharpe']:6.2f}  "
                  f"MaxDD {m['max_dd']:7.1%}")
    if report.get("oos_ic"):
        o = report["oos_ic"]
        print(f"{'oos_ic':24}: mean {o['mean']:+.3f}  folds {o['n_folds']}  "
              f"positive {o['fraction_positive']:.0%}")
    for name, g in report.get("gates", {}).items():
        print(f"  gate {name:16}: {'PASS' if g['ok'] else 'fail'} — {g['detail']}")
    print("-" * 78)
    verdict = "PASS ✅" if report.get("PASS") else "FAIL ❌"
    print(f"VERDICT: {verdict}")
    if not report.get("PASS") and report.get("reason"):
        print(f"REASON : {report['reason']}")
    print("=" * 78)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Crypto strategy validation gates")
    ap.add_argument("--strategy", required=True,
                    choices=["basis_meanrev", "perp_rotation", "liq_reversion",
                             "event_perp"])
    ap.add_argument("--allow-no-live", action="store_true",
                    help="basis gate: waive the live-slippage check (paper verdict)")
    ap.add_argument("--json", default=None, help="write full report JSON here")
    args = ap.parse_args(argv)
    if args.strategy == "basis_meanrev":
        report = validate_basis(allow_no_live=args.allow_no_live)
    elif args.strategy == "perp_rotation":
        report = validate_perp_rotation()
    elif args.strategy == "liq_reversion":
        report = validate_liq_reversion()
    else:
        report = validate_event_perp()
    _print(report)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, default=float))
        print(f"\nWrote {args.json}")
    sys.exit(0 if report.get("PASS") else 1)


if __name__ == "__main__":
    main()
