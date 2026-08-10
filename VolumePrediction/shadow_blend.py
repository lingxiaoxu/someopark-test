"""shadow_blend — 分层服务(选项 B)的每日影子,双变体对照(用户批准 2026-08-10)。

终判(P4 §3c,v2 主裁 RNN 4/5)后的实施第一步:**影子先行,不动 production 指针**。
两个候选变体 + 现役 prod 三方每日对照:
  blend2 — RNN 服务其全部覆盖票 + ma5 兜底(实测 RNN 覆盖⊇lgbm 覆盖 → lgbm 层 0 行)
  blend3 — 真三层(用户 2026-08-10): RNN 层=持仓票∪高流动票(RNN 覆盖内 ADV̂
           top50%),其余 lgbm 覆盖票由 lgbm 守尾部,ma5 兜底

影子层是**纯拼接、零新算力**:每日已有
  outputs/shadow_rnn/rnn_pred_{date}.parquet   (RNN 覆盖票,shadow_rnn 产)
  outputs/history/volume_forecast_{date}.parquet(现役两层: lgbm+ma5)
按变体路由规则替换行 → blend_pred_{date}.parquet / blend3_pred_{date}.parquet。

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


def _splice(prod: pd.DataFrame, rnn: pd.DataFrame, rnn_layer) -> pd.DataFrame:
    out = prod.copy()
    take = pd.Index(rnn_layer).intersection(rnn.index).intersection(out.index)
    for c in ("pred_v", "pred_V", "pred_eta", "model_version"):
        if c in rnn.columns:
            out.loc[take, c] = rnn.loc[take, c]
    return out


def build_blend(pred_date: str) -> dict | None:
    """两个 blend 变体一次拼接落盘(幂等覆盖,零新算力):

    blend2 — RNN 服务其**全部**覆盖票,其余保留 prod。实测 RNN 覆盖 ⊇ lgbm 覆盖,
             故实质= RNN 学习层 + ma5 兜底(lgbm 层 0 行)。
    blend3 — 真三层(用户 2026-08-10): RNN 层 = 持仓票 ∪ 高流动票(RNN 覆盖内
             ADV̂ ≥ 中位数,即 top 50%,按现役 prod 的 pred_V 排,PIT 安全);
             其余 lgbm 覆盖票留给 lgbm 守尾部;ma5 兜底。"""
    rnn_p = RNN_DIR / f"rnn_pred_{pred_date}.parquet"
    prod_p = OUT / "history" / f"volume_forecast_{pred_date}.parquet"
    if not rnn_p.exists() or not prod_p.exists():
        log.warning(f"[BLEND] {pred_date}: 缺 {'rnn' if not rnn_p.exists() else 'prod'} 工件 — 跳过")
        return None
    rnn = pd.read_parquet(rnn_p).set_index("ticker")
    prod = pd.read_parquet(prod_p).set_index("ticker")
    covered = rnn.index.intersection(prod.index)

    blend2 = _splice(prod, rnn, covered)

    held = _held_tickers(pred_date)
    liq = prod.loc[covered, "pred_V"]
    thr = float(liq.median())
    layer3 = set(liq[liq >= thr].index) | (held & set(covered))
    blend3 = _splice(prod, rnn, list(layer3))

    BLEND_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, df in (("blend", blend2), ("blend3", blend3)):
        p = BLEND_DIR / f"{name}_pred_{pred_date}.parquet"
        tmp = p.with_suffix(".tmp")
        df.reset_index().to_parquet(tmp, index=False)
        tmp.replace(p)
        out[name] = df
    m3 = blend3["model_version"].value_counts().to_dict()
    log.info(f"[BLEND] {pred_date}: blend2 rnn层={len(covered)};"
             f" blend3 rnn层={len(layer3)} (liq_thr={thr:,.0f}) mix3={m3}")
    return out


def evaluate(pred_date: str, actual_date: str) -> dict | None:
    """滞后评估: blend2 / blend3 / 现役 prod 三方(v2 口径: 消费子集主裁 + 全宇宙参考)。"""
    from VolumePrediction.service import VolumeService
    day = VolumeService()._load_day(actual_date)
    if day is None or day.empty:
        log.error(f"[BLEND] {actual_date} 无实际数据 — 跳过")
        return None
    actual = day.set_index("ticker")["dollar_volume"]

    prod_p = OUT / "history" / f"volume_forecast_{pred_date}.parquet"
    b2_p = BLEND_DIR / f"blend_pred_{pred_date}.parquet"
    b3_p = BLEND_DIR / f"blend3_pred_{pred_date}.parquet"
    if not (prod_p.exists() and b2_p.exists() and b3_p.exists()):
        return None
    prod = pd.read_parquet(prod_p).set_index("ticker")
    b2 = pd.read_parquet(b2_p).set_index("ticker")
    b3 = pd.read_parquet(b3_p).set_index("ticker")

    common = b2.index.intersection(prod.index).intersection(actual.index)
    if len(common) < 30:
        return None
    a = actual.loc[common]
    mu, mu_src = _ab_mu()
    row = {"pred_date": pred_date, "actual_date": actual_date,
           "n_common": int(len(common))}
    variants = (("blend", b2), ("blend3", b3), ("prod", prod))
    for tag, src in variants:
        m = _metrics(src.loc[common, "pred_V"], a, mu=mu)
        for k in ("r2", "mape", "log_mse", "econ"):
            row[f"{tag}_{k}"] = m[k]
    held = _held_tickers(pred_date)
    hc = [t for t in common if t in held]
    row["n_held"] = len(hc)
    for tag, src in variants:
        hm = _metrics(src.loc[hc, "pred_V"], a.loc[hc], mu=mu, min_n=20) if hc else \
            {"mape": None, "log_mse": None}
        row[f"{tag}_held_mape"] = hm["mape"]
        row[f"{tag}_held_log_mse"] = hm["log_mse"]
    for tag, src in (("blend", b2), ("blend3", b3)):
        if "model_version" in src.columns:
            vc = src.loc[common, "model_version"].value_counts()
            row[f"{tag}_mix"] = ";".join(f"{k}:{v}" for k, v in vc.items())

    def _lower(tag, k):
        b, p = row.get(f"{tag}_{k}"), row.get(f"prod_{k}")
        return None if (b is None or p is None) else bool(b < p)
    for tag in ("blend", "blend3"):
        row[f"{tag}_wins_held_mape"] = _lower(tag, "held_mape")
        row[f"{tag}_wins_held_log_mse"] = _lower(tag, "held_log_mse")
        row[f"{tag}_wins_mape"] = _lower(tag, "mape")
        row[f"{tag}_wins_log_mse"] = _lower(tag, "log_mse")
        hm_, hl_ = row[f"{tag}_wins_held_mape"], row[f"{tag}_wins_held_log_mse"]
        row[f"{tag}_wins"] = (bool(hm_ and hl_) if (hm_ is not None and hl_ is not None)
                              else row[f"{tag}_wins_mape"])
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
        if rebuild or not (BLEND_DIR / f"blend_pred_{pdd}.parquet").exists() \
                or not (BLEND_DIR / f"blend3_pred_{pdd}.parquet").exists():
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
                          "blend3_wins": int(df["blend3_wins"].sum())
                          if "blend3_wins" in df else None,
                          "last": df.iloc[-1].to_dict()},
                         ensure_ascii=False, default=str)[:900])
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
