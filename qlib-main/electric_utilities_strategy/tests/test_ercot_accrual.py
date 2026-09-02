"""ERCOT macro-accrual loaders — tmp sqlite,零生产读写。"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from electric_utilities_strategy.data import ercot_signals as erc


@pytest.fixture
def macro_db(tmp_path, monkeypatch):
    db = tmp_path / "macro.db"
    con = sqlite3.connect(db)
    con.execute("create table ercot_daily (date text, metric text, value real)")
    days = pd.date_range("2019-01-01", periods=1200, freq="D")
    rows = []
    for i, d in enumerate(days):
        dem = 1000.0 + 0.3 * i + 50 * np.sin(i / 58.0)          # trending demand
        gas = 400.0 + (100.0 if i > 900 else 0.0) + 20 * np.sin(i / 30.0)
        rows += [(d.date().isoformat(), "eia_demand_mwh", dem),
                 (d.date().isoformat(), "eia_gas_gen_mwh", gas),
                 (d.date().isoformat(), "eia_coal_gen_mwh", 150.0), (d.date().isoformat(), "eia_nuclear_gen_mwh", 120.0),
                 (d.date().isoformat(), "eia_solar_gen_mwh", 80.0), (d.date().isoformat(), "eia_wind_gen_mwh", 250.0)]
    for i, d in enumerate(days[-20:]):
        rows.append((d.date().isoformat(), "rt_spp_hubavg", 30.0 + i))
    con.executemany("insert into ercot_daily values (?,?,?)", rows); con.commit(); con.close()
    monkeypatch.setattr(erc, "MACRO_DB", db)
    monkeypatch.setattr(erc, "_macro_accrual_enabled", lambda: True)
    yield db


def test_demand_yoy_positive_and_lagged(macro_db):
    z = erc.load_macro_ercot_demand_yoy()
    assert not z.empty and z.name == "ercot_demand_yoy"
    raw = erc.load_macro_ercot_metric("eia_demand_mwh")
    assert z.index[-1] == raw.index[-1] + pd.Timedelta(days=erc.ACCRUAL_LAG_DAYS)


def test_gas_share_jump_registers(macro_db):
    z = erc.load_macro_ercot_gas_share()
    assert not z.empty and z.loc["2021-10":].max() > 1.0        # +100 MWh gas step at i>900 (≈2021-06)


def test_rt_price_empty_until_enough_history(macro_db):
    assert erc.load_macro_ercot_rt_price().empty              # only 20 days accrued


def test_gate_off_returns_empty(macro_db, monkeypatch):
    monkeypatch.setattr(erc, "_macro_accrual_enabled", lambda: False)
    assert erc.load_macro_ercot_demand_yoy().empty and erc.load_macro_ercot_gas_share().empty
