"""
replication_econ — GKMSZ 经济框架复刻跑批器(Plan §三 G6-G8: Table 2/3)
=====================================================================
协议:
- 样本/切分/特征与 replication_g 完全一致(paper split,全谱 earn 累进列)
- Table 2(G6/G7): 逐 μ(config econ.mu_grid 七档)评估各模型的归一化 MEL
  (mel_normalized 口径: oracle=100%, ma5=0%):
    · ols_stat / nn_stat(5种子) / rnn_stat(5种子): 统计模型 μ 无关,拟合一次,
      z=s(η̂+ma5;μ) 只在评估时随 μ 变——全网格零重训
    · ada_econ: 旧作语义(统计拟合树 → s 映射),同样 μ 无关
    · nn_econ(EconNN,G7 方法2): 直接以 losscon 为训练目标 —— μ 相关,
      每 μ×种子重训(重头戏,论文协议 50 epochs)
- Table 3(G8): TransferEconNN —— nn_stat 预训练权重 → 经济微调(5 epochs),
  每 μ×种子;并记录"微调后 MSE 变化 vs MEL 提升"签名(predict_eta)
- 产物: outputs/replication/table2_mel.csv, table3_transfer.csv,
  econ_summary.json;每完成一个 μ 立即增量落盘(可中断续读)

用法:
  python -m VolumePrediction.replication_econ --panel paper_full_v2
    [--seeds 5] [--quick]   # quick: 单种子+减 epochs 冒烟
"""
from __future__ import annotations

import argparse
import copy
import json
import time
from typing import Dict, List

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, load_config, get_logger
from VolumePrediction.replication_g import load_panel, paper_split, cols_upto

log = get_logger("replication_econ")
REP_DIR = OUT / "replication"


def _prep(panel_tag: str):
    panel = load_panel(panel_tag)
    tr, te = paper_split(panel)
    tr = tr[tr["eta"].notna() & tr["ma5_v"].notna()]
    te = te[te["eta"].notna() & te["ma5_v"].notna()]
    cols = cols_upto(panel, "earn")          # 全谱
    log.info(f"econ prep: {len(cols)} cols | train {len(tr):,} | test {len(te):,}")
    return tr, te, cols


