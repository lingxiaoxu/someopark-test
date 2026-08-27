"""PR-12 (#192) — the forward scorer for the energy width ladder.

Two classes of test, and the first one is the reason this file exists at all.

`shadow_width` MIRRORS `backtest.replay_series`'s two asof rules (step back behind a print
the book closed after; drop an event whose `data_horizon` reached past the release) because
it needs the distribution and `replay_series` returns Brier. A mirror is a duplication and
duplications drift — silently, and in the direction that flatters whoever last touched one
of them. So the asof is not asserted against a hand-written expectation here; it is
asserted against what `replay_series` ACTUALLY passed the model, recorded by instrumenting
the model itself.

The second class pins the estimator: the settled ladder censors the print into an interval,
the tails are IN, and an event nobody can bracket is counted rather than dropped.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import energy
from prediction_market_macro.model.common import Empirical
from prediction_market_macro.research import shadow_width as sw

ASOF = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _row(**kw):
    """A settlement leg as sqlite3.Row-ish: realized_interval only ever subscripts."""
    return {"ticker": kw.get("ticker", "T"), "floor_strike": kw.get("floor"),
            "cap_strike": kw.get("cap"), "strike_type": kw.get("st", "greater"),
            "result": kw["result"]}


# ── the interval estimator ────────────────────────────────────────────────────
def test_a_two_sided_ladder_brackets_the_print():
    legs = [_row(floor=3.0, result="yes"), _row(floor=3.2, result="yes"),
            _row(floor=3.4, result="no"), _row(floor=3.6, result="no")]
    assert sw.realized_interval(legs) == (3.2, 3.4, "interior")


def test_an_all_yes_ladder_is_scored_as_an_open_upper_tail_not_dropped():
    """The old estimator took the midpoint of a two-sided bracket and therefore threw away
    exactly the largest moves — 60.3% of KXAAAGASW events, 34.4% of KXU3 — which
    mechanically makes any model look too wide. An unbounded side is information, not a
    missing value."""
    legs = [_row(floor=3.0, result="yes"), _row(floor=3.2, result="yes")]
    lo, hi, kind = sw.realized_interval(legs)
    assert (lo, kind) == (3.2, "upper_censored") and hi == float("inf")


def test_an_all_no_ladder_is_scored_as_an_open_lower_tail():
    legs = [_row(floor=3.0, result="no"), _row(floor=3.2, result="no")]
    lo, hi, kind = sw.realized_interval(legs)
    assert (hi, kind) == (3.0, "lower_censored") and lo == -float("inf")


def test_a_bucket_ladder_uses_the_winning_bucket():
    legs = [_row(floor=69.0, cap=70.0, result="no"),
            _row(floor=70.0, cap=71.0, result="yes"),
            _row(floor=71.0, cap=72.0, result="no")]
    assert sw.realized_interval(legs) == (70.0, 71.0, "bucket")


def test_an_all_no_bucket_ladder_is_unscorable_rather_than_guessed():
    """Below every bucket or above every bucket — the settlement pattern alone cannot say
    which, and picking one would be inventing an observation."""
    legs = [_row(floor=69.0, cap=70.0, result="no"),
            _row(floor=70.0, cap=71.0, result="no")]
    assert sw.realized_interval(legs) is None


def test_an_inconsistent_settlement_is_refused():
    """A YES above a NO on a `greater` ladder is not a print, it is a data bug."""
    legs = [_row(floor=3.4, result="yes"), _row(floor=3.0, result="no")]
    assert sw.realized_interval(legs) is None


def test_a_zero_probability_interval_is_floored_and_counted_not_minus_infinity():
    """20k Monte-Carlo samples cannot represent a probability below 5e-5, so an interval
    no sample lands in reads as exactly 0. Scoring it as -inf would let one event decide
    the registration by sampling resolution rather than by the model."""
    d = Empirical(tuple(np.linspace(3.0, 4.0, 20_000).tolist()))
    ll, floored = sw._interval_ll(d, 100.0, 101.0)
    assert floored is True and math.isfinite(ll)
    assert ll == pytest.approx(math.log(sw._P_FLOOR))
    ll2, floored2 = sw._interval_ll(d, 3.4, 3.6)
    assert floored2 is False and ll2 == pytest.approx(math.log(0.2), abs=1e-3)


def test_the_open_tails_use_the_real_cdf_limits():
    d = Empirical(tuple(np.linspace(0.0, 1.0, 20_000).tolist()))
    ll_up, _ = sw._interval_ll(d, 0.75, float("inf"))
    ll_dn, _ = sw._interval_ll(d, -float("inf"), 0.25)
    assert ll_up == pytest.approx(math.log(0.25), abs=1e-3)
    assert ll_dn == pytest.approx(math.log(0.25), abs=1e-3)


# ── the registration cannot drift away from the grid ──────────────────────────
def test_the_ladder_is_read_from_the_grid_and_still_contains_the_default():
    """If the registration restated the rungs they could drift apart from the grid the
    selector actually searches, and the registration is the immovable one."""
    rungs = sw.ladder()
    assert sw.DEFAULT_RUNG in rungs
    assert energy.DEFAULT_PARAMS["fut_sigma_scale"] == sw.DEFAULT_RUNG
    assert any(r > 1.0 for r in rungs), "a ladder that can only narrow cannot refute"


def test_the_bonferroni_alpha_divides_by_the_non_default_rungs():
    out = sw.run.__doc__ is not None
    others = [r for r in sw.ladder() if r != sw.DEFAULT_RUNG]
    assert out and len(others) == 3
    assert sw.ALPHA / len(others) == pytest.approx(0.0166667, abs=1e-6)


def test_the_registration_is_stamped_and_the_current_code_is_documented():
    """A live registration whose fingerprint is still PENDING grades nothing — and a
    fingerprint that no longer matches any recorded version means `model/energy.py` moved
    underneath it, which the readout must say rather than quietly compare across."""
    assert sw.REGISTERED_FINGERPRINT != "PENDING"
    note = sw.code_change_note()
    assert note["code_changed_since_registration"] is False
    assert note["change_is_documented"] is True


def test_an_unrecorded_energy_version_is_reported_as_undocumented(monkeypatch):
    monkeypatch.setattr(sw, "code_fingerprint", lambda: "deadbeefcafe")
    note = sw.code_change_note()
    assert note["code_changed_since_registration"] is True
    assert note["change_is_documented"] is False
    assert "UNDOCUMENTED CHANGE" in note["note"]


# ── the asof mirror ───────────────────────────────────────────────────────────
def _seed(conn, series="KXWTIW", root="CL", n_bars=400, closes_after_release=False):
    """Three settled weekly events plus enough futures bars to bootstrap."""
    rng = np.random.default_rng(5)
    px = 70.0
    start = ASOF - timedelta(days=n_bars + 40)
    for i in range(n_bars):
        day = start + timedelta(days=i)
        px = max(px * math.exp(0.015 * rng.standard_t(4)), 1.0)
        kt = day + timedelta(hours=20)
        conn.execute(
            "INSERT OR IGNORE INTO fut_daily(root, event_time, open, high, low, close,"
            " volume, knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,NULL,?,?)",
            (root, day.date().isoformat(), px, px, px, round(px, 4),
             kt.isoformat(), kt.isoformat()))
    hour = "12:00:00Z" if closes_after_release else "20:00:00Z"
    for tok, d in (("26AUG07", "2026-08-07"), ("26AUG14", "2026-08-14"),
                   ("26AUG21", "2026-08-21")):
        for strike, res in ((60.0, "yes"), (90.0, "no")):
            tk = f"{series}-{tok}-T{int(strike)}"
            conn.execute("INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (tk, series, f"{series}-{tok}", tok, None, "greater", strike,
                          None, f"{d}T{hour}", "settled", "2026-06-01T00:00:00Z"))
            conn.execute("INSERT OR REPLACE INTO settlements VALUES(?,?,?,?,?,?)",
                         (tk, series, tok, res, f"{d}T{hour}", "2026-06-01T00:00:00Z"))
    conn.commit()
    return conn


@pytest.fixture()
def conn(tmp_path):
    return _seed(init_db(tmp_path / "w.db"))


def test_the_asof_mirror_agrees_with_replay_series_on_every_event(conn):
    """The load-bearing test. Not "asof == close - 1h" — that is the thing being mirrored,
    and asserting it here would pass just as happily if `replay_series` changed and this
    module did not. Instrument the model, run the real replay, and compare against what
    it actually asked for.
    """
    from prediction_market_macro.research.backtest import replay_series
    seen: list[datetime] = []
    real = energy.predict

    def spy(c, asof, period, series, params=None):
        seen.append(asof)
        return real(c, asof, period, series, params)

    energy.predict = spy
    try:
        replay_series(conn, "KXWTIW", asof_offsets=("-1h",))
    finally:
        energy.predict = real

    mine = [sw.asof_for(ev) for ev in sw._events(conn, "KXWTIW")]
    assert seen, "the replay scored nothing — the fixture, not the mirror, is broken"
    assert sorted(seen) == sorted(mine)


def test_the_mirror_also_agrees_when_the_book_closed_after_the_print():
    """The clamp is the branch that actually differs between the two files, so it gets its
    own event rather than riding on the happy path."""
    import tempfile, pathlib
    from prediction_market_macro.research.backtest import replay_series
    with tempfile.TemporaryDirectory() as td:
        c = _seed(init_db(pathlib.Path(td) / "w2.db"), closes_after_release=True)
        seen: list[datetime] = []
        real = energy.predict

        def spy(cc, asof, period, series, params=None):
            seen.append(asof)
            return real(cc, asof, period, series, params)

        energy.predict = spy
        try:
            replay_series(c, "KXWTIW", asof_offsets=("-1h",))
        finally:
            energy.predict = real
        mine = [sw.asof_for(ev) for ev in sw._events(c, "KXWTIW")]
        assert seen and sorted(seen) == sorted(mine)


# ── the verdict machinery ─────────────────────────────────────────────────────
def test_no_verdict_is_offered_below_the_registered_sample_size(conn):
    out = sw.run(conn, asof=ASOF)
    for s in sw.SERIES:
        v = out["series"][s]
        assert v["n_forward"] < sw.N_FORWARD
        assert v["verdict"].startswith("PENDING")
        assert all(not a["passes"] for a in v["arms"].values())


def test_a_rung_that_wins_only_on_the_mean_does_not_pass(conn):
    """`passes` needs BOTH a positive total and significance; one big event must not
    carry a registration."""
    sc = {"n_forward": 20, "n_required": 12,
          "arms": {"0.7": {"mean_ll_gain_nats": 0.9, "n_better": 3, "n": 20,
                           "wilcoxon_stat": 1.0, "wilcoxon_p_one_sided": 0.4,
                           "passes": False}}}
    assert sw._verdict(sc, 0.0167).startswith("FALSIFIED")


def test_the_criterion_text_says_a_win_authorises_a_default_change_and_not_a_bet(conn):
    out = sw.run(conn, asof=ASOF)
    assert "authorises a DEFAULT change and nothing else" in out["criterion"]
    assert out["k_discovery"] == 19 and out["k_forward"] == 3
