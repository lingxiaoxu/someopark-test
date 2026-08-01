"""
eda — B17-23 探索性分析(附录 A;G2 图 1/基准表复刻)
===================================================
输出全部写调用方指定 out_dir(测试传 /tmp/vp_tests/...,生产传 outputs/)。

- fig1_distributions: 复刻论文图 1——左: v 直方图(近正态);右: η 混合分布
  (对称、尾部较长)。
- baseline_predictability_table: 复刻 §2.3 基准表——ma5/lag1/ma22/ma252 对 v 的
  R²(总方差口径),对照论文 93.68/92.53/92.60/86.12(我方数据同量级验收,G2)。
- correlation_matrix: 特征相关矩阵(共线诊断,服务弱点④)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

PAPER_BASELINE_R2 = {"ma5": 0.9368, "lag1": 0.9253, "ma22": 0.9260, "ma252": 0.8612}


def _ma_by_ticker(v: pd.Series, window: int) -> pd.Series:
    return v.groupby(level=1).transform(lambda s: s.rolling(window).mean())


def baseline_predictability_table(panel: pd.DataFrame,
                                  out_dir: Optional[Path] = None) -> pd.DataFrame:
    """对 v 的四基准 R²(绕总均值)。基准值当日不可见自身 → 全部用 shift(1) 后的
    滚动统计(lag1=昨日 v;maN=截至昨日的 N 日均)——与论文"t 日预测只用 ≤t−1"一致。"""
    v = panel["v"].astype(float)
    preds: Dict[str, pd.Series] = {}
    v_lag = v.groupby(level=1).shift(1)
    preds["lag1"] = v_lag
    for name, w in (("ma5", 5), ("ma22", 22), ("ma252", 252)):
        preds[name] = _ma_by_ticker(v_lag, w)
    denom = float(((v - v.mean()) ** 2).sum())
    rows = []
    for name in ("ma5", "lag1", "ma22", "ma252"):
        p = preds[name]
        mask = p.notna()
        sse = float(((v[mask] - p[mask]) ** 2).sum())
        den = float(((v[mask] - v[mask].mean()) ** 2).sum())
        r2 = 1 - sse / den if den else float("nan")
        rows.append({"baseline": name, "r2_v": r2,
                     "paper_r2": PAPER_BASELINE_R2[name],
                     "gap_pp": (r2 - PAPER_BASELINE_R2[name]) * 100})
    df = pd.DataFrame(rows).set_index("baseline")
    if out_dir:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_dir / "baseline_predictability.csv")
    return df


def fig1_distributions(panel: pd.DataFrame, out_dir: Path) -> Path:
    """图 1 复刻: v 直方图 + η 分布(含正态叠线,展示对称长尾)。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    v = panel["v"].dropna()
    eta = panel["eta"].dropna()
    axes[0].hist(v, bins=80, density=True, alpha=0.8)
    axes[0].set_title("log dollar volume v (Fig.1 left)")
    axes[1].hist(eta, bins=120, density=True, alpha=0.8)
    x = np.linspace(eta.quantile(0.001), eta.quantile(0.999), 300)
    mu, sd = float(eta.mean()), float(eta.std())
    axes[1].plot(x, np.exp(-(x - mu) ** 2 / (2 * sd ** 2)) / (sd * np.sqrt(2 * np.pi)),
                 lw=1.2, label="normal ref")
    axes[1].legend()
    axes[1].set_title("eta = v - ma5(v) (Fig.1 right)")
    p = out_dir / "fig1_distributions.png"
    fig.tight_layout()
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p


def correlation_matrix(panel: pd.DataFrame, out_dir: Optional[Path] = None,
                       max_features: int = 60) -> pd.DataFrame:
    """特征相关矩阵(前 max_features 列,防图过大);CSV+热图。"""
    from VolumePrediction.models import feature_cols
    cols = feature_cols(panel)[:max_features]
    corr = panel[cols].corr()
    if out_dir:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        corr.to_csv(out_dir / "feature_correlation.csv")
        fig, ax = plt.subplots(figsize=(10, 8))
        im = ax.imshow(corr.values, cmap="coolwarm", vmin=-1, vmax=1)
        fig.colorbar(im)
        ax.set_title("feature correlation")
        fig.savefig(out_dir / "feature_correlation.png", dpi=110)
        plt.close(fig)
    return corr
