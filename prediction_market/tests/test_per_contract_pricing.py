"""Tests for the per-contract ¢ + milestone price-track feature (plan 18).

Covers the pure conversion layer, the historical-price samplers, the Kalshi
candlestick parser, the champion-cents venue mapping, milestone capture/idempotency,
the milestone export, and the PnL-report ¢ reconciliation — all offline (venue calls
are monkeypatched; no live network in the unit tests).
"""
from __future__ import annotations

import sqlite3

from prediction_market.ingest import store
from prediction_market.util.pricing import (
    to_cents, mid, mid_cents, quote_to_cents, model_cents, settle_cents, pnl_cents,
)
from prediction_market.util.price_history import price_at, kalshi_mid_series


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


# ── 1. pricing conversion layer ───────────────────────────────────────────────
def test_to_cents_and_none():
    assert to_cents(0.64) == 64.0
    assert to_cents(0.995) == 99.5
    assert to_cents(None) is None


def test_mid_falls_back_when_one_side_missing():
    assert mid(0.83, 0.82) == 0.825
    assert mid(None, 0.99) == 0.99      # deep ITM: no ask → fall back to bid
    assert mid(0.01, None) == 0.01
    assert mid(None, None) is None
    assert mid_cents(None, 0.99) == 99.0


def test_quote_to_cents_preserves_and_adds():
    q = {"home": {"ask": 0.83, "bid": 0.82}, "draw": {"ask": 0.14, "bid": 0.13}, "away": None}
    out = quote_to_cents(q)
    assert out["home"]["ask"] == 0.83          # original preserved (ADD ONLY)
    assert out["home"]["ask_c"] == 83.0 and out["home"]["bid_c"] == 82.0 and out["home"]["mid_c"] == 82.5
    assert out["away"] is None
    assert quote_to_cents(None) is None


def test_quote_to_cents_deep_itm_ask_none_falls_back_to_bid():
    # The exact bug: a deep in-the-money side has an empty opposite book → yes_ask=None,
    # but yes_bid is a valid price. mid_c must fall back to it (so the UI shows 99¢, not —).
    q = {"home": {"ask": None, "bid": 0.99}, "draw": {"ask": 0.01, "bid": None}, "away": {"ask": 0.01, "bid": None}}
    out = quote_to_cents(q)
    assert out["home"]["ask_c"] is None
    assert out["home"]["bid_c"] == 99.0
    assert out["home"]["mid_c"] == 99.0


def test_model_cents():
    assert model_cents({"home": 0.72, "draw": 0.18, "away": 0.10}) == {"home": 72.0, "draw": 18.0, "away": 10.0}
    assert model_cents(None) is None


def test_settle_and_pnl_cents():
    assert settle_cents("home", "home") == 100.0
    assert settle_cents("home", "away") == 0.0
    assert settle_cents("home", None) is None
    assert pnl_cents(64.0, True) == 36.0
    assert pnl_cents(64.0, False) == -64.0
    assert pnl_cents(None, True) is None


# ── 2. historical-price samplers ──────────────────────────────────────────────
def test_price_at_nearest_bar_and_gap():
    series = [{"ts": 1000, "price": 0.60}, {"ts": 1300, "price": 0.62}, {"ts": 1600, "price": 0.55}]
    v, ts = price_at(series, 1290, key="price")
    assert v == 0.62 and ts == 1300
    # nearest bar 700s away > 900s? no — within gap; but a far target rejects:
    v2, ts2 = price_at(series, 100000, key="price", max_gap_s=900)
    assert v2 is None and ts2 is None
    assert price_at([], 1000) == (None, None)


def test_kalshi_mid_series_adds_mid():
    bars = [{"ts": 1, "ask": 0.70, "bid": 0.68}, {"ts": 2, "ask": None, "bid": 0.99}]
    out = kalshi_mid_series(bars)
    assert out[0]["mid"] == 0.69
    assert out[1]["mid"] == 0.99   # ask None → bid


