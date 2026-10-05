"""live_watch safety tests — the properties that must never regress:
ships disarmed, dry-run never submits, kill switch trips and stays tripped."""
import json
from pathlib import Path

import pandas as pd
import pytest

from crypto_trading.crypto_common.execution import Order
from crypto_trading.crypto_strategies.live_watch import common


@pytest.fixture(autouse=True)
def _no_live_recorder_reads(monkeypatch):
    """No test in this file may read LIVE recorder files (2026-09-30).

    The day-band flow gate and the no-dump tier read the HL / index recorder
    tails; pinning only the UTC hour left submit() tests flickering with real
    market conditions (a genuine skip turned `live_disarmed` into
    `skipped_by_flow_gate`). Tests that exercise the gate stub decide()
    explicitly and never depend on real rows."""
    import crypto_trading.crypto_common.execution_events as ee
    monkeypatch.setattr(ee.EventExecutionRouter, "_tail_rows",
                        staticmethod(lambda path, n: []))


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
    # threshold (T) markets: no cap
    assert settle_outcome("yes", 64000, 64100) is True
    assert settle_outcome("yes", 64000, 63900) is False
    assert settle_outcome("no", 64000, 63900) is True
    # BUCKET (B) markets: cap breach = yes LOSES (the bug the official
    # results exposed: B64250 settled 64376.33 → "no")
    assert settle_outcome("yes", 64200, 64376.33, cap=64299.99) is False
    assert settle_outcome("no", 64200, 64376.33, cap=64299.99) is True
    assert settle_outcome("yes", 64200, 64250.0, cap=64299.99) is True
    assert settle_outcome("yes", 64200, 64150.0, cap=64299.99) is False
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


# ── demo mirror mapping tests ────────────────────────────────────────────────

def test_order_to_demo_translates_ticker_and_subaccount():
    o = Order.from_signed("KXBTCPERP", 10, 6.40, post_only=True, subaccount=64)
    d = o.to_demo()
    assert d.ticker == "KXBTCPERP1"          # demo suffix (probed 2026-08-23)
    assert d.subaccount == 0                 # demo's only subaccount
    assert d.client_order_id != o.client_order_id
    assert d.client_order_id.startswith("demo-")
    # already-suffixed ticker must not double up
    assert d.to_demo().ticker == "KXBTCPERP1"
    # economics untouched
    assert (d.side, d.count, d.price, d.post_only) == (o.side, o.count, o.price, True)


def test_emit_demo_mirror_off_by_default(sandbox, monkeypatch):
    """With demo_mirror false (the shipped default) emit must NOT touch the
    demo path at all."""
    from crypto_trading.crypto_common.execution import ExecutionRouter
    called = []
    monkeypatch.setattr(ExecutionRouter, "submit_demo",
                        lambda self, order: called.append(order) or {})
    o = Order.from_signed("KXBTCPERP", 10, 6.40, post_only=True, subaccount=64)
    rec = common.emit("w_test", o, enabled=False, reason="unit")
    assert called == []
    assert "demo_mirror" not in rec


# ── events execution layer (W5 demo mirror lives in crypto_common, not in
#    the strategy module — layering rule re-affirmed 2026-08-25) ─────────────

def test_choose_demo_market_maps_close_hour_and_zone():
    from crypto_trading.crypto_common.execution_events import choose_demo_market
    mkts = [
        {"ticker": "A", "close_time": "2026-08-26T01:00:00Z", "yes_ask": 22, "no_ask": 80},
        {"ticker": "B", "close_time": "2026-08-26T01:00:00Z", "yes_ask": 45, "no_ask": 57},
        {"ticker": "C", "close_time": "2026-08-26T02:00:00Z", "yes_ask": 21, "no_ask": 81},  # wrong hour
        {"ticker": "D", "close_time": "2026-08-26T01:00:00Z", "yes_ask": 0, "no_ask": 100},  # unquoted
    ]
    # yes-side intent at 0.21 → nearest same-hour yes ask is A@22c (not C)
    assert choose_demo_market(mkts, "2026-08-26T01:00:00Z", "yes", 0.21) == (
        "A", 22, "2026-08-26T01:00:00Z")
    # no-side intent at 0.55 → B's no_ask 57c
    assert choose_demo_market(mkts, "2026-08-26T01:00:00Z", "no", 0.55) == (
        "B", 57, "2026-08-26T01:00:00Z")
    # unquoted hour → graceful fallback to nearest-price QUOTED market,
    # with the mapped close reported (demo only quotes dailies — measured)
    t, a, ct = choose_demo_market(mkts, "2026-08-26T03:00:00Z", "yes", 0.21)
    assert (t, a) == ("C", 21) and ct == "2026-08-26T02:00:00Z"  # nearest price wins
    # demo batch schema: *_dollars STRING fields must parse identically
    dm = [{"ticker": "D", "close_time": "2026-08-26T01:00:00Z",
           "yes_ask_dollars": "0.2200", "no_ask_dollars": "0.8000"}]
    assert choose_demo_market(dm, "2026-08-26T01:00:00Z", "yes", 0.21) == (
        "D", 22, "2026-08-26T01:00:00Z")
    # "1.0000" = empty-book sentinel → unquoted
    dm2 = [{"ticker": "E", "close_time": "2026-08-26T01:00:00Z",
            "no_ask_dollars": "1.0000"}]
    assert choose_demo_market(dm2, "2026-08-26T01:00:00Z", "no", 0.5) is None
    # nothing quoted anywhere → None
    assert choose_demo_market(
        [{"ticker": "X", "close_time": "2026-08-26T03:00:00Z",
          "yes_ask": 0, "no_ask": 100}],
        "2026-08-26T03:00:00Z", "yes", 0.3) is None


def test_choose_demo_market_15m_is_same_window_only_and_never_closed():
    """15M mirror (2026-09-01): a cached list can hold windows that already
    closed (117 x 409 market_closed measured) and the flagship fallback maps
    a 15-minute bet onto a different market entirely. With now_iso closed
    windows are dropped; with exact_only there is no fallback at all."""
    from crypto_trading.crypto_common.execution_events import choose_demo_market
    mkts = [
        {"ticker": "OLD", "close_time": "2026-09-01T20:30:00Z", "yes_ask": 70, "no_ask": 32},
        {"ticker": "CUR", "close_time": "2026-09-01T21:15:00Z", "yes_ask": 71, "no_ask": 31},
        {"ticker": "NXT", "close_time": "2026-09-01T21:30:00Z", "yes_ask": 69, "no_ask": 33},
    ]
    now = "2026-09-01T21:07:00Z"
    # exact window present → it, never the closed one even if price-closer
    assert choose_demo_market(mkts, "2026-09-01T21:15:00Z", "yes", 0.70,
                              now_iso=now, exact_only=True) == (
        "CUR", 71, "2026-09-01T21:15:00Z")
    # exact window absent → None (no fallback to NXT), instead of a wrong bet
    assert choose_demo_market(mkts, "2026-09-01T21:45:00Z", "yes", 0.70,
                              now_iso=now, exact_only=True) is None
    # graceful (non-15M) path still falls back, but only to FUTURE closes
    t, a, ct = choose_demo_market(mkts, "2026-09-01T21:45:00Z", "yes", 0.70,
                                  now_iso=now)
    assert t in ("CUR", "NXT") and ct > now
    # legacy call without now_iso is unchanged (old tests above)


def test_w5_module_has_no_venue_code():
    """The strategy file must not touch venue clients directly."""
    import inspect
    from crypto_trading.crypto_strategies.live_watch import w5_knockdown as w5
    src = inspect.getsource(w5)
    assert "KalshiEventOrderClient" not in src
    assert "create_order" not in src
    assert "EventExecutionRouter" in src          # goes through the layer


def test_events_v2_body_translates_no_side():
    """Buying NO at p must become an ASK on the YES book at 1−p (V2 single
    book), with fixed-point dollar price — probed against the live V2 docs."""
    from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient
    b = KalshiEventOrderClient.v2_body(ticker="T", contract_side="no",
                                       price_dollars=0.01, count=25)
    assert (b["side"], b["price"], b["count"]) == ("ask", "0.9900", "25.00")
    y = KalshiEventOrderClient.v2_body(ticker="T", contract_side="yes",
                                       price_dollars=0.22, count=25)
    assert (y["side"], y["price"]) == ("bid", "0.2200")


# ── isolation principle (user, 2026-08-25): the 24/7 probes must be
#    unaffectable by the demo path — latency, errors, anything ───────────────

def test_emit_never_waits_on_demo_mirror(sandbox, monkeypatch):
    """A hanging demo venue must not delay the probe loop: emit returns
    immediately even when the demo call sleeps."""
    import time as _t

    from crypto_trading.crypto_common.execution import ExecutionRouter
    monkeypatch.setattr(common, "load_cfg",
                        lambda: {"subaccount": 64, "demo_mirror": True})
    monkeypatch.setattr(ExecutionRouter, "submit_demo",
                        lambda self, order: _t.sleep(8))
    o = Order.from_signed("KXBTCPERP", 10, 6.40, post_only=True, subaccount=64)
    t0 = _t.monotonic()
    rec = common.emit("w_test", o, enabled=False, reason="unit")
    assert _t.monotonic() - t0 < 1.0            # probe loop not held hostage
    assert rec["demo_mirror"] == "dispatched_async"


def test_mirror_async_swallows_exceptions(sandbox):
    """An exploding demo call must never propagate into the caller."""
    import time as _t

    def boom():
        raise RuntimeError("demo venue on fire")
    assert common.mirror_async("w_test", boom) == "dispatched_async"
    _t.sleep(0.3)                                # let the thread log its line
    logs = list(sandbox.glob("log_*.jsonl"))
    assert logs and "demo venue on fire" in logs[0].read_text()


def test_demo_order_body_gtc_self_destructs():
    """A mirrored GTC entry must carry expiration_ts (paper cancels its
    pendings implicitly; a demo twin without expiry rests forever as a
    zombie). IOC exits must NOT carry one."""
    from crypto_trading.crypto_common.execution import demo_order_body
    o = Order.from_signed("KXBTCPERP", 10, 6.40, post_only=True, subaccount=64)
    b, d = demo_order_body(o, now_ts=1_000_000.0)
    assert b["expiration_ts"] == 1_000_900          # +15min
    assert d.ticker == "KXBTCPERP1" and b["subaccount"] == 0
    x = Order.from_signed("KXBTCPERP", -10, 6.40, tif="immediate_or_cancel",
                          reduce_only=True, subaccount=64)
    bx, _ = demo_order_body(x, now_ts=1_000_000.0)
    assert "expiration_ts" not in bx


