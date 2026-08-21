"""research/synth/regen.py — the weekly job that keeps the synthetic sample current.

Generation itself is covered by the tests of panel/generator/worlds/book/build. What is
tested here is the job around it, which is where an unattended weekly step goes wrong
quietly: a series generated that the gate never binds on, a store that grows without
bound, one market's generator failure starving the other six, worlds deleted before their
replacements exist, or generation running against the live database.
"""
from __future__ import annotations

import json
import sqlite3
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import param_argmin as PA
from prediction_market_macro.research.synth import build as BD
from prediction_market_macro.research.synth import panel as P
from prediction_market_macro.research.synth import regen as RG

UTC = timezone.utc
NOW = datetime(2026, 8, 21, tzinfo=UTC)


# ── scope ───────────────────────────────────────────────────────────────────
def test_targets_are_exactly_the_monthly_markets():
    """The weekly markets settle 10-11 times in the 75-day window, so `sample_cap` sits
    far above their static CAP and a synthetic sample buys them nothing. Generating them
    anyway would be ~25 minutes a week of compute for a gate that never bites."""
    got = RG.targets()
    assert got, "no monthly market left to generate — the job has become a no-op"
    for s in got:
        assert P.PANELS[BD.SETTLES[s].panel].freq == "MS"
    for s, st in BD.SETTLES.items():
        if P.PANELS[st.panel].freq != "MS":
            assert s not in got
            assert PA.sample_cap(10) >= PA.CAP.get(s, 0), \
                f"{s} is weekly but the gate does bite on it — it may need a sample"


def test_every_target_is_actually_buildable():
    """`targets` is derived from the panel table; if a panel there has no SINKS entry the
    job discovers that at 3am, one FAIL per series, having already paid for the snapshot."""
    for s in RG.targets():
        sinks = BD._sinks(BD.SETTLES[s].panel)
        assert set(sinks) == {c.name for c in P.PANELS[BD.SETTLES[s].panel].gen_columns}


