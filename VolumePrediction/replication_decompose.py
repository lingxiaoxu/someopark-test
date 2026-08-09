"""
replication_decompose — G9 三改进源分解 + C.1 paper-split 市值五分位(审计补齐)
==============================================================================
两个审计 ⚠️ 项一次闭环(全部吃已存档数据,不重训深模型):

【G9 三改进源】(plan L179: all>tech>ma5 / 非线性>线性 / econ 增量)
  ① 信息集: R²(all 谱 NN) > R²(tech NN) > R²(ma5 基线)
     — NN 预测用已存档 eta_pred_nn_{tech,earn}.parquet(earn=G3 累进最后一组=全谱);
       ma5 基线即 η̂≡0(η 定义为 v−ma5 的残差,基线预测残差为零)。
  ② 非线性: NN vs OLS 同列集对照 — OLS 现场补跑(线性回归,秒级,同 paper_split)。
  ③ econ 增量: 引用 G7/G8 已验收产物(table2: transfer_econ vs nn_stat 的 MEL,
     全 7 档 μ 优于纯统计)— 不重复实验,引用留档。

【C.1 市值分层】(plan L150: paper-split 协议轨的市值五分位稳定性)
  用全谱 NN 存档预测,按测试期票均 fund1_size_ln_mcap 五分位算各层 R²(η),
  呈现 mega→nano 可预测性模式(此前只有 walkforward 轨的十分位版)。

产物: outputs/replication/g9_decomposition.csv
      outputs/replication/c1_size_quintile_paper.csv
运行: conda run -n someopark_run python -m VolumePrediction.replication_decompose
"""
from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, get_logger
from VolumePrediction.replication_g import (load_panel, paper_split, cols_upto,
                                            _r2_eta, make_model)

log = get_logger("replication_decompose")
REP = OUT / "replication"


def _ols_r2(tr: pd.DataFrame, te: pd.DataFrame, cols: list) -> float:
    m, fkw = make_model("ols", len(cols), 0, quick=False)
    fkw = {k: v for k, v in fkw.items() if v is not None}
    m.fit(tr[cols], tr["eta"], **fkw)
    yhat = m.predict(te[cols])
    yhat.index = te.index
    return _r2_eta(te["eta"], yhat)


