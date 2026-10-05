"""Weekly sizing/timing mathematics for W7 (user, 2026-10-02): the report in
research/sizing_math_20261002.md, regenerated every week from the latest 14-day
scenario tables. Research only - nothing here trades or changes a setting.

    python -m crypto_trading.crypto_strategies.w7_scenarios.weekly_math [--out DIR]

Sections: per-contract mathematics and log-growth g(f); per-bin flip rates,
costs, edges and Kelly; price-band x bin edges with clustered t and week split;
per coin; cross-coin correlation and window VaR; multi-asset Kelly; bootstrap
drawdowns by size; bankroll-based sizes; selection-noise check of the table.
"""
from __future__ import annotations

import argparse
import glob
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from . import derived
from .config import BAND, COINS, OUT
from .tables import COLS

BINS = ["T-11.25", "T-9.75", "T-8.25", "T-6.75", "T-5.25", "T-3.75"]
ALLOWED = ["T-9.75", "T-8.25", "T-6.75", "T-5.25"]
RESEARCH = OUT / "research"
B0 = 200.0


def _cl_se(g: pd.DataFrame) -> float:
    """Window-clustered SE of the mean per-contract pnl ($)."""
    w = g.groupby("close_ts").pnl.sum() / 100
    n = g.groupby("close_ts").size()
    m = g.pnl.mean() / 100
    return float(np.sqrt(((w - n * m) ** 2).sum()) / len(g)) if len(g) else float("nan")


def load() -> tuple[pd.DataFrame, str]:
    run = sorted(glob.glob(str(OUT / "runs" / "*" / "labelled.parquet")))[-1]
    lab = pd.read_parquet(run)
    x = lab[lab.inband].copy()
    x["fee"] = 0.07 * x.cost * (1 - x.cost)
    x["stake"] = x.cost + x.fee
    x["gain"] = 1 - x.stake
    x["r"] = x.pnl / 100 / x.stake
    return x, run.split("/")[-2]


def per_bin(x: pd.DataFrame, group=None) -> pd.DataFrame:
    rows = []
    for b in BINS:
        g = x[x.bin == b] if group is None else x[(x.bin == b) & group]
        if len(g) < 30:
            continue
        p = g.fav_won.mean(); mu = g.pnl.mean() / 100; se = _cl_se(g)
        rows.append(dict(bin=b, n=len(g), p=p, stake=g.stake.mean(), gain_c=100 * g.gain.mean(),
                         mu_c=100 * mu, se_c=100 * se, t=mu / se if se > 0 else np.nan,
                         kelly=g.r.mean() / g.r.var() if g.r.var() > 0 else np.nan,
                         kelly_lo=(g.r.mean() - g.r.std() / np.sqrt(len(g))) / g.r.var() if g.r.var() > 0 else np.nan))
    return pd.DataFrame(rows)


def growth_table() -> list[tuple]:
    out = []
    for label, p, b in (("T-8.25 pooled", 0.8916, 0.1236), ("T-6.75 pooled", 0.9109, 0.1067)):
        fstar = (p * b - (1 - p)) / b
        out.append((label, fstar, [(f, p * np.log(1 + f * b) + (1 - p) * np.log(1 - f)) for f in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.72)]))
    return out


def bands(x: pd.DataFrame) -> list[dict]:
    mid = x.close_ts.median(); rows = []
    for lo, hi in ((BAND[0], 0.86), (0.86, 0.94), (0.94, BAND[1])):
        sel = (x.cost >= lo) & (x.cost < hi + (0.001 if hi == BAND[1] else 0))
        for b in ALLOWED + ["all"]:
            g = x[sel & (x.bin.isin(ALLOWED) if b == "all" else (x.bin == b))]
            if len(g) < 30:
                continue
            m = g.pnl.mean() / 100; se = _cl_se(g)
            rows.append(dict(band=f"{lo:.2f}-{hi:.2f}", bin=b, mu_c=100 * m, t=m / se if se > 0 else np.nan, n=len(g),
                             flip=1 - g.fav_won.mean(), w1=g[g.close_ts < mid].pnl.mean(), w2=g[g.close_ts >= mid].pnl.mean()))
    return rows