def test_the_daily_lane_never_imports_a_generator():
    """The morning pass has to finish before the board trades. An import of torch on that
    path is minutes of startup for a quantity that changes monthly."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(PA))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    banned = [m for m in mods
              if m == "torch" or m.startswith(("torch.", "prediction_market_macro.research"
                                               ".synth.regen"))
              or m.endswith((".synth.build", ".synth.generator"))]
    assert not banned, f"the morning pass would import {banned}"


# ── the store ───────────────────────────────────────────────────────────────
def _built(tmp_path, n=4, worlds=("w0.db",)):
    return BD.BuildResult(
        series="KXPCECORE", cutoff=NOW, splice=NOW - timedelta(days=40),
        anchor="2026-06-01", worlds=[tmp_path / w for w in worlds],
        events=[], coverage={"k": 1.0}, meta={"panel": "inflation_monthly"})


def _mat(rows, cols):
    return [[float(i * 10 + j) for j in range(cols)] for i in range(rows)]


def test_store_writes_one_row_per_candidate_with_its_mean_and_spread(tmp_path):
    conn = init_db(tmp_path / "m.db")
    grid = [{}, {"a": 1}, {"a": 2}]
    rid = RG._store(conn, "KXPCECORE", _built(tmp_path), grid,
                    kept_s=[0, 1, 2, 3], mat_s=_mat(4, 3), now=NOW, n_real=2)
    rows = list(conn.execute("SELECT set_idx, set_json, n_events, mean_pnl, sd_pnl"
                             " FROM synth_scores WHERE run_id=? ORDER BY set_idx", (rid,)))
    assert [r[0] for r in rows] == [0, 1, 2]
    assert [json.loads(r[1]) for r in rows] == grid
    assert all(r[2] == 4 for r in rows)
    # column j of _mat(4,3) is [j, 10+j, 20+j, 30+j]: mean 15+j, and an identical spread
    assert [r[3] for r in rows] == pytest.approx([15.0, 16.0, 17.0])
    assert rows[0][4] == pytest.approx(rows[2][4]), "a common shift must not change sd"


def test_store_records_what_the_run_was_built_from(tmp_path):
    """Provenance, not data. Without `n_real_at_build` there is no way to tell later
    whether a stored sample was ever the binding half of n_eff."""
    conn = init_db(tmp_path / "m.db")
    rid = RG._store(conn, "KXPCECORE", _built(tmp_path), [{}, {"a": 1}],
                    kept_s=[0, 1, 2], mat_s=_mat(3, 2), now=NOW, n_real=2)
    row = conn.execute("SELECT * FROM synth_runs WHERE run_id=?", (rid,)).fetchone()
    meta = json.loads(row["meta_json"])
    assert row["series"] == "KXPCECORE" and row["n_events"] == 3
    assert row["grid_hash"] == PA.grid_hash([{}, {"a": 1}])
    assert meta["n_real_at_build"] == 2 and meta["worlds"]
    assert datetime.fromisoformat(row["splice_ts"]) < datetime.fromisoformat(row["cutoff_ts"])


def test_store_prunes_old_runs_so_the_table_does_not_grow_forever(tmp_path):
    """Weekly forever with a 200-row grid is ~10k rows a year per series, none of which
    the daily lane reads — it only ever takes the newest run."""
    conn = init_db(tmp_path / "m.db")
    for i in range(6):
        RG._store(conn, "KXPCECORE", _built(tmp_path), [{}, {"a": 1}], kept_s=[0, 1, 2],
                  mat_s=_mat(3, 2), now=NOW + timedelta(days=7 * i), n_real=2,
                  keep_runs=3)
    assert conn.execute("SELECT COUNT(*) FROM synth_runs").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM synth_scores").fetchone()[0] == 6
    kept = [r[0] for r in conn.execute("SELECT run_id FROM synth_runs ORDER BY built_ts")]
    assert kept[-1].endswith(
        (NOW + timedelta(days=35)).strftime("%Y%m%dT%H%M%S")), "pruned the newest"


def test_store_prunes_only_its_own_series(tmp_path):
    conn = init_db(tmp_path / "m.db")
    for i in range(5):
        RG._store(conn, "KXPCECORE", _built(tmp_path), [{}, {"a": 1}], kept_s=[0, 1, 2],
                  mat_s=_mat(3, 2), now=NOW + timedelta(days=7 * i), n_real=2, keep_runs=2)
    RG._store(conn, "KXCPI", _built(tmp_path), [{}, {"a": 1}], kept_s=[0, 1, 2],
              mat_s=_mat(3, 2), now=NOW, n_real=2, keep_runs=2)
    got = dict(conn.execute("SELECT series, COUNT(*) FROM synth_runs GROUP BY series"))
    assert got == {"KXPCECORE": 2, "KXCPI": 1}


def test_a_stored_run_is_readable_by_the_daily_lane(tmp_path, monkeypatch):
    """The join between the two jobs is `set_hash`. This is the round trip that proves
    the weekly writer and the daily reader agree on it."""
    conn = init_db(tmp_path / "m.db")
    grid = [{}, {"a": 1}, {"a": 2}]
    rid = RG._store(conn, "KXPCECORE", _built(tmp_path), grid, kept_s=[0, 1, 2, 3],
                    mat_s=_mat(4, 3), now=NOW, n_real=2)
    conn.execute("INSERT INTO synth_lambda(series, measured_ts, lam, lam_point, lam_lo,"
                 " lam_hi, rho, n_real, n_synth, k, detail_json)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 ("KXPCECORE", NOW.isoformat(), 0.2, 0.25, 0.2, 0.3, 0.4, 4, 4, 3, "{}"))
    conn.commit()
    syn, rep = PA.read_synth(conn, "KXPCECORE", NOW + timedelta(days=1))
    assert syn is not None and syn.run_id == rid
    assert syn.weight == pytest.approx(0.2 * 4)
    assert set(syn.means) == {PA.set_hash(p) for p in grid}
    assert rep.get("skipped") is None


def test_a_stale_run_is_refused_rather_than_blended(tmp_path):
    """A world conditioned on a macro state months old is not conditioned on 当前环境,
    which is the entire premise of using it."""
    conn = init_db(tmp_path / "m.db")
    RG._store(conn, "KXPCECORE", _built(tmp_path), [{}, {"a": 1}], kept_s=[0, 1, 2],
              mat_s=_mat(3, 2), now=NOW, n_real=2)
    conn.execute("INSERT INTO synth_lambda(series, measured_ts, lam, lam_point, lam_lo,"
                 " lam_hi, rho, n_real, n_synth, k, detail_json)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 ("KXPCECORE", NOW.isoformat(), 0.2, 0.25, 0.2, 0.3, 0.4, 4, 4, 2, "{}"))
    conn.commit()
    old = NOW + timedelta(days=PA.SYNTH_MAX_AGE_DAYS + 1)
    syn, rep = PA.read_synth(conn, "KXPCECORE", old)
    assert syn is None and rep["age_days"] > PA.SYNTH_MAX_AGE_DAYS
    assert str(PA.SYNTH_MAX_AGE_DAYS) in rep["skipped"]


# ── one series ──────────────────────────────────────────────────────────────
class _Stub:
    """The expensive halves of `one`, replaced: generation and per-event replay."""

    def __init__(self, monkeypatch, tmp_path, *, n_events=20, grids=(1, 5), score=None):
        self.built = []
        self.scored = []
        out = tmp_path / "KXPCECORE"
        out.mkdir(exist_ok=True)
        worlds = [out / "world_0.db"]
        for w in worlds:
            w.write_bytes(b"")

        def fake_build(src, series, now, **kw):
            self.built.append(series)
            d = kw.get("out_dir")
            if d:
                d.mkdir(parents=True, exist_ok=True)
                for w in worlds:
                    (d / w.name).write_bytes(b"")
            return BD.BuildResult(series=series, cutoff=now,
                                  splice=now - timedelta(days=40), anchor="2026-06-01",
                                  worlds=[(kw.get("out_dir") or out) / w.name
                                          for w in worlds],
                                  events=list(range(n_events)), coverage={},
                                  meta={"panel": "inflation_monthly"})

        def fake_ladder(conn, series, lo, probed=None):
            gs = [[{}] + [{"a": i} for i in range(g - 1)] for g in grids]
            union, seen = [], set()
            for g in gs:
                for p in g:
                    h = PA.set_hash(p)
                    if h not in seen:
                        seen.add(h)
                        union.append(p)
            return gs, union

        def fake_score(events, grid, log=None):
            self.scored.append(len(grid))
            kept = events if score is None else events[:score]
            return kept, [[1.0] * len(grid) for _ in kept]

        monkeypatch.setattr(BD, "build", fake_build)
        monkeypatch.setattr(BD, "score_matrix", fake_score)
        monkeypatch.setattr(PA, "grid_ladder", fake_ladder)
        monkeypatch.setattr(PA, "universe", lambda *a, **k: [1, 2])
        monkeypatch.setattr(PA, "synth_lambda", lambda *a, **k: (0.0, {"source": None}))


def _one(conn, tmp_path, series="KXPCECORE"):
    return RG.one(conn, None, series, root=tmp_path, book=[], now=NOW, n_paths=1)


def test_one_stores_even_when_lambda_is_zero(tmp_path, monkeypatch):
    """The chicken-and-egg: at lambda 0 the sample carries no weight, so a job that
    declined to store would leave the table empty forever and be useless the moment
    lambda was measured. Storing is what makes the lane switch on without a code change."""
    conn = init_db(tmp_path / "m.db")
    _Stub(monkeypatch, tmp_path)
    r = _one(conn, tmp_path)
    assert r["stored"] and r["lam"] == 0.0 and r["weight"] == 0.0
    assert conn.execute("SELECT COUNT(*) FROM synth_scores").fetchone()[0] == r["k"]
    syn, rep = PA.read_synth(conn, "KXPCECORE", NOW)
    assert syn is None and "lambda is zero" in rep["skipped"]


def test_one_scores_the_ladder_union_not_one_grid(tmp_path, monkeypatch):
    """n_eff moves between this job and the read — a settlement lands, or lambda is
    re-measured — so scoring today's grid alone would leave `_objective` refusing to blend
    the very sample this job paid to generate."""
    conn = init_db(tmp_path / "m.db")
    st = _Stub(monkeypatch, tmp_path, grids=(1, 3, 9))
    r = _one(conn, tmp_path)
    assert r["grids"] == [1, 3, 9] and r["k"] == 9
    assert st.scored == [9], "scored once, over the union"
    stored = {row[0] for row in conn.execute(
        "SELECT set_hash FROM synth_scores WHERE run_id=?", (r["run_id"],))}
    for g in (1, 3, 9):
        grid = [{}] + [{"a": i} for i in range(g - 1)]
        assert {PA.set_hash(p) for p in grid} <= stored, f"grid of {g} not covered"


def test_one_refuses_a_defaults_only_ladder_rather_than_storing_a_single_column(
        tmp_path, monkeypatch):
    conn = init_db(tmp_path / "m.db")
    _Stub(monkeypatch, tmp_path, grids=(1,))
    r = _one(conn, tmp_path)
    assert not r["stored"] and "defaults-only" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM synth_runs").fetchone()[0] == 0


def test_one_refuses_a_sample_too_small_to_average(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "m.db")
    _Stub(monkeypatch, tmp_path, n_events=20, score=2)
    r = _one(conn, tmp_path)
    assert not r["stored"] and "2/20" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM synth_runs").fetchone()[0] == 0


def test_one_replaces_the_previous_worlds_only_after_the_new_ones_exist(
        tmp_path, monkeypatch):
    """A crash mid-generation must leave last week's sample in place, not none at all."""
    conn = init_db(tmp_path / "m.db")
    out = tmp_path / "KXPCECORE"
    out.mkdir(parents=True, exist_ok=True)
    (out / "world_9.db").write_bytes(b"old")
    seen = {}

    def fake_build(src, series, now, **kw):
        seen["old_still_there"] = (out / "world_9.db").exists()
        (out / "world_0.db").write_bytes(b"new")
        return BD.BuildResult(series=series, cutoff=now, splice=now - timedelta(days=40),
                              anchor="a", worlds=[out / "world_0.db"],
                              events=list(range(20)), coverage={}, meta={})

    monkeypatch.setattr(BD, "build", fake_build)
    monkeypatch.setattr(BD, "score_matrix",
                        lambda ev, grid, log=None: (ev, [[1.0] * len(grid) for _ in ev]))
    monkeypatch.setattr(PA, "grid_ladder",
                        lambda *a, **k: ([[{}, {"a": 1}]], [{}, {"a": 1}]))
    monkeypatch.setattr(PA, "universe", lambda *a, **k: [1, 2])
    monkeypatch.setattr(PA, "synth_lambda", lambda *a, **k: (0.0, {}))
    r = _one(conn, tmp_path)
    assert r["stored"]
    assert seen["old_still_there"], "last week's worlds were deleted before generating"
    assert not (out / "world_9.db").exists(), "superseded worlds must not accumulate"
    assert (out / "world_0.db").exists()


