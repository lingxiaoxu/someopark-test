"""tests/test_gates_step4.py — entropy gate, devig-based sanity gap, capture filter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prediction_market_macro.strategy.capture import cap_key, filter_structs
from prediction_market_macro.strategy.decision import decide
from prediction_market_macro.strategy.edge import Leg, Struct

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
CLOSE = NOW + timedelta(hours=6)


def _st(fair, cost, ticker="T1", side="yes", depth=500.0, kind="single"):
    return Struct(kind, (Leg(ticker, side, cost, depth),), fair=fair, cost=cost,
                  max_loss=cost, desc=f"{side.upper()} {ticker} @{cost:.2f}")


def test_entropy_gate_flat_pmf_passes():
    d = decide([_st(0.60, 0.50)], now=NOW, close_time=CLOSE, release_ts=None,
               market_implied=None, already_open=False, bankroll=100.0,
               entropy_norm=0.99)
    assert d.action == "pass" and any("entropy" in r for r in d.reasons)


def test_entropy_gate_sharp_pmf_proceeds():
    d = decide([_st(0.60, 0.50)], now=NOW, close_time=CLOSE, release_ts=None,
               market_implied=None, already_open=False, bankroll=100.0,
               entropy_norm=0.40)
    assert d.action == "open"


def test_sanity_gap_uses_devig_not_cost():
    # fair 0.60 vs cost 0.30 → raw gap 0.30 (>0.25 would block), but the devigged
    # market prob is 0.50 → gap 0.10 → allowed (spread noise removed)
    st = _st(0.60, 0.30)
    blocked = decide([st], now=NOW, close_time=CLOSE, release_ts=None,
                     market_implied=None, already_open=False, bankroll=100.0)
    assert blocked.action == "pass"
    allowed = decide([st], now=NOW, close_time=CLOSE, release_ts=None,
                     market_implied={st.desc: 0.50}, already_open=False,
                     bankroll=100.0)
    assert allowed.action == "open"


def test_depth_frac_cap_blocks_thin_book():
    thin = _st(0.60, 0.50, depth=60.0)     # passes $50 gate; 20% cap = $12 > cost ok
    d = decide([thin], now=NOW, close_time=CLOSE, release_ts=None,
               market_implied=None, already_open=False, bankroll=100.0)
    assert d.action == "open" and d.size_usd <= 12.0
    micro = _st(0.80, 0.60, depth=2.0)     # gap 0.20 clears sanity; 20%×$2 < cost
    g = dict(__import__("prediction_market_macro.strategy.decision",
                        fromlist=["GATES"]).GATES)
    g["min_leg_depth_usd"] = 0.0
    d2 = decide([micro], now=NOW, close_time=CLOSE, release_ts=None,
                market_implied=None, already_open=False, bankroll=100.0, gates=g)
    assert d2.action == "pass" and any("depth_cap" in r for r in d2.reasons)


def test_cap_key_offsets():
    assert cap_key("yes", 101.0, 100.0, 0.5) == "yes@+2"
    assert cap_key("no", 96.0, 100.0, 0.5) == "no@-5"      # clipped at -5... -8→-5
    assert cap_key("yes", None, 100.0, 0.5) is None


def test_filter_structs_drops_bad_capture():
    capture = {"yes@+2": {"n": 10, "expected": 1.0, "realized": 0.1},
               "yes@-1": {"n": 10, "expected": 1.0, "realized": 0.9}}
    good = _st(0.6, 0.5, ticker="G")
    bad = _st(0.6, 0.5, ticker="B")
    strikes = {"G": 99.5, "B": 101.0}
    kept, dropped = filter_structs([good, bad], capture, 100.0, 0.5, strikes)
    assert good in kept and bad not in kept
    assert len(dropped) == 1 and "capture_gate" in dropped[0]


def test_filter_structs_small_n_passes():
    capture = {"yes@+2": {"n": 3, "expected": 1.0, "realized": 0.0}}
    st = _st(0.6, 0.5, ticker="B")
    kept, dropped = filter_structs([st], capture, 100.0, 0.5, {"B": 101.0})
    assert st in kept and not dropped
