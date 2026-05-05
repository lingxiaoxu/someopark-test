"""
walk_forward.py — Walk-Forward IS/OOS Framework for Sector Rotation
====================================================================
Dense walk-forward analysis with:
  - Expanding (anchored) or rolling fixed-width IS window
  - Short step size (default 10 trading days ≈ 2 weeks) → ~45 folds
  - Embargo gap between IS and OOS (default 5 trading days)
  - 59-param-set sweep per fold with macro-conditioned selection
  - Deflated Sharpe Ratio (Bailey & López de Prado 2014) for multiple-testing
  - Walk-Forward Efficiency (WFE = OOS_SR / IS_SR)
  - Synthetic OOS equity curve from stitched fold segments

Theory
------
  WFO:   Pardo (2008) — rolling IS/OOS evaluation
  DSR:   Bailey & López de Prado (2014) — adjust Sharpe for N=59 trials
  CPCV:  López de Prado (2018) — purging & embargo for time-series
  CPO:   Chan, Belov & Ciobanu (2021) — regime-conditioned param selection

Usage
-----
    from sector_rotation.walk_forward import WalkForwardAnalyzer
    analyzer = WalkForwardAnalyzer(base_cfg, prices, macro)
    result   = analyzer.run()          # returns WFResult
    print(result.summary())
"""
from __future__ import annotations

import logging
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import scipy.stats

logger = logging.getLogger(__name__)

# ── Path setup for MacroStateStore ──────────────────────────────────────────
_THIS_DIR = Path(__file__).parent.resolve()
_PROJECT_DIR = _THIS_DIR.parent.parent.resolve()
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.data.loader import load_all, load_config


# ═══════════════════════════════════════════════════════════════════════════
#  Data classes
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class WFFold:
    """Time boundaries for a single walk-forward fold."""
    fold_id: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp           # last IS trading day (inclusive)
    embargo_end: pd.Timestamp      # last embargo day (inclusive)
    oos_start: pd.Timestamp        # first OOS trading day
    oos_end: pd.Timestamp          # last OOS trading day (inclusive)


@dataclass
class WFFoldResult:
    """Full result for one walk-forward fold."""
    fold: WFFold

    # ── IS phase ──────────────────────────────────────────────────────────
    is_metrics: Dict[str, Dict[str, float]]   # {param_set_name: {sharpe, calmar, ...}}
    is_best_name: str                          # selected param set
    is_best_sharpe: float                      # IS Sharpe of selected set
    is_macro_vector: Dict[str, float]          # IS-period last-30d mean SIMILARITY_FEATURES
    selection_method: str                      # "mcps" | "is_sharpe" | "fallback"
    mcps_score: float                          # macro-cond Sharpe of selected set (nan if fallback)
    dsr_pvalue: float                          # DSR p-value of selected (adjusted for N=59)

    # ── OOS phase ─────────────────────────────────────────────────────────
    oos_name: str                              # param set used in OOS (= is_best_name)
    oos_equity: pd.Series                      # OOS daily equity curve (base=1.0)
    oos_metrics: Dict[str, float]              # {sharpe, calmar, maxdd, ann_ret, ann_vol, ...}
    oos_regime: str                            # dominant macro regime during OOS

    # ── Efficiency ────────────────────────────────────────────────────────
    wfe: float                                 # Walk-Forward Efficiency = OOS_SR / IS_SR


