"""param_argmin.daily — the standing-user-policy loop's contracts.

1. Fingerprint cache: an unchanged sample never rescored (the morning pass must be
   cheap on no-news days).
2. A changed argmin writes a NEW manual_params row (history grows, nothing replaced)
   and the note carries the DSR objection.
3. An unchanged argmin writes NO manual row (no history spam), but still logs.
4. The grid always carries the default at index 0 — argmin can return to defaults.
5. #201: a write is refused when the sample graded a branch production does not run,
   an already-live override on such a series is reported every pass rather than
   reverted, and a proposal of `{}` is let through because reverting to the registered
   default is the one move that needs no evidence.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import param_argmin as pa
from prediction_market_macro.research import param_select as ps

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE experiments(name TEXT, config_hash TEXT, series TEXT,
        window TEXT, metrics_json TEXT, created_ts TEXT,
        PRIMARY KEY(name, config_hash));
    CREATE TABLE settlements(ticker TEXT, series TEXT, period TEXT, result TEXT,
        settled_ts TEXT, first_seen_ts TEXT);
    CREATE TABLE contracts(ticker TEXT, series TEXT, period TEXT, status TEXT,
        close_time TEXT);
    CREATE TABLE alerts(ts TEXT, level TEXT, source TEXT, message TEXT);
    """)
    return c


def _patch(monkeypatch, best_params, fp="3:2026-08-08"):
    monkeypatch.setattr(pa, "MARKETS", ["KXNATGASW"])
    monkeypatch.setattr(pa, "_fingerprint", lambda *a: fp)
    monkeypatch.setattr(pa, "rescore", lambda *a, **k: {
        "grid": [{}, best_params], "grid_report": {}, "n_events": 3,
        "best_idx": 1, "best_params": best_params,
        "pnl_best": 1.0, "pnl_default": -1.0})
    # The four contracts above are about the adoption loop, not about #201. The parity
    # functions are stubbed OUT here so those tests keep testing what they were written
    # to test; the tests below stub `branch_parity.parity_check` instead and exercise
    # the real ones.
    monkeypatch.setattr(pa, "parity_veto", lambda *a, **k: None)
    monkeypatch.setattr(pa, "stale_override_warning", lambda *a, **k: None)


