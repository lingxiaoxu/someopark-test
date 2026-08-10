"""The displayed track record is the most externally-visible number this system produces,
and the history half of it is a derived artefact. These tests are about the four ways the
derivation could produce a plausible, wrong, public number.

  1. Wrong stream. `edge` and `hybrid` differ by several ROI points on any real run, and
     only `hybrid` matches what the live half counts. Nothing about the output shape would
     reveal the mistake.
  2. Overlapping window. Days counted once in the backtest and again in the live ledger
     inflate the combined figure, and the combined figure is the headline.
  3. Unbounded source. A run whose window is "the last 60 days" re-frozen a month later is
     a different segment wearing the same label.
  4. Ungated source. This one is not hypothetical: the first freeze took a `:pure` run —
     db-state gates off, fixed default params — and published +9.98% for a window that
     scores -6.84% under the gates production actually applies. Every other guard passed.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from prediction_market_macro.ops import freeze_track as ft

CUT = "2026-07-31T16:14:00+00:00"


def _trade(day, settle, staked, realized):
    return {"series": "KXWTIW", "period": settle, "day": day, "desc": "YES x @0.40",
            "fair": 0.5, "cost": 0.4, "count": 2, "staked": staked,
            "realized": realized, "won": realized > 0, "lead_days": 3.0,
            "settle": settle}


def _run(bounded=True, streams=None, **kw):
    # the defaults describe a PRODUCTION-shaped run: gates on, selector on, favourite
    # filter on. Anything else has to be asked for by the test that wants it.
    m = {"days": 60, "fair_mode": "model", "bounded": bounded,
         "window_start": "2026-06-01", "window_end": "2026-07-31",
         "db_gates": True, "select_params": True, "argmax_filter": True,
         "note": "db-state gates ON", **kw}
    m["streams"] = streams if streams is not None else {
        "edge": {"trades": [_trade("2026-06-01", "2026-06-04", 1.0, 0.5)]},
        "hybrid": {"trades": [_trade("2026-06-01", "2026-06-04", 1.0, 0.5),
                              _trade("2026-07-01", "2026-07-04", 3.0, -3.0)]},
    }
    return m


@pytest.fixture()
def conn():
    from prediction_market_macro.ingest.store import SCHEMA
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _store(conn, m, h="d60:model:end2026-07-31"):
    conn.execute("INSERT INTO experiments(name, config_hash, metrics_json, created_ts)"
                 " VALUES('daily_walkforward',?,?,?)", (h, json.dumps(m), "2026-08-04T15:11:06"))
    return h


# ── 1. the stream ───────────────────────────────────────────────────────────────

def test_the_default_stream_is_the_one_the_live_half_counts(conn):
    """`frontend_export` sums decisions of kind open|argmax|arb|snipe — both streams. If
    the default here were `edge`, the two halves of one displayed record would be two
    different strategies and the combined ROI would describe neither."""
    assert ft.STREAM == "hybrid"
    p = ft.build(conn, _store(conn, _run()), CUT)
    assert p["n_trades"] == 2, "the edge-only stream has 1 trade; hybrid has 2"
    assert p["staked"] == 4.0 and p["realized"] == -2.5
    assert "hybrid stream (the live rule)" in p["note"]


def test_a_missing_stream_is_an_error_not_an_empty_history(conn):
    h = _store(conn, _run(streams={"edge": {"trades": [_trade("2026-06-01", "2026-06-04",
                                                              1.0, 0.5)]}}))
    with pytest.raises(LookupError, match="hybrid"):
        ft.build(conn, h, CUT)


def test_an_empty_stream_is_refused_rather_than_shown_as_a_zero_trade_record(conn):
    h = _store(conn, _run(streams={"hybrid": {"trades": []}}))
    with pytest.raises(ValueError, match="0-trade"):
        ft.build(conn, h, CUT)


def test_a_capped_trade_list_is_refused_rather_than_frozen_as_a_partial_history(conn):
    """`walkforward._stream_summary` caps its list at 60 while its own n_trades counts the
    full run. Since every aggregate here is recomputed from the list, a capped source would
    export a perfectly self-consistent history that is simply missing its first trades —
    lower stake, different ROI, and no error on any surface."""
    m = _run()
    m["streams"]["hybrid"]["n_trades"] = 61      # ran 61, carries 2
    with pytest.raises(ValueError, match="capped its trade list"):
        ft.build(conn, _store(conn, m), CUT)


def test_a_source_without_an_n_trades_field_is_still_freezable(conn):
    """The guard keys off a field the hand-written rows predate; its absence must not turn
    into a refusal to freeze anything at all."""
    m = _run()
    m["streams"]["hybrid"].pop("n_trades", None)
    assert ft.build(conn, _store(conn, m), CUT)["n_trades"] == 2


# ── 2. overlap with the live segment ────────────────────────────────────────────

def test_a_history_running_past_the_cutover_is_refused(conn):
    m = _run()
    m["streams"]["hybrid"]["trades"].append(_trade("2026-08-01", "2026-08-03", 1.0, 1.0))
    with pytest.raises(ValueError, match="overlap"):
        ft.build(conn, _store(conn, m), CUT)


def test_a_history_ending_exactly_on_the_cutover_day_is_allowed(conn):
    m = _run()
    m["streams"]["hybrid"]["trades"].append(_trade("2026-07-30", "2026-07-31", 1.0, 1.0))
    assert ft.build(conn, _store(conn, m), CUT)["n_trades"] == 3


# ── 3. the source ───────────────────────────────────────────────────────────────

def test_an_unbounded_run_cannot_be_frozen(conn):
    with pytest.raises(ValueError, match="not a bounded run"):
        ft.build(conn, _store(conn, _run(bounded=False)), CUT)


def test_an_unknown_source_hash_is_an_error(conn):
    with pytest.raises(LookupError):
        ft.build(conn, "d60:model:nope", CUT)


# ── 4. the source ran the strategy production runs ──────────────────────────────

@pytest.mark.parametrize("flag", ["db_gates", "select_params", "argmax_filter"])
def test_a_run_that_switched_off_a_live_rule_cannot_be_frozen(conn, flag):
    """Each of these is a `--no-*` flag on the harness, and each makes the run measure a
    strategy that is wider than the one the ledger will fill against. The `:pure` case
    (db_gates+select_params off) is the one that actually reached the page."""
    with pytest.raises(ValueError, match="not the strategy production runs"):
        ft.build(conn, _store(conn, _run(**{flag: False})), CUT)


def test_the_pure_runs_missing_flags_read_as_off_not_as_unknown(conn):
    """`walkforward` writes `db_gates: null` rather than `false` when the flag is off, and
    the pre-#122 runs have no `select_params` key at all. Both must refuse."""
    m = _run()
    m["db_gates"], m["select_params"] = None, None
    with pytest.raises(ValueError, match="not the strategy production runs"):
        ft.build(conn, _store(conn, m), CUT)


