"""The Kalshi DEMO mirror of the 择时(实现) strategy (exec/kalshi_mirror).

Pure-logic coverage: sizing, the demo-only guards, the buy/sell wire translation through the
reused crypto_trading client, schema presence, the live-loop hook, and exit-trigger parity
with strategy/smart_exit. The venue itself was exercised live on the demo host before the
module was written (V2 create 201 / cancel 200)."""
from __future__ import annotations

import inspect
import os
import sqlite3

import pytest

from prediction_market_soccer.exec import kalshi_mirror as km


def test_contracts_is_the_ledger_sizing_rounded_to_whole_contracts():
    # $1 @ 25¢ → 4 contracts (the ledger's contracts_for), whole and ≥1
    assert km.contracts(0.25, 1.0) == 4
    assert km.contracts(0.37, 1.0) == 3          # 2.70 → nearest whole
    assert km.contracts(0.95, 0.2) == 1          # never below one contract
    assert km.contracts(0.0, 1.0) == 0           # no ask → nothing


def test_contracts_never_exceeds_the_strategy_stake_ceiling():
    n = km.contracts(0.10, 5.0)                  # $5 asked, ceiling is $2
    assert n * 0.10 <= km.MAX_ORDER_USD + 1e-9
    assert n == 20


def test_enabled_reads_the_flag(monkeypatch):
    monkeypatch.setenv(km.ENV_FLAG, "false")
    assert km.enabled() is False
    monkeypatch.setenv(km.ENV_FLAG, "true")
    assert km.enabled() is True
    monkeypatch.delenv(km.ENV_FLAG)
    assert km.enabled() is False                 # default OFF


def test_broker_refuses_a_non_demo_soccer_env(monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "prod")
    with pytest.raises(RuntimeError, match="not demo"):
        km.DemoBroker()


class _FakeClient:
    base = "https://external-api.demo.kalshi.co/trade-api/v2"

    def __init__(self):
        self.calls = []

    def create_order(self, **kw):
        self.calls.append(kw)
        return {"status_code": 201, "response": '{"order_id":"o1","fill_count":"2.00","remaining_count":"0.00",'
                                                '"average_fill_price":"0.4100"}', "body_sent": kw}


class _NoLimiter:
    def acquire_read(self, *a, **k): return True
    def acquire_write(self, *a, **k): return True


def _fake_broker():
    b = km.DemoBroker.__new__(km.DemoBroker)
    b.c, b.limiter = _FakeClient(), _NoLimiter()
    return b


def test_buy_yes_takes_the_ask_as_a_yes_contract():
    b = _fake_broker()
    res = b.buy_yes("KXEPLGAME-X-ABC", 2, 0.41, "cid")
    kw = b.c.calls[-1]
    assert kw["side"] == "yes" and kw["price_dollars"] == 0.41 and kw["count"] == 2
    assert kw["tif"] == "immediate_or_cancel"       # nothing may rest
    assert res["ok"] and res["fill_count"] == 2.0 and res["avg_fill"] == 0.41


def test_sell_yes_is_expressed_as_buying_no_at_one_minus_bid():
    """The client only speaks 'buy'; its documented translation makes buy-NO @ (1−p) an
    ask @ p on the single book — i.e. selling our YES leg at the bid."""
    b = _fake_broker()
    b.sell_yes("KXEPLGAME-X-ABC", 3, 0.62, "cid")
    kw = b.c.calls[-1]
    assert kw["side"] == "no" and kw["count"] == 3
    assert abs(kw["price_dollars"] - 0.38) < 1e-9


def test_orders_are_refused_outside_the_unit_interval():
    b = _fake_broker()
    with pytest.raises(ValueError):
        b.buy_yes("T", 1, 1.0, "cid")
    with pytest.raises(ValueError):
        b.buy_yes("T", 0, 0.5, "cid")


def test_schema_has_the_mirror_tables():
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:")
    c.executescript(store._SCHEMA)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"kalshi_mirror", "kalshi_mirror_eval"} <= names
    cols = {r[1] for r in c.execute("PRAGMA table_info(kalshi_mirror)")}
    assert {"fixture_api_id", "track", "side", "ticker", "ledger_entry_c", "fill_count",
            "exit_fill_count", "pnl_c", "client_order_id"} <= cols


