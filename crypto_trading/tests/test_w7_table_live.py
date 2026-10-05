"""W7 table-driven prod entries (2026-10-02): decisions, T-8 gating, sizing, fallbacks.
No network: armed=False everywhere, books and the table are faked."""
import json
import time

import pytest

from crypto_trading.crypto_common.execution_events import EventExecutionRouter
from crypto_trading.crypto_strategies.w7_scenarios import live_plan, tables
from crypto_trading.crypto_strategies.w7_scenarios.config import COINS


def _cell(mean, n=200):
    return {"mean": mean, "t": 1.0, "n": n, "windows": n}


def _table(rows: dict, pooled: dict | None = None):
    empty = {b: None for b in tables.COLS}
    t = {co: {sc: {"share": 0.0, "cells": dict(empty)} for sc in tables.ROWS} for co in list(COINS) + ["5币合计"]}
    for (co, sc), cells in rows.items():
        t[co][sc]["cells"].update(cells)
    for sc, cells in (pooled or {}).items():
        t["5币合计"][sc]["cells"].update(cells)
    return t


def test_rules_top1_top2_cap_and_tiers():
    filler = {(co, sc): {"T-8.25": _cell(3.0)} for co in COINS for sc in tables.ROWS[:-1]}
    filler[("BTC", "上涨·低波动")] = {"T-3.75": _cell(20.0), "T-5.25": _cell(9.0), "T-9.75": _cell(4.0)}
    filler[("ETH", "上涨·低波动")] = {"T-11.25": _cell(-1.0), "T-6.75": _cell(-2.0)}
    filler[("SOL", "上涨·低波动")] = {"T-9.75": _cell(0.1), "T-6.75": _cell(0.05)}
    filler[("DOGE", "上涨·低波动")] = {"T-11.25": _cell(9.0), "T-9.75": _cell(3.0), "T-3.75": _cell(8.0), "T-5.25": _cell(2.5)}
    R = live_plan.rules_from(_table(filler))
    doge = {s["slot"]: s for s in R["DOGE"]["上涨·低波动"]}
    assert doge["top1"]["pick"] == "T-9.75" and doge["top2"]["pick"] == "T-5.25"   # both ends clamped into range
    btc = {s["slot"]: s for s in R["BTC"]["上涨·低波动"]}
    assert btc["top1"]["pick"] == "T-5.25" and btc["top1"]["mult"] == 1.5      # T-3.75 capped to T-5.25, top decile
    assert btc["top2"]["pick"] == "T-9.75" and btc["top2"]["mult"] == 1.0      # next distinct bin, never x1.5
    assert R["ETH"]["上涨·低波动"] == []                                       # top1 loses -> skip; top2 negative -> none
    sol = {s["slot"]: s for s in R["SOL"]["上涨·低波动"]}
    assert sol["top1"]["mult"] == 0.5 and sol["top2"]["mult"] == 0.5           # bottom-decile values halve


def test_scanner_never_fires_at_t11():
    assert live_plan.bin_due(11.25) is None and live_plan.bin_for(11.25) is None
    assert set(live_plan.BIN_CENTRES) == {"T-9.75", "T-8.25", "T-6.75", "T-5.25"}


def test_scanner_fires_at_the_bin_centre_not_early():
    assert live_plan.bin_due(9.75 + 0.5) is None                 # the old trigger fired here (~30 s early)
    assert live_plan.bin_due(9.75 + 0.09) == "T-9.75"            # first tick within 6 s of the centre
    assert live_plan.bin_due(9.75 - 0.5) == "T-9.75"             # a delayed tick still fires
    assert live_plan.bin_due(9.75 - 0.7) is None
    assert live_plan.bin_due(5.25 - 0.3) == "T-5.25" and live_plan.bin_due(3.75) is None


def test_bin_for_and_ticker_clock():
    assert live_plan.bin_for(9.5) == "T-9.75" and live_plan.bin_for(8.0) == "T-8.25"
    assert live_plan.bin_for(5.0) == "T-5.25" and live_plan.bin_for(3.75) is None   # never later than T-5.25
    assert live_plan.bin_for(10.5) is None                                           # between bins
    assert live_plan.close_ts_from_ticker("KXBTC15M-26NOV151200-00") == 1_794_762_000  # EST after fall-back