def run(panel_tag: str, seeds: int = 5, quick: bool = False) -> dict:
    from VolumePrediction.models.econ import (
        mel, mel_normalized, s_opt, EconNN, EconAda, TransferEconNN)
    from VolumePrediction.models.deep import PaperNN, PaperRNN
    from VolumePrediction.models.linear import OLSModel

    REP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()["econ"]
    mu_grid = [float(m) for m in cfg["mu_grid"]]
    tr, te, cols = _prep(panel_tag)
    Xtr, ytr, m5tr = tr[cols], tr["eta"], tr["ma5_v"]
    Xte, yte, m5te = te[cols], te["eta"], te["ma5_v"]
    v_true = (yte + m5te).values             # 测试期真实 v
    n_seeds = 1 if quick else seeds
    ep_stat = 5 if quick else 50
    ep_econ = 5 if quick else 50
    ep_ft = 2 if quick else 5

    # ── 统计模型: 拟合一次(μ 无关) ──────────────────────────────────────────
    t0 = time.time()
    ols = OLSModel().fit(Xtr, ytr)
    eta_ols = ols.predict(Xte).values
    log.info(f"ols fit ({time.time()-t0:.0f}s)")

    nn_nets, eta_nn_seeds = [], []
    for sd in range(n_seeds):
        t0 = time.time()
        m = PaperNN(len(cols), seed=sd)
        m.fit(Xtr, ytr, epochs=ep_stat)
        nn_nets.append(m)
        eta_nn_seeds.append(m.predict(Xte).values)
        log.info(f"nn_stat seed {sd} ({time.time()-t0:.0f}s)")
    eta_nn = np.mean(eta_nn_seeds, axis=0)

    eta_rnn_seeds = []
    for sd in range(n_seeds):
        t0 = time.time()
        m = PaperRNN(len(cols), seed=sd)
        m.fit(Xtr, ytr, epochs=ep_stat)
        eta_rnn_seeds.append(m.predict(Xte).values)
        log.info(f"rnn_stat seed {sd} ({time.time()-t0:.0f}s)")
    eta_rnn = np.mean(eta_rnn_seeds, axis=0)

    t0 = time.time()
    ada = EconAda(mu=1e-6)                   # μ 只影响 predict_z,拟合 μ 无关
    ada.fit(Xtr, ytr, m5tr)
    eta_ada = ada.model.predict(Xte.values)
    log.info(f"ada fit ({time.time()-t0:.0f}s)")

    m5te_v = m5te.values
    t2_rows: List[dict] = []
    t3_rows: List[dict] = []
    t2_path = REP_DIR / "table2_mel.csv"
    t3_path = REP_DIR / "table3_transfer.csv"

    for mu in mu_grid:
        row = {"mu": mu}
        # 统计系: z = s(η̂+ma5; μ)
        for name, eta_hat in [("ols_stat", eta_ols), ("nn_stat", eta_nn),
                              ("rnn_stat", eta_rnn), ("ada_econ", eta_ada)]:
            z = s_opt(eta_hat + m5te_v, mu)
            row[name] = round(mel_normalized(v_true, z, m5te_v, mu) * 100, 2)
        # nn_econ: 每 μ×种子重训(G7 方法2)
        zs = []
        for sd in range(n_seeds):
            t0 = time.time()
            em = EconNN(len(cols), mu=mu, seed=sd, epochs=ep_econ)
            em.fit(Xtr, ytr, m5tr)
            zs.append(em.predict_z(Xte).values)
            log.info(f"nn_econ mu={mu:g} seed {sd} ({time.time()-t0:.0f}s)")
        row["nn_econ"] = round(
            mel_normalized(v_true, np.mean(zs, axis=0), m5te_v, mu) * 100, 2)
        t2_rows.append(row)
        pd.DataFrame(t2_rows).to_csv(t2_path, index=False)   # 增量落盘

        # Table 3: 迁移微调(深拷贝预训练网,防跨 μ 污染)
        zt, dmse, dmel = [], [], []
        for sd in range(n_seeds):
            t0 = time.time()
            base = copy.deepcopy(nn_nets[sd])
            tf = TransferEconNN(base, mu=mu, finetune_epochs=ep_ft, seed=sd)
            tf.finetune(Xtr, ytr, m5tr)
            zt.append(tf.predict_z(Xte, m5te).values)
            # 签名: 微调后统计口径 vs 微调前
            eta_after = tf.predict_eta(Xte).values
            mse_b = float(np.nanmean((yte.values - eta_nn_seeds[sd]) ** 2))
            mse_a = float(np.nanmean((yte.values - eta_after) ** 2))
            dmse.append(mse_a - mse_b)
            log.info(f"transfer mu={mu:g} seed {sd} ({time.time()-t0:.0f}s)")
        z_tf = np.mean(zt, axis=0)
        z_st = s_opt(eta_nn + m5te_v, mu)
        t3_rows.append({
            "mu": mu,
            "nn_stat": round(mel_normalized(v_true, z_st, m5te_v, mu) * 100, 2),
            "transfer_econ": round(
                mel_normalized(v_true, z_tf, m5te_v, mu) * 100, 2),
            "d_mse_after_ft": round(float(np.mean(dmse)), 6),
        })
        pd.DataFrame(t3_rows).to_csv(t3_path, index=False)
        log.info(f"μ={mu:g} 完成: T2={row} | T3={t3_rows[-1]}")

    summary = {"panel_tag": panel_tag, "seeds": n_seeds, "quick": quick,
               "mu_grid": mu_grid,
               "table2": t2_rows, "table3": t3_rows}
    (REP_DIR / "econ_summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False))
    log.info(f"econ replication artifacts → {REP_DIR}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="paper_full_v2")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    res = run(a.panel, seeds=a.seeds, quick=a.quick)
    print(json.dumps({"table3": res["table3"]}, ensure_ascii=False))