# ── W7 noise-fade pure-logic tests ───────────────────────────────────────────

def test_noisefade_knocked_side_and_z():
    from crypto_trading.crypto_strategies.event_binary.research_noisefade import (
        knocked_side, noise_z)
    # spot below strike → yes needs the upward recovery → buy yes
    assert knocked_side(88990.0, 89000.0) == "yes"
    assert knocked_side(89010.0, 89000.0) == "no"
    # z: 10bps deficit, 120s left, σ5=2bps → σ_rem=2·sqrt(24)≈9.8bps → z≈1.02
    z = noise_z(89000.0, 89000.0 * (1 + 0.0010), 120.0, 0.0002)
    assert 0.9 < z < 1.15
    # zero deficit → z=0;零波动 → inf(拒绝入场)
    assert noise_z(89000.0, 89000.0, 120.0, 0.0002) == 0.0
    assert noise_z(89000.0, 89100.0, 120.0, 0.0) == float("inf")


def test_noisefade_per_market_tte_gate():
    """THE fork bug: TTE must be evaluated per close-hour, never from the
    snapshot's first market. Simulated snapshot with two hours mixed."""
    from crypto_trading.crypto_strategies.event_binary import research_noisefade as nf
    # 纯函数层面锁定语义:同一 spot,两个小时的近平值分开选
    ms_now = [{"ticker": "A-T1", "floor_strike": 89000.0, "close_time": "2026-08-26T05:00:00Z"}]
    ms_next = [{"ticker": "B-T1", "floor_strike": 89000.0, "close_time": "2026-08-26T06:00:00Z"}]
    # 语义检查依赖 stream 内部按 close 分组 —— 结构测试:源码不得再出现 ms[0] 取 close 的模式
    import inspect
    src = inspect.getsource(nf.stream_entries)
    assert "by_close" in src and "ms[0]" not in src


# ── W7 probe structural tests ────────────────────────────────────────────────

def test_w7_shares_canonical_constants():
    """W7 v3 (2026-08-31): symmetric-favorite FLB — probe and canonical module
    must share every frozen constant so the two can never drift."""
    from crypto_trading.crypto_strategies.event_binary import research_favorite_no as fn
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    assert w7.REM_MIN_TARGET is fn.REM_MIN_TARGET
    assert (w7.COST_LO, w7.COST_HI) == (fn.COST_LO, fn.COST_HI) == (0.60, 0.98)
    assert (w7.PRIMARY_LO, w7.PRIMARY_HI) == (0.85, 0.98)
    assert w7.OBS_LO == 0.50 and w7.MAKER_IMPROVE == 0.01
    assert w7.MARKETS is fn.MARKETS
    assert w7.favorite_side is fn.favorite_side and w7.no_cost is fn.no_cost
    assert w7.yes_cost is fn.yes_cost
    # side semantics: favorite = the expensive side; dead even = no trade
    assert fn.favorite_side(0.30) == "no" and fn.favorite_side(0.70) == "yes"
    assert fn.favorite_side(0.50) is None


def test_favorite_no_semantics():
    """Buying NO costs 1 − yes_bid; NO is the favorite when the YES mid < 0.50.
    Getting either backwards puts us on the PAYING side of the mispricing —
    which is precisely how every earlier replication lost money."""
    from crypto_trading.crypto_strategies.event_binary import research_favorite_no as fn
    assert fn.no_cost(0.25) == pytest.approx(0.75)
    assert fn.favorite_is_no(0.25) is True          # yes cheap → no favorite
    assert fn.favorite_is_no(0.75) is False         # yes favorite → skip
    assert fn.favorite_is_no(0.50) is False         # exactly even → skip
    # breakeven: paying 0.75 with fee needs ~76.3% to break even
    be = 0.75 + fn.fee(0.75)
    assert 0.76 < be < 0.77


def test_w7_module_no_direct_venue_code():
    import inspect
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    src = inspect.getsource(w7)
    assert "KalshiEventOrderClient" not in src
    assert "create_order" not in src
    assert "EventExecutionRouter" in src        # goes through the layer
    assert "mirror_async" in src                # and never blocks the probe


def test_emit_per_strategy_mirror_flag(sandbox, monkeypatch):
    """w4-style: global mirror OFF but the strategy's own flag ON must mirror;
    a strategy without the flag must not."""
    from crypto_trading.crypto_common.execution import ExecutionRouter
    called = []
    monkeypatch.setattr(ExecutionRouter, "submit_demo",
                        lambda self, order: called.append(order) or {})
    monkeypatch.setattr(common, "load_cfg",
                        lambda: {"subaccount": 64, "demo_mirror": False,
                                 "w4_carry": {"demo_mirror": True},
                                 "w1_basis": {}})
    o = Order.from_signed("KXBTCPERP", -100, 6.40, post_only=True, subaccount=64)
    common.emit("w4_carry", o, enabled=False, reason="unit")
    import time as _t; _t.sleep(0.2)
    assert len(called) == 1
    common.emit("w1_basis", o, enabled=False, reason="unit")
    _t.sleep(0.2)
    assert len(called) == 1                     # w1 not mirrored


def test_w7_survives_state_from_a_previous_freeze(sandbox, monkeypatch):
    """Re-freezing a slot must not break on the old rule's state.

    Regression: W7 was re-registered from the hourly noise-deficit rule to the
    15M favorite-NO rule; the carried-over probe dict had keys
    {signals, entered, unresolved} and the new code's `probe["looked"] += 1`
    raised KeyError on the first window that reached the entry point.
    """
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    common.save_state("w7_noisefade", {
        "probe": {"signals": 30, "entered": 1, "unresolved": 0},   # OLD keys
        "positions": {}, "trades": [], "cum_net_usd": 0.0})
    monkeypatch.setattr(w7, "latest_snapshot", lambda s: None)     # no tape
    rep = w7.run({"w7_noisefade": {"enabled": False, "contracts": 25,
                                   "max_cum_loss_usd": 30,
                                   "min_trades_for_kill": 40}})
    assert rep["status"] == "OK"
    for k in ("looked", "entered", "unresolved"):
        assert k in rep["probe"]


def test_walk_ladder_sides_are_read_only_and_price_correctly(monkeypatch):
    """Single book: buying a side consumes the OTHER side's resting bids at
    1-p, best (highest) first. Reading your OWN side's ladder prices you
    against your competition, not your counterparty (the v2 bug, 2026-08-27).
    Locked for BOTH sides, on the functions the probe actually runs."""
    import inspect

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    assert w7.LADDER_FOR_SIDE == {"no": "yes_dollars", "yes": "no_dollars"}
    src = inspect.getsource(w7.fetch_orderbook)
    assert "requests.get" in src and ".post(" not in src        # read-only

    # yes bids: 0.30 x10 (NO @0.70), 0.28 x20 (NO @0.72), 0.20 x999
    # no bids: 0.24 x10 (YES @0.76), 0.22 x20 (YES @0.78)
    ob = {"yes_dollars": [["0.20", "999"], ["0.28", "20"], ["0.30", "10"]],
          "no_dollars": [["0.22", "20"], ["0.24", "10"]]}
    d = w7.walk_ladder(ob, "no", 25)
    assert d["top_cost"] == 0.70                       # 1 - best yes bid 0.30
    # 10 @0.70 + 15 @0.72 = 17.8 / 25 = 0.712
    assert d["fill_cost"] == 0.712
    assert d["filled"] == 25 and d["shortfall"] == 0
    assert d["slippage_c"] == 1.2                      # 1.2c worse than touch
    y = w7.walk_ladder(ob, "yes", 25)
    assert y["top_cost"] == 0.76                       # 1 - best NO bid 0.24
    # 10 @0.76 + 15 @0.78 = 19.3 / 25 = 0.772
    assert y["fill_cost"] == 0.772
    # a thin book reports the shortfall instead of pretending to fill
    thin = w7.walk_ladder({"yes_dollars": [["0.30", "4"]]}, "no", 25)
    assert thin["filled"] == 4 and thin["shortfall"] == 21


def test_w7_maker_fill_proxy_semantics():
    """Maker book fill = the opposite side CROSSES the posted level (same
    proxy as the 2026-08-31 backtest so numbers stay comparable). A quote
    merely moving next to the level is NOT a fill."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    # NO buyer posted a YES ask at 0.31 (touch yes_bid was 0.30)
    assert w7.maker_filled("no", 0.31, [(1, 0.29, 0.32), (2, 0.31, 0.33)]) is True
    assert w7.maker_filled("no", 0.31, [(1, 0.30, 0.32), (2, 0.29, 0.31)]) is False
    # YES buyer posted a YES bid at 0.74 (touch yes_ask was 0.75)
    assert w7.maker_filled("yes", 0.74, [(1, 0.70, 0.76), (2, 0.71, 0.74)]) is True
    assert w7.maker_filled("yes", 0.74, [(1, 0.70, 0.76), (2, 0.72, 0.75)]) is False


def test_w7_evidence_kill_semantics():
    """Paper probes stop only when data REFUTES the edge (window t <= -2,
    n >= 30) — never on paper dollars (that fired twice on pure noise)."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    def W(vals):  # windows dict from per-window per-trade means
        return {f"c{i}": {"n": 1, "wins": 0, "sum_c": v} for i, v in enumerate(vals)}

    # t = -0.05 world (measured 2026-08-29): must NOT kill
    st = {"windows_primary": W([+30, -30] * 52)}
    assert w7.evidence_kill(st) is False
    # clearly refuting world: 40 windows all around -10c → t << -2 → kill
    st2 = {"windows_primary": W([-10 + (i % 3 - 1) for i in range(40)])}
    assert w7.evidence_kill(st2) is True and "refutes" in st2["killed_reason"]
    # too few windows: never kill regardless of mean
    st3 = {"windows_primary": W([-50] * 10)}
    assert w7.evidence_kill(st3) is False
    # v3: the kill judges the PRIMARY cell — a terrible WIDE band alone
    # must not kill while the primary cell holds up
    st5 = {"windows": W([-50] * 60), "windows_primary": W([+3, -1] * 20)}
    assert w7.evidence_kill(st5) is False
    # sticky
    st4 = {"killed": True, "windows_primary": W([+50] * 40)}
    assert w7.evidence_kill(st4) is True