def window_risk(x: pd.DataFrame, b: str = "T-8.25") -> dict:
    g = x[x.bin == b]
    piv = g.pivot_table(index="close_ts", columns="coin", values="fav_won").reindex(columns=list(COINS))
    rho = piv.corr().values
    ws = g.groupby("close_ts").agg(k=("fav_won", "size"), lost=("fav_won", lambda s: int((s == 0).sum())),
                                   pnl=("pnl", lambda s: s.sum() / 100), stake=("stake", "sum"))
    p_all5 = ((ws.k == 5) & (ws.lost == 5)).sum() / max((ws.k == 5).sum(), 1)
    p1 = 1 - g.fav_won.mean()
    return dict(rho_mean=float(rho[np.triu_indices(len(COINS), 1)].mean()), rho=pd.DataFrame(rho, index=list(COINS), columns=list(COINS)),
                p_all5=float(p_all5), p_all5_indep=float(p1 ** 5), unit_mean=float(ws.pnl.mean()), unit_sd=float(ws.pnl.std()),
                unit_q01=float(ws.pnl.quantile(.01)), unit_q05=float(ws.pnl.quantile(.05)), unit_min=float(ws.pnl.min()),
                sd_ratio_vs_indep=float(ws.pnl.std() / np.sqrt(ws.k.mean() * (g.pnl / 100).var())))


def multi_kelly(x: pd.DataFrame, b: str) -> dict:
    g = x[x.bin == b]
    C = list(COINS)
    rho = g.pivot_table(index="close_ts", columns="coin", values="fav_won").reindex(columns=C).corr().values
    sd = np.array([g[g.coin == c].r.std() for c in C]); mu = np.array([g[g.coin == c].r.mean() for c in C])
    stake = np.array([g[g.coin == c].stake.mean() for c in C])
    Sig = np.outer(sd, sd) * rho
    f_own = np.linalg.solve(Sig, mu); f_pool = np.linalg.solve(Sig, np.full(len(C), g.r.mean()))
    return dict(f_own=dict(zip(C, f_own)), f_pool=dict(zip(C, f_pool)), stake=dict(zip(C, stake)),
                f_pool_sum=float(np.clip(f_pool, 0, None).sum()))


def bootstrap(x: pd.DataFrame, sizes_grid, days=30, paths=3000, seed=7) -> list[dict]:
    rng = np.random.default_rng(seed)
    g = x[x.bin == "T-8.25"].copy()
    et = pd.to_datetime(g.close_ts, unit="s", utc=True).dt.tz_convert("America/New_York")
    g["day"] = et.dt.date; g["night"] = pd.to_datetime(g.close_ts, unit="s", utc=True).dt.hour.isin(range(6))
    out = []
    for btc, oth in sizes_grid:
        n = np.where(g.night, np.where(g.coin == "BTC", 15, 10) * min(1, btc / 40), np.where(g.coin == "BTC", btc, oth))
        usd = pd.Series(n * g.pnl.values / 100, index=g.index)
        byday = {d: usd[g.day == d].groupby(g.close_ts[g.day == d]).sum().values for d in g.day.unique()}
        keys = list(byday); res = []
        for _ in range(paths):
            seq = np.concatenate([byday[keys[i]] for i in rng.integers(0, len(keys), days)])
            eq = np.r_[B0, B0 + np.cumsum(seq)]
            res.append(((np.maximum.accumulate(eq) - eq).max(), eq.min()))
        res = np.array(res)
        out.append(dict(size=f"{btc}/{oth}", p_dd50=float((res[:, 0] > 0.5 * B0).mean()), p_ruin=float((res[:, 1] < 0.2 * B0).mean()),
                        max_stake=float((n * g.stake).groupby(g.close_ts).sum().max()), worst=float(usd.groupby(g.close_ts).sum().min())))
    return out


