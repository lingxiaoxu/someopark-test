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


def test_gapped_windows_are_discarded_not_judged():
    """A window whose fill ledger may be incomplete is missing data, not
    evidence: it is discarded and counted, never judged. (Merely looking
    late is a different thing - see the trustworthy-P&L test.)"""
    st = live._initial_state(k.Parameters())
    rows = [dict(close_ts=i, fills=1, quantity=10., net_usd=1+(i%2),
                 unverified_order_quantity=(5.0 if i == 0 else 0.0))
            for i in range(300)]
    st["books"]["tilted"]["trades"] = rows
    live._latch(st)
    assert st.get("verdict") is None          # 299 clean < 300: keep waiting
    rows.append(dict(close_ts=300, fills=1, quantity=10., net_usd=2,
                     unverified_order_quantity=0.0))
    live._latch(st)
    v = st["verdict"]
    assert v["passed"] is True and v["windows"] == 300
    assert v["gapped_windows_discarded"] == 1 and v["t_window"] > 2.5


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
    assert row["net_usd"] == pytest.approx(-.8-k.fee_usd(.4, 2, k.Parameters().maker_coefficient))


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
    assert row["net_usd"] == pytest.approx(-.8-k.fee_usd(.4, 2, k.Parameters().maker_coefficient))
    assert row["unverified_order_quantity"] == 0
    assert row["coverage_gap"]


def test_zero_fill_peer_gap_discards_that_whole_window():
    """A peer leg with an unread order lifetime poisons its whole window:
    the P&L of that window cannot be trusted even though OUR leg looks fine,
    because the legs settle one macro move together."""
    st = live._initial_state(k.Parameters())
    rows = [dict(close_ts=i, fills=1, quantity=10., net_usd=1+i%2,
                 unverified_order_quantity=0.0) for i in range(301)]
    rows.append(dict(close_ts=100, fills=0, net_usd=0,
                     unverified_order_quantity=5.0))
    st["books"]["tilted"]["trades"] = rows
    live._latch(st)
    v = st["verdict"]
    assert v["windows"] == 300 and v["gapped_windows_discarded"] == 1
    assert v["passed"] is True


def test_terminal_outage_marks_gap_without_successful_recovery():
    st = live._initial_state(k.Parameters())
    m = market()
    st["inputs"][m["ticker"]] = {"last_book_ts": 950.}
    st["books"]["paired"]["positions"][m["ticker"]] = m
    live._mark_management_gaps(st, 1100.)
    assert m["coverage_gap"]
    assert st["gaps"][0]["end"] == m["close_ts"]


def test_success_resets_failure_streak_but_preserves_request_pacing(monkeypatch):
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
    monkeypatch.setattr(live, "_HTTP_LAST_SUCCESS_TS", 0.)
    live.read_get("/markets")
    live.read_get("/markets")
    assert slept == [1.5, 1.5]
    assert live._HTTP_RATE_STREAK == 0
    assert live._HTTP_MIN_INTERVAL == 1.5
    assert live._HTTP_LAST_SUCCESS_TS == 203.


def test_successes_between_429s_do_not_accumulate_a_failure_streak(monkeypatch):
    clock = [5_000.]
    status = [429]

    class Response:
        headers = {}
        @property
        def status_code(self): return status[0]
        def raise_for_status(self): pass
        def json(self): return {}

    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    monkeypatch.setattr(live.time, "sleep", lambda dt: clock.__setitem__(0, clock[0] + dt))
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 2.5)
    monkeypatch.setattr(live, "_HTTP_LAST_RATE_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_LAST_DECAY_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_LAST_SUCCESS_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_RATE_STREAK", 0)
    monkeypatch.setattr(live, "_HTTP_COUNTS", {"requests": 0, "rate_limited": 0})
    for _ in range(6):
        status[0] = 429
        with pytest.raises(live.ReadRateLimited, match="collateral"):
            live.read_get("/markets")
        assert live._HTTP_RATE_STREAK == 1
        assert live._HTTP_RESUME_TS-clock[0] <= 10.
        clock[0] = live._HTTP_RESUME_TS
        status[0] = 200
        live.read_get("/markets")
        assert live._HTTP_RATE_STREAK == 0
        assert live._HTTP_LAST_SUCCESS_TS == clock[0]
        assert live._HTTP_MIN_INTERVAL == 2.5  # successes do not shed pace
    assert live._HTTP_COUNTS == {"requests": 12, "rate_limited": 6}


