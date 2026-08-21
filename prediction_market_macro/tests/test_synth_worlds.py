"""synth/worlds — the writers that turn a generated path into a database production reads.

The round trip in `roundtrip()` is the real gate and it runs against the live db, so it
lives in research scripts, not here. What this file pins is everything the round trip
could NOT have caught: the branches it never exercises because production data happens not
to contain them (a `custom` leg, an at-the-money print on a `strict_gt=False` series, a
knowledge-time cutoff), and the invariants that would fail silently rather than loudly.

The settlement tests deserve their reason stated. A strictness error moves exactly one leg
— the at-the-money one — and that leg is where most of the PnL is decided, so it is both
the most consequential bug available here and the one least likely to look wrong in
aggregate. `strict_gt` is a rulebook fact stored per series in the registry, and this file
exists to make sure `settle_leg` keeps asking the registry instead of developing an opinion.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from prediction_market_macro.ingest import cleveland_nowcast as cleveland
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research.synth import worlds as W

UTC = timezone.utc


def _leg(ticker, st, floor=None, cap=None, **kw):
    return {"ticker": ticker, "strike_type": st, "floor_strike": floor,
            "cap_strike": cap, **kw}


# ── settlement ───────────────────────────────────────────────────────────────
def test_greater_at_the_money_follows_strict_gt_both_ways():
    """The one number that decides most events. `strict_gt` is per-series and rulebook-
    verified: KXCPI's "Above 3.0" settles NO on a 3.0 print, KXJOBLESSCLAIMS' settles YES."""
    leg = _leg("T", "greater", floor=3.0)
    assert W.settle_leg(leg, 3.0, strict_gt=True) == "no"
    assert W.settle_leg(leg, 3.0, strict_gt=False) == "yes"
    # away from the money the flag cannot matter, and must not
    for strict in (True, False):
        assert W.settle_leg(leg, 3.1, strict) == "yes"
        assert W.settle_leg(leg, 2.9, strict) == "no"


def test_greater_or_equal_ignores_strict_gt_because_its_name_is_the_rule():
    leg = _leg("T", "greater_or_equal", floor=250.0)
    assert W.settle_leg(leg, 250.0, strict_gt=True) == "yes"
    assert W.settle_leg(leg, 250.0, strict_gt=False) == "yes"


def test_less_and_between_settle_on_their_own_bounds():
    assert W.settle_leg(_leg("T", "less", cap=2.0), 1.9, True) == "yes"
    assert W.settle_leg(_leg("T", "less", cap=2.0), 2.0, True) == "no"
    b = _leg("T", "between", floor=1.0, cap=2.0)
    assert [W.settle_leg(b, y, True) for y in (0.9, 1.0, 1.5, 2.0, 2.1)] == \
        ["no", "yes", "yes", "yes", "no"]


def test_custom_and_null_strike_types_are_refused_not_guessed():
    """199 `custom` and 604 NULL legs exist in the book. Their rule lives in prose; a
    default here would put a wrong outcome into the sample with nothing to signal it."""
    for st in ("custom", None):
        with pytest.raises(ValueError, match="cannot settle strike_type"):
            W.settle_leg(_leg("T", st, floor=1.0), 1.0, True)


def test_a_bound_the_strike_type_needs_but_lacks_is_an_error():
    with pytest.raises(ValueError, match="no floor_strike"):
        W.settle_leg(_leg("T", "greater"), 1.0, True)
    with pytest.raises(ValueError, match="missing a bound"):
        W.settle_leg(_leg("T", "between", floor=1.0), 1.0, True)


def test_settle_event_takes_strictness_from_the_registry_not_its_caller():
    legs = [_leg("A", "greater", floor=3.0)]
    assert W.settle_event(legs, 3.0, "KXCPI")["A"] == "no"            # strict_gt True
    assert W.settle_event(legs, 3.0, "KXJOBLESSCLAIMS")["A"] == "yes"  # strict_gt False


def test_a_laddered_event_settles_yes_below_the_print_and_no_above():
    legs = [_leg(f"T{i}", "greater", floor=f) for i, f in enumerate((2.8, 2.9, 3.0, 3.1))]
    got = W.settle_event(legs, 2.95, "KXCPI")
    assert list(got.values()) == ["yes", "yes", "no", "no"]


# ── recovering the number the database does not store ────────────────────────
def test_implied_outcome_lands_inside_the_interval_the_legs_pin():
    legs = [dict(_leg("A", "greater", floor=2.8), result="yes"),
            dict(_leg("B", "greater", floor=2.9), result="yes"),
            dict(_leg("C", "greater", floor=3.0), result="no")]
    y = W._implied_outcome(legs, "KXCPI")
    assert 2.9 <= y < 3.0
    # the point of recovering it: it must reproduce the pattern it came from
    assert W.settle_event(legs, y, "KXCPI") == {"A": "yes", "B": "yes", "C": "no"}


def test_implied_outcome_opens_a_one_sided_ladder_by_one_round_rule_step():
    """Every leg YES pins only a lower bound. KXCPI rounds to 0.1, so the recovered value
    sits one tick above the highest strike rather than at an arbitrary distance."""
    legs = [dict(_leg("A", "greater", floor=2.8), result="yes")]
    assert W._implied_outcome(legs, "KXCPI") == pytest.approx(2.85)


def test_implied_outcome_raises_on_settlements_that_no_value_could_produce():
    """A finding about the data, not something to approximate around."""
    legs = [dict(_leg("A", "greater", floor=3.0), result="no"),
            dict(_leg("B", "greater", floor=2.8), result="yes"),
            dict(_leg("C", "less", cap=2.7), result="yes")]
    with pytest.raises(ValueError, match="inconsistent"):
        W._implied_outcome(legs, "KXCPI")


def test_implied_outcome_refuses_a_ladder_with_no_bounds_at_all():
    with pytest.raises(ValueError, match="pin no interval"):
        W._implied_outcome([dict(_leg("A", "custom"), result="yes")], "KXCPI")


# ── categorical markets are settled by category, never by threshold ──────────
def _fomc(*results):
    """KXFEDDECISION's real shape: five mutually-exclusive `custom` legs, no strikes."""
    cats = ("H26", "H25", "H0", "C25", "C26")
    return [dict(_leg(f"KXFEDDECISION-26JUN-{c}", "custom"), result=r)
            for c, r in zip(cats, results)]


