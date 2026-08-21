"""synth/book — the transplanted market side.

`measure()` runs against the live db and is exercised by the research driver, not here.
What this file pins is everything that decides whether a transplanted book is a book at
all: that pricing and settlement cannot disagree, that an open tail is assumed rather than
renormalised away, that a donor's ladder survives being snapped onto a coarser grid without
producing duplicate contracts, that a target's replay day is paired with the donor day at
the same distance from close rather than the same index, and that a candle lands on exactly
the timestamp `pnl_score.entry_days` will come looking for.

The recurring theme is that every one of these fails QUIETLY. A pricing/settlement
mismatch shows up as a small persistent edge at the money, which reads as skill. A dropped
tail reads as a sharper market. A duplicated leg reads as a wider ladder. An hour-off
candle reads as one fewer trading day. None of them raise, and all of them would change the
parameter ranking these worlds exist to produce.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.research.synth import book as B
from prediction_market_macro.research.synth import worlds as W

UTC = timezone.utc


def _day(dtc, z_m=0.0, r=1.0, hs=0.01, shape=None, tail=0.0):
    if shape is None:                       # a symmetric 3-point standardized book
        shape = [(-1.0, 0.25), (0.0, 0.5), (1.0, 0.25)]
    return B.DonorDay(dtc=dtc, z_m=z_m, r=r, half_spread=hs, shape=shape, tail_mass=tail)


def _donor(series="KXJOBLESSCLAIMS", tok="26JUN11", z_y=0.0, days=None, offsets=None):
    return B.Donor(series=series, tok=tok, z_y=z_y,
                   days=days if days is not None else [_day(d) for d in (6, 4, 2, 0.5)],
                   offsets=offsets if offsets is not None else
                   [{"strike_type": "greater", "floor_off": o, "cap_off": None}
                    for o in (-1.0, 0.0, 1.0)],
                   n_legs=3)


# ── pinned moments ───────────────────────────────────────────────────────────
def test_pinned_moments_pins_open_tails_one_step_out_and_reports_the_mass():
    """`devig.ladder_implied` keys the outermost buckets +/-inf. Dropping them would
    renormalise the market away from its own tails — free sharpness, in the direction that
    flatters us — so they are pinned one round_rule step past the outermost finite key and
    the assumed mass is returned rather than absorbed."""
    pmf = {-math.inf: 0.1, 1.0: 0.2, 2.0: 0.4, 3.0: 0.2, math.inf: 0.1}
    m, sd, tail = B.pinned_moments(pmf, step=1.0)
    # tails land at 0.0 and 4.0; the book is symmetric about 2.0
    assert m == pytest.approx(2.0)
    assert sd == pytest.approx(math.sqrt(0.1 * 4 + 0.2 * 1 + 0 + 0.2 * 1 + 0.1 * 4))
    assert tail == pytest.approx(0.2)


def test_pinned_moments_dropping_the_tail_would_have_understated_the_sd():
    """Stated as a comparison because the failure mode is not an exception, it is a
    market that looks more confident than it was."""
    pmf = {-math.inf: 0.25, 1.0: 0.5, math.inf: 0.25}
    _m, sd_pinned, _t = B.pinned_moments(pmf, step=1.0)
    _m2, sd_dropped, _t2 = B.pinned_moments({1.0: 0.5}, step=1.0)
    assert sd_pinned > sd_dropped == 0.0


def test_pinned_moments_refuses_an_all_infinite_or_empty_book():
    assert B.pinned_moments({}, 1.0) == (None, None, None)
    assert B.pinned_moments({math.inf: 1.0}, 1.0) == (None, None, None)


# ── pricing must equal settlement ────────────────────────────────────────────
@pytest.mark.parametrize("strict_gt", [True, False])
@pytest.mark.parametrize("st,floor,cap", [("greater", 3.0, None),
                                          ("greater_or_equal", 3.0, None),
                                          ("less", None, 3.0),
                                          ("between", 2.0, 3.0)])
def test_leg_prob_equals_settlement_mass_on_every_strike_type(st, floor, cap, strict_gt):
    """The invariant the whole module rests on: the probability quoted for a leg is the
    mass of outcomes that leg would actually be PAID on. A leg priced `>=` and settled `>`
    is a free edge at exactly the money, which is where the size goes."""
    leg = {"ticker": "T", "strike_type": st, "floor_strike": floor, "cap_strike": cap}
    pmf = {1.0: 0.1, 2.0: 0.2, 3.0: 0.4, 4.0: 0.2, 5.0: 0.1}
    expected = sum(v for x, v in pmf.items()
                   if W.settle_leg(leg, x, strict_gt) == "yes")
    assert B._leg_prob(pmf, leg, strict_gt) == pytest.approx(expected)


def test_leg_prob_moves_with_strict_gt_at_the_money():
    """Not merely 'consistent': the at-the-money mass has to actually change hands, or the
    parametrized test above would pass on an implementation that ignored `strict_gt`."""
    leg = {"ticker": "T", "strike_type": "greater", "floor_strike": 3.0, "cap_strike": None}
    pmf = {3.0: 0.5, 4.0: 0.5}
    assert B._leg_prob(pmf, leg, True) == pytest.approx(0.5)
    assert B._leg_prob(pmf, leg, False) == pytest.approx(1.0)


def test_leg_prob_refuses_a_custom_leg_rather_than_pricing_it_at_zero():
    """Inherited from `settle_leg`, and load-bearing: a silent 0.0 would be quoted as a
    1c bid on a contract nobody can settle."""
    with pytest.raises(ValueError):
        B._leg_prob({1.0: 1.0}, {"ticker": "T", "strike_type": "custom"}, True)


# ── the ladder ───────────────────────────────────────────────────────────────
def test_build_ladder_dedupes_strikes_that_collapse_onto_the_target_grid():
    """A donor quoted on a finer grid than the target's round_rule can put two strikes on
    one snapped value. Two legs with identical bounds are the same contract twice, and
    `enumerate_structs` would build a guaranteed-zero-payoff spread between them."""
    donor = _donor(offsets=[{"strike_type": "greater", "floor_off": o, "cap_off": None}
                            for o in (0.0, 0.001, 0.002, 1.0)])
    spec = REGISTRY["KXJOBLESSCLAIMS"]
    legs = B.build_ladder(donor.offsets, "KXJOBLESSCLAIMS", "26JUN11", m_mean=220_000.0,
                          m_sd=10_000.0)
    assert len(legs) == 2, "three near-identical offsets should collapse to one strike"
    assert len({l["ticker"] for l in legs}) == len(legs)
    for l in legs:
        assert l["floor_strike"] % spec.round_rule == 0


def test_build_ladder_keeps_strike_type_because_it_is_the_settlement_rule():
    """KXWTIW partitions its line with `between` buckets; every other series quotes a
    `greater` survival ladder. Re-expressing one as the other changes what the legs pay."""
    donor = _donor(offsets=[{"strike_type": "between", "floor_off": -1.0, "cap_off": 0.0},
                            {"strike_type": "between", "floor_off": 0.0, "cap_off": 1.0}])
    legs = B.build_ladder(donor.offsets, "KXWTIW", "26AUG21", m_mean=70.0, m_sd=2.0)
    assert [l["strike_type"] for l in legs] == ["between", "between"]
    assert all(l["floor_strike"] is not None and l["cap_strike"] is not None for l in legs)


# ── donor days pair by time to close, not by index ───────────────────────────
def test_day_at_matches_on_days_to_close_not_position():
    """Replay grids differ in length between donor and target — a 7-day window holds 6 or
    7 quoted days. `dtc` is the only coordinate on which the two are commensurable;
    matching by index would pair a target's opening day against a donor day deep into its
    convergence, which is precisely where the market got tight and confident."""
    donor = _donor(days=[_day(6.0, z_m=0.6), _day(3.0, z_m=0.3), _day(0.2, z_m=0.02)])
    assert donor.day_at(5.9).z_m == pytest.approx(0.6)
    assert donor.day_at(0.0).z_m == pytest.approx(0.02)
    assert donor.day_at(3.4).z_m == pytest.approx(0.3)


# ── drawing ──────────────────────────────────────────────────────────────────
def test_draw_is_uniform_among_the_k_nearest_in_z_y():
    """Not the single nearest: with 75 donors the nearest would be reused across many
    synthetic events and its idiosyncrasies would become a systematic feature of the
    sample rather than a draw from it."""
    donors = [_donor(tok=f"E{i}", z_y=float(i), days=[_day(3.0, z_m=float(i))])
              for i in range(10)]
    rng = np.random.default_rng(0)
    picked = {B.draw(donors, 0.0, rng, k=3).tok for _ in range(200)}
    assert picked == {"E0", "E1", "E2"}


def test_draw_excludes_a_series_when_asked_and_refuses_an_empty_pool():
    donors = [_donor(series="KXWTIW", tok="A", z_y=0.0, days=[_day(3.0, z_m=0.0)]),
              _donor(series="KXNATGASW", tok="B", z_y=5.0, days=[_day(3.0, z_m=1.0)])]
    assert B.draw(donors, 0.0, np.random.default_rng(0), slope=0.0,
                  exclude_series="KXWTIW").series == "KXNATGASW"
    with pytest.raises(ValueError):
        B.draw([], 0.0, np.random.default_rng(0))


def test_zm_slope_recovers_a_planted_regression():
    donors = [_donor(tok=f"E{i}", z_y=z, days=[_day(3.0, z_m=0.4 + 0.5 * z)])
              for i, z in enumerate(np.linspace(-2, 2, 9))]
    assert B.zm_slope(donors) == pytest.approx(0.5)


def test_draw_realigns_the_donor_onto_the_targets_own_z_y():
    """k=10 buys donor diversity with a z_y mismatch, and carried through untouched that
    mismatch attenuates the +0.57 dependence and tilts the whole synthetic market low.
    The shift evaluates the donor's own regression at the target's condition."""
    donors = [_donor(tok=f"E{i}", z_y=z, days=[_day(3.0, z_m=0.4 + 0.5 * z)])
              for i, z in enumerate(np.linspace(-2, 2, 9))]
    got = B.draw(donors, 1.0, np.random.default_rng(0), k=1)
    assert got.z_y == pytest.approx(1.0)
    assert got.days[0].z_m == pytest.approx(0.4 + 0.5 * 1.0)


def test_draw_realignment_carries_the_donors_own_residual_unchanged():
    """The shift must not manufacture a dependence tighter than the measured one: a donor
    that sat 0.7 above its own regression line must still sit 0.7 above it afterwards, or
    the synthetic market would be a deterministic function of the outcome and the strategy
    would be scored against a counterparty that already knows the answer."""
    odd = _donor(tok="ODD", z_y=0.0, days=[_day(3.0, z_m=0.7)])   # +0.7 off the line
    got = B.draw([odd], 2.0, np.random.default_rng(0), k=1, slope=0.5)
    assert got.tok == "ODD"
    assert got.days[0].z_m == pytest.approx(0.5 * 2.0 + 0.7)


def test_draw_ladder_takes_geometry_from_the_target_series_only():
    """Ladder geometry is a VENUE fact about a series; the market's view is what pools
    across them. Mixing the two put a 40-leg KXNATGASW ladder on a claims event, pinned
    49% of quotes at the band against 20% on the real book, and collapsed the delivered
    dependence to +0.26 from the +0.57 the donors carry."""
    donors = [_donor(series="KXNATGASW", tok="N", z_y=0.0,
                     offsets=[{"strike_type": "between", "floor_off": -1.0,
                               "cap_off": 1.0}]),
              _donor(series="KXJOBLESSCLAIMS", tok="J", z_y=0.0)]
    got = B.draw_ladder(donors, "KXJOBLESSCLAIMS", np.random.default_rng(0))
    assert [o["strike_type"] for o in got] == ["greater"] * 3


def test_draw_ladder_refuses_a_series_whose_geometry_was_never_observed():
    """Inventing a ladder would be fabricating the market's structure, not transplanting
    it — and it would do so silently, on the series where we have least evidence."""
    with pytest.raises(ValueError, match="never been observed"):
        B.draw_ladder([_donor(series="KXWTIW")], "KXGDP", np.random.default_rng(0))


def test_coverage_flags_synthetic_outcomes_that_no_donor_reaches():
    """The S4 gate. If the incumbent's P_sd on synthetic worlds differs systematically
    from its P_sd on real ones, every z_y* lands beyond the pool and each 'transplant' is
    an extrapolation wearing a transplant's clothes."""
    donors = [_donor(tok=f"E{i}", z_y=z) for i, z in enumerate((-1.0, 0.0, 1.0))]
    inside = B.coverage(donors, [-0.4, 0.1, 0.9])
    assert inside["outside"] == 0.0
    outside = B.coverage(donors, [8.0, 9.0])
    assert outside["outside"] == 1.0
    assert outside["donor_range"] == [-1.0, 1.0]
    assert outside["median_gap"] == pytest.approx(7.5)


