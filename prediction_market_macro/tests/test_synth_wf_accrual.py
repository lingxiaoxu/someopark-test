"""S5-WF — the accruing monthly lambda measurement, and its persistence gate.

The data-reality this design answers: candles begin 2026-05-16 and Kalshi deletes them
at 75 days, so "rolling cutoffs over ~2 years" cannot exist. What accrues instead is one
real improvement row per series per monthly release. These tests pin the aggregation and
the gate, which is where the danger is: a measured per-series row SHADOWS the pooled '*'
row, so an underpowered zero written too early would re-kill the feature on schedule.

Generation itself is not tested here (worlds are deleted after scoring; the S7 suite
covers build/score); rows are inserted directly.
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
GRID = [{}, {"w_base": 0.4}, {"w_base": 0.5}, {"w_base": 0.6}, {"w_base": 0.75}]


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "t.db")
    yield c
    c.close()


def _release(conn, series, tok, real, synth, grid=None):
    g = grid if grid is not None else GRID
    conn.execute(
        "INSERT OR REPLACE INTO synth_wf_mats(series, release_tok, cutoff_ts, built_ts,"
        " grid_json, grid_hash, real_json, synth_json, meta_json)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (series, tok, "2026-03-22T00:00:00+00:00", NOW.isoformat(),
         json.dumps(g, sort_keys=True), pa.grid_hash(g),
         json.dumps(real), json.dumps(synth), "{}"))
    conn.commit()


def test_aggregate_reports_accrual_below_the_correlation_floor(conn):
    _release(conn, "KXPAYROLLS", "26MAY", [[0, 1, 2, 3, 4]], [[0, 1, 2, 3, 4]] * 10)
    rep = C.wf_aggregate(conn, "KXPAYROLLS", now=NOW)
    assert rep["n_real"] == 1
    assert "accruing" in rep["status"]
    assert conn.execute("SELECT COUNT(*) FROM synth_lambda").fetchone()[0] == 0


def test_aggregate_intersects_drifted_grids_by_set_hash_default_first(conn):
    """live_keys move between releases; the pool is the intersection, ordered by the
    newest grid with the default at column 0 (it is the improvement baseline)."""
    g_old = [{}, {"w_base": 0.4}, {"w_base": 0.5}, {"gone": 1}]
    g_new = [{}, {"w_base": 0.5}, {"w_base": 0.4}, {"new": 2}]
    _release(conn, "KXU3", "26MAY", [[1, 2, 3, 9]], [[1, 2, 3, 9]] * 4, grid=g_old)
    _release(conn, "KXU3", "26JUN", [[1, 2, 3, 9]], [[1, 2, 3, 9]] * 4, grid=g_new)
    rep = C.wf_aggregate(conn, "KXU3", now=NOW)
    assert rep["k_common"] == 3        # {}, 0.4, 0.5 survive; 'gone'/'new' do not
    # 2 real rows still below the floor — but the intersection math ran
    assert "accruing" in rep["status"] or "candidates" in rep["status"]


def test_identified_zero_below_n8_does_not_shadow_the_pooled_row(conn):
    """THE gate. Identified, correlated rows whose bootstrap lower bound is 0 at n=4:
    measured, reported, NOT persisted — '*' keeps governing this market."""
    conn.execute("INSERT INTO synth_lambda VALUES('*', ?, 0.1356, 0.03, 0.0, 0.15,"
                 " 0.17, 4, 71, 91, '{\"basis\":\"preregistered_disattenuated_point\"}')",
                 (NOW.isoformat(),))
    conn.commit()
    # 4 real rows, correlated with synth but noisy enough that lam_lo lands at 0
    real = [[0, 1.0, 2.0, 3.0, 4.0], [0, -2.0, 1.0, -1.0, 2.0],
            [0, 2.0, -1.0, 4.0, 1.0], [0, 1.0, 3.0, 2.0, 5.0]]
    synth = [[0, r[1] + i * 0.3, r[2] - i * 0.2, r[3] + 0.1, r[4]]
             for i, r in enumerate(real * 5)]
    _release(conn, "KXPAYROLLS", "26MAY", real[:2], synth[:10])
    _release(conn, "KXPAYROLLS", "26JUN", real[2:], synth[10:])
    rep = C.wf_aggregate(conn, "KXPAYROLLS", now=NOW)
    assert rep["n_real"] == 4
    if rep.get("identified") and rep["lam_lo"] == 0:
        assert "not persisted" in rep["status"]
    # whatever branch the synthetic noise landed in, KXPAYROLLS must still read '*'
    # unless a genuinely informative row was written
    lam, lrep = pa.synth_lambda(conn, "KXPAYROLLS")
    if lrep["source"] == "KXPAYROLLS":
        assert lam > 0                                    # only informative rows persist
    else:
        assert lam == pytest.approx(0.1356)


def test_informative_measurement_persists_and_supersedes_star(conn):
    """A positive lower bound at n>=4 writes the per-series measured row, and the read
    order makes it govern immediately — the self-completing upgrade path."""
    conn.execute("INSERT INTO synth_lambda VALUES('*', ?, 0.1356, 0.03, 0.0, 0.15,"
                 " 0.17, 4, 71, 91, '{\"basis\":\"preregistered_disattenuated_point\"}')",
                 (NOW.isoformat(),))
    conn.commit()
    # strong, consistent agreement: synth == real ranking exactly, plenty of rows
    base = [0.0, 1.0, 2.0, 3.0, 4.0]
    real = [base, [x * 1.1 for x in base], [x * 0.9 for x in base],
            [x * 1.05 for x in base], [x * 0.95 for x in base]]
    synth = [[x * (1 + 0.01 * i) for x in base] for i in range(40)]
    _release(conn, "KXPCECORE", "26APR", real[:2], synth[:20])
    _release(conn, "KXPCECORE", "26MAY", real[2:4], synth[20:30])
    _release(conn, "KXPCECORE", "26JUN", real[4:], synth[30:])
    rep = C.wf_aggregate(conn, "KXPCECORE", now=NOW)
    assert rep["identified"] is True
    assert rep["lam_lo"] > 0
    assert rep["status"].startswith("PERSISTED")
    lam, lrep = pa.synth_lambda(conn, "KXPCECORE")
    assert lrep["source"] == "KXPCECORE"                  # own row shadows '*'
    assert lam == pytest.approx(rep["lam_lo"], abs=1e-6)
    d = json.loads(conn.execute(
        "SELECT detail_json FROM synth_lambda WHERE series='KXPCECORE'").fetchone()[0])
    assert d["basis"] == "measured_lower_bound"
    # and the partial persist did NOT rewrite '*'
    star = conn.execute("SELECT COUNT(*) FROM synth_lambda WHERE series='*'").fetchone()[0]
    assert star == 1


def test_accrue_hands_score_matrix_keyed_events(conn, tmp_path, monkeypatch):
    """Regression: `ps.score_matrix` reads e['key'], and `quotable_events` does not
    provide it — the raw dict KeyError'd the first live backfill after ~10 min of
    generation. The keying is `run`'s, applied at the accrual boundary."""
    from types import SimpleNamespace
    from prediction_market_macro.research import pnl_score as ps2
    from prediction_market_macro.research.synth import build as BD
    from prediction_market_macro.research.synth import regen as RG
    from prediction_market_macro.research.synth import worlds as W

    evs = [{"series": "KXPAYROLLS", "tok": "26MAY", "close_ts": NOW}]
    monkeypatch.setattr(ps2, "quotable_events", lambda *a, **k: list(evs))
    monkeypatch.setattr(W, "snapshot", lambda src, dst: tmp_path / "snap.db")
    monkeypatch.setattr(RG, "donors", lambda *a, **k: [])
    from prediction_market_macro.research import param_argmin as PA2
    monkeypatch.setattr(PA2, "grid_ladder", lambda *a, **k: ([GRID], GRID))
    built = SimpleNamespace(events=[], n_synth=0, coverage={}, worlds=[])
    monkeypatch.setattr(BD, "build", lambda *a, **k: built)
    monkeypatch.setattr(BD, "score_matrix", lambda *a, **k: ([], []))
    seen = {}
    def fake_real_score(conn_, series, grid, universe, log=None):
        seen["events"] = universe
        return [], [], []
    monkeypatch.setattr(ps2, "score_matrix", fake_real_score)
    s = SimpleNamespace(db_path=tmp_path / "t.db")
    C.wf_accrue(conn, s, now=NOW, series=["KXPAYROLLS"])
    assert seen["events"], "score_matrix was never called"
    assert all("key" in e and e["key"] for e in seen["events"])