def test_a_categorical_event_settles_on_the_ticker_suffix_not_a_strike():
    """KXFEDDECISION has no numeric strike anywhere — `settle_leg` cannot apply, and the
    category label is the same one `decide_all._structs_categorical` prices against."""
    got = W.settle_event(_fomc(*["no"] * 5), "H0", "KXFEDDECISION")
    assert got == {"KXFEDDECISION-26JUN-H26": "no", "KXFEDDECISION-26JUN-H25": "no",
                   "KXFEDDECISION-26JUN-H0": "yes", "KXFEDDECISION-26JUN-C25": "no",
                   "KXFEDDECISION-26JUN-C26": "no"}


def test_an_outcome_naming_no_leg_is_refused_not_settled_all_no():
    """An all-NO event would look like a perfectly valid observation and score as one."""
    with pytest.raises(ValueError, match="names no leg"):
        W.settle_event(_fomc(*["no"] * 5), "C50", "KXFEDDECISION")


def test_structure_decides_the_settlement_rule_and_the_registry_owns_structure():
    with pytest.raises(ValueError, match="needs a category label"):
        W.settle_event(_fomc(*["no"] * 5), 3.0, "KXFEDDECISION")
    with pytest.raises(ValueError, match="needs a numeric released value"):
        W.settle_event([_leg("A", "greater", floor=2.8)], "H0", "KXCPI")


