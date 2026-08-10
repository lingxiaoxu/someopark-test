"""#143 — PR-7 step 1: S2 is recorded, never executed, and never graded early.

Three things have to hold, and they are the three ways this could quietly go wrong:

1. **It does not trade.** A shadow that writes a `decisions` row is not a shadow. The
   whole reason PR-7 step 1 runs in shadow is that step 0's rejection is fragile (CI upper
   bound 2.8pp from zero, sign flip at theta=+0.25), so no live money buys these 30 trades.
2. **It measures the same thing the live rule measures.** S2 is `EXIT_EDGE` moved to 0.0
   and nothing else. If the shadow computed its own `hold_edge`, the comparison would be
   between two strategies rather than between two thresholds — #141 is what that costs.
3. **It refuses to report a verdict before 30 trades.** #128 is the precedent for a
   number getting quoted off a run that wasn't measuring what the headline claimed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import exits
from prediction_market_macro.ops.ledger import open_positions
from prediction_market_macro.research import shadow_s2

TS = "2026-08-05T00:00:00+00:00"
# P(x > 0.1) = 0.60, P(x > 0.2) = 0.25 — the same plain ladder the exits tests use.
LADDER = '{"0.05": 0.4, "0.15": 0.35, "0.25": 0.25}'
CAL = "BEA_PCE"                                    # REGISTRY['KXPCECORE'].calendar

# The bucket is YES(>0.1) + NO(>0.2), so
#   hold_edge = (0.60 - mid_lo) + (0.75 - (1 - mid_hi)) = 0.35 - mid_lo + mid_hi
# and these three books put it either side of the two thresholds. Spelled out rather
# than tuned by hand so a reader can check the arithmetic against the ladder above.
INTACT = dict(bid=0.20, ask=0.22, hi_bid=0.78, hi_ask=0.80)      # +0.93: both rules hold
BAND = dict(bid=0.57, ask=0.59, hi_bid=0.20, hi_ask=0.22)        # -0.02: S2 only
REVERSED = dict(bid=0.78, ask=0.80, hi_bid=0.20, hi_ask=0.22)    # -0.23: both fire


class _S:
    pass


def _clock_at(iso: str):
    """A `datetime` drop-in whose `now()` is fixed, for pinning `shadow_run`'s stamp.

    Subclasses datetime rather than faking it, so `fromisoformat`/comparison/arithmetic
    inside the module under test keep working untouched.
    """
    fixed = datetime.fromisoformat(iso)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    return _Clock


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _position(conn, *, lo_px=0.55, hi_px=0.70, bid=0.20, ask=0.22, hi_bid=0.78,
              hi_ask=0.80, depth=500.0, series="KXPCECORE", period="2026-09",
              ts=TS, size=1.0):
    """One bucket position with a book, a pred and contract metadata. Returns its id.

    Defaults to the INTACT book, so a test that wants a trigger has to ask for one.
    """
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, series, period, "{}", "open", 0.35, 0.25, 0.1, size, "{}",
         "pce/0.1.0", "{}", ""))
    did = cur.lastrowid
    for ticker, side, px, strike in (("T0.1", "yes", lo_px, 0.1),
                                     ("T0.2", "no", hi_px, 0.2)):
        conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')", (did, ts, ticker, side, px, 1, 0.01))
        conn.execute(
            "INSERT OR IGNORE INTO contracts(ticker, event_ticker, series, period,"
            " floor_strike, strike_type, close_time, first_seen_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ticker, f"{series}-X", series, period, strike, "greater",
             "2026-09-30T00:00:00+00:00", ts))
    for ticker, b, a in (("T0.1", bid, ask), ("T0.2", hi_bid, hi_ask)):
        conn.execute(
            "INSERT OR REPLACE INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth,"
            " ask_depth) VALUES(?,?,?,?,?,?)", (ts, ticker, b, a, depth, depth))
    conn.execute(
        "INSERT OR REPLACE INTO preds(series, period, asof, ladder_json, dist_json,"
        " model_version, data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (series, period, ts, LADDER, "{}", "pce/0.1.0", ts, ts))
    conn.commit()
    return did


# ── 1. it is a shadow ────────────────────────────────────────────────────────────────

def test_the_shadow_never_writes_a_trade(conn):
    """The load-bearing safety property: `decisions` and `fills` must be untouched.

    S2 is registered at K=3 on a fragile rejection and may not spend live money before its
    forward criterion passes. If this test ever fails, the rule went live by accident.
    """
    _position(conn, **REVERSED)
    before = (conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"],
              conn.execute("SELECT COUNT(*) c FROM fills").fetchone()["c"])
    assert exits.shadow_run(conn, _S()) == 1, "test is vacuous — S2 did not fire"
    after = (conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"],
             conn.execute("SELECT COUNT(*) c FROM fills").fetchone()["c"])
    assert before == after
    assert conn.execute("SELECT COUNT(*) c FROM decisions WHERE kind='exit'"
                        ).fetchone()["c"] == 0


def test_every_open_position_is_logged_even_when_the_rule_does_not_fire(conn):
    """A quiet day must be distinguishable from a dead logger."""
    _position(conn)                                    # INTACT: hold_edge = +0.93
    assert exits.shadow_run(conn, _S()) == 0
    row = conn.execute("SELECT * FROM shadow_exits").fetchone()
    assert row["triggered"] == 0 and row["rule"] == "S2"
    assert "edge_intact" in row["note"]


# ── 2. it measures what the live rule measures ───────────────────────────────────────

def test_the_shadow_edge_is_the_live_edge(conn):
    """Same position, same book: `hold_state` feeds both paths, so they cannot disagree."""
    did = _position(conn, **REVERSED)
    pos = [p for p in open_positions(conn) if p["id"] == did][0]
    live_edge = exits.hold_state(conn, pos)["hold_edge"]
    exits.shadow_run(conn, _S())
    logged = conn.execute("SELECT hold_edge FROM shadow_exits").fetchone()["hold_edge"]
    assert logged == pytest.approx(live_edge, abs=1e-12)


def test_s2_is_exit_edge_with_the_wedge_removed(conn):
    """S2 introduces no fitted constant — it is rule 1's threshold moved to zero."""
    assert exits.S2_EDGE == 0.0
    assert exits.EXIT_EDGE < exits.S2_EDGE, "S2 must be the TIGHTER of the two"