def test_http_200_with_invalid_json_is_not_successful_read_evidence(monkeypatch):
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): raise json.JSONDecodeError("invalid payload", "<html>", 0)

    monkeypatch.setattr(live.time, "time", lambda: 5_000.)
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_RATE_STREAK", 3)
    monkeypatch.setattr(live, "_HTTP_LAST_SUCCESS_TS", 4_000.)
    monkeypatch.setattr(live, "_HTTP_COUNTS", {"requests": 0, "rate_limited": 0})
    with pytest.raises(json.JSONDecodeError):
        live.read_get("/markets")
    assert live._HTTP_LAST_SUCCESS_TS == 4_000.
    assert live._HTTP_RATE_STREAK == 3
    assert live._HTTP_COUNTS == {"requests": 1, "rate_limited": 0}


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
    # close at 652 puts the 112.-clock 540s from close: inside the v7 entry
    # window, so quoting mechanics can be exercised
    m["close_ts"] = 652.
    monkeypatch.setattr(live.w7_noisefade, "latest_snapshot", lambda _: {
        "recv_ts": 112., "markets": [{"ticker": m["ticker"], "status": "active",
        "open_time": live.iso(100.), "close_time": live.iso(652.)}]})
    raw = {"yes_dollars": [[.4, 100]], "no_dollars": [] if single_side else [[.55, 100]]}
    monkeypatch.setattr(live, "read_get", lambda *a, **kw: raw)
    return st, m, path


@pytest.mark.parametrize("persisted_success", [None, 105.])
def test_http_success_timestamp_restores_and_saves_with_old_state_compatibility(
        monkeypatch, tmp_path, persisted_success):
    st, _, path = _adapter_fixture(monkeypatch, tmp_path)
    if persisted_success is not None:
        st["http_read_control"] = {"last_success_ts": persisted_success}
    monkeypatch.setattr(live, "_HTTP_LAST_SUCCESS_TS", 0.)
    path.write_text(json.dumps(st))
    live.run({live.NAME: {}})
    expected = persisted_success or 0.
    assert live._HTTP_LAST_SUCCESS_TS == expected
    saved = json.loads(path.read_text())
    assert saved["http_read_control"]["last_success_ts"] == expected


def test_successful_http_read_saves_recovery_time_and_cleared_streak(monkeypatch, tmp_path):
    real_read_get = live.read_get
    st, _, path = _adapter_fixture(monkeypatch, tmp_path)
    st["http_read_control"] = {"rate_streak": 3, "last_success_ts": 105.}

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"yes_dollars": [[.4, 100]], "no_dollars": [[.55, 100]]}

    monkeypatch.setattr(live, "read_get", real_read_get)
    monkeypatch.setattr(live, "recorded_orderbook", lambda *a: None)
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_LAST_SUCCESS_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_RATE_STREAK", 0)
    path.write_text(json.dumps(st))
    live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    assert saved["http_read_control"]["last_success_ts"] == 112.
    assert saved["http_read_control"]["rate_streak"] == 0


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
        # priced inside the v4 entry band: this test is about catching up and
        # still managing the market, not about the band gate
        return {"yes_dollars": [[.34, 100]], "no_dollars": [[.65, 100]]}
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


