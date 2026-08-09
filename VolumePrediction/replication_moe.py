"""
replication_moe — G5/C.1 专家混合(MoE)对照实验(plan §三 L151 补齐)
====================================================================
论文附录 C.1 结论: 按市值分组训练专家(mixture-of-experts)相对 pooled 单模型
"无明显收益"。本实验做同款验证:

  pooled : PaperNN(tech+fund1 窄集)全样本训练 → OOS R²(η)
  MoE    : 按训练期票均 ln_mcap 五分位分组,各组独立训同协议 PaperNN,
           测试期按票路由到所属专家 → 拼接全体 → OOS R²(η)

公平性: pooled 与全部 5 个专家用**完全相同协议**(同 epochs/seed/列集/split),
差异只来自"分组与否"。票级路由(专家按票,不按行);测试期新票(无训练期市值)
路由中位组并计数披露。

产物: outputs/replication/moe_experiment.csv + 控制台摘要(G10 补录用)
运行: conda run -n someopark_run python -m VolumePrediction.replication_moe [--quick]
      --quick: 5 epochs/1 seed 冒烟;正式: 论文协议 epochs(重,数小时)
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Dict, List

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, get_logger
from VolumePrediction.replication_g import (load_panel, paper_split, cols_upto,
                                            _r2_eta, make_model)

log = get_logger("replication_moe")
REP_DIR = OUT / "replication"
N_GROUPS = 5


def _fit_predict(tr: pd.DataFrame, te: pd.DataFrame, cols: List[str],
                 seed: int, quick: bool) -> pd.Series:
    """一次同协议 PaperNN 训练+预测。接口与 replication_g.run 逐位一致:
    fit/predict 吃带 MultiIndex 的 DataFrame/Series(_sort_panel 依赖 index)。"""
    m, fkw = make_model("nn", len(cols), seed, quick)
    fkw = {k: v for k, v in fkw.items() if v is not None}
    m.fit(tr[cols], tr["eta"], **fkw)
    yhat = m.predict(te[cols])
    yhat.index = te.index
    return yhat


def run(panel_tag: str = "latest", quick: bool = False, seed: int = 0) -> dict:
    t0 = time.time()
    panel = load_panel(panel_tag)
    tr, te = paper_split(panel)
    tr, te = tr[tr["eta"].notna()], te[te["eta"].notna()]
    cols = cols_upto(panel, "fund1")            # 论文优势域: tech+fund1 窄集
    size_col = "fund1_size_ln_mcap"
    if size_col not in panel.columns:
        raise RuntimeError(f"panel lacks {size_col} — MoE grouping impossible")

    # ── 票级市值五分位(训练期均值;PIT: 只用训练期信息定路由)──────────────
    tkr_mcap = tr.groupby(level="ticker")[size_col].mean().dropna()
    grp_of = pd.Series(pd.qcut(tkr_mcap, N_GROUPS, labels=False, duplicates="drop"),
                       index=tkr_mcap.index)
    te_tickers = te.index.get_level_values("ticker")
    unrouted = sorted(set(te_tickers.unique()) - set(grp_of.index))
    mid = N_GROUPS // 2

    # ── pooled 基准 ──────────────────────────────────────────────────────────
    log.info(f"pooled: n_train={len(tr):,} n_test={len(te):,} cols={len(cols)}")
    yhat_pool = _fit_predict(tr, te, cols, seed, quick)
    r2_pool = _r2_eta(te["eta"], yhat_pool)

    # ── MoE: 五组专家,同协议 ────────────────────────────────────────────────
    yhat_moe = pd.Series(np.nan, index=te.index)
    grp_rows = []
    for g in range(N_GROUPS):
        g_tkrs = set(grp_of[grp_of == g].index)
        tr_g = tr[tr.index.get_level_values("ticker").isin(g_tkrs)]
        te_tkrs = g_tkrs | (set(unrouted) if g == mid else set())
        te_g = te[te.index.get_level_values("ticker").isin(te_tkrs)]
        if tr_g.empty or te_g.empty:
            grp_rows.append({"group": g, "n_train": len(tr_g), "n_test": len(te_g),
                             "r2_within": None})
            continue
        yh = _fit_predict(tr_g, te_g, cols, seed, quick)
        yhat_moe.loc[yh.index] = yh
        grp_rows.append({"group": g, "n_tickers": len(g_tkrs),
                         "n_train": len(tr_g), "n_test": len(te_g),
                         "r2_within": round(_r2_eta(te_g["eta"], yh) * 100, 4)})
        log.info(f"expert g{g}: tickers={len(g_tkrs)} train={len(tr_g):,} "
                 f"R2_within={grp_rows[-1]['r2_within']}%")
    mask = yhat_moe.notna()
    r2_moe = _r2_eta(te.loc[mask, "eta"], yhat_moe[mask])

    # ── 汇总与落盘 ───────────────────────────────────────────────────────────
    delta_pp = (r2_moe - r2_pool) * 100
    verdict = ("consistent_with_paper(no_gain)" if delta_pp <= 0.3
               else "GAIN_FOUND(diverges_from_paper)")
    out = pd.DataFrame(grp_rows)
    out.attrs = {}
    summary = {
        "panel_tag": panel_tag, "protocol": ("quick_5ep_1seed" if quick
                                             else "paper_epochs_1seed"),
        "cols": len(cols), "n_train": len(tr), "n_test": len(te),
        "r2_eta_pooled_pct": round(r2_pool * 100, 4),
        "r2_eta_moe_pct": round(r2_moe * 100, 4),
        "delta_pp": round(delta_pp, 4), "verdict": verdict,
        "unrouted_new_tickers": len(unrouted),
        "elapsed_s": round(time.time() - t0, 1),
    }
    REP_DIR.mkdir(parents=True, exist_ok=True)
    csv = REP_DIR / "moe_experiment.csv"
    hdr = pd.DataFrame([summary])
    with open(csv, "w") as fh:
        hdr.to_csv(fh, index=False)
        fh.write("\n")
        out.to_csv(fh, index=False)
    log.info(f"MoE experiment → {csv}: pooled={summary['r2_eta_pooled_pct']}% "
             f"moe={summary['r2_eta_moe_pct']}% Δ={summary['delta_pp']}pp [{verdict}]")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="G5/C.1 MoE 对照实验")
    ap.add_argument("--panel", default="latest")
    ap.add_argument("--quick", action="store_true", help="5 epochs/1 seed 冒烟协议")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    print(json.dumps(run(a.panel, quick=a.quick, seed=a.seed),
                     indent=2, ensure_ascii=False))
