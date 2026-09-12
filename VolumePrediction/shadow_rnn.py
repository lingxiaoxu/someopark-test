"""shadow_rnn — 版本隔离的 RNN 候选与实际现役模型每日影子 AB。

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
- 只读现役工件与 raw;非现役候选只滚自己的序列并写独立影子目录,不碰生产指针
- 任何失败大声 log 并返回非零,绝不静默(影子数据缺口会污染 AB 判决)
- seq_tail 断档时 serve 会抛错 —— 这是有意的,宁可缺一天也不出错位预测

用法: python -m VolumePrediction.shadow_rnn [--version NEW_VERSION]
      [--target YYYY-MM-DD] [--no-roll]
默认跟随正式 blend 版本且不滚动它的序列;显式版本使用 candidates/<version>/。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO

log = logging.getLogger("VolumePrediction.shadow_rnn")

OUT = REPO / "VolumePrediction" / "outputs"
SHADOW_DIR = OUT / "shadow_rnn"
TRACK_CSV = SHADOW_DIR / "rnn_ab_tracking.csv"
CANDIDATE = OUT / "registry" / "artifacts" / "rnn_v6f32n_20260731"


@dataclass(frozen=True)
class ShadowContext:
    version: str
    art: Path
    directory: Path
    track: Path
    service: object
    serving: bool


def _context(*, version=None, shadow_dir=None, service=None,
             isolated=None) -> ShadowContext:
    """Resolve per-call paths; explicit candidates never share published predictions."""
    if service is None:
        from VolumePrediction.service import VolumeService
        service = VolumeService()
    blend = service.registry.load().get("blend") or {}
    explicit = version is not None
    version = version or blend.get("rnn_version") or CANDIDATE.name
    if not isinstance(version, str) or not version or Path(version).name != version \
            or version in (".", ".."):
        raise ValueError("RNN version must be a single directory name")
    root = Path(service.art)
    formal = root / "shadow_rnn"
    isolate = explicit if isolated is None else bool(isolated)
    serving = bool(blend.get("enabled") and blend.get("rnn_version") == version)
    directory = Path(shadow_dir) if shadow_dir is not None else (
        formal / "candidates" / version if isolate else formal)
    if isolate and directory.resolve() == formal.resolve():
        raise ValueError("isolated candidate cannot write the formal shadow directory")
    if explicit and not serving and directory.resolve() == formal.resolve():
        raise ValueError("non-serving candidate cannot write the formal shadow directory")
    return ShadowContext(version, root / "registry" / "artifacts" / version,
                         directory, directory / "rnn_ab_tracking.csv", service,
                         serving)


def _validate_prediction(frame: pd.DataFrame, version: str, asof: str) -> None:
    """Do not consume another model's same-day file or a misdated forecast."""
    if frame.empty or "model_version" not in frame or \
            set(frame["model_version"].astype(str)) != {version}:
        raise ValueError(f"RNN prediction version mismatch: expected {version}")
    if "date" not in frame or set(frame["date"].astype(str)) != {asof}:
        raise ValueError(f"RNN prediction date mismatch: expected {asof}")
    if "ticker" not in frame or frame["ticker"].duplicated().any():
        raise ValueError("RNN prediction ticker keys are missing or duplicated")


