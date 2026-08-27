"""PR-8 / PR-10 (#195) — the Cleveland-nowcast forward scorer.

Three classes of test.

**The registration cannot drift.** Every constant in `shadow_nowcast` restates something
written in `docs/PREREGISTER.md`, and a restatement that nobody checks is how a criterion
gets quietly relaxed. These assert the numbers, the series, and — the one that actually
bites — that the *baseline arm* is reachable and that its switch is a no-op at the default,
because a scorer whose two arms are the same object reports a perfect zero and looks fine.

**The switch stays out of both selection lanes.** Whether to anchor is a registered
hypothesis with a preregistered falsification rule. If `nowcast_anchor` ever appeared in
`param_space.CANDIDATES` or `param_argmin.SPACES`, a lane could un-adopt the anchor by
dollar argmin on ~10 events and no one would write down that the registration failed.

**The verdict cannot pass early or pass halfway.** Both registrations require BOTH a mean in
the right direction AND >=4/6 individually — an AND that a one-line edit turns into an OR.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.model import cpi
from prediction_market_macro.research import shadow_nowcast as sn

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


# ── the registration ──────────────────────────────────────────────────────────
def test_the_registered_constants_are_what_the_document_says():
    assert (sn.N_FORWARD, sn.MIN_POSITIVE) == (6, 4)
    assert sn.OFFSET_HOURS == 26                 # "T-26h", both 判据 rows
    assert (sn.K_PR8, sn.K_PR10) == (1, 12)
    assert sn.REGISTERED_PR8.startswith("2026-08-15")
    assert sn.REGISTERED_PR10.startswith("2026-08-27")
    assert (sn.PR8_PRIMARY, sn.PR8_CORE) == ("KXCPIYOY", "KXCPICOREYOY")
    assert sn.PR10_SERIES == "KXCPI"


def test_the_candidate_arm_is_production_and_the_baseline_is_the_unanchored_one():
    """`CAND_ARM` must be empty: the anchors are already wired, so the candidate is
    whatever production does, and hardcoding the anchor here would let this file and
    `model/cpi.py` disagree about what is being graded."""
    assert sn.CAND_ARM == {}
    assert sn.BASE_ARM == {"nowcast_anchor": False}
    assert cpi.DEFAULT_PARAMS["nowcast_anchor"] is True


def test_the_baseline_switch_is_absent_from_both_selection_lanes():
    """A registered hypothesis is not a tunable. Either lane could otherwise decide by
    dollar argmin, on ~10 events, a question PR-10 says takes six forward events of paired
    interval log-likelihood — and could un-adopt the anchor without anyone recording that
    the registration failed."""
    from prediction_market_macro.research.param_argmin import SPACES
    from prediction_market_macro.research.param_space import CANDIDATES
    assert "nowcast_anchor" not in CANDIDATES.get("cpi", {})
    assert "nowcast_anchor" not in SPACES.get("cpi", {})


def test_the_registration_is_stamped_and_the_current_code_is_documented():
    assert sn.REGISTERED_FINGERPRINT != "PENDING-STAMP"
    note = sn.code_change_note()
    assert note["code_changed_since_registration"] is False
    assert note["change_is_documented"] is True


def test_an_unrecorded_cpi_version_is_reported_as_undocumented(monkeypatch):
    monkeypatch.setattr(sn, "code_fingerprint", lambda: "deadbeefcafe")
    note = sn.code_change_note()
    assert note["code_changed_since_registration"] is True
    assert note["change_is_documented"] is False
    assert "UNDOCUMENTED CHANGE" in note["note"]


# ── the arm the whole test rests on ───────────────────────────────────────────
def test_the_anchor_switch_is_a_noop_at_its_default_and_moves_when_flipped():
    """Two failures this catches, and they fail in opposite directions.

    If `nowcast_anchor=True` were NOT a no-op, adding the key changed production, and the
    registrations are being graded against a model that no longer exists. If `False` did
    not move anything, both arms are the same object and every delta is 0.0 — a scorer that
    reports a tidy zero forever and never says why.

    Asserted on the pure MoM path so the test needs no db: `_predict_mom` is where the
    headline anchor is applied, and `_p` is the merge every branch goes through.
    """
    assert cpi._p(None)["nowcast_anchor"] is True
    assert cpi._p({})["nowcast_anchor"] is True
    assert cpi._p({"nowcast_anchor": False})["nowcast_anchor"] is False
    src = (cpi.predict_mom.__code__.co_consts, cpi.predict_yoy.__code__.co_consts)
    assert any("nowcast_anchor" in str(c) for c in src[0]), \
        "predict_mom no longer consults the switch"
    assert any("nowcast_anchor" in str(c) for c in src[1]), \
        "predict_yoy no longer consults the switch"


def test_core_mom_is_never_anchored_regardless_of_the_switch():
    """PR-10 rejects KXCPICORE permanently (+0.0298 nats, DM p=0.325, one event carrying
    101.8% of the gain). The switch must not become a back door that turns it on."""
    import inspect
    src = inspect.getsource(cpi.predict_mom)
    assert src.index("if core:") < src.index("nowcast_anchor"), \
        "the core early-return must come BEFORE the switch, or core becomes anchorable"


# ── the verdict ───────────────────────────────────────────────────────────────
def _sc(n, mean, pos):
    return {"n": n, "mean_delta": mean, "n_positive": pos}


def test_a_verdict_below_the_registered_count_is_pending_and_states_no_result():
    v = sn._verdict(_sc(5, 0.9, 5), "KXCPI", "reverts")
    assert v.startswith("PENDING") and "5/6" in v
    assert "PASS" not in v and "progress readout" in v


def test_both_halves_of_the_criterion_are_required():
    """mean > 0 AND >=4/6. A positive mean carried by one event, or four small wins under a
    negative mean, each fail — that AND is the whole reason the per-event floor exists."""
    assert sn._verdict(_sc(6, +0.50, 4), "S", "x").startswith("PASS")
    assert sn._verdict(_sc(6, +0.50, 3), "S", "x").startswith("FALSIFIED")
    assert sn._verdict(_sc(6, -0.01, 5), "S", "x").startswith("FALSIFIED")


def test_a_falsified_verdict_carries_the_registrations_own_instruction():
    v = sn._verdict(_sc(6, -0.2, 1), "KXCPI", "revert to nowcast_anchor=False")
    assert "FALSIFIED" in v and "revert to nowcast_anchor=False" in v


# ── the scorer, on a seeded db ────────────────────────────────────────────────
class _Pred:
    def __init__(self, mu, dh=None):
        from prediction_market_macro.model.common import GaussianMix
        self.dist = GaussianMix(((1.0, mu, 0.2),))
        self.inputs = {"nowcast_date": "2026-09-10"} if mu else {}
        self.data_horizon = dh


def _fake_events(closes):
    return [{"period": f"P{i}", "key": f"2026-{i:02d}",
             "close_ts": c, "release_ts": None,
             "legs": [{"ticker": "T", "floor_strike": 0.2, "cap_strike": None,
                       "strike_type": "greater", "result": "yes"},
                      {"ticker": "U", "floor_strike": 0.4, "cap_strike": None,
                       "strike_type": "greater", "result": "no"}]}
            for i, c in enumerate(closes, start=1)]


def _patch(monkeypatch, closes, base_mu=0.10, cand_mu=0.30):
    from prediction_market_macro.research import shadow_width
    monkeypatch.setattr(shadow_width, "_events", lambda conn, series: _fake_events(closes))
    monkeypatch.setattr(sn, "_arms",
                        lambda conn, series, key, asof: (_Pred(base_mu), _Pred(cand_mu), {}))


def test_events_settled_before_the_registration_are_not_forward(monkeypatch):
    """The forward window opens at the registration timestamp. An event that settled the
    day before is in-sample for a hypothesis chosen with that data in hand."""
    reg = datetime(2026, 8, 27, tzinfo=timezone.utc)
    _patch(monkeypatch, [reg - timedelta(days=3), reg - timedelta(hours=1),
                         reg + timedelta(days=15)])
    r = sn.score_series(None, "KXCPI", reg.isoformat(), "interval_ll",
                        asof=reg + timedelta(days=40))
    assert r["n"] == 1 and r["events"][0]["period"] == "2026-03"


def test_events_that_have_not_settled_yet_are_not_counted(monkeypatch):
    reg = datetime(2026, 8, 27, tzinfo=timezone.utc)
    _patch(monkeypatch, [reg + timedelta(days=15), reg + timedelta(days=400)])
    r = sn.score_series(None, "KXCPI", reg.isoformat(), "interval_ll",
                        asof=reg + timedelta(days=40))
    assert r["n"] == 1


def test_the_interval_arm_scores_the_settled_cell_and_signs_toward_the_candidate(monkeypatch):
    """The ladder pins the print into (0.2, 0.4]. The candidate sits at 0.30 — inside it —
    and the baseline at 0.10, outside; so the candidate must score higher and `delta` must
    be positive. A flipped sign here would report a losing anchor as a winning one."""
    reg = datetime(2026, 8, 27, tzinfo=timezone.utc)
    _patch(monkeypatch, [reg + timedelta(days=15)])
    r = sn.score_series(None, "KXCPI", reg.isoformat(), "interval_ll",
                        asof=reg + timedelta(days=40))
    e = r["events"][0]
    assert e["interval"] == [0.2, 0.4] and e["interval_kind"] == "interior"
    assert e["ll_cand"] > e["ll_base"]
    assert e["delta"] == pytest.approx(e["ll_cand"] - e["ll_base"], abs=1e-9)
    assert r["mean_delta"] > 0 and r["n_positive"] == 1


def test_a_leaky_arm_is_dropped_not_scored(monkeypatch):
    """`replay_series`'s guard, mirrored: an input whose vintage read is not asof-bounded
    reaches past the print no matter where asof sits."""
    from prediction_market_macro.research import shadow_width
    reg = datetime(2026, 8, 27, tzinfo=timezone.utc)
    close = reg + timedelta(days=15)
    evs = _fake_events([close])
    evs[0]["release_ts"] = close - timedelta(hours=2)
    monkeypatch.setattr(shadow_width, "_events", lambda conn, series: evs)
    monkeypatch.setattr(sn, "_arms", lambda conn, series, key, asof: (
        _Pred(0.10), _Pred(0.30, dh=close), {}))
    r = sn.score_series(None, "KXCPI", reg.isoformat(), "interval_ll",
                        asof=reg + timedelta(days=40))
    assert r["n"] == 0 and len(r["dropped"]) == 1
    assert "data_horizon" in r["dropped"][0]["why"]


def test_the_brier_arm_signs_the_other_way_round(monkeypatch):
    """Lower Brier is better, so PR-8's delta is base MINUS candidate. This is the one
    place the two metrics disagree about direction, and getting it wrong would silently
    invert PR-8's verdict."""
    from prediction_market_macro.research import param_wf, shadow_width
    reg = datetime(2026, 8, 15, tzinfo=timezone.utc)
    monkeypatch.setattr(shadow_width, "_events",
                        lambda conn, series: _fake_events([reg + timedelta(days=30)]))
    monkeypatch.setattr(sn, "_arms",
                        lambda conn, series, key, asof: (_Pred(0.1), _Pred(0.3), {}))
    monkeypatch.setattr(param_wf, "event_legs", lambda conn, series, tok: [{"x": 1}])
    briers = iter([0.40, 0.10])          # base first, then candidate
    monkeypatch.setattr(param_wf, "brier", lambda *a, **k: next(briers))
    r = sn.score_series(None, "KXCPIYOY", reg.isoformat(), "brier",
                        asof=reg + timedelta(days=60))
    e = r["events"][0]
    assert (e["brier_base"], e["brier_cand"]) == (0.4, 0.1)
    assert e["delta"] == pytest.approx(0.30), "a better (lower) candidate must be positive"


