"""`param_select` is the only #119 component that touches production predictions, so the
tests here are about its failure modes rather than its arithmetic — `dsr` and `pnl_score`
are tested where they live.

Three ways this could quietly hurt the live book, all pinned below:

  1. **A gate that held becomes indistinguishable from a selector that never ran.** Both
     end up as "defaults" at the prediction site, but only one of them is fine. The row
     with `params_json='{}'` is the evidence that the search ran and declined.
  2. **A stale row is used forever.** A selection stays PIT-valid as it ages, so falling
     back to yesterday's is correct; falling back to last quarter's is not, and nothing in
     the type of the value says which one you have.
  3. **The cache carries a verdict forward past the event that should have changed it.**
     The fingerprint is the whole safety argument for not rescoring daily.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import param_select as psel

_NOW = datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    from prediction_market_macro.ingest.store import SCHEMA
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _stub(monkeypatch, params, rep=None, key="k1", counter=None, parity=True):
    def fake(conn, series, before, adopt_p=0.95, log=None):
        if counter is not None:
            counter.append(series)
        return dict(params), dict(rep or {"adopted": bool(params), "n_obs": 20,
                                          "n_trials": 10, "dsr_p": 0.99})
    monkeypatch.setattr(psel, "select_for", fake)
    monkeypatch.setattr(psel, "_sample_key", lambda conn, s, before: key)
    if parity:
        # #196's veto replays settled history and predicts the open periods; on these
        # empty in-memory stores it finds neither, and "no evidence" is a veto by design.
        # The tests below are about the cache and the row, so it is stubbed to a
        # pass-through here — `test_branch_parity_vetoes_an_adoption` exercises it for
        # real. Stubbed rather than weakened: an unverifiable series must STAY
        # unverifiable in production, which is the entire point of the veto.
        monkeypatch.setattr(psel, "_veto_on_branch_parity",
                            lambda conn, s, now, p, rep: (p, rep))


# ── 1. the gate holding is a recorded event, not an absence ─────────────────────

def test_the_gate_holding_writes_a_row_saying_so(conn, monkeypatch):
    _stub(monkeypatch, {}, rep={"adopted": False, "n_obs": 11, "n_trials": 10,
                                "dsr_p": None, "reason": "only 11 scored events"})
    psel.refresh(conn, asof=_NOW, series=["KXWTIW"], log=None)
    row = conn.execute("SELECT * FROM param_selection WHERE series='KXWTIW'").fetchone()
    assert row["params_json"] == "{}" and row["adopted"] == 0
    assert json.loads(row["report_json"])["reason"].startswith("only 11")
    assert psel.current(conn, "KXWTIW", day=_NOW.date().isoformat()) == {}


def test_a_selector_failure_leaves_no_row_and_raises_an_alert(conn, monkeypatch):
    """Writing '{}' on an exception would record a decision that was never made, and the
    daily run would look healthy. No row + an alert is the honest state."""
    def boom(*_a, **_k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(psel, "select_for", boom)
    monkeypatch.setattr(psel, "_sample_key", lambda *a, **k: "k1")
    psel.refresh(conn, asof=_NOW, series=["KXWTIW"], log=None)
    assert conn.execute("SELECT COUNT(*) c FROM param_selection").fetchone()["c"] == 0
    a = conn.execute("SELECT * FROM alerts WHERE source='param_select'").fetchone()
    assert a["level"] == "error" and "db locked" in a["message"]


def test_one_series_blowing_up_does_not_stop_the_rest(conn, monkeypatch):
    calls = []

    def half(conn_, series, before, adopt_p=0.95, log=None):
        calls.append(series)
        if series == "KXWTIW":
            raise RuntimeError("boom")
        return {}, {"adopted": False, "n_obs": 3}
    monkeypatch.setattr(psel, "select_for", half)
    monkeypatch.setattr(psel, "_sample_key", lambda *a, **k: "k1")
    psel.refresh(conn, asof=_NOW, series=["KXWTIW", "KXNATGASW"], log=None)
    assert calls == ["KXWTIW", "KXNATGASW"]
    assert conn.execute("SELECT COUNT(*) c FROM param_selection").fetchone()["c"] == 1


# ── 2. staleness ────────────────────────────────────────────────────────────────

def test_current_uses_the_most_recent_row_at_or_before_the_day(conn, monkeypatch):
    _stub(monkeypatch, {"vol_window": 26})
    psel.refresh(conn, asof=_NOW - timedelta(days=3), series=["KXWTIW"], log=None)
    assert psel.current(conn, "KXWTIW", day=_NOW.date().isoformat()) == {"vol_window": 26}


def test_current_will_not_run_on_a_selection_older_than_the_stale_horizon(conn,
                                                                         monkeypatch):
    _stub(monkeypatch, {"vol_window": 26})
    old = _NOW - timedelta(days=psel.MAX_STALE_DAYS + 1)
    psel.refresh(conn, asof=old, series=["KXWTIW"], log=None)
    assert psel.current(conn, "KXWTIW", day=_NOW.date().isoformat()) == {}, \
        "a month-old selection must fall back to the registered defaults"


def test_current_never_reads_a_row_stamped_in_the_future(conn, monkeypatch):
    """`day` is a simulated or replayed date in some callers. A row from tomorrow is a
    lookahead, and `ORDER BY day DESC` alone would happily return it."""
    _stub(monkeypatch, {"vol_window": 26})
    psel.refresh(conn, asof=_NOW + timedelta(days=2), series=["KXWTIW"], log=None)
    assert psel.current(conn, "KXWTIW", day=_NOW.date().isoformat()) == {}


def test_an_unreadable_params_blob_degrades_to_defaults(conn):
    conn.execute("INSERT INTO param_selection(series, day, params_json, adopted,"
                 " report_json, created_ts) VALUES(?,?,?,?,?,?)",
                 ("KXWTIW", _NOW.date().isoformat(), "{not json", 1, "{}", "x"))
    assert psel.current(conn, "KXWTIW", day=_NOW.date().isoformat()) == {}


# ── 3. the fingerprint cache ────────────────────────────────────────────────────

def test_an_unchanged_sample_is_carried_forward_without_rescoring(conn, monkeypatch):
    seen = []
    _stub(monkeypatch, {"vol_window": 26}, key="11:2026-07-30", counter=seen)
    psel.refresh(conn, asof=_NOW - timedelta(days=1), series=["KXWTIW"], log=None)
    psel.refresh(conn, asof=_NOW, series=["KXWTIW"], log=None)
    assert seen == ["KXWTIW"], "the second day must reuse, not rescore"
    row = conn.execute("SELECT * FROM param_selection WHERE day=?",
                       (_NOW.date().isoformat(),)).fetchone()
    assert json.loads(row["params_json"]) == {"vol_window": 26}
    assert json.loads(row["report_json"])["carried_forward"] is True


def test_a_new_scoreable_event_invalidates_the_cache(conn, monkeypatch):
    seen = []
    _stub(monkeypatch, {"vol_window": 26}, key="11:2026-07-30", counter=seen)
    psel.refresh(conn, asof=_NOW - timedelta(days=1), series=["KXWTIW"], log=None)
    monkeypatch.setattr(psel, "_sample_key", lambda *a, **k: "12:2026-08-03")
    psel.refresh(conn, asof=_NOW, series=["KXWTIW"], log=None)
    assert seen == ["KXWTIW", "KXWTIW"], "a moved fingerprint must force a rescore"


def test_a_second_run_on_the_same_day_reuses_rather_than_rescoring(conn, monkeypatch):
    """`refresh` is a step in a pipeline that can be re-run after a failure further down.
    Keying the cache on strictly-earlier days would make every same-day rerun pay the full
    scoring cost for an answer it already has."""
    seen = []
    _stub(monkeypatch, {"vol_window": 26}, key="11:2026-07-30", counter=seen)
    psel.refresh(conn, asof=_NOW, series=["KXWTIW"], log=None)
    psel.refresh(conn, asof=_NOW + timedelta(hours=6), series=["KXWTIW"], log=None)
    assert seen == ["KXWTIW"]


def test_force_rescores_even_when_the_sample_has_not_moved(conn, monkeypatch):
    """The fingerprint tracks the DATA, not the code. After a model change the stored
    verdict is stale in a way nothing else can see, so `--force` has to exist and work."""
    seen = []
    _stub(monkeypatch, {"vol_window": 26}, key="11:2026-07-30", counter=seen)
    psel.refresh(conn, asof=_NOW - timedelta(days=1), series=["KXWTIW"], log=None)
    psel.refresh(conn, asof=_NOW, series=["KXWTIW"], force=True, log=None)
    assert seen == ["KXWTIW", "KXWTIW"]


# ── wiring ──────────────────────────────────────────────────────────────────────

def test_pooled_series_are_derived_from_pools_and_never_hand_listed():
    from prediction_market_macro.research.param_wf import POOLS
    assert psel.POOLED_SERIES == {s: n for n, sp in POOLS.items() for s in sp["series"]}
    assert psel.POOLED_SERIES["KXNATGASW"] == "energy_fut"
    assert "KXAAAGASW" not in psel.POOLED_SERIES


def test_a_pool_members_sample_key_moves_when_either_member_moves(monkeypatch):
    """Pooled series are judged on the branch's combined events, so a NATGAS settle has to
    invalidate WTIW's cached verdict too. Fingerprinting only the series' own events would
    leave one member running on a verdict computed without half its sample."""
    fps = {"KXWTIW": "11:a", "KXNATGASW": "11:b"}
    monkeypatch.setattr(psel, "_fingerprint", lambda conn, s, before: fps[s])
    monkeypatch.setattr(psel, "_manual_stamp", lambda conn, s, before: "")
    before = psel._sample_key(None, "KXWTIW", _NOW)
    fps["KXNATGASW"] = "12:c"
    assert psel._sample_key(None, "KXWTIW", _NOW) != before


def test_the_sample_key_moves_when_a_manual_row_is_adopted(monkeypatch):
    """#198c. A `manual_params` write settles no event, so the fingerprint holds still —
    and every cache keyed on it alone (`walkforward._GateBook.params_for`, `refresh`'s
    daily carry-forward) kept serving the pre-adoption answer across the adoption. The
    key has to move on the WRITE, because that is when the answer moves."""
    stamp = {"v": ""}
    monkeypatch.setattr(psel, "_fingerprint", lambda conn, s, before: "11:a")
    monkeypatch.setattr(psel, "_manual_stamp", lambda conn, s, before: stamp["v"])
    at_defaults = psel._sample_key(None, "KXU3", _NOW)
    stamp["v"] = "2026-08-11T04:44:41+00:00"
    adopted = psel._sample_key(None, "KXU3", _NOW)
    stamp["v"] = "2026-08-22T09:13:53+00:00"
    readopted = psel._sample_key(None, "KXU3", _NOW)
    assert len({at_defaults, adopted, readopted}) == 3, \
        "each adoption is its own cache generation, re-adoption included"


def test_the_manual_stamp_is_the_row_in_force_not_the_newest(_now=_NOW):
    """The stamp inherits `manual_params`' PIT rule (#198b) — it has to, or a cache key
    computed for a pre-adoption simulated day would carry today's adoption instant and
    invalidate answers that were never stale."""
    import json
    import sqlite3
    from datetime import datetime, timezone
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE experiments(name TEXT, config_hash TEXT, series TEXT,
                 window TEXT, metrics_json TEXT, created_ts TEXT,
                 PRIMARY KEY(name, config_hash))""")
    for ts in ("2026-08-11T04:44:41+00:00", "2026-08-22T09:13:53+00:00"):
        c.execute("INSERT INTO experiments(name, config_hash, series, window,"
                  " metrics_json, created_ts) VALUES('manual_params',?,?,'live',?,?)",
                  (f"m:{ts}", "KXU3",
                   json.dumps({"active": True, "params": {"laplace": 1.0}}), ts))
    at = lambda d: psel._manual_stamp(                                 # noqa: E731
        c, "KXU3", datetime.fromisoformat(d).replace(tzinfo=timezone.utc))
    assert at("2026-08-05T16:00:00") == ""
    assert at("2026-08-15T16:00:00") == "2026-08-11T04:44:41+00:00"
    assert at("2026-08-26T16:00:00") == "2026-08-22T09:13:53+00:00"