def test_loss_in_one_book_stops_only_that_book(monkeypatch, tmp_path):
    """v2 REVERSES the v1 behaviour this test used to lock: v1's shared stop
    let the tilted book's -$101 terminate the paired CONTROL at -$86.86,
    destroying the comparison the control exists for. Now each book latches
    on its own loss and the other keeps observing."""
    st, m, path = _adapter_fixture(monkeypatch, tmp_path)
    st["books"]["tilted"]["cum_net_usd"] = -101.
    path.write_text(json.dumps(st))
    monkeypatch.setattr(live, "fetch_trades", lambda *a: ([], True))
    live.run({live.NAME: {}})
    saved = json.loads(path.read_text())
    assert saved["books"]["tilted"]["stopped_new"] is True
    assert "dollar backstop" in saved["books"]["tilted"]["stopped_reason"]
    assert not saved["books"]["paired"].get("stopped_new")
    assert not saved.get("stopped_new")
    assert saved["status"] != "STOPPED_NEW"          # one book still observes
    assert saved["books"]["tilted"]["positions"][m["ticker"]]["stop_new"]
    assert not saved["books"]["paired"]["positions"][m["ticker"]]["stop_new"]


def test_pair_classification_set_drift_stop():
    """Three different trades hide inside FIFO "pairs": the designed set
    (0.975..1.0), the drift pair (<0.975 = the first leg had already won),
    and the stop exit (>1.0 = paying to get out). v2 books them apart -
    mixing stops into "set" hid -31c/contract of stop slippage."""
    m = market()
    k.add_fill(m, "yes", 5, .49, 101, 0, liquidity="maker", source="a")
    k.add_fill(m, "no", 5, .49, 102, 0, liquidity="maker", source="b")   # set: 0.98
    k.add_fill(m, "yes", 5, .30, 103, 0, liquidity="maker", source="c")
    k.add_fill(m, "no", 5, .60, 104, 0, liquidity="taker", source="d")   # drift: 0.90
    k.add_fill(m, "yes", 5, .50, 105, 0, liquidity="maker", source="e")
    k.add_fill(m, "no", 5, .57, 106, 0, liquidity="taker", source="f")   # stop: 1.07
    row = k.settle(m, "yes")
    assert row["set_pair_quantity"] == 5
    assert row["set_pair_net_usd"] == pytest.approx(5*(1-.98), abs=.02)
    assert row["drift_pair_quantity"] == 5
    assert row["drift_pair_net_usd"] == pytest.approx(5*(1-.90), abs=.03)
    assert row["stop_pair_quantity"] == 5
    assert row["stop_pair_net_usd"] == pytest.approx(5*(1-1.07), abs=.03)
    total = row["set_pair_net_usd"]+row["drift_pair_net_usd"]+row["stop_pair_net_usd"]
    assert total == pytest.approx(row["paired_net_usd"])


def test_v3_fresh_quotes_only_on_the_favored_side():
    """The account's shape: fresh inventory only where the model leans; the
    other side exists to COMPLETE. Priced inside the v4 entry band so THIS
    test isolates the side rule (the band has its own test): favored "no" at
    .65 must quote no only, and yes may appear only to complete."""
    m, p = market(), k.Parameters()
    b = k.normalize_book({"yes_dollars": [[.34, 50]], "no_dollars": [[.65, 50]]})
    # 460 = 540s before the 1000 close, i.e. inside the v7 entry time window
    created = k.update_quotes(m, b, 460, p, residual=False)
    assert [o["side"] for o in created] == ["no"]
    # after a favored-side fill, the OTHER side may quote up to the completion
    m2 = market()
    k.add_fill(m2, "no", 3, .65, 459, 0, liquidity="maker_model", source="x")
    m2["trade_watermark_ts"] = 460.
    created = k.update_quotes(m2, b, 460, p, residual=False)
    sides = {o["side"]: o["quantity"] for o in created}
    assert sides.get("yes", 0) <= 3 + 1e-9          # completion only, never fresh