# ── serialisation ────────────────────────────────────────────────────────────
def test_save_load_round_trip_keeps_shape_points_as_pairs(tmp_path):
    """JSON has no tuples. `_mapped_pmf` unpacks each shape point as a 2-tuple, so a list
    coming back would raise there rather than here — a long way from the cause."""
    donors = [_donor(tok="A", z_y=0.3), _donor(tok="B", z_y=-1.2, series="KXWTIW")]
    back = B.load(B.save(donors, tmp_path / "d.json"))
    assert [d.tok for d in back] == ["A", "B"]
    assert back[0].days[0].shape == donors[0].days[0].shape
    assert isinstance(back[0].days[0].shape[0], tuple)
    assert back[1].series == "KXWTIW"


# ── quoting ──────────────────────────────────────────────────────────────────
CLOSE = datetime(2026, 6, 11, 14, 30, tzinfo=UTC)


def _per_day(n=4, close=CLOSE, sd=10_000.0):
    """Replay days as `pnl_score.entry_days` produces them: 16:00 UTC, all BEFORE close."""
    return [((close - timedelta(days=d)).replace(hour=16, minute=0), 220_000.0, sd)
            for d in range(n, 0, -1)]


def test_quote_stamps_one_candle_per_replay_day_at_exactly_the_asof():
    """`_candle_quote` takes the last candle at or before the asof. A stamp an hour late
    silently drops that day's quote and the event just looks like it had fewer trading
    days — no error, one less entry, a different PnL."""
    donor = _donor()
    per_day = _per_day()
    legs = B.build_ladder(donor.offsets, "KXJOBLESSCLAIMS", "26JUN11", 220_000.0, 10_000.0)
    book = B.quote(donor, legs, "KXJOBLESSCLAIMS", per_day, CLOSE)
    want = [int(a.timestamp()) for a, _m, _s in per_day]
    for tk, rows in book.items():
        assert [r[0] for r in rows] == want, tk