def test_w7_v3_settlement_books_split(sandbox, monkeypatch):
    """Settlement must route each leg to its book: band legs -> trades /
    windows / cum_net_usd (and windows_primary ONLY for [0.85,0.98]); the
    observation leg -> obs_trades and NOTHING else. Maker parallel book
    settles from the tape with the crossing proxy."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now - pd.Timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rts = (now - pd.Timedelta(minutes=18)).timestamp()
    common.save_state("w7_noisefade", {"positions": {
        # primary-cell NO win (cost 0.87): result "no"
        "T-PRIM": {"cost": 0.87, "close": close, "series": "KXBTC15M",
                   "side": "no", "leg": "band", "maker_posted": 0.14,
                   "entry_rts": rts,
                   "opened": str(pd.Timestamp(rts + 30, unit="s", tz="UTC"))},
        # band YES loss (cost 0.65): result "no"
        "T-BAND": {"cost": 0.65, "close": close, "series": "KXETH15M",
                   "side": "yes", "leg": "band", "maker_posted": 0.64,
                   "entry_rts": rts,
                   "opened": str(pd.Timestamp(rts + 30, unit="s", tz="UTC"))},
        # observation leg YES win (cost 0.55): result "yes"
        "T-OBS": {"cost": 0.55, "close": close, "series": "KXSOL15M",
                  "side": "yes", "leg": "obs", "maker_posted": 0.54,
                  "entry_rts": rts,
                  "opened": str(pd.Timestamp(rts + 30, unit="s", tz="UTC"))},
    }, "trades": [], "cum_net_usd": 0.0})
    res = {"T-PRIM": "no", "T-BAND": "no", "T-OBS": "yes"}
    monkeypatch.setattr(w7, "official_result", lambda t: res[t])
    monkeypatch.setattr(w7, "latest_snapshot", lambda s: None)
    # tape: T-PRIM's posted YES ask 0.14 gets crossed (yes_bid reaches it);
    # T-BAND's posted YES bid 0.64 never crossed (ask stays above)
    tapes = {"T-PRIM": [(rts + 60, 0.14, 0.16)],
             "T-BAND": [(rts + 60, 0.60, 0.66)],
             "T-OBS": [(rts + 60, 0.50, 0.56)]}
    # the mock honours t0/t1 exactly like the real tape reader — the fill
    # window must start at the order's wall-clock time, so quotes before
    # ``opened`` never reach maker_filled (2026-09-01 live catch)
    monkeypatch.setattr(w7, "tape_quotes",
                        lambda series, tkr, t0, t1: [q for q in tapes[tkr]
                                                     if t0 <= q[0] <= t1])
    rep = w7.run({"w7_noisefade": {"enabled": False, "contracts": 25}})
    assert rep["status"] == "OK" and len(rep["settled"]) == 3
    st = common.load_state("w7_noisefade")
    assert len(st["trades"]) == 2 and len(st["obs_trades"]) == 1
    assert len(st["windows"]) == 1                    # same close_time
    assert len(st["windows_primary"]) == 1            # only the 0.87 trade
    assert st["windows_primary"][close]["n"] == 1
    # cum book = band legs only: win 0.87 (fee 0.0079) + loss 0.65 (fee 0.0159)
    prim_c = (1 - 0.87 - 0.07 * 0.87 * 0.13) * 100
    band_c = (0 - 0.65 - 0.07 * 0.65 * 0.35) * 100
    assert st["cum_net_usd"] == pytest.approx(
        (prim_c + band_c) / 100 * 25, abs=0.01)
    by_t = {x["ticker"]: x for x in st["trades"]}
    assert by_t["T-PRIM"]["maker_fill"] is True
    assert by_t["T-PRIM"]["maker_pnl_c"] == pytest.approx(
        (1 - (1 - 0.14)) * 100, abs=0.01)             # win at cost 0.86, no fee
    assert by_t["T-BAND"]["maker_fill"] is False
    assert by_t["T-BAND"]["maker_pnl_c"] is None
    assert st["obs_trades"][0]["win"] is True         # obs booked nowhere else


def test_w7_maker_fill_window_starts_at_order_time(sandbox, monkeypatch):
    """An order cannot fill before it exists: a cross that happened between
    the snapshot's recv_ts and the actual posting moment (``opened``) must
    NOT count. Live catch 2026-09-01: ETH-2230 tape never crossed 0.73 after
    entry, but the pre-entry ask did — and was wrongly counted as a fill."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now - pd.Timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rts = (now - pd.Timedelta(minutes=18)).timestamp()
    opened = rts + 60                                  # posted 60s after snap
    common.save_state("w7_noisefade", {"positions": {
        "T-PRE": {"cost": 0.74, "close": close, "series": "KXETH15M",
                  "side": "yes", "leg": "band", "maker_posted": 0.73,
                  "entry_rts": rts,
                  "opened": str(pd.Timestamp(opened, unit="s", tz="UTC"))},
    }, "trades": [], "cum_net_usd": 0.0})
    monkeypatch.setattr(w7, "official_result", lambda t: "yes")
    monkeypatch.setattr(w7, "latest_snapshot", lambda s: None)
    # cross at rts+30 (BEFORE the order existed), never after
    tape = [(rts + 30, 0.70, 0.72), (opened + 60, 0.76, 0.77),
            (opened + 120, 0.89, 0.91)]
    monkeypatch.setattr(w7, "tape_quotes",
                        lambda series, tkr, t0, t1: [q for q in tape
                                                     if t0 <= q[0] <= t1])
    w7.run({"w7_noisefade": {"enabled": False, "contracts": 25}})
    st = common.load_state("w7_noisefade")
    assert st["trades"][0]["maker_fill"] is False
    assert st["trades"][0]["maker_pnl_c"] is None


def test_demo_market_list_empty_is_an_answer_not_an_outage(monkeypatch):
    """A 200 with no quoted markets means "demo quotes nothing here" →
    [] → no_demo_market. Only a transport failure may return None
    (2026-09-01: 24 mirrors mislabelled skipped_market_list_unavailable)."""
    import crypto_trading.crypto_common.execution_events as ee
    ee._MKT_CACHE.clear()
    calls = []

    class R:
        def __init__(self, code, mkts): self.status_code, self._m = code, mkts
        def json(self): return {"markets": self._m}
    monkeypatch.setattr("requests.get",
                        lambda url, params=None, **k: calls.append(params) or
                        R(200, [{"ticker": "Q", "close_time": "2099-01-01T00:00:00Z",
                                 "yes_ask": 0, "no_ask": 100}]))     # unquoted
    assert ee._demo_markets_cached(("KXBTC15M",)) == []
    # 15M: exactly one plain probe, never the +12h one
    assert len(calls) == 1 and "min_close_ts" not in (calls[0] or {})
    ee._MKT_CACHE.clear()
    monkeypatch.setattr("requests.get", lambda *a, **k: R(429, []))
    assert ee._demo_markets_cached(("KXBTC15M",)) is None       # real outage


def test_w7_pooled_stats_is_the_money_and_clusters_on_windows():
    """The verdict runs on P&L PER CONTRACT with a window-cluster-robust SE.
    Equal-weighting windows is a different estimand and reads several times
    better here, because window size is informative: a five-coin window is one
    big macro move and those lose (live 2026-09-02: equal-weight +2.24c vs
    pooled +0.48c). Getting this backwards could promote a losing rule."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    # two tiny winning windows, one big losing window: pooled must be negative
    # (the money), equal-weight positive (the average window)
    W = {"a": {"n": 1, "wins": 1, "sum_c": 10.0},
         "b": {"n": 1, "wins": 1, "sum_c": 10.0},
         "c": {"n": 10, "wins": 0, "sum_c": -100.0}}
    n_tr, n_w, mu, t = w7.pooled_stats(W)
    assert (n_tr, n_w) == (12, 3)
    assert mu == pytest.approx((10 + 10 - 100) / 12)      # = -6.67c per contract
    _, mu_eq, _ = w7.window_stats(W)
    assert mu_eq == pytest.approx((10 + 10 - 10) / 3)     # = +3.33c per window
    assert mu < 0 < mu_eq                                 # opposite signs

    # Zero between-window variance yields t = 0, NOT infinity: a variance-free
    # book is pathological (a constant, or a bug) and a gate that promotes
    # toward real money must refuse to call that significance. Same convention
    # as window_stats. Degenerate inputs are 0.0, never NaN.
    same = {str(i): {"n": 2, "wins": 2, "sum_c": 8.0} for i in range(40)}
    assert w7.pooled_stats(same)[2] == pytest.approx(4.0)   # mean still right
    assert w7.pooled_stats(same)[3] == 0.0
    assert w7.pooled_stats({})[3] == 0.0
    assert w7.pooled_stats({"x": {"n": 3, "wins": 1, "sum_c": 1.0}})[3] == 0.0


def test_w7_kill_boundary_is_valid_under_continuous_monitoring():
    """The kill is re-tested every cycle (~1,440x/day), so a fixed |t|>=2 bar
    is not a 2.3% rule — it measured 6.2% under the null. The mixture boundary
    must be strictly wider than 2, tightest near the pre-registered n, and
    monotonically looser for small samples."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    b30, b300, b1000 = (w7.always_valid_bound(n) for n in (30, 300, 1000))
    assert b30 > b300 and b300 > 3.0 and b1000 > 2.5     # always stricter than 2
    assert b30 > b300 > 2.0
    assert w7.always_valid_bound(1) == float("inf")      # cannot kill on n<2

    # With realistic window-to-window dispersion, a book that clears the OLD
    # fixed -2 bar must NOT kill any more — that bar is what measured 6.2%
    # false stops under continuous monitoring.
    spread = (-1.5, -0.5, 0.5, 1.5)                      # mean 0, sd ~1.12
    mild = {str(i): {"n": 1, "wins": 0, "sum_c": -9.5 + 20 * spread[i % 4]}
            for i in range(40)}
    _, _, _, t = w7.pooled_stats(mild)
    assert -w7.always_valid_bound(40) < t < -2.0         # past the old bar only
    assert w7.evidence_kill({"windows_primary": mild}) is False
    # ...while a book that clears the mixture boundary still kills
    hard = {str(i): {"n": 1, "wins": 0, "sum_c": -20.0 + 15 * spread[i % 4]}
            for i in range(40)}
    _, _, _, t2 = w7.pooled_stats(hard)
    assert t2 <= -w7.always_valid_bound(40)
    st2 = {"windows_primary": hard}
    assert w7.evidence_kill(st2) is True and "refutes" in st2["killed_reason"]


