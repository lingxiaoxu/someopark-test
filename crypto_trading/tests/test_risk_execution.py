"""risk_kill.py + execution.py tests — tmp dirs, no network, gate must hold."""
import time

import pytest

from crypto_trading.crypto_common import execution as ex
from crypto_trading.crypto_common import risk_kill as rk
from crypto_trading.crypto_common.execution import ExecutionRouter, LiveOrderRefused, Order
from crypto_trading.crypto_common.risk_kill import (Action, GuardConfig, RiskKill,
                                                    RiskState)


@pytest.fixture
def kill(tmp_path, monkeypatch):
    monkeypatch.setattr(rk, "STATE_DIR", tmp_path / "state")
    return RiskKill("teststrat", GuardConfig(max_daily_loss_pct=0.05,
                                             daily_loss_amber_pct=0.03))


def state(**kw) -> RiskState:
    now = time.time()
    base = dict(equity_sod=1000.0, equity_now=1000.0, position_contracts=0.0,
                last_index_ts=now, last_book_ts=now)
    base.update(kw)
    return RiskState(**base)


def test_clean_state_passes(kill):
    ok, why = kill.pre_trade_ok(state())
    assert ok, why


def test_amber_blocks_new_but_allows_reduce(kill):
    s = state(equity_now=960.0)                       # −4%: amber
    ok, why = kill.pre_trade_ok(s, opens_new_risk=True)
    assert not ok and "daily-loss-amber" in why
    ok, _ = kill.pre_trade_ok(s, opens_new_risk=False)
    assert ok


def test_red_trips_persistent_halt(kill):
    s = state(equity_now=940.0)                       # −6%: red
    breaches = kill.evaluate(s)
    assert breaches[0].action is Action.FLATTEN_HALT
    assert kill.halted()                              # halt file written
    # even a healthy state is now blocked until the operator clears it
    ok, why = kill.pre_trade_ok(state())
    assert not ok and "halt-file" in why
    with pytest.raises(ValueError):
        kill.clear_halt("")                           # note required
    kill.clear_halt("tested and reset")
    ok, _ = kill.pre_trade_ok(state())
    assert ok


def test_staleness_blocks_new(kill):
    s = state(last_index_ts=time.time() - 120)
    ok, why = kill.pre_trade_ok(s)
    assert not ok and "stale-index" in why


def test_liq_distance_red(kill):
    s = state(liq_distance_pct=0.10, position_contracts=5)
    breaches = kill.evaluate(s)
    assert any(b.guard == "liq-distance" and b.action is Action.FLATTEN_HALT
               for b in breaches)


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "SIGNALS_DIR", tmp_path / "signals")
    return ExecutionRouter("teststrat", env="demo")


def test_dry_run_default_writes_record(router, tmp_path):
    res = router.submit(Order("KXBTCPERP", "bid", 2, 6.3800))
    assert res["status"] == "dry_run"
    router.close()
    files = list((tmp_path / "signals" / "orders_dryrun" / "teststrat").rglob("*.jsonl"))
    assert files and "KXBTCPERP" in files[0].read_text()


def test_duplicate_client_order_id_deduped(router):
    o = Order("KXBTCPERP", "bid", 1, 6.38)
    assert router.submit(o)["status"] == "dry_run"
    assert router.submit(o)["status"] == "duplicate"


def test_live_refused_when_gate_closed(router):
    # demo env + no dedicated key + margin not enabled → every condition fails
    with pytest.raises(LiveOrderRefused):
        router.submit(Order("KXBTCPERP", "bid", 1, 6.38), live=True)
    gate = router.gate_status()
    assert not gate["live_open"] and not gate["env_prod"]


def test_order_body_matches_official_spec():
    # short (ask), like the real 2026-07-12 fill; body must match create-order spec
    o = Order("KXBTCPERP", "ask", 3, 6.3819, post_only=True, subaccount=64)
    b = o.body()
    assert b["side"] == "ask"                       # bid/ask, NOT buy/sell
    assert b["count"] == "3.00"                     # 2-dp fixed point, NOT "3"
    assert b["price"] == "6.3819"
    assert b["subaccount"] == 64                    # targets the right subaccount
    assert b["self_trade_prevention_type"] == "taker_at_cross"
    assert b["time_in_force"] == "good_till_canceled"
    assert b["post_only"] is True and "client_order_id" in b