def test_implied_outcome_reads_a_categorical_event_off_its_single_yes_leg():
    assert W._implied_outcome(_fomc("no", "no", "yes", "no", "no"),
                              "KXFEDDECISION") == "H0"


def test_implied_outcome_refuses_a_categorical_event_with_two_winners():
    with pytest.raises(ValueError, match="legs settled YES"):
        W._implied_outcome(_fomc("no", "yes", "yes", "no", "no"), "KXFEDDECISION")


# ── writing ──────────────────────────────────────────────────────────────────
def _db(tmp_path, name="w.db"):
    return init_db(tmp_path / name)


def _plan(**kw):
    base = dict(series="KXCPI", period="26AUG",
                legs=[_leg("A", "greater", floor=2.8), _leg("B", "greater", floor=3.0)],
                close_time=datetime(2026, 8, 12, 17, 0, tzinfo=UTC), outcome=2.9,
                book={"A": [(1_760_000_000, 0.7, 0.72)],
                      "B": [(1_760_000_000, 0.2, 0.24)]})
    base.update(kw)
    return W.EventPlan(**base)


def test_write_event_refuses_a_candle_at_the_404_sentinel(tmp_path):
    """`quotable_events` drops rows with `end_ts <= 100000`, so a world written under the
    sentinel is invisible to the very function meant to score it — and invisible looks
    exactly like "the strategy declined to trade"."""
    conn = _db(tmp_path)
    with pytest.raises(ValueError, match="sentinel"):
        W.write_event(conn, _plan(book={"A": [(50_000, 0.7, 0.72)]}))


def test_write_event_ladders_the_outcome_and_reports_it(tmp_path):
    conn = _db(tmp_path)
    got = W.write_event(conn, _plan())
    assert got["yes_legs"] == 1 and got["legs"] == 2 and got["candles"] == 2
    res = dict(conn.execute("SELECT ticker, result FROM settlements").fetchall())
    assert res == {"A": "yes", "B": "no"}


def test_write_event_prefers_each_legs_own_close_time(tmp_path):
    """10 of 808 real events disagree leg-to-leg, and `quotable_events` filters candles on
    each leg's own close — flattening them would make legs quotable production never quoted.
    """
    conn = _db(tmp_path)
    legs = [_leg("A", "greater", floor=2.8, close_time="2026-08-11T17:00:00+00:00"),
            _leg("B", "greater", floor=3.0)]
    W.write_event(conn, _plan(legs=legs))
    got = dict(conn.execute("SELECT ticker, close_time FROM contracts").fetchall())
    assert got["A"] == "2026-08-11T17:00:00+00:00"
    assert got["B"] == "2026-08-12T17:00:00+00:00"


def test_settled_ts_defaults_to_the_close_because_energy_reads_it_point_in_time(tmp_path):
    """`energy._aaa_settled_mids` builds its drift prior from `settled_ts <= asof`. A world
    that left it NULL, or stamped it early, would hand that model outcomes before they were
    knowable — a PIT leak with no symptom other than a suspiciously good energy model."""
    conn = _db(tmp_path)
    W.write_event(conn, _plan())
    got = {r[0] for r in conn.execute("SELECT settled_ts FROM settlements").fetchall()}
    assert got == {"2026-08-12T17:00:00+00:00"}
    W.write_event(conn, _plan(settle_time=datetime(2026, 8, 13, 12, 30, tzinfo=UTC)))
    got = {r[0] for r in conn.execute("SELECT settled_ts FROM settlements").fetchall()}
    assert got == {"2026-08-13T12:30:00+00:00"}


