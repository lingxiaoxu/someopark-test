"""W7 scenario tables: definitions, edge cases, and that the checker catches tampering."""
import json

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.w7_scenarios import check, derived, funding, inputs, scenarios, tables
from crypto_trading.crypto_strategies.w7_scenarios.config import COINS, LABELS, ORDER


def _rows(**kw):
    base = dict(coin="BTC", fav="yes", flow_valid=True, flow_1m=0.0, mom_1m_bp=0.0,
                dev8h=0.0, mkt_vr2=1.0, mkt_trend60=0.0)
    base.update(kw)
    return base


def test_labels_priority_boundaries_and_mutual_exclusion():
    cases = [
        (_rows(flow_1m=-0.5, mom_1m_bp=-1, dev8h=5), "大单反向·低波动"),        # flow beats funding
        (_rows(fav="no", flow_1m=0.5, mom_1m_bp=1, mkt_vr2=1.2), "大单反向·高波动"),  # sign flips for NO
        (_rows(flow_1m=0.5, mom_1m_bp=0.001), "大单同向·低波动"),
        (_rows(flow_1m=-0.5, mom_1m_bp=0.0), "横盘·低波动"),                  # zero move is not 大单
        (_rows(flow_valid=False, flow_1m=-0.9, mom_1m_bp=-9), "横盘·低波动"),  # invalid flow never counts
        (_rows(dev8h=0.25), "funding热门方拥挤"),
        (_rows(fav="no", dev8h=-0.25), "funding热门方拥挤"),
        (_rows(dev8h=-0.25), "funding对手方拥挤"),
        (_rows(dev8h=0.2499, mkt_trend60=25), "上涨·低波动"),
        (_rows(mkt_trend60=-25, mkt_vr2=1.2), "下跌·高波动"),
        (_rows(mkt_trend60=24.99, mkt_vr2=1.1999), "横盘·低波动"),
    ]
    df = pd.DataFrame([c for c, _ in cases])
    got = scenarios.label(df)
    assert list(got) == [want for _, want in cases]
    rng = np.random.default_rng(7)
    n = 4000
    r = pd.DataFrame({"coin": "ETH", "fav": rng.choice(["yes", "no"], n), "flow_valid": rng.random(n) > .2,
                      "flow_1m": rng.uniform(-1, 1, n), "mom_1m_bp": rng.choice([-3, 0, 3], n) * rng.random(n),
                      "dev8h": rng.normal(0, .4, n), "mkt_vr2": rng.uniform(.5, 2, n), "mkt_trend60": rng.normal(0, 40, n)})
    lab = scenarios.label(r)
    assert lab.isin(ORDER).all()                                   # exactly one of the 12
    assert (lab == [check._relabel(x) for x in r.itertuples()]).all()   # producer == independent checker


def test_strike_features_gap_handling_and_market_quorum():
    t0 = 1_790_000_100 - (1_790_000_100 % 900)
    rows = []
    for c in COINS:
        for i in range(800):
            if c == "BTC" and 400 <= i < 403:
                continue                                           # 3-window recorder gap
            rows.append(dict(coin=c, close_ts=t0 + 900 * i, strike=100 * (1 + 0.001 * np.sin(i / 7 + len(c)))))
    f = inputs.strike_features(pd.DataFrame(rows))
    b = f[f.coin == "BTC"].set_index("close_ts")
    after = [t0 + 900 * i for i in range(403, 411)]               # 8 windows after the gap
    assert b.loc[after, "vr2"].isna().all()
    assert b.loc[t0 + 900 * 411, "vr2"] == b.loc[t0 + 900 * 411, "vr2"]   # defined again
    assert b.loc[[t0 + 900 * i for i in (404, 405, 406)], "trend60"].isna().all()   # t-60min falls in the gap
    assert not np.isnan(b.loc[t0 + 900 * 403, "trend60"])          # t-60min = window 399, present
    assert not np.isnan(b.loc[t0 + 900 * 407, "trend60"])
    m = f.drop_duplicates("close_ts").set_index("close_ts")
    assert m.loc[t0 + 900 * 405, "mkt_trend60"] == m.loc[t0 + 900 * 405, "mkt_trend60"]  # 4 coins suffice


