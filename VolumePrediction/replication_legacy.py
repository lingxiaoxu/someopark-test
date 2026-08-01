"""
replication_legacy — P1 出口验收物: 旧 12 模型在新面板上的完整成绩表
====================================================================
plan §九 P1: "输出: 旧 12 模型在新面板上的完整成绩表,与旧论文表并排
(标注宇宙/区间差异)";验收含定性排序复现(PLS 最强线性/NN2 全局领先/
shock 难于 log_volume/财报日 dummies 显著)。

协议: paper split(与 replication_g 一致),全谱特征列;目标 η(论文口径 R²,
分母 Σy²);旧作对照成绩(log_volume 目标, 旧宇宙 2015-2023)在输出表
old_score 列并排——量级可比性受宇宙(R3K 代理 vs 旧 500 票)与区间差异限制,
定性排序才是验收物。

分层跑批(生产窗友好):
  --tier light : ma5/prevday/arima/sarima/ols/lassocv/pcr/pls/fwdstep(CPU 轻)
  --tier ml    : adaboost/lgbm(CPU 重;lgbm 子进程隔离)
  --tier deep  : nn2/lstm_single(MPS,重)
  --tier all
每模型完成即增量落盘 outputs/replication/legacy_scores.csv(断点续跑,
已存在的模型行自动跳过,--force 重算)。
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, get_logger
from VolumePrediction.replication_g import load_panel, paper_split, cols_upto, _r2_eta

log = get_logger("replication_legacy")
CSV = OUT / "replication" / "legacy_scores.csv"

# 旧作成绩留档(P1 并排列;旧 notebook 口径 log_volume 目标全局 R²)
OLD_SCORES = {
    "ma5": None, "prevday": None, "arima": None, "sarima": None,
    "ols": 0.79, "lassocv": 0.79, "pcr": 0.78, "pls": 0.80,
    "fwdstep": 0.79, "adaboost": 0.28, "nn2": 0.815, "lgbm": None,
    "lstm_single": 0.60,
}

TIERS = {
    "light": ["ma5", "prevday", "arima", "sarima", "ols", "lassocv",
              "pcr", "pls", "fwdstep"],
    "ml": ["adaboost", "lgbm"],
    "deep": ["nn2", "lstm_single"],
}


def make_model(kind: str, n_pred: int):
    if kind == "ma5":
        from VolumePrediction.models.baselines import MA5Model
        return MA5Model()
    if kind == "prevday":
        from VolumePrediction.models.baselines import PrevDayModel
        return PrevDayModel()
    if kind in ("arima", "sarima"):
        from VolumePrediction.models import baselines as bl
        cls = bl.ARIMAPerTicker if kind == "arima" else bl.SARIMAPerTicker
        return cls()
    if kind == "ols":
        from VolumePrediction.models.linear import OLSModel
        return OLSModel()
    if kind == "lassocv":
        from VolumePrediction.models.linear import LassoCVModel
        return LassoCVModel()
    if kind == "pcr":
        from VolumePrediction.models.linear import PCRModel
        return PCRModel()
    if kind == "pls":
        from VolumePrediction.models.linear import PLSModel
        return PLSModel()
    if kind == "fwdstep":
        from VolumePrediction.models.linear import ForwardStepwiseModel
        return ForwardStepwiseModel()
    if kind == "adaboost":
        from VolumePrediction.models.ml import AdaBoostModel
        return AdaBoostModel()
    if kind == "lgbm":
        from VolumePrediction.models.ml import LightGBMModel
        return LightGBMModel()
    if kind == "nn2":
        from VolumePrediction.models.ml import NN2Model
        return NN2Model()
    if kind == "lstm_single":
        from VolumePrediction.models.deep import SingleLSTM
        return SingleLSTM(n_pred)
    raise ValueError(kind)


def run(panel_tag: str, tiers: List[str], force: bool = False,
        sample_tickers: Optional[int] = None) -> pd.DataFrame:
    CSV.parent.mkdir(parents=True, exist_ok=True)
    done: Dict[str, dict] = {}
    if CSV.exists() and not force:
        done = {r["model"]: r for r in pd.read_csv(CSV).to_dict("records")}

    panel = load_panel(panel_tag)
    tr, te = paper_split(panel)
    tr = tr[tr["eta"].notna()]
    te = te[te["eta"].notna()]
    cols = cols_upto(panel, "earn")
    log.info(f"legacy scores: {len(cols)} cols | train {len(tr):,} | test {len(te):,}")

    models = [m for t in tiers for m in TIERS[t]]
    rows = list(done.values())
    for mk in models:
        if mk in done:
            log.info(f"{mk}: cached, skip")
            continue
        t0 = time.time()
        try:
            m = make_model(mk, len(cols))
            if mk in ("ma5", "prevday", "arima", "sarima"):
                # 时序基线: 只吃目标序列面板(fit 接口同构,X 传含 v/ma5_v 的面板)
                m.fit(tr, tr["eta"])
                yhat = m.predict(te)
            else:
                m.fit(tr[cols], tr["eta"])
                yhat = m.predict(te[cols])
            r2 = _r2_eta(te["eta"], yhat) * 100
            row = {"model": mk, "r2_eta_pct": round(r2, 2),
                   "old_score_logv": OLD_SCORES.get(mk),
                   "seconds": round(time.time() - t0),
                   "note": ""}
            log.info(f"{mk}: R²(η)={r2:.2f}% ({time.time()-t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            row = {"model": mk, "r2_eta_pct": None,
                   "old_score_logv": OLD_SCORES.get(mk),
                   "seconds": round(time.time() - t0),
                   "note": f"FAILED: {e}"[:160]}
            log.warning(f"{mk} failed: {e}")
        rows.append(row)
        pd.DataFrame(rows).to_csv(CSV, index=False)
    df = pd.DataFrame(rows)
    log.info(f"legacy scores → {CSV}")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="paper_full_v5")
    ap.add_argument("--tier", default="light", choices=["light", "ml", "deep", "all"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    tiers = ["light", "ml", "deep"] if a.tier == "all" else [a.tier]
    print(run(a.panel, tiers, force=a.force).to_string(index=False))