def run(panel_tag: str = "latest") -> dict:
    t0 = time.time()
    panel = load_panel(panel_tag)
    tr, te = paper_split(panel)
    tr, te = tr[tr["eta"].notna()], te[te["eta"].notna()]

    # ── 已存档 NN 预测(与 te 对齐)────────────────────────────────────────────
    nn = {}
    for g in ("tech", "earn"):
        f = REP / f"eta_pred_nn_{g}.parquet"
        if not f.exists():
            raise RuntimeError(f"{f} missing — rerun replication_g with --save-preds")
        s = pd.read_parquet(f)["eta_hat"]
        common = te.index.intersection(s.index)
        nn[g] = (te.loc[common, "eta"], s.loc[common])
    te_al = nn["earn"][0].index                      # 对齐后的测试集(全谱组)

    # ── G9 ①信息集 + ②非线性 ────────────────────────────────────────────────
    r2_nn_tech = _r2_eta(*nn["tech"])
    r2_nn_all = _r2_eta(*nn["earn"])
    r2_ma5 = 0.0                                     # η̂≡0 基线: R²(η)=0 by construction
    log.info("OLS 补跑(tech / all 两列集,paper_split 同协议)...")
    r2_ols_tech = _ols_r2(tr, te, cols_upto(panel, "tech"))
    r2_ols_all = _ols_r2(tr, te, cols_upto(panel, "earn"))

    # ── G9 ③econ 增量(引用 G7/G8 已验收产物: 经迁移路线 table3)──────────────
    # 命题 1 的成立路径是 transfer_econ(G7 差异报告: 从零直训 nn_econ 失败,
    # 经迁移微调后 econ 全 7 档 μ 优于纯统计)→ 引用 table3_transfer.csv。
    econ_ref = {}
    t3 = REP / "table3_transfer.csv"
    if t3.exists():
        d = pd.read_csv(t3)
        if {"transfer_econ", "nn_stat"} <= set(d.columns):
            econ_ref = {"mel_transfer_econ_mean": round(float(d["transfer_econ"].mean()), 3),
                        "mel_nn_stat_mean": round(float(d["nn_stat"].mean()), 3),
                        "n_mu": int(len(d)),
                        "econ_beats_stat_all_mu": bool(
                            (d["transfer_econ"] > d["nn_stat"]).all()),
                        "source": "table3_transfer.csv (G7/G8 accepted)"}

    rows = [
        {"source": "info_set", "contrast": "ma5_baseline", "r2_eta_pct": 0.0,
         "note": "eta_hat≡0 (eta already residual vs ma5)"},
        {"source": "info_set", "contrast": "nn_tech", "r2_eta_pct": round(r2_nn_tech * 100, 4),
         "note": "archived eta_pred_nn_tech"},
        {"source": "info_set", "contrast": "nn_all", "r2_eta_pct": round(r2_nn_all * 100, 4),
         "note": "archived eta_pred_nn_earn (full cumulative)"},
        {"source": "nonlinearity", "contrast": "ols_tech", "r2_eta_pct": round(r2_ols_tech * 100, 4),
         "note": "OLS refit, same split/cols"},
        {"source": "nonlinearity", "contrast": "ols_all", "r2_eta_pct": round(r2_ols_all * 100, 4),
         "note": "OLS refit, same split/cols"},
        {"source": "econ_objective", "contrast": "table2_reference",
         "r2_eta_pct": None,
         "note": json.dumps(econ_ref) if econ_ref else "table2 missing"},
    ]
    g9 = pd.DataFrame(rows)
    g9.to_csv(REP / "g9_decomposition.csv", index=False)

    ok_info = r2_nn_all > r2_nn_tech > r2_ma5
    ok_nonlin = (r2_nn_tech > r2_ols_tech) and (r2_nn_all > r2_ols_all)
    log.info(f"G9 ①info: all {r2_nn_all*100:.2f} > tech {r2_nn_tech*100:.2f} > ma5 0 "
             f"→ {'✓' if ok_info else '✗'}")
    log.info(f"G9 ②nonlin: nn>ols tech({r2_nn_tech*100:.2f}>{r2_ols_tech*100:.2f}) "
             f"all({r2_nn_all*100:.2f}>{r2_ols_all*100:.2f}) → {'✓' if ok_nonlin else '✗'}")

    # ── C.1 paper-split 市值五分位 ───────────────────────────────────────────
    size_col = "fund1_size_ln_mcap"
    tkr_mcap = te[size_col].groupby(level="ticker").mean().dropna()
    quint = pd.qcut(tkr_mcap, 5, labels=False, duplicates="drop")
    c1_rows = []
    y_all, yh_all = nn["earn"]
    y_t, yh_t = nn["tech"]
    for q in range(5):
        tks = set(quint[quint == q].index)
        m_all = y_all.index.get_level_values("ticker").isin(tks)
        m_t = y_t.index.get_level_values("ticker").isin(tks)
        c1_rows.append({
            "quintile": q, "label": ["nano", "small", "mid", "large", "mega"][q],
            "n_tickers": len(tks), "n_obs": int(m_all.sum()),
            "r2_nn_all_pct": round(_r2_eta(y_all[m_all], yh_all[m_all]) * 100, 4),
            "r2_nn_tech_pct": round(_r2_eta(y_t[m_t], yh_t[m_t]) * 100, 4)})
    c1 = pd.DataFrame(c1_rows)
    c1.to_csv(REP / "c1_size_quintile_paper.csv", index=False)
    log.info("C.1 five-quintile (paper split):\n" + c1.to_string(index=False))

    out = {"g9_info_set_ok": ok_info, "g9_nonlinearity_ok": ok_nonlin,
           "g9_econ_reference": econ_ref,
           "r2": {"nn_all": round(r2_nn_all * 100, 4), "nn_tech": round(r2_nn_tech * 100, 4),
                  "ols_all": round(r2_ols_all * 100, 4), "ols_tech": round(r2_ols_tech * 100, 4)},
           "c1_quintiles": c1_rows, "elapsed_s": round(time.time() - t0, 1)}
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False, default=str))