def test_outcomes_official_overrides_and_unresolved_ties_dropped():
    ts = 1_790_641_800
    f = pd.DataFrame({"coin": ["DOGE", "DOGE", "BTC"], "close_ts": [ts, ts + 900, ts],
                      "strike": [0.094159, 1.0, 100.0], "strike_next": [0.094159, 1.0, 101.0]})
    off = {inputs.ticker("DOGE", ts): 0.0}
    o = inputs.resolve_outcomes(f, off)
    assert o.iloc[0] == 0.0                     # tie, official NO wins over '>='
    assert np.isnan(o.iloc[1])                  # tie without an official result is dropped
    assert o.iloc[2] == 1.0
    assert inputs.ticker("BTC", 1_790_873_100) == "KXBTC15M-26OCT011245-45"
    assert inputs.ticker("BTC", 1_794_762_000) == "KXBTC15M-26NOV151200-00"   # EST after DST ends


def test_funding_cutoff_keeps_milliseconds():
    open_ts = 1_790_870_400                         # exactly on the hour
    times = [open_ts - 3600 * k for k in range(8, 0, -1)] + [open_ts, open_ts + 3600]
    fu = pd.DataFrame({"coin": "BTC", "time_ms": [int(t * 1000) for t in times[:-2]] + [open_ts * 1000, open_ts * 1000 + 36],
                       "funding": [0.0000125] * 8 + [0.0001, 0.01], "premium": 0.0, "source": "api"})
    f = pd.DataFrame({"coin": ["BTC"], "close_ts": [open_ts + 900]})
    dev = inputs.funding_at_open(f, fu).iloc[0]
    expect = ((0.0001 - 0.0000125) * 8 * 1e4) / 8          # stamped exactly at open: included
    assert dev == pytest.approx(expect)                    # +36 ms settlement: excluded


