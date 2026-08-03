"""富矿 ablation runner: join 特征组到 prod_v6 面板 → 每组独立 lgbm WF。

纪律(E5): 每组独立、不与主线混算、面板 tag 区分、赢了才留。
- PIT: fund3 特征日 D 的观测 → join 到面板行 D+1(按该 symbol 的面板日序 shift;
  与 prod_v6 tech=z(raw(T-1)) 同口径)
- 覆盖期(2025+)之外全 NaN → 12 窗协议不变,只有 2025-2026 测试窗受影响,
  与 p5_deep lgbm 基线逐窗对比
- 质量门: intraday 组只在 n_mkt_hours==7 时给形态值(源数据尾盘缺录实证)
- z 化: 按面板惯例逐日横截面 z(clip ±5,与 refreeze 面板同款)
用法: conda run -n someopark_run python run_ablation.py <group>
      group ∈ {est, intraday}   (4h 组 ETL 后加)
产物: 本目录 wf_ab_<group>/ (windows/stratified CSV + summary)
"""
import os
import sys

import numpy as np
import pandas as pd

AB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/Users/xuling/code/someopark-test")

GROUP = sys.argv[1] if len(sys.argv) > 1 else "est"


def daily_xz(df: pd.DataFrame, cols) -> pd.DataFrame:
    """逐日横截面 z(与面板惯例同款), clip ±5。"""
    for c in cols:
        g = df.groupby(level="date")[c]
        df[c] = ((df[c] - g.transform("mean")) / g.transform("std")).clip(-5, 5)
    return df


def load_features(group: str) -> pd.DataFrame:
    if group == "est":
        f = pd.read_parquet(os.path.join(AB, "est_revisions.parquet"))
        f["tech_ab_est_rev_1d"] = np.log1p(f["n_rev"].astype(float))
        f = f.sort_values(["symbol", "day"])
        f["tech_ab_est_rev_5d"] = (f.groupby("symbol")["n_rev"]
                                 .transform(lambda s: np.log1p(s.rolling(5, min_periods=1).sum())))
        keep = ["symbol", "day", "tech_ab_est_rev_1d", "tech_ab_est_rev_5d"]
    elif group == "intraday":
        f = pd.read_parquet(os.path.join(AB, "intraday_shape.parquet"))
        full = f["n_mkt_hours"] == 7            # 质量门: 残日不给形态
        for c in ("first_hour_share", "last_hour_share", "midday_dry", "ah_share"):
            f[f"tech_ab_{c}"] = f[c].where(full)
        keep = ["symbol", "day"] + [f"tech_ab_{c}" for c in
                ("first_hour_share", "last_hour_share", "midday_dry", "ah_share")]
    else:
        raise ValueError(group)
    out = f[keep].rename(columns={"day": "obs_date"})
    out["obs_date"] = pd.to_datetime(out["obs_date"])
    return out


def main():
    from VolumePrediction.replication_g import load_panel
    from VolumePrediction.evaluation import walkforward as wf

    panel = load_panel("prod_v6f32")  # 低内存副本(特征 float32,目标 float64)
    print(f"panel: {panel.shape}", flush=True)
    feats = load_features(GROUP)
    fcols = [c for c in feats.columns if c.startswith("tech_ab_")]
    print(f"group={GROUP} features: {fcols}, obs rows {len(feats):,}", flush=True)

    # ── PIT join: 面板行 (date=T, ticker) ← 特征观测日 = T 的前一交易日 ──
    # 面板日序做 shift 映射: 对每个 date T 取面板日历上前一日 P(T);
    # 特征按 (ticker, P(T)) 精确对齐(无 asof 前向填充 → 缺日就 NaN,不粉饰)。
    pdates = panel.index.get_level_values("date").unique().sort_values()
    prev_map = pd.Series(pdates[:-1], index=pdates[1:])   # T -> T-1(交易日)
    key = pd.DataFrame(index=panel.index).reset_index()
    key["obs_date"] = key["date"].map(prev_map)
    merged = key.merge(feats, left_on=["ticker", "obs_date"],
                       right_on=["symbol", "obs_date"], how="left")
    merged = merged.set_index(["date", "ticker"])
    aug = panel.copy()
    for c in fcols:
        aug[c] = merged[c].reindex(aug.index).values
    aug = daily_xz(aug, fcols)
    from VolumePrediction.models import feature_cols as _fc
    _seen = set(_fc(aug))
    missing = [c for c in fcols if c not in _seen]
    assert not missing, (f"新特征未被 feature_cols 认到(前缀不在 FEATURE_PREFIXES) → "
                         f"会被静默丢弃,ablation 无效: {missing}")
    print(f"引擎特征列: {len(_seen)}(含新增 {len(fcols)}) ✓", flush=True)
    cov = aug[fcols].notna().mean()
    print("覆盖率(全面板行):", cov.round(4).to_dict(), flush=True)
    cov25 = aug.loc[aug.index.get_level_values('date') >= '2025-04-01', fcols].notna().mean()
    print("覆盖率(2025-04 后):", cov25.round(4).to_dict(), flush=True)

    out_dir = os.path.join(AB, f"wf_ab_{GROUP}")
    res = wf.run(aug, models=["lgbm"], seeds=1, allow_deep=True,
                 out_dir=out_dir, run_tag=f"ab_{GROUP}")
    print("global_r2:", res["global_r2"], flush=True)


if __name__ == "__main__":
    main()
