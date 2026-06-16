"""Tests for the ET/PT schedule viewer (timezone boundary correctness)."""
from __future__ import annotations

import sqlite3

from prediction_market.ingest import store
from prediction_market.ops.schedule import load_fixtures


def _store_with_fixture(kickoff_utc: str, status="NS"):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, name in ((10, "Argentina"), (20, "Algeria")):
        store.upsert(c, "team", {"api_id": api, "name": name, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "round": "Group Stage - 1",
        "status_short": status, "home_api_id": 10, "away_api_id": 20, "kickoff_ts": kickoff_utc,
        "updated_at": store.utcnow()}, pk=["api_id"])
    return c


def test_utc_to_et_pt_and_date_boundary():
    # 01:00 UTC on 6/17 = 21:00 ET / 18:00 PT on Tue 6/16 (US date is the day before).
    c = _store_with_fixture("2026-06-17T01:00:00+00:00")
    fx = load_fixtures(conn=c)[0]
    assert fx.et.strftime("%Y-%m-%d %H:%M") == "2026-06-16 21:00"
    assert fx.pt.strftime("%Y-%m-%d %H:%M") == "2026-06-16 18:00"
    # Filtering by ET date 6/16 includes it; 6/17 does not.
    assert len(load_fixtures(conn=c, et_date="2026-06-16")) == 1
    assert len(load_fixtures(conn=c, et_date="2026-06-17")) == 0


def test_upcoming_filter():
    c = _store_with_fixture("2026-06-17T01:00:00+00:00", status="FT")
    assert load_fixtures(conn=c, upcoming=True) == []     # finished excluded
    assert len(load_fixtures(conn=c, upcoming=False)) == 1