def test_w7_verdict_latches_once_at_the_registered_size(sandbox, monkeypatch):
    """A fixed-n gate read every 60s is optional stopping. The verdict must not
    exist before 300 primary windows, must be computed once, and must never
    re-open afterwards even if later data would flip it."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    win = {str(i): {"n": 1, "wins": 1, "sum_c": 6.0 + (i % 7)} for i in range(299)}
    st = {"windows_primary": dict(win)}
    assert w7.latch_verdict(st) is None and "verdict" not in st   # 299 < 300
    st["windows_primary"]["299"] = {"n": 1, "wins": 1, "sum_c": 9.0}
    v = w7.latch_verdict(st)
    assert v["passed"] is True and v["decided_at_windows"] == 300
    assert v["pooled_mean_c"] > 0 and v["pooled_t_clustered"] >= 2.5
    # later data cannot re-open the decision
    for i in range(300, 400):
        st["windows_primary"][str(i)] = {"n": 5, "wins": 0, "sum_c": -400.0}
    again = w7.latch_verdict(st)
    assert again == v and again["decided_at_windows"] == 300


def test_save_state_is_atomic(sandbox):
    """The paper book is the money truth but had weaker durability than the
    tape: a torn 380KB write leaves invalid JSON and every later cycle dies on
    load. Readers must see the old book or the new one, never half of one."""
    import json as _json

    common.save_state("w7_noisefade", {"trades": [1, 2, 3]})
    p = common.state_path("w7_noisefade")
    before = p.read_text()

    class Boom(Exception):
        pass

    real = common.json.dumps
    common.json.dumps = lambda *a, **k: (_ for _ in ()).throw(Boom("disk full"))
    try:
        with pytest.raises(Boom):
            common.save_state("w7_noisefade", {"trades": [4, 5, 6]})
    finally:
        common.json.dumps = real
    # the live book is untouched, and no half-written twin is left beside it
    assert p.read_text() == before
    assert _json.loads(p.read_text())["trades"] == [1, 2, 3]
    assert not p.with_suffix(".json.tmp").exists()
    # and a normal save still replaces it wholesale
    common.save_state("w7_noisefade", {"trades": [7]})
    assert _json.loads(p.read_text())["trades"] == [7]


def test_w7_entry_is_priced_and_timed_off_live_data_not_the_tape(sandbox, monkeypatch):
    """v3.1: the tape only says a window EXISTS; the probe's own clock decides
    WHEN (the 90s tape cadence divides the 900s window exactly, so its phase
    was locked and silently chose how good the strategy looked — +2.28c at rem
    7.5-8.0 vs +0.64c at 8.5-9.0), and one live orderbook call decides side,
    price and leg. A stale tape must therefore still produce entries."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now + pd.Timedelta(minutes=8.0)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def snap(age_s, price_hint="0.99"):
        # the snapshot's own quotes are deliberately absurd: if any of them
        # reached the decision, the asserted cost below would not match
        return {"recv_ts": now.timestamp() - age_s,
                "markets": [{"ticker": "KXBTC15M-LIVE", "close_time": close,
                             "yes_bid_dollars": price_hint,
                             "yes_ask_dollars": price_hint}]}

    # a self-consistent thin book: yes 0.10/0.12, so NO is the favorite at
    # 1-0.10 = 0.90 (inside the primary cell) and YES is the longshot at 0.12
    book = {"yes_bid": 0.10, "yes_ask": 0.12,
            "no": {"top_cost": 0.90, "fill_cost": 0.902, "filled": 25,
                   "shortfall": 0, "slippage_c": 0.2, "levels": 3, "top_size": 40},
            "yes": {"top_cost": 0.12, "fill_cost": 0.122, "filled": 25,
                    "shortfall": 0, "slippage_c": 0.2, "levels": 3, "top_size": 40}}
    monkeypatch.setattr(w7, "walk_book_both", lambda t, c: book)
    cfg = {"w7_noisefade": {"enabled": False, "contracts": 25}}

    def run_with(snapshot):
        monkeypatch.setattr(w7, "latest_snapshot",
                            lambda s: snapshot if s == "KXBTC15M" else None)
        common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                           "cum_net_usd": 0.0})
        return w7.run(cfg)

    # a 5-minute-old tape is fine for DISCOVERY, and the entry is priced from
    # the book (0.882), never from the snapshot's 0.99
    rep = run_with(snap(300))
    assert len(rep["entries"]) == 1
    e = rep["entries"][0]
    assert e["side"] == "no" and e["cost"] == 0.902 and e["leg"] == "band"
    assert 7.4 <= e["rem_min"] <= 8.6            # centred on the registered 8.0
    # only a genuinely dead recorder stops entries
    assert run_with(snap(600))["markets"]["KXBTC15M"].startswith("STALE")


def test_w7_side_and_leg_come_from_the_book(sandbox, monkeypatch):
    """Both sides are read from ONE orderbook payload, so the favorite, the
    price and the leg are decided together on live prices — there is no stale
    guess left to re-validate, which is what the old drift gate did by
    discarding 19% of windows on a post-signal price move."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now + pd.Timedelta(minutes=8.0)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {"recv_ts": now.timestamp(),
            "markets": [{"ticker": "KXETH15M-BOOK", "close_time": close,
                         "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.51"}]}
    monkeypatch.setattr(w7, "latest_snapshot",
                        lambda s: snap if s == "KXETH15M" else None)
    cfg = {"w7_noisefade": {"enabled": False, "contracts": 25}}

    def run_book(yes_bid, yes_ask, no_fill, yes_fill):
        book = {"yes_bid": yes_bid, "yes_ask": yes_ask,
                "no": {"top_cost": 1 - yes_bid, "fill_cost": no_fill,
                       "filled": 25, "shortfall": 0, "slippage_c": 0.0},
                "yes": {"top_cost": yes_ask, "fill_cost": yes_fill,
                        "filled": 25, "shortfall": 0, "slippage_c": 0.0}}
        monkeypatch.setattr(w7, "walk_book_both", lambda t, c: book)
        common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                           "cum_net_usd": 0.0})
        return w7.run(cfg)

    # YES is the favorite (mid 0.72) -> buy YES at its fill price, band leg
    rep = run_book(0.71, 0.73, 0.30, 0.735)
    assert rep["entries"][0]["side"] == "yes"
    assert rep["entries"][0]["cost"] == 0.735 and rep["entries"][0]["leg"] == "band"
    # NO is the favorite but only just: cost 0.55 lands in the OBSERVATION leg
    rep = run_book(0.44, 0.46, 0.55, 0.46)
    assert rep["entries"][0]["side"] == "no" and rep["entries"][0]["leg"] == "obs"
    assert rep["probe"]["entered"] == 0 and rep["probe"]["obs_entered"] == 1
    # a dead-even book has no favorite, and a price outside [0.50,0.98] is not
    # the rule's universe at all
    assert run_book(0.49, 0.51, 0.50, 0.51)["entries"] == []
    rep = run_book(0.71, 0.73, 0.30, 0.995)
    assert rep["entries"] == [] and rep["probe"]["looked"] == 1
    # an unavailable book is counted, never silently skipped
    monkeypatch.setattr(w7, "walk_book_both", lambda t, c: None)
    common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                       "cum_net_usd": 0.0})
    assert w7.run(cfg)["probe"]["book_unavailable"] == 1


def test_walk_book_both_reads_one_payload_for_two_sides(monkeypatch):
    """One fetch, both ladders: buying NO consumes resting YES bids at 1-p,
    buying YES consumes resting NO bids at 1-p, and the implied touch on each
    side must come back consistent."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    monkeypatch.setattr(w7, "fetch_orderbook", lambda t: {
        "yes_dollars": [["0.20", "999"], ["0.28", "20"], ["0.30", "10"]],
        "no_dollars": [["0.22", "20"], ["0.24", "10"]]})
    d = w7.walk_book_both("KXBTC15M-X", 25)
    assert d["yes_bid"] == 0.30                    # best resting yes bid
    assert d["yes_ask"] == 0.76                    # 1 - best no bid 0.24
    assert d["no"]["fill_cost"] == 0.712           # 10@0.70 + 15@0.72
    assert d["yes"]["fill_cost"] == 0.772          # 10@0.76 + 15@0.78
    # an empty side makes the whole quote unusable rather than half-known
    monkeypatch.setattr(w7, "fetch_orderbook",
                        lambda t: {"yes_dollars": [["0.30", "10"]], "no_dollars": []})
    assert w7.walk_book_both("KXBTC15M-X", 25) is None
    monkeypatch.setattr(w7, "fetch_orderbook", lambda t: None)
    assert w7.walk_book_both("KXBTC15M-X", 25) is None


