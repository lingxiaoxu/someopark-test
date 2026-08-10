"""shadow_blend — 三层分层服务(选项 B)的每日影子(用户批准 2026-08-10)。

终判(P4 §3c,v2 主裁 RNN 4/5)后的实施第一步:**影子先行,不动 production 指针**。
三层 = RNN 服务其覆盖票 → lgbm 服务其余覆盖票 → ma5 兜底。

影子层的 blend 是**纯拼接、零新算力**:每日已有
  outputs/shadow_rnn/rnn_pred_{date}.parquet   (RNN 覆盖票,shadow_rnn 产)
  outputs/history/volume_forecast_{date}.parquet(现役两层: lgbm+ma5)
把 prod 工件中 RNN 覆盖票的行替换为 RNN 预测(model_version 标 RNN 工件名),
其余行原样保留 → blend_pred_{date}.parquet。

滞后评估(与 shadow_rnn 同口径,复用其 _metrics/_held_tickers/v2 主裁):
blend vs 现役 prod,全宇宙 + 消费子集(持仓票)双口径,追加 blend_ab_tracking.csv。
`blend_wins` = 消费子集 MAPE+log-MSE 双赢(fallback 全宇宙 MAPE)。

纪律: 只写 outputs/shadow_blend/;不碰 production 指针与现役工件;失败大声退非零。
时间线: 8/11-8/14 每日双轨 → 8/15 与 E1 消费切换一并拍板是否切正式 serve。
用法: python -m VolumePrediction.shadow_blend [--rebuild] [--eval-only]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import pandas as pd

from VolumePrediction.shadow_rnn import (_ab_mu, _held_tickers, _metrics,
                                         SHADOW_DIR as RNN_DIR, OUT)

log = logging.getLogger("VolumePrediction.shadow_blend")

BLEND_DIR = OUT / "shadow_blend"
TRACK_CSV = BLEND_DIR / "blend_ab_tracking.csv"


def build_blend(pred_date: str) -> pd.DataFrame | None:
    """纯拼接: prod 工件 + RNN 覆盖票行替换 → 三层 blend 工件(落盘,幂等覆盖)。"""
    rnn_p = RNN_DIR / f"rnn_pred_{pred_date}.parquet"
    prod_p = OUT / "history" / f"volume_forecast_{pred_date}.parquet"
    if not rnn_p.exists() or not prod_p.exists():
        log.warning(f"[BLEND] {pred_date}: 缺 {'rnn' if not rnn_p.exists() else 'prod'} 工件 — 跳过")
        return None
    rnn = pd.read_parquet(rnn_p).set_index("ticker")
    prod = pd.read_parquet(prod_p).set_index("ticker")
    blend = prod.copy()
    covered = rnn.index.intersection(blend.index)
    for c in ("pred_v", "pred_V", "pred_eta", "model_version"):
        if c in rnn.columns:
            blend.loc[covered, c] = rnn.loc[covered, c]
    BLEND_DIR.mkdir(parents=True, exist_ok=True)
    p = BLEND_DIR / f"blend_pred_{pred_date}.parquet"
    tmp = p.with_suffix(".tmp")
    blend.reset_index().to_parquet(tmp, index=False)
    tmp.replace(p)
    mix = blend["model_version"].value_counts().to_dict()
    log.info(f"[BLEND] {pred_date}: {len(blend)} 票 (rnn 层 {len(covered)}) → {p.name} mix={mix}")
    return blend


def evaluate(pred_date: str, actual_date: str) -> dict | None:
    """滞后评估: blend vs 现役 prod(v2 口径: 消费子集主裁 + 全宇宙参考)。"""
    from VolumePrediction.service import VolumeService
    day = VolumeService()._load_day(actual_date)
    if day is None or day.empty:
        log.error(f"[BLEND] {actual_date} 无实际数据 — 跳过")
        return None
    actual = day.set_index("ticker")["dollar_volume"]

    bl_p = BLEND_DIR / f"blend_pred_{pred_date}.parquet"
    prod_p = OUT / "history" / f"volume_forecast_{pred_date}.parquet"
    if not bl_p.exists() or not prod_p.exists():
        return None
    bl = pd.read_parquet(bl_p).set_index("ticker")
    prod = pd.read_parquet(prod_p).set_index("ticker")

    common = bl.index.intersection(prod.index).intersection(actual.index)
    if len(common) < 30:
        return None
    a = actual.loc[common]
    mu, mu_src = _ab_mu()
    row = {"pred_date": pred_date, "actual_date": actual_date,
           "n_common": int(len(common))}
    for tag, src in (("blend", bl), ("prod", prod)):
        m = _metrics(src.loc[common, "pred_V"], a, mu=mu)
        for k in ("r2", "mape", "log_mse", "econ"):
            row[f"{tag}_{k}"] = m[k]
    held = _held_tickers(pred_date)
    hc = [t for t in common if t in held]
    row["n_held"] = len(hc)
    for tag, src in (("blend", bl), ("prod", prod)):
        hm = _metrics(src.loc[hc, "pred_V"], a.loc[hc], mu=mu, min_n=20) if hc else \
            {"mape": None, "log_mse": None}
        row[f"{tag}_held_mape"] = hm["mape"]
        row[f"{tag}_held_log_mse"] = hm["log_mse"]
    if "model_version" in bl.columns:
        vc = bl.loc[common, "model_version"].value_counts()
        row["blend_mix"] = ";".join(f"{k}:{v}" for k, v in vc.items())

    def _lower(k):
        b, p = row.get(f"blend_{k}"), row.get(f"prod_{k}")
        return None if (b is None or p is None) else bool(b < p)
    row["blend_wins_held_mape"] = _lower("held_mape")
    row["blend_wins_held_log_mse"] = _lower("held_log_mse")
    row["blend_wins_mape"] = _lower("mape")
    row["blend_wins_log_mse"] = _lower("log_mse")
    if row["blend_wins_held_mape"] is not None and row["blend_wins_held_log_mse"] is not None:
        row["blend_wins"] = bool(row["blend_wins_held_mape"] and row["blend_wins_held_log_mse"])
    else:
        row["blend_wins"] = row["blend_wins_mape"]
    return row


def _already(actual_date: str) -> bool:
    if not TRACK_CSV.exists():
        return False
    try:
        return actual_date in set(pd.read_csv(TRACK_CSV)["actual_date"].astype(str))
    except Exception:  # noqa: BLE001
        return False


def append_track(row: dict) -> None:
    BLEND_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    if TRACK_CSV.exists():
        old = pd.read_csv(TRACK_CSV)
        if set(old.columns) != set(df.columns):
            pd.concat([old, df], ignore_index=True).to_csv(TRACK_CSV, index=False)
            log.info(f"[BLEND] schema 迁移重写: {row}")
            return
    df.to_csv(TRACK_CSV, mode="a", header=not TRACK_CSV.exists(), index=False)
    log.info(f"[BLEND] 追加 blend_ab_tracking.csv: {row}")


def run_daily(rebuild: bool = False) -> int:
    """构建所有可构建的 blend 工件 + 补齐所有缺失的 (pred→次交易日) 评估,幂等。"""
    from VolumePrediction.service import VolumeService
    raw = VolumeService()._raw_dates()
    if not raw:
        log.error("[BLEND] 无 raw 交易日")
        return 1
    if rebuild and TRACK_CSV.exists():
        bak = TRACK_CSV.with_suffix(".csv.bak")
        TRACK_CSV.replace(bak)
        log.info(f"[BLEND] --rebuild: 旧表备份 {bak.name}")

    rnn_days = sorted(p.stem.split("_")[-1] for p in RNN_DIR.glob("rnn_pred_*.parquet"))
    n_new = 0
    for pdd in rnn_days:
        if not (BLEND_DIR / f"blend_pred_{pdd}.parquet").exists() or rebuild:
            build_blend(pdd)
        nxt = [d for d in raw if d > pdd]
        if not nxt or _already(nxt[0]):
            continue
        row = evaluate(pdd, nxt[0])
        if row:
            append_track(row)
            n_new += 1
    log.info(f"[BLEND] 评估补齐 {n_new} 行")
    if n_new and TRACK_CSV.exists():
        df = pd.read_csv(TRACK_CSV)
        print(json.dumps({"rows": len(df),
                          "blend_wins": int(df["blend_wins"].sum()),
                          "last": df.iloc[-1].to_dict()},
                         ensure_ascii=False, default=str)[:800])
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="重建全部 blend 工件+重算 AB 表")
    ap.add_argument("--eval-only", action="store_true")  # 与 shadow_rnn 对齐的入口语义
    a = ap.parse_args()
    return run_daily(rebuild=a.rebuild)


if __name__ == "__main__":
    sys.exit(main())
