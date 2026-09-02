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