def test_quote_measures_days_to_close_from_the_close_not_the_last_replay_day():
    """`entry_days` stops at 16:00 UTC the day before the close, so inferring the close
    from `per_day` would put every target's last day at dtc=0 and pair it against donor
    days that were still most of a day out. Days-to-close is the coordinate the whole
    transplant aligns on; both sides have to measure it from the same origin."""
    donor = _donor(days=[_day(6.0, z_m=2.0), _day(0.0, z_m=-2.0)])
    legs = [{"ticker": "ATM", "strike_type": "greater",
             "floor_strike": 220_000.0, "cap_strike": None}]
    one = _per_day(n=1)                       # a single day, 22.5h before the close
    near = B.quote(donor, legs, "KXJOBLESSCLAIMS", one, CLOSE)["ATM"][0]
    far = B.quote(donor, legs, "KXJOBLESSCLAIMS", one,
                  CLOSE + timedelta(days=6))["ATM"][0]
    assert near[1] < far[1], "the same replay day must pair with different donor days"


def test_quote_prices_a_ladder_monotonically_and_inside_the_tradable_band():
    """A `greater` ladder must be non-increasing in strike or it is arbitrageable on its
    own face, and Kalshi shows whole cents in [0.01, 0.99]. The legs are re-sorted by
    strike first because `_legs_at` returns them in no particular order, so a donor's
    `offsets` carry that arbitrary order across — real, and not a defect."""
    donor = _donor(offsets=[{"strike_type": "greater", "floor_off": o, "cap_off": None}
                            for o in (1.0, -2.0, 2.0, 0.0, -1.0)])
    legs = B.build_ladder(donor.offsets, "KXJOBLESSCLAIMS", "26JUN11", 220_000.0, 10_000.0)
    book = B.quote(donor, legs, "KXJOBLESSCLAIMS", _per_day(), CLOSE)
    by_strike = sorted(legs, key=lambda l: l["floor_strike"])
    mids = [0.5 * (book[l["ticker"]][0][1] + book[l["ticker"]][0][2]) for l in by_strike]
    assert mids == sorted(mids, reverse=True), mids
    for rows in book.values():
        for _ts, bid, ask in rows:
            assert B.MIN_PRICE <= bid <= ask <= B.MAX_PRICE