# ── 3. Kalshi candlestick parser (offline) ────────────────────────────────────
def test_kalshi_candlestick_parser(monkeypatch):
    from prediction_market.venues.kalshi.market_data import KalshiMarketData
    payload = {"candlesticks": [
        {"end_period_ts": 1781639760, "yes_ask": {"close_dollars": "0.0700"},
         "yes_bid": {"close_dollars": "0.0600"}, "price": {"close_dollars": "0.0650"}, "volume_fp": "12.0"},
        {"end_period_ts": 1781639820, "yes_ask": {}, "yes_bid": {"close_dollars": "0.9900"},
         "price": {"previous_dollars": "0.9800"}},
    ]}
    md = KalshiMarketData()
    monkeypatch.setattr(md, "_get", lambda path, params=None: payload)
    bars = md.candlesticks("KXWCGAME", "KXWCGAME-X-ARG", 0, 1, period_interval=1)
    assert bars[0] == {"ts": 1781639760, "ask": 0.07, "bid": 0.06, "last": 0.065, "vol": 12.0}
    assert bars[1]["ts"] == 1781639820 and bars[1]["ask"] is None and bars[1]["bid"] == 0.99


# ── 4. champion-cents venue mapping (offline) ─────────────────────────────────
def test_champion_cents_maps_both_venues(monkeypatch):
    from prediction_market.venues import champion_prices as cp
    monkeypatch.setattr(cp, "_kalshi_champ_cents", lambda: {"france": 16.0, "spain": 15.0})
    monkeypatch.setattr(cp, "_poly_champ_cents", lambda: {"france": 18.4, "brazil": 6.7})
    cc = cp.champion_cents()
    assert cc["france"] == {"kalshi_c": 16.0, "poly_c": 18.4}
    assert cc["spain"] == {"kalshi_c": 15.0}
    assert cc["brazil"] == {"poly_c": 6.7}


def test_champion_cents_tolerates_venue_failure(monkeypatch):
    from prediction_market.venues import champion_prices as cp
    def boom():
        raise RuntimeError("venue down")
    monkeypatch.setattr(cp, "_kalshi_champ_cents", boom)
    monkeypatch.setattr(cp, "_poly_champ_cents", lambda: {"france": 18.4})
    cc = cp.champion_cents()   # must not raise
    assert cc == {"france": {"poly_c": 18.4}}


# ── 5. milestone capture + idempotency ────────────────────────────────────────
def _seed_match(c, fid=1, hi="argentina", ai="algeria"):
    c.execute("INSERT INTO team(api_id,name) VALUES (10,'Argentina'),(11,'Algeria')")
    c.execute("INSERT INTO team_meta(api_id,canonical_team_id) VALUES (10,?),(11,?)", (hi, ai))
    c.execute("INSERT INTO fixture(api_id,home_api_id,away_api_id,kickoff_ts,status_short,home_goals,away_goals,round) "
              "VALUES (?,?,?,?,?,?,?,?)", (fid, 10, 11, "2026-06-16T19:00:00+00:00", "2H", 3, 0, "Group Stage - 1"))
    c.commit()


def test_capture_milestones_grace_window_and_idempotent():
    # GRACE window: at minute 30 we capture ONLY T30 (within [30,38]) — NOT T15, which
    # is long past (a late first poll must not back-stamp T15 with stale minute-30 data;
    # backfill fills missed milestones accurately from venue history).
    from prediction_market.ops.live_refresh import _capture_milestones
    c = _mem_db(); _seed_match(c)
    inplay = {"matches": [{
        "fixture_id": 1, "minute": 30, "status": "2H", "score": "1-0",
        "model": {"home": 0.8, "draw": 0.15, "away": 0.05},
        "prices": {"kalshi": {"home": {"ask": 0.7, "bid": 0.69}, "draw": {"ask": 0.2, "bid": 0.19}, "away": {"ask": 0.11, "bid": 0.1}},
                   "poly_us": {"home": {"ask": 0.71, "bid": 0.7}}},
    }]}
    n = _capture_milestones(c, inplay)
    assert n == 1
    got = {r["milestone"] for r in c.execute("SELECT milestone FROM milestone_snapshot").fetchall()}
    assert got == {"T30"}          # T15 NOT back-stamped with stale data
    assert _capture_milestones(c, inplay) == 0   # idempotent at the same minute
    t30 = c.execute("SELECT * FROM milestone_snapshot WHERE milestone='T30'").fetchone()
    assert t30["p_model_home"] == 0.8 and t30["kalshi_home_ask"] == 0.7 and t30["poly_home_ask"] == 0.71


