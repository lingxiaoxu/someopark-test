"""The rolling replay's divergence accounting must charge honestly and alarm honestly.

`research/live_replay.py` compares a walk-forward over the live window against the live
ledger. The two paths CANNOT match — the harness has no circuit breaker, no depth gate,
no staleness gate, one trade per event, and one look per day — so an equality check would
fire every day and mean nothing. What the module promises instead is that every
difference is charged to one of those documented limits, and that anything it cannot
charge lands in UNEXPLAINED, which is the only thing worth waking up for.

These tests pin that contract on synthetic ledgers, so they fail when the charging logic
silently starts absorbing a real divergence into a structural bucket.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import live_replay as lr

CUT = lr.TRACK_CUTOVER
UNTIL = "2026-08-19T23:59:59+00:00"


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


_DEC = ("INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " note, closes_decision_id, inputs_json, model_version, gate_snapshot)"
        " VALUES(?,?,?,?,?,?,?,?,'{}','test/0','{}')")


def _pass(conn, series, period, day, note, hhmm="12:00:00"):
    conn.execute(_DEC, (f"{day}T{hhmm}+00:00", series, period, "{}", "pass", None,
                        note, None))
    conn.commit()


def _open(conn, series, period, day, desc="NO X", size=1.0):
    cur = conn.execute(_DEC, (f"{day}T12:00:00+00:00", series, period,
                              json.dumps({"desc": desc}), "open", size, None, None))
    conn.commit()
    return cur.lastrowid


def _close(conn, open_id, series, period, day, realized=0.2):
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " closes_decision_id, inputs_json, model_version, gate_snapshot)"
        " VALUES(?,?,?,'{}','exit',?,?,?,'test/0','{}')",
        (f"{day}T12:00:00+00:00", series, period, realized, open_id,
         json.dumps({"realized_usd": realized})))
    conn.commit()


def _contract(conn, ticker, series, token, end_ts, bid, ask, close_time=None):
    """One contract + one candle row. `token` is KALSHI's period, not the model's key.

    The distinction is the whole point: `contracts.period` holds `26AUG1414` while every
    trade record holds `2026-08-14`, and 0 of 8872 production contract rows are ISO-
    shaped. An earlier version of this helper took the ISO key, so the fixture agreed
    with the code's broken `c.period = ?` filter and both were wrong together — the
    candle count came back 0 for every real event and `no_candles` absorbed everything.
    """
    conn.execute("INSERT INTO contracts(ticker, series, event_ticker, period, close_time,"
                 " first_seen_ts) VALUES(?,?,?,?,?,'t')",
                 (ticker, series, f"{series}-E", token, close_time))
    conn.execute("INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close)"
                 " VALUES(?,?,?,?)", (ticker, end_ts, bid, ask))
    conn.commit()


def _wf(trades):
    """The only shape of `walkforward.run`'s output that `reconcile` reads."""
    return {"streams": {"hybrid": {"trades": trades}}}


def _t(series, period, day, staked=1.0, realized=-1.0):
    return {"series": series, "period": period, "day": day, "desc": "NO X",
            "staked": staked, "realized": realized, "won": realized > 0}


# ── the reason parser ───────────────────────────────────────────────

@pytest.mark.parametrize("note,want", [
    ("stale_inputs pred=27h quotes=8.1h", "stale_inputs"),
    ("already_open_no_averaging_down", "already_open_no_averaging_down"),
    ("circuit_breaker:health_red:replay_mismatch", "circuit_breaker"),
    ("too_far_from_close 9.2d>7.0d", "too_far_from_close"),
    ("kelly_below_one_contract(0.4)", "kelly_below_one_contract"),
    (None, "unknown"),
    ("", "unknown"),
])
def test_reason_token(note, want):
    assert lr._reason(note) == want


def test_unknown_gate_is_not_silently_bucketed():
    """A gate someone adds without telling this file must surface, not be absorbed.

    `_REASONS.get(..., ("other", False))` is the only default in the module, and it is
    deliberately a bucket nobody reads as benign."""
    assert "brand_new_gate" not in lr._REASONS
    assert lr._REASONS.get(lr._reason("brand_new_gate x=1"),
                           ("other", False))[0] == "other"


