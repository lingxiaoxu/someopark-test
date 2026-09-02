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


def test_walk_book_sides_are_read_only_and_price_correctly(monkeypatch):
    """Single book: buying a side consumes the OTHER side's resting bids at
    1-p, best (highest) first. Reading your OWN side's ladder prices you
    against your competition, not your counterparty (the v2 bug, 2026-08-27).
    v3 locks the mapping for BOTH sides."""
    import inspect

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    assert w7.LADDER_FOR_SIDE == {"no": "yes_dollars", "yes": "no_dollars"}
    src = inspect.getsource(w7.walk_book)
    assert "requests.get" in src and ".post(" not in src        # read-only

    class R:
        status_code = 200
        @staticmethod
        def json():
            # yes bids: 0.30 x10 (NO @0.70), 0.28 x20 (NO @0.72), 0.20 x999
            # no bids: 0.24 x10 (YES @0.76), 0.22 x20 (YES @0.78)
            return {"orderbook_fp": {"yes_dollars": [["0.20", "999"],
                                                     ["0.28", "20"],
                                                     ["0.30", "10"]],
                                     "no_dollars": [["0.22", "20"],
                                                    ["0.24", "10"]]}}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    d = w7.walk_book("KXBTC15M-X", 25, "no")
    assert d["top_cost"] == 0.70                       # 1 - best yes bid 0.30
    # 10 @0.70 + 15 @0.72 = 17.8 / 25 = 0.712
    assert d["fill_cost"] == 0.712
    assert d["filled"] == 25 and d["shortfall"] == 0
    assert d["slippage_c"] == 1.2                      # 1.2c worse than touch
    y = w7.walk_book("KXBTC15M-X", 25, "yes")
    assert y["top_cost"] == 0.76                       # 1 - best NO bid 0.24
    # 10 @0.76 + 15 @0.78 = 19.3 / 25 = 0.772
    assert y["fill_cost"] == 0.772


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


def test_w7_drift_gate_and_both_side_entry(sandbox, monkeypatch):
    """v3: the frozen LEG must hold at the ACTUAL fill price, not just on the
    (up to 90s stale) snapshot — v2 recorded 69/972 out-of-leg trades at
    -15.8c avg that were never the registered strategy. And the probe now
    enters WHICHEVER side is the favorite."""
    import pandas as pd

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7

    now = pd.Timestamp.now(tz="UTC")
    close = (now + pd.Timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {"recv_ts": now.timestamp(),
            "markets": [{"ticker": "KXBTC15M-NOFAV", "close_time": close,
                         "yes_bid_dollars": "0.25", "yes_ask_dollars": "0.27"}]}
    monkeypatch.setattr(w7, "latest_snapshot",
                        lambda s: snap if s == "KXBTC15M" else None)
    common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                       "cum_net_usd": 0.0})
    cfg = {"w7_noisefade": {"enabled": False, "contracts": 25}}

    # book drifted: NO now fills at 0.30 (out of leg) -> no entry, drift_skip
    monkeypatch.setattr(w7, "walk_book",
                        lambda t, c, side: {"top_cost": 0.30, "fill_cost": 0.30,
                                            "filled": 25, "shortfall": 0,
                                            "slippage_c": 0.0})
    rep = w7.run(cfg)
    assert rep["entries"] == [] and rep["open_virtual"] == 0
    assert rep["probe"]["entered"] == 0 and rep["probe"]["drift_skips"] == 1
    st = common.load_state("w7_noisefade")
    assert "KXBTC15M-NOFAV" in st["seen_tickers"]     # one look per window

    # in-leg book price -> NO-favorite enters at the book VWAP, with the
    # maker level posted one cent inside the touch (yes_bid 0.25 -> 0.26)
    common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                       "cum_net_usd": 0.0})
    monkeypatch.setattr(w7, "walk_book",
                        lambda t, c, side: {"top_cost": 0.74, "fill_cost": 0.7460,
                                            "filled": 25, "shortfall": 0,
                                            "slippage_c": 0.6})
    rep = w7.run(cfg)
    assert rep["probe"]["entered"] == 1
    assert rep["entries"][0]["side"] == "no"
    assert rep["entries"][0]["cost"] == 0.746
    st = common.load_state("w7_noisefade")
    pos = st["positions"]["KXBTC15M-NOFAV"]
    # maker posts inside the BOOK touch (top_cost 0.74 -> yes_bid 0.26 ->
    # post 0.27), NOT the stale snapshot touch (yb 0.25 -> 0.26): the
    # 2026-09-01 live catch — snapshot-posted levels sat 16-21c off-market.
    assert pos["maker_posted"] == 0.27

    # YES-favorite window (yes_mid 0.74) -> buys YES; obs leg when the fill
    # lands in [0.50, 0.60): recorded as leg="obs", not counted as "entered"
    snap2 = {"recv_ts": now.timestamp(),
             "markets": [{"ticker": "KXBTC15M-YESFAV", "close_time": close,
                          "yes_bid_dollars": "0.72", "yes_ask_dollars": "0.76"}],
             }
    monkeypatch.setattr(w7, "latest_snapshot",
                        lambda s: snap2 if s == "KXBTC15M" else None)
    common.save_state("w7_noisefade", {"positions": {}, "trades": [],
                                       "cum_net_usd": 0.0})
    monkeypatch.setattr(w7, "walk_book",
                        lambda t, c, side: {"top_cost": 0.76, "fill_cost": 0.762,
                                            "filled": 25, "shortfall": 0,
                                            "slippage_c": 0.2})
    rep = w7.run(cfg)
    assert rep["entries"][0]["side"] == "yes"
    assert rep["entries"][0]["cost"] == 0.762
    st = common.load_state("w7_noisefade")
    # book yes_ask (top_cost) 0.76 -> post 0.75
    assert st["positions"]["KXBTC15M-YESFAV"]["maker_posted"] == 0.75


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