@dataclass
class WFResult:
    """Aggregate walk-forward result across all folds."""
    folds: List[WFFoldResult]
    mode: str                              # "anchored" or "rolling"
    n_param_sets: int

    # ── Synthetic OOS track record ────────────────────────────────────────
    synthetic_equity: pd.Series            # stitched OOS segments
    synthetic_metrics: Dict[str, float]    # aggregate metrics on synthetic curve

    # ── Statistical tests ─────────────────────────────────────────────────
    dsr_aggregate: float                   # DSR on synthetic track record
    mean_wfe: float                        # mean WFE across folds

    # ── Per-fold summary ──────────────────────────────────────────────────
    selection_log: List[Dict[str, Any]]    # per-fold selection record
    fold_summary_df: pd.DataFrame          # tabular fold-by-fold

    # ── Per-param aggregate OOS stats ─────────────────────────────────────
    param_oos_stats: Dict[str, Dict[str, float]]  # avg OOS when param was selected

    def summary(self) -> str:
        """Human-readable multi-line summary."""
        lines = [
            f"\n{'═' * 70}",
            f"  WALK-FORWARD ANALYSIS  ({self.mode.upper()} IS)",
            f"  {len(self.folds)} folds × {self.n_param_sets} param sets",
            f"{'═' * 70}",
        ]
        sm = self.synthetic_metrics
        lines.append(f"  Synthetic OOS Sharpe  : {sm.get('sharpe', float('nan')):.3f}")
        lines.append(f"  Synthetic OOS CAGR    : {sm.get('ann_ret', float('nan')):.1%}")
        lines.append(f"  Synthetic OOS MaxDD   : {sm.get('maxdd', float('nan')):.1%}")
        lines.append(f"  Synthetic OOS Calmar  : {sm.get('calmar', float('nan')):.3f}")
        lines.append(f"  DSR (N={self.n_param_sets})        : {self.dsr_aggregate:.3f}")
        lines.append(f"  Mean Walk-Forward Eff : {self.mean_wfe:.3f}")
        lines.append(f"{'─' * 70}")

        # top selected param sets
        from collections import Counter
        sel_counts = Counter(f.is_best_name for f in self.folds)
        lines.append("  Most selected param sets:")
        for name, cnt in sel_counts.most_common(5):
            pct = cnt / len(self.folds) * 100
            lines.append(f"    {name:<30} {cnt:>3} folds ({pct:.0f}%)")

        # WFE distribution
        wfes = [f.wfe for f in self.folds if not np.isnan(f.wfe)]
        if wfes:
            lines.append(f"  WFE distribution: min={min(wfes):.2f}  "
                         f"median={np.median(wfes):.2f}  max={max(wfes):.2f}")

        lines.append(f"{'═' * 70}\n")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
#  Statistical utilities
# ═══════════════════════════════════════════════════════════════════════════

def expected_max_sharpe(n_trials: int, var_sharpes: float) -> float:
    """
    Expected maximum Sharpe ratio under null hypothesis (all true SR=0).
    Bailey & López de Prado (2014), Eq. 7.
    """
    if n_trials <= 1 or var_sharpes <= 0:
        return 0.0
    gamma = 0.5772156649  # Euler–Mascheroni
    std_sr = np.sqrt(var_sharpes)
    z1 = scipy.stats.norm.ppf(1.0 - 1.0 / max(n_trials, 2))
    z2 = scipy.stats.norm.ppf(1.0 - 1.0 / (max(n_trials, 2) * np.exp(-1)))
    return std_sr * ((1 - gamma) * z1 + gamma * z2)


def deflated_sharpe_ratio(
    sr_obs: float,
    sr_0: float,
    T: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """
    DSR p-value: probability that observed SR is genuine after N-trial adjustment.
    Bailey & López de Prado (2014), Eq. 14.

    Returns a value in [0, 1]; > 0.95 means survives at 5% significance.
    """
    if T <= 1:
        return 0.0
    excess_kurt = kurt - 3.0
    denom_sq = 1.0 - skew * sr_obs + (excess_kurt / 4.0) * sr_obs ** 2
    if denom_sq <= 0:
        denom_sq = 1e-6
    stat = (sr_obs - sr_0) * np.sqrt(T - 1) / np.sqrt(denom_sq)
    return float(scipy.stats.norm.cdf(stat))


def _compute_metrics_from_equity(eq: pd.Series) -> Dict[str, float]:
    """Compute standard metrics from a daily equity curve (base-agnostic)."""
    if eq.empty or len(eq) < 2:
        return {k: float("nan") for k in
                ["sharpe", "calmar", "maxdd", "ann_ret", "ann_vol",
                 "skew", "kurt", "n_days"]}

    rets = eq.pct_change().dropna()
    if rets.empty or rets.std() == 0:
        return {k: float("nan") for k in
                ["sharpe", "calmar", "maxdd", "ann_ret", "ann_vol",
                 "skew", "kurt", "n_days"]}

    n = len(rets)
    ann_ret = float((eq.iloc[-1] / eq.iloc[0]) ** (252.0 / max(n, 1)) - 1)
    ann_vol = float(rets.std() * np.sqrt(252))
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252))

    cum = (1 + rets).cumprod()
    drawdown = cum / cum.expanding().max() - 1
    maxdd = float(drawdown.min())
    calmar = ann_ret / abs(maxdd) if maxdd != 0 else float("nan")

    return {
        "sharpe": sharpe,
        "calmar": calmar,
        "maxdd": maxdd,
        "ann_ret": ann_ret,
        "ann_vol": ann_vol,
        "skew": float(rets.skew()),
        "kurt": float(rets.kurtosis() + 3),  # excess → raw
        "n_days": n,
    }


