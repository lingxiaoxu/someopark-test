"""blend3 路由规则 — 单一事实源(E10 议题③② 修复,2026-08-15)。

shadow_blend(影子)与 service.ops.refresh(正式 serve,8/17 拍板后启用)
共用本模块,路由规则只在这里改。

三层语义(用户批准 2026-08-10 选项 B):
  RNN 层  = 持仓票 ∪ 高流动票(RNN 覆盖内流动性 ≥ 中位数,即 top 50%)
  lgbm 层 = 其余 lgbm 覆盖票(守尾部)
  ma5     = 兜底

③② 修复:流动性门从纯 ADV̂(预测值)改为 **max(ADV̂, 近 N 日实测 ADV 中位)**。
病根:被严重低估的票(XHG 型,预测 $1.8k / 实际 $376M)按预测值会掉进弱层,
而弱层预测不准又让它继续被低估 —— 自我固化。实测 ADV 是已实现事实,
max() 保证"实际上高流动"的票一定进强层;PIT 安全(只用 ≤ pred_date 的日子)。
"""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger("VolumePrediction.blend_routing")

MEASURED_ADV_DAYS = 5      # 近 N 日实测窗(中位数,抗单日脉冲)


def recent_measured_adv(pred_date: str, n_days: int = MEASURED_ADV_DAYS,
                        svc=None) -> pd.Series:
    """近 n_days 个 raw 交易日(≤ pred_date,PIT)的实测 $ADV:
    逐票 **max(窗内中位数, 最近一日)**。
    中位数抗单日数据毛刺;最近一日保证 regime 切换(XHG 型: 四天 ~$3k、
    第五天 $376M)**次日即刻**被看见 —— 只用中位数要等 3 天才过半,门修了
    等于没修。数据不可用 → 空 Series(调用方降级为纯 ADV̂,大声不静默)。"""
    try:
        if svc is None:
            from VolumePrediction.service import VolumeService
            svc = VolumeService()
        days = [d for d in svc._raw_dates() if d <= pred_date][-n_days:]
        frames = [svc._load_day(d).set_index("ticker")["dollar_volume"]
                  for d in days]
        frames = [f for f in frames if f is not None and len(f)]
        if not frames:
            return pd.Series(dtype=float)
        wide = pd.concat(frames, axis=1)
        return pd.concat([wide.median(axis=1), wide.iloc[:, -1]],
                         axis=1).max(axis=1)
    except Exception as e:  # noqa: BLE001 — 降级但绝不静默
        log.warning(f"[ROUTING] 实测 ADV 不可用({e})— 门降级为纯 ADV̂")
        return pd.Series(dtype=float)


def rnn_layer(covered: pd.Index, pred_V: pd.Series, held: set,
              measured_adv: pd.Series | None = None,
              full_coverage: bool = False) -> tuple[set, dict]:
    """→ (RNN 层票集合, 诊断 dict)。纯函数,不做 IO(可测/可复现)。

    liq = max(ADV̂, 实测 ADV)(实测缺票按 ADV̂);thr = liq 中位数;
    层 = {liq ≥ thr} ∪ (持仓 ∩ 覆盖)。

    full_coverage=True(RNN promote,用户终判 2026-08-17,AB 10 日 econ 9/10 胜):
    层 = 全部覆盖票,流动性门退役;lgbm 只守 RNN 覆盖外残尾。门控代码保留,
    registry blend.rnn_full_coverage 置 false 即回滚到分层模式。"""
    if full_coverage:
        layer = set(covered)
        return layer, {"thr": 0.0, "gate_source": "full_coverage(promote_20260817)",
                       "n_layer": len(layer), "n_covered": int(len(covered)),
                       "n_measured_lifted": 0}
    liq_pred = pred_V.reindex(covered)
    if measured_adv is not None and len(measured_adv):
        liq = pd.concat([liq_pred, measured_adv.reindex(covered)],
                        axis=1).max(axis=1)
        gate_source = "max(pred,measured)"
    else:
        liq = liq_pred
        gate_source = "pred_only(degraded)"
    thr = float(liq.median())
    layer = set(liq[liq >= thr].index) | (held & set(covered))
    n_rescued = int((liq > liq_pred).reindex(layer).fillna(False).sum())
    return layer, {"thr": thr, "gate_source": gate_source,
                   "n_layer": len(layer), "n_covered": int(len(covered)),
                   "n_measured_lifted": n_rescued}