def test_write_fred_stamps_knowledge_time_at_the_measured_lag(tmp_path):
    """The PIT stamp is the whole point: without it every model reads a synthetic print the
    instant it exists, which is the leak `project_macro_replay_pit_fixes` records twice."""
    conn = _db(tmp_path)
    vals = pd.Series([100.0, 101.0],
                     index=pd.to_datetime(["2026-01-01", "2026-02-01"]))
    assert W.write_fred(conn, "SYNTH", vals, lag_days=21, hour=13) == 2
    rows = conn.execute("SELECT event_time, vintage_date, knowledge_time FROM fred_obs"
                        " ORDER BY event_time").fetchall()
    assert rows[0]["vintage_date"] == "2026-01-22"
    assert rows[0]["knowledge_time"] == "2026-01-22T13:00:00+00:00"
    assert rows[1]["knowledge_time"] == "2026-02-22T13:00:00+00:00"


def test_write_fut_fills_ohlc_from_the_close_it_was_given(tmp_path):
    conn = _db(tmp_path)
    W.write_fut(conn, "CL", pd.Series([61.5], index=pd.to_datetime(["2026-03-02"])))
    r = conn.execute("SELECT open, high, low, close FROM fut_daily").fetchone()
    assert tuple(r) == (61.5, 61.5, 61.5, 61.5)


# ── point-in-time truncation ─────────────────────────────────────────────────
def test_materialize_truncates_on_knowledge_time_not_event_time(tmp_path):
    """The distinction the module was written around. A CPI print for month T is knowable
    two weeks into T+1, so a world spliced at T must still carry the T-1 print published
    inside T. Cutting on event_time deletes the most recent data every model reads."""
    src = _db(tmp_path, "src.db")
    W.write_fred(src, "CPILFESL",
                 pd.Series([1.0, 2.0, 3.0],
                           index=pd.to_datetime(["2026-01-01", "2026-02-01",
                                                 "2026-03-01"])),
                 lag_days=14, hour=13)
    dst = W.materialize(src, tmp_path / "w.db",
                        cutoff=datetime(2026, 2, 20, tzinfo=UTC))
    got = [r[0] for r in dst.execute(
        "SELECT event_time FROM fred_obs ORDER BY event_time").fetchall()]
    # Jan (known 15 Jan) and Feb (known 15 Feb) survive; Mar (known 15 Mar) does not.
    assert got == ["2026-01-01", "2026-02-01"]


def test_materialize_clones_every_table_so_a_world_is_the_shape_production_reads(tmp_path):
    """Verbatim from `sqlite_master`, which is why `cleveland_nowcast` — created lazily by
    its own ingest module rather than by `init_db` — has to be carried too. A world that
    silently lacked it would score CPI on the fallback model production does not run."""
    src = _db(tmp_path, "src.db")
    cleveland.ensure_schema(src)
    dst = W.materialize(src, tmp_path / "w.db")
    have = {r[0] for r in dst.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert set(W._PIT_TABLES) <= have
    src_tables = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()}
    assert src_tables <= have


def test_verify_world_names_the_tables_that_came_back_empty(tmp_path):
    """Most carried tables fail GRACEFULLY when empty — no `preds` scores every candidate
    ungated and reports confident numbers. Emptiness has to be asserted, not assumed."""
    src = _db(tmp_path, "src.db")
    W.write_fred(src, "X", pd.Series([1.0], index=pd.to_datetime(["2026-01-01"])), 1, 13)
    src.execute("INSERT INTO preds(series, period, asof, model_version, dist_json,"
                " data_horizon, created_ts) VALUES('KXCPI','26JAN',"
                "'2026-01-02T00:00:00+00:00','v','{}','2026-01-02T00:00:00+00:00',"
                "'2026-01-02T00:00:00+00:00')")
    src.commit()
    dst = W.materialize(src, tmp_path / "w.db",
                        cutoff=datetime(2026, 1, 1, tzinfo=UTC))
    rep = W.verify_world(dst, src, cutoff=datetime(2026, 1, 1, tzinfo=UTC))
    assert rep["tables"]["fred_obs"] == {"world": 0, "source": 1}
    assert "preds" in rep["empty"] and "fred_obs" in rep["empty"]


