"""
smart_select.py — Daily intelligent parameter & version selection (Plan 05 §5)
==============================================================================
COPIED from qlib-main/sector_rotation/smart_select.py (read-only template,
811 lines) — three-layer daily decision engine, mechanics preserved:

  Layer 1: Macro positioning — locate today's regime vector vs historical
           clusters, detect anomalies.
  Layer 2: MCPS real-time scoring — macro-conditioned Sharpe of cached
           equity curves against today's macro vector.
  Layer 3: Version selection — regime stability + vol level + OOS history +
           macro novelty → V1 vs V2 preference.

Switch logic (verbatim): same-version switch = 3 consecutive best days + 5-day
cooldown; cross-version = 5 days + 10-day cooldown; hard caps 2 param / 1
version switch per month.

ADAPTATIONS (only these — every mechanism above is kept):
  1. No root-project imports. The template delegated to SimilarityEngine
     (autoencoder), MacroStateStore and MCPS. Here: macro data is an INJECTED
     DataFrame of crypto regime features (same convention as
     crypto_common.walk_forward), MCPS scoring uses the self-contained
     walk_forward.macro_cond_sharpe, and Layer-1 positioning runs in z-scored
     FEATURE space (KMeans centroids via sklearn) instead of autoencoder
     latent space — same output contract (nearest_cluster / cluster_distance /
     anomaly at 2× median distance).
  2. Features: vix → btc_rvol etc. — default REGIME_FEATURES below (the same
     columns crypto_common.regime consumes). Calibrate on recorded data.
  3. VIX thresholds in Layer 3 → btc_rvol brackets mirroring the regime copy
     (elevated 45 / crisis 60; trend ±8 rvol-points ≈ template's ±3 VIX).
  4. Cache dir → trading_signals/select_cache/ ; annualization 252 → 365.
  5. macro_weight_tilt: Gaussian-kernel similarity weights (same math as
     macro_cond_sharpe) over an injected per-perp price panel replaces the
     autoencoder/equity-cache tilt; ±5% cap and renormalisation verbatim.

Cache contract (written by the WF/batch step, read here — mirrors template):
  select_cache/batch_equity_cache.parquet      columns = param-set equity curves
  select_cache/top_candidates.json             {"top": [{"name","version"}...]}
  select_cache/param_oos_by_regime[_v1|_v2].json   walk_forward.WFResult.param_oos_by_regime()
  select_cache/param_oos_by_macro_cluster.json {param: {"cluster_N": {"mean_oos_sharpe"}}}
  select_cache/macro_latent_centroids.npy      z-space centroids (build_centroids)
  select_cache/multi_horizon_results.json      weekly_review composite scores
  select_cache/selected_param_set.json         dynamic state (save_state)
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common.walk_forward import macro_cond_sharpe

log = logging.getLogger(__name__)

TRADING_DAYS = 365          # crypto 24/7 (template: 252)

# Crypto regime feature set (Plan 00 §5; template: VIX/curve/HY/breadth)
REGIME_FEATURES = ["btc_rvol", "funding", "basis_dispersion", "btc_dominance"]

# btc_rvol brackets — template VIX 20/25/28 → crypto 45/52/60 ("calibrate on
# recorded data", mirrors crypto_common.regime's 30/45/60/90 raw brackets)
RVOL_ELEVATED = 45.0
RVOL_MID = 52.0
RVOL_CRISIS = 60.0
RVOL_TREND_BAND = 8.0       # template: ±3 VIX points


def _cache_dir() -> Path:
    return _config.SIGNALS_DIR / "select_cache"


# ═══════════════════════════════════════════════════════════════════════════
#  Data loading helpers (template verbatim, paths re-rooted)
# ═══════════════════════════════════════════════════════════════════════════

def _load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _load_equity_cache() -> Optional[pd.DataFrame]:
    p = _cache_dir() / "batch_equity_cache.parquet"
    if p.exists():
        return pd.read_parquet(p)
    return None


def _load_centroids() -> Optional[np.ndarray]:
    p = _cache_dir() / "macro_latent_centroids.npy"
    if p.exists():
        return np.load(str(p))
    return None


def _load_selected_state() -> dict:
    p = _cache_dir() / "selected_param_set.json"
    if p.exists():
        return json.loads(p.read_text())
    return {}


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 1: Macro positioning (ADAPTED: z-scored feature space, no autoencoder)
# ═══════════════════════════════════════════════════════════════════════════

def _zstats(macro_df: pd.DataFrame, feats: List[str]) -> tuple[np.ndarray, np.ndarray]:
    mat = macro_df[feats].dropna(how="any").to_numpy(dtype=float)
    mean = mat.mean(axis=0)
    std = mat.std(axis=0)
    std = np.where(std < 1e-8, 1.0, std)
    return mean, std


def build_centroids(macro_df: pd.DataFrame, *, n_clusters: int = 5,
                    features: List[str] | None = None) -> Optional[np.ndarray]:
    """Build + cache regime-space centroids (the WF/batch step calls this —
    template's P0 produced autoencoder centroids offline the same way)."""
    feats = [f for f in (features or REGIME_FEATURES) if f in macro_df.columns]
    sub = macro_df[feats].dropna(how="any")
    if len(sub) < 60 or len(feats) < 2:
        return None
    from sklearn.cluster import KMeans
    mean, std = _zstats(macro_df, feats)
    z = (sub.to_numpy(dtype=float) - mean) / std
    km = KMeans(n_clusters=min(n_clusters, max(2, len(sub) // 30)),
                n_init=10, random_state=0).fit(z)
    _cache_dir().mkdir(parents=True, exist_ok=True)
    np.save(str(_cache_dir() / "macro_latent_centroids.npy"), km.cluster_centers_)
    (_cache_dir() / "centroid_features.json").write_text(json.dumps(feats))
    return km.cluster_centers_


def macro_positioning(signal_date: date, macro_df: pd.DataFrame,
                      features: List[str] | None = None) -> Dict[str, Any]:
    """Locate today's regime vector vs cached centroids (template contract)."""
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
        feats = _load_json(_cache_dir() / "centroid_features.json") or \
            [f for f in (features or REGIME_FEATURES) if f in macro_df.columns]
        feats = [f for f in feats if f in macro_df.columns]
        sub = macro_df[feats].dropna(how="any")
        if len(sub) < 60 or len(feats) < 2:
            return result
        sd = pd.Timestamp(signal_date, tz="UTC")
        upto = sub[sub.index <= sd]
        if upto.empty:
            return result
        today_raw = upto.iloc[-1].to_numpy(dtype=float)
        mean, std = _zstats(macro_df, feats)
        today_z = (today_raw - mean) / std

        dists = np.linalg.norm(centroids - today_z, axis=1)
        nearest = int(np.argmin(dists))
        min_dist = float(dists[nearest])
        # ADAPTED-fix: the template compared min_dist > 2×median(dists to
        # centroids) — impossible by definition (min ≤ median), so its anomaly
        # branch was dead code. The stated intent ("today is far from all
        # known clusters") is implemented against the HISTORICAL baseline:
        # median nearest-centroid distance across the macro history.
        hist_z = (sub.to_numpy(dtype=float) - mean) / std
        hist_nearest = np.linalg.norm(
            hist_z[:, None, :] - centroids[None, :, :], axis=2).min(axis=1)
        median_dist = float(np.median(hist_nearest))
        is_anomaly = min_dist > 2.0 * median_dist

        result.update({
            "available": True,
            "today_latent": today_z.tolist(),
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
#  Layer 2: MCPS real-time scoring (via walk_forward.macro_cond_sharpe)
# ═══════════════════════════════════════════════════════════════════════════

def mcps_realtime_scores(signal_date: date, macro_df: pd.DataFrame,
                         top_candidates: List[dict],
                         features: List[str] | None = None) -> Dict[str, float]:
    """{param_name: mcps_score} from cached equity curves (template contract)."""
    scores: Dict[str, float] = {}
    eq_cache = _load_equity_cache()
    if eq_cache is None:
        log.warning("[SMART SELECT] No equity cache — skipping MCPS scoring")
        return scores
    feats = [f for f in (features or REGIME_FEATURES) if f in macro_df.columns]
    if not feats:
        return scores
    sd = pd.Timestamp(signal_date, tz="UTC" if macro_df.index.tz else None)
    upto = macro_df[macro_df.index <= sd]
    if upto.empty:
        return scores
    today_vec = {f: float(upto[f].dropna().iloc[-1])
                 for f in feats if not upto[f].dropna().empty}
    if len(today_vec) < len(feats):
        return scores

    for cand in top_candidates:
        name = cand.get("name", cand.get("param_set", ""))
        if name not in eq_cache.columns:
            continue
        eq = eq_cache[name].dropna()
        if len(eq) < 60:
            continue
        try:
            sc = macro_cond_sharpe(equity=eq, macro_df=macro_df,
                                   today_vec=today_vec, features=feats)
            if not np.isnan(sc):
                scores[name] = round(float(sc), 4)
        except Exception:
            pass
    return scores


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 3: Version selector (vix → btc_rvol; structure verbatim)
# ═══════════════════════════════════════════════════════════════════════════

def version_selector(macro_df: pd.DataFrame, signal_date: date,
                     macro_pos: Dict[str, Any]) -> Tuple[Optional[str], float]:
    """V1 vs V2 preference; >0.6 → V1, <0.4 → V2, else let MCPS decide."""
    v1_oos = _load_json(_cache_dir() / "param_oos_by_regime_v1.json")
    v2_oos = _load_json(_cache_dir() / "param_oos_by_regime_v2.json")

    if not v2_oos:
        v1_oos = _load_json(_cache_dir() / "param_oos_by_regime.json")
        if not v1_oos:
            return "v1", 0.7
        return "v1", 0.8

    # ── Dimension 1: Regime stability (rvol transitions; template VIX 25/28)
    regime_score = 0.5
    if not macro_df.empty and "btc_rvol" in macro_df.columns:
        rv_recent = macro_df["btc_rvol"].dropna().tail(20)
        if len(rv_recent) >= 10:
            above_crisis = (rv_recent > RVOL_CRISIS).sum()
            transitions = ((rv_recent > RVOL_MID) != (rv_recent.shift() > RVOL_MID)).sum()
            if transitions <= 1 and above_crisis == 0:
                regime_score = 1.0
            elif transitions >= 4 or above_crisis > 5:
                regime_score = 0.0

    # ── Dimension 2: rvol level and trend (template VIX 20/28, trend ±3)
    vol_score = 0.5
    if not macro_df.empty and "btc_rvol" in macro_df.columns:
        rv = macro_df["btc_rvol"].dropna()
        if len(rv) >= 20:
            level = float(rv.iloc[-1])
            m5 = float(rv.iloc[-5:].mean())
            m20 = float(rv.iloc[-20:].mean())
            trend = m5 - m20
            if level < RVOL_ELEVATED and trend < 0:
                vol_score = 1.0
            elif level > RVOL_CRISIS or trend > RVOL_TREND_BAND:
                vol_score = 0.0

    # ── Dimension 3: OOS history in current regime (verbatim)
    oos_score = 0.5
    current_regime = _detect_simple_regime(macro_df)
    v1_best = _best_regime_sharpe(v1_oos, current_regime)
    v2_best = _best_regime_sharpe(v2_oos, current_regime)
    if v1_best is not None and v2_best is not None:
        if v1_best > v2_best + 0.2:
            oos_score = 1.0
        elif v2_best > v1_best + 0.2:
            oos_score = 0.0

    # ── Dimension 4: Macro novelty (verbatim)
    novelty_score = 0.5
    if macro_pos.get("available"):
        cd = macro_pos.get("cluster_distance", 0)
        md = macro_pos.get("median_cluster_distance", 1)
        if md > 0:
            ratio = cd / md
            if ratio < 0.8:
                novelty_score = 1.0
            elif ratio > 1.5:
                novelty_score = 0.0

    v1_confidence = (0.30 * regime_score + 0.25 * vol_score +
                     0.30 * oos_score + 0.15 * novelty_score)
    if v1_confidence > 0.6:
        return "v1", round(v1_confidence, 3)
    elif v1_confidence < 0.4:
        return "v2", round(1.0 - v1_confidence, 3)
    return None, round(v1_confidence, 3)


def _detect_simple_regime(macro_df: pd.DataFrame) -> str:
    """Quick regime from btc_rvol level (template: VIX 20/30)."""
    if macro_df.empty or "btc_rvol" not in macro_df.columns:
        return "risk_on"
    rv = macro_df["btc_rvol"].dropna()
    if rv.empty:
        return "risk_on"
    mean_rv = float(rv.tail(5).mean())
    if mean_rv > RVOL_CRISIS:
        return "risk_off"
    elif mean_rv > RVOL_ELEVATED:
        return "transition"
    return "risk_on"


def _best_regime_sharpe(oos_data: dict, regime: str) -> Optional[float]:
    best = None
    for stats in oos_data.values():
        if isinstance(stats, dict) and regime in stats:
            sr = stats[regime].get("mean_oos_sharpe")
            if sr is not None and (best is None or sr > best):
                best = sr
    return best


# ═══════════════════════════════════════════════════════════════════════════
#  Composite scoring & switch decision (template verbatim; 252 → 365)
# ═══════════════════════════════════════════════════════════════════════════

def _load_multi_horizon() -> dict:
    p = _cache_dir() / "multi_horizon_results.json"
    if p.exists():
        return _load_json(p)
    return {}


def composite_score(mcps_scores: Dict[str, float], cluster_oos: dict,
                    nearest_cluster: Optional[int],
                    eq_cache: Optional[pd.DataFrame],
                    signal_date: date) -> Dict[str, float]:
    """MCPS 35% + cluster OOS 20% + multi-horizon 25% + recent-60d 20%
    (fallback without multi-horizon: 50/30/20) — template verbatim."""
    if not mcps_scores:
        return {}

    def _z(vals: Dict[str, float]) -> Dict[str, float]:
        if len(vals) < 2:
            return {k: 0.5 for k in vals}
        arr = np.array(list(vals.values()))
        std = arr.std()
        if std < 1e-8:
            return {k: 0.5 for k in vals}
        mean = arr.mean()
        return {k: float((v - mean) / std) for k, v in vals.items()}

    mcps_z = _z(mcps_scores)

    cluster_scores: Dict[str, float] = {}
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

    recent_scores = {}
    if eq_cache is not None:
        sd = pd.Timestamp(signal_date)
        if eq_cache.index.tz is not None and sd.tz is None:
            sd = sd.tz_localize("UTC")
        for name in mcps_scores:
            if name in eq_cache.columns:
                eq = eq_cache[name].dropna()
                tail = eq[eq.index <= sd].tail(60)
                if len(tail) >= 30:
                    rets = tail.pct_change().dropna()
                    if len(rets) > 5 and rets.std() > 0:
                        recent_scores[name] = float(
                            rets.mean() / rets.std() * np.sqrt(TRADING_DAYS))
    for name in mcps_scores:
        recent_scores.setdefault(name, 0.0)
    recent_z = _z(recent_scores)

    result = {}
    if has_mh:
        for name in mcps_scores:
            result[name] = round(0.35 * mcps_z.get(name, 0) +
                                 0.20 * cluster_z.get(name, 0) +
                                 0.25 * mh_z.get(name, 0) +
                                 0.20 * recent_z.get(name, 0), 4)
    else:
        for name in mcps_scores:
            result[name] = round(0.50 * mcps_z.get(name, 0) +
                                 0.30 * cluster_z.get(name, 0) +
                                 0.20 * recent_z.get(name, 0), 4)
    return result


def should_switch(current_param: str, current_version: str, best_param: str,
                  best_version: str, state: dict,
                  signal_date: date) -> Tuple[bool, str]:
    """Debounced switch decision — template verbatim."""
    if best_param == current_param and best_version == current_version:
        return False, ""

    history = state.get("switch_history", [])
    health = state.get("health", {})
    days_as_best = health.get("_consecutive_best_days", 0)
    days_since_switch = health.get("days_since_switch", 999)

    month_switches = sum(1 for h in history
                         if h.get("date", "")[:7] == str(signal_date)[:7])
    month_version_switches = sum(
        1 for h in history
        if h.get("date", "")[:7] == str(signal_date)[:7]
        and h.get("from_version") != h.get("to_version"))

    is_version_change = best_version != current_version
    if month_switches >= 2:
        return False, "monthly_switch_limit"
    if is_version_change and month_version_switches >= 1:
        return False, "monthly_version_switch_limit"

    if is_version_change:
        if days_as_best >= 5 and days_since_switch >= 10:
            return True, "version_switch_mcps_drift"
    else:
        if days_as_best >= 3 and days_since_switch >= 5:
            return True, "param_switch_mcps_drift"
    return False, "debounce_not_met"


# ═══════════════════════════════════════════════════════════════════════════
#  Macro-conditioned weight tilt (ADAPTED: kernel weights + injected prices)
# ═══════════════════════════════════════════════════════════════════════════

def macro_weight_tilt(target_weights: pd.Series, macro_df: pd.DataFrame,
                      signal_date: date, prices: pd.DataFrame | None = None,
                      max_tilt: float = 0.05,
                      features: List[str] | None = None) -> pd.Series:
    """±max_tilt weight tilt from perp returns on macro-similar days.

    Same intent/caps/renormalisation as the template; similarity weights come
    from the Gaussian kernel in z-space (macro_cond_sharpe's math) instead of
    the autoencoder engine, and per-perp PRICES are injected explicitly
    (template read sector curves out of the equity cache).
    """
    if prices is None or prices.empty:
        return target_weights
    try:
        feats = [f for f in (features or REGIME_FEATURES) if f in macro_df.columns]
        sub = macro_df[feats].dropna(how="any")
        if len(sub) < 60 or not feats:
            return target_weights
        sd = pd.Timestamp(signal_date, tz="UTC" if sub.index.tz else None)
        upto = sub[sub.index <= sd]
        if upto.empty:
            return target_weights
        today = upto.iloc[-1].to_numpy(dtype=float)

        mat = sub.to_numpy(dtype=float)
        mean, std = mat.mean(axis=0), np.where(mat.std(axis=0) < 1e-8, 1.0,
                                               mat.std(axis=0))
        z = (mat - mean) / std
        tz = (today - mean) / std
        dists = np.sqrt(((z - tz) ** 2).sum(axis=1))
        sigma = max(float(np.median(dists)), 1e-6)
        kernel = pd.Series(np.exp(-(dists ** 2) / (2 * sigma ** 2)), index=sub.index)

        weighted_rets = {}
        rets = prices.pct_change()
        for ticker in target_weights.index:
            if ticker in rets.columns:
                aligned = rets[ticker].reindex(sub.index).dropna()
                w = kernel.reindex(aligned.index).dropna()
                if len(aligned) >= 60 and w.sum() > 0:
                    weighted_rets[ticker] = float(
                        np.average(aligned.loc[w.index], weights=w.values))
        if not weighted_rets:
            return target_weights

        tilt = pd.Series(weighted_rets)
        tilt_z = (tilt - tilt.mean()) / (tilt.std() + 1e-8)
        tilt_factor = tilt_z.clip(-1, 1) * max_tilt

        adjusted = target_weights.copy()
        for ticker in adjusted.index:
            if ticker in tilt_factor.index:
                adjusted[ticker] *= (1.0 + tilt_factor[ticker])
        total = adjusted.sum()
        if total > 0:
            adjusted = adjusted / total
        return adjusted
    except Exception as e:
        log.debug(f"[SMART SELECT] Macro weight tilt failed: {e}")
        return target_weights


# ═══════════════════════════════════════════════════════════════════════════
#  Main entry point (template verbatim, injected macro_df)
# ═══════════════════════════════════════════════════════════════════════════

def smart_param_select(signal_date: date, macro_df: pd.DataFrame,
                       current_state: Optional[dict] = None) -> Dict[str, Any]:
    """Daily selection — same flow/result contract as the template."""
    state = current_state or _load_selected_state()
    current_param = state.get("param_set", "")
    current_version = state.get("signal_version", "v1")

    result = {
        "param_set": current_param,
        "signal_version": current_version,
        "smart_select_available": False,
        "switched": False,
    }

    top_cands_data = _load_json(_cache_dir() / "top_candidates.json")
    top_cands = top_cands_data.get("top", [])
    if not top_cands:
        log.info("[SMART SELECT] No cached candidates — keeping current param set")
        return result

    macro_pos = macro_positioning(signal_date, macro_df)

    mcps_scores = mcps_realtime_scores(signal_date, macro_df, top_cands)
    if not mcps_scores:
        log.info("[SMART SELECT] MCPS scoring unavailable — keeping current param set")
        result["macro_positioning"] = macro_pos
        return result

    ver_pref, v1_conf = version_selector(macro_df, signal_date, macro_pos)

    cluster_oos = _load_json(_cache_dir() / "param_oos_by_macro_cluster.json")
    eq_cache = _load_equity_cache()
    comp_scores = composite_score(mcps_scores, cluster_oos,
                                  macro_pos.get("nearest_cluster"),
                                  eq_cache, signal_date)
    if not comp_scores:
        result["macro_positioning"] = macro_pos
        return result

    if ver_pref and v1_conf > 0.65:
        ver_candidates = {n: s for n, s in comp_scores.items()
                          if _get_candidate_version(n, top_cands) == ver_pref}
        if ver_candidates:
            best_param = max(ver_candidates, key=ver_candidates.get)
            best_version = ver_pref
        else:
            best_param = max(comp_scores, key=comp_scores.get)
            best_version = _get_candidate_version(best_param, top_cands) or current_version
    else:
        best_param = max(comp_scores, key=comp_scores.get)
        best_version = _get_candidate_version(best_param, top_cands) or current_version

    health = state.get("health", {})
    if best_param == health.get("_prev_best_param"):
        health["_consecutive_best_days"] = health.get("_consecutive_best_days", 0) + 1
    else:
        health["_consecutive_best_days"] = 1
    health["_prev_best_param"] = best_param
    health["days_since_switch"] = health.get("days_since_switch", 0) + 1
    state["health"] = health

    do_switch, switch_reason = should_switch(current_param, current_version,
                                             best_param, best_version,
                                             state, signal_date)
    if do_switch:
        result["param_set"] = best_param
        result["signal_version"] = best_version
        result["switched"] = True
        result["switch_reason"] = switch_reason
        switch_history = state.get("switch_history", [])
        switch_history.append({
            "date": str(signal_date),
            "from_param": current_param, "to_param": best_param,
            "from_version": current_version, "to_version": best_version,
            "reason": switch_reason,
            "best_composite": comp_scores.get(best_param),
            "prev_composite": comp_scores.get(current_param),
        })
        result["switch_history"] = switch_history
        health["days_since_switch"] = 0
        health["_consecutive_best_days"] = 0
    else:
        result["switch_history"] = state.get("switch_history", [])

    result.update({
        "smart_select_available": True,
        "composite_scores": comp_scores,
        "mcps_scores": mcps_scores,
        "best_candidate": best_param,
        "best_version": best_version,
        "current_rank": _rank_of(current_param, comp_scores),
        "version_selector": {"recommended": ver_pref, "v1_confidence": v1_conf},
        "macro_positioning": macro_pos,
        "health": {
            "days_since_switch": health.get("days_since_switch", 0),
            "current_rank": _rank_of(result["param_set"], comp_scores),
            "anomaly_detected": macro_pos.get("anomaly", False),
            "_consecutive_best_days": health.get("_consecutive_best_days", 0),
            "_prev_best_param": health.get("_prev_best_param"),
        },
    })
    return result


def _get_candidate_version(name: str, top_cands: list) -> Optional[str]:
    for c in top_cands:
        if c.get("name", c.get("param_set")) == name:
            return c.get("version", "v1")
    return None


def _rank_of(name: str, scores: Dict[str, float]) -> int:
    if not scores or name not in scores:
        return 0
    sorted_names = sorted(scores, key=scores.get, reverse=True)
    try:
        return sorted_names.index(name) + 1
    except ValueError:
        return 0


def save_state(state: dict, result: dict) -> dict:
    """Merge result into state, persist selected_param_set.json (template)."""
    updated = {**state}
    updated["param_set"] = result["param_set"]
    updated["signal_version"] = result["signal_version"]
    updated["last_updated"] = str(result.get("signal_date", date.today()))
    if "health" in result:
        updated["health"] = result["health"]
    if "switch_history" in result:
        updated["switch_history"] = result["switch_history"]
    updated["mcps_score"] = result.get("mcps_scores", {}).get(result["param_set"])
    updated["composite_score"] = result.get("composite_scores", {}).get(result["param_set"])
    if "top_candidates" not in updated:
        updated["top_candidates"] = []
    if result.get("composite_scores"):
        updated["top_candidates"] = [
            {"name": n,
             "version": _get_candidate_version(n, updated.get("top_candidates", [])) or "v1",
             "composite": round(s, 4),
             "mcps_score": result.get("mcps_scores", {}).get(n)}
            for n, s in sorted(result["composite_scores"].items(),
                               key=lambda x: x[1], reverse=True)[:10]
        ]
    if result.get("version_selector"):
        updated["version_selector"] = result["version_selector"]

    _cache_dir().mkdir(parents=True, exist_ok=True)
    (_cache_dir() / "selected_param_set.json").write_text(
        json.dumps(updated, indent=2, default=str))
    return updated


# ═══════════════════════════════════════════════════════════════════════════
#  CLI — daily entry (pipeline.sh `select`)
# ═══════════════════════════════════════════════════════════════════════════

def _build_regime_frame() -> pd.DataFrame:
    """Assemble the crypto regime feature frame from recorded stores."""
    from crypto_trading.crypto_common.loader import (load_funding,
                                                     load_index_composite,
                                                     load_perp_candles)
    out = {}
    try:
        idx = load_index_composite("BTC")
        daily = idx.vw_close.resample("1D").last().dropna()
        rets = daily.pct_change().dropna()
        out["btc_rvol"] = rets.rolling(30).std() * np.sqrt(TRADING_DAYS) * 100
    except Exception as e:
        log.warning(f"btc_rvol unavailable: {e}")
    try:
        f = load_funding("KXBTCPERP")
        out["funding"] = f.funding_rate.resample("1D").sum()
    except Exception as e:
        log.warning(f"funding unavailable: {e}")
    try:
        candles = load_perp_candles("KXBTCPERP", "1d")
        mid = (candles.bid_close + candles.ask_close) / 2
        idx_d = load_index_composite("BTC").vw_close.resample("1D").last()
        basis = ((mid / 1e-4) - idx_d) / idx_d * 1e4
        out["basis_dispersion"] = basis.abs().rolling(7, min_periods=3).std()
    except Exception as e:
        log.warning(f"basis_dispersion unavailable: {e}")
    try:
        dom_path = _config.PRICE_DATA / "regime" / "btc_dominance.csv"
        if dom_path.exists():
            dom = pd.read_csv(dom_path, parse_dates=["date"])
            s = dom.set_index("date")["btc_dominance_pct"]
            s.index = pd.DatetimeIndex(s.index).tz_localize("UTC")
            out["btc_dominance"] = s
    except Exception as e:
        log.warning(f"btc_dominance unavailable: {e}")
    return pd.DataFrame(out).sort_index()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, help="signal date (default: today UTC)")
    ap.add_argument("--build-centroids", action="store_true",
                    help="(re)build regime-space centroids from recorded data")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sd = date.fromisoformat(args.date) if args.date else date.today()
    macro = _build_regime_frame()
    if args.build_centroids:
        c = build_centroids(macro)
        print(f"centroids: {'built ' + str(c.shape) if c is not None else 'insufficient data'}")
    result = smart_param_select(sd, macro)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("switch_history",)}, indent=2, default=str))
    if result.get("smart_select_available"):
        save_state(_load_selected_state(), {**result, "signal_date": sd})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
