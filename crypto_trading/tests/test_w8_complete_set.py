"""Economic identities and causal execution boundaries for W8."""
from dataclasses import replace
import json

import pytest

from crypto_trading.crypto_strategies.event_binary import complete_set as k
from crypto_trading.crypto_strategies.live_watch import w8_complete_set as live


def market():
    return k.new_market("BTC-test", "KXBTC15M", 1000., 100.)


def book():
    return k.normalize_book({"yes_dollars": [[.40, 3.5]], "no_dollars": [[.55, 100.]]})


def order(m, qty=5., queue=3.):
    o = dict(id="one", side="yes", price=.4, quantity=qty, remaining=qty,
             queue_ahead=queue, created_ts=100., activate_ts=100.5, expires_ts=120.)
    m["orders"].append(o)
    return o


def trade(tid, ts, qty, price=.4, taker="no"):
    return dict(trade_id=tid, ticker="BTC-test", ts=ts, yes_price=price,
                quantity=qty, taker_side=taker, is_block_trade=False)


def test_pair_plus_residual_equals_cash_under_both_outcomes():
    for result in ("yes", "no"):
        m = market()
        k.add_fill(m, "yes", 10.5, .4, 101, .0175, liquidity="maker", source="a")
        k.add_fill(m, "no", 7.25, .5, 103, .07, liquidity="taker", source="b")
        row = k.settle(m, result)
        assert row["paired_quantity"] == 7.25
        assert row["residual_quantity"] == 3.25
        assert row["net_usd"] == pytest.approx(row["paired_net_usd"]+row["residual_net_usd"])
        assert row["net_usd"] == pytest.approx(m["bought"][result]-m["cost_usd"]-m["fees_usd"])


def test_profitable_pairs_do_not_hide_unmatched_loss():
    m = market()
    k.add_fill(m, "yes", 10, .6, 101, 0, liquidity="maker", source="a")
    k.add_fill(m, "no", 5, .38, 102, 0, liquidity="maker", source="b")
    row = k.settle(m, "no")
    assert row["paired_net_usd"] == pytest.approx(.1)
    assert row["residual_net_usd"] == pytest.approx(-3.)
    assert row["net_usd"] == pytest.approx(-2.9)
    assert k.settle(m, None) is None
    assert k.net_quantity(m) == 5


def test_fee_rounding_and_fractional_book_walk():
    assert k.fee_usd(.5, 5, .07) == .0875
    assert k.fee_usd(.5, .1, 0) == 0
    assert k.walk_buy(book(), "no", 5) == [(0.6, 3.5)]
    assert k.normalize_book({"yes_dollars": [[.6, 10]], "no_dollars": [[.4, 10]]}) is None
    assert k.floor_price(.8999) == .89
    assert k.floor_price(.0999) == .099
    assert k.floor_price(.9039) == .903


def test_trade_queue_partial_fill_time_and_side():
    m, p = market(), k.Parameters()
    o = order(m)
    k.process_trades(m, [trade("early", 100.5, 100), trade("wrong", 101, 100, taker="yes"),
                         trade("one", 102, 2), trade("two", 103, 2.5)], p)
    assert o["remaining"] == 3.5
    assert k.inventory(m, "yes") == 1.5
    k.process_trades(m, [trade("two", 103, 2.5)], p)
    assert k.inventory(m, "yes") == 1.5  # idempotent replay


def test_cancel_latency_and_delayed_delivery_remain_counted():
    m, p = market(), k.Parameters()
    o = order(m, queue=0)
    o["cancel_ts"] = 105.
    k.update_quotes(m, book(), 130, p)
    k.process_trades(m, [trade("late_arrival", 104.9, 1), trade("after_cancel", 105.1, 1)], p)
    assert o["remaining"] == 4  # order retained even after its nominal lifetime


def test_replacement_does_not_double_reserve_and_budget_bounds():
    p = replace(k.Parameters(), max_gross_per_market=6)
    m = market()
    created = k.update_quotes(m, book(), 200, p)
    assert sum(o["quantity"] for o in created) <= 6
    assert k.update_quotes(m, book(), 200.1, p) == []
    assert all(o["price"] < book()[o["side"]+"_ask"] for o in created)


def test_forced_exit_waits_for_cancel_and_uses_depth_and_fees():
    m, p = market(), k.Parameters()
    k.add_fill(m, "yes", 5, .4, 101, 0, liquidity="maker", source="a")
    o = order(m, qty=1, queue=100)
    o["expires_ts"] = 1000.
    assert k.flatten(m, book(), 971, p) == []
    fills = k.flatten(m, book(), 972, p)
    assert sum(f["quantity"] for f in fills) == 3.5
    assert k.inventory(m, "yes") == 1.5
    assert m["fees_usd"] > 0


