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


def _ma5_floor(svc, pred_date: str) -> pd.Series | None:
    """pred_date 收盘后的 ma5 地板预测($成交额),与生产口径逐字同款。

    **几何**均值 —— service.refresh 是 `v = np.log(piv); ma5 = v[-5:].mean();
    pred_V = np.exp(ma5)`。用算术均值重建会系统性偏高(实测中位 1.15 倍),
    对照臂立刻失真。窗 = 截至 pred_date(含)的 5 个交易日,和 refresh 在
    asof 当天算出、供次日消费的那一份完全一致。
    """
    try:
        ds = [d for d in svc._raw_dates() if d <= pred_date][-5:]
        if len(ds) < 5:
            return None
        fr = []
        for d in ds:
            day = svc._load_day(d)
            if day is None or day.empty:
                return None
            fr.append(day[["ticker", "dollar_volume"]].assign(date=d))
        lf = pd.concat(fr)
        lf = lf[lf["dollar_volume"] > 0]
        piv = lf.pivot_table(index="ticker", columns="date",
                             values="dollar_volume", aggfunc="first")
        return np.exp(np.log(piv).mean(axis=1)).dropna()
    except Exception as e:  # noqa: BLE001 — 地板臂缺失不阻断 AB
        log.warning(f"[SHADOW_RNN] ma5 地板重建失败: {e}")
        return None


def _paired(rnn_s: pd.Series | None, other_s: pd.Series | None,
            actual: pd.Series, min_n: int = 20) -> dict:
    """逐票配对检验: 同一天、同一批票上,RNN 与对照臂谁的 |log 误差| 更小。

    为什么需要(用户批准 2026-08-26): 现有裁决是"当日聚合 MAPE 谁低"再数天数 ——
    10 天的 7/10 做符号检验 p≈0.17,**不显著**,只够当运营直觉。而持仓票每天有
    ~200 只,逐票配对后单日 n≈200、10 日累计 n≈2000,Wilcoxon 才有统计力。
    两种口径互补: 聚合看"平均水平",配对看"多数票上谁更准",聚合会被少数几只
    巨额票主导(事故期 RNN 的 held MAPE 被高估的大票拖成 56.4)。

    误差用 **|log(pred/actual)|** 而非平方: 配对问的是"多数票谁更准",尾部单票
    爆炸由 log_mse 那条指标单管,不该在这里被重复计入。
    → {n, winrate, p}。winrate = RNN 更准的票占比;p = Wilcoxon 符号秩双侧。
    样本不足/退化(两臂逐位相同,如 blend 开启后 ref=prod 的自比自)→ p=None。
    """
    if rnn_s is None or other_s is None:
        return {"n": 0, "winrate": None, "p": None}
    idx = rnn_s.index.intersection(other_s.index).intersection(actual.index)
    if len(idx) < min_n:
        return {"n": int(len(idx)), "winrate": None, "p": None}
    r = rnn_s.reindex(idx).to_numpy(float)
    o = other_s.reindex(idx).to_numpy(float)
    a = actual.reindex(idx).to_numpy(float)
    ok = (r > 0) & (o > 0) & (a > 0) & np.isfinite(r) & np.isfinite(o) & np.isfinite(a)
    if ok.sum() < min_n:
        return {"n": int(ok.sum()), "winrate": None, "p": None}
    er = np.abs(np.log(r[ok] / a[ok]))
    eo = np.abs(np.log(o[ok] / a[ok]))
    diff = er - eo                                   # <0 = RNN 更准
    n = int(ok.sum())
    wr = float((diff < 0).mean())
    p = None
    try:
        from scipy import stats
        if np.any(diff != 0):                        # 全 0 = 两臂同源,无从检验
            p = float(stats.wilcoxon(diff).pvalue)
    except Exception as e:  # noqa: BLE001 — 统计量缺失不阻断 AB 记账
        log.warning(f"[SHADOW_RNN] Wilcoxon 失败: {e}")
    # p 用有效数字而不是 round(p, 6): 强效应下 Wilcoxon 会给到 1e-30 量级,
    # 定点舍入一律压成 0.0,"极显著"和"刚好压线"就分不出来了(实测 8/26 真数据
    # 冻死臂 p 被压成 0.0)。3 位有效数字保留量级,CSV 里也读得懂。
    return {"n": n, "winrate": round(wr, 4),
            "p": None if p is None else float(f"{p:.3g}")}


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


