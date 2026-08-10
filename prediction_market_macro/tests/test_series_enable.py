"""§25.4 — the per-series switch: what it must do, and what it must never do.

The design claims in `strategy/series_enable.py` are all testable, and the two that
matter most are negative ones: the gate can only ever subtract, and it must not be
absorbing. Both are pinned below, because either would be easy to break in a refactor
and neither would show up as a failing number — a gate that never re-enables just looks
like a series that stopped working.
"""
from __future__ import annotations

import json

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.strategy import series_enable as se


def _t(realized: float, staked: float = 1.0) -> dict:
    return {"staked": staked, "realized": realized}


def _win(n):  return [_t(+0.50) for _ in range(n)]
def _loss(n): return [_t(-0.50) for _ in range(n)]


# ── the fold ─────────────────────────────────────────────────────────────────────────

def test_abstains_below_min_n():
    """Fewer than MIN_N trades is not evidence; the series trades."""
    st = se.evaluate(_loss(se.MIN_N - 1))
    assert st["enabled"] is True and st["roi"] is None and st["flips"] == 0


def test_disables_on_a_losing_trailing_window():
    st = se.evaluate(_loss(se.MIN_N))
    assert st["enabled"] is False and st["roi"] == -0.5
    assert se.reason(st).startswith("series_disabled roi=-0.5")


def test_a_profitable_series_is_left_alone():
    st = se.evaluate(_win(se.WINDOW * 2))
    assert st["enabled"] is True and se.reason(st) is None


def test_breakeven_disables():
    """OFF_ROI is `<=`, not `<`: a series that exactly pays its costs is not paying us."""
    assert se.evaluate([_t(0.0) for _ in range(se.MIN_N)])["enabled"] is False


def test_hysteresis_band_is_not_symmetric():
    """Crossing back above zero is NOT enough — re-enable needs > ON_ROI.

    Without the band a series parked near breakeven flips on every event, and each flip
    is a real change in what gets traded.
    """
    trades = _loss(se.MIN_N)                       # off
    # nudge the trailing window to a small POSITIVE roi, still inside the band
    trades += [_t(+0.51) for _ in range(se.MIN_N)]
    st = se.evaluate(trades)
    assert 0 < st["roi"] <= se.ON_ROI, st
    assert st["enabled"] is False, "re-enabled inside the hysteresis band"


def test_re_enables_once_clear_of_the_band():
    st = se.evaluate(_loss(se.MIN_N) + _win(se.WINDOW))
    assert st["roi"] > se.ON_ROI and st["enabled"] is True and st["flips"] == 2


def test_the_fold_is_path_dependent_not_a_test_on_the_last_window():
    """Same final window, different history ⇒ can differ. That is the point of hysteresis.

    Both sequences end on the identical trailing WINDOW of trades sitting inside the
    band; the one that arrived there from ON stays ON, the one that arrived from OFF
    stays OFF. A implementation that only looked at the tail would return the same
    answer for both and silently drop the hysteresis.
    """
    tail = [_t(+0.005) for _ in range(se.WINDOW)]   # roi 0.005, inside the band
    from_on = se.evaluate(_win(se.WINDOW) + tail)
    from_off = se.evaluate(_loss(se.WINDOW) + tail)
    assert from_on["enabled"] is True
    assert from_off["enabled"] is False


def test_zero_staked_window_is_skipped_not_divided_by():
    assert se.evaluate([_t(0.0, 0.0) for _ in range(se.WINDOW)])["enabled"] is True


# ── the live read ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _store(conn, series, metrics):
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('series_enable',?,?,?,?,?)",
        (f"series_enable:{series}", series, "n12", json.dumps(metrics),
         "2026-08-05T00:00:00+00:00"))
    conn.commit()


def test_absent_row_does_not_block(conn):
    """A veto that has never been evaluated must default to letting the trade through."""
    assert se.blocked(conn, "KXCPI") is None


def test_the_stored_verdict_is_read(conn):
    """`would_block`, not `blocked` — under #155's SHADOW the gate reads the row and then
    declines to act on it, so the READ has to be pinned on the function that reports."""
    _store(conn, "KXCPI", {"enabled": False, "roi": -0.44, "n": 8})
    _store(conn, "KXFED", {"enabled": True, "roi": 0.10, "n": 8})
    assert "series_disabled roi=-0.44" in se.would_block(conn, "KXCPI")
    assert se.would_block(conn, "KXFED") is None


# ── #155 SHADOW ──────────────────────────────────────────────────────────────────────

def test_shadow_records_the_verdict_and_acts_on_none_of_it(conn, monkeypatch):
    """The whole of #155 in one assertion pair: `would_block` speaks, `blocked` does not."""
    _store(conn, "KXCPI", {"enabled": False, "roi": -0.44, "n": 8})
    monkeypatch.setattr(se, "SHADOW", True)
    assert se.would_block(conn, "KXCPI") is not None
    assert se.blocked(conn, "KXCPI") is None


