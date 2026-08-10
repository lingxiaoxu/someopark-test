"""The backtest's `staked` must be capital at risk on production's definition.

`decide()` books `size_usd = count * fill_cost(count)`, and `Struct.fill_cost` returns the
NET debit on a bucket — `sum(px) - 1` — because one of the two legs pays in every branch.
`walkforward` recorded `sum(l.price for l in legs) * count` instead, i.e. the gross leg
notional, which overstates a bucket by ~$1 per contract.

That number is not cosmetic. It is:

  * the ROI DENOMINATOR. Realised pnl is unaffected, so an inflated stake makes a losing
    backtest look less lossy. On the displayed 75-day window (`d75:model:end2026-07-31`)
    six bucket trades booked $22.53 against $4.53 of real risk, and the headline hybrid ROI
    read **-9.17%** where the honest figure is **-13.21%**.
  * the input to the simulated risk limits (`_sim_risk_veto` per-cluster / gross /
    per-release-day), so the harness was also rejecting bucket trades production would
    have allowed.

A single is unaffected — `fill_cost` is the sum of its one leg — which is why this hid:
every series that quotes plain ladder legs agreed, and only the `between` books (KXWTIW,
natgas, the claims buckets) diverged.
"""
from __future__ import annotations

import inspect

from prediction_market_macro.research import walkforward
from prediction_market_macro.strategy.decision import GATES, decide
from prediction_market_macro.strategy.edge import Leg, Struct

DEPTH = 1e9


def _bucket(lo_px: float, hi_px: float, fair: float) -> Struct:
    """A two-leg bucket: buy the lower strike YES, sell coverage via the upper NO.

    Cost is the net debit, matching `enumerate_structs`.
    """
    legs = (Leg("T-LO", "yes", lo_px, DEPTH), Leg("T-HI", "no", hi_px, DEPTH))
    cost = lo_px + hi_px - 1.0
    return Struct("bucket", legs, fair=fair, cost=cost, max_loss=cost, desc="BUCKET (a,b]")


def _single(px: float, fair: float) -> Struct:
    return Struct("single", (Leg("T-1", "yes", px, DEPTH),), fair=fair, cost=px,
                  max_loss=px, desc="YES T-1")


def test_gross_notional_overstates_a_bucket_by_a_dollar_per_contract():
    st = _bucket(0.55, 0.54, fair=0.30)          # net debit 0.09, gross 1.09
    count = 11
    gross = sum(l.price for l in st.legs) * count
    assert round(st.fill_cost(count) * count, 4) == 0.99
    assert round(gross, 4) == 11.99
    assert gross - st.fill_cost(count) * count == count * 1.0


def test_a_single_is_unaffected_which_is_why_this_hid():
    st = _single(0.20, fair=0.34)
    for count in (1, 5, 13):
        assert st.fill_cost(count) * count == sum(l.price for l in st.legs) * count


def test_harness_books_the_same_stake_production_does():
    """`decide().size_usd` and the harness's `staked` must agree on the same structure."""
    st = _bucket(0.55, 0.54, fair=0.30)
    d = decide([st], now=None, close_time=None, release_ts=None,
               market_implied={st.desc: st.cost}, already_open=False,
               bankroll=100.0, gates=GATES)
    assert d.action == "open"
    assert d.size_usd == round(st.fill_cost(d.count) * d.count, 2)


def test_the_call_site_uses_fill_cost_not_a_leg_sum():
    """Pinned as source text: the wrong expression runs fine and only shifts a ratio.

    Nothing raises, no test of pnl moves, and the only symptom is a headline ROI that is
    too kind — exactly the failure mode that survived into a published number once.
    """
    src = inspect.getsource(walkforward)
    body = src[src.index("def _trade_row"):]
    row = body[:body.index("return {") + body[body.index("return {"):].index("}")]
    assert '"staked": round(st.fill_cost(count) * count, 4)' in row
    assert "sum(l.price for l in st.legs) * count" not in row
