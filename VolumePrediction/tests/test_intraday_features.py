"""日内形态特征: 前缀注册 + 窗口口径 + 质量门(E5,2026-08-04)。

这组测试守两条曾经出过事的红线:
1. 新特征前缀必须被 feature_cols() 认到(fund3_ 静默丢弃的教训)
2. 只用 4:00-13:00 ET 窗 —— 采集器 14:05 盘中跑,尾盘条永远缺,
   训练窗口若宽于服务窗口就是口径不一致
"""
import numpy as np
import pandas as pd
import pytest

from VolumePrediction.features import intraday_features as ifeat
from VolumePrediction.models import FEATURE_PREFIXES, feature_cols


def test_prefix_registered():
    assert "intraday_" in FEATURE_PREFIXES
    df = pd.DataFrame({c: [0.0] for c in ifeat.FEATURES} | {"eta": [1.0]})
    assert set(feature_cols(df)) == set(ifeat.FEATURES), "特征会被静默丢弃"


def test_window_excludes_unservable_hours():
    """训练窗口不得含 14:00/15:00 —— 服务时取不到。"""
    assert ifeat.MKT_HOURS == [9, 10, 11, 12, 13]
    assert 14 not in ifeat.MKT_HOURS and 15 not in ifeat.MKT_HOURS
    assert max(ifeat.PRE_HOURS) < min(ifeat.MKT_HOURS)


def test_shape_math_and_quality_gate():
    """五根齐全才出特征;数值与手工定义一致。"""
    def bar(sym, day, hh, v):
        ts = pd.Timestamp(f"{day} {hh:02d}:00", tz="America/New_York")
        return {"symbol": sym, "t": int(ts.timestamp() * 1000), "v": v}

    full = [bar("AAA", "2026-06-10", h, v) for h, v in
            zip([4, 5, 9, 10, 11, 12, 13], [10, 10, 100, 50, 50, 20, 30])]
    partial = [bar("BBB", "2026-06-10", h, 10) for h in (9, 10, 11)]  # 只 3 根
    g = ifeat._flush(full + partial)

    mkt = g.loc[("AAA", pd.Timestamp("2026-06-10")), [f"h{h}" for h in ifeat.MKT_HOURS]].sum()
    assert mkt == 250 and g.loc[("AAA", pd.Timestamp("2026-06-10")), "n_mkt5"] == 5
    assert g.loc[("BBB", pd.Timestamp("2026-06-10")), "n_mkt5"] == 3   # 质量门会滤掉

    # first_hour = 100/250 = 0.4;midday = (20+30)/250 = 0.2;premkt = 20/270
    assert abs(100 / mkt - 0.4) < 1e-9
    assert abs((20 + 30) / mkt - 0.2) < 1e-9


def test_add_features_alignment_and_missing():
    """join 按 (date,ticker);无数据的行留 NaN,不臆造。"""
    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-06-10", "2026-06-11"]), ["AAA", "BBB"]],
        names=["date", "ticker"])
    panel = pd.DataFrame({"tech_x": np.arange(4, dtype=float)}, index=idx)
    shape = pd.DataFrame({"symbol": ["AAA"], "day": [pd.Timestamp("2026-06-10")],
                          **{c: [0.5] for c in ifeat.FEATURES}})
    out = ifeat.add_intraday_features(panel, shape=shape)
    assert out.loc[(pd.Timestamp("2026-06-10"), "AAA"), ifeat.FEATURES[0]] == 0.5
    assert np.isnan(out.loc[(pd.Timestamp("2026-06-11"), "AAA"), ifeat.FEATURES[0]])
    assert np.isnan(out.loc[(pd.Timestamp("2026-06-10"), "BBB"), ifeat.FEATURES[0]])
    assert (out["tech_x"] == panel["tech_x"]).all(), "原有列被改动"