def test_predict_all_hands_the_model_none_rather_than_an_empty_dict(monkeypatch):
    """`params={}` and `params=None` reach `{**DEFAULT_PARAMS, **(params or {})}` the same
    way today, but the models are not required to keep that equivalence and #119 must not
    change any prediction on a day the gate held. Pinned at the call site."""
    import prediction_market_macro.ops.predict_all as pa
    seen = {}

    class _P:
        dist = None
        inputs = {}
    monkeypatch.setattr(psel, "current", lambda conn, series, day=None: {})
    monkeypatch.setattr(pa, "_open_periods", lambda conn, s: [("26AUG", "2026-08-01")])

    def fake_fn(conn, now, key, series=None, params="MISSING"):
        seen[series] = params
        raise RuntimeError("stop here — the signature is what is under test")
    import importlib
    monkeypatch.setattr(importlib, "import_module",
                        lambda name: type("M", (), {"predict": staticmethod(fake_fn),
                                                    "predict_kxfed": staticmethod(fake_fn)}))
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    from prediction_market_macro.ingest.store import SCHEMA
    c.executescript(SCHEMA)
    pa.run(c, None)
    assert seen and set(seen.values()) == {None}


# ── 4. #196: a parameter set may not be adopted on evidence about another branch ──

def test_branch_parity_vetoes_an_adoption(conn, monkeypatch):
    """The DSR gate asks "did this set earn more dollars over the settled sample". It
    cannot ask whether that sample ran the code production runs — on KXAAAGASW the sample
    is 51/73 `damped_trend_fallback` while production is 100% `aaa_daily_anchor`, so a set
    tuned on it is tuned for a model that has never placed a bet. The veto is what stops
    that from reaching `predict_all`.
    """
    from prediction_market_macro.research import branch_parity as bp
    _stub(monkeypatch, {"vol_window": 26}, parity=False)
    monkeypatch.setattr(bp, "hist_branch_mix",
                        lambda c, s, **k: bp.mix_from_counts({"damped_trend_fallback": 51,
                                                              "aaa_daily_anchor": 4}))
    monkeypatch.setattr(bp, "live_branch_mix",
                        lambda c, s, **k: bp.mix_from_counts({"aaa_daily_anchor": 96}))
    psel.refresh(conn, asof=_NOW, series=["KXAAAGASW"], log=None)
    row = conn.execute("SELECT * FROM param_selection WHERE series='KXAAAGASW'").fetchone()
    assert row["adopted"] == 0 and json.loads(row["params_json"]) == {}
    rep = json.loads(row["report_json"])
    assert rep["vetoed"] == "branch_parity"
    assert "aaa_daily_anchor" in rep["reason"]
    # and the veto is loud: a silent {} is indistinguishable from the gate holding
    assert conn.execute("SELECT COUNT(*) c FROM alerts WHERE source='param_select'"
                        " AND message LIKE '%vetoed%'").fetchone()["c"] == 1
    # `predict_all` must see the defaults, not the vetoed set
    assert psel.current(conn, "KXAAAGASW", day=_NOW.date().isoformat()) == {}


def test_a_manual_override_is_not_the_selectors_to_veto(conn, monkeypatch):
    """The 2026-08-11 adoption was the user's explicit instruction over the DSR gate's
    objection (README §E). Silently substituting defaults for what a human adopted would
    be a production change nobody asked for; the finding belongs in the report."""
    from prediction_market_macro.research import branch_parity as bp
    _stub(monkeypatch, {"vol_window": 26},
          rep={"adopted": True, "chosen": "manual", "mode": "manual", "n_obs": None},
          parity=False)
    monkeypatch.setattr(bp, "hist_branch_mix",
                        lambda c, s, **k: bp.mix_from_counts({"fallback": 51}))
    monkeypatch.setattr(bp, "live_branch_mix",
                        lambda c, s, **k: bp.mix_from_counts({"live": 96}))
    psel.refresh(conn, asof=_NOW, series=["KXAAAGASW"], log=None)
    row = conn.execute("SELECT * FROM param_selection WHERE series='KXAAAGASW'").fetchone()
    assert json.loads(row["params_json"]) == {"vol_window": 26}
    assert row["adopted"] == 1
