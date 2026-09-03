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