# ── the pass ────────────────────────────────────────────────────────────────
def test_run_generates_off_a_snapshot_never_the_live_connection(tmp_path, monkeypatch):
    """`build` hands the world connection to real prediction models. A model that ever
    learned to write would otherwise write into macro.db."""
    db = tmp_path / "macro.db"
    conn = init_db(db)
    s = types.SimpleNamespace(db_path=db)
    seen = {}

    def fake_snapshot(src, dst):
        seen["snap"] = (Path(src), Path(dst))
        Path(dst).write_bytes(Path(src).read_bytes())
        return Path(dst)

    monkeypatch.setattr(RG.W, "snapshot", fake_snapshot)
    monkeypatch.setattr(RG, "donors", lambda src, root, **k: [])
    monkeypatch.setattr(RG, "targets", lambda: ["KXPCECORE"])
    monkeypatch.setattr(RG, "one", lambda conn_, src, name, **k: (
        seen.__setitem__("src_is_snapshot", Path(src.execute(
            "PRAGMA database_list").fetchone()[2]) == seen["snap"][1]),
        {"stored": True, "run_id": "r", "n_synth": 3, "k": 2, "weight": 0.0})[1])
    RG.run(conn, s, now=NOW, log=None)
    assert seen["snap"][0] == db and seen["snap"][1] == tmp_path / "synth" / "snapshot.db"
    assert seen["src_is_snapshot"], "generation read the live db"


