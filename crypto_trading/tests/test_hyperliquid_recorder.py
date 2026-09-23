"""Hyperliquid alt-data recorder: causality of the address pool and dedup."""
import json

import pytest

from crypto_trading.crypto_common.refdata import hyperliquid as hl


class FakeInfo:
    """Scripted /info: returns queued responses by request type."""

    def __init__(self, responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls = []
        self.counts = {"ok": 0, "rate_limited": 0, "error": 0}

    def post(self, body, timeout=10.0):
        self.calls.append(body)
        queue = self.responses.get(body["type"])
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]


def trade(tid, users, coin="BTC", ts=1_000, px="100", sz="1"):
    return dict(coin=coin, side="A", px=px, sz=sz, time=ts, tid=tid,
                hash=f"0x{tid:064x}", users=users)


def recorder(tmp_path, responses, coins=("BTC",)):
    rec = hl.Recorder(coins=coins, root=tmp_path)
    rec.info = FakeInfo(responses)
    return rec


def rows(tmp_path, sub):
    files = sorted((tmp_path / sub).glob("*.jsonl"))
    return [json.loads(l) for f in files for l in f.read_text().splitlines()]


def test_address_pool_only_ever_contains_already_observed_counterparties(tmp_path):
    """The pool must be a strict function of prints already received.

    This is the property the whole tape rests on: a pool built any other way
    (today's profitable accounts, say) makes every historical study of it a
    selection on the future.
    """
    a, b, c = "0xaaa", "0xbbb", "0xccc"
    rec = recorder(tmp_path, {"recentTrades": [[trade(1, [a, b])],
                                               [trade(1, [a, b]), trade(2, [a, c])]]})
    w = hl.DailyJsonlWriter(tmp_path)
    rec.trades(w, w, "BTC")
    assert set(rec.pool.seen) == {a, b}          # c has not printed yet
    rec.trades(w, w, "BTC")
    assert set(rec.pool.seen) == {a, b, c}
    w.close()
    pool = rows(tmp_path, "address_pool")
    assert [p["address"] for p in pool] == [a, b, c]
    assert all(p["recv_ts"] > 0 and p["first_seen_tid"] in (1, 2) for p in pool)
    # a traded twice, and the pool records that without re-emitting it
    assert rec.pool.seen[a]["prints"] == 2 and rec.pool.seen[c]["prints"] == 1


def test_recent_trades_window_is_deduplicated_by_tid(tmp_path):
    """recentTrades is a rolling window; re-polling must not re-record it."""
    same = [trade(1, ["0xaaa"]), trade(2, ["0xbbb"])]
    rec = recorder(tmp_path, {"recentTrades": [same]})
    w = hl.DailyJsonlWriter(tmp_path)
    for _ in range(4):
        rec.trades(w, w, "BTC")
    w.close()
    assert [t["tid"] for t in rows(tmp_path, "trades/BTC")] == [1, 2]
    assert rec.stats["trades"] == 2


def test_every_row_carries_both_our_receive_time_and_the_venue_clock(tmp_path):
    rec = recorder(tmp_path, {
        "metaAndAssetCtxs": [[{"universe": [{"name": "BTC"}]},
                              [{"markPx": "77000", "oraclePx": "77010",
                                "openInterest": "36000", "funding": "0.0000125",
                                "premium": "-0.0003", "dayNtlVlm": "1.6e9"}]]],
        "l2Book": [{"time": 1_700, "levels": [[{"px": "76999", "sz": "3", "n": 2}],
                                              [{"px": "77001", "sz": "4", "n": 5}]]}],
        "recentTrades": [[trade(7, ["0xaaa"], ts=1_650)]]})
    w = hl.DailyJsonlWriter(tmp_path)
    rec.context(w)
    rec.book(w, "BTC")
    rec.trades(w, w, "BTC")
    w.close()
    ctx = rows(tmp_path, "context/BTC")[0]
    assert ctx["recv_ts"] > 0 and ctx["premium"] == "-0.0003" and ctx["funding"]
    book = rows(tmp_path, "book/BTC")[0]
    assert book["recv_ts"] > 0 and book["venue_ts"] == 1_700
    assert book["bids"] == [["76999", "3", 2]] and book["asks"] == [["77001", "4", 5]]
    tr = rows(tmp_path, "trades/BTC")[0]
    assert tr["recv_ts"] > 0 and tr["venue_ts"] == 1_650


