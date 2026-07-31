"""M0 acceptance tests (PLAN §14 M0). ALL tests use tmp_path — never the production db."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.config.registry import REGISTRY, p0
from prediction_market_macro.ingest import calendars as cal
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler
from prediction_market_macro.model.common import (Categorical, GaussianMix, Pred, brier,
                                                  grid_pmf, leg_fair, pred_to_row, survival)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "test.db")


# ── registry (PLAN 铁律2) ────────────────────────────────────────────────────
def test_registry_complete():
    assert len(p0()) == 10
    for s in REGISTRY.values():
        assert s.settle_source.strip(), s.ticker
        assert s.structure in ("ladder", "categorical", "binary")
        assert s.calendar in cal.CALENDARS
        assert s.round_rule > 0


# ── calendars: DST correctness (§5-bis.4-5) ──────────────────────────────────
def test_calendar_dst_correct():
    evs = {e.period: e for e in cal.CALENDARS["BLS_CPI"]}
    # summer (EDT, UTC-4): Aug 12 08:30 ET == 12:30 UTC
    assert evs["2026-07"].scheduled_ts.hour == 12 and evs["2026-07"].scheduled_ts.minute == 30
    # winter (EST, UTC-5): Dec 10 08:30 ET == 13:30 UTC
    assert evs["2026-11"].scheduled_ts.hour == 13
    # FOMC statement 14:00 ET
    fomc = {e.period: e for e in cal.CALENDARS["FOMC"]}
    assert fomc["2026-09"].scheduled_ts.hour == 18          # EDT
    assert fomc["2026-12"].scheduled_ts.hour == 19          # EST


def test_claims_thanksgiving_shift():
    dates = [e.period for e in cal.CALENDARS["DOL_CLAIMS"]]
    assert "2026-11-25" in dates and "2026-11-26" not in dates


# ── scheduler: materialize + watchdog canary (M0 验收) ───────────────────────
def test_materialize_and_watchdog_canary(conn):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    n = scheduler.materialize(conn, now=now, horizon_days=30)
    assert n > 50
    rows = conn.execute("SELECT * FROM runs WHERE series='KXCPI' AND period='2026-07'").fetchall()
    tasks = {r["task"] for r in rows}
    assert {"arm", "decide", "freeze", "reassess", "reconcile"} <= tasks
    # canary: an overdue decide task must be flagged MISSED, never re-queued
    conn.execute("INSERT INTO runs(lane, series, period, task, due_ts) VALUES(?,?,?,?,?)",
                 ("release_day", "KXTEST", "2026-07", "decide",
                  (now - timedelta(hours=2)).isoformat()))
    conn.commit()
    missed = scheduler.watchdog(conn, now=now)
    assert any(m.get("series") == "KXTEST" for m in missed)
    st = conn.execute("SELECT status FROM runs WHERE series='KXTEST'").fetchone()["status"]
    assert st == "MISSED"
    assert conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"] >= 1


def test_pred_freshness_sla(conn):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn.execute("INSERT INTO contracts(ticker, series, event_ticker, period, status,"
                 " first_seen_ts) VALUES('T1','KXCPI','KXCPI-26JUL','26JUL','active',?)",
                 (now.isoformat(),))
    conn.commit()
    missed = scheduler.watchdog(conn, now=now)
    assert any(m.get("task") == "pred_sla" for m in missed)


# ── ladder math (§18) ────────────────────────────────────────────────────────
def test_grid_pmf_and_strict_semantics():
    d = GaussianMix(((1.0, 0.20, 0.12),))          # CPI-like: mean 0.20, sd 0.12
    pmf = grid_pmf(d, 0.1)
    assert abs(sum(pmf.values()) - 1.0) < 1e-9
    p_eq_02 = pmf.get(0.2, 0.0)
    assert p_eq_02 > 0.25                           # mass on the grid point is large
    gt = survival(pmf, 0.2, strict=True)
    gte = survival(pmf, 0.2, strict=False)
    assert abs(gte - gt - p_eq_02) < 1e-9           # the strict/>= gap IS P(print==strike)
    # leg_fair strike types
    assert abs(leg_fair(pmf, "greater", 0.2, None) - gt) < 1e-12
    assert abs(leg_fair(pmf, "greater_or_equal", 0.2, None) - gte) < 1e-12
    b = leg_fair(pmf, "between", 0.1, 0.3)          # P(0.1 <= x < 0.3) per between convention
    assert 0 < b < 1


def test_pred_online_pit_assertion():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    d = Categorical({"a": 0.6, "b": 0.4})
    ok = Pred("KXFEDDECISION", "2026-09", d, now, "fed/0.1.0", {}, now - timedelta(hours=1))
    pred_to_row(ok, None)                            # fine
    bad = Pred("KXFEDDECISION", "2026-09", d, now, "fed/0.1.0", {},
               now + timedelta(seconds=1))
    with pytest.raises(AssertionError):
        pred_to_row(bad, None)                       # data_horizon > asof must raise


def test_brier():
    assert abs(brier({"h": 1.0, "d": 0.0, "a": 0.0}, "h")) < 1e-12
    assert abs(brier({"h": 1 / 3, "d": 1 / 3, "a": 1 / 3}, "h") - 2 / 3) < 1e-9