def test_the_live_loop_calls_the_mirror_after_milestone_capture():
    from prediction_market_soccer.ops import live_refresh
    src = inspect.getsource(live_refresh.refresh_once)
    assert "kalshi_mirror.run_cycle(conn, inplay)" in src
    assert src.index("_capture_milestones(conn, inplay)") < src.index("kalshi_mirror.run_cycle")


def test_exit_trigger_matches_the_ledger_rule():
    """The mirror sells when bid ≥ fair + min(OVERSHOOT_MARGIN, overshoot_trigger(fair)) —
    the identical expression strategy/smart_exit evaluates on recorded price points."""
    from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN, overshoot_trigger
    from prediction_market_soccer.strategy import smart_exit
    src = inspect.getsource(smart_exit.smart_exit_cashout)
    assert "min(margin, overshoot_trigger(fair / 100.0))" in src
    for fair in (0.30, 0.60, 0.85, 0.95):
        trig = min(OVERSHOOT_MARGIN, overshoot_trigger(fair))
        assert 0.0 <= trig <= OVERSHOOT_MARGIN
        assert fair + trig <= 1.0 + 1e-9
    msrc = inspect.getsource(km._scan_exits)
    assert "min(OVERSHOOT_MARGIN, overshoot_trigger(fair))" in msrc
    # decided on the MILESTONE row (the ledger's price points), the ledger's price order,
    # knockout-scaled lambdas, no red-card term — exactly strategy/smart_exit
    assert "live_match_prob(lam[0], lam[1], mn, sh, sa)" in msrc
    assert "pair_lambdas(hi, ai, knockout=is_knockout(fx[\"round\"]))" in msrc
    assert '(m[f"kalshi_{side}_bid"], m[f"kalshi_{side}_ask"]' in msrc
    assert "_state_at(goals, mn)" in msrc
    assert "kalshi_{s}_bid kb, kalshi_{s}_ask ka, poly_{s}_bid pb, poly_{s}_ask pa" in inspect.getsource(smart_exit._milestone_ticks)


def test_pre_and_inplay_reuse_the_ledgers_own_functions():
    src = inspect.getsource(km._scan_inplay)
    assert "_inplay_entry(conn, fx, hi, ai)" in src
    src = inspect.getsource(km.pre_decision)
    for needle in ("quotes_from_milestone_row(pre_row)", "price_match_calibrated(sm, hi, ai, knockout=False",
                   "host_neutral=knockout", "motivation_multipliers(conn, _fifa_ranks()", "_pit_cal(records"):
        assert needle in src, needle


def test_a_listing_we_could_not_reach_is_never_a_terminal_no_market(monkeypatch, tmp_path):
    """On 2026-09-03 a transient 'database is locked' broke the Kalshi listing call and the
    mirror wrote 'no Kalshi market for this pairing' — a TERMINAL verdict — losing a real
    La Liga pre-match bet. A listing we could not retrieve must defer, not decide."""
    import sqlite3
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(store._SCHEMA)
    c.execute("INSERT INTO fixture (api_id, league_id, season, home_api_id, away_api_id, kickoff_ts, status_short) "
              "VALUES (9,140,2026,1,2,'2026-09-03T19:00:00+00:00','NS')")
    c.commit()
    fx = c.execute("SELECT * FROM fixture WHERE api_id=9").fetchone()

    class _Unreachable:
        failed = {"laliga"}
        def for_match(self, comp, hi, ai): return None
        def index_ok(self, comp): return comp not in self.failed

    class _Retrieved(_Unreachable):
        failed = set()

    monkeypatch.setattr(km, "_log", lambda *a, **k: None)
    out = km._place_entry(c, None, _Unreachable(), fx, "a", "b", track="pre", side="away", stake=1.0,
                          bet_kind="value", entry_min=0, ledger_c=21.0, ledger_venue="kalshi",
                          ledger_edge=0.05, comp="laliga")
    assert out == {"terminal": False}
    assert c.execute("SELECT COUNT(*) FROM kalshi_mirror").fetchone()[0] == 0, "nothing may be written"
    # a listing that WAS retrieved and lacks the pairing is a real answer
    out = km._place_entry(c, None, _Retrieved(), fx, "a", "b", track="pre", side="away", stake=1.0,
                          bet_kind="value", entry_min=0, ledger_c=21.0, ledger_venue="kalshi",
                          ledger_edge=0.05, comp="laliga")
    assert out["terminal"] is True
    r = c.execute("SELECT status, note FROM kalshi_mirror").fetchone()
    assert r["status"] == "skipped" and "no Kalshi market" in r["note"]