def test_capture_milestones_continuous_polling_captures_each():
    # Polling continuously through the match captures each milestone in its grace window.
    from prediction_market.ops.live_refresh import _capture_milestones
    c = _mem_db(); _seed_match(c)
    base = {"fixture_id": 1, "status": "2H", "score": "0-0", "model": {"home": 0.8, "draw": 0.15, "away": 0.05}, "prices": {}}
    for minute in (16, 31, 61, 76):     # near T15 / T30 / T60 / T75
        _capture_milestones(c, {"matches": [{**base, "minute": minute}]})
    _capture_milestones(c, {"matches": [{**base, "minute": 45, "status": "HT"}]})   # HT by status
    got = {r["milestone"] for r in c.execute("SELECT milestone FROM milestone_snapshot").fetchall()}
    assert got == {"T15", "T30", "T60", "T75", "HT"}


def test_capture_milestones_ht_uses_status():
    from prediction_market.ops.live_refresh import _capture_milestones
    c = _mem_db(); _seed_match(c)
    inplay = {"matches": [{"fixture_id": 1, "minute": 45, "status": "HT", "score": "1-0",
                           "model": {"home": 0.8, "draw": 0.15, "away": 0.05}, "prices": {}}]}
    _capture_milestones(c, inplay)
    got = {r["milestone"] for r in c.execute("SELECT milestone FROM milestone_snapshot").fetchall()}
    # HT captured by status; T15/T30 are past their grace windows → left for backfill.
    assert got == {"HT"}


# ── 6. milestone export shape + MTM ───────────────────────────────────────────
def test_milestone_export_builds_marks_and_mtm():
    from prediction_market.ops import milestone_export
    c = _mem_db(); _seed_match(c)
    c.execute("UPDATE fixture SET status_short='FT' WHERE api_id=1")   # settled home win 3-0
    # PRE + FT rows for a France-like home win (use argentina here; result=home, won)
    # Home (Argentina) priced clearly cheap → unambiguous home VALUE pick, so the test
    # is deterministic regardless of the draw-discipline policy (draw_extra_theta).
    c.execute("INSERT INTO milestone_snapshot(fixture_api_id,milestone,elapsed,home_goals,away_goals,"
              "poly_home_ask,poly_draw_ask,poly_away_ask,price_source) VALUES "
              "(1,'PRE',0,0,0,0.40,0.30,0.30,'candlestick'),"
              "(1,'FT',90,3,0,1.0,0.0,0.0,'candlestick')")
    c.commit()
    doc = milestone_export.build(conn=c)
    assert doc["n"] == 1
    m = doc["matches"][0]
    assert m["settled"] is True and m["result"] == "home" and m["score"] == "3-0"
    # marks carry ¢ (poly_c = price×100)
    pre = next(x for x in m["marks"] if x["milestone"] == "PRE")
    assert pre["poly_c"]["home"] == 40.0
    # our_bet pick is the model's pick; mtm present + sign consistent with the result
    assert m["our_bet"]["side"] in ("home", "draw", "away")
    assert m["mtm"] is not None
    if m["our_bet"]["side"] == "home":
        assert m["mtm"]["won"] is True and m["mtm"]["pnl_c"] > 0 and m["mtm"]["path_direction"] == "converging"