def test_v3_leg_stop_disabled_by_default_armable_explicitly():
    """v2 measured the 6c stop realising -31c/contract at observer latency -
    worse than riding (-20c). Off by default; still available explicitly."""
    from dataclasses import replace as _r
    assert k.Parameters().leg_stop_c == 0.0
    m, p = market(), k.Parameters()
    k.add_fill(m, "yes", 5, .60, 101, 0, liquidity="maker_model", source="a")
    deep = k.normalize_book({"yes_dollars": [[.40, 50]], "no_dollars": [[.50, 50]]})
    k.update_quotes(m, deep, 200, p, residual=False)
    assert not m.get("force_flatten")                 # 20c under water, no stop
    m2 = market()
    k.add_fill(m2, "yes", 5, .60, 101, 0, liquidity="maker_model", source="a")
    k.update_quotes(m2, deep, 200, _r(p, leg_stop_c=6.0), residual=False)
    assert m2.get("force_flatten") and m2["leg_stop_hit"]["side"] == "yes"


def test_entry_band_edges_bind(_unused=None):
    """v4: the edge is WHERE we quote, not which side. W8's own filled maker
    contracts pay +2c inside [0.60,0.78) and -2 to -5c outside it, so fresh
    inventory is band-gated. Completion quotes stay exempt: they close risk."""
    """Both band edges gate FRESH inventory (evaluated inside the v7 time
    window). Below .62 the 17-day measurement is flat-to-negative; above .86
    it is untested for a maker at this size. Completion quotes are exempt."""
    from dataclasses import replace as _r
    p = k.Parameters()
    assert (p.entry_band_lo, p.entry_band_hi) == (0.62, 0.86)
    NOW = 460.            # 540s before close: inside the entry time window

    cheap = k.normalize_book({"yes_dollars": [[.44, 50]], "no_dollars": [[.55, 50]]})
    assert k.update_quotes(market(), cheap, NOW, p, residual=False) == []

    inband = k.normalize_book({"yes_dollars": [[.34, 50]], "no_dollars": [[.65, 50]]})
    created = k.update_quotes(market(), inband, NOW, p, residual=False)
    assert [o["side"] for o in created] == ["no"] and created[0]["price"] >= .62

    rich = k.normalize_book({"yes_dollars": [[.08, 50]], "no_dollars": [[.90, 50]]})
    assert k.update_quotes(market(), rich, NOW, p, residual=False) == []

    # with inventory to complete, the out-of-band side still quotes
    m4 = market()
    k.add_fill(m4, "no", 3, .90, NOW - 1, 0, liquidity="maker_model", source="x")
    m4["trade_watermark_ts"] = NOW
    created = k.update_quotes(m4, rich, NOW, p, residual=False)
    assert any(o["side"] == "yes" for o in created)
    # widening the band restores the fresh quote, proving the gate is the cause
    created = k.update_quotes(market(), rich, NOW,
                              _r(p, entry_band_hi=0.95), residual=False)
    assert [o["side"] for o in created] == ["no"]


def test_v5_series_universe_is_the_measured_one():
    """v5 trades BTC/ETH/DOGE/XRP. SOL is excluded on evidence (+0.20c,
    t 0.15 over 17 days, and including it pulls the 4-coin mean from +2.50c
    to +2.05c); a future case for SOL must be made on NEW data, not by
    quietly widening this tuple."""
    assert live.SERIES == ("KXBTC15M", "KXETH15M", "KXDOGE15M", "KXXRP15M")
    assert "KXSOL15M" not in live.SERIES
    # the observer must actually iterate every registered series
    import inspect
    src = inspect.getsource(live.run)
    assert "for series in SERIES" in src


