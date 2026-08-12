"""
replication_econ_v2 — E11-T3: 直训经济学习复活实验(G7 差距#3)
==============================================================
病根(2026-08-11 定位): EconNN 绝对 losscon 在 λ~1e-9/μ~1e-6 量级下损失微小
且 z* 附近二次平坦 → float32 梯度淹没 → 现状 OOS MEL −40~−695(全 μ 档)。

三变体(models/econ.EconNN 的 loss_mode 参数,生产默认 absolute 不动):
  regret / anneal / analytic_s —— 完全复用 G7 的 MEL 归一化协议
(mel_normalized: oracle=100%, ma5=0%;μ 七档 config econ.mu_grid;
 paper_split 同一 OOS),与 outputs/replication/table2_mel.csv 直接可比。

验收(E11-T3): 任一变体 OOS MEL 转正 → 更新 G7 差异报告;全负 → 关闭此线。
sandbox: 只写 outputs/replication/table2_econ_v2.csv + econ_v2_summary.json,
不碰生产 EconNN 默认行为。

用法(仓库根):
  conda run -n someopark_run python -m VolumePrediction.replication_econ_v2 \
      --panel paper_full_v2 [--seeds 3] [--quick]
"""
from __future__ import annotations

import argparse
import json
import time
from typing import List

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, load_config, get_logger
from VolumePrediction.replication_g import load_panel, paper_split, cols_upto

log = get_logger("replication_econ_v2")

REP_DIR = OUT / "replication"
VARIANTS = ("regret", "anneal", "analytic_s")


def _prep(panel_tag: str):
    panel = load_panel(panel_tag)
    tr, te = paper_split(panel)
    cols = cols_upto(panel, "earn")
    return tr.dropna(subset=["eta"]), te.dropna(subset=["eta"]), cols


def run(panel_tag: str, seeds: int = 3, quick: bool = False) -> dict:
    from VolumePrediction.models.econ import EconNN, mel_normalized

    REP_DIR.mkdir(parents=True, exist_ok=True)
    mu_grid = [float(m) for m in load_config()["econ"]["mu_grid"]]
    tr, te, cols = _prep(panel_tag)
    Xtr, ytr, m5tr = tr[cols], tr["eta"], tr["ma5_v"]
    Xte, yte, m5te = te[cols], te["eta"], te["ma5_v"]
    v_true = (yte + m5te).values
    m5te_v = m5te.values
    n_seeds = 1 if quick else seeds
    ep = 5 if quick else 50

    # 现状对照(旧 table2 的 nn_econ 列,若在)
    old = {}
    t2_old = REP_DIR / "table2_mel.csv"
    if t2_old.exists():
        for _, r in pd.read_csv(t2_old).iterrows():
            old[float(r["mu"])] = r.get("nn_econ")

    rows: List[dict] = []
    out_csv = REP_DIR / "table2_econ_v2.csv"
    for mu in mu_grid:
        row = {"mu": mu, "nn_econ_absolute_old": old.get(mu)}
        for mode in VARIANTS:
            zs = []
            for sd in range(n_seeds):
                t0 = time.time()
                m = EconNN(len(cols), mu=mu, seed=sd, epochs=ep, loss_mode=mode)
                m.fit(Xtr, ytr, m5tr)
                zs.append(m.predict_z(Xte).values)
                log.info(f"{mode} mu={mu:g} seed {sd} ({time.time()-t0:.0f}s)")
            row[mode] = round(
                mel_normalized(v_true, np.mean(zs, axis=0), m5te_v, mu) * 100, 2)
        rows.append(row)
        pd.DataFrame(rows).to_csv(out_csv, index=False)     # 增量落盘(可中断)
        log.info(f"μ={mu:g} 完成: {row}")

    df = pd.DataFrame(rows)
    best = {m: float(df[m].max()) for m in VARIANTS}
    verdict = ("REVIVED" if any(v > 0 for v in best.values()) else "CLOSED")
    summary = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "panel": panel_tag, "seeds": n_seeds, "epochs": ep,
        "mu_grid": mu_grid,
        "best_mel_by_variant": best,
        "verdict": verdict,
        "note": ("任一变体 OOS MEL>0 ⇒ 直训经济学习复活(更新 G7 差异报告);"
                 "全负 ⇒ 关闭此线,迁移路线(TransferEconNN)定案。"
                 "生产 EconNN 默认 loss_mode=absolute 未动。"),
        "rows": rows,
    }
    (REP_DIR / "econ_v2_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))
    log.info(f"verdict={verdict} best={best}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="EconNN revival experiment (E11-T3)")
    ap.add_argument("--panel", default="paper_full_v2")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    res = run(a.panel, seeds=a.seeds, quick=a.quick)
    print(json.dumps({k: res[k] for k in ("best_mel_by_variant", "verdict")},
                     indent=2))