def test_no_order_submission_and_v2_no_conversion():
    with pytest.raises(ValueError, match="observation-only"):
        live.run({live.NAME: {"enabled": True}})
    with pytest.raises(ValueError, match="observation-only"):
        live.run({live.NAME: {"demo_mirror": True}})
    intent = live.order_intent("BTC-test", dict(id="1", side="no", price=.38,
                                               quantity=5, expires_ts=1234))
    assert intent["side"] == "ask" and intent["price"] == "0.6200"
    assert intent["submitted"] is False and intent["post_only"] is True


def test_state_roundtrip_preserves_fill_dedup():
    m = market()
    order(m, queue=0)
    t = trade("unique", 101, 2)
    k.process_trades(m, [t], k.Parameters())
    recovered = json.loads(json.dumps(m))
    k.process_trades(recovered, [t], k.Parameters())
    assert k.inventory(recovered, "yes") == 2


def test_gap_disqualifies_fixed_verdict():
    st = live._initial_state(k.Parameters())
    st["books"]["tilted"]["trades"] = [dict(close_ts=i, fills=1,
        net_usd=1+(i%2), coverage_gap=i == 0) for i in range(300)]
    live._latch(st)
    assert st["verdict"]["t_window"] > 2.5
    assert st["verdict"]["passed"] is False
    assert st["verdict"]["coverage_gap_windows"] == 1


def test_taker_pair_includes_fees_and_waits_for_outstanding_quote():
    m, p = market(), k.Parameters()
    k.add_fill(m, "yes", 5, .3, 101, 0, liquidity="maker", source="a")
    cheap_no = k.normalize_book({"yes_dollars": [[.4, 100]], "no_dollars": [[.55, 100]]})
    fills = k.take_pair(m, cheap_no, 110, p, residual=False)
    assert len(fills) == 1 and fills[0]["side"] == "no"
    assert k.net_quantity(m) == 0
    assert m["paired_net_usd"] > 0
    m2 = market()
    k.add_fill(m2, "yes", 5, .4, 101, 0, liquidity="maker", source="a")
    assert k.take_pair(m2, cheap_no, 110, p) == []  # .4+.6+fee exceeds .98


def test_unread_cancel_interval_blocks_replacement_and_taker_hedge():
    m, p = market(), k.Parameters()
    k.add_fill(m, "yes", 5, .3, 101, 0, liquidity="maker", source="a")
    o = order(m, queue=100)
    o["side"], o["cancel_ts"] = "no", 105.
    m["trade_watermark_ts"] = 104.9
    assert k.take_pair(m, book(), 105.1, p, residual=False) == []
    k.update_quotes(m, book(), 105.1, p)
    assert len([x for x in m["orders"] if x["side"] == "no"]) == 1
    m["trade_watermark_ts"] = 105.01
    assert k.take_pair(m, book(), 105.1, p, residual=False)


def test_latch_waits_for_all_known_same_window_markets():
    st = live._initial_state(k.Parameters())
    st["books"]["tilted"]["trades"] = [dict(close_ts=i, fills=1,
        net_usd=1+(i%2), coverage_gap=False) for i in range(300)]
    st["books"]["tilted"]["positions"]["pending_eth"] = {"close_ts": 299}
    live._latch(st)
    assert st["verdict"] is None
    st["books"]["tilted"]["positions"].clear()
    live._latch(st)
    assert st["verdict"]["passed"] is True


def test_final_settlement_drains_unread_prints(monkeypatch):
    st = live._initial_state(k.Parameters())
    m = market()
    m["close_ts"] = 130
    order(m, queue=0)
    m["trade_watermark_ts"] = 101
    st["books"]["paired"]["positions"][m["ticker"]] = m
    monkeypatch.setattr(live, "fetch_trades", lambda *args: ([trade("final", 110, 2)], True))
    monkeypatch.setattr(live, "read_get", lambda *args: {"market": {"result": "no"}})
    monkeypatch.setattr(live, "append_tape", lambda *args: None)
    monkeypatch.setattr(live.common, "log_line", lambda *args, **kwargs: None)
    live._record_settlements(st, 200)
    assert not st["books"]["paired"]["positions"]
    row = st["books"]["paired"]["trades"][0]
    assert row["fills"] == 1
    assert row["net_usd"] == pytest.approx(-.8)


def test_public_rate_limit_cools_down_without_more_requests(monkeypatch):
    calls = []
    class Response:
        status_code = 429
        headers = {"Retry-After": "90"}
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_RATE_STREAK", 0)
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 1.)
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_LAST_RATE_TS", 0.)
    monkeypatch.setattr(live.time, "time", lambda: 100.)
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: calls.append(a) or Response())
    with pytest.raises(live.ReadRateLimited):
        live.read_get("/markets")
    assert live._HTTP_RESUME_TS == 190.
    with pytest.raises(live.ReadRateLimited):
        live.read_get("/markets")
    assert len(calls) == 1


