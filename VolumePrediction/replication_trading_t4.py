"""
replication_trading_t4 — E11-T4: AUM 规模仿真(差距#6 替代;T1 系数落地)
========================================================================
自家轨物理不可测(E11 发现②)→ 规模化经济用**自家校准 λ**(T1 Amihud:
λ(V)=C·V^(−γ))替换论文 impact 刻度,重跑 fig5/表 3,AUM 阶梯取
**我们自己的规模 ×1/×10/×100/×1000**(论文的 1e10 美元刻度换成
"我们的 2017-2026 实测")。与 size_realism 敏感性表合并成"规模化经济地图"。

λ 形状映射(如实声明,不硬凑): simulate 的成本核 cost=coef·traded²/V 是
γ=1 专用(coef=½λV);我们 γ=1.19 ⇒ 用参考 $V 处的等效系数
coef_eff(V_ref)=½·C·V_ref^(1−γ),并给 V_ref 三档(sleeve 名字级中位 $V 的
P25/P50/P75)做敏感性——γ>1 意味着大票比标量映射更便宜、小票更贵,
方向在报告中明示。

产物: outputs/replication/t4_scale_map/(table3_own_coef.csv、fig5、
scale_map_report.md)。纯研究轨,不碰生产。

用法(仓库根):
  conda run -n someopark_run python -m VolumePrediction.replication_trading_t4 \
      [--panel paper_full_v2] [--base-aum 6e6] [--quick]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, load_config, get_logger
from VolumePrediction.replication_g import load_panel
from VolumePrediction.replication_trading import (
    prep_arrays, slice_period, find_model_predictions,
    run_experiment1, run_table3, IMPACT_COEF, REP_DIR, QUICK_MU_GRID,
    QUICK_TICKERS_CAP,
)

log = get_logger("replication_trading_t4")

T4_DIR = OUT / "replication" / "t4_scale_map"


def load_own_lambda() -> dict:
    p = OUT / "registry" / "lambda_calibration.json"
    d = json.loads(p.read_text())["lambda_amihud"]
    if d["calibration_source"] != "amihud_market_proxy":
        raise SystemExit("lambda_calibration.json is paper_prior — run "
                         "calibrate_lambda first (T1)")
    return d


def coef_eff(C: float, gamma: float, v_ref: float) -> float:
    """simulate 成本核等效系数: cost=coef·traded²/V ⇔ λ=2coef/V ⇒
    coef_eff = ½·λ_ours(V_ref)·V_ref = ½·C·V_ref^(1−γ)。"""
    return 0.5 * C * v_ref ** (1.0 - gamma)


def run(panel_tag: str, base_aum: float, quick: bool, seed: int = 7) -> dict:
    lam = load_own_lambda()
    C, gamma = float(lam["C"]), float(lam["gamma"])
    cfg = load_config()
    mu_grid = [float(m) for m in cfg["econ"]["mu_grid"]]
    tickers_cap = None
    if quick:
        mu_grid = [m for m in QUICK_MU_GRID if m in mu_grid] or QUICK_MU_GRID
        tickers_cap = QUICK_TICKERS_CAP

    panel = load_panel(panel_tag)
    arrs = prep_arrays(panel, tickers_cap=tickers_cap)
    sp = cfg["split"]["paper"]
    sl_tr = slice_period(arrs, *sp["train"])
    sl_te = slice_period(arrs, *sp["test"])
    eta_all = find_model_predictions(arrs["dates"], arrs["tickers"], REP_DIR)

    def tiers_for(sl, mask):
        tiers = {"ma5": sl["ma5_v"]}
        if eta_all is not None:
            tiers["model"] = eta_all[np.asarray(mask)] + sl["ma5_v"]
        tiers["oracle"] = sl["v"]
        return tiers

    m_tr = (arrs["dates"] >= sp["train"][0]) & (arrs["dates"] <= sp["train"][1])
    m_te = (arrs["dates"] >= sp["test"][0]) & (arrs["dates"] <= sp["test"][1])
    tiers_tr, tiers_te = tiers_for(sl_tr, m_tr), tiers_for(sl_te, m_te)

    # V_ref 三档: 测试 sleeve 名字级中位 $V 的 P25/P50/P75(γ≠1 的敏感性)
    with np.errstate(all="ignore"):
        name_med_V = np.nanmedian(np.where(sl_te["V"] > 0, sl_te["V"], np.nan),
                                  axis=0)
    name_med_V = name_med_V[np.isfinite(name_med_V)]
    v_refs = {f"P{int(q * 100)}": float(np.quantile(name_med_V, q))
              for q in (0.25, 0.50, 0.75)}
    coefs = {k: coef_eff(C, gamma, v) for k, v in v_refs.items()}
    log.info(f"own lambda C={C:.3g} gamma={gamma:.3f}; V_ref={v_refs}; "
             f"coef_eff={coefs} (paper {IMPACT_COEF})")

    aum_scenarios = [base_aum * m for m in (1, 10, 100, 1000)]
    T4_DIR.mkdir(parents=True, exist_ok=True)

    # 表 3(own coef @ P50)× 我们的 AUM 阶梯 + fig5 @ ×100 规模
    t3 = run_table3(sl_tr, sl_te, tiers_tr, tiers_te, mu_grid, aum_scenarios,
                    seed=seed, out_dir=T4_DIR, impact_coef=coefs["P50"])
    fig5 = run_experiment1(sl_te, tiers_te, mu_grid, aum=base_aum * 100,
                           seed=seed, out_dir=T4_DIR, impact_coef=coefs["P50"])
    (T4_DIR / "fig5_data.csv").rename(T4_DIR / "fig5_own_coef.csv")
    (T4_DIR / "table3_trading.csv").rename(T4_DIR / "table3_own_coef.csv")

    # coef 敏感性: P25/P75 只跑 oracle 档 × 全 AUM(方向界定,省算力)
    sens_rows = []
    for tag, coef in coefs.items():
        t3s = run_table3(sl_tr, sl_te,
                         {"oracle": tiers_tr["oracle"]},
                         {"oracle": tiers_te["oracle"]},
                         mu_grid, aum_scenarios, seed=seed,
                         out_dir=T4_DIR, impact_coef=coef)
        for r in t3s["records"]:
            sens_rows.append({"v_ref": tag, "coef_eff": coef, **r})
    (T4_DIR / "table3_trading.csv").unlink(missing_ok=True)
    sens = pd.DataFrame(sens_rows)
    sens.to_csv(T4_DIR / "coef_sensitivity_oracle.csv", index=False)

    # 报告: 规模化经济地图(own 刻度 + size_realism 交叉引用)
    lines = [
        "# 规模化经济地图(E11-T4: 自家校准 λ × 我们的 AUM 阶梯)",
        f"\n生成: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        f"\n- 自家 λ(T1 Amihud): C={C:.4g}, γ={gamma:.4f}"
        f"(asof {lam['asof']}, n={lam['n_names']}, R²={lam['r2']:.3f})",
        f"- 等效成本系数 coef_eff=½·C·V_ref^(1−γ): "
        + ", ".join(f"{k}(${v_refs[k]:.2e})→{coefs[k]:.4g}" for k in coefs)
        + f"(论文 {IMPACT_COEF})",
        f"- AUM 阶梯 = 我们的规模 ×1/×10/×100/×1000: "
        + ", ".join(f"{a:.0e}" for a in aum_scenarios),
        "- γ>1 声明: 成本核为 γ=1 专用,标量映射在 V>V_ref 的名字高估成本、"
        "V<V_ref 低估——P25/P75 敏感性行给出方向界。",
        "- 与 size_realism 合并阅读: outputs/size_realism_analysis.md"
        "(participation 现实性)+ 本表(净收益/夏普随 AUM 衰减)。",
        "\n## 表 3(own coef @P50)",
        (T4_DIR / "table3_own_coef.csv").read_text(),
        "\n## oracle 档 coef 敏感性(P25/P50/P75)",
        sens.to_csv(index=False),
    ]
    (T4_DIR / "scale_map_report.md").write_text("\n".join(lines))
    log.info(f"T4 artifacts → {T4_DIR}")
    return {"C": C, "gamma": gamma, "coef_eff": coefs,
            "aum_scenarios": aum_scenarios,
            "table3_records": t3["records"], "fig5": fig5["signal"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AUM scale simulation (E11-T4)")
    ap.add_argument("--panel", default="paper_full_v2")
    ap.add_argument("--base-aum", type=float, default=6e6,
                    help="我们的当前组合规模(官方口径 ≈ $6M)")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    res = run(a.panel, a.base_aum, a.quick)
    print(json.dumps({k: res[k] for k in ("C", "gamma", "coef_eff",
                                          "aum_scenarios")}, indent=2))