def test_w7_main_cell_books_only_post_registration_entries(sandbox, monkeypatch):
    """v3.2: MAIN [0.78,0.98] keeps its own book, and that book starts at the
    registration stamp — the 9/2-9/6 trades that suggested 0.78 are its
    discovery period and must not become its evidence. PRIMARY is untouched."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now - pd.Timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    reg = str(now - pd.Timedelta(hours=1))
    before = str(now - pd.Timedelta(hours=2))          # opened before registration
    after = str(now - pd.Timedelta(minutes=18))        # opened after
    common.save_state("w7_noisefade", {
        "main_registered_at": reg, "positions": {
            "T-OLD": {"cost": 0.80, "close": close, "series": "KXBTC15M", "side": "yes",
                      "leg": "band", "maker_posted": 0.79, "entry_rts": 1, "opened": before},
            "T-NEW": {"cost": 0.80, "close": close, "series": "KXETH15M", "side": "yes",
                      "leg": "band", "maker_posted": 0.79, "entry_rts": 1, "opened": after},
            "T-PRIM": {"cost": 0.90, "close": close, "series": "KXSOL15M", "side": "yes",
                       "leg": "band", "maker_posted": 0.89, "entry_rts": 1, "opened": after},
        }, "trades": [], "cum_net_usd": 0.0})
    monkeypatch.setattr(w7, "official_result", lambda t: "yes")
    monkeypatch.setattr(w7, "latest_snapshot", lambda s: None)
    monkeypatch.setattr(w7, "tape_quotes", lambda *a: [])
    rep = w7.run({"w7_noisefade": {"enabled": False, "contracts": 25}})
    st = common.load_state("w7_noisefade")
    assert len(st["trades"]) == 3
    assert sum(v["n"] for v in st["windows_main"].values()) == 2      # NEW + PRIM
    assert sum(v["n"] for v in st["windows_primary"].values()) == 1  # PRIM only
    assert rep["main_windows"] == 1 and rep["primary_windows"] == 1


def test_w7_main_cell_latches_and_kills_independently():
    """Each registered cell decides alone: MAIN can latch while PRIMARY has
    not, and a refuting MAIN book kills even with a healthy PRIMARY book."""
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    def W(vals):
        return {str(i): {"n": 1, "wins": int(v > 0), "sum_c": v} for i, v in enumerate(vals)}
    st = {"windows_main": W([6 + (i % 7) for i in range(300)]),
          "windows_primary": W([6 + (i % 7) for i in range(120)])}
    assert w7.latch_verdict(st) is None                       # PRIMARY: 120 < 300
    vm = w7.latch_cell(st, w7.CELLS["main"])
    assert vm["passed"] is True and vm["cell"].startswith("MAIN")
    assert st["verdict_main"] == vm and "verdict" not in st
    # kill on MAIN alone
    spread = (-1.5, -0.5, 0.5, 1.5)
    st2 = {"windows_primary": W([+5 + spread[i % 4] for i in range(60)]),
           "windows_main": W([-20 + 15 * spread[i % 4] for i in range(60)])}
    assert w7.evidence_kill(st2) is True and "MAIN" in st2["killed_reason"]

def test_runner_survives_a_missing_experimental_strategy():
    """W1-W7 are the product; an experiment must never be able to kill them.
    W8 lives in untracked files but was wired into this tracked launcher as a
    module-scope import, so deleting the experiment (git clean, fresh clone,
    retiring it) would have stopped every probe. Verified in a SUBPROCESS so
    the check cannot disturb this session's module table."""
    import subprocess
    import sys
    import textwrap

    probe = textwrap.dedent("""
        import sys

        class BlockW8:
            def find_spec(self, name, path=None, target=None):
                if name.endswith("w8_complete_set"):
                    raise ImportError("w8 deliberately absent")
                return None

        sys.meta_path.insert(0, BlockW8())
        from crypto_trading.crypto_strategies.live_watch import runner
        assert set(runner.STRATS) >= {"w1", "w2", "w3", "w4", "w5", "w6", "w7"}
        assert "w8" not in runner.STRATS and "w8" not in runner.CADENCE_S
        print("OK", len(runner.STRATS))
    """)
    import os
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo)}
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                       text=True, timeout=120, cwd=str(repo), env=env)
    assert r.returncode == 0, f"runner died without w8:\n{r.stderr[-2000:]}"
    assert r.stdout.startswith("OK 8")          # W1-W7 + the W7 table scanner (2026-10-02)


def test_w7_mirrors_and_live_intents_only_the_main_cell(sandbox, monkeypatch):
    """v3.3 (user 2026-09-12): the LIVE rule is MAIN [0.78,0.98]. Demo must
    rehearse exactly those trades and nothing else, and every MAIN entry must
    also dispatch a (disarmed) live intent — so arming later changes nothing
    but the gate."""
    import time

    import pandas as pd

    from crypto_trading.crypto_common.execution_events import EventExecutionRouter
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    mirrored, lived = [], []
    monkeypatch.setattr(EventExecutionRouter, "mirror_demo",
                        lambda self, **kw: mirrored.append(kw) or {"status": "sent"})
    monkeypatch.setattr(EventExecutionRouter, "submit",
                        lambda self, **kw: lived.append(kw) or {"status": "live_disarmed"})
    now = pd.Timestamp.now(tz="UTC")
    close = (now + pd.Timedelta(minutes=8.0)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {"recv_ts": now.timestamp(),
            "markets": [{"ticker": "KXETH15M-MAINTEST", "close_time": close,
                         "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.51"}]}
    monkeypatch.setattr(w7, "latest_snapshot",
                        lambda s: snap if s == "KXETH15M" else None)
    cfg = {"w7_noisefade": {"enabled": False, "contracts": 25,
                            "demo_mirror": True, "live_orders": False}}

    def run_fill(fc):
        book = {"yes_bid": fc - 0.01, "yes_ask": fc,
                "no": {"top_cost": 1 - (fc - 0.01), "fill_cost": 1 - (fc - 0.01),
                       "filled": 25, "shortfall": 0, "slippage_c": 0.0},
                "yes": {"top_cost": fc, "fill_cost": fc, "filled": 25,
                        "shortfall": 0, "slippage_c": 0.0}}
        monkeypatch.setattr(w7, "walk_book_both", lambda t, c: book)
        common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                           "cum_net_usd": 0.0})
        w7.run(cfg)
        time.sleep(0.3)                      # fire-and-forget threads land

    run_fill(0.72)                           # band, but NOT main
    assert mirrored == [] and lived == []
    run_fill(0.85)                           # main cell
    assert len(mirrored) == 1 and len(lived) == 1
    assert lived[0]["ticker"] == "KXETH15M-MAINTEST"
    assert lived[0]["side"] == "yes" and lived[0]["armed"] is False
    assert lived[0]["entry_price"] == 0.85 and lived[0]["contracts"] == 25


def test_events_live_gate_never_downgrades_silently(monkeypatch):
    """Disarmed -> a full audit row and NO venue construction. Armed with a
    closed gate -> LiveOrderRefused, never a quiet dry-run. Armed with every
    gate open -> the V2 order goes out IOC with the yes/no translation."""
    import crypto_trading.crypto_common.execution_events as ee

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("venue client must not be built while disarmed")
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient",
        Boom)
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    r = ee.EventExecutionRouter(strategy="w7_noisefade").submit(
        ticker="KXBTC15M-X", side="no", entry_price=0.88, contracts=25)
    # 2026-09-30 buffer: the wire limit is paper+1c; the paper price is audited
    assert r["status"] == "live_disarmed" and r["price_dollars"] == 0.89
    assert r["paper_price_dollars"] == 0.88 and r["limit_buffer_c"] == 1

    router = ee.EventExecutionRouter(strategy="w7_noisefade")
    monkeypatch.setattr(ee.EventExecutionRouter, "gate_status",
                        lambda self: {"live_open": False, "env_allows": False})
    with pytest.raises(ee.LiveOrderRefused):
        router.submit(ticker="KXBTC15M-X", side="no", entry_price=0.88,
                      contracts=25, armed=True)

    sent = []

    class Client:
        def __init__(self, *, env=None):
            assert env == "prod"

        def create_order(self, **kw):
            sent.append(kw)
            return {"status_code": 201, "response": "{}", "body_sent": {}}
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient",
        Client)
    monkeypatch.setattr(ee.EventExecutionRouter, "gate_status",
                        lambda self: {"live_open": True})
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    r = router.submit(ticker="KXBTC15M-X", side="no", entry_price=0.88,
                      contracts=25, armed=True)
    assert r["status"] == "live_sent"
    # PROD_SIZING resizes w7 BTC to 40 in the day band (v3, 2026-10-02; the 25
    # passed in is the paper size; prod sizing is decoupled by directive).
    assert sent == [{"ticker": "KXBTC15M-X", "side": "no", "count": 40,
                     "price_dollars": 0.89, "tif": "immediate_or_cancel"}]


def test_events_prod_sizing_override_scopes(monkeypatch):
    """PROD sizing v2 (2026-09-30 directive): UTC-hour banded — day (06-23)
    w7 BTC 40 / alts 30 (v3), night (00-05) BTC 20 / alts 20 (v4, 2026-10-04). Other strategies
    pass through untouched, the override is visible in the DISARMED audit row
    too, and the demo mirror path must NOT consult PROD_SIZING (demo 25)."""
    import inspect
    import crypto_trading.crypto_common.execution_events as ee

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("no venue client while disarmed")
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient",
        Boom)
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")
    for hour, btc, alt in ((14, 40, 30), (3, 20, 20), (0, 20, 20), (5, 20, 20), (6, 40, 30)):
        monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                            staticmethod(lambda h=hour: h))
        assert w7.submit(ticker="KXBTC15M-26SEP300100-00", side="yes",
                         entry_price=0.90, contracts=25)["contracts"] == btc, hour
        for series in ("KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M"):
            r = w7.submit(ticker=f"{series}-26SEP300100-00", side="yes",
                          entry_price=0.90, contracts=25)
            assert r["contracts"] == alt, (hour, series)
    other = ee.EventExecutionRouter(strategy="w5_knockdown")
    assert other.submit(ticker="KXBTC15M-26SEP300100-00", side="yes",
                        entry_price=0.90, contracts=25)["contracts"] == 25
    # demo mirror keeps the caller's size: no PROD_SIZING reference in it
    src = inspect.getsource(ee.EventExecutionRouter._mirror_w7_demo)
    assert "PROD_SIZING" not in src


