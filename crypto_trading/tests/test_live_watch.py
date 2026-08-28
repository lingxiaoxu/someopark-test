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
    """W7 re-frozen 2026-08-27 to the 15M favorite-NO rule (up-side premium)."""
    from crypto_trading.crypto_strategies.event_binary import research_favorite_no as fn
    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    assert w7.REM_MIN_TARGET is fn.REM_MIN_TARGET
    assert (w7.COST_LO, w7.COST_HI) == (fn.COST_LO, fn.COST_HI)
    assert w7.MARKETS is fn.MARKETS
    assert w7.favorite_is_no is fn.favorite_is_no and w7.no_cost is fn.no_cost


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


def test_walk_book_for_no_is_read_only_and_prices_correctly(monkeypatch):
    """Buying NO consumes the YES-bid ladder at 1-p, best (highest) bid first.
    Reading the no_dollars ladder instead would price against our competition
    rather than our counterparty (the bug this test locks out)."""
    import inspect

    from crypto_trading.crypto_strategies.live_watch import w7_noisefade as w7
    src = inspect.getsource(w7.walk_book_for_no)
    assert "requests.get" in src and ".post(" not in src        # read-only
    # the ladder must be read in CODE — the docstring legitimately mentions
    # no_dollars while explaining the bug this test locks out
    code = "\n".join(l for l in src.splitlines() if "``" not in l)
    assert 'ob.get("yes_dollars")' in code
    assert 'ob.get("no_dollars")' not in code                   # right ladder

    class R:
        status_code = 200
        @staticmethod
        def json():
            # yes bids: 0.30 x10 (NO @0.70), 0.28 x20 (NO @0.72), 0.20 x999
            return {"orderbook_fp": {"yes_dollars": [["0.20", "999"],
                                                     ["0.28", "20"],
                                                     ["0.30", "10"]],
                                     "no_dollars": [["0.99", "5"]]}}
    monkeypatch.setattr("requests.get", lambda *a, **k: R())
    d = w7.walk_book_for_no("KXBTC15M-X", 25)
    assert d["top_cost"] == 0.70                       # 1 - best yes bid 0.30
    # 10 @0.70 + 15 @0.72 = 17.8 / 25 = 0.712
    assert d["fill_cost"] == 0.712
    assert d["filled"] == 25 and d["shortfall"] == 0
    assert d["slippage_c"] == 1.2                      # 1.2c worse than touch
