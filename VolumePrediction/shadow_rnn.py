"""shadow_rnn — RNN 候选 vs 现役 lgbm 的每日影子 AB（E4 晋升决策依据）。

为什么要影子而不是直接看回测: 12 窗 walk-forward 给的是历史 OOS 成绩
(窄集 RNN 0.2993 vs 现役 lgbm 0.1813),但生产服务路径与回测路径不同
(冻结统计/seq_tail 滚动/active 过滤/多 seed 均值)。影子跑的是**真实服务路径**,
且两模型评的是**同一批票**——回测里 lgbm 服务全宇宙、RNN 只服务窄集覆盖票,
样本不同质;这里取交集,才是干净对照。

每日两件事:
1. serve 候选工件(update_state=True 滚动 seq_tail)→ 落盘当日预测
2. 滞后口径评估: 昨日预测 vs 今日实际 —— RNN / 现役 production / ma5 三档
   在**同一交集票**上算 log 空间 R² 与 MAPE,追加 rnn_ab_tracking.csv

纪律:
- 只读现役工件与 raw 缓存,只写 outputs/shadow_rnn/,不碰 production 指针
- 任何失败大声 log 并返回非零,绝不静默(影子数据缺口会污染 AB 判决)
- seq_tail 断档时 serve 会抛错 —— 这是有意的,宁可缺一天也不出错位预测

用法: python -m VolumePrediction.shadow_rnn [--target YYYY-MM-DD] [--no-roll]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO

log = logging.getLogger("VolumePrediction.shadow_rnn")

OUT = REPO / "VolumePrediction" / "outputs"
SHADOW_DIR = OUT / "shadow_rnn"
TRACK_CSV = SHADOW_DIR / "rnn_ab_tracking.csv"
CANDIDATE = OUT / "registry" / "artifacts" / "rnn_v6f32n_20260731"


def _metrics(pred_V: pd.Series, actual: pd.Series) -> dict:
    """log 空间 R² + 水平 MAPE(与 shadow_tracking 同口径)。"""
    m = pd.concat([pred_V.rename("p"), actual.rename("a")], axis=1).dropna()
    m = m[(m.p > 0) & (m.a > 0)]
    if len(m) < 30:
        return {"n": len(m), "r2": None, "mape": None}
    lp, la = np.log(m.p), np.log(m.a)
    err = lp - la
    r2 = 1 - float((err ** 2).sum()) / float(((la - la.mean()) ** 2).sum())
    mape = float((np.abs(m.p - m.a) / m.a).mean() * 100)
    return {"n": int(len(m)), "r2": round(r2, 4), "mape": round(mape, 4)}


def serve_candidate(target: str, roll: bool = True) -> pd.DataFrame:
    """跑候选 RNN 的真实服务路径并落盘。"""
    from VolumePrediction import prod_model_rnn as pmr
    out = pmr.serve(CANDIDATE, target, update_state=roll)
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    p = SHADOW_DIR / f"rnn_pred_{target}.parquet"
    tmp = p.with_suffix(".tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(p)
    log.info(f"[SHADOW_RNN] {target}: {len(out)} 票预测 → {p.name}")
    return out


def evaluate(actual_date: str) -> dict | None:
    """滞后口径: 上一交易日的三档预测 vs actual_date 的真实成交额。"""
    from VolumePrediction.service import VolumeService
    svc = VolumeService()
    day = svc._load_day(actual_date)
    if day is None or day.empty:
        log.error(f"[SHADOW_RNN] {actual_date} 无实际数据 — 跳过评估")
        return None
    actual = day.set_index("ticker")["dollar_volume"]

    # 上一份影子预测(RNN)与同日的生产工件(lgbm/ma5 混合)
    preds = sorted(SHADOW_DIR.glob("rnn_pred_*.parquet"))
    prior = [p for p in preds if p.stem.split("_")[-1] < actual_date]
    if not prior:
        log.warning("[SHADOW_RNN] 无更早的 RNN 预测 — 首日,仅落盘不评估")
        return None
    rnn_p = prior[-1]
    pred_date = rnn_p.stem.split("_")[-1]
    rnn = pd.read_parquet(rnn_p).set_index("ticker")

    hist = sorted((OUT / "history").glob("volume_forecast_*.parquet"))
    prod_f = [f for f in hist if f.stem.split("_")[-1] <= pred_date]
    if not prod_f:
        log.error("[SHADOW_RNN] 无对应日期的生产工件 — 跳过")
        return None
    prod = pd.read_parquet(prod_f[-1]).set_index("ticker")

    # 干净对照: 三档取**同一交集票**
    common = rnn.index.intersection(prod.index).intersection(actual.index)
    if len(common) < 30:
        log.error(f"[SHADOW_RNN] 交集票仅 {len(common)} — 跳过")
        return None
    a = actual.loc[common]
    row = {"pred_date": pred_date, "actual_date": actual_date,
           "n_common": int(len(common))}
    for tag, s in (("rnn", rnn.loc[common, "pred_V"]),
                   ("prod", prod.loc[common, "pred_V"])):
        m = _metrics(s, a)
        row[f"{tag}_r2"] = m["r2"]
        row[f"{tag}_mape"] = m["mape"]
    # 生产档在交集上的模型构成(交集应几乎全是 lgbm 覆盖票)
    if "model_version" in prod.columns:
        vc = prod.loc[common, "model_version"].value_counts()
        row["prod_mix"] = ";".join(f"{k}:{v}" for k, v in vc.items())
    row["rnn_wins_r2"] = (row["rnn_r2"] is not None and row["prod_r2"] is not None
                          and row["rnn_r2"] > row["prod_r2"])
    return row


def append_track(row: dict) -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    df.to_csv(TRACK_CSV, mode="a", header=not TRACK_CSV.exists(), index=False)
    log.info(f"[SHADOW_RNN] 追加 {TRACK_CSV.name}: {row}")


def _already_tracked(actual_date: str | None) -> bool:
    """该 actual_date 是否已在 AB 追踪表中（幂等去重）。"""
    if not actual_date or not TRACK_CSV.exists():
        return False
    try:
        return actual_date in set(pd.read_csv(TRACK_CSV)["actual_date"].astype(str))
    except Exception:  # noqa: BLE001
        return False


def run_daily(target: str | None = None, roll: bool = True,
              eval_only: bool = False) -> int:
    """不依赖 argv 的日更入口（供 daily_update 直接调用，避免 argparse/SystemExit）。

    与旧 main() 的两处关键差异（均为修 bug）：
      1. serve 目标只从 **_raw_dates()**（真实有数据的交易日）取，绝不用
         trading_days 生成越过 raw_last 的未来日 —— 那会用陈旧特征污染 seq_tail。
      2. 评估**补齐所有缺失的 (pred→次交易日 actual) 对**（幂等去重），而非只评
         最新一天；断更多日后一次补回。
    """
    if not CANDIDATE.exists():
        log.error(f"[SHADOW_RNN] 候选工件不存在: {CANDIDATE}")
        return 1
    meta = json.loads((CANDIDATE / "meta.json").read_text())

    from VolumePrediction.service import VolumeService
    raw = VolumeService()._raw_dates()
    if not raw:
        log.error("[SHADOW_RNN] 无 raw 交易日")
        return 1
    raw_last = raw[-1]

    if not eval_only:
        seq_d = meta.get("seq_tail_date", meta["trained_through"])
        # 只 serve seq_tail 之后、且有真实 raw 数据的交易日（按序，有状态滚动）。
        pending = [target] if target else [d for d in raw if d > seq_d]
        for tgt in pending:
            if tgt > raw_last:                 # 双保险：绝不 serve 越过 raw_last
                log.warning(f"[SHADOW_RNN] 跳过越界目标 {tgt} > raw_last {raw_last}")
                continue
            try:
                serve_candidate(tgt, roll=roll)
            except Exception as e:  # noqa: BLE001
                log.error(f"[SHADOW_RNN] serve 失败({tgt}): {e}")
                return 2

    # 补齐所有缺失的 (pred_date → 次交易日 actual) 评估，幂等。
    import glob as _glob
    pred_days = sorted(p.split("rnn_pred_")[1][:10]
                       for p in _glob.glob(str(SHADOW_DIR / "rnn_pred_*.parquet")))
    n_new, last_row = 0, None
    for pdd in pred_days:
        nxt = [d for d in raw if d > pdd]
        if not nxt or _already_tracked(nxt[0]):
            continue
        row = evaluate(nxt[0])
        if row:
            append_track(row)
            last_row, n_new = row, n_new + 1
    log.info(f"[SHADOW_RNN] 评估补齐 {n_new} 行 (raw_last={raw_last})")
    if last_row:
        print(json.dumps(last_row, ensure_ascii=False))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None,
                    help="服务目标日(默认 = seq_tail 之后所有有 raw 数据的交易日)")
    ap.add_argument("--no-roll", action="store_true",
                    help="不滚动 seq_tail(只出预测,用于补跑/调试)")
    ap.add_argument("--eval-only", action="store_true", help="只做滞后评估")
    a = ap.parse_args()
    return run_daily(target=a.target, roll=not a.no_roll, eval_only=a.eval_only)


if __name__ == "__main__":
    sys.exit(main())