def _write_prediction(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


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


def _held_tickers(pred_date: str, *, artifacts_dir=None) -> set:
    """消费子集 = 五策略当日真实持仓/在场票。
    取 pred_date 当日的 adapters advice 文件(PIT 正确: advice 与预测同日产出);
    当日缺某策略的文件时回退该策略最近一份 ≤ pred_date 的。"""
    held: set = set()
    adir = Path(artifacts_dir or OUT) / "adapters"
    for stem in ("pairs_mrpt_advice", "pairs_mtfs_advice",
                 "aiss_advice", "aeus_advice", "ssrs_advice"):
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
        for e in (d.get("etfs") or []):
            if (e.get("shares") or 0) > 0:
                held.add(e.get("etf") or e.get("ticker"))
    held.discard(None)
    held.discard("")
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


def serve_candidate(asof: str, roll: bool = True, *, version=None,
                    shadow_dir=None, service=None, isolated=None,
                    context: ShadowContext | None = None) -> pd.DataFrame:
    """asof 收盘后跑候选 RNN 的真实服务路径并落盘。

    两套日期不能混(2026-08-26 修):
      - **落盘命名 = as-of 日**: rnn_pred_{asof} 存的是"截至 asof 所做的、对下一
        交易日的预测"。evaluate 正是按 stem < actual_date 配对的,改名会错位。
      - **serve 的 target = next_trading_day(asof)**: serve(T) 预测 T 日
        (锚 ma5v_next = T 之前 5 个交易日均值)。原先直接传 asof,预测的是
        今天(已发生),却被当作次日预测评估 —— 整条 AB 线晚一天。
    """
    from VolumePrediction import prod_model_rnn as pmr
    ctx = context or _context(version=version, shadow_dir=shadow_dir,
                              service=service, isolated=isolated)
    tgt = pmr.next_trading_day(asof)
    p = ctx.directory / f"rnn_pred_{asof}.parquet"
    meta = json.loads((ctx.art / "meta.json").read_text())
    if meta.get("version") != ctx.version:
        raise ValueError(f"RNN artifact version mismatch: expected {ctx.version}")
    if ctx.serving:
        # refresh owns this model's sequence, even when output is explicitly isolated.
        candidates = [Path(ctx.service.art) / "shadow_rnn" / p.name,
                      ctx.art / f"blend_serve_{asof}.parquet"]
        source = next((f for f in candidates if f.exists()), None)
        if source is None:
            raise FileNotFoundError(f"formal RNN prediction missing: {ctx.version}/{asof}")
        out = pd.read_parquet(source)
        _validate_prediction(out, ctx.version, asof)
        if p.resolve() != source.resolve():
            _write_prediction(out, p)
        return out
    if meta.get("seq_tail_date", meta["trained_through"]) >= tgt:
        # A completed candidate day is replayed from its own file, never rolled twice.
        if not p.exists():
            raise FileNotFoundError(f"candidate state ahead but prediction missing: {p}")
        out = pd.read_parquet(p)
        _validate_prediction(out, ctx.version, asof)
        return out
    out = pmr.serve(ctx.art, tgt, update_state=roll)
    _validate_prediction(out, ctx.version, asof)
    if "generated_at" not in out:
        raise ValueError("RNN prediction lacks generated_at provenance")
    # Legacy serve timestamps are local ET; make new candidate provenance explicit.
    def timestamp(value):
        t = pd.Timestamp(value)
        if pd.isna(t):
            raise ValueError("RNN prediction generated_at is empty")
        return (t.tz_localize(ZoneInfo("America/New_York"))
                if t.tzinfo is None else t).isoformat()
    out["generated_at"] = out["generated_at"].map(timestamp)
    _write_prediction(out, p)
    log.info(f"[SHADOW_RNN] asof={asof} → target={tgt}: {len(out)} 票预测 → {p.name}")
    return out


def evaluate(actual_date: str, *, version=None, shadow_dir=None, service=None,
             isolated=None, pred_date=None,
             context: ShadowContext | None = None) -> dict | None:
    """滞后口径: 上一交易日的三档预测 vs actual_date 的真实成交额。"""
    ctx = context or _context(version=version, shadow_dir=shadow_dir,
                              service=service, isolated=isolated)
    svc = ctx.service
    day = svc._load_day(actual_date)
    if day is None or day.empty:
        log.error(f"[SHADOW_RNN] {actual_date} 无实际数据 — 跳过评估")
        return None
    actual = day.set_index("ticker")["dollar_volume"]

    # 上一份影子预测(RNN)与同日的生产工件(lgbm/ma5 混合)
    preds = sorted(ctx.directory.glob("rnn_pred_*.parquet"))
    prior = [p for p in preds if p.stem.split("_")[-1] < actual_date
             and (pred_date is None or p.stem.split("_")[-1] == pred_date)]
    if not prior:
        log.warning("[SHADOW_RNN] 无更早的 RNN 预测 — 首日,仅落盘不评估")
        return None
    rnn_p = prior[-1]
    pred_date = rnn_p.stem.split("_")[-1]
    from VolumePrediction.prod_model_rnn import next_trading_day
    if next_trading_day(pred_date) != actual_date:
        log.warning(f"[SHADOW_RNN] no next-session pair: {pred_date} → {actual_date}")
        return None
    rnn = pd.read_parquet(rnn_p)
    _validate_prediction(rnn, ctx.version, pred_date)
    rnn = rnn.set_index("ticker")

    prod_p = Path(svc.art) / "history" / f"volume_forecast_{pred_date}.parquet"
    if not prod_p.exists():
        log.error("[SHADOW_RNN] 无对应日期的生产工件 — 跳过")
        return None
    prod = pd.read_parquet(prod_p).set_index("ticker")

    # ── 对照臂(2026-08-26 修 B)────────────────────────────────────────────
    # prod = 当日**已发布**的工件。blend3 启用后 RNN 层直接覆盖了它,而持仓票
    # 恰好全在 RNN 层里 —— 于是 rnn_* 与 prod_* 在消费子集上逐位相同,
    # rnn_wins 结构性恒 False,自比自,毫无判别力(RNN 从 8/07 起连输 13/17 天
    # 无人察觉,这是两个盲区之一)。
    # 真候选臂:
    #   cf_*  = 反事实"不开 blend"(lgbm+ma5),refresh 在覆盖前存档,零额外算力
    #   ma5_* = 地板(几何 ma5)。RNN 必须打得过地板,否则这一层就是负价值。
    cf_p = Path(svc.art) / "history" / f"counterfactual_noblend_{pred_date}.parquet"
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
    generated = rnn.get("generated_at", pd.Series(dtype=str)).dropna()
    row = {"pred_date": pred_date, "actual_date": actual_date,
           "n_common": int(len(common)), "candidate_version": ctx.version,
           "candidate_trained_through": str(rnn["trained_through"].max())
           if "trained_through" in rnn else None,
           "forecast_generated_at": str(generated.max()) if len(generated) else None,
           "forecast_generated_timezone": "America/New_York",
           "held_sample_scope": "common", "promotion_ref": "incumbent"}
    arms = [("rnn", rnn["pred_V"]), ("prod", prod["pred_V"])]
    if cf is not None:
        arms.append(("cf", cf["pred_V"]))
    ma5f = _ma5_floor(svc, pred_date)
    if ma5f is not None:
        arms.append(("ma5", ma5f))
    held = _held_tickers(pred_date, artifacts_dir=svc.art)
    # Expected held coverage excludes candidate availability, preventing a candidate
    # with missing difficult names from improving its score by shrinking the cohort.
    expected = pd.concat([actual.rename("actual"), prod["pred_V"].rename("prod")]
                         + ([ma5f.rename("ma5")] if ma5f is not None else []), axis=1)
    expected = expected[np.isfinite(expected).all(axis=1) & (expected > 0).all(axis=1)]
    row["n_held_expected"] = len(expected.index.intersection(pd.Index(sorted(held))))
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
    aligned = pd.concat([actual.reindex(common).rename("actual")]
                        + [s.reindex(common).rename(tag) for tag, s in keep], axis=1)
    valid = np.isfinite(aligned).all(axis=1) & (aligned > 0).all(axis=1)
    common = aligned.index[valid]
    if len(common) < 30:
        log.error(f"[SHADOW_RNN] 有限正数共同样本仅 {len(common)} — 跳过")
        return None
    a = actual.loc[common]
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

    # Every retained arm uses these exact same five-strategy held ticker keys.
    hc = [t for t in common if t in held]
    row["n_held"] = len(hc)
    row["n_held_common"] = len(hc)
    for tag, s in ok_arms:
        hm = _metrics(s.loc[hc], a.loc[hc], mu=mu, min_n=20) if hc else \
            {"mape": None, "log_mse": None}
        row[f"{tag}_held_mape"] = hm["mape"]
        row[f"{tag}_held_log_mse"] = hm["log_mse"]

    # 生产档在交集上的模型构成(交集应几乎全是 lgbm 覆盖票)
    if "model_version" in prod.columns:
        vc = prod.loc[common, "model_version"].value_counts()
        row["prod_mix"] = ";".join(f"{k}:{v}" for k, v in vc.items())
        row["incumbent_version"] = ";".join(sorted(str(v) for v in vc.index))
        row["incumbent_mix"] = row["prod_mix"]
    else:
        row["incumbent_version"] = "unknown"

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
    for _key, _tag in (("paired_ref", ref), ("paired_ma5", "ma5"),
                       ("paired_incumbent", "prod")):
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

    # rnn_wins retains its historical cf-first definition. Promotion must also
    # defeat the actually published incumbent, which can already be another RNN.
    for metric in ("held_mape", "held_log_mse", "mape", "log_mse"):
        candidate, incumbent = row.get(f"rnn_{metric}"), row.get(f"prod_{metric}")
        row[f"candidate_{metric}"] = candidate
        row[f"incumbent_{metric}"] = incumbent
        row[f"candidate_beats_incumbent_{metric}"] = (
            bool(candidate < incumbent) if candidate is not None and incumbent is not None
            else None)
    wins = [row[f"candidate_beats_incumbent_{m}"]
            for m in ("held_mape", "held_log_mse")]
    row["candidate_beats_incumbent"] = all(wins) if all(w is not None for w in wins) else None

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


def append_track(row: dict, *, version=None, shadow_dir=None, service=None,
                 isolated=None, context: ShadowContext | None = None) -> None:
    ctx = context or _context(version=version, shadow_dir=shadow_dir,
                              service=service, isolated=isolated)
    if row.get("candidate_version", ctx.version) != ctx.version:
        raise ValueError("tracking row candidate_version does not match its directory")
    ctx.directory.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([row])
    if ctx.track.exists():
        old = pd.read_csv(ctx.track)
        # concat aligns labels, including migrated schemas and reordered columns.
        frame = pd.concat([old, frame], ignore_index=True)
    tmp = ctx.track.with_suffix(".tmp")
    frame.to_csv(tmp, index=False)
    tmp.replace(ctx.track)
    log.info(f"[SHADOW_RNN] 追加 {ctx.track}: {row}")


def _already_tracked(actual_date: str | None, *, version=None, shadow_dir=None,
                     service=None, isolated=None,
                     context: ShadowContext | None = None) -> bool:
    """Idempotency key is (candidate version, actual session), including legacy rows."""
    ctx = context or _context(version=version, shadow_dir=shadow_dir,
                              service=service, isolated=isolated)
    if not actual_date or not ctx.track.exists():
        return False
    frame = pd.read_csv(ctx.track)
    versions = frame.get("candidate_version", pd.Series(CANDIDATE.name, index=frame.index))
    versions = versions.fillna(CANDIDATE.name).astype(str)
    return bool(((frame["actual_date"].astype(str) == actual_date)
                 & (versions == ctx.version)).any())


def run_daily(target: str | None = None, roll: bool = True,
              eval_only: bool = False, rebuild: bool = False, *, version=None,
              shadow_dir=None, service=None, isolated=None) -> int:
    """Run one version without changing globals or the production registry.

    Default: follow registry.blend.rnn_version and use the formal shadow directory.
    Explicit version: use shadow_rnn/candidates/<version>; only a non-serving
    candidate owns its own sequence roll. target is the as-of raw session.
    """
    try:
        ctx = _context(version=version, shadow_dir=shadow_dir, service=service,
                       isolated=isolated)
        meta = json.loads((ctx.art / "meta.json").read_text())
        if meta.get("version") != ctx.version:
            raise ValueError(f"artifact version mismatch: {ctx.version}")
        raw = ctx.service._raw_dates()
        if not raw:
            raise ValueError("no raw trading sessions")
    except Exception as exc:
        log.error(f"[SHADOW_RNN] configuration failed: {exc}")
        return 1
    raw_last = raw[-1]

    if not eval_only:
        # Correct registry accessor is svc.registry; only the actual blend version
        # is readonly. Other candidates must continue advancing their own state.
        if ctx.serving:
            roll = False
            log.info("[SHADOW_RNN] blend serving version is readonly; refresh owns seq_tail")
            pending = [target or raw_last]
        else:
            seq_d = meta.get("seq_tail_date", meta["trained_through"])
            pending = [target] if target else [d for d in raw if d >= seq_d]
        for asof in pending:
            if asof > raw_last or asof not in raw:
                log.error(f"[SHADOW_RNN] as-of session unavailable: {asof}; raw_last={raw_last}")
                return 2
            try:
                serve_candidate(asof, roll=roll, context=ctx)
            except Exception as exc:
                log.error(f"[SHADOW_RNN] serve failed ({ctx.version}, {asof}): {exc}")
                return 2

    if rebuild and ctx.track.exists():
        bak = ctx.track.with_suffix(".csv.bak")
        ctx.track.replace(bak)
        log.info(f"[SHADOW_RNN] --rebuild: old tracking backed up to {bak}")

    n_new, last_row = 0, None
    for path in sorted(ctx.directory.glob("rnn_pred_*.parquet")):
        asof = path.stem.split("_")[-1]
        future = [d for d in raw if d > asof]
        if not future or _already_tracked(future[0], context=ctx):
            continue
        try:
            frame = pd.read_parquet(path)
            # Shared formal history legitimately contains previously deployed models.
            if ctx.directory == Path(ctx.service.art) / "shadow_rnn" and \
                    "model_version" in frame and \
                    set(frame["model_version"].astype(str)) != {ctx.version}:
                continue
            _validate_prediction(frame, ctx.version, asof)
            row = evaluate(future[0], pred_date=asof, context=ctx)
            if row:
                append_track(row, context=ctx)
                last_row, n_new = row, n_new + 1
        except Exception as exc:
            log.error(f"[SHADOW_RNN] evaluation failed ({ctx.version}, {asof}): {exc}")
            return 3
    log.info(f"[SHADOW_RNN] 评估补齐 {n_new} 行 (version={ctx.version}, raw_last={raw_last})")
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
    ap.add_argument("--version", help="独立候选版本;默认跟随正式 blend RNN")
    ap.add_argument("--shadow-dir", type=Path, help="显式覆盖影子输出目录")
    ap.add_argument("--isolated", action=argparse.BooleanOptionalAction, default=None,
                    help="显式版本默认隔离;现役版本也可单独记录,不滚动正式状态")
    a = ap.parse_args()
    return run_daily(target=a.target, roll=not a.no_roll, eval_only=a.eval_only,
                     rebuild=a.rebuild, version=a.version, shadow_dir=a.shadow_dir,
                     isolated=a.isolated)


if __name__ == "__main__":
    sys.exit(main())