# ── the window ──────────────────────────────────────────────────────

def test_window_ends_yesterday_not_today():
    """Today is half-run live and fully simulated in the replay; comparing them would
    manufacture a divergence that resolves itself overnight."""
    cut, end, days = lr._window(None)
    today = datetime.now(timezone.utc).date()
    assert end.date() == today - timedelta(days=1)
    assert cut.isoformat() == CUT
    assert days == (end.date() - cut.date()).days


def test_explicit_end_is_honoured():
    end = datetime(2026, 8, 15, 23, 59, 59, tzinfo=timezone.utc)
    cut, got, days = lr._window(end)
    assert got is end and days == 4          # 08-11 .. 08-15


# ── replay-only: charged to the gate live actually cited ────────────

def test_breaker_is_structural_because_the_harness_has_none(conn):
    """The one gate the harness provably cannot port: `breaker_tripped` reads UNACKED
    alerts and `alerts` has no ack timestamp, so at simulated day D there is no PIT way
    to know whether an earlier alert had been acked. A replay trade on a day live cited
    the breaker is explained, not an alarm."""
    _pass(conn, "KXCPI", "2026-07", "2026-08-12", "circuit_breaker:health_red")
    r = lr.reconcile(conn, _wf([_t("KXCPI", "2026-07", "2026-08-12")]), [], CUT, UNTIL)
    assert r["replay_only_by_cause"] == {"STRUCTURAL:circuit_breaker": 1}
    assert r["n_unexplained"] == 0


def test_stale_inputs_is_structural_but_lands_in_the_infra_bucket(conn):
    """Charged as structural for the reconciliation (the harness reads candles and can
    never be stale) AND counted as `infra` by `opportunity` (the strategy never got to
    decide). Both readings are true and the module must not collapse them."""
    _pass(conn, "KXU3", "2026-07", "2026-08-14", "stale_inputs pred=27h quotes=25.0h")
    r = lr.reconcile(conn, _wf([_t("KXU3", "2026-07", "2026-08-14")]), [], CUT, UNTIL)
    assert r["replay_only_by_cause"] == {"STRUCTURAL:stale_inputs": 1}
    o = lr.opportunity(conn, CUT, UNTIL)
    assert o["by_bucket"]["infra"] == 1 and o["infra_share"] == 1.0


def test_edge_gate_is_a_real_disagreement_not_a_harness_limit(conn):
    """`kelly_below_one_contract` means the live rule RAN and said no while the replay
    said yes. Nothing structural explains that — it is the daily-candle-close price and
    the selected params differing — so it must read DISAGREED, which is a research
    signal, not STRUCTURAL, which would read as 'expected, ignore'."""
    _pass(conn, "KXCPI", "2026-07", "2026-08-11", "kelly_below_one_contract 0.4")
    r = lr.reconcile(conn, _wf([_t("KXCPI", "2026-07", "2026-08-11")]), [], CUT, UNTIL)
    assert r["replay_only_by_cause"] == {"DISAGREED:kelly_below_one_contract": 1}
    assert r["n_unexplained"] == 0            # explained ≠ structural


def test_highest_gate_in_the_pipeline_wins(conn):
    """Several reasons on one key on one day: the breaker aborts before the model runs,
    so it explains the divergence more completely than an edge test that ran."""
    day = "2026-08-13"
    _pass(conn, "KXCPI", "2026-07", day, "too_far_from_close 9d", "09:00:00")
    _pass(conn, "KXCPI", "2026-07", day, "circuit_breaker:health_red", "10:00:00")
    _pass(conn, "KXCPI", "2026-07", day, "entropy_gate", "11:00:00")
    r = lr.reconcile(conn, _wf([_t("KXCPI", "2026-07", day)]), [], CUT, UNTIL)
    assert r["replay_only_by_cause"] == {"STRUCTURAL:circuit_breaker": 1}


