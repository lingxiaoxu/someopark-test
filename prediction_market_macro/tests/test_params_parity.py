"""#198 — the gate must refuse to grade PARAMETERS production does not run.

#196 closed the branch half of this and its own docstring named the hole it left: the
observer reads input KEY NAMES and is "blind to one that only changes their values".
Adopted parameters are exactly that. `param_select.current()` has returned an adopted,
weekly-changing dict since 2026-08-11 and `ops/predict_all` predicts through it, while
the replay behind the §9.5 gate passed no params at all and got the registered defaults.
Identical key sets, so branch parity said OK — and the gate that authorises real money
was certifying a configuration production had stopped running.

Measured 2026-08-27 across all 14 series, it moves the brier criterion in BOTH
directions: KXU3 from 0.03774 (beats market 0.04159) to 0.04270 (loses to it), KXFED the
other way. Nought of the graded evidence was about the live config — 0/3 for the CPI
family, 2/14 KXNATGASW, 1/14 KXWTIW.

The failure modes a fix like this has, one test each:

  * the counterfactual quietly REPLACING the honest number instead of being ANDed to it
    (it is in-sample: the lanes chose those params by scoring these very events);
  * a "PIT" reader that is really today's params applied backwards;
  * a share threshold that deadlocks — a weekly selection lane can never put a majority
    of history at today's set, so a `MIN_HIST_SHARE`-style bar here would close the gate
    by the schedule rather than by evidence;
  * the mix being counted on a different sample than the criteria (KXWTIW replays 156
    settled events and scores 14).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import branch_parity as bp
from prediction_market_macro.research.eval import gate_verdict

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
LIVE = {"fut_pool_bars": 750, "fut_vol_window": 40}
LABEL = json.dumps(LIVE, sort_keys=True)


def _bundles():
    """A bundle that clears all five of the 铁律7 criteria."""
    return ({"n_scored-1h": 20, "brier_model-1h": 0.05, "brier_market-1h": 0.09},
            {"roi": 0.2, "edge_capture": 0.8}, {"p": 0.01})


# ── params_of: the label ─────────────────────────────────────────────────────


def test_key_order_is_not_an_identity():
    assert bp.params_of({"a": 1, "b": 2}) == bp.params_of({"b": 2, "a": 1})


def test_the_empty_set_is_named_not_blank():
    """`{}` means "the registered defaults", which is a real configuration production can
    be running — three of the fourteen series are on it. A blank label would sort next to
    a missing read and make "we ran defaults" indistinguishable from "we don't know"."""
    assert bp.params_of({}) == "defaults"
    assert bp.params_of(None) == "defaults"
    assert bp.params_of({"w_last": 0.4}) != "defaults"


def test_two_different_sets_are_two_different_labels():
    """The whole point. #196's observer maps both of these to the same branch, because
    the key set — and therefore `Pred.inputs` — is identical."""
    assert bp.params_of({"w_last": 0.3}) != bp.params_of({"w_last": 0.4})


# ── the verdict is the counterfactual, not the share ─────────────────────────


def test_a_weekly_selection_lane_cannot_deadlock_the_gate():
    """THE design test. `param_argmin` writes a new manual row most weeks, so at any
    moment nearly all of history ran something else — here 1 of 40. A branch-parity-style
    `covered >= 0.5` bar would therefore be unsatisfiable forever, and a gate that can
    never pass is not a safety property, it is an outage. The share is DISCLOSED; the
    verdict comes from the re-score.
    """
    r = bp.params_parity_check({"defaults": 39, LABEL: 1}, LIVE, live_ok=True)
    assert r["hist_share_at_live_params"] == pytest.approx(0.025)
    assert r["parity"] is True, "a tiny share must not by itself fail the check"
    assert r["n_hist_at_live_params"] == 1 and r["n_hist"] == 40


def test_the_counterfactual_failing_fails_the_check():
    r = bp.params_parity_check({"defaults": 13, LABEL: 1}, LIVE, live_ok=False)
    assert r["parity"] is False
    assert "1/14" in r["reason"] and "in-sample" in r["reason"]


def test_an_unrun_counterfactual_is_a_failure_not_a_pass():
    """`live_ok=None` means the re-score raised or was never done. With adopted params
    live and no re-score, nothing at all is known about the config that will trade —
    treating that as clean is the exact shape of the bug being fixed."""
    r = bp.params_parity_check({"defaults": 14}, LIVE, live_ok=None)
    assert r["parity"] is False and "nobody re-scored" in r["reason"]


def test_defaults_live_needs_no_counterfactual():
    """With `{}` adopted, the defaults ARE production, so the primary replay already is
    the counterfactual and a second identical replay would be pure cost. Passing without
    a re-score is correct here and ONLY here."""
    r = bp.params_parity_check({"defaults": 14}, {}, live_ok=None)
    assert r["parity"] is True and r["reason"] is None
    assert r["live_params_are_defaults"] is True


def test_an_empty_history_does_not_divide_by_zero():
    r = bp.params_parity_check({}, LIVE, live_ok=True)
    assert r["n_hist"] == 0 and r["hist_share_at_live_params"] == 0.0


# ── the gate wiring ──────────────────────────────────────────────────────────


def test_a_perfect_row_is_refused_when_the_live_params_lose_to_the_market():
    agg, dec, dm = _bundles()
    assert gate_verdict(agg, dec, dm)["real"] is True         # all five criteria clear
    bad = bp.params_parity_check({"defaults": 20}, LIVE, live_ok=False)
    v = gate_verdict(agg, dec, dm, params_parity=bad)
    assert v["real"] is False
    assert any("params parity" in r for r in v["reasons"])
    assert v["criteria"]["params_parity"] is False


def test_params_parity_none_means_unchecked_not_passed():
    agg, dec, dm = _bundles()
    assert gate_verdict(agg, dec, dm)["criteria"]["params_parity"] is None
    ok = bp.params_parity_check({"defaults": 20}, {}, live_ok=None)
    assert gate_verdict(agg, dec, dm, params_parity=ok)["criteria"]["params_parity"] is True


def test_the_two_parities_are_independent_criteria():
    """Branch and params are different questions and must be able to fail separately —
    collapsing them would let a params failure hide behind a branch pass."""
    agg, dec, dm = _bundles()
    pp = bp.params_parity_check({"defaults": 20}, LIVE, live_ok=False)
    v = gate_verdict(agg, dec, dm, parity={"parity": True}, params_parity=pp)
    assert v["criteria"]["branch_parity"] is True
    assert v["criteria"]["params_parity"] is False
    assert v["real"] is False


def test_the_counterfactual_can_only_tighten_the_gate():
    """The re-score is in-sample generous, so it is admitted as an AND and never as a
    substitute. A verdict that FAILS the five criteria cannot be rescued by a passing
    counterfactual — which is the KXFED shape (live params 0.00206 beat the market that
    the default 0.00307 lost to) and must stay refused."""
    agg, dec, dm = _bundles()
    agg = {**agg, "brier_model-1h": 0.20}                     # loses to market 0.09
    good = bp.params_parity_check({"defaults": 20}, LIVE, live_ok=True)
    v = gate_verdict(agg, dec, dm, params_parity=good)
    assert v["criteria"]["params_parity"] is True
    assert v["real"] is False and any("brier model" in r for r in v["reasons"])


# ── replay_series: the PIT reader ────────────────────────────────────────────


@pytest.mark.parametrize("kw", [
    {"params": {"a": 1}, "params_pit": True},
    # #199 added a third: the set a SIMULATED selector would have chosen. Every pair is
    # ambiguous, not just the original one — the failure mode is identical.
    {"params": {"a": 1}, "params_at": (lambda _asof: {"b": 2})},
    {"params_pit": True, "params_at": (lambda _asof: {"b": 2})},
    {"params": {"a": 1}, "params_pit": True, "params_at": (lambda _asof: {"b": 2})},
])
def test_the_three_params_questions_cannot_be_asked_together(kw):
    """One fixed set over all history, the set production adopted at each event, and the
    set a simulated selector would have chosen there are three different questions.
    Answering only one of them silently is how #198 happened in the first place, so the
    ambiguous call is an error rather than a precedence rule."""
    from prediction_market_macro.research.backtest import replay_series
    with pytest.raises(ValueError, match="three different questions"):
        replay_series(None, "KXWTIW", **kw)

    from prediction_market_macro.research.eval import decision_replay
    with pytest.raises(ValueError, match="three different questions"):
        decision_replay(None, "KXWTIW", **kw)


def test_params_at_alone_is_not_flagged_as_ambiguous():
    """`params_at=<callable>` with neither other knob is the ONLY way walkforward calls
    these, so a guard that tripped on it would take the whole harness down."""
    from prediction_market_macro.research.backtest import replay_series
    with pytest.raises(Exception) as e:                # dies later, on conn=None
        replay_series(None, "KXWTIW", params_at=lambda _asof: None)
    assert "three different questions" not in str(e.value)


def _ladder_db(tmp_path, name="p.db", periods=(("26MAR26", "2026-03-26T12:25:00"),)):
    """Settled legs, with a quote, for one or more claims events. Enough for either
    replay to reach the params read; the model itself will raise on the empty store,
    which is fine — the read happens first and that is what is under test."""
    from prediction_market_macro.ingest.store import init_db
    conn = init_db(tmp_path / name)
    for token, close in periods:
        close_ts = datetime.fromisoformat(close + "+00:00")
        for strike, result in ((200_000, "yes"), (210_000, "no")):
            t = f"KXJOBLESSCLAIMS-{token}-T{strike:g}"
            conn.execute(
                "INSERT INTO contracts(ticker, series, event_ticker, period,"
                " strike_type, floor_strike, close_time, first_seen_ts)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (t, "KXJOBLESSCLAIMS", "E", token, "greater_or_equal", float(strike),
                 close_ts.isoformat(), "2026-03-01T00:00:00+00:00"))
            conn.execute(
                "INSERT INTO settlements(ticker, series, period, result, first_seen_ts)"
                " VALUES(?,?,?,?,?)",
                (t, "KXJOBLESSCLAIMS", token, result, "2026-03-27T00:00:00+00:00"))
            # decision_replay scans daily entry candidates and skips a day with no book
            # at all, so without a quote it never reaches the params read.
            for d in range(9):
                end = close_ts - timedelta(days=d)
                conn.execute(
                    "INSERT OR REPLACE INTO candles(ticker, end_ts, yes_bid_close,"
                    " yes_ask_close) VALUES(?,?,?,?)",
                    (t, int(end.timestamp()), 0.40, 0.44))
    conn.commit()
    return conn


def test_the_pit_reader_is_params_asof_not_current(tmp_path, monkeypatch):
    """`current()` reads the manual override at WALL-CLOCK now. Used per event it would
    paint today's adoption over every past event — the in-sample number wearing the
    honest number's name. `params_asof` is the PIT reader (added 2026-08-12 after the
    health canary re-predicted at defaults and force-exited three live positions 49
    minutes before a CPI print), and it is the one that must be called.

    Behavioural rather than source-inspecting: both functions legitimately DISCUSS
    `current()` in prose, and a test that greps for it grades the comments.
    """
    from prediction_market_macro.research import param_select
    from prediction_market_macro.research.backtest import replay_series
    from prediction_market_macro.research.eval import decision_replay

    for fn, kw in ((replay_series, {"asof_offsets": ("-1h",)}), (decision_replay, {})):
        calls = {"asof": [], "current": 0}

        def spy_asof(_c, _s, asof, _calls=calls):
            _calls["asof"].append(asof)
            return {}

        def spy_current(*_a, _calls=calls, **_k):
            _calls["current"] += 1
            return {}
        monkeypatch.setattr(param_select, "params_asof", spy_asof)
        monkeypatch.setattr(param_select, "current", spy_current)
        conn = _ladder_db(tmp_path, name=f"{fn.__name__}.db")
        fn(conn, "KXJOBLESSCLAIMS", params_pit=True, **kw)
        assert calls["asof"], f"{fn.__name__} never read the PIT params"
        assert calls["current"] == 0, \
            f"{fn.__name__} read params at wall-clock now, not at the event's asof"


def test_the_pit_reader_is_not_called_when_it_was_not_asked_for(tmp_path, monkeypatch):
    """The production call must stay byte-identical when `params_pit` is off — the same
    contract `test_shadow_claims` pins for `params`, and the reason PR-1's two arms are
    still comparable across this change."""
    from prediction_market_macro.research import param_select
    from prediction_market_macro.research.backtest import replay_series

    hits = []
    monkeypatch.setattr(param_select, "params_asof",
                        lambda *a, **k: hits.append(a) or {})
    replay_series(_ladder_db(tmp_path), "KXJOBLESSCLAIMS", asof_offsets=("-1h",))
    assert hits == []


def test_each_event_gets_the_params_of_its_own_asof(tmp_path):
    """A later adoption must not leak backwards — against a REAL `manual_params` row and
    the real reader, because a test that installs its own boundary only proves it can
    write an if-statement.

    The row is a `experiments` row whose `created_ts` IS the adoption instant (there is no
    `manual_params` table), and the whole PIT contract is the one comparison in
    `manual_params`: `before.isoformat() < created_ts`. An event replayed at an asof a
    minute earlier must see the defaults, and — the half `current()` gets wrong — it must
    keep seeing them however long ago that was.
    """
    from prediction_market_macro.ingest.store import init_db
    from prediction_market_macro.research import param_select

    conn = init_db(tmp_path / "adopt.db")
    cut = datetime(2026, 8, 11, 17, 30, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('manual_params',?,?,?,?,?)",
        ("manual:KXNATGASW:t", "KXNATGASW", "live",
         json.dumps({"active": True, "params": LIVE, "note": "75d argmin, user 08-11"}),
         cut.isoformat()))
    conn.commit()

    read = {a: param_select.params_asof(conn, "KXNATGASW", cut + timedelta(seconds=s))
            for a, s in (("long_before", -86400 * 60), ("just_before", -60),
                         ("at", 0), ("after", 86400 * 14))}
    assert read["long_before"] == {} and read["just_before"] == {}
    assert read["at"] == LIVE and read["after"] == LIVE

    # and a deactivating row is an adoption too: after `clear_manual` the series is back
    # on the defaults, but every event before that instant still ran the adopted set.
    param_select.clear_manual(conn, "KXNATGASW")
    assert param_select.params_asof(conn, "KXNATGASW", cut + timedelta(days=1)) == LIVE
    assert param_select.current(conn, "KXNATGASW") == {}


# ── the mix is counted on the sample the criteria come from ──────────────────


def test_the_scored_mix_is_a_different_denominator_from_the_replayed_mix():
    """KXWTIW replays 156 settled events and scores 14 of them (the rest have no market
    quote at asof). A parity check run against the 156 can show the live config holding a
    comfortable majority while every one of the 14 events behind the gate's number ran
    something else — so `eval.run_series` must read the `_scored` variants."""
    import inspect

    from prediction_market_macro.research import backtest, eval as ev
    src = inspect.getsource(backtest.replay_series)
    assert 'if rec.get(f"brier_model{off}") is not None:' in src, \
        "the scored mixes must be gated on the event actually contributing a Brier"
    run = inspect.getsource(ev.run_series)
    assert 'agg.get("branch_mix_scored-1h")' in run
    assert 'agg.get("params_mix-1h")' in run
    assert 'agg.get("branch_mix-1h")' not in run, \
        "the gate must not read the all-events branch mix"


def test_replay_all_records_the_track_record_not_the_defaults():
    """`replay_all`'s rows are what `health`'s drift detectors watch. At the defaults they
    were structurally blind to the one degradation fully under our control: a parameter
    adoption that makes the model worse."""
    import inspect

    from prediction_market_macro.research import backtest
    assert "replay_series(conn, series, params_pit=True)" in \
        inspect.getsource(backtest.replay_all)


def test_gate_history_does_not_flip_walk_forward_numbers_by_default():
    """#199. The same fix belongs in `pit_gates.GateHistory`, but flipping it there
    without threading `_GateBook`'s own per-day choice would pair a defaults-predicted sim
    with an adopted-params gate state — a third combination that never ran. Off by
    default until that is done, and pinned so the default is not changed by accident."""
    import inspect

    from prediction_market_macro.research.pit_gates import GateHistory
    sig = inspect.signature(GateHistory.__init__)
    assert sig.parameters["params_pit"].default is False
    assert "#199" in GateHistory.__doc__ or "own ticket" in GateHistory.__doc__