def selection_noise() -> dict:
    tj = json.load(open(OUT / "latest" / "tables.json")); dj = json.load(open(OUT / "latest" / "derived.json"))
    emax = {k: float(np.mean(np.max(norm.rvs(size=(100000, k), random_state=1), axis=1))) for k in range(1, 7)}
    picked, noise, vals, ses = [], [], [], []
    for co in COINS:
        for sc, r in dj[co].items():
            if sc.startswith("_") or sc == "全部":
                continue
            cells = {b: c for b, c in tj[co][sc]["cells"].items() if c and b in ALLOWED}
            if r["action"] == "trade" and cells:
                picked.append(r["pick_cell"]["mean"]); noise.append(100 * 0.30 / np.sqrt(r["pick_cell"]["n"]) * emax[len(cells)])
    pooled = {}
    for b in ALLOWED:
        allc = [tj[co][sc]["cells"][b]["mean"] for co in COINS for sc in tj[co] if sc != "全部" and tj[co][sc]["cells"].get(b)]
        pooled[b] = float(np.mean(allc)) if allc else 0.0
    for co in COINS:
        for sc in tj[co]:
            if sc == "全部":
                continue
            for b in ALLOWED:
                c = tj[co][sc]["cells"].get(b)
                if c:
                    vals.append(c["mean"] - pooled[b]); ses.append(100 * 0.30 / np.sqrt(c["n"]))
    vals, ses = np.array(vals), np.array(ses)
    tau2 = max(vals.var() - (ses ** 2).mean(), 0) if len(vals) else 0.0
    return dict(n_picks=len(picked), picked_mean=float(np.mean(picked)) if picked else float("nan"),
                noise_mean=float(np.mean(noise)) if noise else float("nan"), tau2=float(tau2),
                shrink=float(tau2 / (tau2 + np.median(ses) ** 2)) if len(ses) else float("nan"))


