"""shadow_rnn — RNN 候选 vs 现役 lgbm 的每日影子 AB（E4 晋升决策依据）。

为什么要影子而不是直接看回测: 12 窗 walk-forward 给的是历史 OOS 成绩
(窄集 RNN 0.2993 vs 现役 lgbm 0.1813),但生产服务路径与回测路径不同
(冻结统计/seq_tail 滚动/active 过滤/多 seed 均值)。影子跑的是**真实服务路径**,
且两模型评的是**同一批票**——回测里 lgbm 服务全宇宙、RNN 只服务窄集覆盖票,
样本不同质;这里取交集,才是干净对照。

每日两件事:
1. serve 候选工件(update_state=True 滚动 seq_tail)→ 落盘当日预测
2. 滞后口径评估: 昨日预测 vs 今日实际 —— RNN / 现役 production 在**同一交集票**上
   算四指标(R²/MAPE/log-MSE/econ)+ 消费子集(持仓票)MAPE/log-MSE,追加
   rnn_ab_tracking.csv。裁决机制 v2(2026-08-09): 主裁=消费子集 MAPE+log-MSE
   双赢;fallback=全宇宙 MAPE;econ/R² 降参考(理由见 evaluate 内注释与 P4 §3c)。

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


def _ab_mu() -> tuple[float, str]:
    """AB 判决用的 μ(经济损失参数)。优先 calibrated tracking 剖面(aiss_rebalance
    → mu_key=aiss_mom_decay, alpha_decay_curve 校准);失败降级 paper_prior 并标注。
    固定一个 μ 保证 AB 时间序列可比(不随策略上下文漂移)。"""
    try:
        from VolumePrediction.econ.objective import resolve, resolve_mu
        prof = resolve(objective="aiss_rebalance")
        if not prof.is_urgent:                       # urgent(μ=inf) 下 z*恒1,无判别力
            return resolve_mu(prof)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[SHADOW_RNN] μ resolve 失败,降级 paper_prior: {e}")
    return 1.0e-6, "paper_prior_fallback"


def _held_tickers(pred_date: str) -> set:
    """裁决机制 v2(用户批准 2026-08-09): 消费子集 = 四策略当日真实持仓/在场票。
    取 pred_date 当日的 adapters advice 文件(PIT 正确: advice 与预测同日产出);
    当日缺某策略的文件时回退该策略最近一份 ≤ pred_date 的。"""
    held: set = set()
    adir = OUT / "adapters"
    for stem in ("pairs_mrpt_advice", "pairs_mtfs_advice",
                 "aiss_advice", "ssrs_advice"):
        cands = sorted(adir.glob(f"{stem}_*.json"))
        cands = [c for c in cands if c.stem.split("_")[-1] <= pred_date]
        if not cands:
            continue
        try:
            d = json.loads(cands[-1].read_text())
        except Exception:  # noqa: BLE001 — 单文件破损不阻断
            continue
        for h in (d.get("holdings") or []):
            held.add(h.get("ticker"))
        for p in (d.get("positions") or []):
            held.add(p.get("s1")); held.add(p.get("s2"))
    held.discard(None)
    return held


def _metrics(pred_V: pd.Series, actual: pd.Series, mu: float | None = None,
             min_n: int = 30) -> dict:
    """多指标评估(评估纪律 2026-08-08):R² 分母被大票方差主导,只看 R² 会误判;
    MAPE / log-MSE 等权公平,econ_regret 直接度量预测误差的经济代价。

    econ_regret_pct: 论文闭式框架下,按预测 v̂ 定交易率 z*(v̂) 但按实际 v 结算,
    相对"完美预测 z*(v)"的归一化损失超额百分比(等权均值):
        regret_i = losscon(v_i, z*(v̂_i), μ) / losscon(v_i, z*(v_i), μ) − 1
    losscon/s_opt 即 econ/policy.py 的 G6/G9 验收闭式解。越小越好。

    econ 修正列(E10 议题③① 配套小修,2026-08-15):裸 regret 是相对量,
    loss_opt→0(小 λ 端)时"最离谱那一只票"放大 10⁵ 倍(XHG 8/13 实证,
    一票占总量 84%)——补两列消放大,原列保留供 AB 表连续性:
      econ_w   = regret 逐票 winsorize 到 p99 后的等权均值(截尾不删票);
      econ_abs = mean(loss_hat − loss_opt),绝对损失差,量纲=归一化损失。
    λ 不再硬编码论文 0.2/V,跟随 config econ.lambda_form(现=自家 Amihud
    标定 C/V^γ,E11-T1),lambda_source 一并返回。"""
    m = pd.concat([pred_V.rename("p"), actual.rename("a")], axis=1).dropna()
    m = m[(m.p > 0) & (m.a > 0)]
    if len(m) < min_n:
        return {"n": len(m), "r2": None, "mape": None, "log_mse": None,
                "econ": None, "econ_w": None, "econ_abs": None,
                "lambda_source": None}
    lp, la = np.log(m.p), np.log(m.a)
    err = lp - la
    r2 = 1 - float((err ** 2).sum()) / float(((la - la.mean()) ** 2).sum())
    mape = float((np.abs(m.p - m.a) / m.a).mean() * 100)
    log_mse = float((err ** 2).mean())
    econ = econ_w = econ_abs = lam_src = None
    if mu is not None and np.isfinite(mu) and mu > 0:
        from VolumePrediction.common import load_config
        from VolumePrediction.econ.policy import lambda_params
        pars = lambda_params(load_config().get("econ", {})
                             .get("lambda_form", "0.2/V"))
        C, g, lam_src = pars["C"], pars["gamma"], pars["calibration_source"]
        lam_a = C * np.exp(-g * la)
        lam_p = C * np.exp(-g * lp)
        z_hat = mu / (mu + lam_p)                    # s_opt(v̂):按预测定的交易率
        z_true = mu / (mu + lam_a)                   # s_opt(v):完美预测的交易率
        loss_hat = lam_a * z_hat ** 2 + mu * (1 - z_hat) ** 2    # 按实际 v 结算
        loss_opt = lam_a * z_true ** 2 + mu * (1 - z_true) ** 2  # = μλ/(μ+λ) > 0
        regret = (loss_hat / loss_opt) - 1.0
        econ = float(regret.mean() * 100)
        econ_w = float(regret.clip(upper=float(regret.quantile(0.99)))
                       .mean() * 100)
        econ_abs = float((loss_hat - loss_opt).mean())
    return {"n": int(len(m)), "r2": round(r2, 4), "mape": round(mape, 4),
            "log_mse": round(log_mse, 5),
            "econ": (round(econ, 4) if econ is not None else None),
            "econ_w": (round(econ_w, 4) if econ_w is not None else None),
            "econ_abs": (float(f"{econ_abs:.6g}") if econ_abs is not None
                         else None),
            "lambda_source": lam_src}


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
    mu, mu_src = _ab_mu()
    row = {"pred_date": pred_date, "actual_date": actual_date,
           "n_common": int(len(common))}
    for tag, s in (("rnn", rnn.loc[common, "pred_V"]),
                   ("prod", prod.loc[common, "pred_V"])):
        m = _metrics(s, a, mu=mu)
        row[f"{tag}_r2"] = m["r2"]
        row[f"{tag}_mape"] = m["mape"]
        row[f"{tag}_log_mse"] = m["log_mse"]
        row[f"{tag}_econ"] = m["econ"]
        row[f"{tag}_econ_w"] = m["econ_w"]        # winsorized regret(③① 修正)
        row[f"{tag}_econ_abs"] = m["econ_abs"]    # 绝对损失差(③① 修正)
    row["ab_mu"] = mu
    row["ab_mu_source"] = mu_src
    row["ab_lambda_source"] = m["lambda_source"]  # 自家标定/降级论文,如实标注

    # 消费子集(裁决机制 v2): 四策略当日真实持仓票——预测误差的真实美元代价所在。
    held = _held_tickers(pred_date)
    hc = [t for t in common if t in held]
    row["n_held"] = len(hc)
    for tag, src in (("rnn", rnn), ("prod", prod)):
        hm = _metrics(src.loc[hc, "pred_V"], a.loc[hc], mu=mu, min_n=20) if hc else \
            {"mape": None, "log_mse": None}
        row[f"{tag}_held_mape"] = hm["mape"]
        row[f"{tag}_held_log_mse"] = hm["log_mse"]

    # 生产档在交集上的模型构成(交集应几乎全是 lgbm 覆盖票)
    if "model_version" in prod.columns:
        vc = prod.loc[common, "model_version"].value_counts()
        row["prod_mix"] = ";".join(f"{k}:{v}" for k, v in vc.items())

    # ── 裁决机制 v2(用户批准 2026-08-09)───────────────────────────────────
    # 主裁: 消费子集 MAPE + log-MSE 双赢(真实持仓票上的精度=当前规模下真实美元
    #       代价的最好代理;实证: 等权 econ 被不交易的小票尾部主导,而持仓票上
    #       econ 无分辨力 0v0——见 P4 §3c)。
    # 次裁(子集不可用时 fallback): 全宇宙 MAPE。
    # 参考列: econ regret(待 E2 λ 实测/AUM 进 material 区后才有真实牙齿)、R²
    #       (分母被超大票方差主导)、全宇宙 log-MSE(尾部否决项,供 promote 复核)。
    def _lower_wins(k: str):
        r, p = row.get(f"rnn_{k}"), row.get(f"prod_{k}")
        if r is None or p is None:
            return None
        return bool(r < p)
    row["rnn_wins_held_mape"] = _lower_wins("held_mape")
    row["rnn_wins_held_log_mse"] = _lower_wins("held_log_mse")
    row["rnn_wins_econ"] = _lower_wins("econ")
    row["rnn_wins_mape"] = _lower_wins("mape")
    row["rnn_wins_r2"] = (row["rnn_r2"] is not None and row["prod_r2"] is not None
                          and row["rnn_r2"] > row["prod_r2"])
    if row["rnn_wins_held_mape"] is not None and row["rnn_wins_held_log_mse"] is not None:
        row["rnn_wins"] = bool(row["rnn_wins_held_mape"] and row["rnn_wins_held_log_mse"])
    else:
        row["rnn_wins"] = row["rnn_wins_mape"]
    return row


def append_track(row: dict) -> None:
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row])
    if TRACK_CSV.exists():
        old = pd.read_csv(TRACK_CSV)
        if set(old.columns) != set(df.columns):
            # schema 迁移(多指标列上线): 旧行补 NaN 对齐新列集后整表重写,
            # 绝不裸 append —— mode="a" 不看已有 header,列错位会破坏整张 AB 表。
            merged = pd.concat([old, df], ignore_index=True)
            merged.to_csv(TRACK_CSV, index=False)
            log.info(f"[SHADOW_RNN] schema 迁移重写 {TRACK_CSV.name} "
                     f"({len(old.columns)}→{len(merged.columns)} 列): {row}")
            return
        # 列集相同但**顺序**可能不同(dict 插入序变了就会发生): set() 比较看不出来,
        # 而 mode="a" 按 df 自己的列序写值、不看已有 header → 静默错位、列数不变、不报错。
        # 2026-08-17 promote 把 *_econ_w/_econ_abs/ab_lambda_source 从 dict 尾部挪到中间,
        # 08-17/08-18 两行就是这么被写坏的。这里按已有表头对齐后再 append。
        df = df[old.columns]
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
              eval_only: bool = False, rebuild: bool = False) -> int:
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
        # seq_tail 单滚纪律(E10 实施-1,2026-08-15): blend3 正式路径启用后,
        # ops.refresh 是唯一滚动者 —— 影子强制转只读,双滚会把序列状态推快一天。
        if roll:
            try:
                from VolumePrediction.service import VolumeService
                if (VolumeService().s.registry.load().get("blend")
                        or {}).get("enabled"):
                    roll = False
                    log.info("[SHADOW_RNN] blend3 已启用 — 影子转只读"
                             "(update_state=False),seq_tail 由 refresh 滚动")
            except Exception:  # noqa: BLE001 — registry 读不到按未启用处理
                pass
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

    if rebuild and TRACK_CSV.exists():
        # 指标口径升级后重算全部历史行(evaluate 是纯函数: parquet+actual 都在盘上)
        bak = TRACK_CSV.with_suffix(".csv.bak")
        TRACK_CSV.replace(bak)
        log.info(f"[SHADOW_RNN] --rebuild: 旧表已备份 {bak.name},全量重评")

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
    ap.add_argument("--rebuild", action="store_true",
                    help="指标口径升级后: 备份并重算整张 AB 表(全部历史 pred 对)")
    a = ap.parse_args()
    return run_daily(target=a.target, roll=not a.no_roll, eval_only=a.eval_only,
                     rebuild=a.rebuild)


if __name__ == "__main__":
    sys.exit(main())
