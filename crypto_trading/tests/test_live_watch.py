"""live_watch safety tests — the properties that must never regress:
ships disarmed, dry-run never submits, kill switch trips and stays tripped."""
import json

import pytest

from crypto_trading.crypto_common.execution import Order
from crypto_trading.crypto_strategies.live_watch import common


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    return tmp_path


def test_config_ships_fully_disarmed():
    cfg = common.load_cfg()
    for k, v in cfg.items():
        if isinstance(v, dict) and "enabled" in v:
            assert v["enabled"] is False, f"{k} must ship disabled"
    assert cfg["subaccount"] == 64


def test_emit_disabled_never_submits(sandbox):
    o = Order.from_signed("KXBTCPERP", -10, 6.40, post_only=True, subaccount=64)
    rec = common.emit("w_test", o, enabled=False, reason="unit")
    assert rec["submitted"] is False
    assert "DRY-RUN" in rec["note"]
    # and the intended order was fully logged for the rehearsal record
    logs = list(sandbox.glob("log_*.jsonl"))
    assert logs and "KXBTCPERP" in logs[0].read_text()


def test_emit_enabled_but_gates_closed_never_submits(sandbox):
    """Level-1 on, level-2 (prod key/ALLOW_LIVE_ORDERS) off → still dry."""
    o = Order.from_signed("KXBTCPERP", 10, 6.40, post_only=True, subaccount=64)
    rec = common.emit("w_test", o, enabled=True, reason="unit")
    assert rec["submitted"] is False


def test_kill_switch_trips_and_persists(sandbox):
    st = {"position": None, "trades": [{}] * 50, "cum_net_usd": -99.0,
          "killed": False}
    cfg = {"max_cum_loss_usd": 30, "min_trades_for_kill": 40}
    assert common.kill_check("w_test", st, cfg) is True
    assert st["killed"] is True
    # persisted: a fresh load sees the kill
    st2 = common.load_state("w_test")
    assert st2["killed"] is True
    # and stays tripped even if P&L recovers
    st2["cum_net_usd"] = +10.0
    assert common.kill_check("w_test", st2, cfg) is True


def test_state_roundtrip(sandbox):
    st = common.load_state("w_rt")
    st["position"] = {"side": -1, "opened": "2026-08-10"}
    common.save_state("w_rt", st)
    back = common.load_state("w_rt")
    assert back["position"]["side"] == -1
    assert json.loads(common.state_path("w_rt").read_text())["position"]["opened"]


# ── W5 knockdown pure-logic tests ────────────────────────────────────────────

def test_knockdown_trigger_fires_only_on_fresh_dip():
    from crypto_trading.crypto_strategies.event_binary.research_knockdown import (
        knockdown_trigger)
    flat = [(0.50, 0.50)] * 5
    assert knockdown_trigger(flat, 0.50, 0.50, dip_c=0.05) is None
    # yes side knocked from 0.50 → 0.38: fires "yes"
    assert knockdown_trigger(flat, 0.38, 0.62, dip_c=0.05) == "yes"
    # dip smaller than threshold: silent
    assert knockdown_trigger(flat, 0.47, 0.53, dip_c=0.05) is None
    # knocked but OUT of the imported zone (too cheap): silent
    assert knockdown_trigger(flat, 0.08, 0.92, dip_c=0.05) is None
    # insufficient history: silent
    assert knockdown_trigger(flat[:3], 0.38, 0.62, dip_c=0.05) is None


def test_knockdown_settle_and_fee():
    from crypto_trading.crypto_strategies.event_binary.research_knockdown import (
        fee, settle_outcome)
    assert settle_outcome("yes", 64000, 64100) is True
    assert settle_outcome("yes", 64000, 63900) is False
    assert settle_outcome("no", 64000, 63900) is True
    assert abs(fee(0.5) - 0.0175) < 1e-9          # 0.07·P(1−P) peak
    assert fee(0.22, 0.10) > fee(0.22, 0.07)      # premium-mult sensitivity