def test_order_rejects_buy_sell_and_bad_enums():
    import pytest
    with pytest.raises(ValueError):
        Order("KXBTCPERP", "buy", 1, 6.38)          # buy/sell no longer accepted
    with pytest.raises(ValueError):
        Order("KXBTCPERP", "bid", 1, 6.38, tif="gtc")   # invalid TIF
    with pytest.raises(ValueError):
        Order("KXBTCPERP", "bid", 0.005, 6.38)      # below 0.01 granularity


def test_from_signed_maps_sign_to_side():
    assert Order.from_signed("KXBTCPERP", -1, 6.40).side == "ask"   # short
    assert Order.from_signed("KXBTCPERP", +2, 6.40).side == "bid"   # long


def test_reduce_only_requires_ioc_fok():
    import pytest
    with pytest.raises(ValueError):
        Order("KXBTCPERP", "ask", 1, 6.40, reduce_only=True)        # gtc + reduce_only
    ok = Order("KXBTCPERP", "ask", 1, 6.40, reduce_only=True,
               tif="immediate_or_cancel")
    assert ok.body()["reduce_only"] is True


class _FakeMargin:
    """Returns positions in the EXACT /margin/positions schema (real fill 2026-07-12)."""
    def __init__(self, positions):
        self._p = positions

    def positions(self):
        return self._p


def test_reconcile_matches_real_position_schema(router):
    # venue = short 1 KXBTCPERP (position "-1.00"), matching the real account
    router._margin = _FakeMargin([
        {"market_ticker": "KXBTCPERP", "position": "-1.00", "entry_price": "6.4015",
         "unrealized_pnl": "-0.0014", "subaccount": 64}])
    # inventory agrees → no break
    assert router.reconcile({"KXBTCPERP": -1.0})["ok"]
    # inventory disagrees (thinks we're long 1) → break flagged
    r = router.reconcile({"KXBTCPERP": 1.0})
    assert not r["ok"]
    assert r["breaks"]["KXBTCPERP"] == {"inventory": 1.0, "venue": -1.0}
    # venue has a position we don't know about → break
    r2 = router.reconcile({})
    assert not r2["ok"] and r2["breaks"]["KXBTCPERP"]["venue"] == -1.0


def test_reconcile_nets_same_ticker_across_subaccounts(router):
    # real account holds KXBTCPERP in BOTH subaccount 64 (-1) and 0 (+2) → net +1
    router._margin = _FakeMargin([
        {"market_ticker": "KXBTCPERP", "position": "-1.00", "subaccount": 64},
        {"market_ticker": "KXBTCPERP", "position": "2.00", "subaccount": 0}])
    # netted (not last-wins) → +1
    assert router.reconcile({"KXBTCPERP": 1.0})["ok"], "should NET to +1, not overwrite"
    # restrict to subaccount 64 only → -1
    r = router.reconcile({"KXBTCPERP": -1.0}, subaccount=64)
    assert r["ok"] and r["venue_positions"]["KXBTCPERP"] == -1.0


def test_order_price_snapped_to_tick_size():
    # off-tick price must snap; bid rounds DOWN, ask rounds UP
    bid = Order("KXBTCPERP", "bid", 1, 6.40125, tick_size=0.001)
    assert bid.wire_price() == pytest.approx(6.401) and bid.body()["price"] == "6.4010"
    ask = Order("KXBTCPERP", "ask", 1, 6.40125, tick_size=0.001)
    assert ask.wire_price() == pytest.approx(6.402)
    # no tick_size → unchanged
    assert Order("KXBTCPERP", "bid", 1, 6.4015).wire_price() == pytest.approx(6.4015)


def test_order_count_must_be_multiple_of_001():
    import pytest as _p
    with _p.raises(ValueError):
        Order("KXBTCPERP", "bid", 1.005, 6.40)      # not a 0.01 multiple
    assert Order("KXBTCPERP", "bid", 1.50, 6.40).body()["count"] == "1.50"