def test_quote_moves_the_book_with_z_m_in_the_direction_of_the_donor():
    """The transplant's whole point. A donor sitting above our mean must put the synthetic
    market above the synthetic model's mean too — that is the +0.70 dependence the sample
    exists to reproduce, and a sign error here would price a market that leans away from
    the truth while every diagnostic still looked healthy."""
    legs = [{"ticker": "ATM", "strike_type": "greater",
             "floor_strike": 220_000.0, "cap_strike": None}]
    hi = B.quote(_donor(days=[_day(d, z_m=+1.0) for d in (4, 3, 2, 1)]), legs,
                 "KXJOBLESSCLAIMS", _per_day(), CLOSE)["ATM"][0]
    lo = B.quote(_donor(days=[_day(d, z_m=-1.0) for d in (4, 3, 2, 1)]), legs,
                 "KXJOBLESSCLAIMS", _per_day(), CLOSE)["ATM"][0]
    assert hi[1] > lo[1] and hi[2] > lo[2]


def test_quote_widens_the_book_with_r():
    """`r` carries how wide the market was relative to us. A donor twice our width must
    price the at-the-money wings richer, or the synthetic market would be uniformly
    sharper than the real one and the strategy would show edge it never had."""
    legs = [{"ticker": "WING", "strike_type": "greater",
             "floor_strike": 240_000.0, "cap_strike": None}]
    wide = B.quote(_donor(days=[_day(d, r=3.0) for d in (4, 3, 2, 1)]), legs,
                   "KXJOBLESSCLAIMS", _per_day(), CLOSE)["WING"][0]
    tight = B.quote(_donor(days=[_day(d, r=0.5) for d in (4, 3, 2, 1)]), legs,
                    "KXJOBLESSCLAIMS", _per_day(), CLOSE)["WING"][0]
    assert wide[1] > tight[1]