def test_final_incomplete_tape_is_retained_then_reconciled(monkeypatch):
    st = live._initial_state(k.Parameters())
    m = market()
    m["close_ts"] = 130
    order(m, queue=0)
    m["trade_watermark_ts"] = 101
    st["books"]["paired"]["positions"][m["ticker"]] = m
    monkeypatch.setattr(live, "fetch_trades", lambda *args: ([], False))
    monkeypatch.setattr(live, "read_get", lambda *args: {"market": {"result": "no"}})
    monkeypatch.setattr(live, "append_tape", lambda *args: None)
    monkeypatch.setattr(live.common, "log_line", lambda *args, **kwargs: None)
    live._record_settlements(st, 200)
    assert m["ticker"] in st["books"]["paired"]["positions"]
    assert st["books"]["paired"]["trades"] == []
    assert m["trade_watermark_ts"] == 101
    assert m["unverified_order_quantity"] == 5
    monkeypatch.setattr(live, "fetch_trades", lambda *args: ([trade("late", 110, 2)], True))
    live._record_settlements(st, 261)
    live._record_settlements(st, 322)
    row = st["books"]["paired"]["trades"][0]
    assert row["net_usd"] == pytest.approx(-.8)
    assert row["unverified_order_quantity"] == 0
    assert row["coverage_gap"]


def test_zero_fill_peer_gap_still_disqualifies_window():
    st = live._initial_state(k.Parameters())
    rows = [dict(close_ts=i, fills=1, net_usd=1+i%2, coverage_gap=False)
            for i in range(300)]
    rows.append(dict(close_ts=100, fills=0, net_usd=0, coverage_gap=True))
    st["books"]["tilted"]["trades"] = rows
    live._latch(st)
    assert st["verdict"]["coverage_gap_windows"] == 1
    assert st["verdict"]["passed"] is False


def test_terminal_outage_marks_gap_without_successful_recovery():
    st = live._initial_state(k.Parameters())
    m = market()
    st["inputs"][m["ticker"]] = {"last_book_ts": 950.}
    st["books"]["paired"]["positions"][m["ticker"]] = m
    live._mark_management_gaps(st, 1100.)
    assert m["coverage_gap"]
    assert st["gaps"][0]["end"] == m["close_ts"]


def test_success_does_not_reset_rate_pressure_and_requests_are_paced(monkeypatch):
    clock = [200.]
    slept = []
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}
    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    def sleep(dt):
        slept.append(dt)
        clock[0] += dt
    monkeypatch.setattr(live.time, "sleep", sleep)
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 201.5)
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 1.5)
    monkeypatch.setattr(live, "_HTTP_RATE_STREAK", 2)
    live.read_get("/markets")
    live.read_get("/markets")
    assert slept == [1.5, 1.5]
    assert live._HTTP_RATE_STREAK == 2
    assert live._HTTP_MIN_INTERVAL == 1.5


def _adapter_fixture(monkeypatch, tmp_path, *, single_side=False):
    st = live._initial_state(k.Parameters())
    m = market()
    m["trade_watermark_ts"] = 101.
    st["inputs"][m["ticker"]] = {"last_trade_ts": 101., "last_cycle_ts": 101., "last_book_ts": 101.}
    st["books"]["paired"]["positions"][m["ticker"]] = m
    path = tmp_path / "state.json"
    monkeypatch.setattr(live.common, "state_path", lambda *a: path)
    monkeypatch.setattr(live.common, "save_state", lambda _, s: path.write_text(json.dumps(s)))
    monkeypatch.setattr(live.common, "log_line", lambda *a, **kw: None)
    monkeypatch.setattr(live, "append_tape", lambda *a: None)
    monkeypatch.setattr(live, "_refresh_fees", lambda *a: True)
    monkeypatch.setattr(live, "_record_settlements", lambda *a: None)
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 1.)
    monkeypatch.setattr(live, "SERIES", ("KXBTC15M",))
    monkeypatch.setattr(live.time, "time", lambda: 112.)
    monkeypatch.setattr(live.w7_noisefade, "latest_snapshot", lambda _: {
        "recv_ts": 112., "markets": [{"ticker": m["ticker"], "status": "active",
        "open_time": live.iso(100.), "close_time": live.iso(1000.)}]})
    raw = {"yes_dollars": [[.4, 100]], "no_dollars": [] if single_side else [[.55, 100]]}
    monkeypatch.setattr(live, "read_get", lambda *a, **kw: raw)
    return st, m, path


