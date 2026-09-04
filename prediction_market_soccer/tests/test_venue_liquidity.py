"""The order-book probe (ops/venue_liquidity).

It exists because ONE demo-book reading taken six hours before kickoff was turned into
"this competition has no liquidity, tonight will not fill" — and it filled twice, minutes
after the market maker posted. The probe replaces that guess with a measurement at fixed
kickoff-relative offsets, and these tests pin the two ways such a measurement can lie:
recording a bucket that is not due, and recording "no market" when the venue could not be
reached at all."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_soccer.ops import venue_liquidity as VL


def _conn():
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store._SCHEMA)
    return c


def _fixture(c, *, api_id: int, minutes_ahead: float, league_id: int = 61):
    ko = datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)
    c.execute("INSERT INTO fixture (api_id, league_id, season, home_api_id, away_api_id, kickoff_ts, "
              "status_short) VALUES (?,?,?,?,?,?,?)",
              (api_id, league_id, 2026, 1000 + api_id, 2000 + api_id, ko.isoformat(timespec="seconds"), "NS"))
    for t in (1000 + api_id, 2000 + api_id):
        c.execute("INSERT OR IGNORE INTO team_meta (api_id, canonical_team_id) VALUES (?,?)", (t, f"club{t}"))
    c.commit()


def test_bucket_is_due_only_inside_its_window():
    c = _conn()
    _fixture(c, api_id=1, minutes_ahead=1440)        # exactly T-24h
    _fixture(c, api_id=2, minutes_ahead=1000)        # between T-24h and T-12h → nothing due
    due = {d["fixture"]: d["bucket"] for d in VL._due_buckets(c)}
    assert due.get(1) == "T-24h"
    assert 2 not in due


def test_a_recorded_bucket_is_never_recorded_twice():
    c = _conn()
    _fixture(c, api_id=3, minutes_ahead=180)         # T-3h
    assert VL._due_buckets(c)
    c.execute("INSERT INTO venue_book_probe (fixture_api_id, comp, venue, side, bucket, ts) "
              "VALUES (3,'ligue1','demo','home','T-3h','x')")
    c.commit()
    assert not VL._due_buckets(c)


def test_far_future_and_started_fixtures_are_out_of_scope():
    c = _conn()
    _fixture(c, api_id=4, minutes_ahead=60 * 24 * 5)     # 5 days out
    _fixture(c, api_id=5, minutes_ahead=-30)            # kicked off 30 min ago
    assert not VL._due_buckets(c)


class _Tk:
    """Discovery that FAILED for 'ligue1' (no listing) but succeeded for 'epl'."""
    def __init__(self):
        self.failed = {"ligue1"}
        self._disc = {"epl": {"x": 1}}

    def for_match(self, comp, hi, ai):
        return None
    def index_ok(self, comp):
        return comp not in self.failed and bool(self._disc.get(comp))


def test_an_unreachable_listing_is_not_recorded_as_no_market(monkeypatch):
    """A rate-limited discovery call must leave the bucket unrecorded so the next cycle
    retries — writing a 'no market' row would freeze a 429 into the measurement."""
    c = _conn()
    _fixture(c, api_id=6, minutes_ahead=180, league_id=61)     # ligue1 → listing failed
    _fixture(c, api_id=7, minutes_ahead=180, league_id=39)     # epl → listing ok, no pairing
    monkeypatch.setattr(VL, "_due_buckets", lambda conn, **k: [
        {"fixture": 6, "comp": "ligue1", "home": 1006, "away": 2006, "kickoff": "x", "bucket": "T-3h", "minutes": 180.0},
        {"fixture": 7, "comp": "epl", "home": 1007, "away": 2007, "kickoff": "x", "bucket": "T-3h", "minutes": 180.0}])
    import prediction_market_soccer.exec.kalshi_mirror as KM
    monkeypatch.setattr(KM, "_Tickers", _Tk)
    monkeypatch.setattr(KM, "DemoBroker", lambda: (_ for _ in ()).throw(RuntimeError("no demo here")))
    out = VL.probe(c, include_prod=False)
    rows = {(r["fixture_api_id"], r["ticker"]) for r in c.execute("SELECT fixture_api_id, ticker FROM venue_book_probe")}
    assert (7, None) in rows, "a retrieved listing without the pairing IS the answer — record it"
    assert not any(f == 6 for f, _ in rows), "an unreachable listing must not be recorded"
    assert out.get("unreachable") == ["ligue1"]


def test_summary_reports_ask_and_bid_availability_separately():
    c = _conn()
    rows = [("demo", "home", 0.40, 0.49), ("demo", "draw", None, None), ("demo", "away", 0.10, 0.19)]
    for venue, side, bid, ask in rows:
        c.execute("INSERT INTO venue_book_probe (fixture_api_id, comp, venue, side, bucket, ticker, bid, ask, ts) "
                  "VALUES (8,'ligue1',?,?,'T-3h','TK',?,?,'x')", (venue, side, bid, ask))
    c.commit()
    t = {(r["comp"], r["venue"], r["bucket"]): r for r in VL.summary(c)["table"]}
    cell = t[("ligue1", "demo", "T-3h")]
    assert cell["n"] == 3 and cell["ask_pct"] == 67 and cell["bid_pct"] == 67
    assert cell["median_spread_c"] == pytest.approx(9.0)     # entry cost, not a rounding artefact


def test_buckets_are_ordered_and_non_overlapping():
    centres = [c for _l, c, _h in VL.BUCKETS]
    assert centres == sorted(centres, reverse=True)
    for (l1, c1, h1), (l2, c2, h2) in zip(VL.BUCKETS, VL.BUCKETS[1:]):
        assert c1 - h1 > c2 + h2, f"{l1} and {l2} windows overlap — a fixture could land in both"


def test_the_live_loop_probes_before_the_match_window_check():
    """The far-out buckets fall on days with no matches, so the probe must run before the
    loop's early return."""
    import inspect
    from prediction_market_soccer.ops import live_refresh
    src = inspect.getsource(live_refresh.refresh_once)
    assert src.index("venue_liquidity.probe(conn)") < src.index("if not _in_match_window(conn):")