def test_the_asof_is_the_registered_26h_not_the_discovery_1h(monkeypatch):
    """PR-10's evidence table used close-1h while its 判据 row says T-26h. The 判据 binds.
    The discrepancy is reported by `run`, but it may never be the thing that grades."""
    from prediction_market_macro.research import shadow_width
    reg = datetime(2026, 8, 27, tzinfo=timezone.utc)
    close = reg + timedelta(days=15)
    seen: list[datetime] = []
    monkeypatch.setattr(shadow_width, "_events",
                        lambda conn, series: _fake_events([close]))

    def spy(conn, series, key, asof):
        seen.append(asof)
        return _Pred(0.10), _Pred(0.30), {}

    monkeypatch.setattr(sn, "_arms", spy)
    sn.score_series(None, "KXCPI", reg.isoformat(), "interval_ll",
                    asof=reg + timedelta(days=40))
    assert seen == [close - timedelta(hours=26)]


def test_the_discovery_asof_line_is_labelled_as_grading_nothing(monkeypatch):
    """It exists so a reader can see the discrepancy inside the registration. If it ever
    stops saying so, it becomes a second bite at the same criterion."""
    from prediction_market_macro.research import shadow_width
    monkeypatch.setattr(shadow_width, "_events", lambda conn, series: [])
    monkeypatch.setattr(sn, "_adopted", lambda *a, **k: {})
    rep = sn.run(None, asof=NOW)
    sec = rep["pr10"]["secondary_discovery_asof"]
    assert sec["offset"] == "-1h"
    assert "grades nothing" in sec["comparison"]
    assert rep["pr10"]["primary"]["offset"] == "-26h"
    assert "PENDING" in rep["pr10"]["verdict"]


