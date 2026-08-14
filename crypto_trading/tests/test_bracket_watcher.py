"""BracketWatcher daemon tests — the piece that ENFORCES brackets. No network:
a fake market client feeds marks; assert the watcher fires the close on trigger."""
import pytest

from crypto_trading.crypto_common import bracket_watcher as bw
from crypto_trading.crypto_common import execution as ex
from crypto_trading.crypto_common.bracket import Bracket, BracketMonitor
from crypto_trading.crypto_common.bracket_watcher import BracketWatcher
from crypto_trading.crypto_common.execution import ExecutionRouter


class _FakeMarket:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask

    def market(self, ticker):
        return {"bid": str(self.bid), "ask": str(self.ask)}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "SIGNALS_DIR", tmp_path / "signals")
    monkeypatch.setattr(bw, "SIGNALS_DIR", tmp_path / "signals")
    return tmp_path


def test_watcher_loads_persisted_bracket_and_fires(wired):
    # arm a short stop-loss @ 6.70 via a persisted monitor (as the strategy does)
    router = ExecutionRouter("basis_meanrev", env="demo")
    BracketMonitor(router, state_path=bw.state_path("basis_meanrev")).arm(
        Bracket("KXBTCPERP", "ask", 1, stop_loss=6.70, entry_price=6.40))
    router.close()

    # a fresh watcher re-arms from disk, then the mark blows through the stop
    w = BracketWatcher("basis_meanrev", env="demo", live=False)
    w.market = _FakeMarket(bid=6.71, ask=6.73)      # mark ~6.72 ≥ 6.70 stop
    assert "KXBTCPERP" in w.monitor.active()        # re-armed from persistence
    fired = w.tick()
    assert fired and fired[0]["trigger"] == "stop_loss"
    assert fired[0]["order_status"] == "dry_run"    # gate keeps it inert
    assert "KXBTCPERP" not in w.monitor.active()    # consumed after firing


def test_watcher_no_fire_when_mark_inside_bracket(wired):
    router = ExecutionRouter("basis_meanrev", env="demo")
    BracketMonitor(router, state_path=bw.state_path("basis_meanrev")).arm(
        Bracket("KXBTCPERP", "ask", 1, take_profit=6.05, stop_loss=6.70,
                entry_price=6.40))
    router.close()
    w = BracketWatcher("basis_meanrev", env="demo", live=False)
    w.market = _FakeMarket(bid=6.39, ask=6.41)      # mark 6.40 — inside bracket
    assert w.tick() == []
    assert "KXBTCPERP" in w.monitor.active()        # still protecting