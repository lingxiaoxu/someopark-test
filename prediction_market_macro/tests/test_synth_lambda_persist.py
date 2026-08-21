"""S8 — the writer `synth_lambda` never had, and the policy it writes under.

§7c found the whole S1–S7 chain running weekly while the daily lane refused every market,
because `calibrate` computed lambda and nothing persisted it. These tests pin the writer's
policy, which is where the honesty lives:

  * per-series rows write the COMMITTED rule (bootstrap lower bound), zeros included;
  * the pooled '*' row pools over IDENTIFIED series only, prefers a positive measured
    lower bound over the pre-registered disattenuated point, and labels which one it wrote;
  * with no identified evidence anywhere, nothing is written and the lane keeps refusing —
    there is no invented floor;
  * what `persist` writes is exactly what `param_argmin.synth_lambda` reads back, per-series
    rows shadowing '*' for the series that have one.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import param_argmin as pa
from prediction_market_macro.research.synth import calibrate as C

UTC = timezone.utc
NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _sl(series, *, rho, lam_point, lam_lo, lam_hi, rel_real, rel_synth,
        n_real=10, n_synth=100, k=21, pick=0.5):
    return C.SeriesLambda(
        series=series, n_real=n_real, n_synth=n_synth, k=k, rho=rho,
        lam_point=lam_point, lam_lo=lam_lo, lam_hi=lam_hi,
        real_improve_of_synth_pick=0.0, real_improve_oracle=1.0, default_real=0.0,
        pick_percentile=pick, rel_real=rel_real, rel_synth=rel_synth)


# The 2026-08-21 measurement, verbatim: one identified series whose lower bound is still
# zero, two whose real reference cannot correlate with itself.
CLAIMS = _sl("KXJOBLESSCLAIMS", rho=0.1743, lam_point=0.0304, lam_lo=0.0, lam_hi=0.1504,
             rel_real=0.4391, rel_synth=0.5105, n_real=4, n_synth=71, k=91, pick=0.8681)
WTI = _sl("KXWTIW", rho=0.0529, lam_point=0.0028, lam_lo=0.0, lam_hi=0.1945,
          rel_real=-0.2725, rel_synth=0.3115, pick=0.1905)
NG = _sl("KXNATGASW", rho=0.2696, lam_point=0.0727, lam_lo=0.0, lam_hi=0.1961,
         rel_real=-0.5609, rel_synth=-0.5221, n_synth=104, pick=0.2857)


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "t.db")
    yield c
    c.close()


# ── per-series rows ─────────────────────────────────────────────────────────
def test_per_series_rows_write_the_lower_bound_even_when_it_is_zero(conn):
    """The committed rule from §6, applied without editorializing. A weekly series with an
    unidentified reference SHOULD refuse a synthetic sample it cannot price, and its zero
    row is what makes that refusal stick against a positive pooled row."""
    C.persist(conn, [CLAIMS, WTI, NG], now=NOW)
    for s in ("KXJOBLESSCLAIMS", "KXWTIW", "KXNATGASW"):
        row = conn.execute("SELECT * FROM synth_lambda WHERE series=?", (s,)).fetchone()
        assert row["lam"] == 0.0
        d = json.loads(row["detail_json"])
        assert d["basis"] == "measured_lower_bound"
    d = json.loads(conn.execute(
        "SELECT detail_json FROM synth_lambda WHERE series='KXWTIW'").fetchone()[0])
    assert d["identified"] is False
    assert d["disattenuated_lam"] is None      # undefined off a broken reference


def test_a_series_own_zero_row_shadows_a_positive_pooled_row(conn):
    """`synth_lambda()` prefers the per-series row. A measured refusal must not be
    overridden by the pooled value that exists for UNmeasured markets."""
    C.persist(conn, [CLAIMS, WTI, NG], now=NOW)
    assert conn.execute("SELECT lam FROM synth_lambda WHERE series='*'").fetchone()[0] > 0
    lam, rep = pa.synth_lambda(conn, "KXWTIW")
    assert lam == 0.0 and rep["source"] == "KXWTIW"


# ── the pooled '*' row ──────────────────────────────────────────────────────
def test_pooled_row_from_the_real_measurement_is_the_disattenuated_point(conn):
    """On the 2026-08-21 sample: every identified lower bound is 0 (a property of a 4-event
    bootstrap, not of the generator), so the policy falls through to the pre-registered
    disattenuated point of the ONLY identified series — (0.1743/sqrt(.4391*.5105))^2 —
    and says so on the row."""
    rep = C.persist(conn, [CLAIMS, WTI, NG], now=NOW)
    assert rep["basis"] == "preregistered_disattenuated_point"
    assert rep["sourced_from"] == "KXJOBLESSCLAIMS"
    row = conn.execute("SELECT * FROM synth_lambda WHERE series='*'").fetchone()
    expect = (0.1743 / (0.4391 * 0.5105) ** 0.5) ** 2
    assert row["lam"] == pytest.approx(expect, abs=1e-4)      # ~0.1356
    d = json.loads(row["detail_json"])
    assert d["basis"] == "preregistered_disattenuated_point"
    assert d["identified_series"] == ["KXJOBLESSCLAIMS"]


def test_a_positive_measured_lower_bound_beats_the_preregistered_point(conn):
    """The day a real measurement produces a positive lower bound, it wins over any point
    estimate — measured beats pre-registered by construction, not by magnitude."""
    good = _sl("KXJOBLESSCLAIMS", rho=0.6, lam_point=0.36, lam_lo=0.08, lam_hi=0.5,
               rel_real=0.5, rel_synth=0.5)
    rep = C.persist(conn, [good, WTI], now=NOW)
    assert rep["basis"] == "measured_min_lo_identified"
    assert conn.execute("SELECT lam FROM synth_lambda WHERE series='*'"
                        ).fetchone()[0] == pytest.approx(0.08)


def test_unidentified_series_cannot_drag_the_pool_down(conn):
    """NG's lam_lo=0 is an artifact of a reference that cannot correlate with itself. In
    the original min() rule that artifact silently converted 'no evidence' into 'evidence
    of nothing'; the pool must exclude it."""
    good = _sl("KXJOBLESSCLAIMS", rho=0.6, lam_point=0.36, lam_lo=0.08, lam_hi=0.5,
               rel_real=0.5, rel_synth=0.5)
    rep = C.persist(conn, [good, NG], now=NOW)          # NG unidentified, lam_lo 0
    assert rep["pooled"] == pytest.approx(0.08)
    assert rep["n_identified"] == 1


