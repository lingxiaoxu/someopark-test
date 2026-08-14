"""TP/SL bracket tests — synthetic, no network. Uses the real screenshot values
(short KXBTCPERP, TP $60,595 / SL $66,974 underlying) as a ground-truth case."""
import pytest

from crypto_trading.crypto_common import execution as ex
from crypto_trading.crypto_common.bracket import (Bracket, BracketMonitor,
                                                  contract_to_underlying,
                                                  underlying_to_contract)
from crypto_trading.crypto_common.execution import ExecutionRouter

CSIZE = 0.0001          # KXBTCPERP contract size


def test_underlying_contract_conversion_roundtrip():
    # app shows $60,595 underlying → 6.0595 contract price
    assert underlying_to_contract(60_595, CSIZE) == pytest.approx(6.0595)
    assert contract_to_underlying(6.0595, CSIZE) == pytest.approx(60_595)


def test_short_bracket_matches_screenshot_geometry():
    # real position: SHORT, entry ~6.4015 ($64,015), TP $60,595 (below), SL $66,974 (above)
    b = Bracket("KXBTCPERP", "ask", 1,
                take_profit=underlying_to_contract(60_595, CSIZE),
                stop_loss=underlying_to_contract(66_974, CSIZE),
                entry_price=6.4015)
    # price falls to TP → take profit (short wins)
    assert b.triggered(underlying_to_contract(60_500, CSIZE)) == "take_profit"
    # price rises to SL → stop loss (short loses)
    assert b.triggered(underlying_to_contract(67_000, CSIZE)) == "stop_loss"
    # in between → nothing
    assert b.triggered(6.4015) is None


def test_long_bracket_geometry():
    b = Bracket("KXBTCPERP", "bid", 2, take_profit=6.60, stop_loss=6.20,
                entry_price=6.40)
    assert b.triggered(6.61) == "take_profit"      # long wins when price rises
    assert b.triggered(6.19) == "stop_loss"        # long loses when price falls
    assert b.triggered(6.40) is None


def test_inverted_bracket_rejected():
    with pytest.raises(ValueError):               # long TP below entry = nonsense
        Bracket("KXBTCPERP", "bid", 1, take_profit=6.20, entry_price=6.40)
    with pytest.raises(ValueError):               # short SL below entry = nonsense
        Bracket("KXBTCPERP", "ask", 1, stop_loss=6.20, entry_price=6.40)
    with pytest.raises(ValueError):               # empty bracket
        Bracket("KXBTCPERP", "ask", 1)


def test_close_order_is_reduce_only_ioc_opposite_side():
    b = Bracket("KXBTCPERP", "ask", 3, stop_loss=6.70, entry_price=6.40)
    o = b.close_order(6.71, subaccount=64)
    assert o.side == "bid"                         # close a short by buying
    assert o.reduce_only and o.tif == "immediate_or_cancel"
    assert o.count == 3 and o.subaccount == 64
    body = o.body()
    assert body["reduce_only"] is True and body["count"] == "3.00"


def test_bracket_close_uses_own_subaccount_by_default():
    # HIGH bug fix: a bracket on subaccount 64 must close to 64, not 0
    b = Bracket("KXBTCPERP", "ask", 1, stop_loss=6.70, entry_price=6.40, subaccount=64)
    assert b.close_order(6.71).subaccount == 64          # defaults to bracket's own
    assert b.close_order(6.71, subaccount=7).subaccount == 7   # explicit override wins


def test_bracket_subaccount_persists(router, tmp_path):
    sp = tmp_path / "b.json"
    BracketMonitor(router, state_path=sp).arm(
        Bracket("KXBTCPERP", "ask", 1, stop_loss=6.70, entry_price=6.40, subaccount=64))
    reloaded = BracketMonitor(router, state_path=sp).active()["KXBTCPERP"]
    assert reloaded.subaccount == 64                     # survives restart


def test_corrupt_state_file_quarantined_not_silent(router, tmp_path):
    sp = tmp_path / "b.json"
    sp.write_text("{ this is not json")
    m = BracketMonitor(router, state_path=sp)            # must not crash
    assert m.active() == {}
    assert (tmp_path / "b.json.corrupt").exists()        # quarantined, not silently dropped


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "SIGNALS_DIR", tmp_path / "signals")
    return ExecutionRouter("teststrat", env="demo")


def test_monitor_fires_close_on_stop_and_is_gated(router):
    mon = BracketMonitor(router, live=False)        # dry-run (gate closed)
    mon.arm(Bracket("KXBTCPERP", "ask", 1, stop_loss=6.70, entry_price=6.40))
    # mark below stop → no trigger
    assert mon.on_mark("KXBTCPERP", 6.50, 6.51) is None
    assert "KXBTCPERP" in mon.active()
    # mark hits stop → fires a dry-run close, disarms
    ev = mon.on_mark("KXBTCPERP", 6.71, 6.72)
    assert ev["trigger"] == "stop_loss"
    assert ev["order_status"] == "dry_run"          # gate keeps it inert
    assert "KXBTCPERP" not in mon.active()          # bracket consumed
    router.close()


def test_monitor_take_profit_path(router):
    mon = BracketMonitor(router, live=False)
    mon.arm(Bracket("KXBTCPERP", "ask", 2, take_profit=6.05, entry_price=6.40))
    ev = mon.on_mark("KXBTCPERP", 6.04, 6.03)
    assert ev["trigger"] == "take_profit" and ev["contracts"] == 2


def test_bracket_persistence_survives_reload(router, tmp_path):
    sp = tmp_path / "brackets.json"
    m1 = BracketMonitor(router, state_path=sp)
    m1.arm(Bracket("KXBTCPERP", "ask", 3, take_profit=6.05, stop_loss=6.70,
                   entry_price=6.40))
    assert sp.exists()
    # a fresh monitor (simulating a daemon restart) re-arms from disk
    m2 = BracketMonitor(router, state_path=sp)
    b = m2.active()["KXBTCPERP"]
    assert b.side == "ask" and b.contracts == 3 and b.stop_loss == 6.70
    m2.disarm("KXBTCPERP")
    assert BracketMonitor(router, state_path=sp).active() == {}   # persisted removal


def test_inverted_tp_sl_rejected_without_entry():
    # no entry_price, but TP/SL still on the wrong sides → must raise
    with pytest.raises(ValueError):
        Bracket("KXBTCPERP", "bid", 1, take_profit=6.20, stop_loss=6.60)   # long TP<SL
    with pytest.raises(ValueError):
        Bracket("KXBTCPERP", "ask", 1, take_profit=6.60, stop_loss=6.20)   # short TP>SL


def test_disarm_kept_on_duplicate_close(router, monkeypatch):
    # if the close returns "duplicate" the position DIDN'T close → keep bracket armed
    mon = BracketMonitor(router, live=False)
    mon.arm(Bracket("KXBTCPERP", "ask", 1, stop_loss=6.70, entry_price=6.40))
    monkeypatch.setattr(router, "submit", lambda *a, **k: {"status": "duplicate"})
    ev = mon.on_mark("KXBTCPERP", 6.71, 6.72)
    assert ev["trigger"] == "stop_loss" and ev["bracket_consumed"] is False
    assert "KXBTCPERP" in mon.active()          # still protected
