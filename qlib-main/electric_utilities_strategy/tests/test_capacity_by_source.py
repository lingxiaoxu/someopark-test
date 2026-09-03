"""EIA-860M by_source 修复 — 假行 + tmp 店,零网络零生产写入。"""
from __future__ import annotations

import json

import pandas as pd

from electric_utilities_strategy.data import altdata_signals as alt


def test_by_source_populated_and_renewables_yoy(tmp_path, monkeypatch):
    rows = []
    for i, per in enumerate(pd.period_range("2019-01", "2022-06", freq="M")):
        p = str(per)
        base = 1000.0 + 10 * i
        rows += [{"period": p, "energy_source_code": "SUN", "nameplate-capacity-mw": str(base * 0.2 * (1 + 0.01 * i))},
                 {"period": p, "energy_source_code": "WND", "nameplate-capacity-mw": str(base * 0.3)},
                 {"period": p, "energy_source_code": "NG",  "nameplate-capacity-mw": str(base * 0.5)},
                 {"period": p, "energy_source_code": None,  "nameplate-capacity-mw": "1.0"}]
    monkeypatch.setattr(alt, "_eia_get", lambda path, params: rows)
    monkeypatch.setattr(alt, "CAPACITY_PATH", tmp_path / "eia_capacity_monthly.json")
    n = alt.update_capacity(start="2019-01", refreeze=True)
    assert n == 42
    rec = json.loads((tmp_path / "eia_capacity_monthly.json").read_text())["records"]
    assert set(rec["2020-06"]["by_source"]) == {"SUN", "WND", "NG"}
    assert abs(rec["2020-06"]["total_mw"] - sum(rec["2020-06"]["by_source"].values()) - 1.0) < 0.2
    s = alt.load_renewables_adds_yoy()
    assert not s.empty and s.iloc[-1] > 0                     # solar growing → positive YoY


def test_hyphen_key_still_accepted(tmp_path, monkeypatch):
    rows = [{"period": "2025-01", "energy-source-code": "SUN", "nameplate-capacity-mw": "5"}]
    monkeypatch.setattr(alt, "_eia_get", lambda path, params: rows)
    monkeypatch.setattr(alt, "CAPACITY_PATH", tmp_path / "c.json")
    alt.update_capacity(start="2025-01", refreeze=True)
    rec = json.loads((tmp_path / "c.json").read_text())["records"]
    assert rec["2025-01"]["by_source"] == {"SUN": 5.0}


# ── 截断回归(2026-09-02)──────────────────────────────────────────────────
# 旧代码每月只留装机最大的 12 个来源码。电池 MWH 早年排不进前 12(实测 2019-01
# 排第 24、898 MW)被静默丢掉,而 renewables 求和要 SUN+WND+MWH → t 有电池、
# t-12 没有的那 12 个月 YoY 虚增。下面的夹具让 MWH 正好跨过第 12 名的门槛。
_FILLERS = [f"F{i:02d}" for i in range(10)]          # 10 个 1000MW 的填充码


def _crossing_rows():
    """24 个月:2020 年 MWH=100(旧代码下排第 14 → 被砍),2021 年 MWH=5000(排第 2 → 保留)。"""
    rows = []
    for per in pd.period_range("2020-01", "2021-12", freq="M"):
        p = str(per)
        mwh = 100.0 if per.year == 2020 else 5000.0
        rows.append({"period": p, "energy_source_code": "NG",  "nameplate-capacity-mw": "50000"})
        rows.append({"period": p, "energy_source_code": "WND", "nameplate-capacity-mw": "3000"})
        rows.append({"period": p, "energy_source_code": "SUN", "nameplate-capacity-mw": "2000"})
        rows.append({"period": p, "energy_source_code": "MWH", "nameplate-capacity-mw": str(mwh)})
        rows += [{"period": p, "energy_source_code": f, "nameplate-capacity-mw": "1000"} for f in _FILLERS]
    return rows


def test_small_code_survives_no_truncation(tmp_path, monkeypatch):
    rows = _crossing_rows()
    monkeypatch.setattr(alt, "_eia_get", lambda path, params: rows)
    monkeypatch.setattr(alt, "CAPACITY_PATH", tmp_path / "c.json")
    alt.update_capacity(start="2020-01", refreeze=True)
    rec = json.loads((tmp_path / "c.json").read_text())["records"]
    assert len(rec) == 24
    for m, r in rec.items():
        assert len(r["by_source"]) == 14, f"{m} 被截断到 {len(r['by_source'])} 个码"
        assert "MWH" in r["by_source"], f"{m} 丢了电池码(截断回归)"
        # 不变式:分来源之和 == 总量(本夹具每行都有来源码)
        assert abs(r["total_mw"] - sum(r["by_source"].values())) < 0.5


def test_renewables_yoy_is_true_growth_not_truncation_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(alt, "_eia_get", lambda path, params: _crossing_rows())
    monkeypatch.setattr(alt, "CAPACITY_PATH", tmp_path / "c.json")
    alt.update_capacity(start="2020-01", refreeze=True)
    s = alt.load_renewables_adds_yoy()
    assert not s.empty
    # 真实增长: (2000+3000+5000) / (2000+3000+100) - 1 = +96.0784%
    # 旧的截断版本会算成 10000/5000 - 1 = +100.00%(分母丢了电池)
    assert abs(float(s.iloc[-1]) - 96.0784) < 0.01
    assert float(s.iloc[-1]) < 99.0, "看起来仍是截断口径(分母缺电池)"
