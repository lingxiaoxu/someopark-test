"""E3 经济回测: 窄集 RNN vs 现役 lgbm vs ma5 在 2024-2026 段的真金白银差距。

方法(复用 G9 记账协议 replication_trading.simulate/perf_stats,不改其代码):
- 预测层用 walk-forward 的**真 OOS** 逐窗预测(preds/*.parquet)拼接 —— 每一天
  的 v̂ 都来自"只见过该日之前数据"的模型,与实盘同口径,无样本内粉饰。
- 目标层用 G9 的 oracle 信号目标(§6.2): 交易需求外生给定,四档预测器只影响
  "怎么把这笔需求铺开"(s(v̂;μ) 闭式解)→ 差异纯粹来自 v̂ 质量。
- 成本: 二次冲击 impact_coef·traded²/V(G9 同款),逐 μ 网格 × AUM 情景。
产物: scratchpad/econ_out/econ_2024_2026.csv + 汇总打印。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/xuling/code/someopark-test")

from VolumePrediction.replication_g import load_panel                    # noqa: E402
from VolumePrediction.replication_trading import (perf_stats, prep_arrays,  # noqa: E402
                                                  oracle_signal_targets,
                                                  simulate, slice_period)

WF = "/Users/xuling/code/someopark-test/VolumePrediction/outputs/walkforward/preds"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "econ_out")
START, END = "2024-01-03", "2026-07-31"
AUMS = [1e8, 1e9, 1e10]
MU_GRID = [1e-9, 1e-8, 1e-7, 1e-6]


def load_wf_eta(tag: str, model: str) -> pd.Series:
    """拼接该模型全部窗的 OOS 预测(窗间不重叠;重复取末次)。"""
    import glob
    fs = sorted(glob.glob(f"{WF}/{tag}_{model}_w*.parquet"))
    if not fs:
        raise FileNotFoundError(f"{tag}_{model}")
    s = pd.concat([pd.read_parquet(f)["eta_hat"] for f in fs])
    return s[~s.index.duplicated(keep="last")]


def main():
    os.makedirs(OUT, exist_ok=True)
    panel = load_panel("prod_v6")          # 需要 V 美元列,窄集副本没有
    arrs = prep_arrays(panel)
    sl = slice_period(arrs, START, END)
    dates, tickers = sl["dates"], sl["tickers"]
    print(f"经济回测段 {START}→{END}: {len(dates)} 交易日 × {len(tickers)} 票", flush=True)

    tiers = {"ma5": sl["ma5_v"]}
    for name, (tag, model) in {"rnn_narrow": ("p5_narrow", "rnn"),
                               "lgbm_full": ("p5_deep", "lgbm")}.items():
        eta = load_wf_eta(tag, model)
        wide = eta.unstack("ticker").reindex(index=dates, columns=tickers)
        cov = float(wide.notna().values.mean())
        print(f"  {name}: OOS 预测覆盖 {cov:.1%} of (day,ticker)", flush=True)
        # v̂ = η̂ + ma5_v;缺预测处留 NaN → simulate 内 z=0(不交易),对各档一视同仁
        tiers[name] = wide.to_numpy(dtype=float) + sl["ma5_v"]
    tiers["oracle"] = sl["v"]

    tgt, sig_info = oracle_signal_targets(sl["ret"], sl["present"], seed=7)
    print(f"信号: {sig_info}", flush=True)

    rows = []
    for aum in AUMS:
        gross = perf_stats(simulate(sl["ret"], sl["V"], tgt, aum=aum, z_override=1.0))
        rows.append({"tier": "gross_z1_nocost", "mu": None, "aum": aum, **gross})
        for tier, vhat in tiers.items():
            for mu in MU_GRID:
                st = perf_stats(simulate(sl["ret"], sl["V"], tgt, aum=aum,
                                         mu=mu, vhat=vhat))
                rows.append({"tier": tier, "mu": mu, "aum": aum, **st})
                print(f"  aum={aum:.0e} {tier:11s} mu={mu:g}: "
                      f"net={st['annret_net_pct']:8.3f}% sharpe={st['sharpe_net']:6.3f} "
                      f"turn={st['turnover_ann']:7.2f}", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/econ_2024_2026.csv", index=False)

    print("\n=== 每个 AUM 下各档的最优 μ(按净夏普) ===", flush=True)
    best = (df[df.tier != "gross_z1_nocost"].dropna(subset=["sharpe_net"])
            .sort_values("sharpe_net", ascending=False)
            .groupby(["aum", "tier"]).head(1)
            .sort_values(["aum", "sharpe_net"], ascending=[True, False]))
    print(best[["aum", "tier", "mu", "annret_net_pct", "sharpe_net",
                "cost_drag_pct", "turnover_ann"]].to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
