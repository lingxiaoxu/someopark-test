"""
smart_select.py — Daily intelligent parameter & version selection (P2/P3/P5)
===========================================================================

Runs every day in the DailySignal pipeline. Three-layer decision engine:

  Layer 1 (P3): Autoencoder macro positioning — encode today's macro to
                latent space, find nearest cluster, detect anomalies.

  Layer 2 (P2): MCPS real-time scoring — score top candidates using
                cached equity curves + today's macro vector.

  Layer 3 (P5): Version selection — combine regime stability, VIX,
                OOS history, and macro novelty to choose V1 vs V2.

Switch logic:
  - Same-version param switch: 3 consecutive days + 10% margin + 5-day cooldown
  - Cross-version switch:      5 consecutive days + 15% margin + 10-day cooldown
  - Hard limit: max 2 param switches / month, max 1 version switch / month

Requires P0 cached data in backtest_results/:
  batch_equity_cache.parquet, param_oos_by_regime.json,
  param_oos_by_macro_cluster.json, macro_latent_centroids.npy,
  top_candidates.json
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).parent.resolve()
_PROJECT_DIR = _THIS_DIR.parent.parent.resolve()
_CACHE_DIR = _THIS_DIR / "backtest_results"


# ═══════════════════════════════════════════════════════════════════════════
#  Data loading helpers
# ═══════════════════════════════════════════════════════════════════════════

def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _load_equity_cache() -> Optional[pd.DataFrame]:
    p = _CACHE_DIR / "batch_equity_cache.parquet"
    if p.exists():
        return pd.read_parquet(p)
    return None


def _load_centroids() -> Optional[np.ndarray]:
    p = _CACHE_DIR / "macro_latent_centroids.npy"
    if p.exists():
        return np.load(str(p))
    return None


def _load_selected_state() -> dict:
    """Load current selected_param_set.json (dynamic state file)."""
    for p in (_THIS_DIR / "selected_param_set.json",
              _CACHE_DIR / "selected_param_set.json"):
        if p.exists():
            return json.loads(p.read_text())
    return {}


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 1 (P3): Autoencoder macro positioning
# ═══════════════════════════════════════════════════════════════════════════

def macro_positioning(
    signal_date: date,
    macro_df: pd.DataFrame,
) -> Dict[str, Any]:
    """
    Encode today's macro state to autoencoder latent space.

    Returns dict with:
      today_latent, nearest_cluster, cluster_distance, anomaly info
    """
    result = {
        "available": False,
        "today_latent": None,
        "nearest_cluster": None,
        "cluster_distance": None,
        "anomaly": False,
        "anomaly_action": None,
    }

    centroids = _load_centroids()
    if centroids is None:
        return result

    try:
        import sys
        if str(_PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(_PROJECT_DIR))
        from SimilarityEngine import SimilarityEngine, AUTOENCODER_FEATURES
        from MacroStateStore import MacroStateStore

        store = MacroStateStore()
        today_vec = store.get(str(signal_date), features=AUTOENCODER_FEATURES)
        if not today_vec or all(v is None for v in today_vec.values()):
            return result

        ae_avail = [f for f in AUTOENCODER_FEATURES if f in macro_df.columns]
        ae_sub = macro_df[ae_avail].dropna(how="any")
        if len(ae_sub) < 60 or len(ae_avail) < 6:
            return result

        engine = SimilarityEngine(method="autoencoder")
        # Trigger training by computing weights (lazy training on first call)
        _today_dummy = {f: float(ae_sub[f].iloc[-1]) for f in ae_avail}
        engine.compute_weights(ae_sub, _today_dummy, ae_avail)

        # Encode today's macro vector
        vec_arr = np.array([today_vec.get(f, 0.0) or 0.0 for f in ae_avail],
                           dtype=np.float32).reshape(1, -1)
        method = engine._methods[0]
        today_latent = method._encode(vec_arr).flatten()

        # Find nearest cluster
        dists = np.linalg.norm(centroids - today_latent, axis=1)
        nearest = int(np.argmin(dists))
        min_dist = float(dists[nearest])

        # Anomaly detection: if today is far from all clusters
        median_dist = float(np.median(dists))
        is_anomaly = min_dist > 2.0 * median_dist

        result.update({
            "available": True,
            "today_latent": today_latent.tolist(),
            "nearest_cluster": nearest,
            "cluster_distance": round(min_dist, 4),
            "median_cluster_distance": round(median_dist, 4),
            "anomaly": is_anomaly,
            "anomaly_action": "auto_conservative" if is_anomaly else None,
        })
    except Exception as e:
        log.warning(f"[SMART SELECT] Macro positioning failed: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 2 (P2): MCPS real-time scoring
# ═══════════════════════════════════════════════════════════════════════════

def mcps_realtime_scores(
    signal_date: date,
    macro_df: pd.DataFrame,
    top_candidates: List[dict],
) -> Dict[str, float]:
    """
    Compute MCPS scores for top candidates using cached equity curves.

    Returns {param_name: mcps_score}.
    """
    scores = {}
    eq_cache = _load_equity_cache()
    if eq_cache is None:
        log.warning("[SMART SELECT] No equity cache — skipping MCPS scoring")
        return scores

    try:
        import sys
        if str(_PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(_PROJECT_DIR))
        from MCPS import macro_cond_sharpe
        from MacroStateStore import MacroStateStore, SIMILARITY_FEATURES

        store = MacroStateStore()
        today_vec = store.get(str(signal_date))
        if not today_vec or all(v is None for v in today_vec.values()):
            return scores

        for cand in top_candidates:
            name = cand.get("name", cand.get("param_set", ""))
            if name not in eq_cache.columns:
                continue
            eq = eq_cache[name].dropna()
            if len(eq) < 60:
                continue
            try:
                sc = macro_cond_sharpe(
                    equity=eq,
                    macro_df=macro_df,
                    today_vec=today_vec,
                    features=list(SIMILARITY_FEATURES),
                    similarity_method="autoencoder",
                )
                if not np.isnan(sc):
                    scores[name] = round(float(sc), 4)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[SMART SELECT] MCPS scoring failed: {e}")

    return scores


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 3 (P5): Version selector
# ═══════════════════════════════════════════════════════════════════════════

def version_selector(
    macro_df: pd.DataFrame,
    signal_date: date,
    macro_pos: Dict[str, Any],
) -> Tuple[Optional[str], float]:
    """
    Multi-dimensional V1 vs V2 version preference.

    Returns (preferred_version, v1_confidence).
      v1_confidence > 0.6 → prefer V1
      v1_confidence < 0.4 → prefer V2
      0.4-0.6 → let MCPS decide naturally
    """
    v1_oos = _load_json(_CACHE_DIR / "param_oos_by_regime_v1.json")
    v2_oos = _load_json(_CACHE_DIR / "param_oos_by_regime_v2.json")

    # If no V2 data yet, default to V1
    if not v2_oos:
        v1_oos = _load_json(_CACHE_DIR / "param_oos_by_regime.json")
        if not v1_oos:
            return "v1", 0.7
        # No V2 data → strong V1 preference
        return "v1", 0.8

    # ── Dimension 1: Regime stability (V1 likes stable, V2 likes volatile)
    regime_score = 0.5
    if not macro_df.empty and "vix" in macro_df.columns:
        vix_recent = macro_df["vix"].dropna().tail(20)
        if len(vix_recent) >= 10:
            # Simple regime proxy: count days VIX crossed 20/28 thresholds
            above_28 = (vix_recent > 28).sum()
            regime_transitions = ((vix_recent > 25) != (vix_recent.shift() > 25)).sum()
            if regime_transitions <= 1 and above_28 == 0:
                regime_score = 1.0  # very stable → V1
            elif regime_transitions >= 4 or above_28 > 5:
                regime_score = 0.0  # volatile → V2
            else:
                regime_score = 0.5

    # ── Dimension 2: VIX level and trend
    vix_score = 0.5
    if not macro_df.empty and "vix" in macro_df.columns:
        vix = macro_df["vix"].dropna()
        if len(vix) >= 20:
            vix_level = float(vix.iloc[-1])
            vix_5d = float(vix.iloc[-5:].mean())
            vix_20d = float(vix.iloc[-20:].mean())
            vix_trend = vix_5d - vix_20d
            if vix_level < 20 and vix_trend < 0:
                vix_score = 1.0  # low & falling → V1
            elif vix_level > 28 or vix_trend > 3:
                vix_score = 0.0  # high or rising → V2
            else:
                vix_score = 0.5

    # ── Dimension 3: OOS history (V1 best vs V2 best in current regime)
    oos_score = 0.5
    current_regime = _detect_simple_regime(macro_df)
    v1_best_oos = _best_regime_sharpe(v1_oos, current_regime)
    v2_best_oos = _best_regime_sharpe(v2_oos, current_regime)
    if v1_best_oos is not None and v2_best_oos is not None:
        if v1_best_oos > v2_best_oos + 0.2:
            oos_score = 1.0
        elif v2_best_oos > v1_best_oos + 0.2:
            oos_score = 0.0
        else:
            oos_score = 0.5

    # ── Dimension 4: Macro novelty (unknown → V2 for diversification)
    novelty_score = 0.5
    if macro_pos.get("available"):
        cd = macro_pos.get("cluster_distance", 0)
        md = macro_pos.get("median_cluster_distance", 1)
        if md > 0:
            ratio = cd / md
            if ratio < 0.8:
                novelty_score = 1.0  # well inside known cluster → V1
            elif ratio > 1.5:
                novelty_score = 0.0  # far from known → V2
            else:
                novelty_score = 0.5

    # ── Dimension 5: Recovery / vol-crush (I-2, 2026-07-21)
    # 崩后修复期识别: VIX 从近期峰值显著回落 = vol crush → 半月节奏(V2)的
    # 再入场价值上升;长期深度平静(非修复)仍偏 V1。中性 0.5 不影响判定。
    recovery_score = 0.5
    if not macro_df.empty and "vix" in macro_df.columns:
        _vix_r = macro_df["vix"].dropna()
        if len(_vix_r) >= 25:
            # 无状态平滑(S2 回放发现逐日峰值滑窗致条件闪烁/27次穿越):
            # 对最近 5 个交易日各自评估 crush 条件,≥3 日成立才触发——
            # 函数保持纯函数(仅依赖 macro_df),防抖不引入持久状态
            _crush_days = 0
            for _k in range(5):
                _sub = _vix_r.iloc[: len(_vix_r) - _k]
                _now_k = float(_sub.iloc[-1])
                _peak_k = float(_sub.tail(20).max())
                if _peak_k > 26 and _now_k < 0.80 * _peak_k:
                    _crush_days += 1
            vix_now_r = float(_vix_r.iloc[-1])
            vix_peak_20d = float(_vix_r.tail(20).max())
            if _crush_days >= 3:
                recovery_score = 0.0     # vol crush(稳定确认) → 修复期 → 偏 V2
            elif vix_now_r < 18 and vix_peak_20d < 22:
                recovery_score = 1.0     # 长期平静(非修复) → 偏 V1

    # ── Combine (I-2 重分配: regime 25%, VIX 20%, OOS 30%, novelty 10%, recovery 15%)
    v1_confidence = (
        0.25 * regime_score +
        0.20 * vix_score +
        0.30 * oos_score +
        0.10 * novelty_score +
        0.15 * recovery_score
    )

    log.info(
        f"[version_selector] regime={regime_score:.2f} vix={vix_score:.2f} "
        f"oos={oos_score:.2f} novelty={novelty_score:.2f} recovery={recovery_score:.2f} "
        f"→ v1_confidence={v1_confidence:.3f}"
    )

    if v1_confidence > 0.6:
        return "v1", round(v1_confidence, 3)
    elif v1_confidence < 0.4:
        return "v2", round(1.0 - v1_confidence, 3)
    else:
        return None, round(v1_confidence, 3)


def _detect_simple_regime(macro_df: pd.DataFrame) -> str:
    """Quick regime detection from VIX level."""
    if macro_df.empty or "vix" not in macro_df.columns:
        return "risk_on"
    vix = macro_df["vix"].dropna()
    if vix.empty:
        return "risk_on"
    mean_vix = float(vix.tail(5).mean())
    if mean_vix > 30:
        return "risk_off"
    elif mean_vix > 20:
        return "transition"
    return "risk_on"


def _best_regime_sharpe(oos_data: dict, regime: str) -> Optional[float]:
    """Find best mean_oos_sharpe across all params for given regime."""
    best = None
    for stats in oos_data.values():
        if isinstance(stats, dict) and regime in stats:
            sr = stats[regime].get("mean_oos_sharpe")
            if sr is not None and (best is None or sr > best):
                best = sr
    return best


# ═══════════════════════════════════════════════════════════════════════════
#  Composite scoring & switch decision
# ═══════════════════════════════════════════════════════════════════════════

def _load_multi_horizon() -> dict:
    """Load weekly multi-horizon results (P4) for daily composite scoring."""
    p = _CACHE_DIR / "multi_horizon_results.json"
    if p.exists():
        return _load_json(p)
    return {}


def composite_score(
    mcps_scores: Dict[str, float],
    cluster_oos: dict,
    nearest_cluster: Optional[int],
    eq_cache: Optional[pd.DataFrame],
    signal_date: date,
) -> Dict[str, float]:
    """
    Combine:
      MCPS realtime      (35%)
      Cluster OOS        (20%)
      Multi-horizon P4   (25%)  ← from weekly_review cached results
      Recent 60d Sharpe  (20%)

    If multi-horizon data unavailable, falls back to:
      MCPS (50%) + Cluster OOS (30%) + Recent 60d (20%)
    """
    if not mcps_scores:
        return {}

    # Normalize helper
    def _z(vals: Dict[str, float]) -> Dict[str, float]:
        if len(vals) < 2:
            return {k: 0.5 for k in vals}
        arr = np.array(list(vals.values()))
        std = arr.std()
        if std < 1e-8:
            return {k: 0.5 for k in vals}
        mean = arr.mean()
        return {k: float((v - mean) / std) for k, v in vals.items()}

    # Component 1: MCPS
    mcps_z = _z(mcps_scores)

    # Component 2: Cluster OOS
    cluster_scores = {}
    if nearest_cluster is not None and cluster_oos:
        cl_key = f"cluster_{nearest_cluster}"
        for name in mcps_scores:
            entry = cluster_oos.get(name, {}).get(cl_key, {})
            sr = entry.get("mean_oos_sharpe")
            if sr is not None:
                cluster_scores[name] = sr
    for name in mcps_scores:
        cluster_scores.setdefault(name, 0.0)
    cluster_z = _z(cluster_scores)

    # Component 3: Multi-horizon weighted score (P4)
    mh_data = _load_multi_horizon()
    mh_composite = mh_data.get("composite_scores", {})
    mh_scores = {}
    for name in mcps_scores:
        if name in mh_composite and mh_composite[name] is not None:
            mh_scores[name] = mh_composite[name]
    for name in mcps_scores:
        mh_scores.setdefault(name, 0.0)
    has_mh = any(v != 0.0 for v in mh_scores.values())
    mh_z = _z(mh_scores) if has_mh else {}

    # Component 4: Recent 60-day Sharpe
    recent_scores = {}
    if eq_cache is not None:
        sd = pd.Timestamp(signal_date)
        for name in mcps_scores:
            if name in eq_cache.columns:
                eq = eq_cache[name].dropna()
                tail = eq[eq.index < sd].tail(60)   # PIT(2026-07-28): 回放态剔 signal-date 当日行;live 语义=昨日完整行,两态一致
                if len(tail) >= 30:
                    rets = tail.pct_change().dropna()
                    if len(rets) > 5 and rets.std() > 0:
                        recent_scores[name] = float(rets.mean() / rets.std() * np.sqrt(252))
    for name in mcps_scores:
        recent_scores.setdefault(name, 0.0)
    recent_z = _z(recent_scores)

    # Weighted composite — use multi-horizon if available
    result = {}
    if has_mh:
        # Full 4-component scoring (plan target)
        for name in mcps_scores:
            result[name] = round(
                0.35 * mcps_z.get(name, 0) +
                0.20 * cluster_z.get(name, 0) +
                0.25 * mh_z.get(name, 0) +
                0.20 * recent_z.get(name, 0),
                4,
            )
    else:
        # Fallback: no multi-horizon data yet
        for name in mcps_scores:
            result[name] = round(
                0.50 * mcps_z.get(name, 0) +
                0.30 * cluster_z.get(name, 0) +
                0.20 * recent_z.get(name, 0),
                4,
            )

    return result


def should_switch(
    current_param: str,
    current_version: str,
    best_param: str,
    best_version: str,
    state: dict,
    signal_date: date,
) -> Tuple[bool, str]:
    """
    Debounced switch decision.

    Returns (should_switch_bool, reason_str).
    """
    if best_param == current_param and best_version == current_version:
        return False, ""

    history = state.get("switch_history", [])
    health = state.get("health", {})

    # Count consecutive days best_param has been #1
    days_as_best = health.get("_consecutive_best_days", 0)

    # Days since last switch
    days_since_switch = health.get("days_since_switch", 999)

    # Monthly switch limits
    month_switches = sum(
        1 for h in history
        if h.get("date", "")[:7] == str(signal_date)[:7]
    )
    month_version_switches = sum(
        1 for h in history
        if h.get("date", "")[:7] == str(signal_date)[:7]
        and h.get("from_version") != h.get("to_version")
    )

    is_version_change = best_version != current_version

    # Hard limits
    if month_switches >= 2:
        return False, "monthly_switch_limit"
    if is_version_change and month_version_switches >= 1:
        return False, "monthly_version_switch_limit"

    # Debounce thresholds
    if is_version_change:
        if days_as_best >= 5 and days_since_switch >= 10:
            return True, "version_switch_mcps_drift"
    else:
        if days_as_best >= 3 and days_since_switch >= 5:
            return True, "param_switch_mcps_drift"

    return False, "debounce_not_met"


# ═══════════════════════════════════════════════════════════════════════════
#  Macro-conditioned weight adjustment (P3)
# ═══════════════════════════════════════════════════════════════════════════

def macro_weight_tilt(
    target_weights: pd.Series,
    macro_df: pd.DataFrame,
    signal_date: date,
    max_tilt: float = 0.05,
) -> pd.Series:
    """
    Use autoencoder latent-space similarity to tilt sector weights.
    Adjusts ±max_tilt (default 5%) based on how similar sectors performed
    on macro-similar days.

    This is ON TOP of regime-based weight adjustments (V1 multipliers / V2 matrices).
    """
    try:
        import sys
        if str(_PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(_PROJECT_DIR))
        from SimilarityEngine import SimilarityEngine, AUTOENCODER_FEATURES
        from MacroStateStore import MacroStateStore

        store = MacroStateStore()
        today_vec = store.get(str(signal_date), features=AUTOENCODER_FEATURES)
        if not today_vec or all(v is None for v in today_vec.values()):
            return target_weights

        ae_avail = [f for f in AUTOENCODER_FEATURES if f in macro_df.columns]
        ae_sub = macro_df[ae_avail].dropna(how="any")
        if len(ae_sub) < 60:
            return target_weights

        engine = SimilarityEngine(method="autoencoder")
        weights, w_index = engine.compute_weights(ae_sub, today_vec, ae_avail)

        # Load sector prices for tilt calculation
        from semiconductor_strategy.data.loader import load_prices
        sector_tickers = list(target_weights.index)
        # Use macro_df index as proxy for available dates
        # Compute weighted mean returns for each sector
        # (We need prices but they're not passed here — use the equity cache sectors)
        eq_cache = _load_equity_cache()
        if eq_cache is None:
            return target_weights

        weighted_rets = {}
        for ticker in sector_tickers:
            if ticker in eq_cache.columns:
                eq = eq_cache[ticker].dropna()
                rets = eq.pct_change().dropna()
                aligned = rets.reindex(w_index).dropna()
                w_aligned = weights[:len(aligned)]
                if len(aligned) >= 60 and len(w_aligned) == len(aligned):
                    weighted_rets[ticker] = float(np.average(aligned.values, weights=w_aligned))

        if not weighted_rets:
            return target_weights

        tilt = pd.Series(weighted_rets)
        tilt_z = (tilt - tilt.mean()) / (tilt.std() + 1e-8)
        tilt_factor = tilt_z.clip(-1, 1) * max_tilt

        adjusted = target_weights.copy()
        for ticker in adjusted.index:
            if ticker in tilt_factor.index:
                adjusted[ticker] *= (1.0 + tilt_factor[ticker])

        # Re-normalize
        total = adjusted.sum()
        if total > 0:
            adjusted = adjusted / total

        return adjusted

    except Exception as e:
        log.debug(f"[SMART SELECT] Macro weight tilt failed: {e}")
        return target_weights


# ═══════════════════════════════════════════════════════════════════════════
#  Main entry point — called from DailySignal
# ═══════════════════════════════════════════════════════════════════════════

def smart_param_select(
    signal_date: date,
    macro_df: pd.DataFrame,
    current_state: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Daily intelligent parameter & version selection.

    Parameters
    ----------
    signal_date : today's signal date
    macro_df    : macro DataFrame (from MacroStateStore or FRED fallback)
    current_state : current selected_param_set.json contents (or None)

    Returns
    -------
    dict with:
      param_set, signal_version, composite_scores, mcps_scores,
      version_selector, macro_positioning, switch_decision, ...
    """
    state = current_state or _load_selected_state()
    current_param = state.get("param_set", "")
    current_version = state.get("signal_version", "v1")

    result = {
        "param_set": current_param,
        "signal_version": current_version,
        "smart_select_available": False,
        "switched": False,
    }

    # Load top candidates from last batch run
    top_cands_data = _load_json(_CACHE_DIR / "top_candidates.json")
    top_cands = top_cands_data.get("top", [])
    if not top_cands:
        log.info("[SMART SELECT] No cached candidates — keeping current param set")
        return result

    # ── Layer 1: Autoencoder macro positioning ──
    macro_pos = macro_positioning(signal_date, macro_df)

    # ── Layer 2: MCPS real-time scoring ──
    mcps_scores = mcps_realtime_scores(signal_date, macro_df, top_cands)
    if not mcps_scores:
        log.info("[SMART SELECT] MCPS scoring unavailable — keeping current param set")
        result["macro_positioning"] = macro_pos
        return result

    # ── Layer 3: Version selection ──
    ver_pref, v1_conf = version_selector(macro_df, signal_date, macro_pos)

    # ── Composite scoring ──
    cluster_oos = _load_json(_CACHE_DIR / "param_oos_by_macro_cluster.json")
    eq_cache = _load_equity_cache()
    comp_scores = composite_score(
        mcps_scores=mcps_scores,
        cluster_oos=cluster_oos,
        nearest_cluster=macro_pos.get("nearest_cluster"),
        eq_cache=eq_cache,
        signal_date=signal_date,
    )

    if not comp_scores:
        result["macro_positioning"] = macro_pos
        return result

    # Select best (respecting version preference)
    if ver_pref and v1_conf > 0.65:
        # Strong version preference — filter to that version's candidates
        ver_candidates = {n: s for n, s in comp_scores.items()
                         if _get_candidate_version(n, top_cands) == ver_pref}
        if ver_candidates:
            best_param = max(ver_candidates, key=ver_candidates.get)
            best_version = ver_pref
        else:
            best_param = max(comp_scores, key=comp_scores.get)
            best_version = _get_candidate_version(best_param, top_cands) or current_version
    else:
        # Let MCPS decide naturally
        best_param = max(comp_scores, key=comp_scores.get)
        best_version = _get_candidate_version(best_param, top_cands) or current_version

    # ── Switch decision (debounced) ──
    # Track consecutive best days
    health = state.get("health", {})
    if best_param == health.get("_prev_best_param"):
        health["_consecutive_best_days"] = health.get("_consecutive_best_days", 0) + 1
    else:
        health["_consecutive_best_days"] = 1
    health["_prev_best_param"] = best_param
    health["days_since_switch"] = health.get("days_since_switch", 0) + 1

    do_switch, switch_reason = should_switch(
        current_param, current_version,
        best_param, best_version,
        state, signal_date,
    )

    if do_switch:
        result["param_set"] = best_param
        result["signal_version"] = best_version
        result["switched"] = True
        result["switch_reason"] = switch_reason

        # Update switch history
        switch_history = state.get("switch_history", [])
        switch_history.append({
            "date": str(signal_date),
            "from_param": current_param,
            "to_param": best_param,
            "from_version": current_version,
            "to_version": best_version,
            "reason": switch_reason,
            "best_composite": comp_scores.get(best_param),
            "prev_composite": comp_scores.get(current_param),
        })
        result["switch_history"] = switch_history
        health["days_since_switch"] = 0
        health["_consecutive_best_days"] = 0
    else:
        result["switch_history"] = state.get("switch_history", [])

    # Build full result
    result.update({
        "smart_select_available": True,
        "composite_scores": comp_scores,
        "mcps_scores": mcps_scores,
        "best_candidate": best_param,
        "best_version": best_version,
        "current_rank": _rank_of(current_param, comp_scores),
        "version_selector": {
            "recommended": ver_pref,
            "v1_confidence": v1_conf,
        },
        "macro_positioning": macro_pos,
        "health": {
            "days_since_switch": health.get("days_since_switch", 0),
            "current_rank": _rank_of(
                result["param_set"], comp_scores),
            "anomaly_detected": macro_pos.get("anomaly", False),
            "_consecutive_best_days": health.get("_consecutive_best_days", 0),
            "_prev_best_param": health.get("_prev_best_param"),
        },
    })

    return result