def render(x: pd.DataFrame, run: str) -> str:
    L = [f"# W7 仓位与时点的数学分析(每周自动生成,{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)", "",
         f"数据:情形表 {run},{pd.Timestamp(x.close_ts.min(), unit='s'):%Y-%m-%d} → {pd.Timestamp(x.close_ts.max(), unit='s'):%Y-%m-%d %H:%M} UTC;"
         "价带内热门方,成本按 25 张走档含手续费;t 按窗口聚类。方法说明见 research/sizing_math_20261002.md。", ""]
    L += ["## 1. 每张期望、翻盘率、Kelly(按时点)", "",
          "| 时点 | 笔数 | 胜率 | 投入 | 赢/张 | 每张期望 | SE | t | Kelly f* | Kelly(−1SE) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in per_bin(x).itertuples():
        L.append(f"| {r.bin} | {r.n} | {100*r.p:.1f}% | {r.stake:.3f} | +{r.gain_c:.1f}c | {r.mu_c:+.2f}c | {r.se_c:.2f} | {r.t:+.2f} | {100*r.kelly:+.1f}% | {100*r.kelly_lo:+.1f}% |")
    L += ["", "对数增长 g(f) = p·ln(1+f·b) + (1−p)·ln(1−f),f = 每窗口投入占资金比例:", ""]
    for label, fstar, vals in growth_table():
        L.append(f"- {label}: f* = {100*fstar:+.1f}%;" + ", ".join(f"f={f:.0%}: {g:+.5f}" for f, g in vals))
    L += ["", "## 2. 入场价带 × 时点(每张期望 / t / 翻盘率 / 两周)", "", "| 价带 | 时点 | 期望 | t | 笔数 | 翻盘 | 前半 | 后半 |", "|---|---|---|---|---|---|---|---|"]
    for r in bands(x):
        L.append(f"| {r['band']} | {r['bin']} | {r['mu_c']:+.2f}c | {r['t']:+.2f} | {r['n']} | {100*r['flip']:.1f}% | {r['w1']:+.2f} | {r['w2']:+.2f} |")
    L += ["", "## 3. 分币(四个允许时点,每张期望 / t)", "", "| 币 | " + " | ".join(ALLOWED) + " |", "|---|" + "---|" * len(ALLOWED)]
    for c in COINS:
        pb = per_bin(x, x.coin == c).set_index("bin")
        L.append(f"| {c} | " + " | ".join(f"{pb.loc[b, 'mu_c']:+.1f} ({pb.loc[b, 't']:+.1f})" if b in pb.index else "—" for b in ALLOWED) + " |")
    wr = window_risk(x)
    L += ["", "## 4. 五币相关性与窗口风险(T-8.25)", "",
          f"- 五币结果平均相关 ρ = {wr['rho_mean']:.3f};五币同输 {100*wr['p_all5']:.2f}%(独立 {100*wr['p_all5_indep']:.4f}%);窗口标准差为独立假设的 {wr['sd_ratio_vs_indep']:.2f} 倍。",
          f"- 每币 1 张的窗口盈亏:均值 {wr['unit_mean']:+.3f}$,标准差 {wr['unit_sd']:.3f}$,1% 分位 {wr['unit_q01']:+.2f}$,5% 分位 {wr['unit_q05']:+.2f}$,最差 {wr['unit_min']:+.2f}$。",
          f"- VaR(99%) ≈ {wr['unit_q01']:.2f} × 每币张数;最差 ≈ {wr['unit_min']:.2f} × 每币张数。", ""]
    L.append("相关矩阵:\n\n" + wr["rho"].round(2).to_markdown() + "\n")
    for b in ("T-8.25", "T-6.75"):
        mk = multi_kelly(x, b)
        L.append(f"- 多资产 Kelly f = Σ⁻¹μ({b},统一优势):五币合计 {100*mk['f_pool_sum']:.1f}% 资金/窗口 → $200 时约 ${mk['f_pool_sum']*B0:.0f},1/4 Kelly ${mk['f_pool_sum']*B0/4:.0f}。")
    L += ["", "## 5. 仓位 vs 回撤(自助法 30 天,起始 $200,只按 T-8)", "", "| BTC/其他 | P(回撤>50%) | P(净值<$40) | 单窗最大投入 | 14天最差窗 |", "|---|---|---|---|---|"]
    for r in bootstrap(x, ((60, 40), (40, 30), (30, 20), (20, 15), (10, 10), (5, 5))):
        L.append(f"| {r['size']} | {100*r['p_dd50']:.0f}% | {100*r['p_ruin']:.1f}% | ${r['max_stake']:.0f} | {r['worst']:+.0f} |")
    u = -wr["unit_q01"]
    L += ["", "## 6. 按资金的张数(99% 窗口亏损 ≤ 10% 资金)", "", "| 资金 | 每币张数 | 对应最差窗口 |", "|---|---|---|"]
    for Bk in (200, 500, 1000, 2000, 5000):
        n = 0.1 * Bk / u
        L.append(f"| ${Bk} | {n:.1f} | {wr['unit_min']*n:+.0f} |")
    L.append(f"\n现行 40/30(窗口约 $144)按此规则需要约 ${10*u*32:.0f};按表机制(峰值约 1.6×)约 ${16*u*32:.0f}。")
    sn = selection_noise()
    L += ["", "## 7. 表格选格的噪声检验", "",
          f"- top1 格 {sn['n_picks']} 个,平均 {sn['picked_mean']:+.2f}c;若真实优势为 0,'k 选 1 取最大'的期望 {sn['noise_mean']:+.2f}c(比值 {sn['picked_mean']/sn['noise_mean'] if sn['noise_mean'] else float('nan'):.2f})。",
          f"- 格子相对时点平均的真实格间方差 {sn['tau2']:.1f}c²,收缩因子 {sn['shrink']:.2f}(0 = 格子不含超出时点平均的信息)。", ""]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(RESEARCH))
    a = ap.parse_args(argv)
    x, run = load()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    md = render(x, run)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    (out / f"sizing_weekly_{stamp}.md").write_text(md)
    (out / "sizing_weekly_latest.md").write_text(md)
    (out / ".weekly_stamp").write_text(str(time.time()))
    print(out / f"sizing_weekly_{stamp}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
