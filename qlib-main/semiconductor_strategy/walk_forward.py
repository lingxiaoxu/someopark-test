"""
walk_forward.py — Walk-Forward IS/OOS Framework for AISS
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
    from semiconductor_strategy.walk_forward import WalkForwardAnalyzer
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

from semiconductor_strategy.AISSStrategyRuns import PARAM_SETS, apply_param_set
from semiconductor_strategy.backtest.engine import AISSBacktest
from semiconductor_strategy.data.loader import load_all, load_config


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
    selection_method: str                      # "oos_retrospective" | "mcps" | "is_sharpe" | "fallback"
    mcps_score: float                          # macro-cond Sharpe of selected set (nan if fallback)
    dsr_pvalue: float                          # DSR p-value of selected (adjusted for N=59)

    # ── OOS phase ─────────────────────────────────────────────────────────
    oos_name: str                              # param set used in OOS (= is_best_name)
    oos_equity: pd.Series                      # OOS daily equity curve (base=1.0)
    oos_metrics: Dict[str, float]              # {sharpe, calmar, maxdd, ann_ret, ann_vol, ...}
    oos_regime: str                            # dominant macro regime during OOS

    # ── Efficiency ────────────────────────────────────────────────────────
    wfe: float                                 # Walk-Forward Efficiency = OOS_SR / IS_SR

    # ── OOS retrospective data (populated for all folds, used by later folds) ──
    all_oos_sharpes: Dict[str, float] = field(default_factory=dict)
    # ^ OOS Sharpe for every param set over this fold's OOS window (not just selected)
    oos_macro_vec: Dict[str, float] = field(default_factory=dict)
    # ^ mean macro feature values during this fold's OOS period (query target for future folds)


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

    # ── Dynamic-selection analysis: oracle ceiling + static baseline ──────
    # (all defaulted → backward-compatible with old constructors / _empty_result)
    # oracle_equity   : per-fold argmax(OOS Sharpe) param stitched (theoretical
    #                   OOS ceiling — hindsight; what ANY per-fold selector could
    #                   at best achieve over the SAME fold partition as synthetic).
    # static_best_*   : single full-period best param (the "no dynamic selection"
    #                   baseline; IS/full-period optimistic, NOT an OOS number).
    # comparison      : 3-layer roll-up {static_best, synthetic, oracle} + capture
    #                   ratios + mean per-fold regret.
    oracle_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    oracle_metrics: Dict[str, float] = field(default_factory=dict)
    oracle_selection_log: List[Dict[str, Any]] = field(default_factory=list)
    static_best_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    static_best_metrics: Dict[str, float] = field(default_factory=dict)
    static_best_name: str = ""
    comparison: Dict[str, Any] = field(default_factory=dict)

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

        # ── Oracle ceiling vs realizable vs static (dynamic-selection analysis) ──
        cmp = getattr(self, "comparison", None)
        if cmp:
            sb, sy, orc = cmp.get("static_best", {}), cmp.get("synthetic", {}), cmp.get("oracle", {})
            lines.append(f"{'─' * 70}")
            lines.append("  DYNAMIC SELECTION — oracle ceiling vs realizable vs static")
            lines.append(f"    {'layer':<26}{'Sharpe':>9}{'CAGR':>9}{'MaxDD':>9}")
            lines.append(f"    {'static best (IS, full)':<26}{sb.get('sharpe', float('nan')):>9.2f}"
                         f"{sb.get('cagr', float('nan')):>9.1%}{sb.get('maxdd', float('nan')):>9.1%}"
                         f"   [{self.static_best_name}]")
            lines.append(f"    {'realizable synthetic OOS':<26}{sy.get('sharpe', float('nan')):>9.2f}"
                         f"{sy.get('cagr', float('nan')):>9.1%}{sy.get('maxdd', float('nan')):>9.1%}")
            lines.append(f"    {'oracle ceiling OOS':<26}{orc.get('sharpe', float('nan')):>9.2f}"
                         f"{orc.get('cagr', float('nan')):>9.1%}{orc.get('maxdd', float('nan')):>9.1%}")
            lines.append(f"    capture ratio (SR/CAGR): {cmp.get('capture_ratio_sharpe', float('nan')):.2f}"
                         f" / {cmp.get('capture_ratio_cagr', float('nan')):.2f}"
                         f"   mean regret={cmp.get('mean_regret_sharpe', float('nan')):.2f} SR"
                         f"   optimal picks={cmp.get('n_folds_optimal', 0)}/{len(self.folds)}")

        lines.append(f"{'═' * 70}\n")
        return "\n".join(lines)

    def to_detail_dict(self) -> Dict[str, Any]:
        """Serialize full fold-level detail for persistence (P0)."""
        def _r(v, d=4):
            return round(v, d) if not np.isnan(v) else None

        return {
            "mode": self.mode,
            "n_folds": len(self.folds),
            "n_param_sets": self.n_param_sets,
            "mean_wfe": _r(self.mean_wfe),
            "dsr_aggregate": _r(self.dsr_aggregate),
            "synthetic_metrics": {k: _r(v) for k, v in self.synthetic_metrics.items()},
            "param_oos_stats": self.param_oos_stats,
            "selection_log": self.selection_log,
            "folds": [
                {
                    "fold_id": fr.fold.fold_id,
                    "is_start": str(fr.fold.is_start.date()),
                    "is_end": str(fr.fold.is_end.date()),
                    "oos_start": str(fr.fold.oos_start.date()),
                    "oos_end": str(fr.fold.oos_end.date()),
                    "selected": fr.is_best_name,
                    "method": fr.selection_method,
                    "is_sharpe": _r(fr.is_best_sharpe),
                    "mcps_score": _r(fr.mcps_score),
                    "dsr_pvalue": _r(fr.dsr_pvalue),
                    "oos_metrics": {k: _r(v) for k, v in fr.oos_metrics.items()},
                    "oos_regime": fr.oos_regime,
                    "wfe": _r(fr.wfe),
                    "all_oos_sharpes": {k: _r(v) for k, v in fr.all_oos_sharpes.items()},
                    "oos_macro_vec": {
                        k: round(v, 4) if v is not None else None
                        for k, v in fr.oos_macro_vec.items()
                    },
                }
                for fr in self.folds
            ],
        }

    def to_oracle_dict(self) -> Dict[str, Any]:
        """Serialize the oracle-ceiling / 3-layer comparison for P0 persistence."""
        def _r(v, d=4):
            try:
                return round(float(v), d) if v is not None and not np.isnan(float(v)) else None
            except (TypeError, ValueError):
                return v

        def _clean(obj):  # recursive nan→None so the comparison dict is valid JSON
            if isinstance(obj, dict):
                return {k: _clean(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_clean(v) for v in obj]
            if isinstance(obj, float):
                return None if np.isnan(obj) else round(obj, 4)
            return obj

        return {
            "mode": self.mode,
            "n_folds": len(self.folds),
            "n_param_sets": self.n_param_sets,
            "comparison": _clean(self.comparison),
            "static_best_name": self.static_best_name,
            "static_best_metrics": {k: _r(v) for k, v in self.static_best_metrics.items()},
            "oracle_metrics": {k: _r(v) for k, v in self.oracle_metrics.items()},
            "synthetic_metrics": {k: _r(v) for k, v in self.synthetic_metrics.items()},
            "oracle_selection_log": self.oracle_selection_log,
        }

    def param_oos_by_regime(self) -> Dict[str, Dict[str, Dict[str, float]]]:
        """Aggregate OOS Sharpe by (param_set, regime) across all folds."""
        from collections import defaultdict
        buckets: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for fr in self.folds:
            regime = fr.oos_regime
            for ps_name, oos_sr in fr.all_oos_sharpes.items():
                if not np.isnan(oos_sr):
                    buckets[ps_name][regime].append(oos_sr)
        result = {}
        for ps_name, regimes in buckets.items():
            result[ps_name] = {
                regime: {
                    "mean_oos_sharpe": round(float(np.mean(srs)), 4),
                    "n_folds": len(srs),
                }
                for regime, srs in regimes.items()
            }
        return result


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
    # BLdP(2014) Eq.7: 第二分位点分母是 N·e(乘 e)。旧代码 np.exp(-1)=除以 e:
    # N=2 时 ppf 参数为负 → NaN;N=3 为负值;N≥4 系统性低估 sr_0 22-98% →
    # DSR 通缩不足、闸门偏松(2026-07-26 修复;根目录 pairs 版 math.e 一直正确)
    z2 = scipy.stats.norm.ppf(1.0 - 1.0 / (max(n_trials, 2) * np.e))
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


def _oos_retrospective_select(
    today_vec: Dict[str, float],
    prior_folds: List["WFFoldResult"],
    features: List[str],
) -> tuple:
    """
    OOS fold retrospective matching — select param set by matching today's macro
    vector against the macro environment of prior completed OOS folds.

    For each prior fold we know:
      - oos_macro_vec: mean macro state DURING that OOS period
      - all_oos_sharpes: actual OOS Sharpe of every param set in that window

    We find prior OOS windows whose macro resembled our current conditions
    (IS-tail macro = best proxy for upcoming OOS), then use Gaussian-kernel-
    weighted average OOS Sharpe to rank param sets.

    This is superior to IS-based MCPS because it uses *actual OOS outcomes*,
    not IS simulations.

    Parameters
    ----------
    today_vec : dict — IS-tail macro vector (proxy for upcoming OOS conditions)
    prior_folds : list[WFFoldResult] — completed folds with oos_macro_vec populated
    features : list[str] — which macro features to use for distance

    Returns
    -------
    (best_name: str, best_score: float)  — empty string if insufficient data
    """
    import math

    # Only use folds that have valid oos_macro_vec and all_oos_sharpes
    valid: List[tuple] = []
    for pf in prior_folds:
        if not pf.oos_macro_vec or not pf.all_oos_sharpes:
            continue
        vec = np.array([pf.oos_macro_vec.get(f, np.nan) for f in features],
                       dtype=float)
        if np.any(np.isnan(vec)):
            continue
        valid.append((vec, pf.all_oos_sharpes))

    if not valid:
        return "", float("nan")

    today_arr = np.array([today_vec.get(f, np.nan) for f in features], dtype=float)
    if np.any(np.isnan(today_arr)):
        return "", float("nan")

    # Normalize features (z-score using prior fold vectors as reference)
    mat = np.stack([v for v, _ in valid], axis=0)  # (N_prior, n_features)
    col_mean = mat.mean(axis=0)
    col_std  = mat.std(axis=0)
    col_std  = np.where(col_std < 1e-8, 1.0, col_std)
    mat_z        = (mat - col_mean) / col_std
    today_z      = (today_arr - col_mean) / col_std

    # Gaussian kernel weights: σ = median distance
    diffs = mat_z - today_z          # (N_prior, n_features)
    dists = np.sqrt((diffs ** 2).sum(axis=1))   # (N_prior,)
    sigma = float(np.median(dists))
    sigma = max(sigma, 1e-6)
    weights = np.exp(-(dists ** 2) / (2.0 * sigma ** 2))  # (N_prior,)
    total_w = float(weights.sum())
    if total_w < 1e-12:
        return "", float("nan")
    weights /= total_w

    # Collect all candidate param names
    all_params: set = set()
    for _, oos_srs in valid:
        all_params.update(oos_srs.keys())

    # Weighted average OOS Sharpe per param
    param_scores: Dict[str, float] = {}
    for param in all_params:
        w_sum = 0.0
        ws_sum = 0.0
        for j, (_, oos_srs) in enumerate(valid):
            sr = oos_srs.get(param, np.nan)
            if not np.isnan(sr):
                w_sum  += weights[j] * sr
                ws_sum += weights[j]
        if ws_sum > 1e-12:
            param_scores[param] = w_sum / ws_sum

    if not param_scores:
        return "", float("nan")

    best_name  = max(param_scores, key=param_scores.get)
    best_score = param_scores[best_name]
    return best_name, float(best_score)


# ═══════════════════════════════════════════════════════════════════════════
#  WalkForwardAnalyzer
# ═══════════════════════════════════════════════════════════════════════════

class WalkForwardAnalyzer:
    """
    Walk-forward IS/OOS framework for 59 AISS param sets.

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
        signal_version: str = None,
        selection_method: str = "legacy",
    ):
        self.base_cfg = base_cfg
        self.prices = prices
        self.macro = macro
        self.is_years_min = is_years_min
        self.oos_months = oos_months
        self.step_days = step_days
        self.embargo_days = embargo_days
        self.mode = mode
        self.signal_version = signal_version  # None = use config default
        # Per-fold param-selection method (Stage 2 bake-off).  "legacy" reproduces
        # the original priority chain byte-for-byte; new methods (trailing_oos /
        # wfe_weighted / regime_ensemble) are causal (use ONLY prior folds) and
        # fall through to the legacy chain when they have insufficient evidence.
        self.selection_method = selection_method
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
                if self.signal_version:
                    cfg.setdefault("signals", {})["signal_version"] = self.signal_version
                engine = AISSBacktest(cfg)
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

    # Minimum completed folds before switching to OOS retrospective matching.
    # First _OOS_RETRO_MIN_FOLDS folds use IS stability → MCPS → IS Sharpe as warm-up.
    # 4 prior folds provide enough macro diversity to anchor the Gaussian kernel.
    _OOS_RETRO_MIN_FOLDS: int = 4

    def _evaluate_fold(
        self,
        fold: WFFold,
        eq_map: Dict[str, pd.Series],
        macro_df: pd.DataFrame,
        prior_folds: Optional[List["WFFoldResult"]] = None,
    ) -> "WFFoldResult":
        """
        Run IS selection + OOS evaluation for a single fold.

        Selection priority:
          1. OOS retrospective matching (if >= _OOS_RETRO_MIN_FOLDS prior folds)
             — uses actual OOS Sharpes of prior folds weighted by macro similarity
          2. IS stability filter (sub-period consistency check)
          3. MCPS on IS data (IS macro-conditioned Sharpe via SimilarityEngine)
          4. IS Sharpe fallback + DSR adjustment
          5. Fallback: first param set (IS < 60 days)

        prior_folds : completed folds so far (fold i uses folds 0..i-1).
                      Each prior fold must have oos_macro_vec and all_oos_sharpes
                      populated (done in this method).
        """
        features = self._similarity_features
        prior_folds = prior_folds or []

        # ── IS metrics for all param sets ─────────────────────────────────
        is_metrics: Dict[str, Dict[str, float]] = {}
        for name, eq in eq_map.items():
            eq_is = eq[(eq.index >= fold.is_start) & (eq.index <= fold.is_end)]
            if len(eq_is) < 60:
                continue
            is_metrics[name] = _compute_metrics_from_equity(eq_is)

        if not is_metrics:
            fallback_name = self._set_names[0]
            return self._make_fallback_fold(fold, fallback_name, eq_map)

        # ── IS macro vector (last 30 trading days of IS) ─────────────────
        # Used as "today_vec" — best no-leakage proxy for upcoming OOS macro.
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

        # ── OOS macro vector for THIS fold (stored for future folds) ─────
        # Mean macro state across the full OOS window — used as the
        # "fingerprint" of what actually happened during OOS.
        oos_macro_vec: Dict[str, float] = {}
        if not macro_df.empty and features:
            macro_oos = macro_df[(macro_df.index >= fold.oos_start) &
                                 (macro_df.index <= fold.oos_end)]
            if len(macro_oos) >= 10:
                oos_mean = macro_oos[features].mean()
                oos_macro_vec = {
                    f: float(oos_mean[f]) if f in oos_mean.index and not pd.isna(oos_mean[f])
                    else None
                    for f in features
                }

        # ── All param sets' OOS Sharpe (stored for future folds) ─────────
        # Slicing pre-computed equity curves is fast; no additional backtests.
        all_oos_sharpes: Dict[str, float] = {}
        for name, eq_full in eq_map.items():
            seg = eq_full[(eq_full.index >= fold.oos_start) &
                          (eq_full.index <= fold.oos_end)]
            if len(seg) >= 20:
                seg_norm = seg / seg.iloc[0]
                m = _compute_metrics_from_equity(seg_norm)
                sr = m.get("sharpe", float("nan"))
                if not np.isnan(sr):
                    all_oos_sharpes[name] = sr

        # ── Selection ─────────────────────────────────────────────────────
        # Priority: OOS retrospective → IS stability → MCPS → IS Sharpe → fallback
        n_trials = len(is_metrics)
        best_name: str = ""
        best_score: float = float("nan")
        dsr_p: float = 0.0
        selection_method = "is_sharpe"

        # 0. Stage 2 bake-off: a non-legacy method gets first attempt.  It is
        # causal (uses only prior folds' realized OOS) and returns "" when it has
        # too little evidence, falling through to the legacy chain below.  In
        # "legacy" mode this hook is skipped → behaviour is byte-identical.
        if self.selection_method != "legacy":
            nm_name, nm_score = self._select_param_new(
                fold, eq_map, macro_df, prior_folds, is_metrics, is_macro_vec, features)
            if nm_name:
                best_name = nm_name
                best_score = nm_score
                selection_method = self.selection_method

        # 1. OOS retrospective matching (requires enough completed prior folds)
        retro_candidates = [
            pf for pf in prior_folds
            if pf.oos_macro_vec and pf.all_oos_sharpes
        ]
        if not best_name and len(retro_candidates) >= self._OOS_RETRO_MIN_FOLDS and is_macro_vec:
            retro_name, retro_score = _oos_retrospective_select(
                today_vec=is_macro_vec,
                prior_folds=retro_candidates,
                features=features,
            )
            if retro_name and not np.isnan(retro_score):
                best_name = retro_name
                best_score = retro_score
                selection_method = "oos_retrospective"

        # 2. Stability-filtered IS Sharpe (when OOS retrospective not yet available)
        # Splits IS into n_splits sub-windows; score = mean_sub_sharpe - 0.5 * std.
        # Prevents overfit params (high IS SR, unstable sub-period) from dominating
        # warmup folds before oos_retrospective accumulates enough evidence.
        if not best_name:
            stab_name, stab_score = self._stable_is_select(is_metrics, eq_map, fold)
            if stab_name:
                best_name = stab_name
                best_score = stab_score
                selection_method = "is_stability"

        # 3. MCPS on IS data (macro-conditioned Sharpe via SimilarityEngine)
        # Uses autoencoder (23 macro features → 12-dim latent) or Euclidean
        # Gaussian-kernel to weight IS days by macro similarity to IS-tail.
        if not best_name and is_macro_vec and not macro_df.empty:
            macro_is = macro_df[(macro_df.index >= fold.is_start) &
                                (macro_df.index <= fold.is_end)]
            mcps_scores: Dict[str, float] = {}
            for name in is_metrics:
                if name not in eq_map:
                    continue
                eq_is = eq_map[name][(eq_map[name].index >= fold.is_start) &
                                     (eq_map[name].index <= fold.is_end)]
                if len(eq_is) < 60:
                    continue
                try:
                    sc = _macro_cond_sharpe_is(
                        equity_is=eq_is,
                        macro_is=macro_is,
                        today_vec=is_macro_vec,
                        features=features,
                    )
                    if not np.isnan(sc):
                        mcps_scores[name] = sc
                except Exception:
                    pass
            if mcps_scores:
                best_name = max(mcps_scores, key=mcps_scores.get)
                best_score = mcps_scores[best_name]
                selection_method = "mcps"

        # 4. IS Sharpe fallback
        if not best_name:
            all_sharpes = {n: m.get("sharpe", float("-inf"))
                          for n, m in is_metrics.items()
                          if not np.isnan(m.get("sharpe", float("nan")))}
            if not all_sharpes:
                best_name  = self._set_names[0]
                best_score = float("nan")
                dsr_p      = 0.0
            else:
                score_var = float(np.var(list(all_sharpes.values()))) if len(all_sharpes) > 1 else 0.01
                sr_0 = expected_max_sharpe(n_trials, score_var)
                best_name  = max(all_sharpes, key=all_sharpes.get)
                best_score = all_sharpes[best_name]
                best_is_m  = is_metrics.get(best_name, {})
                dsr_p = deflated_sharpe_ratio(
                    sr_obs=best_score, sr_0=sr_0,
                    T=int(best_is_m.get("n_days", 252)),
                    skew=best_is_m.get("skew", 0.0),
                    kurt=best_is_m.get("kurt", 3.0),
                )

        # ── OOS evaluation (selected param set only) ──────────────────────
        oos_eq = pd.Series(dtype=float)
        oos_metrics: Dict[str, float] = {}

        if best_name in eq_map:
            eq_full = eq_map[best_name]
            seg = eq_full[(eq_full.index >= fold.oos_start) &
                          (eq_full.index <= fold.oos_end)]
            if not seg.empty:
                oos_eq = seg / seg.iloc[0]
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
            all_oos_sharpes=all_oos_sharpes,
            oos_macro_vec=oos_macro_vec,
        )

    # ──────────────────────────────────────────────────────────────────────
    #  Stage 2 candidate selectors (causal — only prior folds' realized OOS)
    # ──────────────────────────────────────────────────────────────────────

    _TRAILING_K: int = 6      # trailing window of prior folds
    _MIN_PRIORS: int = 3      # min prior folds before a new method engages

    def _is_regime(self, fold: "WFFold", macro_df: pd.DataFrame) -> str:
        """IS-tail (~3m) dominant regime, mirroring the oos_regime VIX buckets."""
        if macro_df is None or macro_df.empty or "vix" not in macro_df.columns:
            return "unknown"
        vix_is = macro_df.loc[(macro_df.index >= fold.is_start) &
                              (macro_df.index <= fold.is_end), "vix"].dropna()
        if vix_is.empty:
            return "unknown"
        mv = float(vix_is.tail(63).mean())
        if mv > 30:
            return "risk_off"
        if mv > 20:
            return "transition"
        return "risk_on"

    def _select_param_new(self, fold, eq_map, macro_df, prior_folds,
                          is_metrics, is_macro_vec, features) -> tuple:
        """
        Candidate per-fold selectors that directly target the oracle regret using
        ONLY information available at decision time (prior folds' realized OOS).
        Returns (name, score) or ("", nan) → fall through to the legacy chain when
        evidence is insufficient (warmup folds).
        """
        method = self.selection_method
        priors = [pf for pf in prior_folds if pf.all_oos_sharpes]
        if len(priors) < self._MIN_PRIORS:
            return "", float("nan")
        recent = priors[-self._TRAILING_K:]
        cands = list(is_metrics.keys())

        def _trailing_mean(name: str) -> float:
            vals = [pf.all_oos_sharpes.get(name) for pf in recent
                    if pf.all_oos_sharpes.get(name) is not None
                    and not np.isnan(pf.all_oos_sharpes.get(name))]
            return float(np.mean(vals)) if vals else float("nan")

        def _argmax(scored: Dict[str, float]) -> tuple:
            scored = {n: v for n, v in scored.items()
                      if v is not None and not np.isnan(v)}
            if not scored:
                return "", float("nan")
            best = max(scored, key=scored.get)
            return best, scored[best]

        if method == "trailing_oos":
            return _argmax({n: _trailing_mean(n) for n in cands})

        if method == "wfe_weighted":
            scored: Dict[str, float] = {}
            for n in cands:
                tm = _trailing_mean(n)
                if np.isnan(tm):
                    continue
                wfes = []
                for pf in recent:
                    osr = pf.all_oos_sharpes.get(n)
                    isr = pf.is_metrics.get(n, {}).get("sharpe") if pf.is_metrics else None
                    if (osr is not None and isr is not None
                            and not np.isnan(osr) and not np.isnan(isr) and abs(isr) > 1e-6):
                        wfes.append(osr / isr)
                stab = 1.0 / (1.0 + float(np.var(wfes))) if len(wfes) >= 2 else 0.5
                scored[n] = tm * stab
            return _argmax(scored)

        if method == "regime_ensemble":
            is_reg = self._is_regime(fold, macro_df)
            n_in_regime = sum(1 for pf in priors if pf.oos_regime == is_reg)
            if is_reg != "unknown" and n_in_regime >= 2:
                scored = {}
                for n in cands:
                    vals = [pf.all_oos_sharpes.get(n) for pf in priors
                            if pf.oos_regime == is_reg
                            and pf.all_oos_sharpes.get(n) is not None
                            and not np.isnan(pf.all_oos_sharpes.get(n))]
                    if vals:
                        scored[n] = float(np.mean(vals))
                name, score = _argmax(scored)
                if name:
                    return name, score
            # regime bucket too thin → fall back to trailing_oos
            return _argmax({n: _trailing_mean(n) for n in cands})

        return "", float("nan")

    def _stable_is_select(
        self,
        is_metrics: Dict[str, Dict[str, float]],
        eq_map: Dict[str, pd.Series],
        fold: "WFFold",
        n_splits: int = 4,
        stability_weight: float = 0.5,
    ) -> tuple:
        """
        Select param set with best stability-penalized IS Sharpe.

        Splits IS window into n_splits sub-periods.  Each param set is scored:
            score = mean_sub_period_sharpe - stability_weight * std_sub_period_sharpe

        This prevents overfit strategies (high overall IS SR but wildly variable
        sub-period performance) from being selected during the warmup phase before
        oos_retrospective accumulates enough prior-fold evidence.

        Falls back to overall IS Sharpe when sub-window computation fails.
        Returns (best_name, score) or ("", nan).
        """
        is_dates = self.prices.loc[fold.is_start:fold.is_end].index
        n_days = len(is_dates)
        if n_days < n_splits * 20:
            return "", float("nan")

        # Build sub-window boundaries
        split_size = n_days // n_splits
        windows: List[tuple] = []
        for i in range(n_splits):
            w_start = is_dates[i * split_size]
            w_end = is_dates[(i + 1) * split_size - 1] if i < n_splits - 1 else is_dates[-1]
            windows.append((w_start, w_end))

        stability_scores: Dict[str, float] = {}
        for name, m in is_metrics.items():
            if name not in eq_map:
                continue
            eq_full = eq_map[name]
            sub_sharpes: List[float] = []
            for w_start, w_end in windows:
                seg = eq_full[(eq_full.index >= w_start) & (eq_full.index <= w_end)]
                if len(seg) < 15:
                    continue
                seg_norm = seg / seg.iloc[0]
                sub_m = _compute_metrics_from_equity(seg_norm)
                sr = sub_m.get("sharpe", float("nan"))
                if not np.isnan(sr):
                    sub_sharpes.append(sr)

            if len(sub_sharpes) >= 2:
                mean_sr = float(np.mean(sub_sharpes))
                std_sr = float(np.std(sub_sharpes))
                stability_scores[name] = mean_sr - stability_weight * std_sr
            else:
                # Fallback to overall IS Sharpe when too few sub-windows
                overall_sr = m.get("sharpe", float("-inf"))
                if not np.isnan(overall_sr):
                    stability_scores[name] = overall_sr

        if not stability_scores:
            return "", float("nan")

        best_name = max(stability_scores, key=stability_scores.get)
        return best_name, stability_scores[best_name]

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

        # ── Evaluate each fold (sequential: fold i sees folds 0..i-1) ─────
        fold_results: List[WFFoldResult] = []
        for i, fold in enumerate(folds):
            n_retro = sum(
                1 for pf in fold_results if pf.oos_macro_vec and pf.all_oos_sharpes
            )
            logger.info(
                f"  Fold {fold.fold_id + 1}/{len(folds)}: "
                f"IS=[{fold.is_start.date()}→{fold.is_end.date()}] "
                f"OOS=[{fold.oos_start.date()}→{fold.oos_end.date()}] "
                f"(retro_pool={n_retro})"
            )
            fr = self._evaluate_fold(fold, eq_map, macro_df, prior_folds=fold_results)
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
        # DSR uses variance of per-PARAM-SET IS Sharpes (not per-fold OOS Sharpes).
        # Per-fold OOS variance is huge (~1.0) due to different market conditions
        # across folds, making sr_0 unrealistically high and DSR always 0.
        # Per-param IS Sharpe variance is the correct input: it measures how
        # much the 59 strategies differ from each other (selection bias).
        n_sets = len(self.param_sets)
        all_is_sharpes = []
        for fr in fold_results:
            for name, m in fr.is_metrics.items():
                sr = m.get("sharpe", float("nan"))
                if not np.isnan(sr):
                    all_is_sharpes.append(sr)
        dsr_agg = 0.0
        if all_is_sharpes and not np.isnan(synthetic_metrics.get("sharpe", float("nan"))):
            var_s = float(np.var(all_is_sharpes)) if len(all_is_sharpes) > 1 else 0.01
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

        # ── Dynamic-selection analysis: oracle ceiling + static baseline ──
        # Reuses the already-computed eq_map (NO extra backtests).
        oracle_eq, oracle_metrics, oracle_log = self._compute_oracle(fold_results, eq_map)
        static_name, static_eq, static_metrics = self._compute_static_best(eq_map)

        def _layer(m: Dict[str, float]) -> Dict[str, float]:
            return {
                "sharpe": float(m.get("sharpe", float("nan"))),
                "cagr":   float(m.get("ann_ret", float("nan"))),
                "maxdd":  float(m.get("maxdd", float("nan"))),
                "calmar": float(m.get("calmar", float("nan"))),
            }
        orc_sr   = oracle_metrics.get("sharpe", float("nan"))
        orc_cagr = oracle_metrics.get("ann_ret", float("nan"))
        syn_sr   = synthetic_metrics.get("sharpe", float("nan"))
        syn_cagr = synthetic_metrics.get("ann_ret", float("nan"))
        cap_sr   = (syn_sr / orc_sr) if (not np.isnan(orc_sr) and orc_sr > 0) else float("nan")
        cap_cagr = (syn_cagr / orc_cagr) if (not np.isnan(orc_cagr) and orc_cagr > 0) else float("nan")
        regrets  = [d["regret"] for d in oracle_log if d.get("regret") is not None]
        comparison = {
            "static_best": _layer(static_metrics),
            "synthetic":   _layer(synthetic_metrics),
            "oracle":      _layer(oracle_metrics),
            "capture_ratio_sharpe": cap_sr,
            "capture_ratio_cagr":   cap_cagr,
            "mean_regret_sharpe":   float(np.mean(regrets)) if regrets else float("nan"),
            "n_folds_optimal":      sum(1 for d in oracle_log if d.get("optimal")),
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
            oracle_equity=oracle_eq,
            oracle_metrics=oracle_metrics,
            oracle_selection_log=oracle_log,
            static_best_equity=static_eq,
            static_best_metrics=static_metrics,
            static_best_name=static_name,
            comparison=comparison,
        )

    # ──────────────────────────────────────────────────────────────────────
    #  Stitch OOS segments into synthetic track record
    # ──────────────────────────────────────────────────────────────────────

    def _stitch_equity_segments(self, equity_segments: List[pd.Series]) -> pd.Series:
        """
        Stitch a list of per-fold OOS equity curves (in fold order) into one
        synthetic equity, assigning each date to the EARLIEST fold covering it
        (avoids double-counting under dense stepping).

        Shared by the realizable synthetic curve (selected param per fold) and
        the oracle ceiling curve (best-OOS param per fold) so both use the
        IDENTICAL date partition — guaranteeing an apples-to-apples comparison.
        """
        claimed_dates: set = set()
        segments: List[pd.Series] = []
        for eq in equity_segments:
            if eq is None or eq.empty:
                continue
            rets = eq.pct_change().dropna()
            new_rets = rets[~rets.index.isin(claimed_dates)]
            if not new_rets.empty:
                segments.append(new_rets)
                claimed_dates.update(new_rets.index)
        if not segments:
            return pd.Series(dtype=float)
        all_rets = pd.concat(segments).sort_index()
        all_rets = all_rets[~all_rets.index.duplicated(keep="first")]
        return (1 + all_rets).cumprod()

    def _stitch_oos(self, fold_results: List[WFFoldResult]) -> pd.Series:
        """Realizable synthetic OOS equity from the SELECTED param per fold."""
        if not fold_results:
            return pd.Series(dtype=float)
        return self._stitch_equity_segments([fr.oos_equity for fr in fold_results])

    # ──────────────────────────────────────────────────────────────────────
    #  Oracle ceiling + static baseline (dynamic-selection analysis)
    # ──────────────────────────────────────────────────────────────────────

    def _compute_oracle(self, fold_results: List[WFFoldResult], eq_map):
        """
        Oracle ceiling: per fold, pick argmax(all_oos_sharpes) (hindsight), slice
        that param's OOS segment from eq_map (same slice as the selected param at
        lines ~779-785), and stitch with the SAME partition as the realizable
        synthetic. This is the theoretical OOS ceiling any per-fold selector could
        reach over the same folds.  Returns (oracle_equity, oracle_metrics, log).
        """
        oracle_segments: List[pd.Series] = []
        oracle_log: List[Dict[str, Any]] = []
        for fr in fold_results:
            sel_name = fr.is_best_name
            sel_sr = fr.all_oos_sharpes.get(sel_name, float("nan")) if fr.all_oos_sharpes else float("nan")
            if fr.all_oos_sharpes:
                orc_name = max(fr.all_oos_sharpes, key=fr.all_oos_sharpes.get)
                orc_sr = fr.all_oos_sharpes[orc_name]
            else:
                orc_name, orc_sr = sel_name, float("nan")  # empty fold → keep selected
            seg = pd.Series(dtype=float)
            if orc_name in eq_map:
                eq_full = eq_map[orc_name]
                s = eq_full[(eq_full.index >= fr.fold.oos_start) &
                            (eq_full.index <= fr.fold.oos_end)]
                if not s.empty:
                    seg = s / s.iloc[0]
            oracle_segments.append(seg)
            regret = (orc_sr - sel_sr) if (not np.isnan(orc_sr) and not np.isnan(sel_sr)) else float("nan")
            oracle_log.append({
                "fold": fr.fold.fold_id,
                "oos_start": str(fr.fold.oos_start.date()),
                "oos_end": str(fr.fold.oos_end.date()),
                "oracle_param": orc_name,
                "oracle_oos_sharpe": round(orc_sr, 4) if not np.isnan(orc_sr) else None,
                "selected_param": sel_name,
                "selected_oos_sharpe": round(sel_sr, 4) if not np.isnan(sel_sr) else None,
                "regret": round(regret, 4) if not np.isnan(regret) else None,
                "optimal": bool(orc_name == sel_name),
            })
        oracle_eq = self._stitch_equity_segments(oracle_segments)
        oracle_metrics = _compute_metrics_from_equity(oracle_eq)
        return oracle_eq, oracle_metrics, oracle_log

    def _compute_static_best(self, eq_map):
        """
        Single full-period best param ("no dynamic selection" baseline).  This
        peeks at the whole period → IS/full-period OPTIMISTIC (not an OOS number);
        it represents what you'd get holding one fixed param.  Returns
        (name, equity, metrics).
        """
        best_name, best_sr = "", float("-inf")
        best_eq, best_m = pd.Series(dtype=float), {}
        for name, eq in eq_map.items():
            if eq is None or eq.empty or len(eq) < 60:
                continue
            eq_n = eq / eq.iloc[0]
            m = _compute_metrics_from_equity(eq_n)
            sr = m.get("sharpe", float("nan"))
            if not np.isnan(sr) and sr > best_sr:
                best_sr, best_name, best_eq, best_m = sr, name, eq_n, m
        return best_name, best_eq, best_m

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
        description="Walk-Forward IS/OOS analysis for AISS"
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
    parser.add_argument("--signal-version", default=None, choices=["v1", "v2"],
                        help="Override signal version (v1=4-factor, v2=7-factor)")
    parser.add_argument("--selection-method", default="legacy",
                        choices=["legacy", "trailing_oos", "wfe_weighted", "regime_ensemble"],
                        help="Per-fold param selection method (default: legacy)")
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
        signal_version=args.signal_version,
        selection_method=args.selection_method,
    )

    if args.mode == "both":
        results = run_dual_mode(base_cfg, prices, macro, **common_kwargs)
        for mode_name, wf_r in results.items():
            print(wf_r.summary())
            csv_path = out_dir / f"wf_{mode_name}_fold_summary.csv"
            wf_r.fold_summary_df.to_csv(csv_path, index=False)
            print(f"  Fold summary → {csv_path}")
        # WF Diagnostic Excel (use anchored result)
        # 沙盒守卫(2026-07-21): 显式 --output-dir 视为沙盒跑 → 跳过硬写
        # historical_runs/ 的 Excel(export_wf_diagnostic_excel 路径不可重定向)
        if args.output_dir:
            print("  [sandbox] --output-dir set: WF diagnostic Excel skipped")
        else:
            try:
                from semiconductor_strategy.portfolio_record import export_wf_diagnostic_excel
                if "anchored" in results:
                    export_wf_diagnostic_excel(
                        results["anchored"], mode="wf",
                        signal_version=args.signal_version or "v1")
            except Exception as _e:
                logger.warning(f"WF diagnostic Excel failed: {_e}")
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
        # WF Diagnostic Excel (沙盒守卫同上)
        if args.output_dir:
            print("  [sandbox] --output-dir set: WF diagnostic Excel skipped")
        else:
            try:
                from semiconductor_strategy.portfolio_record import export_wf_diagnostic_excel
                export_wf_diagnostic_excel(
                    wf_r, mode="wf",
                    signal_version=args.signal_version or "v1")
            except Exception as _e:
                logger.warning(f"WF diagnostic Excel failed: {_e}")
