"""
MCPS — Macro-Conditioned Parameter Selection
通过高斯核相似度，从 Walk-Forward Top-K 候选参数中选择当日最匹配的参数集。

特征集由 MacroStateStore.SIMILARITY_FEATURES 驱动（保持一致）。

用法：
  from MCPS import select_param
  chosen = select_param(today_vector, candidates)
"""
from __future__ import annotations

import logging
import math

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


def select_param(
    today_vector: dict,
    candidates: list[dict],
    features: list[str] | None = None,
) -> str:
    """
    从候选列表中选出综合得分最高的 param_set。

    candidates 每项格式：
        {
            'param_set':      str,
            'pair_sharpe':    float,
            'dsr_pvalue':     float,
            'is_macro_vector': dict,   # IS 期 last-30d 均值向量
        }

    score = gaussian_sim(today, is_vector) × dsr_pvalue

    当 today_vector 特征不全 / 候选数 < 2 时，直接返回 dsr_pvalue 最高的候选。
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
    # gaussian_sim 会产生极小的浮点差异主导排序 → 直接退化为 pair_sharpe/dsr top-1
    if sigma < 1e-3 or today_v is None:
        chosen = max(candidates, key=lambda c: (float(c.get('pair_sharpe', 0.0)),
                                                float(c.get('dsr_pvalue', 0.0))))['param_set']
        log.info(f"[MCPS] 选中 {chosen}（DSR-only，σ崩塌/无宏观向量，共 {len(candidates)} 候选）")
        return chosen

    scored = []
    for c, iv in zip(candidates, is_vecs):
        dsr = float(c.get('dsr_pvalue', 0.5))
        sharpe = float(c.get('pair_sharpe', 0.0))
        sim = gaussian_sim(today_v, iv, sigma) if iv is not None else 1.0
        score = sim * dsr
        # tie-break: (score desc, dsr desc, pair_sharpe desc) — 保证确定性
        scored.append((score, dsr, sharpe, c['param_set']))
        log.debug(f"  {c['param_set']}: sim={sim:.4f} dsr={dsr:.3f} score={score:.4f}")

    scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    chosen = scored[0][3]
    log.info(f"[MCPS] 选中 {chosen}（score={scored[0][0]:.4f}，共 {len(candidates)} 候选，σ={sigma:.3f}）")

    return chosen
