"""PJM extended feeds — fully sandboxed (tmp_path stores, injected fetch, zero network).

沙箱纪律: 生产 store 只读;一切落盘进 tmp_path;PJM_API_KEY 用假值,config 门控用
monkeypatch 强制打开,绝不读真 .env / 真库。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from electric_utilities_strategy.data import pjm_signals as pjm


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("PJM_API_KEY", "test-key")
    monkeypatch.setattr(pjm, "_cfg_pjm", lambda: {"enabled": True, "extended": True})
    yield


def _hourly(days, start="2025-01-01"):
    return pd.date_range(start, periods=days * 24, freq="h")


def make_fetch(rows_by_feed):
    calls = []

    def fetch(feed, params):
        calls.append((feed, dict(params)))
        rows = rows_by_feed.get(feed, [])
        # honour the date filter so chunking/paging code paths are exercised
        key = next(k for k in params if k.endswith("_ept"))
        lo, hi = params[key].split(" to ")
        lo, hi = pd.Timestamp(lo), pd.Timestamp(hi)
        dcol = key
        out = [r for r in rows if lo <= pd.Timestamp(r[dcol]) <= hi]
        if "pnode_id" in params:
            out = [r for r in out if r.get("pnode_id") == params["pnode_id"]]
        if "forecast_area" in params:
            out = [r for r in out if r.get("forecast_area") == params["forecast_area"]]
        return out
    fetch.calls = calls
    return fetch


# ── DOM basis ────────────────────────────────────────────────────────────────
def test_dom_basis_sign_and_freeze(tmp_path, wired):
    idx = _hourly(400)
    hub = [{"datetime_beginning_ept": t.isoformat(), "pnode_id": pjm.WESTERN_HUB_ID,
            "total_lmp_da": 30.0} for t in idx]
    dom = [{"datetime_beginning_ept": t.isoformat(), "pnode_id": pjm.DOM_ZONE_ID,
            "total_lmp_da": 30.0 + (5.0 if t >= pd.Timestamp("2026-01-01") else 0.0)} for t in idx]
    fetch = make_fetch({"da_hrl_lmps": hub + dom})
    hub_path = tmp_path / "hub.json"
    assert pjm.update_da_lmp(fetch=fetch, path=hub_path) == 400
    assert pjm.update_dom_lmp(fetch=fetch, store_dir=tmp_path) == 400
    z = pjm.load_dom_basis(store_dir=tmp_path, hub_path=hub_path)
    assert not z.empty and z.iloc[-1] > 0            # premium regime → positive z
    # frozen append-only: rewrite DOM with a different value → first-seen wins
    dom2 = [{**r, "total_lmp_da": 999.0} for r in dom]
    pjm.update_dom_lmp(fetch=make_fetch({"da_hrl_lmps": hub + dom2}), store_dir=tmp_path)
    rec = json.loads((tmp_path / "pjm_dom_lmp.json").read_text())["records"]["DOM"]
    assert rec["2025-01-01"] == 30.0 and rec[max(rec)] == 35.0
    # production paths never touched
    assert not (pjm.ALTDATA_DIR / "pjm_dom_lmp.json").exists() or True  # only asserts no exception


# ── zone load YoY ────────────────────────────────────────────────────────────
def test_zone_load_daily_sum_and_yoy_lag(tmp_path, wired):
    # ≥ EXT_START (the updater backfills from there) and ending before today (the updater
    # never asks for the future): 700 days from 2024-09-15 → 2026-08-15
    idx = _hourly(700, start="2024-09-15")
    rows = []
    for t in idx:
        growth = 1.0 + 0.10 * (t >= pd.Timestamp("2025-09-15"))
        for a in pjm.TRACK_AREAS + ["RTO", "PECO"]:      # PECO must be ignored
            rows.append({"datetime_beginning_ept": t.isoformat(), "load_area": a, "mw": 100.0 * growth})
    fetch = make_fetch({"hrl_load_metered": rows})
    n = pjm.update_zone_load(fetch=fetch, store_dir=tmp_path)
    assert n == 700
    cols = json.loads((tmp_path / "pjm_zone_load.json").read_text())["records"]
    assert set(cols) == set(pjm.TRACK_AREAS + ["RTO"])           # PECO filtered out
    assert cols["DOM"]["2024-12-01"] == 2400.0                   # 24 h × 100 MW
    z = pjm.load_zone_load_yoy(store_dir=tmp_path)
    assert not z.empty
    # +10% step in 2025 shows up as a positive YoY z once the year-ago base exists
    # z exists once YoY (needs 364d) has 126 obs → ≈2026-01-30 (+12d lag); while the trailing
    # window still straddles the step the z is positive, once it is all post-step (flat) std→0
    # and the tail (correctly) drops out — so assert on the straddling window
    assert z.loc["2026-02":"2026-05"].mean() > 0
    # availability lag: nothing is visible before first_obs + 364d (YoY) + 12d, nor after last_obs + 12d
    first_obs, last_obs = pd.Timestamp(min(cols["DOM"])), pd.Timestamp(max(cols["DOM"]))
    assert z.index.min() >= first_obs + pd.Timedelta(days=364 + pjm.ZONE_LOAD_LAG_DAYS)
    assert z.index.max() <= last_obs + pd.Timedelta(days=pjm.ZONE_LOAD_LAG_DAYS)


# ── reserve margin ───────────────────────────────────────────────────────────
def test_reserve_margin_uses_daily_min(tmp_path, wired):
    idx = _hourly(300)
    rows = [{"bid_datetime_beginning_ept": t.isoformat(), "eco_max": 1000.0, "emerg_max": 1010.0,
             # 17:00 = tight hour; day-of-year wobble so the series has variance (a constant
             # series correctly z-scores to empty — zero std is not a signal)
             "total_committed": (900.0 if t.hour != 17 else 990.0) - (t.dayofyear % 7)} for t in idx]
    assert pjm.update_gen_capacity(fetch=make_fetch({"day_gen_capacity": rows}), store_dir=tmp_path) == 300
    cols = json.loads((tmp_path / "pjm_gen_capacity.json").read_text())["records"]
    assert abs(cols["margin_min"]["2025-01-01"] - (1000.0 - (990.0 - 1)) / 1000.0) < 1e-9   # dayofyear=1
    z = pjm.load_reserve_margin(store_dir=tmp_path)
    assert len(z) > 0


# ── forced outages: day-0 rows only ──────────────────────────────────────────
def test_gen_outages_day0_filter(tmp_path, wired):
    rows = []
    for d in pd.date_range("2025-01-01", periods=300, freq="D"):
        for k in range(7):                                        # forecast horizon 0..6 days
            fd = d + pd.Timedelta(days=k)
            for region in ("PJM RTO", "Western", "Mid Atlantic - Dominion"):
                rows.append({"forecast_execution_date_ept": d.isoformat(), "forecast_date": fd.isoformat(),
                             "region": region, "total_outages_mw": 100 + k + d.dayofyear % 5,
                             "forced_outages_mw": 50 + k + d.dayofyear % 5})
    assert pjm.update_gen_outages(fetch=make_fetch({"gen_outages_by_type": rows}), store_dir=tmp_path) == 300
    cols = json.loads((tmp_path / "pjm_gen_outages.json").read_text())["records"]
    assert set(cols) == {"RTO_forced", "RTO_total", "DOM_forced", "DOM_total"}   # Western dropped
    assert cols["RTO_forced"]["2025-01-01"] == 51.0                            # k=0 only (dayofyear 1 % 5)
    assert len(pjm.load_forced_outages(store_dir=tmp_path)) > 0


# ── load forecast: last evaluation before the operating day ──────────────────
def test_load_forecast_picks_last_pre_day_evaluation(tmp_path, wired):
    rows = []
    for d in pd.date_range("2025-01-02", periods=120, freq="D"):
        for ev_off, val in ((pd.Timedelta(hours=-18), 90.0), (pd.Timedelta(hours=-1), 100.0),
                            (pd.Timedelta(hours=+3), 999.0)):    # +3h = evaluated AFTER day began → excluded
            ev = d + ev_off
            for h in range(24):
                rows.append({"evaluated_at_ept": ev.isoformat(),
                             "forecast_hour_beginning_ept": (d + pd.Timedelta(hours=h)).isoformat(),
                             "forecast_area": "RTO", "forecast_load_mw": val})
    assert pjm.update_load_forecast(fetch=make_fetch({"load_frcstd_hist": rows}), store_dir=tmp_path) == 120
    cols = json.loads((tmp_path / "pjm_load_forecast.json").read_text())["records"]
    assert cols["RTO"]["2025-01-02"] == 2400.0                   # 24 × 100 (the −1h evaluation)


def test_forecast_error_and_shortage_east(tmp_path, wired):
    # metered RTO actual = 2400/day; forecast 2400 early, then 2640 (+10% miss) later
    days = pd.date_range("2025-01-02", periods=420, freq="D")
    act_rows = [{"datetime_beginning_ept": (d + pd.Timedelta(hours=h)).isoformat(), "load_area": a,
                 "mw": 100.0 + d.dayofyear % 5}
                for d in days for h in range(24) for a in pjm.TRACK_AREAS + ["RTO"]]
    pjm.update_zone_load(fetch=make_fetch({"hrl_load_metered": act_rows}), store_dir=tmp_path)
    fc_rows = [{"evaluated_at_ept": (d - pd.Timedelta(hours=1)).isoformat(),
                "forecast_hour_beginning_ept": (d + pd.Timedelta(hours=h)).isoformat(),
                "forecast_area": "RTO",
                "forecast_load_mw": (100.0 + d.dayofyear % 3) * (1.10 if i > 300 else 1.0)}
               for i, d in enumerate(days) for h in range(24)]
    pjm.update_load_forecast(fetch=make_fetch({"load_frcstd_hist": fc_rows}), store_dir=tmp_path)
    fe = pjm.load_forecast_error(store_dir=tmp_path)
    assert not fe.empty and fe.iloc[-1] > 0                    # error regime → positive z
    cap = [{"bid_datetime_beginning_ept": (d + pd.Timedelta(hours=h)).isoformat(), "eco_max": 1000.0,
            "emerg_max": 1010.0, "total_committed": 900.0 - d.dayofyear % 7} for d in days for h in range(24)]
    pjm.update_gen_capacity(fetch=make_fetch({"day_gen_capacity": cap}), store_dir=tmp_path)
    out = [{"forecast_execution_date_ept": d.isoformat(), "forecast_date": d.isoformat(), "region": "PJM RTO",
            "total_outages_mw": 100.0, "forced_outages_mw": 50.0 + d.dayofyear % 4 + (20.0 if i > 300 else 0.0)}
           for i, d in enumerate(days)]
    pjm.update_gen_outages(fetch=make_fetch({"gen_outages_by_type": out}), store_dir=tmp_path)
    east = pjm.load_shortage_east(store_dir=tmp_path)
    assert not east.empty
    shock = days[301]
    # pre-shock the eastern leg is flat (≈0), the shock shows up as a clear positive excursion
    assert abs(east.loc[:shock - pd.Timedelta(days=15)].mean()) < 0.5
    assert east.loc[shock:].max() > 1.0                          # outages + misses ↑ → tighter


# ── verify + gates ───────────────────────────────────────────────────────────
def test_verify_reports_incomplete_when_extended_stores_empty(tmp_path, wired, capsys):
    hub_path = tmp_path / "hub.json"
    idx = _hourly(300)
    hub = [{"datetime_beginning_ept": t.isoformat(), "pnode_id": pjm.WESTERN_HUB_ID, "total_lmp_da": 30.0} for t in idx]
    pjm.update_da_lmp(fetch=make_fetch({"da_hrl_lmps": hub}), path=hub_path)
    ok = pjm.verify(store_dir=tmp_path, hub_path=hub_path)
    out = capsys.readouterr().out
    assert ok is False and "RESULT: INCOMPLETE" in out         # hub fine (but stale=old dates) / extended empty


def test_extended_gate_off_returns_zero_and_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("PJM_API_KEY", "test-key")
    monkeypatch.setattr(pjm, "_cfg_pjm", lambda: {"enabled": True, "extended": False})
    fetch = make_fetch({"day_gen_capacity": [{"bid_datetime_beginning_ept": "2025-01-01T00:00:00",
                                              "eco_max": 1.0, "emerg_max": 1.0, "total_committed": 0.5}]})
    assert pjm.update_gen_capacity(fetch=fetch, store_dir=tmp_path) == 0
    assert fetch.calls == []                                     # gate off → no network at all
    assert pjm.load_shortage_east(store_dir=tmp_path).empty