def test_one_market_failing_does_not_starve_the_others(tmp_path, monkeypatch):
    """The daily lane already treats an absent run as real-sample-only, so six good
    samples plus one FAIL is strictly better than an aborted pass."""
    db = tmp_path / "macro.db"
    conn = init_db(db)
    s = types.SimpleNamespace(db_path=db)
    monkeypatch.setattr(RG.W, "snapshot",
                        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()),
                                          Path(dst))[1])
    monkeypatch.setattr(RG, "donors", lambda src, root, **k: [])
    monkeypatch.setattr(RG, "targets", lambda: ["A", "B", "C"])

    def fake_one(conn_, src, name, **k):
        if name == "B":
            raise RuntimeError("generator diverged")
        return {"stored": True, "run_id": f"{name}_1", "n_synth": 9, "k": 4, "weight": 0.0}

    monkeypatch.setattr(RG, "one", fake_one)
    out = RG.run(conn, s, now=NOW, log=None)
    assert set(out) == {"A", "B", "C"}
    assert out["A"].startswith("ok") and out["C"].startswith("ok")
    assert out["B"].startswith("FAIL RuntimeError") and "diverged" in out["B"]


def test_run_reports_a_skip_as_a_skip_not_as_a_success(tmp_path, monkeypatch):
    db = tmp_path / "macro.db"
    conn = init_db(db)
    s = types.SimpleNamespace(db_path=db)
    monkeypatch.setattr(RG.W, "snapshot",
                        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()),
                                          Path(dst))[1])
    monkeypatch.setattr(RG, "donors", lambda src, root, **k: [])
    monkeypatch.setattr(RG, "targets", lambda: ["A"])
    monkeypatch.setattr(RG, "one", lambda *a, **k: {"stored": False, "reason": "no grid"})
    out = RG.run(conn, s, now=NOW, log=None)
    assert out["A"] == "skipped: no grid"


