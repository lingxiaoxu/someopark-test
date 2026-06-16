"""Tests for the performance/P&L report and risk report (plan 03 §9, 04 §6, 05 §5)."""
from __future__ import annotations

import sqlite3

from prediction_market.ingest import store
from prediction_market.ops import (
    frontend_export,
    performance_report,
    risk_report,
    upcoming_export,
)


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


# ── performance / P&L ─────────────────────────────────────────────────────────
def test_performance_report_no_settled_matches():
    c = _mem_db()
    rep = performance_report.build(conn=c)
    assert rep.n_settled == 0
    assert rep.settled_signal_pnl == 0.0
    assert any("no settled" in n for n in rep.notes)


def test_performance_report_with_settled_match():
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 2, "away_goals": 0,
                 "updated_at": store.utcnow()}, pk=["api_id"])
    rep = performance_report.build(conn=c)
    assert rep.n_settled == 1
    assert rep.brier_uniform == round(2 / 3, 4)          # uniform baseline reference
    assert 0.0 <= rep.brier <= 2.0
    assert rep.n_settled_signals == 0


# ── risk ──────────────────────────────────────────────────────────────────────
def test_risk_report_gates_and_caps():
    c = _mem_db()
    rep = risk_report.build(conn=c)
    # Hard $1 cap always present + reported.
    assert rep.gates["hard_order_cap_usd"] == 1.0
    assert any("hard-capped at $1.00" in b for b in rep.blocked_summary)
    # No fills recorded → zero exposure.
    assert rep.exposure["open_positions"] == 0
    assert rep.exposure["total_at_risk_usd"] == 0.0
    # Prod balance never queried (standing rule).
    assert "not queried" in str(rep.venue_balances["kalshi_prod_usd"])
    # API budget present and within the monthly cap.
    assert 0 <= rep.api_budget["used"] <= rep.api_budget["cap"]


# ── PDF rendering (house style) ───────────────────────────────────────────────
def test_performance_pdf_renders(tmp_path):
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 2, "away_goals": 0,
                 "updated_at": store.utcnow()}, pk=["api_id"])
    rep = performance_report.build(conn=c)
    out = tmp_path / "perf.pdf"
    performance_report.build_pdf(rep, str(out), as_of="2026-06-16")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"


def test_risk_pdf_renders(tmp_path):
    c = _mem_db()
    rep = risk_report.build(conn=c)
    out = tmp_path / "risk.pdf"
    risk_report.build_pdf(rep, str(out), as_of="2026-06-16")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"


# ── frontend data contract ────────────────────────────────────────────────────
def test_frontend_export_contract():
    c = _mem_db()
    doc = frontend_export.build(conn=c, as_of="2026-06-16")
    # Every section the UI relies on must be present.
    for key in ("schema_version", "headline", "interfaces", "modes", "schedule",
                "inputs", "outputs", "value", "performance", "risk", "predictions"):
        assert key in doc, f"missing {key}"
    assert doc["interfaces"] and "command" in doc["interfaces"][0]
    assert "gates" in doc["risk"] and "n_settled" in doc["performance"]
    assert "upcoming" in doc["predictions"]


# ── upcoming cross-venue export ───────────────────────────────────────────────
def test_upcoming_venue_devig():
    q = {"home": {"ask": 0.07, "bid": 0.06}, "draw": {"ask": 0.13, "bid": 0.12},
         "away": {"ask": 0.81, "bid": 0.80}}
    p = upcoming_export._venue_devig(q)
    assert abs(p["home"] + p["draw"] + p["away"] - 1.0) < 1e-6   # de-vigged → sums to 1
    assert upcoming_export._venue_devig(None) is None
    assert upcoming_export._venue_devig({"home": {"ask": None}}) is None


def test_upcoming_best_buy_edge_and_lock():
    model = {"home": 0.55, "draw": 0.24, "away": 0.21}
    # Venue cheap on home → model 0.55 vs ask 0.30 = big buy edge.
    vq = {"home": {"ask": 0.30, "bid": 0.29}, "draw": {"ask": 0.25, "bid": 0.24},
          "away": {"ask": 0.50, "bid": 0.49}}
    be = upcoming_export._best_buy_edge(model, vq, "kalshi", theta=0.03)
    assert be["side"] == "home" and be["tradable"] is True
    # Lock arb: buy home cheap on kalshi (0.30) + sell home high bid on poly (0.41) → locks.
    kq = {"home": {"ask": 0.30, "bid": 0.29}}
    pq = {"home": {"ask": 0.42, "bid": 0.41}}
    lock = upcoming_export._lock_arb(kq, pq)
    assert lock["buy_venue"] == "kalshi" and lock["sell_venue"] == "poly_us"
    assert lock["net_lock"] > 0 and lock["tradable"] is True


def test_upcoming_build_db_only():
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "NS",
                 "home_api_id": 10, "away_api_id": 20, "kickoff_ts": "2026-06-17T19:00:00+00:00",
                 "round": "Group Stage - 1", "updated_at": store.utcnow()}, pk=["api_id"])
    rows = upcoming_export.build(limit=5, conn=c, with_venues=False)
    assert len(rows) == 1
    m = rows[0]
    assert m["home"]["id"] == "france" and m["away"]["id"] == "senegal"
    assert m["et_date"] == "2026-06-17"
    assert abs(m["model"]["home"] + m["model"]["draw"] + m["model"]["away"] - 1.0) < 0.02
    assert m["kalshi"] is None and m["poly_us"] is None   # venues skipped → genuinely None