def test_a_run_predating_the_argmax_filter_flag_is_still_freezable(conn):
    """Unlike the other two, `argmax_filter` was added long after the runs it describes,
    and every run stored before it had the filter ON. Absent must therefore mean True, or
    the guard would retroactively disqualify correct history."""
    m = _run()
    m.pop("argmax_filter")
    assert ft.build(conn, _store(conn, m), CUT)["n_trades"] == 2


def test_research_can_still_export_an_ungated_run_deliberately(conn):
    """The escape hatch is a keyword argument with no CLI flag: comparing arms is a real
    need, but it must not be one keystroke away from the command that updates the page."""
    p = ft.build(conn, _store(conn, _run(db_gates=None, select_params=None)), CUT,
                 allow_ungated=True)
    assert p["n_trades"] == 2


# ── arithmetic + provenance ─────────────────────────────────────────────────────

def test_the_aggregates_are_recomputed_from_the_trades_not_copied(conn):
    """The stream carries its own summary. Trusting it would propagate any upstream
    disagreement between the summary and the list the page actually renders."""
    m = _run()
    # n_trades stays honest here — it is the truncation tripwire, tested separately below.
    m["streams"]["hybrid"].update({"n_trades": 2, "staked": 1.0, "realized": 500.0,
                                   "roi": 5.0, "won": 999})
    p = ft.build(conn, _store(conn, m), CUT)
    assert (p["n_trades"], p["won"], p["staked"], p["realized"]) == (2, 1, 4.0, -2.5)
    assert p["roi"] == round(-2.5 / 4.0, 5)


def test_the_frozen_row_records_where_it_came_from(conn):
    h = _store(conn, _run())
    ft.freeze(conn, h, cutover=CUT)
    row = conn.execute("SELECT * FROM experiments WHERE name='track_history'").fetchone()
    assert row["config_hash"] == f"frozen:{h}"
    src = json.loads(row["metrics_json"])["source"]
    assert src["config_hash"] == h and src["stream"] == "hybrid"
    assert src["window"] == ["2026-06-01", "2026-07-31"]
    assert src["fair_mode"] == "model"


def test_freezing_twice_keeps_the_older_segment_auditable(conn):
    """`frontend_export` takes the newest by created_ts; what it replaced must still be
    readable, so a displayed number can always be traced to the run it came from."""
    ft.freeze(conn, _store(conn, _run()), cutover=CUT)
    ft.freeze(conn, _store(conn, _run(window_start="2026-05-01"), h="d90:model:x"),
              cutover=CUT)
    rows = conn.execute("SELECT config_hash FROM experiments WHERE name='track_history'"
                        " ORDER BY created_ts").fetchall()
    assert len(rows) == 2


def test_the_payload_keeps_the_shape_the_frontend_already_reads(conn):
    """The row this replaces was hand-written; the page reads these keys. Renaming or
    dropping one would blank the history panel with no error anywhere."""
    p = ft.build(conn, _store(conn, _run()), CUT)
    assert {"n_trades", "won", "win_rate", "staked", "realized", "roi", "span",
            "trades", "note"} <= set(p)
    assert p["span"] == ["2026-06-01", "2026-07-01"]
