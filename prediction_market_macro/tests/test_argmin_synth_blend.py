"""param_argmin's synthetic half (S6) — what a DFM-generated event is allowed to buy.

The synthetic sample exists to relieve the sample gate on the monthly markets, which
settle 2-3 times in a 75-day window and were picking a winner out of ~100 candidate sets.
It relieves it only DISCOUNTED, `n_eff = n_real + lambda * n_synth`, and lambda is a
measured exchange rate rather than a chosen one.

What these tests pin is the part that is dangerous rather than the part that is clever:
that an unmeasured lambda leaves the lane byte-identical to its pre-S6 behaviour, that the
uniform PnL inflation §5c measured in the synthetic world cannot move a decision, and that
every refusal to blend is reported rather than silently taken.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import param_argmin as pa

UTC = timezone.utc
NOW = datetime(2026, 8, 21, tzinfo=UTC)
GRID = [{}, {"w_base": 0.4}, {"w_base": 0.6}]


# ── identity of a parameter set ─────────────────────────────────────────────
def test_set_hash_ignores_dict_ordering_but_not_content():
    """The grid is rebuilt every morning from `itertools.product`, and Python dict order
    follows insertion. If the hash followed it too, yesterday's scores would miss today's
    grid at random and the blend would silently never happen."""
    assert pa.set_hash({"a": 1, "b": 2}) == pa.set_hash({"b": 2, "a": 1})
    assert pa.set_hash({"a": 1}) != pa.set_hash({"a": 1.5})


def test_grid_hash_is_order_sensitive_because_index_zero_is_the_default():
    """Every improvement is measured against grid[0]. Two grids with the same members in a
    different order are not the same experiment."""
    assert pa.grid_hash(GRID) != pa.grid_hash([GRID[0], GRID[2], GRID[1]])


# ── the objective ───────────────────────────────────────────────────────────
def _sample(means, lam=0.1, n_synth=100, age_days=1):
    return pa.SynthSample("run", lam, n_synth, NOW - timedelta(days=age_days),
                          {pa.set_hash(p): m for p, m in zip(GRID, means)})


def test_without_a_synthetic_sample_the_argmax_is_the_old_one():
    """The no-op guarantee. `totals` is a sum over events, the objective is a mean
    improvement; both are affine in the same direction, so the winner cannot move."""
    totals = [10.0, 4.0, 25.0]
    obj, blended = pa._objective(totals, 5, GRID, None, {})
    assert not blended
    assert max(range(3), key=lambda j: obj[j]) == max(range(3), key=lambda j: totals[j])


def test_lambda_zero_is_a_no_op_not_a_change_of_objective():
    """`read_synth` returns None at lambda <= 0, so a market with no measurement behaves
    exactly as it did before S6 — which is what makes shipping this safe while the
    exchange rate is still being measured."""
    conn = init_db(":memory:")
    conn.execute("INSERT INTO synth_lambda(series, measured_ts, lam, lam_point, lam_lo,"
                 " lam_hi, detail_json) VALUES('*','2026-08-21',0.0,0.03,0.0,0.15,'{}')")
    syn, rep = pa.read_synth(conn, "KXPAYROLLS", NOW)
    assert syn is None
    assert "lambda is zero" in rep["skipped"]


def test_a_uniform_synthetic_windfall_cannot_move_the_winner():
    """§5c measured `mean|z_y|` at 0.725 synthetic against 0.964 real: the incumbent is
    more accurate on synthetic worlds, so synthetic PnL is inflated for EVERY candidate.
    Measuring improvement against the default cancels exactly that."""
    totals = [10.0, 4.0, 25.0]
    plain = pa._objective(totals, 5, GRID, _sample([0.0, 1.0, 2.0]), {})[0]
    rich = pa._objective(totals, 5, GRID, _sample([50.0, 51.0, 52.0]), {})[0]
    assert plain == pytest.approx(rich)


def test_the_synthetic_side_can_overturn_a_real_winner_and_that_is_the_point():
    """If it could not, `n_eff` would be widening the search without informing it — the
    gate would loosen on evidence that never touches the decision."""
    totals = [0.0, 1.0, 1.2]                      # real likes index 2, barely
    syn = _sample([0.0, 9.0, 0.0], lam=0.5, n_synth=100)   # synthetic likes index 1, a lot
    obj, blended = pa._objective(totals, 5, GRID, syn, {})
    assert blended
    assert max(range(3), key=lambda j: obj[j]) == 1


def test_a_synthetic_run_that_misses_one_candidate_blends_none_of_them():
    """Partial coverage would score candidate A on 105 events and candidate B on 5. That
    is the exact bias `pnl_score.score_matrix`'s all-sets keep rule exists to remove, and
    it must not come back in through the side door."""
    syn = _sample([0.0, 9.0, 0.0])
    del syn.means[pa.set_hash(GRID[2])]
    rep: dict = {}
    obj, blended = pa._objective([0.0, 1.0, 1.2], 5, GRID, syn, rep)
    assert not blended
    assert "2/3" in rep["skipped"]
    assert max(range(3), key=lambda j: obj[j]) == 2, "must fall back to the real winner"


# ── reading the store ───────────────────────────────────────────────────────
def _seed(conn, *, lam=0.2, built=NOW, n_events=100, series="KXPAYROLLS"):
    conn.execute("INSERT INTO synth_lambda(series, measured_ts, lam, lam_point, lam_lo,"
                 " lam_hi, detail_json) VALUES('*','2026-08-21',?,?,?,0.3,'{}')",
                 (lam, lam, lam))
    conn.execute("INSERT INTO synth_runs(run_id, series, cutoff_ts, splice_ts, built_ts,"
                 " n_paths, n_events, grid_hash, meta_json)"
                 " VALUES('r1',?,?,?,?,8,?,?,'{}')",
                 (series, NOW.isoformat(), NOW.isoformat(), built.isoformat(),
                  n_events, pa.grid_hash(GRID)))
    for i, p in enumerate(GRID):
        conn.execute("INSERT INTO synth_scores(run_id, set_hash, set_json, set_idx,"
                     " n_events, mean_pnl, sd_pnl) VALUES('r1',?,?,?,?,?,1.0)",
                     (pa.set_hash(p), json.dumps(p), i, n_events, float(i)))
    conn.commit()
    return conn


def test_a_run_is_read_back_with_its_weight():
    syn, rep = pa.read_synth(_seed(init_db(":memory:")), "KXPAYROLLS", NOW)
    assert syn is not None and syn.n_synth == 100 and syn.lam == pytest.approx(0.2)
    assert syn.weight == pytest.approx(20.0)
    assert rep["source"] == "*"


def test_a_stale_run_is_refused_by_name_not_used_quietly():
    """The generator is conditioned on the macro state at its cutoff. Past
    SYNTH_MAX_AGE_DAYS that state is no longer the one the parameters are about to face,
    which is the entire premise of conditioning."""
    old = NOW - timedelta(days=pa.SYNTH_MAX_AGE_DAYS + 1)
    syn, rep = pa.read_synth(_seed(init_db(":memory:"), built=old), "KXPAYROLLS", NOW)
    assert syn is None
    assert str(pa.SYNTH_MAX_AGE_DAYS) in rep["skipped"]


def test_a_per_series_lambda_wins_over_the_pooled_one():
    """The pooled row is the min of per-series lower bounds — the right inheritance for a
    market never measured, the wrong one for a market that was."""
    conn = _seed(init_db(":memory:"), lam=0.05)
    conn.execute("INSERT INTO synth_lambda(series, measured_ts, lam, lam_point, lam_lo,"
                 " lam_hi, detail_json) VALUES('KXPAYROLLS','2026-08-21',0.4,0.4,0.4,"
                 "0.5,'{}')")
    lam, rep = pa.synth_lambda(conn, "KXPAYROLLS")
    assert lam == pytest.approx(0.4) and rep["source"] == "KXPAYROLLS"
    assert pa.synth_lambda(conn, "KXU3")[0] == pytest.approx(0.05)


def test_a_db_without_the_synthetic_store_says_so_rather_than_raising():
    """Any db older than this feature, and every test fixture built by hand. A swallowed
    OperationalError here would read exactly like a typo in the query, so the missing
    tables are named."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    lam, rep = pa.synth_lambda(conn, "KXPAYROLLS")
    assert lam == 0.0
    assert "synth_runs" in rep["note"]


# ── the gate the whole thing exists to relieve ──────────────────────────────
def test_the_discounted_sample_widens_the_gate_and_only_by_the_discount():
    """KXPAYROLLS settles twice in 75 days: n=2 supports a width of 1, i.e. defaults only.
    100 synthetic events at the measured lambda buy a real search — but the arithmetic is
    the same inversion, applied to n_eff, with nothing special-cased."""
    assert pa.sample_cap(2) == 1
    assert pa.sample_cap(2 + 0.2 * 100) > pa.CAP["KXPAYROLLS"]
    assert pa.sample_cap(2 + 0.03 * 100) == pa.sample_cap(5.0), "no special-casing"


def test_sample_cap_takes_a_float_because_n_eff_is_never_an_integer():
    assert pa.sample_cap(2.5) > pa.sample_cap(2.0)
    assert pa.sample_cap(0.4) == 0, "a fractional sample below one event supports nothing"