def test_events_day_band_flow_gate_skip_bypass_and_fail_open(monkeypatch):
    """2026-09-30 flow gate: DAY band skips a prod order only on an explicit
    'skip' verdict from the registered downside_paper policy; the NIGHT band
    never consults the gate; a policy exception or non-skip verdict sends
    normally (fail-open). Demo mirror must not reference FLOW_GATE."""
    import inspect
    import crypto_trading.crypto_common.execution_events as ee
    import crypto_trading.crypto_strategies.downside_paper.policy as pol

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("no venue client on a skipped/disarmed order")
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient",
        Boom)
    monkeypatch.setattr(ee.EventExecutionRouter, "_tail_rows",
                        staticmethod(lambda path, n: []))
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")

    # day band + skip verdict -> skipped audit row, no venue construction
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 14))
    monkeypatch.setattr(pol, "decide", lambda c, f, p: {
        "decision": "skip", "aligned_observed_flow_1m": -0.9,
        "aligned_momentum_1m_bp": -12.0})
    r = w7.submit(ticker="KXETH15M-26SEP300700-00", side="yes",
                  entry_price=0.85, contracts=25)
    assert r["status"] == "skipped_by_flow_gate" and r["contracts"] == 30
    assert r["aligned_observed_flow_1m"] == -0.9
    assert r["flow_gate"]["decision"] == "skip"

    # night band: gate bypassed even with a skip verdict -> disarmed audit row
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 3))
    r = w7.submit(ticker="KXETH15M-26SEP300100-00", side="yes",
                  entry_price=0.85, contracts=25)
    assert r["status"] == "live_disarmed" and r["contracts"] == 20
    assert r["flow_gate"]["decision"] == "night"

    # day band + accept verdict -> sends (disarmed audit row here)
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 14))
    monkeypatch.setattr(pol, "decide",
                        lambda c, f, p: {"decision": "accept"})
    r = w7.submit(ticker="KXETH15M-26SEP300700-00", side="yes",
                  entry_price=0.85, contracts=25)
    assert r["status"] == "live_disarmed"
    assert r["flow_gate"]["decision"] == "accept"

    # day band + policy blowing up -> fail-open, order proceeds
    def explode(c, f, p):
        raise RuntimeError("feature pipeline down")
    monkeypatch.setattr(pol, "decide", explode)
    r = w7.submit(ticker="KXETH15M-26SEP300700-00", side="yes",
                  entry_price=0.85, contracts=25)
    assert r["status"] == "live_disarmed"
    assert r["flow_gate"]["decision"] == "error"

    # other strategies and the demo mirror never consult the gate
    assert ee.EventExecutionRouter(strategy="w5_knockdown")._flow_gate_decision(
        "KXBTC15M-X", "yes")["decision"] == "off"
    src = inspect.getsource(ee.EventExecutionRouter._mirror_w7_demo)
    assert "FLOW_GATE" not in src


def test_events_gate_status_defaults_closed(monkeypatch):
    """The shipped configuration must evaluate to a fully closed gate: no
    ALLOW_LIVE_ORDERS, no armed flag -> live_open False, whatever the keys."""
    import crypto_trading.crypto_common.execution_events as ee
    monkeypatch.delenv("ALLOW_LIVE_ORDERS", raising=False)
    g = ee.EventExecutionRouter(strategy="w7_noisefade").gate_status()
    assert g["live_open"] is False
    assert g["env_allows"] is False and g["cfg_armed"] is False


def test_kalshi_key_prod_naming_serves_order_paths_only(monkeypatch):
    """The user's prod key lives under KALSHI_PROD_* (prediction_market
    naming). Order paths (borrowed_ok=False) must find it as a DEDICATED key;
    demo/read paths must NOT get it (a prod key cannot authenticate against
    the demo host — the mirror keeps the demo key)."""
    import crypto_trading.crypto_common.config as C

    monkeypatch.delenv("KALSHI_MARGIN_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_MARGIN_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(C, "_PM_ENV", {
        "KALSHI_PROD_API_KEY_ID": "prod-kid",
        "KALSHI_PROD_PRIVATE_KEY_PATH": "/tmp/prod.pem",
        "KALSHI_API_KEY_ID": "demo-kid",
        "KALSHI_PRIVATE_KEY_PATH": "/tmp/demo.pem"})
    k = C.kalshi_key("margin", borrowed_ok=False)
    assert k.key_id == "prod-kid" and k.borrowed is False
    k = C.kalshi_key("margin", borrowed_ok=True)
    assert k.key_id == "demo-kid" and k.borrowed is True
    # a dedicated crypto key still wins over everything
    monkeypatch.setenv("KALSHI_MARGIN_KEY_ID", "dedicated-kid")
    monkeypatch.setenv("KALSHI_MARGIN_PRIVATE_KEY_PATH", "/tmp/ded.pem")
    assert C.kalshi_key("margin", borrowed_ok=False).key_id == "dedicated-kid"