def test_ticker_clock_in_the_dst_fall_back_hour_takes_the_instant_nearest_to_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    first = int(datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=0).timestamp())    # 01:30 EDT = 05:30 UTC
    second = int(datetime(2026, 11, 1, 1, 30, tzinfo=ny, fold=1).timestamp())   # 01:30 EST = 06:30 UTC
    assert second - first == 3600
    tk = "KXBTC15M-26NOV010130-00"
    assert live_plan.close_ts_from_ticker(tk, now=first - 8 * 60) == first      # W7's T-8 call in the first pass
    assert live_plan.close_ts_from_ticker(tk, now=second - 8 * 60) == second    # ... and in the repeated hour
    assert live_plan.close_ts_from_ticker(tk, now=first + 600) == first         # just after the first close


def _plan(monkeypatch, rules, ok=True):
    monkeypatch.setattr(live_plan, "load_plan", lambda now=None: (
        {"ok": True, "run": "R", "age_s": 60, "rules": rules} if ok else {"ok": False, "reason": "table_stale"}))


def test_decide_trade_skip_and_fallbacks(monkeypatch):
    rules = {"BTC": {"上涨·低波动": [dict(slot="top1", pick="T-9.75", mult=1.5, v=7.0),
                                    dict(slot="top2", pick="T-8.25", mult=1.0, v=2.0)]}}
    _plan(monkeypatch, rules)
    monkeypatch.setattr(live_plan, "classify", lambda c, ct, s, now=None: {"scenario": "上涨·低波动", "inputs": {}})
    d = live_plan.decide("BTC", 0, "T-9.75", "yes")
    assert d["mult"] == 1.0 and d["mult_table"] == 1.5                  # 2026-10-05: no x1.5 (table value kept for audit)
    d = live_plan.decide("BTC", 0, "T-8.25", "yes"); assert d["action"] == "trade" and d["slot"] == "top2"
    assert live_plan.decide("BTC", 0, "T-6.75", "yes")["action"] == "skip"
    monkeypatch.setattr(live_plan, "classify", lambda c, ct, s, now=None: {"scenario": None, "missing": ["dev8h"], "inputs": {}})
    assert live_plan.decide("BTC", 0, "T-8.25", "yes")["action"] == "fallback"
    _plan(monkeypatch, rules, ok=False)
    assert live_plan.decide("BTC", 0, "T-8.25", "yes")["reason"] == "table_stale"


def test_load_plan_marks_old_table_stale(monkeypatch, tmp_path):
    out = tmp_path / "w7s"; (out / "latest").mkdir(parents=True)
    t = _table({(co, sc): {"T-8.25": _cell(1.0)} for co in COINS for sc in tables.ROWS[:-1]})
    (out / "latest" / "tables.json").write_text(json.dumps(t))
    (out / "latest" / "meta.json").write_text(json.dumps({"built_ts": time.time() - 7 * 3600}))
    (out / "status.json").write_text(json.dumps({"ok": True, "run": "X"}))
    monkeypatch.setattr(live_plan, "OUT", out)
    monkeypatch.setitem(live_plan._PLAN, "sig", None)
    p = live_plan.load_plan()
    assert not p["ok"] and p["reason"] == "table_stale"
    (out / "latest" / "meta.json").write_text(json.dumps({"built_ts": time.time() - 60}))
    p = live_plan.load_plan()
    assert p["ok"] and p["rules"]["BTC"]["上涨·低波动"][0]["pick"] == "T-8.25"