def _macro_cond_sharpe_is(
    equity_is: pd.Series,
    macro_is: pd.DataFrame,
    today_vec: Dict[str, float],
    features: List[str],
    min_overlap: int = 60,
) -> float:
    """
    Macro-conditioned Sharpe computed STRICTLY on IS data.
    Delegates to MCPS.macro_cond_sharpe() — single source of truth.
    """
    try:
        from MCPS import macro_cond_sharpe
    except ImportError:
        # Fallback: project root might not be in path
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "MCPS", str(_PROJECT_DIR / "MCPS.py"))
        _mcps_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mcps_mod)
        macro_cond_sharpe = _mcps_mod.macro_cond_sharpe

    return macro_cond_sharpe(
        equity=equity_is,
        macro_df=macro_is,
        today_vec=today_vec,
        features=features,
        min_overlap=min_overlap,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  WalkForwardAnalyzer
# ═══════════════════════════════════════════════════════════════════════════

class WalkForwardAnalyzer:
    """
    Walk-forward IS/OOS framework for 59 sector rotation param sets.

    Parameters
    ----------
    base_cfg : dict
        Base config from load_config().
    prices : pd.DataFrame
        Daily adjusted close prices (ETFs + benchmark).
    macro : pd.DataFrame
        Daily macro indicators.
    is_years_min : int
        Minimum IS window in years (first fold).
    oos_months : int
        OOS evaluation window in months.
    step_days : int
        Roll forward by this many trading days each fold.
        Default 10 ≈ 2 calendar weeks → ~45 folds for 8-year backtest.
    embargo_days : int
        Number of trading days removed between IS end and OOS start.
    mode : str
        "anchored" — IS always starts from backtest_start (expanding IS).
        "rolling"  — IS window is fixed-width (most recent is_years_min years).
    param_sets : dict or None
        Override PARAM_SETS (default: all 59).
    """

    def __init__(
        self,
        base_cfg: dict,
        prices: pd.DataFrame,
        macro: pd.DataFrame,
        is_years_min: int = 3,
        oos_months: int = 6,
        step_days: int = 10,
        embargo_days: int = 5,
        mode: str = "anchored",
        param_sets: Optional[Dict[str, dict]] = None,
    ):
        self.base_cfg = base_cfg
        self.prices = prices
        self.macro = macro
        self.is_years_min = is_years_min
        self.oos_months = oos_months
        self.step_days = step_days
        self.embargo_days = embargo_days
        self.mode = mode
        self.param_sets = param_sets or PARAM_SETS
        self._set_names = list(self.param_sets.keys())

        bt_cfg = base_cfg.get("backtest", {})
        self._bt_start = pd.Timestamp(bt_cfg.get("start_date", "2018-07-01"))
        self._bt_end = pd.Timestamp(
            bt_cfg.get("end_date") or prices.index[-1].strftime("%Y-%m-%d")
        )

        # Load macro state store for MCPS
        self._macro_store = None
        self._similarity_features: List[str] = []
        try:
            from MacroStateStore import MacroStateStore, SIMILARITY_FEATURES
            self._macro_store = MacroStateStore()
            self._similarity_features = list(SIMILARITY_FEATURES)
        except Exception as e:
            logger.warning(f"MacroStateStore unavailable ({e}); "
                           f"falling back to IS Sharpe selection")

    # ──────────────────────────────────────────────────────────────────────
    #  Fold generation
    # ──────────────────────────────────────────────────────────────────────

    def generate_folds(self) -> List[WFFold]:
        """Generate dense walk-forward folds with embargo."""
        trading_dates = self.prices.loc[self._bt_start: self._bt_end].index
        if trading_dates.empty:
            return []

        min_is_len = self.is_years_min * 252  # approximate
        oos_len = int(self.oos_months * 21)   # approximate trading days

        folds: List[WFFold] = []
        fold_id = 0

        # First possible OOS start: after min IS period + embargo
        first_oos_idx = min_is_len + self.embargo_days
        if first_oos_idx >= len(trading_dates):
            logger.warning("Not enough data for even one fold")
            return []

        cursor = first_oos_idx
        while cursor + oos_len <= len(trading_dates):
            oos_start_idx = cursor
            oos_end_idx = min(cursor + oos_len - 1, len(trading_dates) - 1)

            # IS end is embargo_days before OOS start
            is_end_idx = oos_start_idx - self.embargo_days - 1
            if is_end_idx < 0:
                cursor += self.step_days
                continue

            # IS start depends on mode
            if self.mode == "anchored":
                is_start_idx = 0
            else:  # rolling
                is_start_idx = max(0, is_end_idx - min_is_len + 1)

            # Embargo period
            embargo_end_idx = oos_start_idx - 1

            folds.append(WFFold(
                fold_id=fold_id,
                is_start=trading_dates[is_start_idx],
                is_end=trading_dates[is_end_idx],
                embargo_end=trading_dates[embargo_end_idx],
                oos_start=trading_dates[oos_start_idx],
                oos_end=trading_dates[oos_end_idx],
            ))
            fold_id += 1
            cursor += self.step_days

        logger.info(f"Generated {len(folds)} walk-forward folds "
                    f"(mode={self.mode}, step={self.step_days}d, "
                    f"oos={self.oos_months}m, embargo={self.embargo_days}d)")
        return folds

    # ──────────────────────────────────────────────────────────────────────
    #  Pre-run all 59 backtests (full period, once)
    # ──────────────────────────────────────────────────────────────────────

    def _prerun_all(self) -> Dict[str, pd.Series]:
        """
        Run all param sets for the full backtest period ONCE.

        Returns dict of {name: equity_curve}. Signal computation at time t
        uses only data up to t (causal), so slicing IS/OOS from the full
        curve is valid — no future information leakage in the equity itself.
        The only source of overfitting is PARAMETER SELECTION, which the
        walk-forward framework addresses by restricting selection to IS data.
        """
        eq_map: Dict[str, pd.Series] = {}
        n = len(self._set_names)
        logger.info(f"Pre-running {n} backtests for full-period equity curves...")

        for i, name in enumerate(self._set_names):
            try:
                cfg = apply_param_set(self.base_cfg, self.param_sets[name])
                engine = SectorRotationBacktest(cfg)
                result = engine.run(prices=self.prices, macro=self.macro)
                if result.equity_curve is not None and not result.equity_curve.empty:
                    eq_map[name] = result.equity_curve
            except Exception as exc:
                logger.debug(f"  [{name}] failed: {exc}")

            if (i + 1) % 10 == 0 or (i + 1) == n:
                logger.info(f"  Pre-run progress: {i + 1}/{n}")

        logger.info(f"Pre-run complete: {len(eq_map)}/{n} successful")
        return eq_map

    # ──────────────────────────────────────────────────────────────────────
    #  Load macro DataFrame for MCPS
    # ──────────────────────────────────────────────────────────────────────

    def _load_macro_df(self) -> pd.DataFrame:
        """Load full macro state DataFrame from MacroStateStore."""
        if self._macro_store is None:
            return pd.DataFrame()
        try:
            return self._macro_store.load(str(self._bt_start.date()))
        except Exception as e:
            logger.warning(f"MacroStateStore.load failed: {e}")
            return pd.DataFrame()

    # ──────────────────────────────────────────────────────────────────────
    #  Evaluate one fold
    # ──────────────────────────────────────────────────────────────────────

    def _evaluate_fold(
        self,
        fold: WFFold,
        eq_map: Dict[str, pd.Series],
        macro_df: pd.DataFrame,
    ) -> WFFoldResult:
        """Run IS selection + OOS evaluation for a single fold."""
        features = self._similarity_features

        # ── IS metrics for all param sets ─────────────────────────────────
        is_metrics: Dict[str, Dict[str, float]] = {}
        for name, eq in eq_map.items():
            eq_is = eq[(eq.index >= fold.is_start) & (eq.index <= fold.is_end)]
            if len(eq_is) < 60:
                continue
            is_metrics[name] = _compute_metrics_from_equity(eq_is)

        if not is_metrics:
            # Fallback: return empty fold with first param set
            fallback_name = self._set_names[0]
            return self._make_fallback_fold(fold, fallback_name, eq_map)

        # ── IS macro vector (last 30 trading days of IS) ─────────────────
        is_macro_vec: Dict[str, float] = {}
        if not macro_df.empty and features:
            macro_is = macro_df[(macro_df.index >= fold.is_start) &
                                (macro_df.index <= fold.is_end)]
            if len(macro_is) >= 30:
                tail30 = macro_is[features].tail(30).mean()
                is_macro_vec = {
                    f: float(tail30[f]) if f in tail30.index and not pd.isna(tail30[f])
                    else None
                    for f in features
                }

        # ── Macro-conditioned Sharpe (MCPS) on IS data ───────────────────
        mcs_scores: Dict[str, float] = {}
        selection_method = "is_sharpe"

        if is_macro_vec and not macro_df.empty and features:
            macro_is_df = macro_df[(macro_df.index >= fold.is_start) &
                                   (macro_df.index <= fold.is_end)]
            for name, eq in eq_map.items():
                if name not in is_metrics:
                    continue
                eq_is = eq[(eq.index >= fold.is_start) & (eq.index <= fold.is_end)]
                score = _macro_cond_sharpe_is(
                    eq_is, macro_is_df, is_macro_vec, features
                )
                if not np.isnan(score):
                    mcs_scores[name] = score

            if len(mcs_scores) >= 3:
                selection_method = "mcps"

        # ── DSR filter + selection ────────────────────────────────────────
        n_trials = len(is_metrics)

        if selection_method == "mcps":
            all_scores = list(mcs_scores.values())
            score_var = float(np.var(all_scores)) if len(all_scores) > 1 else 0.01
            sr_0 = expected_max_sharpe(n_trials, score_var)

            best_name = max(mcs_scores, key=mcs_scores.get)
            best_score = mcs_scores[best_name]
            best_is_m = is_metrics.get(best_name, {})
            dsr_p = deflated_sharpe_ratio(
                sr_obs=best_score,
                sr_0=sr_0,
                T=int(best_is_m.get("n_days", 252)),
                skew=best_is_m.get("skew", 0.0),
                kurt=best_is_m.get("kurt", 3.0),
            )
        else:
            # Fallback: select by IS Sharpe
            all_sharpes = {n: m.get("sharpe", float("-inf"))
                          for n, m in is_metrics.items()
                          if not np.isnan(m.get("sharpe", float("nan")))}
            if not all_sharpes:
                best_name = self._set_names[0]
                best_score = float("nan")
                dsr_p = 0.0
            else:
                score_var = float(np.var(list(all_sharpes.values()))) if len(all_sharpes) > 1 else 0.01
                sr_0 = expected_max_sharpe(n_trials, score_var)

                best_name = max(all_sharpes, key=all_sharpes.get)
                best_score = all_sharpes[best_name]
                best_is_m = is_metrics.get(best_name, {})
                dsr_p = deflated_sharpe_ratio(
                    sr_obs=best_score,
                    sr_0=sr_0,
                    T=int(best_is_m.get("n_days", 252)),
                    skew=best_is_m.get("skew", 0.0),
                    kurt=best_is_m.get("kurt", 3.0),
                )

        # ── OOS evaluation ────────────────────────────────────────────────
        oos_eq = pd.Series(dtype=float)
        oos_metrics: Dict[str, float] = {}

        if best_name in eq_map:
            eq_full = eq_map[best_name]
            seg = eq_full[(eq_full.index >= fold.oos_start) &
                          (eq_full.index <= fold.oos_end)]
            if not seg.empty:
                oos_eq = seg / seg.iloc[0]  # normalize to base 1.0
                oos_metrics = _compute_metrics_from_equity(oos_eq)

        # ── OOS dominant regime ───────────────────────────────────────────
        oos_regime = "unknown"
        if not macro_df.empty and "vix" in macro_df.columns:
            vix_oos = macro_df.loc[
                (macro_df.index >= fold.oos_start) &
                (macro_df.index <= fold.oos_end), "vix"
            ].dropna()
            if not vix_oos.empty:
                mean_vix = float(vix_oos.mean())
                if mean_vix > 30:
                    oos_regime = "risk_off"
                elif mean_vix > 20:
                    oos_regime = "transition"
                else:
                    oos_regime = "risk_on"

        # ── Walk-Forward Efficiency ───────────────────────────────────────
        is_sr = is_metrics.get(best_name, {}).get("sharpe", float("nan"))
        oos_sr = oos_metrics.get("sharpe", float("nan"))
        wfe = float("nan")
        if not np.isnan(is_sr) and not np.isnan(oos_sr) and abs(is_sr) > 1e-6:
            wfe = oos_sr / is_sr

        return WFFoldResult(
            fold=fold,
            is_metrics=is_metrics,
            is_best_name=best_name,
            is_best_sharpe=is_sr,
            is_macro_vector=is_macro_vec,
            selection_method=selection_method,
            mcps_score=best_score,
            dsr_pvalue=dsr_p,
            oos_name=best_name,
            oos_equity=oos_eq,
            oos_metrics=oos_metrics,
            oos_regime=oos_regime,
            wfe=wfe,
        )

    def _make_fallback_fold(
        self, fold: WFFold, name: str, eq_map: Dict[str, pd.Series]
    ) -> WFFoldResult:
        """Create a minimal fold result when IS data is insufficient."""
        oos_eq = pd.Series(dtype=float)
        if name in eq_map:
            seg = eq_map[name][
                (eq_map[name].index >= fold.oos_start) &
                (eq_map[name].index <= fold.oos_end)
            ]
            if not seg.empty:
                oos_eq = seg / seg.iloc[0]

        return WFFoldResult(
            fold=fold,
            is_metrics={},
            is_best_name=name,
            is_best_sharpe=float("nan"),
            is_macro_vector={},
            selection_method="fallback",
            mcps_score=float("nan"),
            dsr_pvalue=0.0,
            oos_name=name,
            oos_equity=oos_eq,
            oos_metrics=_compute_metrics_from_equity(oos_eq),
            oos_regime="unknown",
            wfe=float("nan"),
        )

    # ──────────────────────────────────────────────────────────────────────
    #  Main run
    # ──────────────────────────────────────────────────────────────────────

    def run(self) -> WFResult:
        """Execute full walk-forward analysis."""
        folds = self.generate_folds()
        if not folds:
            logger.error("No folds generated — insufficient data")
            return self._empty_result()

        eq_map = self._prerun_all()
        if not eq_map:
            logger.error("All backtests failed — no equity curves")
            return self._empty_result()

        macro_df = self._load_macro_df()

        # ── Evaluate each fold ────────────────────────────────────────────
        fold_results: List[WFFoldResult] = []
        for i, fold in enumerate(folds):
            logger.info(
                f"  Fold {fold.fold_id + 1}/{len(folds)}: "
                f"IS=[{fold.is_start.date()}→{fold.is_end.date()}] "
                f"OOS=[{fold.oos_start.date()}→{fold.oos_end.date()}]"
            )
            fr = self._evaluate_fold(fold, eq_map, macro_df)
            fold_results.append(fr)
            logger.info(
                f"    → selected={fr.is_best_name} "
                f"(method={fr.selection_method}, "
                f"IS_SR={fr.is_best_sharpe:.3f}, "
                f"OOS_SR={fr.oos_metrics.get('sharpe', float('nan')):.3f}, "
                f"WFE={fr.wfe:.2f})"
            )

        # ── Stitch synthetic OOS equity ───────────────────────────────────
        # Use non-overlapping segments: for overlapping dates, take the
        # most recent fold's equity (last fold to cover that date wins).
        # This produces a clean, non-overlapping synthetic track record.
        synthetic_eq = self._stitch_oos(fold_results)
        synthetic_metrics = _compute_metrics_from_equity(synthetic_eq)

        # ── Aggregate DSR on synthetic track ──────────────────────────────
        n_sets = len(self.param_sets)
        all_oos_sharpes = [
            fr.oos_metrics.get("sharpe", float("nan")) for fr in fold_results
        ]
        valid_oos_sharpes = [s for s in all_oos_sharpes if not np.isnan(s)]
        dsr_agg = 0.0
        if valid_oos_sharpes and not np.isnan(synthetic_metrics.get("sharpe", float("nan"))):
            var_s = float(np.var(valid_oos_sharpes)) if len(valid_oos_sharpes) > 1 else 0.01
            sr_0 = expected_max_sharpe(n_sets, var_s)
            dsr_agg = deflated_sharpe_ratio(
                sr_obs=synthetic_metrics["sharpe"],
                sr_0=sr_0,
                T=int(synthetic_metrics.get("n_days", 252)),
                skew=synthetic_metrics.get("skew", 0.0),
                kurt=synthetic_metrics.get("kurt", 3.0),
            )

        # ── Mean WFE ─────────────────────────────────────────────────────
        wfes = [fr.wfe for fr in fold_results if not np.isnan(fr.wfe)]
        mean_wfe = float(np.mean(wfes)) if wfes else float("nan")

        # ── Selection log ─────────────────────────────────────────────────
        selection_log = [
            {
                "fold": fr.fold.fold_id,
                "is_start": str(fr.fold.is_start.date()),
                "is_end": str(fr.fold.is_end.date()),
                "oos_start": str(fr.fold.oos_start.date()),
                "oos_end": str(fr.fold.oos_end.date()),
                "selected": fr.is_best_name,
                "method": fr.selection_method,
                "is_sharpe": round(fr.is_best_sharpe, 4),
                "mcps_score": round(fr.mcps_score, 4) if not np.isnan(fr.mcps_score) else None,
                "dsr_pvalue": round(fr.dsr_pvalue, 4),
                "oos_sharpe": round(fr.oos_metrics.get("sharpe", float("nan")), 4),
                "oos_return": round(fr.oos_metrics.get("ann_ret", float("nan")), 4),
                "oos_maxdd": round(fr.oos_metrics.get("maxdd", float("nan")), 4),
                "oos_regime": fr.oos_regime,
                "wfe": round(fr.wfe, 4) if not np.isnan(fr.wfe) else None,
            }
            for fr in fold_results
        ]

        # ── Fold summary DataFrame ────────────────────────────────────────
        fold_summary_df = pd.DataFrame(selection_log)

        # ── Per-param OOS aggregate ───────────────────────────────────────
        param_oos_stats: Dict[str, Dict[str, float]] = {}
        from collections import defaultdict
        param_buckets = defaultdict(list)
        for fr in fold_results:
            if fr.oos_metrics:
                param_buckets[fr.is_best_name].append(fr.oos_metrics)
        for name, metric_list in param_buckets.items():
            param_oos_stats[name] = {
                "n_selected": len(metric_list),
                "mean_oos_sharpe": float(np.mean([m.get("sharpe", float("nan"))
                                                   for m in metric_list])),
                "mean_oos_return": float(np.mean([m.get("ann_ret", float("nan"))
                                                   for m in metric_list])),
                "mean_oos_maxdd": float(np.mean([m.get("maxdd", float("nan"))
                                                  for m in metric_list])),
            }

        return WFResult(
            folds=fold_results,
            mode=self.mode,
            n_param_sets=n_sets,
            synthetic_equity=synthetic_eq,
            synthetic_metrics=synthetic_metrics,
            dsr_aggregate=dsr_agg,
            mean_wfe=mean_wfe,
            selection_log=selection_log,
            fold_summary_df=fold_summary_df,
            param_oos_stats=param_oos_stats,
        )

    # ──────────────────────────────────────────────────────────────────────
    #  Stitch OOS segments into synthetic track record
    # ──────────────────────────────────────────────────────────────────────

    def _stitch_oos(self, fold_results: List[WFFoldResult]) -> pd.Series:
        """
        Build synthetic OOS equity from non-overlapping segments.

        When folds overlap (dense stepping), we partition the timeline so
        each calendar date belongs to exactly one fold — the EARLIEST fold
        whose OOS window covers that date. This avoids double-counting and
        produces a proper out-of-sample track record.
        """
        if not fold_results:
            return pd.Series(dtype=float)

        # Collect all (date, return) pairs, assigning each date to earliest fold
        claimed_dates: set = set()
        segments: List[pd.Series] = []

        for fr in fold_results:
            if fr.oos_equity.empty:
                continue
            # Daily returns from this fold's OOS equity
            rets = fr.oos_equity.pct_change().dropna()
            # Keep only dates not yet claimed by earlier folds
            new_rets = rets[~rets.index.isin(claimed_dates)]
            if not new_rets.empty:
                segments.append(new_rets)
                claimed_dates.update(new_rets.index)

        if not segments:
            return pd.Series(dtype=float)

        all_rets = pd.concat(segments).sort_index()
        # Remove any remaining duplicates (safety)
        all_rets = all_rets[~all_rets.index.duplicated(keep="first")]
        # Build equity curve
        synthetic = (1 + all_rets).cumprod()
        return synthetic

    # ──────────────────────────────────────────────────────────────────────
    #  Empty result helper
    # ──────────────────────────────────────────────────────────────────────

    def _empty_result(self) -> WFResult:
        return WFResult(
            folds=[],
            mode=self.mode,
            n_param_sets=len(self.param_sets),
            synthetic_equity=pd.Series(dtype=float),
            synthetic_metrics={},
            dsr_aggregate=0.0,
            mean_wfe=float("nan"),
            selection_log=[],
            fold_summary_df=pd.DataFrame(),
            param_oos_stats={},
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience: run both anchored + rolling and return both
# ═══════════════════════════════════════════════════════════════════════════

def run_dual_mode(
    base_cfg: dict,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    **kwargs,
) -> Dict[str, WFResult]:
    """
    Run walk-forward analysis in both anchored and rolling modes.
    Returns {"anchored": WFResult, "rolling": WFResult}.
    """
    results = {}
    for mode in ("anchored", "rolling"):
        logger.info(f"\n{'═' * 60}")
        logger.info(f"  Walk-Forward Analysis: {mode.upper()} mode")
        logger.info(f"{'═' * 60}")
        analyzer = WalkForwardAnalyzer(
            base_cfg=base_cfg,
            prices=prices,
            macro=macro,
            mode=mode,
            **kwargs,
        )
        results[mode] = analyzer.run()
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Walk-Forward IS/OOS analysis for sector rotation"
    )
    parser.add_argument("--mode", default="both", choices=["anchored", "rolling", "both"],
                        help="IS window mode (default: both)")
    parser.add_argument("--is-years", type=int, default=3,
                        help="Minimum IS window in years (default: 3)")
    parser.add_argument("--oos-months", type=int, default=6,
                        help="OOS evaluation window in months (default: 12)")
    parser.add_argument("--step-days", type=int, default=15,
                        help="Step forward N trading days per fold (default: 10)")
    parser.add_argument("--embargo-days", type=int, default=5,
                        help="Embargo days between IS and OOS (default: 5)")
    parser.add_argument("--output-dir", default=None,
                        help="Directory for CSV output (default: backtest_results/)")
    args = parser.parse_args()

    base_cfg = load_config()
    prices, macro = load_all(config=base_cfg)

    out_dir = Path(args.output_dir) if args.output_dir else _THIS_DIR / "backtest_results"
    out_dir.mkdir(parents=True, exist_ok=True)

    common_kwargs = dict(
        is_years_min=args.is_years,
        oos_months=args.oos_months,
        step_days=args.step_days,
        embargo_days=args.embargo_days,
    )

    if args.mode == "both":
        results = run_dual_mode(base_cfg, prices, macro, **common_kwargs)
        for mode_name, wf_r in results.items():
            print(wf_r.summary())
            csv_path = out_dir / f"wf_{mode_name}_fold_summary.csv"
            wf_r.fold_summary_df.to_csv(csv_path, index=False)
            print(f"  Fold summary → {csv_path}")
    else:
        analyzer = WalkForwardAnalyzer(
            base_cfg=base_cfg,
            prices=prices,
            macro=macro,
            mode=args.mode,
            **common_kwargs,
        )
        wf_r = analyzer.run()
        print(wf_r.summary())
        csv_path = out_dir / f"wf_{args.mode}_fold_summary.csv"
        wf_r.fold_summary_df.to_csv(csv_path, index=False)
        print(f"  Fold summary → {csv_path}")