def test_pacer_decays_after_a_sustained_clean_period(monkeypatch):
    """The pacer ratchets up instantly on a 429 but must also come back down,
    or one bad minute pins it forever: at 4s the two requests inside a cycle
    exceed the caught_up tolerance and EVERY window is marked gapped, making
    the 300-clean-window verdict unreachable (measured 2026-09-12, 0.9% rate
    limited yet pinned at 4.0s). Decay is slow, one step at a time, and a
    single success still restores nothing."""
    clock = [10_000.]

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {}

    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    monkeypatch.setattr(live.time, "sleep", lambda dt: clock.__setitem__(0, clock[0] + dt))
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: Response())
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 4.)
    monkeypatch.setattr(live, "_HTTP_LAST_DECAY_TS", 0.)

    # a 429 five minutes ago: still inside the quiet period, no decay
    monkeypatch.setattr(live, "_HTTP_LAST_RATE_TS", clock[0] - 300.)
    live.read_get("/markets")
    assert live._HTTP_MIN_INTERVAL == 4.

    # ten minutes clean -> one step, and only one no matter how many requests
    monkeypatch.setattr(live, "_HTTP_LAST_RATE_TS", clock[0] - 900.)
    live.read_get("/markets")
    assert live._HTTP_MIN_INTERVAL == 3.5
    live.read_get("/markets")
    live.read_get("/markets")
    assert live._HTTP_MIN_INTERVAL == 3.5          # not once per request

    # five more minutes -> the next step; never below the floor
    clock[0] += 400.
    live.read_get("/markets")
    assert live._HTTP_MIN_INTERVAL == 3.0
    for _ in range(10):
        clock[0] += 400.
        live.read_get("/markets")
    assert live._HTTP_MIN_INTERVAL == live._HTTP_FLOOR_INTERVAL == 1.5


def test_idle_market_skips_the_trade_fetch_and_still_advances(monkeypatch, tmp_path):
    """With no resting order and no inventory, no fill can occur in the
    interval, so its trade tape carries no information and must not cost a
    request. Halving per-cycle requests is what lets four series stay inside
    the revisit budget (2026-09-13: the old always-fetch cycle fell behind by
    55s median and produced zero clean windows in five hours)."""
    st, m, path = _adapter_fixture(monkeypatch, tmp_path)
    path.write_text(json.dumps(st))
    calls = []
    monkeypatch.setattr(live, "fetch_trades",
                        lambda *a: calls.append(a) or ([], True))
    live.run({live.NAME: {}})
    assert calls == []                                  # idle -> no fetch
    saved = json.loads(path.read_text())
    assert saved["inputs"][m["ticker"]]["last_trade_ts"] > 101.  # still advances

    # once an order exists the tape must be read again
    st2 = json.loads(path.read_text())
    pos = st2["books"]["paired"]["positions"][m["ticker"]]
    pos["orders"] = [dict(id="o1", side="yes", price=.65, quantity=5.,
                          remaining=5., queue_ahead=0., created_ts=100.,
                          activate_ts=100.5, expires_ts=1000.)]
    st2["last_tick_ts"] = 0.
    path.write_text(json.dumps(st2))
    live.run({live.NAME: {}})
    assert len(calls) == 1


def test_backfill_span_can_exceed_one_cycle():
    """The cap must be larger than a four-series revisit (~33s), or the
    watermark advances slower than the clock and the observer never catches
    up - the defect that made every window gapped."""
    assert live.BACKFILL_MAX_S >= 120.


def test_collateral_429_does_not_slow_us_but_a_real_streak_does(monkeypatch):
    """A 429 caused by ANOTHER process saturating the shared budget must not
    ratchet our pace: measured 2026-09-13 we are 2.2% of the traffic (0.26
    req/s vs the daily recorder's 11.5), and a hand probe at 10 req/s drew no
    429s at all. Halving our rate cannot reduce a collision we do not cause -
    it only dirties every window. A sustained streak still escalates."""
    clock = [5_000.]

    class R429:
        status_code = 429
        headers = {}

    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    monkeypatch.setattr(live.time, "sleep", lambda dt: clock.__setitem__(0, clock[0] + dt))
    monkeypatch.setattr(live.requests, "get", lambda *a, **kw: R429())
    monkeypatch.setattr(live, "_HTTP_MIN_INTERVAL", 1.5)
    monkeypatch.setattr(live, "_HTTP_NEXT_REQUEST_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_LAST_RATE_TS", 0.)
    monkeypatch.setattr(live, "_HTTP_RATE_STREAK", 0)

    # first three are treated as collateral: pace untouched, short cooldown
    for i in range(3):
        monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
        with pytest.raises(live.ReadRateLimited, match="collateral"):
            live.read_get("/markets")
        assert live._HTTP_MIN_INTERVAL == 1.5
        assert live._HTTP_RESUME_TS - clock[0] <= 10.      # seconds, not minutes
        clock[0] += 1.

    # a fourth in the same streak is us: now escalate
    monkeypatch.setattr(live, "_HTTP_RESUME_TS", 0.)
    with pytest.raises(live.ReadRateLimited, match="own rate"):
        live.read_get("/markets")
    assert live._HTTP_MIN_INTERVAL > 1.5
    assert live._HTTP_RESUME_TS - clock[0] >= 60.