def test_s2_fires_in_the_band_the_live_rule_still_holds(conn):
    """The whole content of the rule: -0.06 < hold_edge <= 0 is S2's territory alone."""
    # fair(bucket) = 0.35. Quote the legs so the structure's mid edge lands at -0.02.
    _position(conn, **BAND)
    pos = open_positions(conn)[0]
    he = exits.hold_state(conn, pos)["hold_edge"]
    assert exits.EXIT_EDGE < he <= exits.S2_EDGE, f"test setup missed the band: {he}"

    assert exits.shadow_run(conn, _S()) == 1               # S2 would close
    assert exits.run(conn, _S()) == 0                      # live holds
    assert conn.execute("SELECT COUNT(*) c FROM decisions WHERE kind='exit'"
                        ).fetchone()["c"] == 0


def test_an_unsellable_book_is_not_a_trigger(conn):
    """The shadow arm has to be an order that could have been sent. No depth, no fill."""
    _position(conn, **REVERSED, depth=5.0)
    assert exits.shadow_run(conn, _S()) == 0
    row = conn.execute("SELECT * FROM shadow_exits").fetchone()
    assert row["triggered"] == 0 and "no_depth" in row["note"]


def test_the_freeze_window_blocks_the_shadow_too(conn):
    """10 minutes pre-release the live book may not trade, so neither may the shadow."""
    _position(conn, **REVERSED)
    soon = datetime.now(timezone.utc) + timedelta(minutes=5)
    conn.execute("INSERT INTO releases(cal, period, scheduled_ts) VALUES(?,?,?)",
                 (CAL, "2026-09", soon.isoformat()))
    conn.commit()
    assert exits.shadow_run(conn, _S()) == 0
    assert conn.execute("SELECT COUNT(*) c FROM shadow_exits").fetchone()["c"] == 0


def test_an_unmeasurable_book_is_skipped_rather_than_logged_as_intact(conn):
    """A wide book yields no holding edge. Writing `triggered=0` for it would later read
    as 'S2 looked and declined', which is a different claim from 'S2 could not look'."""
    _position(conn, bid=0.18, ask=0.98)
    assert exits.shadow_run(conn, _S()) == 0
    assert conn.execute("SELECT COUNT(*) c FROM shadow_exits").fetchone()["c"] == 0


def test_the_recorded_pnl_is_the_live_paths_own_formula(conn):
    """`exit_realized` is shared, so the S2 arm cannot be priced on a different basis
    from the exits the live book actually books."""
    did = _position(conn, **REVERSED)
    pos = [p for p in open_positions(conn) if p["id"] == did][0]
    expected = exits.exit_realized(exits.hold_state(conn, pos)["legs_exit"])
    exits.shadow_run(conn, _S())
    got = conn.execute("SELECT realized_usd FROM shadow_exits").fetchone()["realized_usd"]
    assert got == pytest.approx(expected, abs=1e-6)


# ── 3. it refuses to grade early ─────────────────────────────────────────────────────

def _closed_trade(conn, did, *, series, period, realized, kind="settle_note",
                  ts="2026-08-09T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, series, period, "{}", kind, None, None, None, realized,
         json.dumps({"realized_usd": realized}), "pce/0.1.0", "{}", ""))
    conn.commit()


