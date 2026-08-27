"""#202 — the sample-keyed selector cache that makes #199 affordable.

#199 made `walkforward._GateBook` resolve parameters at every GRADED EVENT's own asof,
not just once per simulated day. Measured on the 30d A/B against a production clone:
per-series asofs went ~30 -> 124, those collapsed to 15 grid rescores by the per-run
memo, and each rescore is a full grid replay at ~8.8s — KXJOBLESSCLAIMS 3.0s -> 135.3s,
the whole 30d run 40s -> 6315s. The weekly regen makes eight such runs in one process.

The extra asofs land on days production never stood on, so `param_selection`'s DAY key
cannot serve any of them. `param_selection_cache` is keyed on `_sample_key` instead —
the module's own invalidation contract — and these tests pin the four things that makes
it safe rather than merely fast:

1. A stored `{}` is an ANSWER ("the gate held"), not a miss. Confusing the two would
   turn every defaults day into a rescore and delete the whole saving.
2. A moved sample key does NOT hit. This is the contract; if it broke, the lab would
   serve pre-settle answers forever and look fast while being wrong.
3. `refresh(force=True)` clears it. `force` is the documented remedy for the one thing
   the fingerprint deliberately ignores — a code change that moves predictions — and a
   remedy that misses half the caches is not a remedy.
4. A failed selector is NOT cached. Caching an exception's `{}` would pin a transient
   failure into the db permanently.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest import store
from prediction_market_macro.research import param_select as ps
from prediction_market_macro.research import walkforward as wf

NOW = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
SETS = {"vol_window": 13, "seasonal_years": 15}


def _conn():
    """The REAL schema, not a hand-copied subset.

    `_sample_key` reaches `experiments` (manual stamp) and the settlements/contracts/
    candles join (fingerprint), so a partial fixture just fails one table at a time.
    Using `store.SCHEMA` also puts the `param_selection_cache` DDL itself under test —
    a fixture that declares its own twin of the table would keep passing after the real
    one drifted, which is the failure mode this cache can least afford.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    return c


# ── 1. a stored {} is an answer ─────────────────────────────────────────────────

def test_a_stored_defaults_answer_is_a_hit_and_not_a_miss():
    c = _conn()
    ps.store_answer(c, "KXU3", "k1", {}, {"adopted": False}, NOW)
    assert ps.cached_answer(c, "KXU3", "k1") == {}, (
        "'the gate held, use the defaults' is the COMMON answer. Reading it back as a"
        " miss would rescore the grid on every defaults day — the majority of them —"
        " and the cache would save nothing at all.")


def test_an_absent_row_is_a_miss_and_is_distinguishable_from_defaults():
    assert ps.cached_answer(_conn(), "KXU3", "never-scored") is None


def test_an_adopted_answer_round_trips():
    c = _conn()
    ps.store_answer(c, "KXU3", "k1", SETS, None, NOW)
    assert ps.cached_answer(c, "KXU3", "k1") == SETS


def test_the_cache_is_per_series():
    c = _conn()
    ps.store_answer(c, "KXU3", "k1", SETS, None, NOW)
    assert ps.cached_answer(c, "KXCPI", "k1") is None, (
        "the sample key is built per series; sharing a key across series would hand one"
        " series' adopted set to another")


# ── 2. the invalidation contract ────────────────────────────────────────────────

def test_a_moved_sample_key_does_not_hit():
    c = _conn()
    ps.store_answer(c, "KXU3", "fp1|m:none", SETS, None, NOW)
    assert ps.cached_answer(c, "KXU3", "fp2|m:none") is None, (
        "a new settle moves the fingerprint; serving the old answer across it is"
        " exactly the staleness #200 had to fix in the per-run memo")


def test_a_new_manual_override_moves_the_key_and_so_misses(monkeypatch):
    """#198c one layer down: the stamp is IN the key, so the cache inherits the fix."""
    c = _conn()
    k_before = ps._sample_key(c, "KXU3", NOW)
    ps.store_answer(c, "KXU3", k_before, {}, None, NOW)
    ps.set_manual(c, "KXU3", SETS, note="argmin cron")
    k_after = ps._sample_key(c, "KXU3", datetime.now(timezone.utc))
    assert k_after != k_before
    assert ps.cached_answer(c, "KXU3", k_after) is None


# ── 3. force=True is a real remedy ──────────────────────────────────────────────

def test_force_refresh_clears_the_sample_keyed_cache(monkeypatch):
    c = _conn()
    ps.store_answer(c, "KXU3", "k1", SETS, None, NOW)
    monkeypatch.setattr(ps, "MODULE_OF", {"KXU3": object()})
    monkeypatch.setattr(ps, "select_for",
                        lambda *a, **k: ({}, {"adopted": False, "n_obs": 3}))
    monkeypatch.setattr(ps, "_veto_on_branch_parity",
                        lambda conn, s, now, p, rep: (p, rep))
    ps.refresh(c, asof=NOW, series=["KXU3"], force=True, log=None)
    rows = c.execute("SELECT sample_key FROM param_selection_cache"
                     " WHERE series='KXU3' AND sample_key='k1'").fetchall()
    assert rows == [], (
        "`force` is the documented remedy after a model change — the fingerprint is"
        " deliberately blind to code. A remedy that clears the day-keyed cache and"
        " leaves the sample-keyed one would serve pre-change answers to the lab"
        " forever, and would do it silently because the run gets FASTER, not slower.")