# ── measurement, snapshots, rebuild ──────────────────────────────────────────
def test_publication_lag_is_measured_from_the_data_and_takes_the_median(tmp_path):
    """Tabulating lags by hand is what rots. One outlier vintage must not move the answer,
    which is why the median is used rather than the mean."""
    conn = _db(tmp_path)
    for i, extra in enumerate([0, 0, 0, 0, 90]):        # one re-benchmarking outlier
        ev = pd.Timestamp("2026-01-01") + pd.DateOffset(months=i)
        vi = (ev + timedelta(days=21 + extra)).date()
        conn.execute(
            "INSERT INTO fred_obs(sid, event_time, value, vintage_date, knowledge_time,"
            " first_seen_ts) VALUES('PAYEMS',?,1.0,?,?,?)",
            (ev.date().isoformat(), vi.isoformat(), f"{vi.isoformat()}T13:00:00+00:00",
             f"{vi.isoformat()}T13:00:00+00:00"))
    conn.commit()
    assert W.publication_lag(conn, "PAYEMS") == (21, 13)


def test_publication_lag_refuses_a_series_it_has_never_seen(tmp_path):
    with pytest.raises(ValueError, match="no fred_obs rows"):
        W.publication_lag(_db(tmp_path), "NOPE")


def test_snapshot_reads_through_the_wal_a_file_copy_would_miss(tmp_path):
    """`macro.db` is WAL with a live writer, so an uncheckpointed commit lives in the
    sidecar. A byte copy of the `.db` would drop it — intermittently, depending on when the
    last checkpoint landed, which is the worst possible failure mode for a gate."""
    src = _db(tmp_path, "src.db")
    src.execute("PRAGMA journal_mode=WAL")
    W.write_fred(src, "X", pd.Series([7.0], index=pd.to_datetime(["2026-01-01"])), 1, 13)
    out = W.snapshot(src, tmp_path / "snap.db")
    copy = sqlite3.connect(str(out))
    assert copy.execute("SELECT COUNT(*) FROM fred_obs").fetchone()[0] == 1


def test_rebuild_event_refuses_to_leave_a_real_leg_behind(tmp_path):
    """A world inherits the real ladder from the snapshot. If a plan names fewer legs than
    the event has, INSERT OR REPLACE would leave the others in place as real rows and
    `_legs_at` would quote them — the rebuilt event would not be the whole event."""
    conn = _db(tmp_path)
    W.write_event(conn, _plan())
    conn.execute("INSERT INTO contracts(ticker, series, event_ticker, period, strike_type,"
                 " floor_strike, close_time, status, first_seen_ts)"
                 " VALUES('C','KXCPI','KXCPI-26AUG','26AUG','greater',3.2,"
                 " '2026-08-12T17:00:00+00:00','settled','real')")
    conn.commit()
    partial = _plan(legs=[_leg("A", "greater", floor=2.8)])
    with pytest.raises(ValueError, match="not be the whole event"):
        W.rebuild_event(conn, partial)


def test_read_event_round_trips_through_write_event(tmp_path):
    """The narrow, db-free half of the S3 gate: a written event read back and written again
    must settle the same way. The full gate (`roundtrip`) demands `event_pnl` agree too, and
    needs the production database to say anything, so it lives in research scripts."""
    conn = _db(tmp_path)
    W.write_event(conn, _plan())
    plan = W.read_event(conn, "KXCPI", "26AUG")
    assert plan.close_time == datetime(2026, 8, 12, 17, 0, tzinfo=UTC)
    assert plan.event_ticker == "KXCPI-26AUG"
    assert set(plan.book) == {"A", "B"}
    before = dict(conn.execute("SELECT ticker, result FROM settlements").fetchall())
    W.rebuild_event(conn, plan)
    assert dict(conn.execute("SELECT ticker, result FROM settlements").fetchall()) == before


def test_read_event_refuses_an_event_with_no_settled_legs(tmp_path):
    with pytest.raises(ValueError, match="no settled legs"):
        W.read_event(_db(tmp_path), "KXCPI", "26AUG")