def test_a_failed_orderbook_request_is_marked_and_excluded_from_the_summary(monkeypatch):
    """A request that failed and a book that is genuinely empty both leave every price NULL
    — and this is the table whose whole purpose is telling those two apart."""
    c = _conn()
    _fixture(c, api_id=20, minutes_ahead=180)
    monkeypatch.setattr(VL, "_due_buckets", lambda conn, **k: [
        {"fixture": 20, "comp": "ligue1", "home": 1020, "away": 2020, "kickoff": "x",
         "bucket": "T-3h", "minutes": 180.0}])

    class _Tk:
        def for_match(self, comp, hi, ai): return {"home": "H", "draw": "D", "away": "A"}
        def index_ok(self, comp): return True

    class _Book:
        def __init__(self, bid, ask): self.yes_bid, self.yes_ask, self.yes_depth, self.no_depth = bid, ask, 10, 10

    class _Broker:
        def book(self, t):
            if t == "D":
                raise TimeoutError("read timeout")      # one leg's REQUEST fails
            return _Book(0.30, 0.31)

    import prediction_market_soccer.exec.kalshi_mirror as KM
    monkeypatch.setattr(KM, "_Tickers", _Tk)
    monkeypatch.setattr(KM, "DemoBroker", _Broker)
    out = VL.probe(c, include_prod=False)
    assert out["fetch_failed"] == ["demo:ligue1"]
    rows = {r["side"]: (r["fetch_ok"], r["ask"]) for r in c.execute("SELECT side, fetch_ok, ask FROM venue_book_probe")}
    assert rows["draw"] == (0, None) and rows["home"][0] == 1
    cell = {(r["comp"], r["venue"], r["bucket"]): r for r in VL.summary(c)["table"]}[("ligue1", "demo", "T-3h")]
    assert cell["n"] == 2 and cell["ask_pct"] == 100, "the failed leg must not read as a missing ask"
