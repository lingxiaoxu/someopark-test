import pytest
"""Research replay invariants: no foresight fills and no missing inventory loss."""
from crypto_trading.crypto_strategies.event_binary import complete_set as cs
from crypto_trading.crypto_strategies.event_binary.research_complete_set import (
    normalize_trade,replay_prints,sequential_taker,simultaneous_pair,summarize)


def book(yb=.49,nb=.49,size=1.):
    return cs.normalize_book({"orderbook_fp":{"yes_dollars":[[str(yb),str(size)]],
                                              "no_dollars":[[str(nb),str(size)]]}})


def row(ts,b):
    return dict(ts=ts,close=900.,ticker="KXBTC15M-test",series="KXBTC15M",book=b)


def test_taker_complete_set_cost_cannot_ignore_spread_or_either_fee():
    result=simultaneous_pair(book(size=25),25,.07)
    assert result["cost_before_fees"]==1.02
    assert result["all_in_per_pair"]>1.05
    assert simultaneous_pair(book(size=1),25,.07) is None


def test_sequential_unpaired_loss_is_included_and_pairs_reconcile():
    rows=[row(20,book(.59,.39,10)),row(120,book(.59,.39,10))]
    lose=sequential_taker(rows,"no")
    assert lose["paired_quantity"]==0
    assert lose["net_usd"]< -3
    assert lose["residual_quantity"]==5
    paired=sequential_taker(rows+[row(220,book(.69,.29,10))],"no")
    paired_other_outcome=sequential_taker(rows+[row(220,book(.69,.29,10))],"yes")
    assert paired["paired_quantity"]==5
    assert paired["net_usd"]>0
    assert abs(paired["net_usd"]-paired_other_outcome["net_usd"])<1e-8
    s=summarize([lose,paired])
    assert s["markets_with_unpaired_inventory"]==1
    assert abs(s["net_usd"]-s["paired_net_usd"]-s["residual_net_usd"])<1e-8


def test_public_print_replay_cannot_fill_before_order_existed():
    # v3 quotes fresh inventory only on the favored side; make YES favored
    # (mid .52) so the replayed order exists on the side the prints hit. The
    # v4 entry band is widened here: this test is about fill CAUSALITY.
    from dataclasses import replace as _r
    # ts 300/400 against a 900 close = 10min/8.3min left: inside the v7 entry
    # window, so an order exists for the prints to hit
    rows=[row(300,book(.51,.47)),row(400,book(.51,.47))]
    pre=[dict(trade_id="before",ticker="KXBTC15M-test",ts=299.,quantity=100.,
              yes_price=.49,taker_side="no",is_block_trade=False)]
    # v8 sets the live maker coefficient to 0 (the venue verifies fee_type=
    # quadratic hourly). This test guards the v1 regression where maker fills
    # were booked fee-FREE while the schedule charged for them, so it pins a
    # fee-bearing coefficient rather than tracking the live one.
    wide=_r(cs.Parameters(),entry_band_lo=.01,entry_band_hi=.99,maker_coefficient=.0175)
    result=replay_prints(rows,pre,"yes",wide,residual=False)
    assert result["quantity"]==0
    assert result["net_usd"]==0
    future=pre+[dict(trade_id="after",ticker="KXBTC15M-test",ts=402.,quantity=6.,
                    yes_price=.49,taker_side="no",is_block_trade=False)]
    traded=replay_prints(rows,future,"no",wide,residual=False)
    assert traded["quantity"]==5
    # loses the whole basis when "no" settles: payout 0, so net is exactly
    # -(cost+fees), fee-bearing since v2 (v1 booked maker fills fee-free)
    assert traded["net_usd"]==pytest.approx(
        -(traded["cost_usd"]+traded["fees_usd"])) and traded["fees_usd"]>0
    assert traded["residual_quantity"]==5


def test_parser_prefers_current_side_and_preserves_fractional_quantity():
    result=normalize_trade(dict(trade_id="x",ticker="KXBTC15M-test",created_time="2026-09-10T12:00:00Z",
        count_fp="3.11",yes_price_dollars="0.9010",taker_outcome_side="no",taker_side="yes",is_block_trade=True))
    assert result["quantity"]==3.11 and result["taker_side"]=="no"
    assert result["is_block_trade"]