def test_accounts_keep_raw_margin_state_and_filter_to_our_coins(tmp_path):
    """No derived risk number is written: cross liquidation is account-level,
    so the tape must keep what a later model needs to compute it."""
    rec = recorder(tmp_path, {
        "recentTrades": [[trade(1, ["0xaaa"])]],
        "clearinghouseState": [{
            "marginSummary": {"accountValue": "100", "totalMarginUsed": "40"},
            "crossMaintenanceMarginUsed": "12",
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "2", "entryPx": "77000",
                              "liquidationPx": None, "positionValue": "154000",
                              "marginUsed": "3850", "leverage": {"type": "cross", "value": 40}}},
                {"position": {"coin": "PEPE", "szi": "9"}}]}],
        "userTwapSliceFills": [[{"fill": {"coin": "BTC", "sz": "0.5", "side": "B",
                                          "tid": 501}},
                                {"fill": {"coin": "PEPE", "sz": "1", "tid": 502}}]]})
    w = hl.DailyJsonlWriter(tmp_path)
    rec.trades(w, w, "BTC")
    rec.accounts(w, w, n=1)
    w.close()
    acct = rows(tmp_path, "accounts")[0]
    assert acct["margin_summary"]["accountValue"] == "100"
    assert acct["cross_maintenance_margin_used"] == "12"
    assert [p["coin"] for p in acct["positions"]] == ["BTC"]   # PEPE filtered
    assert acct["positions"][0]["liquidation_px"] is None      # kept, not invented
    twap = rows(tmp_path, "twap_fills")[0]
    assert [f["fill"]["coin"] for f in twap["fills"]] == ["BTC"]


def test_pool_rotation_is_round_robin_and_not_ranked(tmp_path):
    pool = hl.AddressPool()
    for i in range(5):
        pool.add(f"0x{i}", ts=i, coin="BTC")
    pool.seen["0x0"]["prints"] = 999          # activity must not buy priority
    assert pool.next_batch(2) == ["0x0", "0x1"]
    assert pool.next_batch(2) == ["0x2", "0x3"]
    assert pool.next_batch(2) == ["0x4", "0x0"]


def test_pool_is_bounded_and_drops_the_oldest_first(tmp_path):
    pool = hl.AddressPool(maxlen=3)
    for i in range(5):
        pool.add(f"0x{i}", ts=i, coin="BTC")
    assert list(pool.seen) == ["0x2", "0x3", "0x4"]


def test_a_failing_cycle_never_ends_the_tape(tmp_path):
    rec = recorder(tmp_path, {})              # every request returns None
    out = rec.record(interval=0.0, cycles=3)
    assert out["cycles"] == 3 and out["trades"] == 0


def test_second_instance_refuses_to_start(tmp_path):
    a = hl.Recorder(root=tmp_path)
    a.acquire()
    try:
        with pytest.raises(SystemExit, match="already running"):
            hl.Recorder(root=tmp_path).acquire()
    finally:
        a.release()
    hl.Recorder(root=tmp_path).acquire()       # lock released, so it may start


def test_twap_slice_history_is_deduplicated_across_polls(tmp_path):
    """userTwapSliceFills returns the address's whole history, every poll."""
    history = [{"fill": {"coin": "BTC", "sz": "0.5", "side": "B", "tid": 11}},
               {"fill": {"coin": "BTC", "sz": "0.5", "side": "B", "tid": 12}},
               {"fill": {"coin": "PEPE", "sz": "1", "tid": 13}}]
    rec = recorder(tmp_path, {
        "recentTrades": [[trade(1, ["0xaaa"])]],
        "clearinghouseState": [{"marginSummary": {}, "assetPositions": []}],
        "userTwapSliceFills": [history]})
    w = hl.DailyJsonlWriter(tmp_path)
    rec.trades(w, w, "BTC")
    for _ in range(3):
        rec.accounts(w, w, n=1)
    w.close()
    written = rows(tmp_path, "twap_fills")
    assert len(written) == 1                       # only the first poll wrote
    assert [f["fill"]["tid"] for f in written[0]["fills"]] == [11, 12]
    assert rec.stats["twap_fills"] == 2
