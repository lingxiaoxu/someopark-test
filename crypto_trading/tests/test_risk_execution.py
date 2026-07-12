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
    res = router.submit(Order("KXBTCPERP", "buy", 2, 6.3800))
    assert res["status"] == "dry_run"
    router.close()
    files = list((tmp_path / "signals" / "orders_dryrun" / "teststrat").rglob("*.jsonl"))
    assert files and "KXBTCPERP" in files[0].read_text()


def test_duplicate_client_order_id_deduped(router):
    o = Order("KXBTCPERP", "buy", 1, 6.38)
    assert router.submit(o)["status"] == "dry_run"
    assert router.submit(o)["status"] == "duplicate"


def test_live_refused_when_gate_closed(router):
    # demo env + no dedicated key + margin not enabled → every condition fails
    with pytest.raises(LiveOrderRefused):
        router.submit(Order("KXBTCPERP", "buy", 1, 6.38), live=True)
    gate = router.gate_status()
    assert not gate["live_open"] and not gate["env_prod"]


def test_order_body_wire_format():
    o = Order("KXBTCPERP", "sell", 3, 6.3819, post_only=True)
    b = o.body()
    assert b["count"] == "3" and b["price"] == "6.3819"
    assert b["post_only"] is True and "client_order_id" in b