def test_run_is_silent_when_the_scheduler_passes_no_logger(tmp_path, monkeypatch):
    """`ops/refresh` calls it with log=None and captures the return value instead."""
    db = tmp_path / "macro.db"
    conn = init_db(db)
    s = types.SimpleNamespace(db_path=db)
    monkeypatch.setattr(RG.W, "snapshot",
                        lambda src, dst: (Path(dst).write_bytes(Path(src).read_bytes()),
                                          Path(dst))[1])
    monkeypatch.setattr(RG, "donors", lambda src, root, **k: [])
    monkeypatch.setattr(RG, "targets", lambda: [])
    assert RG.run(conn, s, now=NOW, log=None) == {}


# ── the donor book ──────────────────────────────────────────────────────────
def test_donors_are_reused_within_the_cache_window_and_remeasured_after(
        tmp_path, monkeypatch):
    """Re-measuring costs a full pass over every quotable event in the db. The book moves
    with venue liquidity, not with the macro state, so it is stable far longer."""
    import os
    calls = []
    monkeypatch.setattr(RG.B, "measure", lambda src, log=None: (calls.append(1), [])[1])
    monkeypatch.setattr(RG.B, "save", lambda got, path: path.write_text("[]"))
    monkeypatch.setattr(RG.B, "load", lambda path: ["cached"])
    RG.donors(None, tmp_path, now=NOW)
    book = tmp_path / "donors.json"
    assert len(calls) == 1 and book.exists()
    os.utime(book, (NOW.timestamp(), NOW.timestamp()))
    assert RG.donors(None, tmp_path, now=NOW + timedelta(days=1)) == ["cached"]
    assert len(calls) == 1, "re-measured a one-day-old book"
    RG.donors(None, tmp_path, now=NOW + timedelta(days=RG.DONOR_MAX_AGE_DAYS + 1))
    assert len(calls) == 2, "reused a book past its window"


# ── re-scoring without regenerating ─────────────────────────────────────────
def test_rescore_refuses_when_the_worlds_are_gone(tmp_path, monkeypatch):
    """Only generation can fix that, and it must say so rather than storing a run with
    zero events."""
    conn = init_db(tmp_path / "m.db")
    RG._store(conn, "KXPCECORE", _built(tmp_path, worlds=("missing.db",)),
              [{}, {"a": 1}], kept_s=[0, 1, 2], mat_s=_mat(3, 2), now=NOW, n_real=2)
    r = RG.rescore_latest(conn, types.SimpleNamespace(db_path=tmp_path / "m.db"),
                          "KXPCECORE", now=NOW, log=None)
    assert not r["stored"] and "needs a full regeneration" in r["reason"]


def test_rescore_says_so_when_there_is_nothing_to_rescore(tmp_path):
    conn = init_db(tmp_path / "m.db")
    r = RG.rescore_latest(conn, types.SimpleNamespace(db_path=tmp_path / "m.db"),
                          "KXPCECORE", now=NOW, log=None)
    assert not r["stored"] and r["reason"] == "no run to re-score"