def test_no_verdict_before_the_registered_count(conn):
    did = _position(conn)
    _closed_trade(conn, did, series="KXPCECORE", period="2026-09", realized=-0.4)
    out = shadow_s2.run(conn)
    assert out["n_trades"] == 1 and out["n_required"] == 30
    assert out["verdict"].startswith("PENDING")
    assert out["ci95_gap"] is None, "an interval at n=1 would invite quoting it"


def test_a_trade_s2_never_touched_contributes_identically_to_both_arms(conn):
    """Inert trades stay in the sample. Dropping them would score S2 only on the days it
    acted, which is the subset most likely to look good by luck."""
    did = _position(conn)
    _closed_trade(conn, did, series="KXPCECORE", period="2026-09", realized=-0.4)
    out = shadow_s2.run(conn)
    t = out["trades"][0]
    assert t["s2_fired"] is False
    assert t["live_realized"] == t["s2_realized"] == -0.4
    assert out["roi_gap"] == 0.0


def test_s2_is_scored_at_the_price_it_was_recorded_at(conn, monkeypatch):
    """The arm comes from the logged row, not from a re-reading of today's book.

    The clock is pinned because `shadow_run` stamps with `datetime.now()` while every
    other row here is dated inside a fictional 08-05 → 08-09 window. Unpinned, the shadow
    row landed at the real wall clock and the S2 lookup's `ts_utc <= close.ts_utc` held
    only while real time happened to precede the hardcoded close — so this test passed by
    coincidence until 2026-08-09T00:00Z and then failed on its own. Pinning puts the
    shadow where the fixtures always meant it to be: after the open, before the close.
    """
    monkeypatch.setattr(exits, "datetime", _clock_at("2026-08-06T00:00:00+00:00"))
    did = _position(conn, **REVERSED)
    exits.shadow_run(conn, _S())
    logged = conn.execute("SELECT realized_usd FROM shadow_exits").fetchone()["realized_usd"]
    _closed_trade(conn, did, series="KXPCECORE", period="2026-09", realized=-1.0)
    out = shadow_s2.run(conn)
    t = out["trades"][0]
    assert t["s2_fired"] is True
    assert t["s2_realized"] == pytest.approx(logged) != t["live_realized"]
    assert out["roi_gap"] == pytest.approx((logged - (-1.0)) / 1.0, abs=1e-4)


def test_a_trigger_after_the_live_close_is_not_counted(conn):
    """S2 can only ever exit EARLIER than live. A row stamped after the position was
    already closed describes a position that no longer existed."""
    did = _position(conn)
    _closed_trade(conn, did, series="KXPCECORE", period="2026-09", realized=-0.4,
                  ts="2026-08-06T00:00:00+00:00")
    conn.execute(
        "INSERT INTO shadow_exits(ts_utc, rule, decision_id, series, period, hold_edge,"
        " triggered, realized_usd, legs_json, note)"
        " VALUES('2026-08-07T00:00:00+00:00','S2',?,?,?,-0.01,1,-0.1,'[]','late')",
        (did, "KXPCECORE", "2026-09"))
    conn.commit()
    assert shadow_s2.run(conn)["trades"][0]["s2_fired"] is False


def test_a_close_with_no_realized_pnl_is_dropped_not_zeroed(conn):
    """`cancel` rows retire a position administratively. Scoring one as a flat trade would
    put a fabricated 0 into both arms."""
    did = _position(conn)
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES('2026-08-09T00:00:00+00:00',?,?,'{}','cancel',NULL,NULL,NULL,NULL,"
        " '{\"retires_decision_id\": 1}','retire/1.0','{}','')",
        ("KXPCECORE", "2026-09"))
    conn.commit()
    out = shadow_s2.run(conn)
    assert out["n_trades"] == 0 and len(out["unscorable"]) == 1
    assert out["unscorable"][0]["close_kind"] == "cancel"


def test_a_still_open_trade_is_counted_as_pending_not_as_a_result(conn):
    _position(conn)
    out = shadow_s2.run(conn)
    assert out["n_trades"] == 0 and out["n_still_open"] == 1


def test_trades_before_the_registration_are_out_of_sample(conn):
    """The forward window starts at the registration date, full stop."""
    did = _position(conn, ts="2026-08-01T00:00:00+00:00")
    _closed_trade(conn, did, series="KXPCECORE", period="2026-09", realized=-0.4)
    assert shadow_s2.run(conn)["n_trades"] == 0


def test_the_verdict_closes_the_family_on_failure(conn, monkeypatch):
    """PR-7 registered that a forward miss ends the price-exit line — no retry with a
    different theta and no trailing variant. The text has to say so."""
    for i in range(3):
        did = _position(conn, series="KXTEST", period=f"2026-0{i + 1}")
        _closed_trade(conn, did, series="KXTEST", period=f"2026-0{i + 1}", realized=-0.4)
    out = shadow_s2.run(conn, n_forward=3)
    assert out["verdict"].startswith("FAIL")
    assert "does not go live" in out["verdict"] and "trailing" in out["verdict"]
    assert out["ci95_gap"] is not None
