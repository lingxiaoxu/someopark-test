"""数据层单测(合成数据;/tmp 纪律;无网络除单点标注)。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

TMP = Path("/tmp/vp_tests/data_layer")
TMP.mkdir(parents=True, exist_ok=True)


def test_dt_coercion_guards_bson_type_trap():
    """§6.5: 字符串查询静默 0 条 → _dt 必须产 datetime。"""
    from VolumePrediction.data.inhouse_loader import _dt
    assert isinstance(_dt("2019-01-02"), datetime)
    assert isinstance(_dt(date(2019, 1, 2)), datetime)
    assert _dt("2019-01-02") == datetime(2019, 1, 2)


def test_dollar_volume_definition():
    """§7.1: V=shares×vw。"""
    from VolumePrediction.data import polygon_loader as pl
    df = pd.DataFrame({"ticker": ["A"], "v": [100.0], "vw": [2.5],
                       "o": [1], "h": [1], "l": [1], "c": [1],
                       "t": [0], "n": [1], "date": ["2020-01-02"]})
    p = TMP / "grouped_2020-01-02.parquet"
    df.to_parquet(p, index=False)
    orig = pl.RAW_DIR
    try:
        pl.RAW_DIR = TMP
        out = pl.load_day("2020-01-02")
        assert out["dollar_volume"].iloc[0] == 250.0
    finally:
        pl.RAW_DIR = orig


def test_splits_adjust_dollar_invariant(monkeypatch):
    """§7.10: 复权后 价÷f 量×f,美元量不变。"""
    from VolumePrediction.data import splits_loader as sl
    idx = pd.date_range("2026-06-01", periods=10, freq="B")
    df = pd.DataFrame({"o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0,
                       "vw": 100.0, "v": 1000.0}, index=idx)
    df["dollar_volume"] = df["v"] * df["vw"]
    dv_before = df["dollar_volume"].copy()
    split = [{"ticker": "TST", "execution_date": "2026-06-08",
              "split_from": 1, "split_to": 10}]
    monkeypatch.setattr(sl, "splits_for", lambda t, since="2019-01-01": split)
    out, n = sl.adjust(df.copy(), "TST")
    assert n == 1
    pre = out.index < pd.Timestamp("2026-06-08")
    assert np.allclose(out.loc[pre, "c"], 10.0)          # 价 ÷10
    assert np.allclose(out.loc[pre, "v"], 10000.0)       # 量 ×10
    assert np.allclose(out["dollar_volume"], dv_before)  # 美元量不变
    assert np.allclose(out.loc[~pre, "c"], 100.0)


def test_universe_rules_on_synthetic(monkeypatch):
    """§7.3: top-N/价格≥1/IPO 60d/类型过滤 逐规则验证(合成 raw)。"""
    from VolumePrediction.data import polygon_loader as pl
    from VolumePrediction.data import universe as uni
    raw = TMP / "uni_raw"
    raw.mkdir(exist_ok=True)
    days = pl.trading_days("2023-06-25", "2023-06-30")
    asof = days[-1]
    all_days = pl.trading_days("2022-06-30", asof)
    # 4 票: BIG(高量)、PENNY(价<1)、YOUNG(仅 30 天)、ETF1(类型 ETF)
    for i, d in enumerate(all_days):
        rows = [dict(ticker="BIG", v=1e6, vw=50.0, o=50, h=50, l=50, c=50.0,
                     t=0, n=1, date=d),
                dict(ticker="PENNY", v=1e7, vw=0.5, o=.5, h=.5, l=.5, c=0.5,
                     t=0, n=1, date=d)]
        if i >= len(all_days) - 30:
            rows.append(dict(ticker="YOUNG", v=5e6, vw=20.0, o=20, h=20, l=20,
                             c=20.0, t=0, n=1, date=d))
        rows.append(dict(ticker="ETF1", v=2e6, vw=30.0, o=30, h=30, l=30,
                         c=30.0, t=0, n=1, date=d))
        pd.DataFrame(rows).to_parquet(raw / f"grouped_{d}.parquet", index=False)
    monkeypatch.setattr(pl, "RAW_DIR", raw)
    monkeypatch.setattr(uni, "UNI_DIR", TMP / "uni_out")
    ref = pd.DataFrame({"ticker": ["BIG", "PENNY", "YOUNG", "ETF1"],
                        "type": ["CS", "CS", "CS", "ETF"],
                        "active": [True] * 4, "list_date": [None] * 4,
                        "exchange": ["XNYS"] * 4})
    monkeypatch.setattr(uni, "fetch_reference_snapshot", lambda a, force=False: ref)
    members = uni.build_vintage(2023, top_n=10, force=True)
    got = set(members["ticker"])
    assert got == {"BIG"}, got   # PENNY 价滤/YOUNG 60d 滤/ETF1 类型滤


def test_no_yfinance_anywhere():
    import sys
    import VolumePrediction.data.polygon_loader  # noqa: F401
    import VolumePrediction.data.factor_proxy    # noqa: F401
    assert "yfinance" not in sys.modules


def test_key_never_in_error_text(monkeypatch):
    from VolumePrediction.data import polygon_loader as pl
    monkeypatch.setenv("POLYGON_API_KEY", "SECRETKEY123")
    msg = pl._sanitize("boom SECRETKEY123 leaked")
    assert "SECRETKEY123" not in msg and "<KEY>" in msg
