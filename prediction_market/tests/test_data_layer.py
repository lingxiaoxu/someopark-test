"""Tests for the soccer data layer (plan 02, 11) — store, budget guard, parsing.

No network: uses a temp SQLite connection and sample API-Football payloads.
"""
from __future__ import annotations

import sqlite3

import pytest

from prediction_market.config import SoccerConfig
from prediction_market.ingest import store
from prediction_market.ingest.api_football import ApiFootball, BudgetExceededError
from prediction_market.ingest.soccer_ingest import _event_rows, _fixture_row


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


# ── store ────────────────────────────────────────────────────────────────────
def test_upsert_is_idempotent(conn):
    row = {"api_id": 99, "name": "Testland", "national": 1, "updated_at": store.utcnow()}
    store.upsert(conn, "team", row, pk=["api_id"])
    store.upsert(conn, "team", {**row, "name": "Testland FC"}, pk=["api_id"])  # update, not insert
    rows = conn.execute("SELECT name FROM team WHERE api_id=99").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Testland FC"


def test_watermark_freshness(conn):
    assert store.is_fresh(conn, "fixtures", 3600) is False
    store.set_watermark(conn, "fixtures", note="x")
    assert store.is_fresh(conn, "fixtures", 3600) is True
    assert store.is_fresh(conn, "fixtures", 0) is False  # ttl 0 → never fresh


def test_monthly_count_excludes_status(conn):
    store.log_api_call(conn, "status", {}, http_status=200, results_count=0, req_remaining=None, req_limit=None)
    store.log_api_call(conn, "fixtures", {"league": 1}, http_status=200, results_count=72, req_remaining=10, req_limit=7500)
    assert store.monthly_request_count(conn) == 1  # status not billed


def test_params_hash_deterministic():
    assert store.params_hash({"a": 1, "b": 2}) == store.params_hash({"b": 2, "a": 1})
    assert store.params_hash({"a": 1}) != store.params_hash({"a": 2})


# ── budget guard ─────────────────────────────────────────────────────────────
def test_budget_guard_blocks_over_monthly(conn, monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "dummy")
    cfg = SoccerConfig(monthly_budget=2, max_requests_per_run=10)
    api = ApiFootball(conn, cfg)
    for _ in range(2):  # fill the monthly budget with billed calls
        store.log_api_call(conn, "fixtures", {}, http_status=200, results_count=1, req_remaining=1, req_limit=1)
    with pytest.raises(BudgetExceededError, match="monthly"):
        api._check_budget(billed=True)


def test_budget_guard_blocks_per_run_cap(conn, monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "dummy")
    cfg = SoccerConfig(monthly_budget=7000, max_requests_per_run=1)
    api = ApiFootball(conn, cfg)
    api._run_count = 1  # already at the per-run cap
    with pytest.raises(BudgetExceededError, match="per-run"):
        api._check_budget(billed=True)


# ── parsing (API-Football envelope → store rows) ─────────────────────────────
SAMPLE_FIXTURE = {
    "fixture": {"id": 1300, "date": "2026-06-13T18:00:00+00:00",
                "status": {"short": "FT", "long": "Match Finished", "elapsed": 90},
                "venue": {"name": "MetLife Stadium", "city": "East Rutherford"}},
    "league": {"id": 1, "season": 2026, "round": "Group Stage - 1"},
    "teams": {"home": {"id": 2384, "name": "USA", "logo": "u.png"},
              "away": {"id": 2385, "name": "Paraguay", "logo": "p.png"}},
    "goals": {"home": 4, "away": 1},
    "events": [
        {"time": {"elapsed": 23, "extra": None}, "team": {"id": 2384},
         "player": {"id": 50, "name": "C. Pulisic"}, "assist": {"id": 51},
         "type": "Goal", "detail": "Normal Goal", "comments": None},
    ],
}


def test_fixture_row_extraction():
    row = _fixture_row(SAMPLE_FIXTURE)
    assert row["api_id"] == 1300
    assert row["status_short"] == "FT"
    assert row["home_api_id"] == 2384 and row["away_api_id"] == 2385
    assert row["home_goals"] == 4 and row["away_goals"] == 1
    assert row["round"] == "Group Stage - 1"
    assert row["venue_name"] == "MetLife Stadium"


def test_event_rows_extraction():
    rows = _event_rows(1300, SAMPLE_FIXTURE["events"])
    assert len(rows) == 1
    e = rows[0]
    assert e["fixture_api_id"] == 1300 and e["seq"] == 0
    assert e["type"] == "Goal" and e["minute"] == 23
    assert e["player_api_id"] == 50 and e["assist_api_id"] == 51


def test_store_detailed_roundtrip(conn):
    from prediction_market.ingest.soccer_ingest import _store_detailed
    _store_detailed(conn, SAMPLE_FIXTURE)
    assert conn.execute("SELECT home_goals FROM fixture WHERE api_id=1300").fetchone()["home_goals"] == 4
    assert conn.execute("SELECT COUNT(*) c FROM fixture_event WHERE fixture_api_id=1300").fetchone()["c"] == 1
    # Idempotent: re-storing the same item does not duplicate events.
    _store_detailed(conn, SAMPLE_FIXTURE)
    assert conn.execute("SELECT COUNT(*) c FROM fixture_event WHERE fixture_api_id=1300").fetchone()["c"] == 1


def test_prediction_percent_parse_and_store_tables(conn):
    from prediction_market.ingest.soccer_ingest import _pct
    assert _pct("45%") == 0.45
    assert _pct("100%") == 1.0
    assert _pct(None) is None and _pct("n/a") is None
    # new plan-05 tables exist and venue registry seeds.
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"venue", "xref", "ob_snapshot", "xv_spread", "model_run",
            "sim_champion", "sim_golden_boot", "signal", "prediction", "match_odds"} <= tables
    assert conn.execute("SELECT COUNT(*) n FROM venue").fetchone()["n"] == 3


def test_monitor_levels_and_runs(conn):
    from prediction_market.ops.monitor import _level, _age_hours, health_report
    assert _level(0.5, 0.8, 0.95) == "OK"
    assert _level(0.85, 0.8, 0.95) == "WARN"
    assert _level(0.99, 0.8, 0.95) == "ALERT"
    assert _age_hours(None) is None
    # health_report runs on an empty store and flags missing model run.
    rep = health_report(conn=conn)
    names = {c.name: c.level for c in rep.checks}
    assert names.get("model_freshness") == "ALERT"  # no model_run yet
    assert rep.worst in ("OK", "WARN", "ALERT")