def test_rescore_short_circuits_when_the_stored_run_already_covers_the_ladder(
        tmp_path, monkeypatch):
    """`one` and `rescore_latest` must hash the same object, or this branch is dead code
    and every call pays for a full re-score of a sample that was already correct."""
    conn = init_db(tmp_path / "m.db")
    world = tmp_path / "w0.db"
    world.write_bytes(b"")
    union = [{}, {"a": 1}, {"a": 2}]
    RG._store(conn, "KXPCECORE", _built(tmp_path, worlds=("w0.db",)), union,
              kept_s=[0, 1, 2], mat_s=_mat(3, 3), now=NOW, n_real=2)
    monkeypatch.setattr(PA, "grid_ladder", lambda *a, **k: ([union], union))
    monkeypatch.setattr(RG.BD.ps, "quotable_events", lambda *a, **k: [])
    monkeypatch.setattr(BD, "score_matrix", lambda *a, **k: pytest.fail("re-scored anyway"))
    r = RG.rescore_latest(conn, types.SimpleNamespace(db_path=tmp_path / "m.db"),
                          "KXPCECORE", now=NOW, log=None)
    assert not r["stored"] and "already covers" in r["reason"]


# ── what the models are allowed to read out of a world ──────────────────────
def test_each_monthly_predictor_only_reads_series_its_world_regenerates():
    """A world is truncated at the splice by KNOWLEDGE time, so a FRED series the panel
    does not generate does not leak the future — it goes SILENT. That is the safe failure
    but not a harmless one: a model reading a flatlined GASREGW computes a gasoline signal
    from a fake data outage, and the synthetic sample it produces is then not "符合当时情况"
    in the way the whole exercise requires. The check is on the dispatch module's own sid
    literals, which is where every monthly model names them today."""
    import ast
    import importlib
    import inspect
    from prediction_market_macro.ingest.fred import CORE_SIDS
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH

    for s in RG.targets():
        mod = importlib.import_module(SERIES_DISPATCH[s][0])
        named = {n.value for n in ast.walk(ast.parse(inspect.getsource(mod)))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        reads = named & set(CORE_SIDS)
        writes = {sk.name for sk in BD._sinks(BD.SETTLES[s].panel).values()
                  if sk.kind == "fred"}
        assert reads, f"{s}: found no FRED sid in {mod.__name__} — the check went blind"
        assert reads <= writes, \
            f"{s} reads {sorted(reads - writes)}, which {BD.SETTLES[s].panel} leaves " \
            f"frozen at the splice — add it to the panel or to that panel's SINKS"


def test_a_fresh_sample_invalidates_the_daily_cache(tmp_path):
    """`daily` skips a market whose fingerprint is unchanged. On a monthly market the next
    settlement is up to 31 days away, so a fingerprint that ignored the synthetic sample
    would leave a week-old run unread for a month — and on the day lambda first landed,
    nothing at all would rescore."""
    conn = init_db(tmp_path / "m.db")
    fp0 = PA._fingerprint(conn, "KXPCECORE", NOW)
    RG._store(conn, "KXPCECORE", _built(tmp_path), [{}, {"a": 1}], kept_s=[0, 1, 2],
              mat_s=_mat(3, 2), now=NOW, n_real=2)
    fp1 = PA._fingerprint(conn, "KXPCECORE", NOW)
    assert fp1 != fp0, "a stored run did not invalidate the cache"
    RG._store(conn, "KXPCECORE", _built(tmp_path), [{}, {"a": 1}], kept_s=[0, 1, 2],
              mat_s=_mat(3, 2), now=NOW + timedelta(days=7), n_real=2)
    fp2 = PA._fingerprint(conn, "KXPCECORE", NOW)
    assert fp2 != fp1, "next week's run did not invalidate the cache"
    conn.execute("INSERT INTO synth_lambda(series, measured_ts, lam, lam_point, lam_lo,"
                 " lam_hi, rho, n_real, n_synth, k, detail_json)"
                 " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 ("KXPCECORE", NOW.isoformat(), 0.14, 0.2, 0.14, 0.3, 0.4, 4, 3, 2, "{}"))
    conn.commit()
    assert PA._fingerprint(conn, "KXPCECORE", NOW) != fp2, \
        "the day lambda lands, every monthly market must rescore"