def test_evaluated_on_another_day_is_the_granularity_divergence(conn):
    """Live looked at the event, just not on the day the replay bought. One look per day
    at a candle close vs every refresh cycle is exactly what that looks like."""
    _pass(conn, "KXCPI", "2026-07", "2026-08-15", "too_far_from_close 9d")
    r = lr.reconcile(conn, _wf([_t("KXCPI", "2026-07", "2026-08-11")]), [], CUT, UNTIL)
    assert r["replay_only_by_cause"] == {"STRUCTURAL:different_day": 1}


def test_no_live_decision_at_all_is_UNEXPLAINED_and_alarms(conn):
    """The alarm case. The replay traded an event the live pipeline never once evaluated
    in the whole window — the series fell out of a lane, or the refresh died before
    reaching it. Nothing in the harness's documented limits covers that."""
    r = lr.reconcile(conn, _wf([_t("KXCPI", "2026-07", "2026-08-11")]), [], CUT, UNTIL)
    assert r["n_unexplained"] == 1
    assert r["unexplained"][0]["detail"].startswith("live never wrote any decision")
    assert r["verdict"].startswith("REVIEW")


# ── live-only: charged to what the harness cannot represent ─────────

def test_second_entry_on_a_key_is_the_no_reentry_limit(conn):
    """`opened` is keyed by event and never cleared, so the replay takes at most one
    trade per event. Live's first entry matches; the second is charged to that limit and
    must NOT be double-counted as a match."""
    _open(conn, "KXNATGASW", "2026-08-14", "2026-08-12")
    _open(conn, "KXNATGASW", "2026-08-14", "2026-08-13")
    live = lr.live_entries(conn, CUT, UNTIL)
    r = lr.reconcile(conn, _wf([_t("KXNATGASW", "2026-08-14", "2026-08-12")]),
                     live, CUT, UNTIL)
    assert r["n_matched"] == 1
    assert r["live_only_by_cause"] == {"STRUCTURAL:no_reentry": 1}
    assert r["n_unexplained"] == 0


def test_still_open_position_cannot_be_in_the_replay(conn):
    """`_open_settled_events` only walks events with a settlement row, so a live position
    that has not closed is structurally absent from the simulation."""
    _open(conn, "KXWTIW", "2026-08-28", "2026-08-18")
    live = lr.live_entries(conn, CUT, UNTIL)
    assert live[0]["open"] is True
    r = lr.reconcile(conn, _wf([]), live, CUT, UNTIL)
    assert r["live_only_by_cause"] == {"STRUCTURAL:still_open": 1}


def _settled_live_trade(conn, token, key="2026-08-14", close_time=None,
                        end_ts=1755000000, bid=40, ask=42):
    """A closed KXWTIW live trade plus the contract it was written against.

    `token` and `key` name the SAME event in the two dialects — production always agrees
    on that (`26AUG1414` ⇄ `2026-08-14`) and a fixture that disagrees is testing the
    wrong thing.
    """
    did = _open(conn, "KXWTIW", key, "2026-08-13")
    _close(conn, did, "KXWTIW", key, "2026-08-14")
    _contract(conn, "KXWTIW-X", "KXWTIW", token, end_ts, bid, ask,
              close_time=close_time)
    return lr.live_entries(conn, CUT, UNTIL)


def test_the_candle_lookup_speaks_kalshis_period_dialect(conn):
    """REGRESSION. `contracts.period` is `26AUG1414`; the trade says `2026-08-14`.

    The lookup used to compare them with `=`, so it found nothing on every real event and
    charged every closed live-only trade to `no_candles` — an event with 189 candle rows
    included. If this test fails with `no_candles`, the token/key join has been undone.
    """
    live = _settled_live_trade(conn, "26AUG1414")
    r = lr.reconcile(conn, _wf([]), live, CUT, UNTIL)
    assert "STRUCTURAL:no_candles" not in r["live_only_by_cause"]
    assert r["live_only_by_cause"] == {"DISAGREED:replay_declined": 1}
    assert "189" not in r["live_only"][0]["detail"]      # count is real, not hardcoded
    assert "1 candle rows" in r["live_only"][0]["detail"]