def test_w7_cum_equals_the_sum_of_stored_trades_exactly(sandbox, monkeypatch):
    """The paper book's headline must equal the trades it is made of, to the
    cent, forever. Accumulating full precision while storing 2dp drifted them
    apart by $0.055 over 3,831 trades and tripped the health check that exists
    to catch a real accounting bug."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now - pd.Timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # costs chosen so the raw pnl has long decimal tails in both directions
    pos = {}
    for i, cost in enumerate([0.6133, 0.7777, 0.8451, 0.9099, 0.7003]):
        pos[f"KXBTC15M-R{i}"] = {
            "cost": cost, "close": close, "series": "KXBTC15M", "side": "no",
            "leg": "band", "maker_posted": None, "entry_rts": 1,
            "opened": str(now - pd.Timedelta(minutes=18))}
    common.save_state("w7_noisefade", {"positions": pos, "trades": [],
                                       "cum_net_usd": 0.0})
    monkeypatch.setattr(w7, "official_result",
                        lambda t: "no" if t.endswith(("0", "2", "4")) else "yes")
    monkeypatch.setattr(w7, "latest_snapshot", lambda s: None)
    monkeypatch.setattr(w7, "tape_quotes", lambda *a: [])
    rep = w7.run({"w7_noisefade": {"enabled": False, "contracts": 25}})
    assert len(rep["settled"]) == 5
    st = common.load_state("w7_noisefade")
    booked = sum(t["pnl_c"] for t in st["trades"]) * 25 / 100
    assert st["cum_net_usd"] == pytest.approx(booked, abs=1e-9)


def test_prod_submit_rounds_paper_cost_to_whole_cents(monkeypatch):
    """Prod tick rule (learned 2026-09-28, first live batch): sub-cent limit
    prices are rejected with 400 invalid_price above $0.10. The paper cost is
    a depth-weighted average, so the prod IOC limit must round to the cent;
    the disarmed audit row shows exactly what an armed order would send.

    Pinned to the NIGHT band (2026-09-30): the day-band flow gate and no-dump
    tier read LIVE recorder files, so an unpinned no-order here flickered with
    real market conditions. The rounding invariant lives on paper_price; the
    wire price additionally carries the +1c buffer."""
    from crypto_trading.crypto_common.execution_events import EventExecutionRouter
    monkeypatch.setattr(EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 3))
    r = EventExecutionRouter(strategy="w7_noisefade")
    out = r.submit(ticker="KXDOGE15M-x", side="no", entry_price=0.8876,
                   contracts=25, armed=False)
    assert out["status"] == "live_disarmed"
    assert out["paper_price_dollars"] == 0.89
    assert out["price_dollars"] == 0.90 and out["limit_buffer_c"] == 1
    assert r.submit(ticker="KXBTC15M-x", side="no", entry_price=0.85,
                    contracts=25, armed=False)["paper_price_dollars"] == 0.85
    assert r.submit(ticker="KXETH15M-x", side="no", entry_price=0.904,
                    contracts=25, armed=False)["paper_price_dollars"] == 0.90
    assert r.submit(ticker="KXSOL15M-x", side="yes", entry_price=0.985,
                    contracts=25, armed=False)["price_dollars"] == 0.99


def test_events_no_dump_tier_quarters_day_band_no_orders(monkeypatch):
    """2026-09-30 no-after-dump tier: DAY-band NO orders are cut to x0.25 when
    the coin's prior-60min index move < -50bp; YES orders and milder moves are
    untouched; a missing trend fails OPEN to full size; the NIGHT band never
    consults the trend at all. Audit rides on every day-band NO row."""
    import crypto_trading.crypto_common.execution_events as ee

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("no venue client while disarmed")
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient",
        Boom)
    monkeypatch.setattr(ee.EventExecutionRouter, "_tail_rows",
                        staticmethod(lambda path, n: []))
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 14))

    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: -80.0))
    r = w7.submit(ticker="KXBTC15M-26SEP301400-00", side="no",
                  entry_price=0.85, contracts=25)
    assert r["contracts"] == 10 and r["no_dump"] == {"trend_bp": -80.0,
                                                     "lookback_s": 1800.0,
                                                     "mult": 0.25}
    # dump-quartered NO is EXCLUDED from the +1c buffer (user design)
    assert r["price_dollars"] == 0.85 and r["limit_buffer_c"] == 0
    r = w7.submit(ticker="KXETH15M-26SEP301400-00", side="no",
                  entry_price=0.85, contracts=25)
    assert r["contracts"] == 8                      # 30 x0.25 = 7.5 -> 8
    # yes untouched, and carries no no_dump audit
    r = w7.submit(ticker="KXETH15M-26SEP301400-00", side="yes",
                  entry_price=0.85, contracts=25)
    assert r["contracts"] == 30 and "no_dump" not in r

    # milder move and missing trend: full size, audit says mult 1.0
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: -30.0))
    r = w7.submit(ticker="KXETH15M-26SEP301400-00", side="no",
                  entry_price=0.85, contracts=25)
    assert r["contracts"] == 30 and r["no_dump"]["mult"] == 1.0
    # a NORMAL no order gets the buffer
    assert r["price_dollars"] == 0.86 and r["limit_buffer_c"] == 1
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    r = w7.submit(ticker="KXETH15M-26SEP301400-00", side="no",
                  entry_price=0.85, contracts=25)
    assert r["contracts"] == 30 and r["no_dump"] == {"trend_bp": None,
                                                     "lookback_s": 1800.0,
                                                     "mult": 1.0}

    # night band never consults the trend (a poisoned seam must not fire)
    def poison(cls, coin, lb=1800.0):
        raise AssertionError("night band must not read the trend")
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(poison))
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 3))
    r = w7.submit(ticker="KXBTC15M-26SEP300300-00", side="no",
                  entry_price=0.85, contracts=25)
    assert r["contracts"] == 20 and "no_dump" not in r          # night BTC 20 (v4, 2026-10-04)


def test_events_limit_buffer_cap_and_scope(monkeypatch):
    """+1c buffer caps at 0.99 and never touches non-PROD_SIZING strategies."""
    import crypto_trading.crypto_common.execution_events as ee

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("no venue client while disarmed")
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient",
        Boom)
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour",
                        staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")
    r = w7.submit(ticker="KXBTC15M-X", side="yes", entry_price=0.98,
                  contracts=25)
    assert r["price_dollars"] == 0.99 and r["limit_buffer_c"] == 1
    r = w7.submit(ticker="KXBTC15M-X", side="yes", entry_price=0.99,
                  contracts=25)
    assert r["price_dollars"] == 0.99 and r["limit_buffer_c"] == 0
    other = ee.EventExecutionRouter(strategy="w5_knockdown")
    r = other.submit(ticker="KXBTC15M-X", side="yes", entry_price=0.90,
                     contracts=25)
    assert r["price_dollars"] == 0.90 and r["limit_buffer_c"] == 0


def _user_dir(tmp_path, monkeypatch, users, owner_kid="owner-kid", allow=None, ratios=None):
    """Build an isolated ~/.kalshi with validated user accounts.

    ``allow``: the owner's manual live-trading allowlist; defaults to every
    user built here (pass [] to model 'validated but not enabled'). Every user
    also gets the owner's approval record at ratio 1.0 unless `ratios` says
    otherwise (None = no approval for that user)."""
    import json as _j
    import crypto_trading.crypto_common.config as cfg
    d = tmp_path / "kalshi"
    (d / "disabled").mkdir(parents=True)
    (d / "trading" / "approved").mkdir(parents=True)
    reg, lines = {}, []
    for uid, kid in users:
        pem = d / f"prod_{uid}.pem"
        pem.write_text("dummy")
        reg[uid] = {"status": "active"}
        lines += [f"KALSHI_PROD_API_KEY_ID_{uid}={kid}",
                  f"KALSHI_PROD_PRIVATE_KEY_PATH_{uid}={pem}"]
        ratio = (ratios or {}).get(uid, 1.0)
        if ratio is not None:
            (d / "trading" / "approved" / f"{uid}.json").write_text(_j.dumps({"user_id": uid, "ratio": ratio}))
    (d / "users.json").write_text(_j.dumps(reg))
    (d / "users.env").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(cfg, "USER_KEY_DIR", d)
    allowed = ",".join(u for u, _ in users) if allow is None else ",".join(allow)
    fake = {"KALSHI_PROD_API_KEY_ID": owner_kid, "KALSHI_PROD_TRADING_USER_IDS": allowed}
    monkeypatch.setattr(cfg, "env", lambda k, default="": fake.get(k) or default)
    return d


def test_validated_user_never_trades_without_owner_allowlist(tmp_path, monkeypatch):
    """2026-10-01 user directive: passing the key check gives a user his own
    VIEW only. Mirroring W7 into his account needs the owner's manual
    allowlist entry; an empty list means the owner trades alone."""
    import crypto_trading.crypto_common.config as cfg
    import crypto_trading.crypto_common.execution_events as ee
    _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")], allow=[])
    assert cfg.kalshi_user_accounts() == []
    calls = []

    class Client:
        def __init__(self, *, env=None, key=None, **k):
            self.who = "owner" if key is None else key.key_id

        def create_order(self, **kw):
            calls.append(self.who)
            return {"status_code": 201, "response": "{}", "body_sent": {}}
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Client)
    monkeypatch.setattr(ee.EventExecutionRouter, "gate_status", lambda self: {"live_open": True})
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    import threading
    spawned = []
    real_thread = threading.Thread
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: spawned.append(k.get("name")) or real_thread(*a, **k))
    r = ee.EventExecutionRouter(strategy="w7_noisefade").submit(
        ticker="KXETH15M-X", side="yes", entry_price=0.85, contracts=25, armed=True)
    assert calls == ["owner"] and "user_results" not in r     # owner row byte-identical to before
    assert "w7-user-mirror" not in spawned                    # empty allowlist: no thread at all
    assert not (tmp_path / "kalshi" / "journal").exists()     # ...and no journal I/O
    # allowlisting ONE user enables exactly that one
    _user_dir(tmp_path / "b", monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")], allow=[U2])
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U2]


U1 = "aaaaaaa1-0000-4000-8000-000000000001"
U2 = "aaaaaaa2-0000-4000-8000-000000000002"


def test_user_accounts_mirror_after_owner_identical_intent(tmp_path, monkeypatch):
    """2026-10-01: validated user accounts receive the IDENTICAL order the
    owner sent, strictly AFTER the owner's order returned; a 401 disables only
    that account; the owner's own key id can never appear as a user."""
    import crypto_trading.crypto_common.execution_events as ee
    import crypto_trading.crypto_common.config as cfg
    _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")])
    calls = []

    class Client:
        def __init__(self, *, env=None, key=None, **k):
            assert env == "prod"
            self.who = "owner" if key is None else key.key_id

        def create_order(self, **kw):
            calls.append((self.who, kw))
            code = 401 if self.who == "kid-u2" else 201
            return {"status_code": code, "response": "{}", "body_sent": {}}
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Client)
    monkeypatch.setattr(ee.EventExecutionRouter, "gate_status",
                        lambda self: {"live_open": True})
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    # run the (normally daemon-thread) user mirror inline so the test is deterministic
    monkeypatch.setattr(ee.EventExecutionRouter, "_spawn_user_mirror",
                        lambda self, intent: self._mirror_user_accounts(dict(intent)))
    r = ee.EventExecutionRouter(strategy="w7_noisefade").submit(
        ticker="KXETH15M-X", side="yes", entry_price=0.85, contracts=25, armed=True)
    assert r["status"] == "live_sent"
    assert "user_results" not in r                      # owner row never carries user data
    assert calls[0][0] == "owner"                       # owner first, always
    assert sorted(c[0] for c in calls[1:]) == ["kid-u1", "kid-u2"]
    owner_kw = calls[0][1]
    assert all(kw == owner_kw for _, kw in calls[1:])   # identical intent
    assert owner_kw["count"] == 30 and owner_kw["price_dollars"] == 0.86
    rows = _journal(tmp_path)
    by = {}
    for row in rows:
        by.setdefault(row["account"], []).append(row)
    for uid in (U1, U2):                                # pending_send, then result, same mirror_id
        assert [x["status"] for x in by[uid]] == ["pending_send", "live_sent"]
        assert by[uid][0]["mirror_id"] == by[uid][1]["mirror_id"]
        assert by[uid][1]["ticker"] == "KXETH15M-X" and by[uid][1]["contracts"] == 30
    assert by[U1][1]["status_code"] == 201 and by[U2][1].get("disabled") is True
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U1]   # U2 now off
    jd = tmp_path / "kalshi" / "journal"
    assert oct(jd.stat().st_mode & 0o777) == "0o700"
    assert all(oct(f.stat().st_mode & 0o777) == "0o600" for f in jd.iterdir())


def _journal(tmp_path):
    import json as _j
    out = []
    for f in sorted((tmp_path / "kalshi" / "journal").glob("*.jsonl")):
        out += [_j.loads(x) for x in f.read_text().splitlines() if x.strip()]
    return out


def test_gate_skips_are_journaled_only_for_mirrored_users_and_only_when_armed(tmp_path, monkeypatch):
    import crypto_trading.crypto_common.execution_events as ee
    import crypto_trading.crypto_strategies.downside_paper.policy as pol
    _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")], allow=[U1])
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    monkeypatch.setattr(pol, "decide", lambda c, f, p: {"decision": "skip"})
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")
    assert w7.submit(ticker="KXETH15M-X", side="yes", entry_price=0.85,
                     contracts=25)["status"] == "skipped_by_flow_gate"   # disarmed
    assert not (tmp_path / "kalshi" / "journal").exists()
    r = w7.submit(ticker="KXETH15M-X", side="yes", entry_price=0.85, contracts=25, armed=True)
    assert r["status"] == "skipped_by_flow_gate" and "user_results" not in r
    rows = _journal(tmp_path)
    assert [(x["account"], x["status"]) for x in rows] == [(U1, "skipped_by_flow_gate")]


def test_user_accounts_never_receive_disarmed_or_gated_orders(tmp_path, monkeypatch):
    import crypto_trading.crypto_common.execution_events as ee
    import crypto_trading.crypto_strategies.downside_paper.policy as pol
    _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1")])

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("no order may be built")
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Boom)
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")
    assert w7.submit(ticker="KXETH15M-X", side="yes", entry_price=0.85,
                     contracts=25)["status"] == "live_disarmed"
    monkeypatch.setattr(pol, "decide", lambda c, f, p: {"decision": "skip"})
    assert w7.submit(ticker="KXETH15M-X", side="yes", entry_price=0.85,
                     contracts=25, armed=True)["status"] == "skipped_by_flow_gate"


def test_user_account_loader_rejects_owner_key_and_incomplete_entries(tmp_path, monkeypatch):
    import crypto_trading.crypto_common.config as cfg
    d = _user_dir(tmp_path, monkeypatch, [(U1, "owner-kid"), (U2, "kid-u2")])
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U2]   # owner key excluded
    (d / f"prod_{U2}.pem").unlink()
    assert cfg.kalshi_user_accounts() == []                           # missing key file
    (d / "users.json").write_text("{not json")
    assert cfg.kalshi_user_accounts() == []                           # corrupt registry


def test_owner_private_key_reuploaded_under_another_login_is_never_a_user(tmp_path, monkeypatch):
    """D0 (2026-10-01 review): exclusion is by the key the owner's orders really
    sign with AND by private-key fingerprint, not only KALSHI_PROD_API_KEY_ID."""
    import crypto_trading.crypto_common.config as cfg
    d = _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")])
    owner_pem = tmp_path / "owner.pem"
    owner_pem.write_text("OWNER-PRIVATE-KEY")
    (d / f"prod_{U1}.pem").write_text("OWNER-PRIVATE-KEY\n")      # same key, other login
    fake = {"KALSHI_PROD_API_KEY_ID": "owner-kid", "KALSHI_PROD_PRIVATE_KEY_PATH": str(owner_pem),
            "KALSHI_MARGIN_KEY_ID": "margin-kid",
            "KALSHI_PROD_TRADING_USER_IDS": f"{U1},{U2}"}
    monkeypatch.setattr(cfg, "env", lambda k, default="": fake.get(k) or default)
    monkeypatch.setattr(cfg, "_PM_ENV", {})
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U2]
    # a user carrying the owner's MARGIN key id is excluded too
    (d / "users.env").write_text(f"KALSHI_PROD_API_KEY_ID_{U2}=margin-kid\nKALSHI_PROD_PRIVATE_KEY_PATH_{U2}={d}/prod_{U2}.pem\n")
    assert cfg.kalshi_user_accounts() == []