def test_funding_refresh_recorder_fill_then_api_replaces(tmp_path, monkeypatch):
    path = tmp_path / "f.csv"
    now = 1_790_900_000_000
    monkeypatch.setattr(funding, "_now_ms", lambda: now)
    h = (now // funding.HOUR_MS) * funding.HOUR_MS
    monkeypatch.setattr(funding, "_api_history", lambda c, s, e: None)
    monkeypatch.setattr(funding, "_recorder_hours", lambda c, a, u: [dict(coin=c, time_ms=h, funding=1e-5, premium=0.0, source="recorder")])
    funding.refresh(path)
    df = funding.load(path)
    assert set(df.source) == {"recorder"} and len(df) == len(COINS)
    monkeypatch.setattr(funding, "_api_history", lambda c, s, e: [dict(coin=c, time_ms=h + 36, funding=2e-5, premium=0.0, source="api")])
    funding.refresh(path)
    df = funding.load(path)
    assert set(df.source) == {"api"} and len(df) == len(COINS) and (df.funding == 2e-5).all()


def _synthetic_run(tmp_path, monkeypatch):
    rng = np.random.default_rng(3)
    start = 1_790_000_000 - (1_790_000_000 % 900)
    rows = []
    for w in range(96):
        close = start + 900 * (w + 1)
        for c in COINS:
            outcome = float(rng.random() < .5)                     # one official result per coin-window
            for b in LABELS:
                rem = {"T-3.75": 3.7, "T-5.25": 5.2, "T-6.75": 6.7, "T-8.25": 8.2, "T-9.75": 9.7, "T-11.25": 11.2}[b]
                cost = float(rng.uniform(0.8, 0.97))
                rows.append(dict(coin=c, ticker=inputs.ticker(c, close), close_ts=close, recv_ts=close - rem * 60, bin=b,
                                 fav=rng.choice(["yes", "no"]),
                                 flow_valid=True, flow_1m=float(rng.uniform(-1, 1)), mom_1m_bp=float(rng.normal(0, 3)),
                                 dev8h=float(rng.normal(0, .3)), mkt_vr2=float(rng.uniform(.6, 1.8)),
                                 mkt_trend60=float(rng.normal(0, 40)), top_cost=cost, fill25=cost, fill40=cost, fill60=cost,
                                 outcome_yes=outcome))
    lab = pd.DataFrame(rows)
    lab["scen"] = scenarios.label(lab)
    lab = lab.join(scenarios.cost_and_pnl(lab))
    run = tmp_path / "run"; run.mkdir()
    meta = {"start_ts": start, "end_ts": start + 86400, "built_ts": start + 90000, "window_days": 1.0,
            "start_utc": "s", "end_utc": "e", "built_utc": "b", "version": "t", "trades": int(lab.inband.sum()),
            "unclassified_inband": 0, "freshness": {}}
    t = tables.build_tables(lab)
    lab.to_parquet(run / "labelled.parquet", index=False)
    (run / "tables.json").write_text(json.dumps(tables.to_json(t), ensure_ascii=False))
    (run / "tables.md").write_text(tables.render_md(t, meta))
    (run / "meta.json").write_text(json.dumps(meta))
    # funding cache consistent with dev8h (one settlement row per window open, flat 8h mean)
    fu = pd.DataFrame([dict(coin=c, time_ms=int((t_ - 900) * 1000), funding=0.0, premium=0.0, source="api")
                       for c in COINS for t_ in sorted(lab.close_ts.unique())])
    fpath = tmp_path / "fund.csv"; fu.to_csv(fpath, index=False)
    monkeypatch.setattr(check, "FUNDING_CACHE", fpath)
    lab2 = lab.copy()
    # make dev8h equal what the checker will recompute from that cache
    fu2 = fu.assign(t=fu.time_ms / 1000, dev=(fu.funding - 0.0000125) * 8 * 1e4)
    fu2["dev8h"] = fu2.groupby("coin")["dev"].transform(lambda x: x.rolling(8, min_periods=6).mean())
    for i, r in lab2.iterrows():
        h = fu2[(fu2.coin == r.coin) & (fu2.t <= r.close_ts - 900)]
        lab2.at[i, "dev8h"] = h.dev8h.iloc[-1] if len(h) else np.nan
    lab2 = lab2[lab2.dev8h.notna()].copy()
    lab2["scen"] = scenarios.label(lab2)
    t = tables.build_tables(lab2)
    lab2.to_parquet(run / "labelled.parquet", index=False)
    (run / "tables.json").write_text(json.dumps(tables.to_json(t), ensure_ascii=False))
    (run / "tables.md").write_text(tables.render_md(t, meta))
    best = derived.best_times(json.loads((run / "tables.json").read_text()))
    (run / "derived.json").write_text(json.dumps(best, ensure_ascii=False))
    (run / "derived.md").write_text(derived.render_md(best, meta))
    off = {check._ticker(c, ts): bool(o) for c, ts, o in zip(lab2.coin, lab2.close_ts, lab2.outcome_yes)}
    monkeypatch.setattr(check, "_official", lambda: off)
    return run


def test_checker_passes_clean_run_and_catches_tampering(tmp_path, monkeypatch):
    run = _synthetic_run(tmp_path, monkeypatch)
    res = check.run_checks(run, freshness=False, flow_samples=0, raw_samples=0, coverage=False)
    assert res["ok"], res["errors"]
    # 1) one table number changed in the markdown
    md = (run / "tables.md").read_text()
    lines = md.split("\n")
    k = next(i for i, l in enumerate(lines) if l.startswith("| 全部 |"))         # BTC 全部 row (always populated)
    parts = lines[k].split("|")
    j = next(i for i in range(3, len(parts)) if parts[i].strip() not in ("—", ""))
    parts[j] = " +9.99c (+9.9) "
    lines[k] = "|".join(parts)
    assert "\n".join(lines) != md
    (run / "tables.md").write_text("\n".join(lines))
    assert not check.run_checks(run, freshness=False, flow_samples=0, raw_samples=0, coverage=False)["ok"]
    (run / "tables.md").write_text(md)
    # 2) one scenario label changed in the data
    lab = pd.read_parquet(run / "labelled.parquet")
    i = lab.index[lab.scen != "横盘·低波动"][0]
    lab.at[i, "scen"] = "横盘·低波动"
    lab.to_parquet(run / "labelled.parquet", index=False)
    res = check.run_checks(run, freshness=False, flow_samples=0, raw_samples=0, coverage=False)
    assert not res["ok"] and any("decision list" in e for e in res["errors"])


def test_cell_statistics_match_independent_checker():
    rng = np.random.default_rng(11)
    g = pd.DataFrame({"close_ts": rng.integers(0, 60, 400) * 900, "pnl": rng.normal(1, 30, 400)})
    ref = pd.concat([g, pd.DataFrame({"close_ts": rng.integers(0, 60, 300) * 900, "pnl": rng.normal(0, 30, 300)})])
    a = tables.cell(g, ref)
    b = check._cell(g, ref)
    assert a["mean"] == pytest.approx(b[0]) and a["t"] == pytest.approx(b[1]) and a["n"] == b[2]
    assert tables.fmt(a) == check._fmt(b)
    assert tables.cell(g.head(29), ref) is None and check._cell(g.head(29), ref) is None


def test_freshness_is_per_coin_and_maintenance_aware(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    run = _synthetic_run(tmp_path, monkeypatch)
    meta = json.loads((run / "meta.json").read_text())
    built = meta["built_ts"]
    fresh = {c: built - 60 for c in COINS}
    meta["freshness"] = {"strips_newest_recv": dict(fresh, XRP=built - 5000), "hl_trades_newest_recv": fresh}
    (run / "meta.json").write_text(json.dumps(meta))
    res = check.run_checks(run, freshness=True, flow_samples=0, raw_samples=0, coverage=False)
    assert any(e.startswith("STALE: Kalshi 15M recorder for XRP") for e in res["errors"])   # one coin silent
    # the same silence inside Kalshi's Thursday 03:00-05:00 ET maintenance is only a warning
    thu = datetime(2026, 10, 8, 4, 10, tzinfo=ZoneInfo("America/New_York")).timestamp()
    shift = thu - built
    meta["built_ts"] = thu
    meta["freshness"] = {"strips_newest_recv": {c: thu - 5000 for c in COINS},
                         "hl_trades_newest_recv": {c: thu - 60 for c in COINS}}
    (run / "meta.json").write_text(json.dumps(meta))
    fu = pd.read_csv(check.FUNDING_CACHE)
    fu["time_ms"] = fu.time_ms + int(shift * 1000)            # keep funding fresh relative to the new build time
    fu.to_csv(check.FUNDING_CACHE, index=False)
    lab = pd.read_parquet(run / "labelled.parquet")
    res = check.run_checks(run, freshness=True, flow_samples=0, raw_samples=0, coverage=False)
    assert not any("Kalshi 15M recorder" in e for e in res["errors"])
    assert any("Thursday maintenance" in w for w in res["warnings"])


def _cellv(mean, n=200, t=1.0):
    return {"mean": mean, "t": t, "n": n, "windows": n}


def _tables_with(btc_row: dict, pooled_row: dict) -> dict:
    empty = {b: None for b in tables.COLS}
    t = {co: {sc: {"share": 0.0, "cells": dict(empty)} for sc in tables.ROWS} for co in list(COINS) + ["5币合计"]}
    t["BTC"]["上涨·低波动"]["cells"].update(btc_row)
    t["5币合计"]["上涨·低波动"]["cells"].update(pooled_row)
    return t


def test_derived_best_time_rule():
    # own row partly available -> best among the coin's OWN available bins, pooled ignored
    t = _tables_with({"T-11.25": _cellv(1.0), "T-6.75": _cellv(3.0)}, {"T-9.75": _cellv(9.0)})
    r = derived.best_times(t)["BTC"]["上涨·低波动"]
    assert r["best"] == r["pick"] == "T-6.75" and r["source"] == "own" and r["action"] == "trade"
    # own row empty -> pooled row's best, flagged
    t = _tables_with({}, {"T-9.75": _cellv(2.0), "T-11.25": _cellv(-1.0)})
    r = derived.best_times(t)["BTC"]["上涨·低波动"]
    assert r["pick"] == "T-9.75" and r["source"] == r["pick_source"] == "pooled"
    # both empty -> no answer
    assert derived.best_times(_tables_with({}, {}))["BTC"]["上涨·低波动"]["best"] is None
    # exact tie -> the bin closest to the live T-8.25
    t = _tables_with({"T-11.25": _cellv(2.0), "T-9.75": _cellv(2.0), "T-6.75": _cellv(2.0)}, {})
    assert derived.best_times(t)["BTC"]["上涨·低波动"]["pick"] == "T-9.75"
    # every shown bin loses -> skip; the least-bad bin is still recorded
    r = derived.best_times(_tables_with({"T-6.75": _cellv(-2.0), "T-5.25": _cellv(-5.0)}, {}))["BTC"]["上涨·低波动"]
    assert r["best"] == "T-6.75" and r["action"] == "skip" and r["mult"] == 0.0


def test_derived_never_later_than_t525_and_size_tiers():
    # T-3.75 best -> T-5.25 of the same table
    r = derived.best_times(_tables_with({"T-3.75": _cellv(8.0), "T-5.25": _cellv(2.0), "T-9.75": _cellv(5.0)}, {}))
    r = r["BTC"]["上涨·低波动"]
    assert r["best"] == "T-3.75" and r["pick"] == "T-5.25" and r["pick_cell"]["mean"] == 2.0 and r["action"] == "trade"
    # ... skipped when T-5.25 loses
    r = derived.best_times(_tables_with({"T-3.75": _cellv(8.0), "T-5.25": _cellv(-2.0)}, {}))["BTC"]["上涨·低波动"]
    assert r["pick"] == "T-5.25" and r["action"] == "skip"
    # ... pooled T-5.25 when the coin's table does not show one
    r = derived.best_times(_tables_with({"T-3.75": _cellv(8.0)}, {"T-5.25": _cellv(1.0)}))["BTC"]["上涨·低波动"]
    assert r["pick"] == "T-5.25" and r["pick_source"] == "pooled" and r["action"] == "trade"
    # T-11.25 best -> T-9.75 (user 2026-10-02: entries only between T-9.75 and T-5.25)
    r = derived.best_times(_tables_with({"T-11.25": _cellv(8.0), "T-9.75": _cellv(1.5), "T-6.75": _cellv(1.0)}, {}))["BTC"]["上涨·低波动"]
    assert r["best"] == "T-11.25" and r["pick"] == "T-9.75" and r["pick_cell"]["mean"] == 1.5 and r["action"] == "trade"
    r = derived.best_times(_tables_with({"T-11.25": _cellv(8.0), "T-9.75": _cellv(-1.0)}, {}))["BTC"]["上涨·低波动"]
    assert r["pick"] == "T-9.75" and r["action"] == "skip"
    r = derived.best_times(_tables_with({"T-11.25": _cellv(8.0)}, {"T-9.75": _cellv(2.0)}))["BTC"]["上涨·低波动"]
    assert r["pick"] == "T-9.75" and r["pick_source"] == "pooled" and r["action"] == "trade"
    best = derived.best_times(_tables_with({"T-11.25": _cellv(8.0), "T-9.75": _cellv(1.5)}, {}))
    md = derived.render_md(best, {"window_days": 14, "start_utc": "s", "end_utc": "e", "built_utc": "b"})
    assert "| 上涨·低波动 | T-9.75 ×0.5 / — |" in md      # clamped bin shown (x0.5: its 1.5c is the bottom decile of 1 pick); no top2
    # tiers: many coin cells so the percentiles are well defined
    empty = {b: None for b in tables.COLS}
    t = {co: {sc: {"share": 0.0, "cells": dict(empty)} for sc in tables.ROWS} for co in list(COINS) + ["5币合计"]}
    vals = iter(range(1, 61))
    for co in COINS:
        for sc in tables.ORDER if hasattr(tables, "ORDER") else tables.ROWS[:-1]:
            t[co][sc]["cells"]["T-8.25"] = _cellv(float(next(vals)))
    best = derived.best_times(t)
    th = best["_thresholds"]
    assert abs(th["hi_c"] - 54.1) < 1e-9 and abs(th["lo_c"] - 6.9) < 1e-9
    m = {best[co][sc]["pick_cell"]["mean"]: best[co][sc]["mult"] for co in COINS for sc in tables.ROWS[:-1]}
    assert m[55.0] == 1.5 and m[54.0] == 1.0 and m[7.0] == 1.0 and m[6.0] == 0.5
    md = derived.render_md(best, {"window_days": 14, "start_utc": "s", "end_utc": "e", "built_utc": "b"})
    assert "T-8.25 ×1.5" in md and "T-8.25 ×0.5" in md


def test_derived_top2_is_the_best_other_allowed_bin_of_the_same_table():
    # top1 T-6.75; top2 = best OTHER allowed bin (T-9.75 at 2.0): T-3.75 (2.5) and T-11.25 are never taken
    t = _tables_with({"T-11.25": _cellv(1.0), "T-9.75": _cellv(2.0), "T-6.75": _cellv(3.0), "T-3.75": _cellv(2.5),
                      "T-5.25": _cellv(1.0)}, {})
    r = derived.best_times(t)["BTC"]["上涨·低波动"]
    assert r["pick"] == "T-6.75" and r["top2"]["pick"] == "T-9.75" and r["top2"]["source"] == "own"
    assert r["top2"]["mult"] == 0.5                       # the only traded pick is 3.0 -> lo = 3.0 -> 2.0 is "bottom decile"
    # top1 skipped (clamped bin loses) but top2 still taken, as traded live
    r = derived.best_times(_tables_with({"T-11.25": _cellv(9.0), "T-9.75": _cellv(-1.0), "T-5.25": _cellv(0.5)}, {}))["BTC"]["上涨·低波动"]
    assert r["action"] == "skip" and r["top2"]["pick"] == "T-5.25"
    # no positive other bin -> no top2; pooled row -> pooled top2
    assert derived.best_times(_tables_with({"T-9.75": _cellv(2.0), "T-6.75": _cellv(-0.5)}, {}))["BTC"]["上涨·低波动"]["top2"] is None
    r = derived.best_times(_tables_with({}, {"T-9.75": _cellv(2.0), "T-5.25": _cellv(1.0)}))["BTC"]["上涨·低波动"]
    assert r["pick_source"] == "pooled" and r["top2"] == {"pick": "T-5.25", "cell": _cellv(1.0), "source": "pooled", "mult": 0.5}
    md = derived.render_md(derived.best_times(t), {"window_days": 14, "start_utc": "s", "end_utc": "e", "built_utc": "b"})
    assert "| 上涨·低波动 | T-6.75 ×1.5 / T-9.75 ×0.5 |" in md


def test_checker_catches_a_wrong_derived_answer(tmp_path, monkeypatch):
    run = _synthetic_run(tmp_path, monkeypatch)
    assert check.run_checks(run, freshness=False, flow_samples=0, raw_samples=0, coverage=False)["ok"]
    dj = json.loads((run / "derived.json").read_text())
    r = dj["BTC"]["全部"]
    r["best"] = next(b for b in tables.COLS if b != r["best"])
    (run / "derived.json").write_text(json.dumps(dj, ensure_ascii=False))
    res = check.run_checks(run, freshness=False, flow_samples=0, raw_samples=0, coverage=False)
    assert not res["ok"] and any("derived" in e for e in res["errors"])
    # ... and a wrong top2
    dj = json.loads((run / "derived.json").read_text())
    r = dj["BTC"]["全部"]
    r["best"] = next(b for b in tables.COLS if b != r["best"]) if False else r["best"]
    r["top2"] = {"pick": "T-9.75", "cell": r["pick_cell"], "source": "own", "mult": 1.0} if not r.get("top2") else None
    (run / "derived.json").write_text(json.dumps(dj, ensure_ascii=False))
    res = check.run_checks(run, freshness=False, flow_samples=0, raw_samples=0, coverage=False)
    assert not res["ok"] and any("derived" in e for e in res["errors"])


def test_cli_times_are_utc_even_without_a_zone():
    from crypto_trading.crypto_strategies.w7_scenarios import run
    assert run._ts("2026-10-01T09:00:00") == run._ts("2026-10-01T09:00:00Z") == 1790845200.0
    assert run._ts("2026-10-01T05:00:00-04:00") == 1790845200.0