def test_an_event_closing_after_the_cutoff_is_not_a_missing_candle(conn):
    """The replay stops at yesterday. A position live opened on an event that settles
    LATER is invisible to it for that reason alone, and folds in on its own next run —
    charging it to `no_candles` blamed the data for a window boundary."""
    live = _settled_live_trade(conn, "26AUG2117", key="2026-08-21",
                               close_time="2026-08-21T21:00:00Z",
                               end_ts=0, bid=None, ask=None)
    r = lr.reconcile(conn, _wf([]), live, CUT, UNTIL)
    assert r["live_only_by_cause"] == {"STRUCTURAL:outside_window": 1}
    assert "2026-08-21" in r["live_only"][0]["detail"]
    assert r["n_unexplained"] == 0


def test_the_404_sentinel_does_not_count_as_priceable(conn):
    """ingest/kalshi_md writes a NULL-price row at end_ts=0 when the candlestick endpoint
    404s — 6700 of 14683 rows. Counting those as priceable would report 'the replay could
    have seen this' precisely when it could not, i.e. UNEXPLAINED for a real limit."""
    live = _settled_live_trade(conn, "26AUG1414", close_time="2026-08-14T21:00:00Z",
                               end_ts=0, bid=None, ask=None)
    r = lr.reconcile(conn, _wf([]), live, CUT, UNTIL)
    assert r["live_only_by_cause"] == {"STRUCTURAL:no_candles": 1}
    assert r["n_unexplained"] == 0


def test_a_gate_the_replay_applied_itself_is_a_disagreement_not_a_mystery(conn):
    """The replay priced the event and its OWN gate refused while live traded it. That is
    a disagreement about a gate — nameable, countable, and not worth an alarm."""
    live = _settled_live_trade(conn, "26AUG1414")
    wf = _wf([]) | {"feature_rows": [{"series": "KXWTIW", "period": "2026-08-14",
                                      "placed": False, "blocked_by": "skill_blocked"}]}
    r = lr.reconcile(conn, wf, live, CUT, UNTIL)
    assert r["live_only_by_cause"] == {"DISAGREED:replay_skill_blocked": 1}
    assert r["n_unexplained"] == 0


def test_a_priceable_bet_the_replay_built_and_never_traded_is_UNEXPLAINED(conn):
    """The live-side alarm, and the ONLY thing that still reaches it.

    The replay produced a placeable bet on this very event, the event is priceable and
    inside the window, and yet it is absent from the traded set. No documented limit
    covers that, so it must surface rather than be absorbed.
    """
    live = _settled_live_trade(conn, "26AUG1414")
    wf = _wf([]) | {"feature_rows": [{"series": "KXWTIW", "period": "2026-08-14",
                                      "placed": True, "blocked_by": None}]}
    r = lr.reconcile(conn, wf, live, CUT, UNTIL)
    assert r["n_unexplained"] == 1
    assert "never traded it" in r["unexplained"][0]["detail"]


def test_unexplained_is_reachable_at_all_on_the_live_side(conn):
    """A meta-test, because the bug this file now guards was not a wrong answer — it was
    a taxonomy in which the alarm could never fire. Every live-only trade fell into one
    of three buckets, two of which needed a re-entry or an open position, so ALIGNED was
    guaranteed rather than earned. If some future refactor makes UNEXPLAINED unreachable
    again, the suite must fail here instead of going quietly green forever."""
    import ast
    import inspect
    import textwrap
    fn = ast.parse(textwrap.dedent(inspect.getsource(lr._explain_live_only))).body[0]
    src = inspect.getsource(lr._explain_live_only)
    assert "UNEXPLAINED" in src, "the live-side alarm has been removed"
    # It must also not BE the fallthrough. A function whose final unconditional return is
    # UNEXPLAINED alarms on every ordinary disagreement; one that can never reach it never
    # alarms at all. Both are the same failure — the bucket carrying no information.
    last = fn.body[-1]
    assert isinstance(last, ast.Return), "the charge list must end in a definite answer"
    assert "UNEXPLAINED" not in ast.dump(last), \
        "UNEXPLAINED became the catch-all; give the ordinary case its own bucket"


# ── opportunity attribution ────────────────────────────────────────