def test_one_kalshi_account_is_never_mirrored_twice(tmp_path, monkeypatch):
    """2026-10-01 review: one person with two Supabase logins could verify the
    SAME Kalshi account twice. Every user sharing a Kalshi account with another
    user id (same key id, or overlapping account key-id hashes recorded at
    verification) or with the owner is dropped - never double-ordered."""
    import hashlib
    import json as _j
    import crypto_trading.crypto_common.config as cfg
    h = lambda k: hashlib.sha256(k.encode()).hexdigest()
    U3 = "aaaaaaa3-0000-4000-8000-000000000003"
    # same key id under two user ids -> both out, the third user unaffected
    _user_dir(tmp_path / "a", monkeypatch, [(U1, "kid-x"), (U2, "kid-x"), (U3, "kid-3")])
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U3]
    # different key ids, but the same Kalshi account (verification saw both keys)
    d = _user_dir(tmp_path / "b", monkeypatch, [(U1, "kid-1"), (U2, "kid-2"), (U3, "kid-3")])
    reg = _j.loads((d / "users.json").read_text())
    reg[U1]["account_key_hashes"] = [h("kid-1"), h("kid-2")]
    reg[U2]["account_key_hashes"] = [h("kid-2")]
    (d / "users.json").write_text(_j.dumps(reg))
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U3]
    # a user's Kalshi account that also holds the OWNER's key -> out
    reg = {u: {"status": "active"} for u in (U1, U2, U3)}
    reg[U3]["account_key_hashes"] = [h("kid-3"), h("owner-kid")]
    (d / "users.json").write_text(_j.dumps(reg))
    assert [a.user_id for a in cfg.kalshi_user_accounts()] == [U1, U2]



def test_user_needs_owner_approval_and_stop_marker_wins(tmp_path, monkeypatch):
    """2026-10-04 standard flow: an allowlisted user is mirrored only with the
    owner's approval record (valid ratio, same id). The user's own application
    never trades by itself; a stop marker (user on the panel, or the owner)
    takes effect on the very next call, no restart."""
    import json as _j
    import crypto_trading.crypto_common.config as cfg
    d = _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")], ratios={U1: 0.25, U2: None})
    (d / "trading" / "requests").mkdir(parents=True)
    (d / "trading" / "requests" / f"{U2}.json").write_text(
        _j.dumps({"user_id": U2, "ratio": 1.0, "ack_version": "2026-10-04"}))
    assert [(a.user_id, a.ratio) for a in cfg.kalshi_user_accounts()] == [(U1, 0.25)]   # U2 applied, not approved
    for bad in ({"user_id": U2, "ratio": 0.5}, {"user_id": U1, "ratio": 0}, {"user_id": U1, "ratio": 1.5},
                {"user_id": U1, "ratio": "x"}, {"user_id": U1}, [1]):
        (d / "trading" / "approved" / f"{U1}.json").write_text(_j.dumps(bad))
        assert cfg.kalshi_user_accounts() == [], bad              # other id / outside (0, 1] / garbage
    (d / "trading" / "approved" / f"{U1}.json").write_text(_j.dumps({"user_id": U1, "ratio": 0.5}))
    assert [a.ratio for a in cfg.kalshi_user_accounts()] == [0.5]
    (d / "trading" / "stopped").mkdir(parents=True)
    (d / "trading" / "stopped" / f"{U1}.json").write_text(_j.dumps({"user_id": U1, "by": "user"}))
    assert cfg.kalshi_user_accounts() == []                       # stop wins


def test_user_orders_are_scaled_by_the_approved_ratio(tmp_path, monkeypatch):
    """User contracts = floor(owner contracts x ratio), never above the owner's;
    below one contract nothing is sent and the skip is journaled. The owner's
    own order is untouched."""
    import crypto_trading.crypto_common.execution_events as ee
    _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1"), (U2, "kid-u2")], ratios={U1: 0.25, U2: 0.1})
    calls = []

    class Client:
        def __init__(self, *, env=None, key=None, **k):
            self.who = "owner" if key is None else key.key_id

        def create_order(self, **kw):
            calls.append((self.who, kw["count"]))
            return {"status_code": 201, "response": "{}", "body_sent": {}}
    monkeypatch.setattr(
        "crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Client)
    monkeypatch.setattr(ee.EventExecutionRouter, "gate_status", lambda self: {"live_open": True})
    monkeypatch.setattr(ee.EventExecutionRouter, "MACRO_GUARD", {})     # never depend on the real calendar
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp",
                        classmethod(lambda cls, coin, lb=1800.0: None))
    monkeypatch.setattr(ee.EventExecutionRouter, "_spawn_user_mirror",
                        lambda self, intent: self._mirror_user_accounts(dict(intent)))
    r = ee.EventExecutionRouter(strategy="w7_noisefade").submit(
        ticker="KXETH15M-X", side="yes", entry_price=0.85, contracts=25, armed=True)
    assert r["status"] == "live_sent" and calls[0] == ("owner", 30)    # owner: alt day base 30
    assert sorted(calls[1:]) == [("kid-u1", 7), ("kid-u2", 3)]          # floor(30*.25)=7, floor(30*.1)=3
    sent = [x for x in _journal(tmp_path) if x["status"] == "live_sent"]
    assert sorted((x["account"], x["contracts"], x["owner_contracts"], x["ratio"]) for x in sent) == \
        [(U1, 7, 30, 0.25), (U2, 3, 30, 0.1)]
    calls.clear()                                                       # a 5-lot (macro guard size)
    out = ee.EventExecutionRouter(strategy="w7_noisefade")._mirror_user_accounts(
        {"ticker": "KXETH15M-Y", "side": "yes", "contracts": 5, "price_dollars": 0.9})
    assert calls == [("kid-u1", 1)]                                     # floor(5*.25)=1; 10% of 5 < 1
    assert [(x["account"], x["status"], x["contracts"]) for x in out if x["account"] == U2] == \
        [(U2, "skipped_below_one_contract", 0)]

def test_prod_band_and_night_sizes_v4_20261004():
    """User 2026-10-04: W7 prod (and its paper mirrors W11/W13, which read these constants) trade only
    favourites priced 0.80-0.97; the night band (UTC 00-05) sizes are 20 for BTC and the alts alike."""
    import inspect
    import crypto_trading.crypto_common.execution_events as ee
    # conftest blanks PROD_BAND for every test, so check the shipped value in the source itself
    assert 'PROD_BAND: dict = {"w7_noisefade": (0.80, 0.97)}' in inspect.getsource(ee)
    sz = ee.EventExecutionRouter.PROD_SIZING["w7_noisefade"]
    assert sz["night"] == {"KXBTC15M": 20, "default": 20} and sz["day"] == {"KXBTC15M": 40, "default": 30}
    assert tuple(sz["night_hours_utc"]) == (0, 1, 2, 3, 4, 5)


def test_session_isolation_from_real_user_accounts_and_venue_network():
    """2026-10-05 incident guard (tests/conftest.py): no test can reach the real
    ~/.kalshi, the real allowlist, or any Kalshi/Supabase host - not even a daemon
    thread that outlives its test."""
    from pathlib import Path
    import requests
    import crypto_trading.crypto_common.config as cfg
    import crypto_trading.ops.export_prediction_frontend as ex
    assert cfg.trading_user_ids() == set()
    assert cfg.USER_KEY_DIR != Path.home() / ".kalshi" and ex.USER_KEY_DIR == cfg.USER_KEY_DIR
    assert cfg.kalshi_user_accounts() == []
    for url in ("https://external-api.kalshi.com/trade-api/v2/portfolio/orders",
                "https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance",
                "https://abc.supabase.co/auth/v1/admin/users"):
        with pytest.raises(RuntimeError, match="blocked in tests"):
            requests.Session().post(url, json={})


def test_user_mirror_inherits_the_no_1_5x_cap(tmp_path, monkeypatch):
    """2026-10-05 (user: 去掉 1.5 倍, also for the user account): the mirror sends the
    owner's FINAL intent x the user's ratio, so the x1.5 cap reaches every user too."""
    import crypto_trading.crypto_common.execution_events as ee
    _user_dir(tmp_path, monkeypatch, [(U1, "kid-u1")], ratios={U1: 1.0})
    calls = []

    class Client:
        def __init__(self, *, env=None, key=None, **k):
            self.who = "owner" if key is None else key.key_id

        def create_order(self, **kw):
            calls.append((self.who, kw["count"]))
            return {"status_code": 201, "response": "{}", "body_sent": {}}
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", Client)
    monkeypatch.setattr(ee.EventExecutionRouter, "TABLE_LIVE",
                        {"w7_noisefade": {"enabled": True, "window_cap_mult": 1.5, "max_size_mult": 1.0}})
    monkeypatch.setattr(ee.EventExecutionRouter, "MACRO_GUARD", {})
    monkeypatch.setattr(ee.EventExecutionRouter, "gate_status", lambda self: {"live_open": True})
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_trend_bp", classmethod(lambda cls, coin, lb=1800.0: None))
    monkeypatch.setattr(ee.EventExecutionRouter, "_flow_gate_decision", lambda self, t, s: {"decision": "accept"})
    monkeypatch.setattr(ee.EventExecutionRouter, "_spawn_user_mirror",
                        lambda self, intent: self._mirror_user_accounts(dict(intent)))
    r = ee.EventExecutionRouter(strategy="w7_noisefade").submit(
        ticker="KXBTC15M-26OCT050800-00", side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
        entry_source="table_top1_T-9.75", table_decision={"action": "trade", "slot": "top1", "mult": 1.5, "scenario": "S"})
    assert r["status"] == "live_sent" and r["contracts"] == 40 and r["size_mult"] == 1.0 and r["size_mult_table"] == 1.5
    assert calls == [("owner", 40), ("kid-u1", 40)]                  # day BTC base 40, not 60, for both accounts
