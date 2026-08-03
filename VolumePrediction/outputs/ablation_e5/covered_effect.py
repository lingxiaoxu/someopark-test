"""幸存组深查: 把增益限定到"真有特征覆盖的行"上量化效应大小。

全面板 delta 会被 90% 无覆盖行稀释;真正该问的是——在有日内形态数据的
那 10% 行上,加了这组特征到底提升多少。方法: 基线与 ablation 各存逐窗
OOS 预测,在 (有覆盖 ∧ 同窗) 子集上分别算 η 口径 R²。
"""
import os
import sys

import numpy as np
import pandas as pd

AB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/Users/xuling/code/someopark-test")
sys.path.insert(0, AB)

from VolumePrediction.replication_g import load_panel                    # noqa: E402
from VolumePrediction.evaluation import walkforward as wf                # noqa: E402
from VolumePrediction.evaluation.walkforward import oos_r2_eta           # noqa: E402
from run_ablation import load_features, daily_xz                         # noqa: E402

GROUP = sys.argv[1] if len(sys.argv) > 1 else "intraday"

panel = load_panel("prod_v6f32")
feats = load_features(GROUP)
fcols = [c for c in feats.columns if c.startswith("tech_ab_")]

pdates = panel.index.get_level_values("date").unique().sort_values()
prev_map = pd.Series(pdates[:-1], index=pdates[1:])
key = pd.DataFrame(index=panel.index).reset_index()
key["obs_date"] = key["date"].map(prev_map)
merged = (key.merge(feats, left_on=["ticker", "obs_date"],
                    right_on=["symbol", "obs_date"], how="left")
          .set_index(["date", "ticker"]))
aug = panel.copy()
for c in fcols:
    aug[c] = merged[c].reindex(aug.index).values
covered = aug[fcols].notna().any(axis=1)
aug = daily_xz(aug, fcols)
print(f"group={GROUP} covered rows: {covered.sum():,} / {len(aug):,} "
      f"({covered.mean():.2%})", flush=True)

wf.run(panel, models=["lgbm"], seeds=1, allow_deep=True,
       out_dir=os.path.join(AB, "cov_base"), run_tag="cb", save_preds=True)
wf.run(aug, models=["lgbm"], seeds=1, allow_deep=True,
       out_dir=os.path.join(AB, f"cov_{GROUP}"), run_tag="cg", save_preds=True)

rows = []
for w in range(12):
    pb = os.path.join(AB, "cov_base", "preds", f"cb_lgbm_w{w}.parquet")
    pg = os.path.join(AB, f"cov_{GROUP}", "preds", f"cg_lgbm_w{w}.parquet")
    if not (os.path.exists(pb) and os.path.exists(pg)):
        continue
    b = pd.read_parquet(pb)["eta_hat"]
    g = pd.read_parquet(pg)["eta_hat"]
    idx = b.index.intersection(g.index)
    m = covered.reindex(idx).fillna(False).values
    if m.sum() < 1000:
        rows.append({"window": w, "n_covered": int(m.sum()),
                     "base_r2": None, "grp_r2": None, "delta": None})
        continue
    ic = idx[m]
    yt = panel.loc[ic, "eta"]
    r_b = oos_r2_eta(yt, b.loc[ic])
    r_g = oos_r2_eta(yt, g.loc[ic])
    rows.append({"window": w, "n_covered": int(m.sum()),
                 "base_r2": round(r_b, 6), "grp_r2": round(r_g, 6),
                 "delta": round(r_g - r_b, 6)})
t = pd.DataFrame(rows)
print(t.to_string(index=False), flush=True)
val = t.dropna(subset=["delta"])
if len(val):
    w = val["n_covered"]
    print(f"\n覆盖行加权平均 delta: {np.average(val['delta'], weights=w):+.6f}"
          f"  (n={int(w.sum()):,} 行)", flush=True)