def test_capped_trade_interval_preserves_watermark_and_actual_cancel_time(monkeypatch, tmp_path):
    st, m, path = _adapter_fixture(monkeypatch, tmp_path)
    order(m, queue=0)
    path.write_text(json.dumps(st))
    monkeypatch.setattr(live, "fetch_trades", lambda *a: ([], False))
    result = live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    retained = saved["books"]["paired"]["positions"][m["ticker"]]
    assert retained["trade_watermark_ts"] == 101.
    assert saved["inputs"][m["ticker"]]["last_trade_ts"] == 101.
    assert retained["orders"][0]["cancel_ts"] == 112.5
    assert result["status"] == saved["status"] == "DATA_DEGRADED"


def test_single_sided_adapter_reduces_existing_inventory(monkeypatch, tmp_path):
    st, m, path = _adapter_fixture(monkeypatch, tmp_path, single_side=True)
    k.add_fill(m, "yes", 3., .4, 100.5, 0, liquidity="maker_model", source="old")
    path.write_text(json.dumps(st))
    monkeypatch.setattr(live, "fetch_trades", lambda *a: ([], True))
    result = live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    retained = saved["books"]["paired"]["positions"][m["ticker"]]
    assert k.net_quantity(retained) == 0
    assert result["markets"]["KXBTC15M"]["status"] == "EXIT_ONLY_ONE_SIDED"
    assert retained["orders"] == []


def test_four_second_pacing_can_catch_up_and_manage(monkeypatch, tmp_path):
    st, m, path = _adapter_fixture(monkeypatch, tmp_path)
    path.write_text(json.dumps(st))
    clock = [112.]
    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 4.)
    def fetch(*args):
        clock[0] += 4.
        return [], True
    def depth(*args, **kwargs):
        clock[0] += 4.
        return {"yes_dollars": [[.4, 100]], "no_dollars": [[.55, 100]]}
    monkeypatch.setattr(live, "fetch_trades", fetch)
    monkeypatch.setattr(live, "read_get", depth)
    result = live.run({live.NAME: {}})
    assert result["markets"]["KXBTC15M"]["status"] == "OK"
    saved = json.loads(path.read_text())
    assert saved["books"]["paired"]["positions"][m["ticker"]]["orders"]


def test_successful_small_backfill_grows_instead_of_lagging_forever(monkeypatch, tmp_path):
    st, m, path = _adapter_fixture(monkeypatch, tmp_path)
    st["inputs"][m["ticker"]]["backfill_span_s"] = .5
    path.write_text(json.dumps(st))
    monkeypatch.setattr(live, "fetch_trades", lambda *a: ([], True))
    result = live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    assert saved["inputs"][m["ticker"]]["backfill_span_s"] == 1.
    assert saved["inputs"][m["ticker"]]["last_trade_ts"] == 101.5
    assert result["markets"]["KXBTC15M"]["status"] == "CATCHING_UP"


def test_request_crossing_close_does_not_create_taker_fill(monkeypatch, tmp_path):
    st, m, path = _adapter_fixture(monkeypatch, tmp_path, single_side=True)
    m["close_ts"] = 115.
    k.add_fill(m, "yes", 3., .4, 100.5, 0, liquidity="maker_model", source="old")
    path.write_text(json.dumps(st))
    monkeypatch.setattr(live.w7_noisefade, "latest_snapshot", lambda _: {
        "recv_ts": 112., "markets": [{"ticker": m["ticker"], "status": "active",
        "open_time": live.iso(100.), "close_time": live.iso(115.)}]})
    clock = [112.]
    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    monkeypatch.setattr(live, "fetch_trades", lambda *a: ([], True))
    def depth(*args, **kwargs):
        clock[0] = 120.
        return {"yes_dollars": [[.4, 100]], "no_dollars": []}
    monkeypatch.setattr(live, "read_get", depth)
    result = live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    retained = saved["books"]["paired"]["positions"][m["ticker"]]
    assert len(retained["fills"]) == 1
    assert k.net_quantity(retained) == 3.
    assert result["markets"]["KXBTC15M"]["status"] == "AWAITING_SETTLEMENT"


def test_loss_in_second_book_stops_first_book_before_quoting(monkeypatch, tmp_path):
    st, m, path = _adapter_fixture(monkeypatch, tmp_path)
    st["books"]["tilted"]["cum_net_usd"] = -101.
    path.write_text(json.dumps(st))
    monkeypatch.setattr(live, "fetch_trades", lambda *a: ([], True))
    live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    assert saved["stopped_new"]
    for b in saved["books"].values():
        assert b["positions"][m["ticker"]]["orders"] == []