def test_init_db_does_not_take_the_write_lock_when_the_schema_is_current():
    """KalshiDiscovery builds one connection per competition per cycle purely to READ the
    club registry; init_db used to run the DDL and venue seed every time."""
    import sqlite3
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    store.init_db(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == store._schema_fingerprint()
    calls = {"n": 0}

    class _Spy:
        def __init__(self, inner): self._i = inner
        def __getattr__(self, k): return getattr(self._i, k)
        def executescript(self, *a, **k):
            calls["n"] += 1
            return self._i.executescript(*a, **k)
    spy = _Spy(c)
    store.init_db(spy)
    assert calls["n"] == 0, "a current schema must not re-run the DDL"
    c.execute("PRAGMA user_version=0")           # a schema change forces the rebuild
    store.init_db(spy)
    assert calls["n"] == 1


def test_a_lost_send_response_stays_pending_and_is_never_re_sent_blind(monkeypatch):
    """An IOC either fills at the matching engine or dies there; a lost response says which
    happened, not that nothing happened. Marking it 'error' would make the row retryable and
    a second order could land on top of a live one, leaving a contract our books never exit."""
    import sqlite3
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    store.init_db(c)
    c.execute("INSERT INTO fixture (api_id, league_id, season, home_api_id, away_api_id, kickoff_ts, status_short) "
              "VALUES (11,61,2026,1,2,'2026-09-04T17:00:00+00:00','NS')")
    c.commit()
    fx = c.execute("SELECT * FROM fixture WHERE api_id=11").fetchone()

    class _Tk:
        def for_match(self, comp, hi, ai): return {"home": "TK-H", "draw": "TK-D", "away": "TK-A"}
        def index_ok(self, comp): return True

    class _Book:
        yes_ask, yes_bid, yes_depth, no_depth = 0.30, 0.29, 100, 100

    class _Broker:
        def book(self, t): return _Book()
        def buy_yes(self, *a, **k): raise TimeoutError("read timeout")

    monkeypatch.setattr(km, "_log", lambda *a, **k: None)
    out = km._place_entry(c, _Broker(), _Tk(), fx, "a", "b", track="pre", side="home", stake=1.0,
                          bet_kind="value", entry_min=0, ledger_c=30.0, ledger_venue="kalshi",
                          ledger_edge=0.05, comp="ligue1")
    assert out == {"terminal": False}
    r = c.execute("SELECT status, note, client_order_id FROM kalshi_mirror").fetchone()
    assert r["status"] == "pending", "a lost response must stay pending for reconciliation"
    assert "unknown" in r["note"]
    coid = r["client_order_id"]
    # a second attempt must NOT replace the row while it is pending
    out2 = km._place_entry(c, _Broker(), _Tk(), fx, "a", "b", track="pre", side="home", stake=1.0,
                           bet_kind="value", entry_min=0, ledger_c=30.0, ledger_venue="kalshi",
                           ledger_edge=0.05, comp="ligue1")
    assert out2 == {"terminal": True}
    rows = c.execute("SELECT client_order_id FROM kalshi_mirror").fetchall()
    assert len(rows) == 1 and rows[0]["client_order_id"] == coid, "no blind re-send"


def test_reconcile_releases_a_pending_row_only_when_the_venue_has_no_such_order(monkeypatch):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    store.init_db(c)
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    c.execute("INSERT INTO kalshi_mirror (fixture_api_id, track, side, ticker, count, client_order_id, "
              "status, submitted_at) VALUES (12,'pre','home','TK-H',1,'coid-1','pending',?)", (old,))
    c.commit()
    monkeypatch.setattr(km, "_log", lambda *a, **k: None)

    class _B:
        def __init__(self, orders): self._o = orders
        def orders_for(self, t): return self._o
    # venue knows nothing about it → released to the retry path
    km._reconcile_pending(c, _B([]))
    assert c.execute("SELECT status FROM kalshi_mirror").fetchone()["status"] == "error"
    # a filled order is adopted, not retried
    c.execute("UPDATE kalshi_mirror SET status='pending'")
    c.commit()
    km._reconcile_pending(c, _B([{"client_order_id": "coid-1", "order_id": "o1",
                                  "fill_count_fp": "1.00", "yes_price_dollars": "0.31"}]))
    r = c.execute("SELECT status, fill_count, avg_fill_c FROM kalshi_mirror").fetchone()
    assert r["status"] == "open" and r["fill_count"] == 1.0 and r["avg_fill_c"] == 31.0