def test_infra_and_strategy_are_never_reported_as_one_number(conn):
    """The whole point of the split: 'we passed N times' answers neither question."""
    for i in range(3):
        _pass(conn, "KXCPI", "2026-07", "2026-08-14", "stale_inputs", f"0{i}:00:00")
    for i in range(7):
        _pass(conn, "KXCPI", "2026-07", "2026-08-14", "too_far_from_close", f"1{i%6}:00:00")
    o = lr.opportunity(conn, CUT, UNTIL)
    assert o["n_pass"] == 10
    assert o["by_bucket"] == {"strategy": 7, "infra": 3}
    assert o["infra_share"] == 0.3
    assert o["per_day"]["2026-08-14"]["infra_share"] == 0.3


def test_counterfactual_only_credits_the_exact_blocked_event_day(conn):
    """It must not credit the replay's whole P&L to the infra bucket. Only a replay trade
    on the SAME (series, period, day) that live lost to infra is evidence about that
    block; anything else is a different trade being smuggled into the counterfactual."""
    _pass(conn, "KXCPI", "2026-07", "2026-08-14", "stale_inputs")
    wf = _wf([_t("KXCPI", "2026-07", "2026-08-14", 1.0, 0.5),      # the blocked one
              _t("KXU3", "2026-07", "2026-08-14", 1.0, 9.0),       # unrelated
              _t("KXCPI", "2026-07", "2026-08-15", 1.0, 9.0)])     # right event, wrong day
    cf = lr.opportunity(conn, CUT, UNTIL, wf)["counterfactual"]
    assert cf["n_infra_blocked_events"] == 1
    assert cf["n_replay_traded"] == 1
    assert cf["replay_realized"] == 0.5
    assert "never as foregone P&L" in cf["caveat"]


def test_run_alarms_into_alerts_on_an_unexplained_divergence(conn, monkeypatch):
    """An unexplained divergence means the shipped rule and the researched rule have
    parted company in a way this file does not model. Logging it is not enough — that is
    the class of thing that goes unnoticed for weeks."""
    monkeypatch.setattr(lr, "replay",
                        lambda c, end=None, log=None: {"streams": {"hybrid": {
                            "trades": [_t("KXCPI", "2026-07", "2026-08-11")],
                            "n_trades": 1, "won": 0, "staked": 1.0,
                            "realized": -1.0, "roi": -1.0}}})
    out = lr.run(conn, end=datetime.fromisoformat(UNTIL), log=None)
    assert out["reconciliation"]["n_unexplained"] == 1
    a = conn.execute("SELECT level, source, message FROM alerts").fetchall()
    assert len(a) == 1 and a[0]["source"] == "research.live_replay"
    assert a[0]["level"] == "warn" and "unexplained" in a[0]["message"]
    row = conn.execute("SELECT config_hash, metrics_json FROM experiments"
                       " WHERE name='live_replay'").fetchone()
    assert row["config_hash"] == "livewin:end2026-08-19"
    assert json.loads(row["metrics_json"])["window_end"] == "2026-08-19"


def test_run_is_silent_when_everything_is_charged(conn, monkeypatch):
    _pass(conn, "KXCPI", "2026-07", "2026-08-11", "circuit_breaker:health_red")
    monkeypatch.setattr(lr, "replay",
                        lambda c, end=None, log=None: {"streams": {"hybrid": {
                            "trades": [_t("KXCPI", "2026-07", "2026-08-11")],
                            "n_trades": 1, "won": 0, "staked": 1.0,
                            "realized": -1.0, "roi": -1.0}}})
    out = lr.run(conn, end=datetime.fromisoformat(UNTIL), log=None)
    assert out["reconciliation"]["verdict"].startswith("ALIGNED")
    assert conn.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"] == 0


def test_livewin_hash_cannot_collide_with_the_headline_walkforward_rows():
    """`frontend_export` reads the daily headline with `window LIKE '30d%'` and
    walkforward writes `INSERT OR REPLACE` on (name, config_hash). A 30-day-long live
    window without the tag would overwrite the published headline row."""
    import inspect
    src = inspect.getsource(lr.replay)
    assert 'hash_tag="livewin"' in src
