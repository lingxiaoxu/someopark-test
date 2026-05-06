"""
MCPS — Macro-Conditioned Parameter Selection
====================================================================
通过高斯核相似度，从候选参数中选择当日最匹配的参数集。

核心算法（Gaussian-kernel-weighted Sharpe）：
  对每个历史交易日 t，计算其宏观状态与当前宏观状态的距离 d_t，
  权重 w_t = exp(-d_t² / 2σ²)，σ = median(所有 d_t)。
  加权 Sharpe = wmean / √wvar × √252
  → 历史上宏观环境越接近今天的时期，对 Sharpe 贡献越大。

通用接口，同时支持：
  - mrpt/mtfs 配对交易：select_param(..., score_field="dsr_pvalue")
  - 板块轮动 (SR)：      macro_cond_sharpe(equity, macro_df, today_vec, features)

特征集由 MacroStateStore.SIMILARITY_FEATURES 驱动（保持一致）。

用法：
  from MCPS import select_param, macro_cond_sharpe

  # mrpt/mtfs（原始接口，向后兼容）
  chosen = select_param(today_vector, candidates)

  # 板块轮动 — 计算单个参数集的宏观条件Sharpe
  score = macro_cond_sharpe(equity_is, macro_is_df, today_vec, features)

  # 板块轮动 — 从多个候选中选出最优
  chosen = select_param(today_vector, sr_candidates,
                        score_field="is_sharpe", quality_field="is_calmar")
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional

from MacroStateStore import SIMILARITY_FEATURES

log = logging.getLogger(__name__)

# 特征顺序与 MacroStateStore.SIMILARITY_FEATURES 保持同步
FEATURES: list[str] = SIMILARITY_FEATURES


def _to_vec(d: dict, features: list[str]) -> list[float] | None:
    """把 dict 转成固定顺序的数值向量；任何 feature 缺失/None/nan 则返回 None。"""
    vals = []
    for f in features:
        v = d.get(f)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        vals.append(float(v))
    return vals


def _dist2(a: list[float], b: list[float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _median_pairwise_sigma(vecs: list[list[float]]) -> float:
    """计算所有有效向量对之间欧氏距离的中位数，作为高斯核 σ。"""
    dists = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            dists.append(math.sqrt(_dist2(vecs[i], vecs[j])))
    if not dists:
        return 1.0
    dists.sort()
    mid = len(dists) // 2
    return max(dists[mid], 1e-6)


def gaussian_sim(v1: list[float], v2: list[float], sigma: float) -> float:
    return math.exp(-_dist2(v1, v2) / (2 * sigma ** 2))


def macro_cond_sharpe(
    equity,
    macro_df,
    today_vec: dict,
    features: list[str] | None = None,
    min_overlap: int = 60,
    normalize: bool = True,
    similarity_method: str | None = "autoencoder",
) -> float:
    """
    Gaussian-kernel-weighted Sharpe ratio — 宏观条件化夏普比率。

    对 equity 的每个历史交易日 t，根据其宏观状态与 today_vec 的距离
    赋予高斯核权重：
        d_t = ||macro_t - today_vec||₂
        σ   = median(d_t for all t)
        w_t = exp(-d_t² / 2σ²)

    加权 Sharpe：
        wmean = Σ(w_t × r_t) / Σw_t
        wvar  = Σ(w_t × (r_t - wmean)²) / Σw_t
        score = wmean / √wvar × √252

    适用于 SR 和 mrpt/mtfs 两套系统：
      - SR: equity = 某参数集的 IS 期 equity curve
      - mrpt/mtfs: equity = 某配对的 IS 期净值曲线

    Parameters
    ----------
    equity   : pd.Series — daily equity curve (DatetimeIndex, base-agnostic)
    macro_df : pd.DataFrame — daily macro state (same DatetimeIndex range)
    today_vec: dict — current macro state {feature: float}
    features : list[str] — which columns to use (default: SIMILARITY_FEATURES)
    min_overlap : int — minimum aligned (equity ∩ macro) days required
    normalize : bool — z-score normalize RAW features before distance calc (default True).
                Only normalizes features that are NOT already z-scored (i.e. name does
                not end with '_z'). This prevents features with large raw ranges
                (e.g. consumer_sent 50-100) from dominating z-scored features (±3).
                MacroStateStore and SIMILARITY_FEATURES are NOT modified.
    similarity_method : str or None — which SimilarityEngine method to use.
                "autoencoder" (default) = 23 macro features → 12-dim latent space.
                "euclidean" = 6 SIMILARITY_FEATURES, Gaussian kernel on raw distance.
                "ensemble" = average of euclidean + autoencoder.
                None = inline Euclidean (legacy fast path, no SimilarityEngine).

    Returns
    -------
    float — annualized macro-conditioned Sharpe (nan if insufficient data)
    """
    import numpy as np
    import pandas as pd

    # ── Delegate to SimilarityEngine if requested ─────────────────────
    if similarity_method is not None:
        from SimilarityEngine import SimilarityEngine
        engine = SimilarityEngine(method=similarity_method, normalize=normalize)
        weights, w_index = engine.compute_weights(macro_df, today_vec, features)
        if len(weights) == 0:
            return float("nan")

        rets = equity.pct_change().dropna()
        rets = rets.reindex(w_index).dropna()
        weights = weights[np.isin(w_index, rets.index)]
        if len(rets) < min_overlap:
            return float("nan")

        total_w = float(weights.sum())
        if total_w <= 0:
            return float("nan")
        rets_arr = rets.values
        wmean = float((weights @ rets_arr) / total_w)
        wvar = float((weights @ (rets_arr - wmean) ** 2) / total_w)
        if wvar <= 0:
            return float("nan")
        return float(wmean / math.sqrt(wvar) * math.sqrt(252))

    # ── Inline Euclidean (default, fastest path) ──────────────────────
    feats = features or FEATURES
    avail = [f for f in feats if f in macro_df.columns]
    if not avail:
        return float("nan")

    # Daily returns from equity curve
    rets = equity.pct_change().dropna()

    # Align: only days with both returns AND complete macro features
    sub = macro_df[avail].dropna(how="any")
    rets = rets.reindex(sub.index).dropna()
    sub = sub.reindex(rets.index)

    if len(rets) < min_overlap:
        return float("nan")

    # Build today's vector — must be complete
    today_v = [today_vec.get(f) for f in avail]
    if any(v is None or (isinstance(v, float) and (v != v)) for v in today_v):
        return float("nan")
    today_arr = np.array([float(v) for v in today_v])

    # Distances from today to each historical day
    macro_mat = sub.values                            # (T, n_features)

    if normalize:
        for i, f in enumerate(avail):
            if not f.endswith('_z'):
                col = macro_mat[:, i]
                mu = float(col.mean())
                sd = float(col.std())
                if sd > 0:
                    macro_mat[:, i] = (col - mu) / sd
                    today_arr[i] = (today_arr[i] - mu) / sd

    diffs = macro_mat - today_arr                     # broadcast
    dists = np.sqrt((diffs ** 2).sum(axis=1))         # (T,) Euclidean

    # Adaptive σ = median distance (robust to outliers)
    sigma = float(np.median(dists))
    sigma = max(sigma, 1e-3)

    # Gaussian kernel weights
    weights = np.exp(-(dists ** 2) / (2.0 * sigma ** 2))
    total_w = float(weights.sum())
    if total_w <= 0:
        return float("nan")

    # Weighted mean and variance of returns
    rets_arr = rets.values
    wmean = float((weights @ rets_arr) / total_w)
    wvar = float((weights @ (rets_arr - wmean) ** 2) / total_w)
    if wvar <= 0:
        return float("nan")

    # Annualized weighted Sharpe
    return float(wmean / math.sqrt(wvar) * math.sqrt(252))



def select_param(
    today_vector: dict,
    candidates: list[dict],
    features: list[str] | None = None,
    score_field: str = "dsr_pvalue",
    quality_field: str = "pair_sharpe",
) -> str:
    """
    从候选列表中选出综合得分最高的 param_set。

    通用接口 —— score_field / quality_field 控制打分来源：

      mrpt/mtfs（默认）:
        score_field   = "dsr_pvalue"    (0-1, DSR 显著性)
        quality_field = "pair_sharpe"   (IS 期 Sharpe)

      板块轮动 (SR):
        score_field   = "is_sharpe"     (IS 期 Sharpe)
        quality_field = "is_calmar"     (IS 期 Calmar)

    candidates 每项必须包含：
        {
            'param_set':       str,
            score_field:       float,
            quality_field:     float,
            'is_macro_vector': dict,   # IS 期 last-30d 均值向量
        }

    score = gaussian_sim(today, is_vector) × candidate[score_field]
    tie-break: (score desc, score_field desc, quality_field desc)

    当 today_vector 特征不全 / 候选数 < 2 / σ 崩塌时，
    直接返回 score_field 最高的候选（退化为无宏观条件的 top-1）。
    """
    feats = features or FEATURES

    if len(candidates) == 1:
        return candidates[0]['param_set']

    today_v = _to_vec(today_vector, feats)

    # 提取所有 IS 向量
    is_vecs = [_to_vec(c.get('is_macro_vector', {}), feats) for c in candidates]

    # 自适应 σ：用有效 IS 向量计算中位数对距离
    valid_vecs = [v for v in is_vecs if v is not None]
    sigma = _median_pairwise_sigma(valid_vecs) if len(valid_vecs) >= 2 else 1.0

    # σ 崩塌检测：所有候选 IS 向量来自同一窗口（两两距离=0），σ 退化为 1e-6
    # gaussian_sim 会产生极小的浮点差异主导排序 → 直接退化为 top-1
    if sigma < 1e-3 or today_v is None:
        chosen = max(candidates, key=lambda c: (float(c.get(score_field, 0.0)),
                                                float(c.get(quality_field, 0.0))))['param_set']
        log.info(f"[MCPS] 选中 {chosen}（{score_field}-only，σ崩塌/无宏观向量，共 {len(candidates)} 候选）")
        return chosen

    scored = []
    for c, iv in zip(candidates, is_vecs):
        sf_val = float(c.get(score_field, 0.5))
        qf_val = float(c.get(quality_field, 0.0))
        sim = gaussian_sim(today_v, iv, sigma) if iv is not None else 1.0
        score = sim * sf_val
        # tie-break: (score desc, score_field desc, quality_field desc) — 保证确定性
        scored.append((score, sf_val, qf_val, c['param_set']))
        log.debug(f"  {c['param_set']}: sim={sim:.4f} {score_field}={sf_val:.3f} score={score:.4f}")

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    chosen = scored[0][3]
    log.info(f"[MCPS] 选中 {chosen}（score={scored[0][0]:.4f}，共 {len(candidates)} 候选，σ={sigma:.3f}）")

    return chosen
