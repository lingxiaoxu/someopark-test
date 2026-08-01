"""
replication_trading — GKMSZ 交易实验跑批器(Plan §三 G9 全部: 记账协议/图5/图7/表3)
====================================================================================
记账协议(论文 §6.1,式 11-15;Plan G9 第一条):
- 份额演化   x⁰_{i,t} = x_{i,t-1} · R^raw_{i,t}(权重按**原始收益**漂移;AUM 记账值
  固定——账面值惯例,日收益不复利,年化=日均×252)
- 当日执行   z_{i,t}∈[0,1]: x_{i,t} = x⁰_{i,t} + z_{i,t}·(x*_{i,t} − x⁰_{i,t})
- payoff(式13) r_implemented,t = Σ_i x_{i,t}·r_{i,t+1} − Σ_i cost_{i,t}/AUM,
  cost_{i,t} = 0.1·traded²/V = ½·λ(v)·traded²(λ=0.2/V;
  与 econ/policy.impact_cost_dollars(coef=0.1) 完全同式)
- 换手率(式15) turnover_t = Σ_i |x_{i,t} − x⁰_{i,t}|(权重空间,**不随 AUM 变**——单测断言)
- 动态耦合: 今日 x_t 即明日份额演化之基,逐日如实模拟;缺席标的(退市/停牌,V 或 v̂ 缺失)
  z=0 不可交易、payoff 记 0、权重冻结(退市清算细节属论文 §7 未做声明范围)
- 执行率 z = s(v̂;μ)(models/econ.s_opt 闭式解 μ/(μ+λ(v̂))),v̂ 档位:
    ma5    v̂ = ma5_v(0% 端基线)
    model  v̂ = η̂ + ma5_v(η̂ 读 outputs/replication/eta_pred_*.parquet 存档预测)
    oracle v̂ = v_true(完美量预见上界)
  **声明**: 当前 outputs/replication 无存档 η̂ → 按任务协议以 ma5 与 oracle 两档执行并在
  trading_summary.json 留档声明(model 档接口保留,存档一旦出现自动启用;
  "三改进源分解 all>tech>ma5" 依赖各因子组存档预测,同此声明挂起)。

实验一(§6.2 先知信号,图 5): 测试期(paper split 2022-23)每票每日 1% 概率获得
完美 5 日方向预知(sign of Σ r_{t+1..t+5});有效期 5 日、新信号覆盖旧信号;
多/空两组各 50% AUM 组内等权;μ 沿 config.econ.mu_grid 逐档 →
年化收益 & 夏普 vs 换手率曲线(fig5_oracle_signal.png + fig5_data.csv);
z=1 零成本即"税前夏普~7"型理想化基线(summary 记 gross 基线)。

实验二(§6.3 因子动物园,图 7): 风格因子逐个做 50 分位掩码等权多空、月初调仓,
AUM=1e10(多空各 50 亿),全因子统一 μ(默认 mu_from_aum(1e10));
对比"预测量执行 vs ma5 执行"的净年化增量
(fig7_factor_zoo.png + factor_zoo_c3.csv / factor_zoo_c4.csv;
验收: 增量普遍为正、高换手因子增益更大)。
**声明**: 面板 fund2_* 中可横截面排序的风格因子仅 4 列(fund2_fred_*=宏观序列、
fund2_ind_*=行业哑元,均不可作横截面 50 分位)→ 按 G3 fund2 覆盖折让的同一精神
扩入 fund1_*/tech_* 横截面特征凑足 ≥10,实际清单留档 summary。

表 3 同构表(table3_trading.csv): AUM∈config.econ.aum_scenarios × 各法(ma5/model/
oracle/gross_z1)税前年化收益与夏普矩阵;μ 在训练期(2019-21)同协议模拟中调优
(argmax 净年化,信号流独立种子)→ OOS(2022-23)应用(论文"μ 训练集调优→OOS"协议);
与论文口径差异注明于 caveat 列(税前、仅二次冲击成本无 bid-ask/费用、我方 R3K 代理
面板 2022-23、oracle 档代模型档)。

用法:
  conda run -n someopark_run python -m VolumePrediction.replication_trading \
      --panel paper_full_v4 [--quick] [--exp 1,2,3] [--seed 7] \
      [--fig5-aum 1e9] [--mu-zoo 1e-9] [--tickers-cap 600]
  --quick: 缩样本(按在场天数取前 tickers_cap 只)+减 μ 档(4 档)+因子截前 10,冒烟用
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, load_config, get_logger
from VolumePrediction.replication_g import load_panel
from VolumePrediction.econ.policy import IMPACT_COEF, mu_from_aum
from VolumePrediction.models.econ import s_opt

log = get_logger("replication_trading")
REP_DIR = OUT / "replication"

TRADING_DAYS = 252
SIGNAL_P = 0.01            # §6.2: 每票每日 1% 概率获得先知信号
SIGNAL_HORIZON = 5         # 完美预知 5 日方向,持有 5 日
QUICK_MU_GRID = [1e-9, 1e-7, 1e-5, 1e-3]   # quick: 保留全跨度以现 U 形
QUICK_TICKERS_CAP = 600
QUICK_ZOO_FACTORS = 10
ZOO_MIN_NAMES = 10         # 单边可排序最少票数,低于则沿用上期目标

CAVEAT = ("pre-tax; quadratic impact cost only (no bid-ask/fees/borrow); "
          "our R3K-proxy panel (paper split OOS 2022-23); "
          "oracle tier substitutes for model tier when no stored eta predictions")


# ═══════════════════ 面板 → (T,N) 数组 ═══════════════════

def prep_arrays(panel: pd.DataFrame, tickers_cap: Optional[int] = None) -> Dict:
    """(date,ticker) 面板 → 同一 ticker 轴的 (T,N) float 数组字典。

    present := ret/V 同时有效(可入信号与目标);v̂ 缺失由 simulate 的 z=0 兜底。
    tickers_cap: 按在场天数取前 N 只(--quick 缩样本)。
    """
    wide = {c: panel[c].unstack("ticker").sort_index() for c in ("ret", "v", "ma5_v", "V")}
    if tickers_cap is not None:
        keep = wide["ret"].notna().sum().sort_values(ascending=False).index[:tickers_cap]
        keep = sorted(keep)
        wide = {c: w[keep] for c, w in wide.items()}
    dates = wide["ret"].index
    tickers = list(wide["ret"].columns)
    arrs = {c: w.to_numpy(dtype=float) for c, w in wide.items()}
    present = (np.isfinite(arrs["ret"]) & np.isfinite(arrs["V"]) & (arrs["V"] > 0))
    return {"dates": dates, "tickers": tickers, "present": present, **arrs}


def slice_period(arrs: Dict, start: str, end: str) -> Dict:
    m = (arrs["dates"] >= start) & (arrs["dates"] <= end)
    out = {"dates": arrs["dates"][m], "tickers": arrs["tickers"]}
    for k in ("ret", "v", "ma5_v", "V", "present"):
        out[k] = arrs[k][np.asarray(m)]
    return out


def find_model_predictions(dates, tickers, rep_dir: Path = REP_DIR
                           ) -> Optional[np.ndarray]:
    """存档模型预测(约定 eta_pred_*.parquet,index=(date,ticker),首列=η̂)→
    v̂=η̂+ma5 由调用侧合成;无存档 → None(声明由调用侧落 summary)。"""
    cands = (sorted(rep_dir.glob("eta_pred_*.parquet"))
             + sorted(rep_dir.glob("pred_eta_*.parquet")))
    if not cands:
        return None
    df = pd.read_parquet(cands[-1])
    eta = df.iloc[:, 0].unstack("ticker").reindex(index=dates, columns=tickers)
    log.info(f"model tier: loaded stored predictions {cands[-1].name}")
    return eta.to_numpy(dtype=float)


# ═══════════════════ 记账协议模拟核(式 11-15) ═══════════════════

def simulate(ret: np.ndarray, V: np.ndarray, target: np.ndarray,
             aum: float, mu: Optional[float] = None,
             vhat: Optional[np.ndarray] = None,
             z_override: Optional[float] = None,
             impact_coef: float = IMPACT_COEF,
             return_paths: bool = False) -> Dict[str, np.ndarray]:
    """逐日推进记账协议(动态耦合如实模拟;向量化按日批处理全截面)。

    ret/V/target/vhat: (T,N);模拟 T-1 步(末日无 t+1 收益不入账)。
    z 来源: z_override(标量,直给)或 s(v̂;μ) 闭式解;不可交易(V 缺失/≤0
    或 v̂ 缺失)处强制 z=0。cost=impact_coef·traded²/V(美元),入账除以 AUM。
    返回逐日序列: r_gross / cost_ret / r_net / turnover(+可选 x/x0 全路径)。
    """
    T, N = ret.shape
    ret_fill = np.nan_to_num(ret, nan=0.0)
    tgt_fill = np.nan_to_num(target, nan=0.0)
    tradable = np.isfinite(V) & (V > 0)
    if z_override is None:
        if vhat is None or mu is None:
            raise ValueError("need (vhat, mu) or z_override")
        with np.errstate(over="ignore", invalid="ignore"):
            z_all = s_opt(vhat, mu)
        z_all = np.where(np.isfinite(z_all) & tradable, z_all, 0.0)
    else:
        z_all = None

    x = np.zeros(N)
    n_steps = T - 1
    r_gross = np.empty(n_steps)
    cost_ret = np.empty(n_steps)
    turnover = np.empty(n_steps)
    x_path = np.empty((n_steps, N)) if return_paths else None
    x0_path = np.empty((n_steps, N)) if return_paths else None

    for t in range(n_steps):
        x0 = x * (1.0 + ret_fill[t])                      # 份额演化 x⁰_t=x_{t-1}·R^raw_t
        z = (np.where(tradable[t], z_override, 0.0)
             if z_override is not None else z_all[t])
        x = x0 + z * (tgt_fill[t] - x0)                   # 当日执行
        dx = x - x0
        traded = np.abs(dx) * aum
        Vt = V[t]
        c = np.zeros(N)
        m = tradable[t] & (traded > 0)
        c[m] = impact_coef * traded[m] * traded[m] / Vt[m]  # =½λ(v)·traded²
        r_gross[t] = float(np.nansum(x * ret[t + 1]))     # 式13 第一项(r_{t+1})
        cost_ret[t] = float(c.sum()) / aum
        turnover[t] = float(np.abs(dx).sum())             # 式15(权重空间,AUM 无关)
        if return_paths:
            x_path[t] = x
            x0_path[t] = x0

    out = {"r_gross": r_gross, "cost_ret": cost_ret,
           "r_net": r_gross - cost_ret, "turnover": turnover}
    if return_paths:
        out["x_path"], out["x0_path"] = x_path, x0_path
    return out


def perf_stats(sim: Dict[str, np.ndarray]) -> Dict[str, float]:
    """账面值惯例: 年化=日均×252(不复利);夏普=日均/日σ·√252。"""
    def _ann(r):
        r = np.asarray(r, dtype=float)
        mu_d = float(np.mean(r))
        sd = float(np.std(r, ddof=1)) if len(r) > 1 else float("nan")
        sh = mu_d / sd * math.sqrt(TRADING_DAYS) if sd and sd > 0 else float("nan")
        return mu_d * TRADING_DAYS * 100.0, sh
    ag, shg = _ann(sim["r_gross"])
    an, shn = _ann(sim["r_net"])
    return {"annret_gross_pct": round(ag, 3), "sharpe_gross": round(shg, 3),
            "annret_net_pct": round(an, 3), "sharpe_net": round(shn, 3),
            "cost_drag_pct": round(float(np.mean(sim["cost_ret"])) * TRADING_DAYS * 100, 3),
            "turnover_ann": round(float(np.mean(sim["turnover"])) * TRADING_DAYS, 3)}


# ═══════════════════ 实验一: 先知信号目标(§6.2) ═══════════════════

def oracle_signal_targets(ret: np.ndarray, present: np.ndarray,
                          p: float = SIGNAL_P, horizon: int = SIGNAL_HORIZON,
                          seed: int = 7) -> Tuple[np.ndarray, Dict]:
    """每票每日概率 p 获得完美 horizon 日方向预知 → 目标权重 ±等权归一。

    方向 = sign(Σ_{k=1..h} log R_{t+k})(与持有期 payoff r_{t+1..t+h} 精确对齐);
    信号有效 horizon 日,新信号覆盖旧信号;多/空组各 50% AUM 组内等权。
    """
    T, N = ret.shape
    rng = np.random.default_rng(seed)
    starts = (rng.random((T, N)) < p) & present
    logret = np.log1p(np.nan_to_num(ret, nan=0.0))
    S = np.vstack([np.zeros((1, N)), np.cumsum(logret, axis=0)])   # S[t]=Σ rows<t
    idx = np.arange(T)
    fwd = S[np.minimum(idx + 1 + horizon, T)] - S[idx + 1]          # Σ_{t+1..t+h}
    dirs = np.sign(fwd)
    D = np.where(starts & (dirs != 0), dirs, np.nan)
    active = pd.DataFrame(D).ffill(limit=horizon - 1).to_numpy()
    active = np.where(present, active, np.nan)
    longs = active > 0
    shorts = active < 0
    nl = longs.sum(axis=1)
    ns = shorts.sum(axis=1)
    wl = np.divide(0.5, nl, out=np.zeros(T), where=nl > 0)
    ws = np.divide(0.5, ns, out=np.zeros(T), where=ns > 0)
    tgt = longs * wl[:, None] - shorts * ws[:, None]
    info = {"n_signal_starts": int(starts.sum()),
            "avg_active_names": round(float((longs | shorts).sum(axis=1).mean()), 1)}
    return tgt, info


def run_experiment1(sl: Dict, tiers: Dict[str, np.ndarray], mu_grid: Sequence[float],
                    aum: float, seed: int, out_dir: Path,
                    signal_p: float = SIGNAL_P, horizon: int = SIGNAL_HORIZON) -> Dict:
    """图 5 复刻: 逐 μ 逐档模拟 → 年化收益/夏普 vs 换手率曲线 + 数据 CSV。"""
    tgt, sig_info = oracle_signal_targets(sl["ret"], sl["present"],
                                          p=signal_p, horizon=horizon, seed=seed)
    gross = perf_stats(simulate(sl["ret"], sl["V"], tgt, aum=aum, z_override=1.0))
    log.info(f"exp1 signals: {sig_info} | z=1 idealized gross "
             f"annret={gross['annret_gross_pct']}% sharpe={gross['sharpe_gross']}")
    rows: List[dict] = []
    for tier, vhat in tiers.items():
        for mu in mu_grid:
            st = perf_stats(simulate(sl["ret"], sl["V"], tgt, aum=aum, mu=mu, vhat=vhat))
            rows.append({"tier": tier, "mu": mu, "aum": aum, **st})
            log.info(f"exp1 {tier} mu={mu:g}: net={st['annret_net_pct']}% "
                     f"sharpe={st['sharpe_net']} turn={st['turnover_ann']}")
    df = pd.DataFrame(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "fig5_data.csv", index=False)
    _plot_fig5(df, gross, aum, out_dir / "fig5_oracle_signal.png")
    return {"signal": sig_info, "gross_z1_baseline": gross, "rows": rows}


def _plot_fig5(df: pd.DataFrame, gross: Dict, aum: float, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
    for tier, sub in df.groupby("tier"):
        sub = sub.sort_values("turnover_ann")
        axes[0].plot(sub["turnover_ann"], sub["annret_net_pct"], "o-", label=tier)
        axes[1].plot(sub["turnover_ann"], sub["sharpe_net"], "o-", label=tier)
        for _, r in sub.iterrows():
            axes[0].annotate(f"1e{int(round(math.log10(r['mu'])))}",
                             (r["turnover_ann"], r["annret_net_pct"]),
                             fontsize=7, alpha=0.7)
    axes[0].axhline(gross["annret_gross_pct"], ls="--", c="gray", lw=0.8,
                    label="z=1 gross (no cost)")
    axes[1].axhline(gross["sharpe_gross"], ls="--", c="gray", lw=0.8)
    axes[0].set_xlabel("annualized turnover (×AUM/yr)")
    axes[0].set_ylabel("net annualized return (%)")
    axes[1].set_xlabel("annualized turnover (×AUM/yr)")
    axes[1].set_ylabel("net Sharpe")
    axes[0].set_title(f"Fig.5 replica — oracle-signal, AUM={aum:.0e} (μ swept)")
    axes[1].set_title("Sharpe vs turnover")
    axes[0].legend(fontsize=8)
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ═══════════════════ 表 3 同构表(μ 训练期调优 → OOS) ═══════════════════

def run_table3(sl_tr: Dict, sl_te: Dict, tiers_tr: Dict, tiers_te: Dict,
               mu_grid: Sequence[float], aum_scenarios: Sequence[float],
               seed: int, out_dir: Path,
               signal_p: float = SIGNAL_P, horizon: int = SIGNAL_HORIZON) -> Dict:
    tgt_tr, _ = oracle_signal_targets(sl_tr["ret"], sl_tr["present"],
                                      p=signal_p, horizon=horizon, seed=seed + 1000)
    tgt_te, _ = oracle_signal_targets(sl_te["ret"], sl_te["present"],
                                      p=signal_p, horizon=horizon, seed=seed)
    recs: List[dict] = []
    for tier in tiers_te:
        for aum in aum_scenarios:
            best_mu, best_ret = None, -np.inf
            for mu in mu_grid:
                st = perf_stats(simulate(sl_tr["ret"], sl_tr["V"], tgt_tr,
                                         aum=aum, mu=mu, vhat=tiers_tr[tier]))
                if st["annret_net_pct"] > best_ret:
                    best_mu, best_ret = mu, st["annret_net_pct"]
            st = perf_stats(simulate(sl_te["ret"], sl_te["V"], tgt_te,
                                     aum=aum, mu=best_mu, vhat=tiers_te[tier]))
            recs.append({"method": tier, "aum": aum, "mu_star": best_mu,
                         "train_annret_net_pct": best_ret, **st})
            log.info(f"table3 {tier} aum={aum:.0e}: mu*={best_mu:g} "
                     f"OOS net={st['annret_net_pct']}% sharpe={st['sharpe_net']}")
    gz = perf_stats(simulate(sl_te["ret"], sl_te["V"], tgt_te,
                             aum=aum_scenarios[0], z_override=1.0))
    wide_rows = []
    for tier in tiers_te:
        row = {"method": tier}
        for r in [r for r in recs if r["method"] == tier]:
            a = f"{r['aum']:.0e}"
            row[f"annret_net_pct_aum{a}"] = r["annret_net_pct"]
            row[f"sharpe_net_aum{a}"] = r["sharpe_net"]
            row[f"mu_star_aum{a}"] = r["mu_star"]
            row[f"turnover_ann_aum{a}"] = r["turnover_ann"]
        row["caveat"] = CAVEAT
        wide_rows.append(row)
    grow = {"method": "gross_z1_nocost"}
    for aum in aum_scenarios:
        a = f"{aum:.0e}"
        grow[f"annret_net_pct_aum{a}"] = gz["annret_gross_pct"]
        grow[f"sharpe_net_aum{a}"] = gz["sharpe_gross"]
        grow[f"mu_star_aum{a}"] = float("nan")
        grow[f"turnover_ann_aum{a}"] = gz["turnover_ann"]
    grow["caveat"] = "idealized: z=1 immediate rebalance, zero cost (paper's pre-tax ~7-Sharpe benchmark row)"
    wide_rows.append(grow)
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(wide_rows).to_csv(out_dir / "table3_trading.csv", index=False)
    return {"records": recs, "gross_z1": gz}


# ═══════════════════ 实验二: 因子动物园(§6.3) ═══════════════════

def zoo_factor_columns(panel: pd.DataFrame) -> Tuple[List[str], List[str]]:
    """横截面可排序风格因子清单 + 声明(fund2 仅 4 列 → 扩入 fund1/tech,G3 折让同源)。"""
    f2 = [c for c in panel.columns if c.startswith("fund2_")
          and not c.startswith(("fund2_ind_", "fund2_fred_"))]
    f1 = [c for c in panel.columns if c.startswith("fund1_")]
    tech = [c for c in panel.columns if c.startswith("tech_")]
    decls = []
    if len(f2) < 10:
        decls.append(
            f"panel has only {len(f2)} cross-sectional fund2 style columns "
            f"({f2}); fund2_fred_*=macro / fund2_ind_*=dummies are not rankable "
            f"cross-sectionally -> zoo augmented with fund1_*/tech_* characteristics "
            f"(same spirit as the documented G3 fund2 coverage concession)")
    return f2 + f1 + tech, decls


def build_monthly_targets(sl: Dict, factor_wide: pd.DataFrame,
                          min_names: int = ZOO_MIN_NAMES) -> Tuple[np.ndarray, Dict]:
    """50 分位掩码等权多空、月初调仓的目标权重路径(多 0.5/空 −0.5)。

    factor_wide: 行=调仓日(测试期各月首个交易日),列=sl['tickers'] 对齐。
    高因子值一半做多、低一半做空;可排序票数 < min_names 时沿用上期目标。
    """
    dates = sl["dates"]
    T, N = sl["ret"].shape
    month = pd.DatetimeIndex(dates).to_period("M")
    rb_idx = [0] + [i for i in range(1, T) if month[i] != month[i - 1]]
    tgt = np.zeros((T, N))
    prev = np.zeros(N)
    n_rebalanced = 0
    for k, ridx in enumerate(rb_idx):
        vals = factor_wide.iloc[k].to_numpy(dtype=float)
        elig = sl["present"][ridx] & np.isfinite(vals)
        ne = int(elig.sum())
        if ne >= max(min_names, 2):                        # 至少 1 多 + 1 空
            order = np.argsort(vals[elig])                 # 升序
            ids = np.flatnonzero(elig)[order]
            n_short = ne // 2
            w = np.zeros(N)
            w[ids[:n_short]] = -0.5 / n_short
            w[ids[n_short:]] = 0.5 / (ne - n_short)
            prev = w
            n_rebalanced += 1
        end = rb_idx[k + 1] if k + 1 < len(rb_idx) else T
        tgt[ridx:end] = prev
    return tgt, {"n_rebalance_dates": len(rb_idx), "n_effective": n_rebalanced}


def run_factor_zoo(sl: Dict, panel: pd.DataFrame, zoo_cols: List[str],
                   tiers: Dict[str, np.ndarray], mu: float, aum: float,
                   out_dir: Path, min_names: int = ZOO_MIN_NAMES) -> Dict:
    """图 7 + C.3/C.4 同构 CSV: 逐因子对比 预测量执行 vs ma5 执行 的净年化增量。"""
    if "ma5" not in tiers or len(tiers) < 2:
        raise ValueError("factor zoo needs tier 'ma5' plus a prediction tier")
    pred_tier = "model" if "model" in tiers else [t for t in tiers if t != "ma5"][0]
    dates = sl["dates"]
    T = len(dates)
    month = pd.DatetimeIndex(dates).to_period("M")
    rb_idx = [0] + [i for i in range(1, T) if month[i] != month[i - 1]]
    rb_dates = dates[rb_idx]
    dl = panel.index.get_level_values("date")
    fsub = panel.loc[dl.isin(rb_dates), zoo_cols]

    rows: List[dict] = []
    dropped: List[str] = []
    for col in zoo_cols:
        wide = (fsub[col].unstack("ticker")
                .reindex(index=rb_dates, columns=sl["tickers"]))
        tgt, tinfo = build_monthly_targets(sl, wide, min_names=min_names)
        if tinfo["n_effective"] == 0:
            dropped.append(col)
            continue
        st = {t: perf_stats(simulate(sl["ret"], sl["V"], tgt, aum=aum, mu=mu,
                                     vhat=tiers[t])) for t in ("ma5", pred_tier)}
        rows.append({
            "factor": col, "mu": mu, "aum": aum,
            "turnover_ann_ma5": st["ma5"]["turnover_ann"],
            f"turnover_ann_{pred_tier}": st[pred_tier]["turnover_ann"],
            "annret_net_pct_ma5": st["ma5"]["annret_net_pct"],
            f"annret_net_pct_{pred_tier}": st[pred_tier]["annret_net_pct"],
            "increment_pp": round(st[pred_tier]["annret_net_pct"]
                                  - st["ma5"]["annret_net_pct"], 3),
            "sharpe_net_ma5": st["ma5"]["sharpe_net"],
            f"sharpe_net_{pred_tier}": st[pred_tier]["sharpe_net"],
            "d_sharpe": round(st[pred_tier]["sharpe_net"] - st["ma5"]["sharpe_net"], 3),
            "cost_drag_pct_ma5": st["ma5"]["cost_drag_pct"],
            f"cost_drag_pct_{pred_tier}": st[pred_tier]["cost_drag_pct"],
            # 纯执行成本通道(剔除"更快跟踪→更多因子自身收益暴露"的 gross 通道):
            "cost_saving_pp": round(st["ma5"]["cost_drag_pct"]
                                    - st[pred_tier]["cost_drag_pct"], 3),
        })
        log.info(f"zoo {col}: incr={rows[-1]['increment_pp']}pp "
                 f"turn(ma5)={rows[-1]['turnover_ann_ma5']}")
    if dropped:
        log.warning(f"zoo dropped (no rankable cross-section): {dropped}")
    c3_cols = ["factor", "mu", "aum", "turnover_ann_ma5", f"turnover_ann_{pred_tier}",
               "annret_net_pct_ma5", f"annret_net_pct_{pred_tier}", "increment_pp",
               "cost_drag_pct_ma5", f"cost_drag_pct_{pred_tier}", "cost_saving_pp"]
    c4_cols = ["factor", "mu", "aum", "turnover_ann_ma5",
               "sharpe_net_ma5", f"sharpe_net_{pred_tier}", "d_sharpe"]
    df = pd.DataFrame(rows, columns=None if rows else c3_cols + c4_cols[4:])
    out_dir.mkdir(parents=True, exist_ok=True)
    c3 = df[c3_cols] if len(df) else pd.DataFrame(columns=c3_cols)
    c4 = df[c4_cols] if len(df) else pd.DataFrame(columns=c4_cols)
    c3.to_csv(out_dir / "factor_zoo_c3.csv", index=False)
    c4.to_csv(out_dir / "factor_zoo_c4.csv", index=False)
    def _frac_pos(col):
        return round(float((df[col] > 0).mean()), 3) if len(df) else float("nan")

    def _corr(col):
        if len(df) <= 2:
            return float("nan")
        return round(float(np.corrcoef(df["turnover_ann_ma5"], df[col])[0, 1]), 3)

    _plot_fig7(df, pred_tier, mu, aum, out_dir / "fig7_factor_zoo.png")
    accept = {"n_factors": len(df), "dropped": dropped, "pred_tier": pred_tier,
              # 净增量通道(含 gross 暴露差): 因子自身 2022-23 正负会翻转其符号
              "frac_positive_increment": _frac_pos("increment_pp"),
              "corr_increment_vs_turnover": _corr("increment_pp"),
              # 纯执行成本通道(论文"增量普遍为正"的稳健读法): 应 ≈1.0
              "frac_positive_cost_saving": _frac_pos("cost_saving_pp"),
              "corr_cost_saving_vs_turnover": _corr("cost_saving_pp")}
    log.info(f"zoo acceptance: {accept}")
    return {"rows": rows, "acceptance": accept}


def _plot_fig7(df: pd.DataFrame, pred_tier: str, mu: float, aum: float,
               path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7.5, 5))
    if len(df):
        colors = np.where(df["increment_pp"] > 0, "tab:green", "tab:red")
        ax.scatter(df["turnover_ann_ma5"], df["increment_pp"], c=colors, s=28)
        for _, r in df.iterrows():
            ax.annotate(r["factor"].replace("fund2_", "").replace("fund1_", "")
                        .replace("tech_", ""),
                        (r["turnover_ann_ma5"], r["increment_pp"]),
                        fontsize=6.5, alpha=0.75)
    ax.axhline(0, c="k", lw=0.7)
    ax.set_xlabel("annualized turnover, ma5 execution (×AUM/yr)")
    ax.set_ylabel(f"net annual return increment vs ma5 ({pred_tier} tier, pp/yr)")
    ax.set_title(f"Fig.7 replica — factor zoo, AUM={aum:.0e}, μ={mu:g}")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ═══════════════════ 主入口 ═══════════════════

def run(panel_tag: str = "latest", quick: bool = False, seed: int = 7,
        exps: Sequence[str] = ("1", "2", "3"), fig5_aum: float = 1e9,
        mu_zoo: Optional[float] = None, tickers_cap: Optional[int] = None,
        panel: Optional[pd.DataFrame] = None, out_dir: Optional[Path] = None,
        zoo_min_names: int = ZOO_MIN_NAMES) -> dict:
    out_dir = Path(out_dir) if out_dir else REP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    mu_grid = [float(m) for m in cfg["econ"]["mu_grid"]]
    aum_scenarios = [float(a) for a in cfg["econ"]["aum_scenarios"]]
    if quick:
        mu_grid = [m for m in QUICK_MU_GRID if m in mu_grid] or QUICK_MU_GRID
        tickers_cap = tickers_cap or QUICK_TICKERS_CAP

    t0 = time.time()
    if panel is None:
        panel = load_panel(panel_tag)
    arrs = prep_arrays(panel, tickers_cap=tickers_cap)
    sp = cfg["split"]["paper"]
    sl_tr = slice_period(arrs, *sp["train"])
    sl_te = slice_period(arrs, *sp["test"])
    log.info(f"trading prep: {len(arrs['tickers'])} tickers | "
             f"train {len(sl_tr['dates'])}d test {len(sl_te['dates'])}d "
             f"({time.time()-t0:.0f}s) quick={quick}")

    declarations: List[str] = []
    eta_all = find_model_predictions(arrs["dates"], arrs["tickers"], out_dir)

    def tiers_for(sl, offset_mask):
        tiers = {"ma5": sl["ma5_v"]}
        if eta_all is not None:
            tiers["model"] = eta_all[np.asarray(offset_mask)] + sl["ma5_v"]
        tiers["oracle"] = sl["v"]
        return tiers

    m_tr = (arrs["dates"] >= sp["train"][0]) & (arrs["dates"] <= sp["train"][1])
    m_te = (arrs["dates"] >= sp["test"][0]) & (arrs["dates"] <= sp["test"][1])
    tiers_tr, tiers_te = tiers_for(sl_tr, m_tr), tiers_for(sl_te, m_te)
    if eta_all is None:
        declarations.append(
            "no stored eta predictions under outputs/replication (eta_pred_*.parquet) "
            "-> per task protocol running ma5 + oracle(v_true) tiers only; model tier "
            "and the all>tech>ma5 improvement decomposition auto-enable once "
            "predictions are archived")
        log.warning(declarations[-1])

    summary: dict = {"panel_tag": panel_tag, "quick": quick, "seed": seed,
                     "mu_grid": mu_grid, "aum_scenarios": aum_scenarios,
                     "tiers": list(tiers_te), "caveat": CAVEAT}

    if "1" in exps:
        summary["experiment1_fig5"] = run_experiment1(
            sl_te, tiers_te, mu_grid, aum=fig5_aum, seed=seed, out_dir=out_dir)
    if "3" in exps:
        summary["table3"] = run_table3(
            sl_tr, sl_te, tiers_tr, tiers_te, mu_grid, aum_scenarios,
            seed=seed, out_dir=out_dir)
    if "2" in exps:
        zoo_cols, zdecls = zoo_factor_columns(panel)
        declarations.extend(zdecls)
        if quick:
            zoo_cols = zoo_cols[:QUICK_ZOO_FACTORS]
        aum_zoo = 1e10
        mu_z = mu_zoo if mu_zoo is not None else float(
            f"{mu_from_aum(aum_zoo, aum_scenarios, mu_grid):.3g}")
        summary["experiment2_zoo"] = run_factor_zoo(
            sl_te, panel, zoo_cols, tiers_te, mu=mu_z, aum=aum_zoo,
            out_dir=out_dir, min_names=zoo_min_names)
        summary["experiment2_zoo"]["mu_zoo"] = mu_z
        summary["experiment2_zoo"]["factors"] = zoo_cols

    summary["declarations"] = declarations
    (out_dir / "trading_summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, default=str))
    log.info(f"G9 trading artifacts → {out_dir} ({time.time()-t0:.0f}s total)")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="G9 trading experiments (fig5/fig7/table3)")
    ap.add_argument("--panel", default="latest")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--exp", default="1,2,3", help="subset of 1,2,3 (3=table3)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--fig5-aum", type=float, default=1e9)
    ap.add_argument("--mu-zoo", type=float, default=None)
    ap.add_argument("--tickers-cap", type=int, default=None)
    a = ap.parse_args()
    res = run(a.panel, quick=a.quick, seed=a.seed, exps=tuple(a.exp.split(",")),
              fig5_aum=a.fig5_aum, mu_zoo=a.mu_zoo, tickers_cap=a.tickers_cap)
    brief = {"tiers": res["tiers"], "declarations": res["declarations"]}
    if "experiment1_fig5" in res:
        brief["exp1_gross_z1"] = res["experiment1_fig5"]["gross_z1_baseline"]
    if "experiment2_zoo" in res:
        brief["zoo_acceptance"] = res["experiment2_zoo"]["acceptance"]
    print(json.dumps(brief, ensure_ascii=False, default=str))