def _parity(monkeypatch, ok: bool, reason="graded on X but production runs Y"):
    """Stub the parity verdict itself, so `parity_veto`/`stale_override_warning` run for
    real. Both of them build the mixes first, so those are stubbed too — they read a live
    db and this fixture has none."""
    from prediction_market_macro.research import branch_parity as bp
    monkeypatch.setattr(bp, "hist_branch_mix", lambda *a, **k: {"n": 40})
    monkeypatch.setattr(bp, "live_branch_mix", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(bp, "parity_check",
                        lambda *a, **k: {"parity": ok, "reason": None if ok else reason})


def _hermetic_manual(monkeypatch):
    """Clock-free manual store. The real set_manual stamps wall-clock created_ts
    while these tests run at a FIXED simulated NOW — the PIT comparison inside
    manual_params turns that into a date-dependent time bomb (this file passed on
    2026-08-11 and failed on 08-12 with zero code changes). daily()'s changed/
    unchanged logic only needs get/set semantics, so give it exactly that."""
    store: dict = {}
    monkeypatch.setattr(pa, "set_manual",
                        lambda c, s, p, note: store.__setitem__(s, dict(p)))
    monkeypatch.setattr(pa, "manual_params",
                        lambda c, s, now: (store[s], "ts") if s in store else None)
    return store


def test_changed_argmin_adopts_and_history_grows(conn, monkeypatch):
    _patch(monkeypatch, {"fut_vol_window": 40})
    out = pa.daily(conn, now=NOW, log=None)
    assert out["KXNATGASW"].startswith("ADOPTED")
    h = ps.history(conn, "KXNATGASW")
    assert len(h) == 1 and h[0]["params"] == {"fut_vol_window": 40}
    assert "DSR" in h[0]["note"]
    # second run, same fingerprint -> cached, no new history
    out2 = pa.daily(conn, now=NOW + timedelta(hours=1), log=None)
    assert out2["KXNATGASW"] == "cached"
    assert len(ps.history(conn, "KXNATGASW")) == 1


def test_new_sample_same_argmin_logs_but_writes_no_manual_row(conn, monkeypatch):
    store = _hermetic_manual(monkeypatch)
    _patch(monkeypatch, {"fut_vol_window": 40})
    pa.daily(conn, now=NOW, log=None)
    assert store["KXNATGASW"] == {"fut_vol_window": 40}
    _patch(monkeypatch, {"fut_vol_window": 40}, fp="4:2026-08-09")   # new settle
    out = pa.daily(conn, now=NOW + timedelta(days=1), log=None)
    assert out["KXNATGASW"] == "unchanged"
    logs = conn.execute("SELECT COUNT(*) FROM experiments"
                        " WHERE name='param_argmin'").fetchone()[0]
    assert logs == 2


def test_argmin_can_return_to_defaults(conn, monkeypatch):
    store = _hermetic_manual(monkeypatch)
    _patch(monkeypatch, {"fut_vol_window": 40})
    pa.daily(conn, now=NOW, log=None)
    _patch(monkeypatch, {}, fp="5:2026-08-10")      # defaults win the new sample
    out = pa.daily(conn, now=NOW + timedelta(days=2), log=None)
    assert out["KXNATGASW"].startswith("ADOPTED")
    assert store["KXNATGASW"] == {}                 # active override: the defaults


# ── #201: the parity guard on the lane that actually writes ───────────────────
def _patch_no_parity_stub(monkeypatch, best_params, fp="3:2026-08-08"):
    """`_patch` minus the parity stub-out — for the tests that want the real guard.

    Deliberately NOT written as `_patch(...)` followed by `monkeypatch.undo()`: undo()
    unwinds every patch on the fixture, including the hermetic manual store, and the
    resulting failure would look like a parity bug.
    """
    monkeypatch.setattr(pa, "MARKETS", ["KXFED"])
    monkeypatch.setattr(pa, "_fingerprint", lambda *a: fp)
    monkeypatch.setattr(pa, "rescore", lambda *a, **k: {
        "grid": [{}, best_params], "grid_report": {}, "n_events": 40,
        "best_idx": 1, "best_params": best_params,
        "pnl_best": 1.0, "pnl_default": -1.0})


def test_a_write_is_refused_when_the_sample_graded_the_wrong_branch(conn, monkeypatch):
    """The KXFED case that motivated #201: argmin picks `w_rule` on a 40-event sample in
    which 97.5% of events ran a branch production does not run."""
    store = _hermetic_manual(monkeypatch)
    _patch_no_parity_stub(monkeypatch, {"w_rule": 0.25})
    _parity(monkeypatch, ok=False)
    out = pa.daily(conn, now=NOW, log=None)
    assert out["KXFED"].startswith("PARITY-VETOED")
    assert "KXFED" not in store, "the refused set must not reach production"
    alerts = conn.execute("SELECT message FROM alerts WHERE source='param_argmin'"
                          " AND message LIKE '%refused%'").fetchall()
    assert len(alerts) == 1
    row = json.loads(conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='param_argmin'").fetchone()[0])
    assert row["adopted_change"] is False
    assert row["branch_parity_veto"]["parity"] is False


def test_a_revert_to_defaults_is_let_through_even_on_a_failing_series(conn, monkeypatch):
    """Reverting to the registered default is the one move that needs no evidence, and
    it is how a bad row gets cleaned up by measurement instead of by hand."""
    store = _hermetic_manual(monkeypatch)
    _patch_no_parity_stub(monkeypatch, {"w_rule": 0.25})
    _parity(monkeypatch, ok=True)
    pa.daily(conn, now=NOW, log=None)
    assert store["KXFED"] == {"w_rule": 0.25}

    _patch_no_parity_stub(monkeypatch, {}, fp="4:2026-09-01")
    _parity(monkeypatch, ok=False)
    out = pa.daily(conn, now=NOW + timedelta(days=1), log=None)
    assert not out["KXFED"].startswith("PARITY-VETOED")
    assert store["KXFED"] == {}


def test_an_already_live_override_is_reported_every_pass_not_reverted(conn, monkeypatch):
    """A veto stops new writes; it cannot un-adopt what is already in force. Silence would
    make the only trace of a live-wrong override the day it was refused a REPLACEMENT —
    which is the wrong day, and might never come."""
    store = _hermetic_manual(monkeypatch)
    _patch_no_parity_stub(monkeypatch, {"w_rule": 0.25})
    _parity(monkeypatch, ok=True)
    pa.daily(conn, now=NOW, log=None)

    # parity breaks later; the argmin now agrees with what is already adopted, so there is
    # no write to refuse and the ONLY signal available is the stale-override warning.
    _patch_no_parity_stub(monkeypatch, {"w_rule": 0.25}, fp="4:2026-09-01")
    _parity(monkeypatch, ok=False)
    out = pa.daily(conn, now=NOW + timedelta(days=1), log=None)
    assert out["KXFED"].startswith("unchanged")
    assert "STALE-OVERRIDE" in out["KXFED"]
    assert store["KXFED"] == {"w_rule": 0.25}, "reporting is not reverting"
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE message LIKE"
                        " '%ACTIVE override%'").fetchone()[0] == 1


def test_a_gated_market_still_gets_the_stale_override_warning(conn, monkeypatch):
    """Thin sample and live-wrong override are independent problems. The sample gate bars
    NEW selection; it must not also silence the report on what is already in force."""
    store = _hermetic_manual(monkeypatch)
    store["KXFED"] = {"w_rule": 0.25}
    monkeypatch.setattr(pa, "MARKETS", ["KXFED"])
    monkeypatch.setattr(pa, "_fingerprint", lambda *a: "fp")
    monkeypatch.setattr(pa, "rescore", lambda *a, **k: {
        "grid": [{}], "grid_report": {"cap": 1, "sample_cap": 1}, "n_events": 2,
        "best_idx": 0, "best_params": {}, "pnl_best": None, "pnl_default": None,
        "gated": True})
    _parity(monkeypatch, ok=False)
    out = pa.daily(conn, now=NOW, log=None)
    assert out["KXFED"].startswith("GATED")
    assert "STALE-OVERRIDE" in out["KXFED"]
    assert store["KXFED"] == {"w_rule": 0.25}


def test_no_parity_work_is_done_when_nothing_would_change(conn, monkeypatch):
    """The check costs a full replay plus one predict per open period. It may only run on
    a series that is about to be written, or that already carries an override."""
    _hermetic_manual(monkeypatch)
    _patch_no_parity_stub(monkeypatch, {})
    calls = []
    from prediction_market_macro.research import branch_parity as bp
    monkeypatch.setattr(bp, "hist_branch_mix",
                        lambda *a, **k: calls.append(1) or {"n": 40})
    monkeypatch.setattr(bp, "live_branch_mix", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(bp, "parity_check", lambda *a, **k: {"parity": True})
    out = pa.daily(conn, now=NOW, log=None)
    assert out["KXFED"] == "unchanged"
    assert calls == [], "no override proposed and none in force — nothing to check"


def test_real_build_keeps_default_at_index_zero(conn):
    # build() needs a db with settled events for the probe; on an empty db the grid
    # must still be default-only rather than crashing.
    grid, rep = pa.build(conn, "KXNATGASW", NOW)
    assert grid[0] == {}