def test_quote_charges_the_donors_half_spread():
    donor = _donor(days=[_day(d, hs=0.04) for d in (4, 3, 2, 1)])
    legs = [{"ticker": "ATM", "strike_type": "greater",
             "floor_strike": 220_000.0, "cap_strike": None}]
    _ts, bid, ask = B.quote(donor, legs, "KXJOBLESSCLAIMS", _per_day(),
                            CLOSE)["ATM"][0]
    assert ask - bid == pytest.approx(0.08, abs=1e-9)


def test_quote_output_is_the_book_shape_write_event_consumes(tmp_path):
    """The two halves of a synthetic event are written by different modules; this pins
    that `quote` emits exactly what `worlds.write_event` takes, so a shape change breaks
    here rather than as an sqlite type error deep in a generation run."""
    from prediction_market_macro.ingest.store import init_db
    src = init_db(tmp_path / "src.db")
    dst = W.materialize(src, tmp_path / "w.db", datetime(2026, 6, 1, tzinfo=UTC))
    donor = _donor()
    legs = B.build_ladder(donor.offsets, "KXJOBLESSCLAIMS", "26JUN11", 220_000.0, 10_000.0)
    book = B.quote(donor, legs, "KXJOBLESSCLAIMS", _per_day(), CLOSE)
    W.write_event(dst, W.EventPlan(series="KXJOBLESSCLAIMS", period="26JUN11", legs=legs,
                                   close_time=CLOSE, outcome=225_000.0, book=book))
    back = W.read_event(dst, "KXJOBLESSCLAIMS", "26JUN11")
    assert back.outcome == pytest.approx(225_000.0)
    assert {l["ticker"] for l in back.legs} == {l["ticker"] for l in legs}
    dst.close()
    src.close()
