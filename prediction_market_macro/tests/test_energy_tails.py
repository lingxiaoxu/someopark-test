"""energy/0.5.0 — empirical innovations, and the two things measurement forced me to
put BACK.

KXAAAGASW was the worst-calibrated series in the book (Brier 0.14235 against the
market's 0.01709) and #743 was its worst open: N(4.129, 0.0856) called P(AAA > 4.20)
= 20.4% where the market said 1% and the print came in at 4.095. The plan blamed the
normal's thin tails. That diagnosis was wrong twice over, and these tests pin the
corrected version:

  1. 4.20 sat 0.83 sigma from the mean — the BODY. Swapping normal -> fat-tailed moves
     that probability by half a point (0.204 -> 0.207). The tail shape was never what
     priced this bet, so `test_shape_swap_does_not_move_the_body` pins that a shape
     change is a shape change: it must NOT quietly rescale anything.
  2. The fat-tail fix is real, but it pays on WTI/NG, where the wings are the product:
     measured CL kurtosis 9.3 and NG 37.1, and at +3 sigma the bootstrap prices 1.09%
     against the normal's 0.12%.

Separately, two corrections I made and had to revert after measuring, both pinned here
so nobody (me included) "fixes" them again:
  - the 1.5x proxy inflation in the cold-start branch is CORRECT (LOO error 0.0862 vs
    1.5 * sig_w = 0.0857); dropping it makes that branch 3.5x overconfident;
  - the settled-event drift regression beats a full-history AR(1) (LOO 0.0862 vs
    0.1196), because it carries a forward-looking RBOB term the AR(1) cannot see.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import energy
from prediction_market_macro.model.common import Empirical, grid_pmf, leg_fair

ASOF = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _fat_pool(n: int = 2000, seed: int = 7) -> np.ndarray:
    """Innovations with the kurtosis energy actually shows (~9), not a normal's 3.

    Routed through _innovation_pool because _bootstrap_z assumes a CENTERED pool — an
    earlier draft of this fixture handed over the raw draws and the median-offset
    assertion below caught it, which is exactly what it is there for.
    """
    rng = np.random.default_rng(seed)
    return energy._innovation_pool(rng.standard_t(5, size=n) * 0.01)


# ── the bootstrap primitive ─────────────────────────────────────────────────
def test_shape_swap_does_not_move_the_body():
    """_bootstrap_z must come back at MAD-sigma 1 exactly like a standard normal.

    This is the whole basis for claiming a before/after difference is "shape". If the
    bootstrap also widened the distribution, every comparison in the plan doc would be
    confounded and the WTI wing numbers would mean nothing.
    """
    rng = np.random.default_rng(0)
    z = energy._bootstrap_z(rng, _fat_pool(), steps=1, n=60_000)
    assert energy._mad_sigma(z) == pytest.approx(1.0, abs=1e-9)
    assert abs(float(np.median(z))) < 0.03


def test_bootstrap_is_fatter_than_the_normal_it_replaces():
    """Same body, heavier wings — the actual payload of energy/0.5.0."""
    rng = np.random.default_rng(0)
    z = energy._bootstrap_z(rng, _fat_pool(), steps=1, n=200_000)
    norm = rng.standard_normal(200_000)
    assert energy._mad_sigma(norm) == pytest.approx(1.0, abs=0.02)
    # at 3 MAD-sigma the fat pool must price a wing the normal calls impossible
    p_boot = float(np.mean(z > 3.0))
    p_norm = float(np.mean(norm > 3.0))
    assert p_boot > 3 * p_norm, (p_boot, p_norm)
    # ...while the quartile is untouched
    assert np.quantile(z, 0.75) == pytest.approx(np.quantile(norm, 0.75), abs=0.05)


def test_summing_steps_thins_the_tail_the_way_aggregation_really_does():
    """A 5-day move is less fat-tailed than a 1-day move; scaling ONE draw by sqrt(5)
    would keep the daily kurtosis and overprice a week-out wing."""
    rng = np.random.default_rng(0)
    pool = _fat_pool(4000)
    k1 = float(np.mean((energy._bootstrap_z(rng, pool, 1, 100_000)) ** 4))
    k5 = float(np.mean((energy._bootstrap_z(rng, pool, 5, 100_000)) ** 4))
    assert k5 < k1, (k1, k5)          # both already MAD-normalised, so this is shape


def test_innovation_pool_refuses_a_sample_too_thin_to_bootstrap():
    assert energy._innovation_pool(np.arange(10, dtype=float)) is None
    assert energy._innovation_pool(np.array([])) is None
    pool = energy._innovation_pool(_fat_pool(500))
    assert pool is not None and len(pool) == 500
    assert abs(float(np.median(pool))) < 1e-12        # centered


def test_thin_pool_falls_back_to_the_normal_rather_than_bootstrapping_noise():
    dist, shape = energy._emp_dist(4.0, 0.05, None, 1.0, np.random.default_rng(0))
    assert shape == "normal_fallback"
    assert dist.to_json()["kind"] == "gmix"


# ── the Empirical dist carrying it ──────────────────────────────────────────
def test_empirical_survives_serialisation_finely_enough_to_price_a_wing():
    """201 quantiles quantised a fair value at half a Kalshi tick, which is useless
    for the 1-2% legs these models trade. 1001 gives a tenth of a tick."""
    rng = np.random.default_rng(0)
    d = Empirical(tuple((4.0 + 0.05 * rng.standard_t(5, 20_000)).tolist()))
    back = Empirical(tuple(d.to_json()["quantiles"]))
    for q in (0.001, 0.01, 0.10, 0.5, 0.90, 0.99, 0.999):
        assert back.quantile(q) == pytest.approx(d.quantile(q), abs=0.004)
    x = d.quantile(0.99)
    assert back.cdf(x) == pytest.approx(d.cdf(x), abs=0.002)


def test_grid_pmf_span_survives_a_fat_tailed_sample():
    """max-min would size the grid off the single most extreme draw; at a $0.001
    round_rule that is tens of thousands of points. Quantile span keeps it finite
    while still carrying >99.9% of the mass (grid_pmf asserts it)."""
    rng = np.random.default_rng(0)
    d = Empirical(tuple((2.66 * np.exp(0.03 * rng.standard_t(3, 20_000))).tolist()))
    pmf = grid_pmf(d, 0.001)
    assert sum(pmf.values()) == pytest.approx(1.0, abs=1e-9)
    assert len(pmf) < 60_000
    assert leg_fair(pmf, "greater", d.quantile(0.5), None) == pytest.approx(0.5, abs=0.01)


# ── the AAA branches ────────────────────────────────────────────────────────
def _seed_gas(conn, n_weeks: int = 400, seed: int = 3):
    """Weekly GASREGW with genuine momentum (b~0.55) and fat weekly innovations."""
    rng = np.random.default_rng(seed)
    lvl, d = 3.50, 0.0
    start = ASOF - timedelta(weeks=n_weeks)
    for i in range(n_weeks):
        day = start + timedelta(weeks=i)
        d = 0.55 * d + 0.02 * rng.standard_t(5)
        lvl = max(lvl + d, 1.0)
        kt = day + timedelta(hours=22)
        conn.execute("INSERT OR IGNORE INTO fred_obs VALUES(?,?,?,?,?,?)",
                     ("GASREGW", day.date().isoformat(), round(lvl, 3),
                      kt.date().isoformat(), kt.isoformat(), kt.isoformat()))
    conn.commit()
    return lvl


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def test_cold_start_keeps_the_measured_proxy_inflation(conn):
    """The 1.5x is load-bearing, not decoration. I removed it as "double counting"
    and the measurement put it straight back: the AAA forecast error is the gas move's
    unpredictable part PLUS the AAA-vs-GASREGW gap, and that sum is ~1.5x the
    unconditional weekly sigma (0.0862 measured vs 0.0857 implied)."""
    _seed_gas(conn)
    p = energy.predict(conn, ASOF, (ASOF + timedelta(days=7)).date().isoformat(),
                       "KXAAAGASW")
    assert p.inputs["mode"] == "damped_trend_fallback"     # no settled events seeded
    s, _h = energy.FeatureStore(conn).fred_series("GASREGW", ASOF)
    dw = s.diff().dropna()
    sig_w = max(energy._mad_sigma(dw.tail(52).values), 0.01)
    weeks = p.inputs["weeks"]
    # inputs stores sigma rounded to 4dp for the evidence chain
    assert p.inputs["sigma"] == pytest.approx(sig_w * 1.5 * math.sqrt(weeks), abs=5e-5)
    # and the gas-move-only sigma would have been far tighter — the trap I fell into
    assert energy._mad_sigma(energy._gas_ar1(dw)[2]) < 0.6 * p.inputs["sigma"]


def test_cold_start_still_bootstraps_its_shape(conn):
    _seed_gas(conn)
    p = energy.predict(conn, ASOF, (ASOF + timedelta(days=7)).date().isoformat(),
                       "KXAAAGASW")
    assert p.inputs["shape"].startswith("bootstrap(")
    assert p.dist.to_json()["kind"] == "empirical"
    # body pinned to the branch's sigma; only the wings differ from a normal
    assert energy._mad_sigma(np.asarray(p.dist.samples)) == pytest.approx(
        p.inputs["sigma"], rel=0.02)


def test_gas_ar1_is_momentum_and_cannot_go_explosive(conn):
    _seed_gas(conn)
    s, _h = energy.FeatureStore(conn).fred_series("GASREGW", ASOF)
    a, b, pool = energy._gas_ar1(s.diff().dropna())
    assert 0.0 <= b <= 0.9                       # clipped, never a divergent path
    assert b > 0.3                               # gasoline weeks trend, they do not revert
    assert len(pool) >= energy._MIN_POOL


def test_gas_ar1_declines_to_fit_a_short_history(conn):
    _seed_gas(conn, n_weeks=40)
    s, _h = energy.FeatureStore(conn).fred_series("GASREGW", ASOF)
    assert energy._gas_ar1(s.diff().dropna()) is None


def test_predict_is_pit_clean(conn):
    _seed_gas(conn)
    p = energy.predict(conn, ASOF, (ASOF + timedelta(days=7)).date().isoformat(),
                       "KXAAAGASW")
    assert p.data_horizon <= p.asof
    assert p.model_version == energy.VERSION


def test_predict_is_replay_stable(conn):
    """rng(0) per call: two runs of the same asof must be byte-identical or the
    replay canaries in health §9.6-3 fire on our own nondeterminism."""
    _seed_gas(conn)
    per = (ASOF + timedelta(days=7)).date().isoformat()
    a = energy.predict(conn, ASOF, per, "KXAAAGASW")
    b = energy.predict(conn, ASOF, per, "KXAAAGASW")
    assert a.dist.to_json() == b.dist.to_json()


def test_negative_wti_print_drops_out_instead_of_splicing_a_45pct_day():
    """CL 2020-04-20 settled at -37.63 — the one bar where a log-return does not exist.

    Both diffs touching it must vanish. The tempting alternative (drop the bar, then
    diff) would join 04-17 to 04-21 across the May expiry and add a single -45%
    innovation to a pool whose whole purpose is to describe realistic tails.
    """
    import numpy as np
    from prediction_market_macro.model.energy import _innovation_pool
    # 18.27 -> (-37.63) -> 10.01 are the real 2020-04-17/20/21 CL settles; the rest is a
    # smooth +0.5%/day tail, long enough to clear _MIN_POOL
    px = np.array([18.27, -37.63] + [10.01 * 1.005 ** i for i in range(150)])
    with np.errstate(invalid="ignore"):
        pool = _innovation_pool(np.diff(np.log(px)))
    assert pool is not None
    assert len(pool) == len(px) - 1 - 2                 # the two NaN diffs are gone
    assert np.all(np.isfinite(pool))
    # every surviving innovation is a 0.5% step, so the spliced 04-17 -> 04-21 return
    # (~-0.60) would stick far out of the range if it had been kept
    assert abs(np.log(10.01) - np.log(18.27)) > 0.55
    assert float(np.max(np.abs(pool))) < 0.01


# ── #192 / PR-12: fut_sigma_scale ──────────────────────────────────────────────
# The over-width at KXNATGASW survived hypothesis 1 (the vol floor: binds 1/21 NG events),
# hypothesis 2 (MAD/sd scale-shape mixing: real, but rank-inverted against the data) and
# hypothesis 3 (bracket-midpoint censoring artifact: refuted for NG specifically, which
# scores 21/21 with zero dropped events). What is left is a plain width defect and there
# was no key that could express one, so `fut_sigma_scale` exists. These tests pin the two
# properties that make it safe to add: at the default it changes NOTHING, and off the
# default it changes ONLY the width.


def _seed_fut(conn, root: str = "NG", n: int = 400, seed: int = 11, s0: float = 3.0):
    """Daily futures bars with fat innovations — enough to clear _MIN_POOL."""
    rng = np.random.default_rng(seed)
    px = s0
    start = ASOF - timedelta(days=n + 5)
    for i in range(n):
        day = start + timedelta(days=i)
        px = max(px * math.exp(0.02 * rng.standard_t(4)), 0.2)
        kt = day + timedelta(hours=20)
        conn.execute(
            "INSERT OR IGNORE INTO fut_daily(root, event_time, open, high, low, close,"
            " volume, knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,NULL,?,?)",
            (root, day.date().isoformat(), px, px, px, round(px, 4),
             kt.isoformat(), kt.isoformat()))
    conn.commit()


def test_fut_sigma_scale_defaults_to_a_byte_identical_no_op(conn):
    """1.0 must be a no-op to the BYTE, not merely to five decimals.

    A new key that shifts predictions by a rounding step is not a no-op: it would move
    every stored prediction on the day it lands, break health's replay canary against
    yesterday's rows, and make the before/after of whatever the grid later picks
    unreadable. `float(x) * 1.0` is exact in IEEE754, and this asserts we actually get
    that rather than something that merely looks like it.
    """
    _seed_fut(conn)
    per = (ASOF + timedelta(days=5)).date().isoformat()
    a = energy.predict(conn, ASOF, per, "KXNATGASW")
    b = energy.predict(conn, ASOF, per, "KXNATGASW", params={"fut_sigma_scale": 1.0})
    assert a.inputs == b.inputs
    assert a.dist.to_json() == b.dist.to_json()


@pytest.mark.parametrize("series,root", [("KXNATGASW", "NG"), ("KXWTIW", "CL")])
def test_fut_sigma_scale_is_live_on_both_futures_series(conn, series, root):
    """`param_space.live_keys` drops a key that moves nothing, so a key that is wired but
    invisible in `inputs` would be silently dropped from the grid and the search would
    quietly not happen. Both futures series must see it."""
    _seed_fut(conn, root=root, s0=3.0 if root == "NG" else 70.0)
    per = (ASOF + timedelta(days=5)).date().isoformat()
    a = energy.predict(conn, ASOF, per, series)
    b = energy.predict(conn, ASOF, per, series, params={"fut_sigma_scale": 0.5})
    assert a.inputs != b.inputs
    assert repr(a.dist) != repr(b.dist)


def test_fut_sigma_scale_scales_the_width_and_only_the_width(conn):
    """lambda must multiply sigma_h exactly and leave the anchor, the horizon and the
    shape alone — otherwise a grid rung is not the hypothesis it claims to test."""
    _seed_fut(conn)
    per = (ASOF + timedelta(days=5)).date().isoformat()
    a = energy.predict(conn, ASOF, per, "KXNATGASW")
    for lam in (0.7, 0.85, 1.2):
        b = energy.predict(conn, ASOF, per, "KXNATGASW",
                           params={"fut_sigma_scale": lam})
        assert b.inputs["sigma_h"] == pytest.approx(a.inputs["sigma_h"] * lam, rel=1e-4)
        for k in ("s0", "h_bdays", "shape", "root", "sigma_daily", "last_bar"):
            assert b.inputs[k] == a.inputs[k], k


def test_the_scale_is_applied_after_the_floor_not_before(conn):
    """If the scale sat on sigma_daily the floor would eat a narrowing on exactly the
    low-vol events, so a rung of 0.7 would mean 0.7 sometimes and 1.0 the rest of the
    time. Pinned by forcing a floor so high that it binds, then checking the scale still
    bites the full amount."""
    _seed_fut(conn)
    per = (ASOF + timedelta(days=5)).date().isoformat()
    hi = {"fut_min_sigma_daily": {"NG": 5.0, "CL": 5.0}}
    a = energy.predict(conn, ASOF, per, "KXNATGASW", params=hi)
    b = energy.predict(conn, ASOF, per, "KXNATGASW",
                       params={**hi, "fut_sigma_scale": 0.7})
    assert a.inputs["sigma_daily"] == b.inputs["sigma_daily"] == 5.0   # floor binds
    assert b.inputs["sigma_h"] == pytest.approx(a.inputs["sigma_h"] * 0.7, rel=1e-4)


def test_the_grid_ladder_can_widen_as_well_as_narrow():
    """A one-sided ladder cannot refute the hypothesis it is testing — it can only
    agree with it. The default must also be IN the list, or selection can never return
    'the current model'."""
    from prediction_market_macro.research.param_space import CANDIDATES
    _probe, vals = CANDIDATES["energy"]["fut_sigma_scale"]
    assert energy.DEFAULT_PARAMS["fut_sigma_scale"] in vals
    assert any(v > 1.0 for v in vals) and any(v < 1.0 for v in vals)
