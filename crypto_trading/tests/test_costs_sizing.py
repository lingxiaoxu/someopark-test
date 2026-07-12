"""costs.py + sizing.py tests — synthetic, no network."""
from crypto_trading.crypto_common.costs import (fee_dollars, funding_payment,
                                                round_trip_cost_dollars,
                                                slippage_dollars, walk_book)
from crypto_trading.crypto_common.sizing import SizeDecision, SizingConfig, size_position

ASKS = [["6.40", "10"], ["6.39", "5"], ["6.41", "20"]]   # unsorted, best 6.39
BIDS = [["6.36", "10"], ["6.38", "4"], ["6.37", "6"]]    # best 6.38


def test_walk_book_buy_sorts_ascending_and_averages():
    w = walk_book(ASKS, 10, side="buy")
    # 5 @ 6.39 + 5 @ 6.40
    assert w.filled == 10 and abs(w.avg_price - 6.395) < 1e-12
    assert w.worst_price == 6.40 and not w.exhausted


def test_walk_book_sell_descends_and_exhausts():
    w = walk_book(BIDS, 25, side="sell")
    assert w.exhausted and w.filled == 20            # book only has 20
    assert w.worst_price == 6.36


def test_slippage_positive_vs_mid():
    s = slippage_dollars(ASKS, 10, side="buy", mid=6.385)
    # avg 6.395 vs mid 6.385 → 0.01/contract × 10
    assert abs(s["dollars"] - 0.10) < 1e-9 and s["bps"] > 0


def test_funding_sign_convention():
    # positive rate: long pays, short receives
    assert funding_payment(+100, 6.38, 1e-4) < 0
    assert funding_payment(-100, 6.38, 1e-4) > 0
    assert funding_payment(0, 6.38, 1e-4) == 0


def test_fee_scenarios():
    assert fee_dollars(1000, scenario="zero") == 0.0
    proj = fee_dollars(1000, role="maker", scenario="projected")
    assert proj == 1000 * 0.0005                     # probe default maker rate


def test_round_trip_includes_both_legs_and_fees():
    rt = round_trip_cost_dollars(ASKS, BIDS, 5, mid=6.385, scenario="zero")
    # buy 5 @6.39 → +0.025; sell walks 4@6.38 + 1@6.37 (avg 6.378) → +0.035
    assert abs(rt - 0.06) < 1e-9


def test_sizing_vol_target_and_leverage_cap():
    # low realized vol → vol-target wants 4× → leverage cap binds at 2×
    d = size_position(equity=1000, contract_price=6.38, realized_vol_annual=0.10,
                      cfg=SizingConfig(target_vol_annual=0.40, leverage_max=2.0))
    assert d.binding == "leverage" and d.leverage <= 2.0 + 1e-9
    assert d.contracts == int(2000 // 6.38)


def test_sizing_min_size_refusal():
    d = size_position(equity=5, contract_price=6.38, realized_vol_annual=0.80,
                      cfg=SizingConfig(target_vol_annual=0.40))
    assert d.contracts == 0 and d.binding == "min-size"


def test_sizing_cost_gate_refuses_thin_edge():
    d = size_position(equity=1000, contract_price=6.38, realized_vol_annual=0.40,
                      expected_edge_dollars=0.01, round_trip_cost_dollars=0.05)
    assert isinstance(d, SizeDecision) and d.contracts == 0 and d.binding == "cost-gate"


def test_sizing_kelly_cap_binds_on_tiny_edge():
    d = size_position(equity=1000, contract_price=6.38, realized_vol_annual=0.40,
                      edge_per_notional=0.001,
                      cfg=SizingConfig(target_vol_annual=0.40, kelly_fraction=0.25))
    # full Kelly f* = 0.001/0.16 = 0.00625 → 0.25× → 1.5625 notional → 0 contracts
    assert d.contracts == 0 and d.binding == "min-size"