def test_recorded_orderbook_reused_only_while_fresh(tmp_path, monkeypatch):
    """Reuse the strip recorder's own write instead of spending a paced
    request - but only while it is younger than the quote TTL, because a
    20s-lived quote posted off a staler book models a slower trader than we
    are. The recorder itself is never touched: this is a pure read."""
    root = tmp_path / "prod"
    (root / "KXBTC15M" / "orderbook").mkdir(parents=True)
    monkeypatch.setattr(live, "STRIPS_ROOT", root)
    now = 1_789_000_000.0
    import datetime as _dt
    day = _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%d")
    f = root / "KXBTC15M" / "orderbook" / f"{day}.jsonl"

    def write(rows):
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    ob = {"orderbook_fp": {"yes_dollars": [[".34", "50"]], "no_dollars": [[".65", "50"]]}}
    write([{"recv_ts": now - 5, "ticker": "KXBTC15M-A", "ob": ob}])
    got = live.recorded_orderbook("KXBTC15M", "KXBTC15M-A", now)
    assert got is not None and got[1] == 5 and got[0] == ob

    # too old -> caller must fetch its own
    write([{"recv_ts": now - 45, "ticker": "KXBTC15M-A", "ob": ob}])
    assert live.recorded_orderbook("KXBTC15M", "KXBTC15M-A", now) is None
    # another ticker's fresh row must not be served for ours
    write([{"recv_ts": now - 2, "ticker": "KXBTC15M-OTHER", "ob": ob}])
    assert live.recorded_orderbook("KXBTC15M", "KXBTC15M-A", now) is None
    # missing file is simply a miss, never an exception
    assert live.recorded_orderbook("KXNOPE15M", "KXNOPE15M-A", now) is None
    # a future-stamped row is not "fresh"
    write([{"recv_ts": now + 60, "ticker": "KXBTC15M-A", "ob": ob}])
    assert live.recorded_orderbook("KXBTC15M", "KXBTC15M-A", now) is None


def test_clean_window_means_trustworthy_pnl_not_perfect_watching():
    """A window counts when its FILL LEDGER is provably complete, and is
    excluded when a gap could have hidden a fill. Excluding merely-late
    revisits too made the verdict unreachable: at 89% per-observation
    completeness and ~50 observations per window, P(no gap) ~= 0.3%."""
    st = live._initial_state(k.Parameters())

    def rows(n, *, unverified=0.0, late=False):
        return [dict(close_ts=i, fills=1, quantity=10., net_usd=1 + (i % 2),
                     coverage_gap=late, unverified_order_quantity=unverified)
                for i in range(n)]

    # late revisits only -> trustworthy, counted, and disclosed
    st["books"]["tilted"]["trades"] = rows(300, late=True)
    live._latch(st)
    v = st["verdicts"]["tilted"]
    assert v["windows"] == 300 and v["gapped_windows_discarded"] == 0
    assert v["management_gap_windows"] == 300      # measured and reported

    # an unread order lifetime -> the P&L may be wrong -> discarded
    st2 = live._initial_state(k.Parameters())
    bad = rows(5, unverified=5.0)
    for i, r in enumerate(bad):
        r["close_ts"] = 1000 + i
    st2["books"]["tilted"]["trades"] = rows(300) + bad
    live._latch(st2)
    v2 = st2["verdicts"]["tilted"]
    assert v2["windows"] == 300 and v2["gapped_windows_discarded"] == 5