def test_no_identified_series_writes_no_pooled_row_at_all(conn):
    """No invented floor. Absence of evidence stays absent on the record, and the daily
    lane keeps refusing exactly as it did pre-S8."""
    rep = C.persist(conn, [WTI, NG], now=NOW)
    assert rep["pooled"] is None
    assert conn.execute("SELECT COUNT(*) FROM synth_lambda WHERE series='*'"
                        ).fetchone()[0] == 0
    lam, rep2 = pa.synth_lambda(conn, "KXPAYROLLS")
    assert lam == 0.0 and rep2["source"] is None


# ── what the daily lane reads back ──────────────────────────────────────────
def test_monthly_market_with_no_own_row_reads_the_pooled_value(conn):
    """The whole point of S8: a monthly market — never measured, no per-series row —
    resolves to the '*' row and stops refusing."""
    C.persist(conn, [CLAIMS, WTI, NG], now=NOW)
    lam, rep = pa.synth_lambda(conn, "KXPAYROLLS")
    assert lam == pytest.approx(0.1356, abs=1e-3)
    assert rep["source"] == "*"


def test_lambda_board_states_the_switch_position_for_every_monthly_target(conn):
    """The weekly visibility line (§7c's other lesson): 'no row' and 'row that is zero'
    refuse identically in the daily log, so the weekly log must say which one it is —
    effective lambda, source row, basis, and age."""
    from prediction_market_macro.research.synth import regen as RG
    board = RG.lambda_board(conn, now=NOW)
    assert set(board) == set(RG.targets())
    assert all(v["lambda"] == 0.0 and v["source"] is None for v in board.values())
    C.persist(conn, [CLAIMS, WTI, NG], now=NOW)
    board = RG.lambda_board(conn, now=NOW)
    for v in board.values():
        assert v["lambda"] == pytest.approx(0.1356, abs=1e-3)
        assert v["source"] == "*"
        assert v["basis"] == "preregistered_disattenuated_point"
        assert v["age_days"] == 0


def test_repersisting_at_a_later_ts_supersedes_not_duplicates(conn):
    """(series, measured_ts) is the key and `synth_lambda()` reads the newest row — a
    walk-forward remeasurement lands on top without deleting the audit trail."""
    C.persist(conn, [CLAIMS, WTI, NG], now=NOW)
    good = _sl("KXJOBLESSCLAIMS", rho=0.6, lam_point=0.36, lam_lo=0.08, lam_hi=0.5,
               rel_real=0.5, rel_synth=0.5)
    C.persist(conn, [good], now=NOW.replace(hour=12))
    lam, rep = pa.synth_lambda(conn, "KXPAYROLLS")
    assert lam == pytest.approx(0.08)                    # newest '*' wins
    assert conn.execute("SELECT COUNT(*) FROM synth_lambda WHERE series='*'"
                        ).fetchone()[0] == 2             # audit trail intact