def test_flipping_the_switch_makes_it_a_veto_with_no_other_change(conn, monkeypatch):
    """The switch has to be sufficient on its own. If landing the gate live ever needed a
    second edit somewhere else, that somewhere else is where the two lanes drift apart."""
    _store(conn, "KXCPI", {"enabled": False, "roi": -0.44, "n": 8})
    monkeypatch.setattr(se, "SHADOW", False)
    assert se.blocked(conn, "KXCPI") == se.would_block(conn, "KXCPI") is not None


def test_both_lanes_read_the_one_switch(conn, monkeypatch):
    """`ops/decide_all` and `research/pit_gates.GateState.disabled` must resolve through
    `veto`, so SHADOW cannot be on in one lane and off in the other — the #109/#128/#151
    divergence, which has already happened once on this exact gate."""
    from prediction_market_macro.research.pit_gates import GateState
    off = {"enabled": False, "roi": -0.44, "n": 8}
    gs = GateState("KXCPI", 0, None, {}, None, 1.0, off)
    monkeypatch.setattr(se, "SHADOW", True)
    assert gs.disabled is None and gs.would_disable is not None
    monkeypatch.setattr(se, "SHADOW", False)
    assert gs.disabled == gs.would_disable is not None


def test_a_disabled_series_is_recorded(conn):
    from datetime import datetime, timezone
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    se.record_shadow(conn, "KXCPI", {"enabled": False, "roi": -0.44, "n": 8, "flips": 1},
                     now)
    conn.commit()
    r = conn.execute("SELECT * FROM shadow_series_enable").fetchone()
    assert (r["day"], r["series"], r["evaluated"], r["would_block"]) == \
        ("2026-08-06", "KXCPI", 1, 1)
    assert r["roi"] == -0.44 and r["n"] == 8 and "series_disabled" in r["reason"]


def test_an_enabled_series_is_recorded_too(conn):
    """Otherwise "the gate wanted to block nothing" is indistinguishable from "the recorder
    was dead" — which is the failure that let §25.4 sit inert and unnoticed for a day."""
    from datetime import datetime, timezone
    se.record_shadow(conn, "KXFED", {"enabled": True, "roi": 0.10, "n": 8},
                     datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc))
    conn.commit()
    r = conn.execute("SELECT * FROM shadow_series_enable").fetchone()
    assert r["evaluated"] == 1 and r["would_block"] == 0 and r["reason"] is None


def test_an_unevaluated_series_is_recorded_as_blind_not_skipped(conn):
    """`{}` means the weekly job has never produced a verdict. That is a third state and
    it must be visible: absent rows would read as "evaluated and fine"."""
    from datetime import datetime, timezone
    se.record_shadow(conn, "KXU3", {},
                     datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc))
    conn.commit()
    r = conn.execute("SELECT * FROM shadow_series_enable").fetchone()
    assert r["evaluated"] == 0 and r["would_block"] == 0 and r["roi"] is None


def test_recording_twice_in_a_day_is_idempotent(conn):
    """decide_all runs many cycles a day; the shadow log is a verdict-per-day, not a
    tick log, or the eventual read-out would weight busy days more heavily."""
    from datetime import datetime, timezone
    for hour in (9, 12, 18):
        se.record_shadow(conn, "KXCPI", {"enabled": False, "roi": -0.44, "n": 8},
                         datetime(2026, 8, 6, hour, 0, tzinfo=timezone.utc))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM shadow_series_enable").fetchone()["c"] == 1


def test_unreadable_metrics_do_not_block(conn):
    conn.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('series_enable','h','KXU3','n1','{not json',?)",
        ("2026-08-05T00:00:00+00:00",))
    conn.commit()
    assert se.blocked(conn, "KXU3") is None


# ── the two invariants ───────────────────────────────────────────────────────────────

def test_it_can_only_ever_subtract(conn):
    """There is no input for which this module produces a trade that would not happen.

    `blocked`/`reason` return either a string (veto) or None (no opinion). Neither
    return value can clear another gate, and there is deliberately no `enable()`.
    """
    assert not hasattr(se, "enable")
    for st in ({"enabled": True}, {"enabled": False, "roi": -1, "n": 9}, {}):
        assert se.reason(st) is None or isinstance(se.reason(st), str)


def test_the_gate_is_not_absorbing(conn):
    """The signal must keep accruing while the series is switched off.

    This is the property that separates §25.4 from #124. The fold is over
    `decision_replay` trades — a candle replay that does not require us to have bet — so
    a disabled series still produces the evidence that can turn it back on. Modelled
    here by continuing to append trades after the disable and checking recovery.
    """
    trades = _loss(se.MIN_N)
    assert se.evaluate(trades)["enabled"] is False
    for _ in range(se.WINDOW):                     # evidence arrives without us trading
        trades.append(_t(+0.50))
        if se.evaluate(trades)["enabled"]:
            break
    else:
        pytest.fail("never re-enabled after a full window of winners")
