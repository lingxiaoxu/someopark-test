"""Ledger tests (Plan 07 §1) — synthetic, no network."""
import pandas as pd
import pytest

from crypto_trading.crypto_common.reporting.ledger import (LedgerRow, compute_ledger_row,
                                                           position_at, position_timeline,
                                                           reconcile_fills,
                                                           reconcile_funding)


def fills_df(rows):
    return pd.DataFrame(rows)


BASE_FILLS = [
    # buy 10 @ 6.39 (decision mid 6.385), later sell 10 @ 6.48 (mid 6.485)
    {"ts": 100.0, "ticker": "KXBTCPERP", "side": "buy", "count": 10, "price": 6.39,
     "role": "taker", "decision_mid": 6.385, "client_order_id": "a"},
    {"ts": 200.0, "ticker": "KXBTCPERP", "side": "sell", "count": 10, "price": 6.48,
     "role": "taker", "decision_mid": 6.485, "client_order_id": "b"},
]


def test_identity_ties_out_zero_fees():
    row = compute_ledger_row("s1", "KXBTCPERP", fills_df(BASE_FILLS))
    # realized = (6.48-6.39)*10 = 0.9 ; slippage = +0.005*10 + 0.005*10 = 0.1
    assert row.gross_trading == pytest.approx(0.9)
    assert row.slippage == pytest.approx(0.1)
    assert row.fees_zero == 0.0
    assert row.net_zero == pytest.approx(0.9 - 0.1)
    # projected scenario differs exactly by the fees
    assert row.net_projected == pytest.approx(row.net_zero - row.fees_projected)
    assert row.fees_projected == pytest.approx((6.39 + 6.48) * 10 * 0.0010)  # taker default


def test_two_scenario_fee_split_maker_vs_taker():
    fills = [dict(BASE_FILLS[0], role="maker"), dict(BASE_FILLS[1], role="taker")]
    row = compute_ledger_row("s1", "KXBTCPERP", fills_df(fills))
    assert row.fees_projected == pytest.approx(6.39 * 10 * 0.0005 + 6.48 * 10 * 0.0010)


def test_unrealized_uses_mark():
    row = compute_ledger_row("s1", "KXBTCPERP", fills_df(BASE_FILLS[:1]), mark=6.50)
    assert row.end_position == 10
    assert row.gross_trading == pytest.approx((6.50 - 6.39) * 10)


def test_funding_applied_to_held_position_pit():
    funding = pd.DataFrame({
        "funding_time": [pd.Timestamp(150.0, unit="s", tz="UTC"),     # long 10 held
                         pd.Timestamp(250.0, unit="s", tz="UTC")],    # flat
        "funding_rate": [1e-4, 1e-4],
        "mark_price": [6.40, 6.50]}).set_index("funding_time")
    row = compute_ledger_row("s1", "KXBTCPERP", fills_df(BASE_FILLS),
                             funding_events=funding)
    # long pays positive rate at t=150 only: −1e-4 × 10 × 6.40
    assert row.funding == pytest.approx(-1e-4 * 10 * 6.40)


def test_position_timeline_and_at():
    tl = position_timeline(fills_df(BASE_FILLS))
    assert position_at(tl, 99.9) == 0.0
    assert position_at(tl, 150.0) == 10.0
    assert position_at(tl, 300.0) == 0.0


def test_ledger_row_dict_has_both_nets():
    d = LedgerRow("s", "T", gross_trading=1.0, fees_projected=0.3).as_dict()
    assert d["net_zero"] == pytest.approx(1.0) and d["net_projected"] == pytest.approx(0.7)


def test_reconcile_fills_catches_breaks():
    intended = pd.DataFrame([
        {"client_order_id": "a", "ticker": "T", "count": 10, "price": 6.39},
        {"client_order_id": "b", "ticker": "T", "count": 5, "price": 6.40},
        {"client_order_id": "c", "ticker": "T", "count": 2, "price": 6.41},
    ])
    actual = pd.DataFrame([
        {"client_order_id": "a", "ticker": "T", "count": 10, "price": 6.39},   # ok
        {"client_order_id": "b", "ticker": "T", "count": 3, "price": 6.40},    # count break
        {"client_order_id": "x", "ticker": "T", "count": 1, "price": 6.42},    # unexpected
    ])
    rec = reconcile_fills(intended, actual)
    kinds = {b["type"] for b in rec["breaks"]}
    assert not rec["ok"]
    assert kinds == {"count_mismatch", "missing_fill", "unexpected_fill"}


def test_reconcile_funding_tie_out():
    ours = pd.DataFrame([
        {"funding_time": "2026-07-07T04:00:00+00:00", "amount": -0.0064},
        {"funding_time": "2026-07-07T12:00:00+00:00", "amount": 0.0031},
    ])
    venue = [{"funding_time": "2026-07-07T04:00:00+00:00", "amount": -0.0064},
             {"funding_time": "2026-07-07T12:00:00+00:00", "amount": 0.0099}]
    rec = reconcile_funding(ours, venue)
    assert not rec["ok"]
    assert rec["breaks"][0]["type"] == "amount_mismatch"
    ok = reconcile_funding(ours, [{"funding_time": "2026-07-07T04:00:00+00:00",
                                   "amount": -0.0064},
                                  {"funding_time": "2026-07-07T12:00:00+00:00",
                                   "amount": 0.0031}])
    assert ok["ok"]