# ── 7. PnL report ¢ reconciliation ────────────────────────────────────────────
def test_bet_log_cents_reconciliation():
    from prediction_market.ops import performance_report as pr
    c = _mem_db()
    # Two settled group matches with real canonical teams + a PRE entry ¢ each.
    c.execute("INSERT INTO team(api_id,name) VALUES (10,'France'),(11,'Senegal'),(12,'Brazil'),(13,'Morocco')")
    c.execute("INSERT INTO team_meta(api_id,canonical_team_id) VALUES (10,'france'),(11,'senegal'),(12,'brazil'),(13,'morocco')")
    c.execute("INSERT INTO fixture(api_id,home_api_id,away_api_id,kickoff_ts,status_short,home_goals,away_goals,round) VALUES "
              "(1,10,11,'2026-06-16T19:00:00+00:00','FT',3,1,'Group Stage - 1'),"
              "(2,12,13,'2026-06-13T19:00:00+00:00','FT',0,1,'Group Stage - 1')")
    c.execute("INSERT INTO milestone_snapshot(fixture_api_id,milestone,poly_home_ask,poly_draw_ask,poly_away_ask) VALUES "
              "(1,'PRE',0.655,0.215,0.135),(2,'PRE',0.58,0.25,0.30)")
    c.commit()
    rep = pr.build(conn=c)
    assert rep.n_settled == 2
    log = rep.bet_log
    assert log, "expected bet rows for settled matches"
    cum = 0.0
    for b in log:
        assert b["entry_cents"] is not None and b["entry_source"] == "poly"
        expect = (100.0 - b["entry_cents"]) if b["won"] else (-b["entry_cents"])
        assert abs(b["pnl_cents"] - round(expect, 1)) < 0.05
        cum = round(cum + b["pnl_cents"], 1)
        assert abs(b["cum_pnl_cents"] - cum) < 0.05
    # headline metrics consistent with the rows
    assert abs(rep.pnl_cents_total - round(sum(b["pnl_cents"] for b in log), 1)) < 0.05
    ent = [b["entry_cents"] for b in log]
    assert abs(rep.avg_entry_cents - round(sum(ent) / len(ent), 1)) < 0.05


def test_match_pick_unsettled_returns_none():
    # A not-yet-settled fixture (null goals) must yield no pick (so milestone_export
    # won't crash on live/upcoming matches that only have a PRE row).
    from prediction_market.ops.performance_report import match_pick
    fx = {"round": "Group Stage - 1", "home_goals": None, "away_goals": None, "raw_json": None}
    assert match_pick(None, None, "france", "senegal", fx) is None


def test_pricetrack_and_betlog_reconcile():
    # The crux: PriceTrack (milestone_export) and the production bet log
    # (performance_report) must agree on pick + won for EVERY settled match — they now
    # share performance_report.match_pick, so this holds for group AND knockout.
    from prediction_market.ops import performance_report as pr, milestone_export
    c = _mem_db()
    c.execute("INSERT INTO team(api_id,name) VALUES (10,'France'),(11,'Senegal'),(12,'Brazil'),(13,'Morocco')")
    c.execute("INSERT INTO team_meta(api_id,canonical_team_id) VALUES (10,'france'),(11,'senegal'),(12,'brazil'),(13,'morocco')")
    # group: France beat Senegal 3-1; knockout: Brazil v Morocco level 1-1, Brazil through (winner flag)
    ko_json = '{"teams":{"home":{"winner":true},"away":{"winner":false}}}'
    c.execute("INSERT INTO fixture(api_id,home_api_id,away_api_id,kickoff_ts,status_short,home_goals,away_goals,round,raw_json) VALUES "
              "(1,10,11,'2026-06-16T19:00:00+00:00','FT',3,1,'Group Stage - 1',NULL),"
              "(2,12,13,'2026-07-05T19:00:00+00:00','AET',1,1,'Round of 16',?)", (ko_json,))
    c.execute("INSERT INTO milestone_snapshot(fixture_api_id,milestone,poly_home_ask,poly_draw_ask,poly_away_ask) VALUES "
              "(1,'PRE',0.655,0.215,0.135),(2,'PRE',0.62,0.0,0.38)")
    c.commit()
    bl = {(b["home"], b["away"]): (b["pick"], b["won"]) for b in pr.build(conn=c).bet_log}
    mm = {(m["home"]["name"], m["away"]["name"]): (m["our_bet"]["side"], (m["mtm"] or {}).get("won"))
          for m in milestone_export.build(conn=c)["matches"] if m.get("settled")}
    assert bl and mm
    for k in set(bl) | set(mm):
        assert bl.get(k) == mm.get(k), f"reconcile fail {k}: betlog {bl.get(k)} vs pricetrack {mm.get(k)}"