LIVE_CFG = {"w7_noisefade": {"enabled": True, "window_cap_mult": 1.5}}      # = production
PROD_BAND = {"w7_noisefade": (0.79, 0.98)}


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG)
    monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    r = EventExecutionRouter(strategy="w7_noisefade")
    monkeypatch.setattr(r, "_utc_hour", lambda: 15)                         # day band: BTC 40, others 30 (v3)
    monkeypatch.setattr(r, "_trend_bp", lambda coin, lb: 0.0)                # no dump
    monkeypatch.setattr(r, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    return r


def test_t8_call_follows_the_table(router, monkeypatch):
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "trade", "slot": "top1", "mult": 1.5,
                                                              "scenario": "S", "bin": "T-8.25"})
    out = router.submit(ticker="KXBTC15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=False)
    assert out["status"] == "live_disarmed" and out["contracts"] == 40 and out["entry_source"] == "table_top1_T-8.25"
    assert out["size_mult"] == 1.0 and out["size_mult_table"] == 1.5      # 2026-10-05: x1.5 capped at the base size
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "skip", "reason": "table_not_this_bin"})
    out = router.submit(ticker="KXETH15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=False)
    assert out["status"] == "skipped_by_table"
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "fallback", "reason": "table_stale"})
    out = router.submit(ticker="KXETH15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=False)
    assert out["contracts"] == 30 and out["entry_source"] == "t8_fallback" and out["size_mult"] == 1.0
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = router.submit(ticker="KXETH15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=False)
    assert out["contracts"] == 30 and out["entry_source"] == "t8_fallback"   # a table error never blocks T-8


def test_scanner_orders_and_dump_compose(router, monkeypatch):
    monkeypatch.setattr(router, "_trend_bp", lambda coin, lb: -80.0)        # fresh dump on a day NO order
    out = router.submit(ticker="KXSOL15M-26OCT021945-45", side="no", entry_price=0.85, contracts=25, armed=False,
                        size_mult=0.5, entry_source="table_top2_T-9.75", table_decision={"action": "trade"})
    assert out["contracts"] == 4 and out["limit_buffer_c"] == 0               # 30 x0.25 -> 8, x0.5, no +1c on dump
    monkeypatch.setattr(router, "_trend_bp", lambda coin, lb: 0.0)
    monkeypatch.setattr(router, "_utc_hour", lambda: 2)                      # night band
    out = router.submit(ticker="KXBTC15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=False,
                        size_mult=1.5, entry_source="table_top1_T-11.25", table_decision={"action": "trade"})
    assert out["contracts"] == 20 and out["price_dollars"] == 0.86           # night 20, x1.5 capped to x1 (2026-10-05); +1c kept


def test_scanner_run_once_per_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG)
    monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    from crypto_trading.crypto_strategies.live_watch import w7_table, common, w7_noisefade as w7
    monkeypatch.setattr(w7_table, "STATE", tmp_path / "s.json")
    close = 1_790_000_100 - (1_790_000_100 % 900) + 900
    monkeypatch.setattr(w7_table.time, "time", lambda: close - 9.70 * 60)
    monkeypatch.setattr(live_plan, "maybe_refresh_funding", lambda now=None: None)
    book = {"yes_bid": 0.84, "yes_ask": 0.86, "yes": {"fill_cost": 0.86, "top_cost": 0.86},
            "no": {"fill_cost": 0.16, "top_cost": 0.16}}
    monkeypatch.setattr(w7, "walk_book_both", lambda t, n: book)
    calls, logs = [], []
    monkeypatch.setattr(live_plan, "decide", lambda coin, ct, b, side, now=None, **k: (
        {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"} if coin == "BTC" else {"action": "skip"}))
    monkeypatch.setattr(common, "mirror_async", lambda name, fn, **kw: calls.append(kw) or "dispatched_async")
    monkeypatch.setattr(common, "log_line", lambda name, row: logs.append(row))
    rep = w7_table.run({"w7_noisefade": {"live_orders": True}})
    assert rep["bin"] == "T-9.75" and len(calls) == 1 and calls[0]["entry_source"] == "table_top1_T-9.75"
    assert calls[0]["armed"] is True and calls[0]["side"] == "yes" and calls[0]["entry_price"] == 0.86
    assert w7_table.run({"w7_noisefade": {"live_orders": True}})["coins"] == {}          # no second attempt
    monkeypatch.setattr(w7_table.time, "time", lambda: close - 8.0 * 60)
    assert w7_table.run({})["status"] == "IDLE"                                         # T-8 belongs to W7's call


def test_table_switch_off_restores_the_old_t8_path(monkeypatch):
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", {"w7_noisefade": {"enabled": False}})
    r = EventExecutionRouter(strategy="w7_noisefade")
    monkeypatch.setattr(r, "_utc_hour", lambda: 15)
    monkeypatch.setattr(r, "_trend_bp", lambda coin, lb: 0.0)
    monkeypatch.setattr(r, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be asked")))
    out = r.submit(ticker="KXETH15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=False)
    assert out["contracts"] == 30 and "entry_source" not in out
    from crypto_trading.crypto_strategies.live_watch import w7_table
    assert w7_table.run({})["status"] == "OFF"


def test_one_top1_and_one_top2_per_window_even_if_the_scenario_changes(armed_router, monkeypatch):
    """2026-10-02 live: BTC fired top1 at T-11.25 and, after the scenario changed,
    another top1 at T-6.75 (and a second top2). The backtest uses each slot once."""
    router = armed_router
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", {"w7_noisefade": {"enabled": True}})   # no cap here
    tk = "KXBTC15M-26OCT021645-45"
    def table(slot, mult):
        return {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    # an audit-only (disarmed) row never consumes a slot
    out = router.submit(ticker=tk, side="yes", entry_price=0.8, contracts=25, armed=False,
                        size_mult=1.5, entry_source="table_top1_T-11.25", table_decision=table("top1", 1.5))
    assert out["status"] == "live_disarmed" and out["contracts"] == 40 and live_plan.used_slots(tk) == set()
    out = router.submit(ticker=tk, side="yes", entry_price=0.8, contracts=25, armed=True,
                        size_mult=1.5, entry_source="table_top1_T-11.25", table_decision=table("top1", 1.5))
    assert out["status"] == "live_sent" and out["contracts"] == 40 and live_plan.used_slots(tk) == {"top1"}
    out = router.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True,
                        size_mult=1.0, entry_source="table_top1_T-6.75", table_decision=table("top1", 1.0))
    assert out["status"] == "skipped_by_table" and "top1_already_used" in out["slot_reason"]
    out = router.submit(ticker=tk, side="yes", entry_price=0.81, contracts=25, armed=True,
                        size_mult=1.0, entry_source="table_top2_T-9.75", table_decision=table("top2", 1.0))
    assert out["status"] == "live_sent"
    assert router.submit(ticker=tk, side="yes", entry_price=0.93, contracts=25, armed=True, size_mult=0.5,
                         entry_source="table_top2_T-5.25", table_decision=table("top2", 0.5))["status"] == "skipped_by_table"
    assert len(_FakeProd.sent) == 2
    # a flow-gate skip does not use the slot
    monkeypatch.setattr(router, "_flow_gate_decision", lambda t, s: {"decision": "skip"})
    tk2 = "KXETH15M-26OCT021645-45"
    assert router.submit(ticker=tk2, side="yes", entry_price=0.8, contracts=25, armed=True, size_mult=1.0,
                         entry_source="table_top1_T-9.75", table_decision=table("top1", 1.0))["status"] == "skipped_by_flow_gate"
    assert live_plan.used_slots(tk2) == set()
    # a refused order (gate closed) does not use the slot either
    monkeypatch.setattr(router, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    monkeypatch.setattr(router, "gate_status", lambda: {"live_open": False, "why": "test"})
    from crypto_trading.crypto_common.execution_events import LiveOrderRefused
    with pytest.raises(LiveOrderRefused):
        router.submit(ticker=tk2, side="yes", entry_price=0.8, contracts=25, armed=True, size_mult=1.0,
                      entry_source="table_top1_T-9.75", table_decision=table("top1", 1.0))
    assert live_plan.used_slots(tk2) == set()
    # decide() reports the used slot as a skip before any book work
    monkeypatch.setattr(live_plan, "load_plan", lambda now=None: {"ok": True, "run": "R", "age_s": 1,
        "rules": {"BTC": {"S2": [dict(slot="top1", pick="T-6.75", mult=1.0, v=3.0)]}}})
    monkeypatch.setattr(live_plan, "classify", lambda c, ct, s, now=None: {"scenario": "S2", "inputs": {}})
    d = live_plan.decide("BTC", 0, "T-6.75", "yes", ticker=tk)
    assert d["action"] == "skip" and d["reason"] == "top1_already_used_this_window"


class _FakeProd:
    """Prod order client stub: every IOC fills in full, like Kalshi's response shape."""
    sent: list = []

    def __init__(self, *a, **k):
        pass

    def create_order(self, *, ticker, side, count, price_dollars, tif):
        _FakeProd.sent.append(dict(ticker=ticker, side=side, count=count, price=price_dollars))
        return {"status_code": 201, "response": json.dumps({"fill_count": f"{count:.2f}", "average_fill_price": f"{price_dollars:.4f}",
                                                            "average_fee_paid": "0.0100", "order_id": "x"})}


@pytest.fixture
def armed_router(router, monkeypatch):
    _FakeProd.sent = []
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", _FakeProd)
    monkeypatch.setattr(router, "gate_status", lambda: {"live_open": True})
    monkeypatch.setattr(router, "_spawn_user_mirror", lambda intent: None)
    return router


def test_prod_band_floor_079_only_for_real_orders(router, monkeypatch):
    """User 2026-10-02: prod entries need the favourite >= 0.79; paper/demo keep 0.78."""
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "fallback", "reason": "table_stale"})
    out = router.submit(ticker="KXETH15M-26OCT021945-45", side="yes", entry_price=0.7849, contracts=25, armed=False)
    assert out["status"] == "skipped_by_prod_band" and out["prod_band"] == [0.79, 0.98]
    out = router.submit(ticker="KXETH15M-26OCT021945-45", side="yes", entry_price=0.79, contracts=25, armed=False)
    assert out["status"] == "live_disarmed" and out["price_dollars"] == 0.80          # +1c buffer still applied
    out = router.submit(ticker="KXETH15M-26OCT021945-45", side="no", entry_price=0.985, contracts=25, armed=False,
                        size_mult=1.0, entry_source="table_top1_T-6.75", table_decision={"action": "trade"})
    assert out["status"] == "skipped_by_prod_band"
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    assert w7.MAIN_LO == 0.78                                                          # paper book untouched


def test_window_cap_top1_plus_top2_at_most_1_5x_base(armed_router):
    """top1 + top2 bought on one coin-window <= 1.5 x base: BTC day 60, others 45. Since 2026-10-05 no
    single entry exceeds the base size (x1.5 requests are capped at x1), so the cap binds on the 2nd entry."""
    r = armed_router; tk = "KXBTC15M-26OCT021945-45"
    t = lambda slot, mult: {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    out = r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                   entry_source="table_top2_T-9.75", table_decision=t("top2", 1.0))
    assert out["status"] == "live_sent" and out["contracts"] == 40 and out["window_cap"]["total"] == 40
    out = r.submit(ticker=tk, side="yes", entry_price=0.86, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-6.75", table_decision=t("top1", 1.5))
    assert out["status"] == "live_sent" and out["contracts"] == 20                     # 40 wanted (x1.5 -> x1), 20 left under the 60 cap
    assert out["window_cap"] == {"cap": 60, "already": 40.0, "trimmed_from": 40, "bought": 20.0, "total": 60.0}
    assert out["size_mult"] == 1.0 and out["size_mult_table"] == 1.5
    assert [o["count"] for o in _FakeProd.sent] == [40, 20]
    # a third attempt on the same window: slots are used AND the cap is full
    out = r.submit(ticker=tk, side="yes", entry_price=0.86, contracts=25, armed=True, size_mult=1.0,
                   entry_source="table_top1_T-5.25", table_decision=t("top1", 1.0))
    assert out["status"] == "skipped_by_window_cap"
    # other coins: 30 base -> cap 45: top1 (x1.5 requested, capped x1) = 30, top2 trimmed to the 15 left
    tk2 = "KXXRP15M-26OCT021945-45"
    out = r.submit(ticker=tk2, side="yes", entry_price=0.9, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-6.75", table_decision=t("top1", 1.5))
    assert out["contracts"] == 30 and out["window_cap"]["total"] == 30.0
    out = r.submit(ticker=tk2, side="yes", entry_price=0.92, contracts=25, armed=True, size_mult=1.0,
                   entry_source="table_top2_T-5.25", table_decision=t("top2", 1.0))
    assert out["status"] == "live_sent" and out["contracts"] == 15 and out["window_cap"]["total"] == 45.0
    assert len(_FakeProd.sent) == 4
    # W7's own T-8 call (fallback x1) is capped too; a disarmed audit never consumes the cap
    assert live_plan.exposure("KXSOL15M-26OCT021945-45") == 0.0


def test_window_cap_night_and_partial_fill(armed_router, monkeypatch):
    r = armed_router
    monkeypatch.setattr(r, "_utc_hour", lambda: 2)                                   # night: BTC 20 -> cap 30
    tk = "KXBTC15M-26OCT022100-00"
    t = lambda slot, mult: {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    out = r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-6.75", table_decision=t("top1", 1.5))
    assert out["contracts"] == 20 and out["window_cap"]["cap"] == 30                   # x1.5 capped: 20 (cap stays 30)
    # partial fill: the cap counts what was actually bought
    class Partial(_FakeProd):
        def create_order(self, *, ticker, side, count, price_dollars, tif):
            return {"status_code": 201, "response": json.dumps({"fill_count": "4.00", "average_fill_price": "0.85", "average_fee_paid": "0.01"})}
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Partial)
    tk2 = "KXDOGE15M-26OCT022100-00"
    out = r.submit(ticker=tk2, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                   entry_source="table_top2_T-9.75", table_decision=t("top2", 1.0))
    assert out["contracts"] == 20 and out["window_cap"]["bought"] == 4.0 and live_plan.exposure(tk2) == 4.0
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", _FakeProd)
    out = r.submit(ticker=tk2, side="yes", entry_price=0.86, contracts=25, armed=True, size_mult=0.5,
                   entry_source="table_top1_T-6.75", table_decision=t("top1", 0.5))
    assert out["contracts"] == 10 and out["window_cap"]["total"] == 14.0               # night 20 x0.5 = 10 fits under 30 - 4


def test_scanner_uses_the_prod_band(monkeypatch, tmp_path):
    from crypto_trading.crypto_strategies.live_watch import w7_table, common, w7_noisefade as w7
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG)
    monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    monkeypatch.setattr(w7_table, "STATE", tmp_path / "s.json")
    close = 1_790_000_100 - (1_790_000_100 % 900) + 900
    monkeypatch.setattr(w7_table.time, "time", lambda: close - 9.70 * 60)
    monkeypatch.setattr(live_plan, "maybe_refresh_funding", lambda now=None: None)
    book = {"yes_bid": 0.78, "yes_ask": 0.785, "yes": {"fill_cost": 0.785, "top_cost": 0.785}, "no": {"fill_cost": 0.22, "top_cost": 0.22}}
    monkeypatch.setattr(w7, "walk_book_both", lambda t, n: book)
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"})
    monkeypatch.setattr(common, "mirror_async", lambda name, fn, **kw: (_ for _ in ()).throw(AssertionError("must not order")))
    monkeypatch.setattr(common, "log_line", lambda name, row: None)
    rep = w7_table.run({"w7_noisefade": {"live_orders": True}})
    assert all(v.startswith("out_of_band") for v in rep["coins"].values()) and len(rep["coins"]) == 5


def test_window_cap_uses_band_base_not_the_dump_cut_and_ignores_rejections(armed_router, monkeypatch):
    r = armed_router
    t = lambda slot, mult: {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    # day NO order after a dump: 30 -> 8 contracts, but the cap stays 1.5 x 30 = 45
    monkeypatch.setattr(r, "_trend_bp", lambda coin, lb: -80.0)
    tk = "KXSOL15M-26OCT021945-45"
    out = r.submit(ticker=tk, side="no", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                   entry_source="table_top2_T-9.75", table_decision=t("top2", 1.0))
    assert out["contracts"] == 8 and out["window_cap"]["cap"] == 45
    # a rejected order (4xx) consumes no cap
    class Reject(_FakeProd):
        def create_order(self, **k):
            return {"status_code": 400, "response": json.dumps({"error": {"code": "insufficient_balance"}})}
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Reject)
    monkeypatch.setattr(r, "_trend_bp", lambda coin, lb: 0.0)
    tk2 = "KXDOGE15M-26OCT021945-45"
    out = r.submit(ticker=tk2, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-6.75", table_decision=t("top1", 1.5))
    assert out["status"] == "live_sent" and out["status_code"] == 400 and out["window_cap"]["bought"] == 0.0
    assert live_plan.exposure(tk2) == 0.0


def test_reservation_released_on_every_path_that_does_not_send(armed_router, monkeypatch):
    """The window cap is reserved before the gates (atomic) and given back when no order goes out."""
    r = armed_router
    t = lambda slot, mult: {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    tk = "KXBTC15M-26OCT021945-45"
    monkeypatch.setattr(r, "_flow_gate_decision", lambda t, s: {"decision": "skip"})
    out = r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-9.75", table_decision=t("top1", 1.5))
    assert out["status"] == "skipped_by_flow_gate" and live_plan.exposure(tk) == 0.0
    monkeypatch.setattr(r, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    out = r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=False, size_mult=1.5,
                   entry_source="table_top1_T-9.75", table_decision=t("top1", 1.5))
    assert out["status"] == "live_disarmed" and live_plan.exposure(tk) == 0.0
    monkeypatch.setattr(r, "gate_status", lambda: {"live_open": False, "why": "test"})
    from crypto_trading.crypto_common.execution_events import LiveOrderRefused
    with pytest.raises(LiveOrderRefused):
        r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                 entry_source="table_top1_T-9.75", table_decision=t("top1", 1.5))
    assert live_plan.exposure(tk) == 0.0
    monkeypatch.setattr(r, "gate_status", lambda: {"live_open": True})
    assert live_plan.claim_slot(tk, "top2")                                           # used by an earlier order
    out = r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                   entry_source="table_top2_T-9.75", table_decision=t("top2", 1.0))
    assert out["status"] == "skipped_by_table" and live_plan.exposure(tk) == 0.0
    # the full cap is still available for the order that does go out
    out = r.submit(ticker=tk, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-9.75", table_decision=t("top1", 1.5))
    assert out["status"] == "live_sent" and out["contracts"] == 40 and live_plan.exposure(tk) == 40.0
    # an exception DURING the send keeps the reservation (the order may have reached the exchange)
    class Boom(_FakeProd):
        def create_order(self, **k):
            raise ConnectionError("socket")
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Boom)
    tk2 = "KXSOL15M-26OCT021945-45"
    with pytest.raises(ConnectionError):
        r.submit(ticker=tk2, side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                 entry_source="table_top2_T-9.75", table_decision=t("top2", 1.0))
    assert live_plan.exposure(tk2) == 30.0


def test_reservation_is_atomic_across_threads():
    import threading
    tk = "KXETH15M-26OCT021945-45"
    got = []
    def worker():
        got.append(live_plan.reserve(tk, 40, 60)[0])
    th = [threading.Thread(target=worker) for _ in range(4)]
    [x.start() for x in th]; [x.join() for x in th]
    assert sum(got) == 60 and sorted(got) == [0, 0, 20, 40] and live_plan.exposure(tk) == 60.0
    assert live_plan.settle(tk, 20, 7.0) == 47.0                                      # partial fill replaces its reservation


def test_ledger_in_memory_is_authoritative_when_the_file_is_bad(monkeypatch, tmp_path, caplog):
    """A corrupt ledger file starts an empty in-memory ledger (and says so); a file that
    cannot be written never relaxes what this process already reserved."""
    bad = tmp_path / "slots.json"
    bad.write_text("{not json")
    monkeypatch.setattr(live_plan, "SLOTS_FILE", bad)
    live_plan.reset_ledger()
    tk = "KXDOGE15M-26OCT021945-45"
    with caplog.at_level("WARNING"):
        assert live_plan.reserve(tk, 30, 45) == (30, 0.0)
    assert any("ledger unreadable" in m for m in caplog.messages)
    monkeypatch.setattr(live_plan, "SLOTS_FILE", tmp_path / "no_such_dir" / "slots.json")   # unwritable
    with caplog.at_level("WARNING"):
        assert live_plan.reserve(tk, 30, 45) == (15, 30.0)                          # still capped from memory
        assert live_plan.claim_slot(tk, "top1") and not live_plan.claim_slot(tk, "top1")
    assert any("not persisted" in m for m in caplog.messages)
    assert live_plan.exposure(tk) == 45.0 and live_plan.used_slots(tk) == {"top1"}
    # old on-disk layout {ticker: {slot: ts}} is migrated when read
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"KXBTC15M-26OCT021945-45": {"top1": 1.0}}))
    monkeypatch.setattr(live_plan, "SLOTS_FILE", old)
    live_plan.reset_ledger()
    assert live_plan.used_slots("KXBTC15M-26OCT021945-45") == {"top1"} and live_plan.exposure("KXBTC15M-26OCT021945-45") == 0.0


def test_scanner_retries_a_missing_book_on_the_next_tick(monkeypatch, tmp_path):
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG)
    monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    from crypto_trading.crypto_strategies.live_watch import w7_table, common, w7_noisefade as w7
    monkeypatch.setattr(w7_table, "STATE", tmp_path / "s.json")
    close = 1_790_000_100 - (1_790_000_100 % 900) + 900
    monkeypatch.setattr(w7_table.time, "time", lambda: close - 9.70 * 60)
    monkeypatch.setattr(live_plan, "maybe_refresh_funding", lambda now=None: None)
    books = {"n": 0}
    book = {"yes_bid": 0.84, "yes_ask": 0.86, "yes": {"fill_cost": 0.86, "top_cost": 0.86},
            "no": {"fill_cost": 0.16, "top_cost": 0.16}}
    def walk(t, n):
        books["n"] += 1
        return None if books["n"] <= 5 else book                   # first tick: no book for any coin
    monkeypatch.setattr(w7, "walk_book_both", walk)
    calls = []
    monkeypatch.setattr(live_plan, "decide", lambda coin, ct, b, side, now=None, **k: (
        {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"} if coin == "BTC" else {"action": "skip"}))
    monkeypatch.setattr(common, "mirror_async", lambda name, fn, **kw: calls.append(kw) or "dispatched_async")
    monkeypatch.setattr(common, "log_line", lambda name, row: None)
    rep = w7_table.run({"w7_noisefade": {"live_orders": True}})
    assert set(rep["coins"].values()) == {"book_unavailable"} and calls == []
    monkeypatch.setattr(w7_table.time, "time", lambda: close - 9.70 * 60 + 10)      # next tick, still in the bin
    rep = w7_table.run({"w7_noisefade": {"live_orders": True}})
    assert len(calls) == 1 and rep["coins"]["BTC"].startswith("trade")
    assert w7_table.run({"w7_noisefade": {"live_orders": True}})["coins"] == {}     # and never a third time


def test_funding_refresh_never_blocks_the_runner(monkeypatch):
    import threading
    from crypto_trading.crypto_strategies.w7_scenarios import funding as fundmod
    import pandas as pd
    monkeypatch.setattr(fundmod, "load", lambda: pd.DataFrame({"time_ms": [0]}))     # stale cache
    gate = threading.Event(); seen = []
    def slow_refresh():
        seen.append(threading.current_thread().name)
        gate.wait(5)
    monkeypatch.setattr(fundmod, "refresh", slow_refresh)
    monkeypatch.setitem(live_plan._FUND, "tried", 0.0)
    monkeypatch.setitem(live_plan._FUND, "thread", None)
    t0 = time.time()
    live_plan.maybe_refresh_funding(1_800_000_000.0)
    assert time.time() - t0 < 1.0                                   # returned at once
    live_plan.maybe_refresh_funding(1_800_000_000.0 + 700)          # a second call while the first still runs
    gate.set(); live_plan._FUND["thread"].join(5)
    assert seen == ["w7-funding-refresh"]                           # exactly one refresh, off the caller's thread


def test_scanner_logs_the_classification_inputs(monkeypatch, tmp_path):
    """Every table_decision row carries the inputs the scenario was classified from (replayable)."""
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG)
    monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    from crypto_trading.crypto_strategies.live_watch import w7_table, common, w7_noisefade as w7
    monkeypatch.setattr(w7_table, "STATE", tmp_path / "s.json")
    close = 1_790_000_100 - (1_790_000_100 % 900) + 900
    monkeypatch.setattr(w7_table.time, "time", lambda: close - 6.70 * 60)
    monkeypatch.setattr(live_plan, "maybe_refresh_funding", lambda now=None: None)
    book = {"yes_bid": 0.84, "yes_ask": 0.86, "yes": {"fill_cost": 0.86, "top_cost": 0.86}, "no": {"fill_cost": 0.16, "top_cost": 0.16}}
    monkeypatch.setattr(w7, "walk_book_both", lambda t, n: book)
    inputs = {"fav": "yes", "mkt_trend60": -20.0, "mkt_vr2": 0.6, "dev8h": -0.4, "flow_valid": True, "flow_1m": -0.83, "mom_1m_bp": -0.4}
    monkeypatch.setattr(live_plan, "decide", lambda coin, ct, b, side, now=None, **k: (
        {"action": "skip", "reason": "table_not_this_bin", "scenario": "S", "inputs": inputs} if coin != "XRP"
        else {"action": "fallback", "reason": "scenario_unclassified", "missing": ["dev8h"], "inputs": {**inputs, "dev8h": None}}))
    logs = []
    monkeypatch.setattr(common, "log_line", lambda name, row: logs.append(row))
    w7_table.run({"w7_noisefade": {"live_orders": False}})
    rows = {r["ticker"][2:].split("15M")[0]: r for r in logs if r["action"] == "table_decision"}
    assert rows["BTC"]["inputs"] == inputs and rows["BTC"]["missing"] is None
    assert rows["XRP"]["missing"] == ["dev8h"] and rows["XRP"]["inputs"]["dev8h"] is None