def serve_candidate(asof: str, roll: bool = True) -> pd.DataFrame:
    """asof 收盘后跑候选 RNN 的真实服务路径并落盘。

    两套日期不能混(2026-08-26 修):
      - **落盘命名 = as-of 日**: rnn_pred_{asof} 存的是"截至 asof 所做的、对下一
        交易日的预测"。evaluate 正是按 stem < actual_date 配对的,改名会错位。
      - **serve 的 target = next_trading_day(asof)**: serve(T) 预测 T 日
        (锚 ma5v_next = T 之前 5 个交易日均值)。原先直接传 asof,预测的是
        今天(已发生),却被当作次日预测评估 —— 整条 AB 线晚一天。
    """
    from VolumePrediction import prod_model_rnn as pmr
    tgt = pmr.next_trading_day(asof)
    out = pmr.serve(CANDIDATE, tgt, update_state=roll)
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    p = SHADOW_DIR / f"rnn_pred_{asof}.parquet"
    tmp = p.with_suffix(".tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(p)
    log.info(f"[SHADOW_RNN] asof={asof} → target={tgt}: {len(out)} 票预测 → {p.name}")
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

    # ── 对照臂(2026-08-26 修 B)────────────────────────────────────────────
    # prod = 当日**已发布**的工件。blend3 启用后 RNN 层直接覆盖了它,而持仓票
    # 恰好全在 RNN 层里 —— 于是 rnn_* 与 prod_* 在消费子集上逐位相同,
    # rnn_wins 结构性恒 False,自比自,毫无判别力(RNN 从 8/07 起连输 13/17 天
    # 无人察觉,这是两个盲区之一)。
    # 真候选臂:
    #   cf_*  = 反事实"不开 blend"(lgbm+ma5),refresh 在覆盖前存档,零额外算力
    #   ma5_* = 地板(几何 ma5)。RNN 必须打得过地板,否则这一层就是负价值。
    cf_p = OUT / "history" / f"counterfactual_noblend_{pred_date}.parquet"
    cf = pd.read_parquet(cf_p).set_index("ticker") if cf_p.exists() else None
    if cf is None:
        log.warning(f"[SHADOW_RNN] {pred_date} 无反事实存档 — cf 臂缺失"
                    f"(8/26 之前的日期本就没有,之后出现说明 refresh 存档失败)")

    # 干净对照: 各档取**同一交集票**
    common = rnn.index.intersection(prod.index).intersection(actual.index)
    if len(common) < 30:
        log.error(f"[SHADOW_RNN] 交集票仅 {len(common)} — 跳过")
        return None
    a = actual.loc[common]
    mu, mu_src = _ab_mu()
    row = {"pred_date": pred_date, "actual_date": actual_date,
           "n_common": int(len(common))}
    arms = [("rnn", rnn["pred_V"]), ("prod", prod["pred_V"])]
    if cf is not None:
        arms.append(("cf", cf["pred_V"]))
    ma5f = _ma5_floor(svc, pred_date)
    if ma5f is not None:
        arms.append(("ma5", ma5f))
    # 所有臂必须评**同一批票**(样本不同 = 对照不成立,见模块 docstring)。
    # 缺票少 → 把 common 收到各臂交集;缺票多(>2%)→ 弃用那条臂,不让它
    # 反过来把 rnn/prod 的历史口径搅了。两种情况都如实记账,不静默。
    keep, drop = [], []
    for tag, s in arms:
        miss = len(common.difference(s.index))
        if miss > max(1, int(0.02 * len(common))):
            log.warning(f"[SHADOW_RNN] {tag} 臂缺 {miss}/{len(common)} 票(>2%)"
                        f" — 弃用该臂,不缩 common")
            row[f"{tag}_n_missing"] = int(miss)
            drop.append(tag)
            continue
        keep.append((tag, s))
        common = common.intersection(s.index)
    if len(common) < 30:
        log.error(f"[SHADOW_RNN] 各臂取交后仅 {len(common)} 票 — 跳过")
        return None
    a = a.loc[common]
    row["n_common"] = int(len(common))
    ok_arms = [(tag, s.reindex(common)) for tag, s in keep]
    row["ab_arms"] = ";".join(t for t, _ in ok_arms)
    if drop:
        row["ab_arms_dropped"] = ";".join(drop)
    for tag, s in ok_arms:
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
    for tag, s in ok_arms:
        hm = _metrics(s.loc[hc], a.loc[hc], mu=mu, min_n=20) if hc else \
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
    #
    # 对照臂选择(2026-08-26 修 B): 优先 cf(反事实不开 blend),没有才退 prod。
    # blend 启用后 prod 的持仓票行**就是 RNN 自己写进去的**,拿它当对照是自比自。
    # 这里把用了哪条臂如实记进 ab_ref,整张表自解释、新旧行不会被混读。
    ref = "cf" if any(t == "cf" for t, _ in ok_arms) else "prod"
    row["ab_ref"] = ref

    # 逐票配对检验(用户批准 2026-08-26,10 日观察期的主证据)。列名**固定**不随
    # ref 变(用了哪条臂看 ab_ref),否则 CSV 的 schema 会随配置漂移、跨日不可比。
    _arm = dict(ok_arms)
    _ah = a.loc[hc] if hc else a.iloc[:0]
    _rh = _arm["rnn"].loc[hc] if hc and "rnn" in _arm else None
    for _key, _tag in (("paired_ref", ref), ("paired_ma5", "ma5")):
        _pr = _paired(_rh, _arm[_tag].loc[hc] if hc and _tag in _arm else None, _ah)
        row[f"{_key}_held_n"] = _pr["n"]
        row[f"{_key}_held_winrate"] = _pr["winrate"]
        row[f"{_key}_held_p"] = _pr["p"]

    def _lower_wins(k: str):
        r, p = row.get(f"rnn_{k}"), row.get(f"{ref}_{k}")
        if r is None or p is None:
            return None
        return bool(r < p)
    row["rnn_wins_held_mape"] = _lower_wins("held_mape")
    row["rnn_wins_held_log_mse"] = _lower_wins("held_log_mse")
    row["rnn_wins_econ"] = _lower_wins("econ")
    row["rnn_wins_mape"] = _lower_wins("mape")
    row["rnn_wins_r2"] = (row["rnn_r2"] is not None and row.get(f"{ref}_r2") is not None
                          and row["rnn_r2"] > row[f"{ref}_r2"])
    if row["rnn_wins_held_mape"] is not None and row["rnn_wins_held_log_mse"] is not None:
        row["rnn_wins"] = bool(row["rnn_wins_held_mape"] and row["rnn_wins_held_log_mse"])
    else:
        row["rnn_wins"] = row["rnn_wins_mape"]

    # 地板否决: RNN 在持仓票上打不过朴素 ma5,这一层就是负价值 —— 与 cf 谁赢无关。
    # (事故期实测 RNN 从 8/07 起连输 ma5 13/17 天,而当时的 AB 表一个信号都没给。)
    if row.get("ma5_held_mape") is not None and row.get("rnn_held_mape") is not None:
        row["rnn_beats_ma5_held"] = bool(row["rnn_held_mape"] < row["ma5_held_mape"])
        if not row["rnn_beats_ma5_held"]:
            log.warning(
                f"[SHADOW_RNN] {pred_date}: 持仓票上 RNN 输给 ma5 地板 "
                f"({row['rnn_held_mape']:.1f} vs {row['ma5_held_mape']:.1f}) "
                f"— RNN 层当前为负价值,连续多日出现应考虑 set_blend(False)")
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
        # pending 是 **as-of 日**(有真实 raw 数据的交易日),serve_candidate 内部
        # 再转成 target=next_trading_day(as-of)。seq_d 记的是最后服务过的 target,
        # 故"还没服务过的 as-of 日" ⟺ next(A) > seq_d ⟺ A >= seq_d。
        pending = [target] if target else [d for d in raw if d >= seq_d]
        for tgt in pending:
            if tgt > raw_last:                 # 双保险：as-of 日绝不越过 raw_last
                log.warning(f"[SHADOW_RNN] 跳过越界 as-of {tgt} > raw_last {raw_last}")
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