def test_v6_no_timed_unwind_but_guards_still_fire():
    """v6: holding a favoured leg to settlement beat unwinding it at T-120 by
    5.39c/contract over 461 measured contracts, so the clock no longer closes
    positions. The guards that answer to RISK rather than to the clock must
    still work."""
    from dataclasses import replace as _r
    p = k.Parameters()
    assert p.flatten_before_s == 0.0 and p.leg_stop_c == 0.0

    # a held leg near close is left alone by the clock
    m = market()
    m["close_ts"] = 1000.
    k.add_fill(m, "no", 5, .65, 100., 0, liquidity="maker_model", source="x")
    b = k.normalize_book({"yes_dollars": [[.30, 50]], "no_dollars": [[.68, 50]]})
    k.update_quotes(m, b, 995., p, residual=False)       # 5s to close
    assert not m.get("force_flatten")
    assert k.inventory(m, "no") == 5                     # still held

    # the window loss guard still forces an exit
    m2 = market()
    k.add_fill(m2, "no", 5, .95, 100., 0, liquidity="maker_model", source="x")
    crashed = k.normalize_book({"yes_dollars": [[.90, 50]], "no_dollars": [[.05, 50]]})
    k.update_quotes(m2, crashed, 500., _r(p, max_window_loss=1.0), residual=False)
    assert m2.get("force_flatten") and m2["stop_new"]
    # the exit first cancels its own resting quotes and waits for the tape to
    # cover their whole life - crossing before that could double-fill
    assert k.flatten(m2, crashed, 502., p) == []
    assert all(o.get("cancel_ts") == 502.5 for o in m2["orders"])
    m2["trade_watermark_ts"] = 503.                      # tape now covers it
    fills = k.flatten(m2, crashed, 503., p)
    assert fills and k.inventory(m2, "no") == 0


def test_v7_entry_confined_to_price_band_and_time_window():
    """v7 opens only inside the measured box: price [0.62,0.86] AND 7-11
    minutes left. Outside it the 17-day measurement is zero or negative
    (T-1..T-3 alone is -4.02c/contract). Completion quotes stay exempt -
    they close risk and must be able to run to the end of the window."""
    from dataclasses import replace as _r
    p = k.Parameters()
    assert (p.entry_band_lo, p.entry_band_hi) == (0.62, 0.86)
    assert (p.entry_rem_lo_s, p.entry_rem_hi_s) == (420., 660.)
    inband = k.normalize_book({"yes_dollars": [[.30, 50]], "no_dollars": [[.68, 50]]})

    def quotes_at(remaining, book=inband, m=None):
        mm = m or market()
        mm["close_ts"] = 1000.
        return k.update_quotes(mm, book, 1000. - remaining, p, residual=False), mm

    # inside the window -> quotes; outside (too early / too late) -> nothing
    created, _ = quotes_at(540.)                      # 9 min left
    assert [o["side"] for o in created] == ["no"]
    assert quotes_at(700.)[0] == []                   # 11.7 min: too early
    assert quotes_at(300.)[0] == []                   # 5 min: too late
    # a completion quote may still be placed late in the window
    m2 = market(); m2["close_ts"] = 1000.
    k.add_fill(m2, "no", 3, .68, 690., 0, liquidity="maker_model", source="x")
    m2["trade_watermark_ts"] = 700.
    created = k.update_quotes(m2, inband, 700., p, residual=False)   # 5 min left
    assert any(o["side"] == "yes" for o in created)
    # and the band edges still bind inside the time window
    rich = k.normalize_book({"yes_dollars": [[.08, 50]], "no_dollars": [[.90, 50]]})
    assert quotes_at(540., rich)[0] == []             # .90 above the band
    cheap = k.normalize_book({"yes_dollars": [[.41, 50]], "no_dollars": [[.57, 50]]})
    assert quotes_at(540., cheap)[0] == []            # .57 below the band