def test_a_plain_refresh_seeds_the_cache_without_clearing_it(monkeypatch):
    c = _conn()
    ps.store_answer(c, "KXU3", "old-key", SETS, None, NOW)
    monkeypatch.setattr(ps, "MODULE_OF", {"KXU3": object()})
    monkeypatch.setattr(ps, "select_for",
                        lambda *a, **k: ({"vol_window": 9}, {"adopted": True}))
    monkeypatch.setattr(ps, "_veto_on_branch_parity",
                        lambda conn, s, now, p, rep: (p, rep))
    ps.refresh(c, asof=NOW, series=["KXU3"], log=None)
    assert ps.cached_answer(c, "KXU3", "old-key") == SETS
    assert ps.cached_answer(c, "KXU3", ps._sample_key(c, "KXU3", NOW)) == {"vol_window": 9}, (
        "the daily run pays for one selection a day anyway; not recording it would make"
        " the lab re-derive an answer production already has")


def test_the_seeded_answer_is_the_pre_veto_one(monkeypatch):
    """The cache holds `select_for`'s output. The branch-parity veto (#201) is
    production's overlay on that, applied by the caller that wants it."""
    c = _conn()
    monkeypatch.setattr(ps, "MODULE_OF", {"KXU3": object()})
    monkeypatch.setattr(ps, "select_for", lambda *a, **k: (SETS, {"adopted": True}))
    monkeypatch.setattr(ps, "_veto_on_branch_parity",
                        lambda conn, s, now, p, rep: ({}, {**rep, "vetoed": True}))
    ps.refresh(c, asof=NOW, series=["KXU3"], log=None)
    assert ps.cached_answer(c, "KXU3", ps._sample_key(c, "KXU3", NOW)) == SETS


# ── 4. failures are not cached, and the book uses the cache ─────────────────────

def _book(conn):
    return wf._GateBook(conn, db_gates=False, select_params=True)


def test_a_selector_failure_is_not_written_to_the_cache(monkeypatch):
    c = _conn()
    b = _book(c)
    monkeypatch.setattr(ps, "_sample_key", lambda *a, **k: "k1")
    monkeypatch.setattr(ps, "select_for", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("transient")))
    assert b.params_for("KXU3", NOW) is None
    assert ps.cached_answer(c, "KXU3", "k1") is None, (
        "a transient failure degrades to the defaults for THIS run; writing that {} to"
        " the db would pin the outage into every future run silently")


def test_the_book_serves_a_stored_answer_without_rescoring(monkeypatch):
    c = _conn()
    b = _book(c)
    monkeypatch.setattr(ps, "_sample_key", lambda *a, **k: "k1")
    ps.store_answer(c, "KXU3", "k1", SETS, None, NOW)
    monkeypatch.setattr(ps, "select_for", lambda *a, **k: pytest.fail(
        "the cache was consulted too late — this is the ~8.8s call #202 exists to skip"))
    assert b.params_for("KXU3", NOW) == SETS
    assert b.stats["param_rescores"] == 0
    assert b.stats["param_cache_hits"] == 1


def test_a_miss_rescores_once_and_then_persists_for_the_next_run(monkeypatch):
    c = _conn()
    calls = []
    monkeypatch.setattr(ps, "_sample_key", lambda *a, **k: "k1")
    monkeypatch.setattr(ps, "select_for",
                        lambda *a, **k: (calls.append(1), (SETS, {}))[1])
    first = _book(c)
    assert first.params_for("KXU3", NOW) == SETS
    assert first.stats["param_rescores"] == 1
    # a SECOND book — a new run in the same process, or a later one on the same db
    second = _book(c)
    assert second.params_for("KXU3", NOW) == SETS
    assert len(calls) == 1, (
        "the weekly regen makes eight runs in one process (a sweep per lead, then 60d,"
        " then 30d). Without cross-run reuse each pays #199's rescores in full.")
    assert second.stats["param_rescores"] == 0
    assert second.stats["param_cache_hits"] == 1


def test_a_read_only_db_degrades_to_compute_instead_of_raising(monkeypatch):
    """`store_answer` swallows: the cache is an optimisation, never a dependency."""
    c = _conn()
    c.execute("DROP TABLE param_selection_cache")
    b = _book(c)
    monkeypatch.setattr(ps, "_sample_key", lambda *a, **k: "k1")
    monkeypatch.setattr(ps, "select_for", lambda *a, **k: (SETS, {}))
    assert b.params_for("KXU3", NOW) == SETS
    assert b.stats["param_rescores"] == 1