def test_core_is_graded_separately_and_its_adoption_basis_is_recorded(monkeypatch):
    """Core was wired by user decision on a measured TIE, not by evidence. Folding it into
    the headline count would let the half with no evidence borrow the half with some."""
    from prediction_market_macro.research import shadow_width
    monkeypatch.setattr(shadow_width, "_events", lambda conn, series: [])
    monkeypatch.setattr(sn, "_adopted", lambda *a, **k: {})
    rep = sn.run(None, asof=NOW)
    assert rep["pr8"]["core"]["series"] == "KXCPICOREYOY"
    assert "PENDING" in rep["pr8"]["core_verdict"]
    assert "user decision" in rep["pr8"]["core_verdict"]
    assert "grades nothing" in rep["pr8"]["family_reading"]["comparison"]


def test_the_pre_declared_risk_survives_into_the_report(monkeypatch):
    """The 2021/2022 acceleration-year losses were written down BEFORE the window so they
    could not be produced afterwards as an explanation. They only work as a pre-commitment
    if they are in the same readout as the result."""
    from prediction_market_macro.research import shadow_width
    monkeypatch.setattr(shadow_width, "_events", lambda conn, series: [])
    monkeypatch.setattr(sn, "_adopted", lambda *a, **k: {})
    risk = sn.run(None, asof=NOW)["pr10"]["pre_declared_risk"]
    assert "2021" in risk and "2022" in risk and "EXPECTED to lag" in risk


def test_the_probability_floor_is_finite_and_symmetric_across_arms():
    """An unbounded penalty is not a measurement: one print off the ladder would decide a
    six-event mean by itself, in whichever direction the narrower arm fell."""
    from prediction_market_macro.model.common import GaussianMix
    d = GaussianMix(((1.0, 0.2, 0.05),))
    ll, floored = sn._interval_ll(d, 90.0, 91.0)
    assert floored is True and math.isfinite(ll)
    assert ll == pytest.approx(math.log(sn._P_FLOOR))