def _get_candidate_version(name: str, top_cands: list) -> Optional[str]:
    """Look up version for a candidate from top_candidates list."""
    for c in top_cands:
        if c.get("name", c.get("param_set")) == name:
            return c.get("version", "v1")
    return None


def _rank_of(name: str, scores: Dict[str, float]) -> int:
    """Get 1-based rank of name in scores (descending)."""
    if not scores or name not in scores:
        return 0
    sorted_names = sorted(scores, key=scores.get, reverse=True)
    try:
        return sorted_names.index(name) + 1
    except ValueError:
        return 0


def save_state(state: dict, result: dict) -> dict:
    """
    Merge smart_select result into state and write to disk.
    Called after smart_param_select() to persist the dynamic state file.
    """
    updated = {**state}
    updated["param_set"] = result["param_set"]
    updated["signal_version"] = result["signal_version"]
    updated["last_updated"] = str(result.get("signal_date", date.today()))

    # Merge health
    if "health" in result:
        updated["health"] = result["health"]

    # Update switch history
    if "switch_history" in result:
        updated["switch_history"] = result["switch_history"]

    # Add smart-select metadata
    updated["mcps_score"] = result.get("mcps_scores", {}).get(result["param_set"])
    updated["composite_score"] = result.get("composite_scores", {}).get(result["param_set"])

    if "top_candidates" not in updated:
        updated["top_candidates"] = []
    # Update top_candidates with current scores
    if result.get("composite_scores"):
        updated["top_candidates"] = [
            {
                "name": n,
                "version": _get_candidate_version(n, updated.get("top_candidates", [])) or "v1",
                "composite": round(s, 4),
                "mcps_score": result.get("mcps_scores", {}).get(n),
            }
            for n, s in sorted(result["composite_scores"].items(),
                               key=lambda x: x[1], reverse=True)[:10]
        ]

    if result.get("version_selector"):
        updated["version_selector"] = result["version_selector"]

    # Write to both locations
    import json as _json
    for p in (_THIS_DIR / "selected_param_set.json",
              _CACHE_DIR / "selected_param_set.json"):
        try:
            p.write_text(_json.dumps(updated, indent=2, default=str))
        except Exception:
            pass

    return updated
