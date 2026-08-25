"""live_watch safety tests — the properties that must never regress:
ships disarmed, dry-run never submits, kill switch trips and stays tripped."""
import json

import pandas as pd
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


# ── W6 residual jump lead-lag pure-logic tests ───────────────────────────────

def test_residual_is_the_unfollowed_gap():
    from crypto_trading.crypto_strategies.jump_leadlag.research_residual import (
        residual_bps)
    # index +30bps, perp hasn't moved → full gap is tradeable
    assert residual_bps(30.0, 0.0) == pytest.approx(30.0)
    # index +30, perp already followed +25 → only 5 left
    assert residual_bps(30.0, 25.0) == pytest.approx(5.0)
    # perp OVERSHOT the index → negative residual, never traded
    assert residual_bps(30.0, 40.0) < 0
    # down-jump mirrors exactly (sign handling is the easy bug here)
    assert residual_bps(-30.0, -25.0) == pytest.approx(5.0)
    assert residual_bps(-30.0, 0.0) == pytest.approx(30.0)
    assert residual_bps(-30.0, -40.0) < 0


def test_w6_filter_thresholds_are_frozen():
    from crypto_trading.crypto_strategies.jump_leadlag import research_residual as rr
    assert (rr.RESIDUAL_MIN, rr.SPREAD_MAX_BPS) == (7.0, 2.1)
    assert (rr.JUMP_BPS, rr.HOLD_MIN) == (25.0, 1)
    ev = pd.DataFrame({"residual_bps": [8.0, 6.0, 9.0],
                       "spread_bps": [1.5, 1.5, 3.0]})
    kept = rr.apply_filter(ev)
    assert list(kept.index) == [0]          # only residual>7 AND spread<=2.1


def test_w6_live_imports_backtest_constants():
    """live and canonical must share one source of truth, not two copies."""
    from crypto_trading.crypto_strategies.jump_leadlag import research_residual as rr
    from crypto_trading.crypto_strategies.live_watch import w6_residual as w6
    assert w6.RESIDUAL_MIN is rr.RESIDUAL_MIN
    assert w6.SPREAD_MAX_BPS is rr.SPREAD_MAX_BPS
    assert w6.JUMP_BPS is rr.JUMP_BPS


def test_w6_exit_path_executes(sandbox, monkeypatch):
    """Drive the EXIT branch end-to-end.

    Regression: a rename during the tier-aware-accounting edit left the exit
    branch referencing a dead name. Entry-only smoke tests passed for a full
    day while every exit raised NameError, so the probe silently stopped
    settling. Exit paths need their own test — they are the branch that
    touches P&L.
    """
    from crypto_trading.crypto_strategies.live_watch import w6_residual as w6

    st = {"positions": {"KXBTCPERP": {"side": 1, "entry": 6.40,
                                      "opened": "2026-08-24T00:00:00+00:00",
                                      "exit_due": "2026-08-24T00:01:00+00:00"}},
          "probe": {"jumps": 5, "fired": 1}, "trades": [], "cum_net_usd": 0.0}
    common.save_state("w6_residual", st)

    idx = pd.date_range("2026-08-24", periods=3, freq="10s", tz="UTC")
    quotes = pd.DataFrame({"bid": [6.44, 6.45, 6.45], "ask": [6.46, 6.47, 6.47]},
                          index=idx)
    monkeypatch.setattr(w6, "load_poll_market_stats", lambda *a, **k: quotes)

    rep = w6.run({"w6_residual": {"enabled": False, "contracts": 10,
                                  "max_cum_loss_usd": 30,
                                  "min_trades_for_kill": 40}})

    btc = rep["markets"]["KXBTCPERP"]
    assert btc["status"] == "EXIT"
    assert "net_bps" in btc and "net_bps_t4" in btc
    # long from 6.40 exited at the bid 6.45 → gross ≈ +78bps, well above fees
    assert btc["net_bps"] > 0 and btc["net_bps_t4"] > btc["net_bps"]
    back = common.load_state("w6_residual")
    assert len(back["trades"]) == 1
    assert back["positions"]["KXBTCPERP"] is None
    assert back["cum_net_usd"] > 0