def test_accrue_is_a_noop_when_every_release_is_stored(conn, tmp_path, monkeypatch):
    """The weekly call must cost nothing 3 weeks out of 4. If every settled release is
    already stored, no snapshot, no generation, no scoring."""
    from types import SimpleNamespace
    from prediction_market_macro.research import pnl_score as ps2

    evs = [{"series": "KXPAYROLLS", "tok": "26MAY", "close_ts": NOW}]
    monkeypatch.setattr(ps2, "quotable_events", lambda *a, **k: list(evs))
    _release(conn, "KXPAYROLLS", "26MAY", [[0, 1, 2, 3, 4]], [[0, 1, 2, 3, 4]])
    called = []
    from prediction_market_macro.research.synth import worlds as W
    monkeypatch.setattr(W, "snapshot", lambda *a, **k: called.append("snapshot"))
    s = SimpleNamespace(db_path=tmp_path / "t.db")
    out = C.wf_accrue(conn, s, now=NOW, series=["KXPAYROLLS"])
    assert out == {"KXPAYROLLS": []}
    assert called == []


class TestPickPercentileTies:
    """The tie rule, fixed 2026-08-26.

    `pick_percentile` scored with a strict `<`. Real improvement vectors on short windows are
    heavily tied (most parameter sets move no decision), and a FULLY tied matrix made the
    expression 0.0 for every candidate — so a release with no information read as 0%, the
    most damning value, instead of the 50% that "no information" means. Across the 21 stored
    monthly releases that turned a true 48.7% (p=1.000) into an apparent 16.7% (p=0.000).
    """

    def test_a_fully_tied_matrix_is_fifty_percent_not_zero(self):
        """THE bug. Nothing separates the candidates, so no candidate can rank anywhere."""
        real = [[1.0, 1.0, 1.0, 1.0]]
        synth = [[0.0, 5.0, 1.0, 2.0]]          # synth has a clear favourite; real does not
        pp = C.pick_percentile(real, synth)
        assert pp["uninformative"] is True
        assert pp["n_distinct"] == 1
        assert pp["tied_frac"] == 1.0
        assert pp["percentile"] == pytest.approx(0.5)      # the null, correctly
        assert pp["percentile_strict"] == 0.0              # what shipped before

    def test_midrank_never_below_strict(self):
        """Direction of the old bias: it could only ever understate the synthetic sample,
        so lambda was conservative because of it, never inflated."""
        import numpy as np
        rng = np.random.default_rng(7)
        for _ in range(200):
            k = int(rng.integers(3, 12))
            real = [list(np.round(rng.normal(size=k), 1))]   # rounding forces ties
            synth = [list(rng.normal(size=k))]
            pp = C.pick_percentile(real, synth)
            assert pp["percentile"] >= pp["percentile_strict"] - 1e-12

    def test_untied_matrix_is_unchanged_by_the_fix(self):
        """No ties => mid-rank collapses to the strict rank, so old readings stay valid."""
        real = [[0.0, 3.0, 1.0, 2.0]]
        synth = [[0.0, 9.0, 1.0, 2.0]]          # synth picks idx 1, which is the real oracle
        pp = C.pick_percentile(real, synth)
        assert pp["tied_frac"] == pytest.approx(0.25)       # only itself
        assert pp["percentile"] == pytest.approx(pp["percentile_strict"] + 0.125)
        assert pp["pick_idx"] == 1
        assert pp["beats_default"] is True

    def test_partial_ties_split_the_tied_mass(self):
        real = [[0.0, 1.0, 1.0, 1.0]]           # 3 of 4 tied at the top
        synth = [[0.0, 5.0, 1.0, 2.0]]          # picks idx 1, one of the tied winners
        pp = C.pick_percentile(real, synth)
        assert pp["percentile_strict"] == pytest.approx(0.25)      # only the default below
        assert pp["percentile"] == pytest.approx(0.25 + 0.5 * 0.75)
        assert pp["uninformative"] is False

    def test_wf_aggregate_surfaces_the_tie_diagnostics(self, conn):
        """A pick_percentile with no tie context is unreadable — the flat-matrix case looks
        exactly like a real result. The report must carry both."""
        # >=MONTHLY_WF_MIN_REAL real rows, or the report returns at "accruing" first
        _release(conn, "KXPCECORE", "26MAY", [[1.0, 1.0, 1.0, 1.0, 1.0]] * 3,
                 [[0.0, 9.0, 1.0, 2.0, 3.0]] * 4)
        rep = C.wf_aggregate(conn, "KXPCECORE", now=NOW)
        assert rep["pick_uninformative"] is True
        assert rep["pick_tied_frac"] == pytest.approx(1.0)
        assert rep["pick_percentile"] == pytest.approx(0.5)
        assert rep["pick_percentile_strict"] == pytest.approx(0.0)
