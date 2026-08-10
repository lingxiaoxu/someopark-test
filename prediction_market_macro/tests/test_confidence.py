"""§25.3 — the per-bet confidence model.

The dangerous failure modes here are not crashes, they are silent ones: a gate that fits
on the future, a monotone constraint that is documented but not enforced, an abstention
that reads as a block, or a "lift" that came from dropping trades a K-fold could see and
the live path could not. Each of those gets a test below.
"""
from __future__ import annotations

import numpy as np
import pytest

from prediction_market_macro.research import confidence as cf


def _row(series="KXCPI", period="2026-06", settle="2026-06-10", stream="edge",
         realized=0.5, staked=1.0, **kw):
    base = {"series": series, "period": period, "settle": settle, "day": "2026-06-01",
            "desc": f"{series}-{period}-{stream}", "stream": stream,
            "abs_div": 0.05, "spread": 0.03, "cost": 0.5, "lead_days": 3.0,
            "entropy_norm": 0.6, "skill_ratio": 1.0, "ser_roi": 0.0, "ser_n": 8,
            "realized": realized, "staked": staked, "won": realized > 0}
    base.update(kw)
    return base


def _sep_sample(n=60):
    """A sample where big divergence loses and small divergence wins — the shape the
    prior in FEATURES claims. Used to check the model can find a signal that IS there,
    so that a null result elsewhere means "no signal", not "cannot fit"."""
    rows = []
    for i in range(n):
        big = i % 2 == 0
        rows.append(_row(series=f"S{i % 4}", period=f"p{i}",
                         settle=f"2026-06-{(i % 27) + 1:02d}",
                         abs_div=0.30 if big else 0.02,
                         realized=-0.9 if big else 0.6))
    return rows


# ── monotone constraints are ENFORCED, not just documented ───────────────────────────

def test_every_signed_coefficient_obeys_its_prior():
    """Fit on data engineered to pull every constrained coefficient the WRONG way.

    A sign constraint that is only in the docstring is the worst kind: the report reads
    as if a prior were imposed while the model quietly learned the opposite.
    """
    rows = []
    for i in range(80):
        good = i % 2 == 0
        # every constrained feature is set so that its FORBIDDEN direction predicts a win
        rows.append(_row(series=f"S{i % 3}", period=f"p{i}",
                         settle=f"2026-06-{(i % 27) + 1:02d}",
                         abs_div=0.30 if good else 0.01,     # prior says -, data says +
                         spread=0.20 if good else 0.01,      # prior -, data +
                         cost=0.20 if good else 0.80,        # prior +, data -
                         lead_days=9.0 if good else 1.0,     # prior -, data +
                         entropy_norm=0.9 if good else 0.2,  # prior -, data +
                         skill_ratio=2.0 if good else 0.5,   # prior -, data +
                         ser_roi=-0.3 if good else 0.3,      # prior +, data -
                         realized=0.6 if good else -0.9))
    m = cf.fit(rows)
    assert m is not None
    for name, sign, _why in cf.FEATURES:
        b = m.coefficients()[name]
        if sign > 0:
            assert b >= 0, f"{name} has prior + but fitted {b}"
        elif sign < 0:
            assert b <= 0, f"{name} has prior - but fitted {b}"


def test_abs_div_is_free_in_both_directions():
    """`abs_div` lost its negative prior on 2026-08-05 (PREREGISTER PR-5 amendment A):
    the divergence table that justified it inverts on post-§25.2a data, and the axis is
    confounded with price level anyway (at cost 0.90 a +0.10 divergence needs fair >=
    1.00). A leftover constraint here would silently forbid the model from reporting the
    direction the corrected data actually shows."""
    def sample(big_div_wins):
        rows = []
        for i in range(60):
            big = i % 2 == 0
            win = big if big_div_wins else not big
            rows.append(_row(series=f"S{i % 3}", period=f"p{i}",
                             settle=f"2026-06-{(i % 27) + 1:02d}",
                             abs_div=0.30 if big else 0.02,
                             realized=0.6 if win else -0.9))
        return rows
    hi = cf.fit(sample(True)).coefficients()["abs_div"]
    lo = cf.fit(sample(False)).coefficients()["abs_div"]
    assert hi > 0 > lo, f"abs_div did not move both ways: {hi} / {lo}"


def test_the_free_coefficient_is_actually_free():
    """`is_argmax` is a feature allowed to take either sign — that is §25.6's question
    being asked inside the model. If it were accidentally constrained the model could
    never report "argmax is worse", only "argmax is not better"."""
    def sample(argmax_wins):
        rows = []
        for i in range(60):
            am = i % 2 == 0
            win = am if argmax_wins else not am
            rows.append(_row(series=f"S{i % 3}", period=f"p{i}",
                             settle=f"2026-06-{(i % 27) + 1:02d}",
                             stream="argmax" if am else "edge",
                             realized=0.6 if win else -0.9))
        return rows
    hi = cf.fit(sample(True)).coefficients()["is_argmax"]
    lo = cf.fit(sample(False)).coefficients()["is_argmax"]
    assert hi > 0 > lo, f"is_argmax did not move both ways: {hi} / {lo}"


def test_it_can_find_a_signal_that_is_really_there():
    m = cf.fit(_sep_sample())
    p = m.predict(_sep_sample())
    big = np.array([r["abs_div"] > 0.1 for r in _sep_sample()])
    assert p[big].mean() < p[~big].mean() - 0.2


# ── PIT: the fold must not see the future ────────────────────────────────────────────

def test_loeo_forward_trains_only_on_strictly_earlier_settlements(monkeypatch):
    """Capture what each fold was handed and assert no training row settles on or after
    the row being scored. This is the same contract `pit_gates.asof` enforces, and it is
    the single assumption the whole §25.3 number rests on."""
    seen: list[list[str]] = []
    real_fit = cf.fit

    def spy(train, **kw):
        seen.append(sorted(r["settle"] for r in train))
        return real_fit(train, **kw)
    monkeypatch.setattr(cf, "fit", spy)

    rows = _sep_sample(80)
    scored = cf.loeo_forward(rows)
    assert len(scored) == len(rows)
    assert seen, "no fold ever called fit"

    # `loeo_forward` walks settlement dates in order and calls `fit` once per date, but
    # only once the strictly-earlier slice reaches MIN_TRAIN — so the recorded slices line
    # up with the ELIGIBLE dates, not with all of them.
    dates = sorted({r["settle"] for r in rows})
    eligible = [d for d in dates
                if sum(1 for r in rows if r["settle"] < d) >= cf.MIN_TRAIN]
    assert len(seen) == len(eligible) and eligible
    for settles, scored_date in zip(seen, eligible):
        assert all(s < scored_date for s in settles), (
            f"fold for {scored_date} trained on {[s for s in settles if s >= scored_date]}")


def test_same_day_settlements_share_one_model():
    """Two events settling the same day must be scored by the same fold. Splitting them
    would let the first one's outcome inform the second, which no live path can do."""
    rows = _sep_sample(80)
    for r in rows[:6]:
        r["settle"] = "2026-06-20"
    scored = {(*[r[k] for k in ("series", "period")],): r
              for r in cf.loeo_forward(rows) if r["settle"] == "2026-06-20"}
    stars = {r["p_star"] for r in scored.values()}
    assert len(stars) == 1, "same settlement date was scored by more than one model"


def test_the_coefficient_path_walks_the_same_folds_as_the_scorer(monkeypatch):
    """#137's readout and the gate's own scores must come from ONE validation.

    `coef_path` and `loeo_forward` both walk `_folds`. If a later edit gave either its own
    loop, the reported coefficient path would describe a model that never scored anything
    — a stability claim about the wrong fit. Recording the training slices from both and
    demanding they match is the cheapest way to keep that honest.
    """
    rows = _sep_sample(80)

    def record(fn):
        seen: list[list[str]] = []
        real = cf.fit

        def spy(train, **kw):
            seen.append(sorted(r["settle"] for r in train))
            return real(train, **kw)
        monkeypatch.setattr(cf, "fit", spy)
        fn(rows)
        monkeypatch.setattr(cf, "fit", real)
        return seen

    scorer = record(cf.loeo_forward)
    path = record(cf.coef_path)
    assert scorer and scorer == path


def _reversal_rows(n_early=24, n_late=96):
    """argmax wins throughout June, then loses throughout July. Two rows per settlement
    date so the fold boundaries land inside each block."""
    rows = []
    for i in range(n_early):
        am = i % 2 == 0
        rows.append(_row(series=f"S{i % 3}", period=f"e{i}",
                         settle=f"2026-06-{(i // 2) + 1:02d}",
                         stream="argmax" if am else "edge",
                         realized=0.6 if am else -0.9))
    for i in range(n_late):
        am = i % 2 == 0
        rows.append(_row(series=f"S{i % 3}", period=f"l{i}",
                         settle=f"2026-07-{(i // 2) + 1:02d}",
                         stream="argmax" if am else "edge",
                         realized=-0.9 if am else 0.6))
    return rows


def test_the_coefficient_path_reports_a_flip_when_the_data_flips():
    """A coefficient that means one thing early and the opposite late must NOT come out
    looking stable. This is the whole reason the path sits next to the full-sample row: an
    average of +0.4 and -0.4 is a confident-looking zero."""
    p = cf.coef_path(_reversal_rows())["is_argmax"]
    assert p["n_folds"] > 1
    assert p["n_sign_flips"] == 1, f"the reversal was not reported once: {p}"
    assert p["min"] < 0 < p["max"]
    assert p["first"] > 0 > p["last"]


def test_a_reversal_that_the_accumulated_data_outvotes_decays_instead_of_flipping():
    """The folds are EXPANDING, not sliding — fold k trains on everything before it. So a
    late reversal that the earlier data outweighs shows up as the coefficient DECAYING
    toward zero, never as a sign flip.

    This is not a defect, it is what an expanding window means, but it governs how the
    real path is read: `n_sign_flips == 0` is much weaker evidence of stability than it
    looks, and the honest statement about a monotone path is "it never wanted the other
    sign at any training size", not "18 independent folds agreed".
    """
    p = cf.coef_path(_reversal_rows(n_early=80, n_late=40))["is_argmax"]
    assert p["n_sign_flips"] == 0
    assert p["last"] < p["max"] / 2, f"the late reversal left no trace at all: {p}"


def test_a_path_that_crosses_through_zero_still_counts_as_one_reversal():
    """Found by the test above: `is_argmax` reversed from +1.63 to -1.24 and reported ZERO
    flips, because one fold rounded to exactly 0.0 and a pairwise comparison saw +,0 then
    0,- — neither a flip. A coefficient resting on a constraint bound does the same thing
    for many folds in a row. Zeros are stepped over, not treated as a sign."""
    assert cf._sign_flips([1.6, 0.0, -1.2]) == 1
    assert cf._sign_flips([1.6, 0.0, 0.0, 0.0, -1.2]) == 1
    assert cf._sign_flips([0.0, 0.0, 0.0]) == 0
    assert cf._sign_flips([1.0, -1.0, 1.0]) == 2
    assert cf._sign_flips([0.0, 1.0, 2.0]) == 0


def test_a_clipped_prior_is_visible_as_a_zero_and_not_as_a_small_number():
    """A bound sign constraint has to be READABLE as one. If `n_zero` counted near-zeroes
    instead of exact ones, a genuinely tiny positive coefficient would be indistinguishable
    from a constraint that bound, and those two mean opposite things.

    This test says nothing about §25.4. It once claimed to — the docstring read "clipping
    to zero in every fold is §25.4's premise failing" — and #146 showed that inference does
    not hold on the real column, which is truncated at the premise's own sign boundary.
    The synthetic rows below have `ser_roi` on every row and both signs present, which is
    exactly what the real data does NOT have; see `FEATURES` and §25.17.
    """
    rows = []
    for i in range(80):
        good = i % 2 == 0
        rows.append(_row(series=f"S{i % 3}", period=f"p{i}",
                         settle=f"2026-06-{(i % 27) + 1:02d}",
                         ser_roi=-0.3 if good else 0.3,     # prior +, data says -
                         realized=0.6 if good else -0.9))
    p = cf.coef_path(rows)["ser_roi"]
    assert p["n_zero"] == p["n_folds"] > 0
    assert p["min"] == p["max"] == 0.0


def test_the_path_is_empty_rather_than_invented_when_nothing_can_fit():
    rows = _sep_sample(cf.MIN_TRAIN - 2)
    assert all(v == {"n_folds": 0} for v in cf.coef_path(rows).values())


def test_early_folds_abstain_instead_of_guessing():
    scored = cf.loeo_forward(_sep_sample(80))
    assert scored[0]["p"] is None
    assert any(r["p"] is not None for r in scored), "never fitted at all"


def test_abstention_lets_the_bet_through():
    """A gate that has not been trained must subtract nothing. `evaluate` implements the
    rule; this pins it, because an abstain read as a block would look like a huge ROI
    lift produced entirely by not betting."""
    rows = _sep_sample(80)
    out = cf.evaluate(rows)
    scored = cf.loeo_forward(rows)
    n_abstain = sum(1 for r in scored if r["p"] is None)
    assert out["n_abstained"] == n_abstain > 0
    assert out["n_allowed"] + out["n_blocked"] == len(scored)


# ── the threshold ────────────────────────────────────────────────────────────────────

def test_p_star_is_the_breakeven_of_the_training_payoffs():
    rows = [_row(period=f"p{i}", realized=0.5, staked=1.0) for i in range(4)]
    rows += [_row(period=f"q{i}", realized=-1.0, staked=1.0) for i in range(4)]
    # w = +0.5, l = -1.0  =>  p* = 1.0 / 1.5
    assert cf.p_star(rows) == pytest.approx(1.0 / 1.5, abs=1e-6)


def test_p_star_does_not_look_at_the_scored_fold():
    """Each fold's threshold comes from its own training slice — not from all trades."""
    rows = _sep_sample(80)
    scored = cf.loeo_forward(rows)
    stars = {r["p_star"] for r in scored if r["p_star"] is not None}
    assert len(stars) > 1, "p_star was constant, so it was not refit per fold"


def test_degenerate_payoff_slices_fall_back_to_a_half():
    assert cf.p_star([_row(realized=0.5)]) == 0.5          # no losers
    assert cf.p_star([_row(realized=-0.5)]) == 0.5         # no winners
    assert cf.p_star([]) == 0.5


# ── the hybrid rebuild ───────────────────────────────────────────────────────────────

def test_blocking_an_edge_bet_promotes_that_events_favourite():
    """This is the rule `decide_all` runs: the argmax leg is independent of the edge leg,
    so a suppressed edge bet leaves the favourite standing. Getting this wrong would
    credit the gate with avoiding a loss it merely swapped."""
    e = _row(stream="edge", realized=-1.0)
    a = _row(stream="argmax", realized=+0.4)
    assert [r["stream"] for r in cf._hybrid([e, a])] == ["edge"]
    assert [r["stream"] for r in cf._hybrid([a])] == ["argmax"]


def test_hybrid_keeps_argmax_bets_on_events_the_edge_stream_never_touched():
    rows = [_row(period="p1", stream="edge"), _row(period="p2", stream="argmax")]
    assert len(cf._hybrid(rows)) == 2


# ── the aggregate reports what it is ─────────────────────────────────────────────────

def test_verdict_requires_both_conditions():
    good = {"ci": [0.10, 0.40], "crosses_zero": False}
    assert cf._verdict(-0.01, good).startswith("PASS")
    assert cf._verdict(-0.30, good).startswith("FALSIFIED")
    assert cf._verdict(-0.01, {"ci": [-0.1, 0.4],
                               "crosses_zero": True}).startswith("FALSIFIED")
    assert cf._verdict(-0.01, {"ci": None}).startswith("FALSIFIED")


def test_bootstrap_is_clustered_by_event():
    """Both streams' bets on one event settle on the same outcome. Resampling trades
    independently would treat one outcome as two and report a CI about sqrt(2) too tight
    — the mistake #129 made and paid for."""
    blocked = [_row(period=f"p{i}", stream="edge", realized=-1.0) for i in range(10)]
    allowed = [_row(period=f"p{i}", stream="argmax", realized=+0.4) for i in range(10)]
    out = cf.bootstrap_diff(blocked, allowed, n=400)
    # blocked and allowed share every (series, period) key, so a clustered resample must
    # draw them together and the difference must be nearly constant
    assert out["ci"][1] - out["ci"][0] < 1e-6


def test_evaluate_reports_the_per_series_control():
    out = cf.evaluate(_sep_sample(80))
    assert "per_series_control" in out and "criterion" in out
    assert out["hybrid_n_gated"] <= out["hybrid_n_base"], (
        "the gate must only ever subtract bets")


def test_fit_refuses_below_min_train_and_on_one_class():
    assert cf.fit([_row(period=f"p{i}") for i in range(cf.MIN_TRAIN - 1)]) is None
    assert cf.fit([_row(period=f"p{i}", realized=0.5)
                   for i in range(cf.MIN_TRAIN + 5)]) is None


def test_missing_features_are_imputed_from_the_training_median_only():
    rows = _sep_sample(40)
    for r in rows[:5]:
        r["skill_ratio"] = None
    sc = cf.Scaler.fit(rows)
    med = float(np.median([r["skill_ratio"] for r in rows
                           if r["skill_ratio"] is not None]))
    i = cf.FEATURE_NAMES.index("skill_ratio")
    assert sc.med[i] == pytest.approx(med)
    X = sc.transform(rows[:1])
    assert np.isfinite(X).all()


# ── PR-6: the per-bet break-even threshold ───────────────────────────────────────────

def test_the_threshold_is_the_contracts_own_breakeven_plus_one_round_trip():
    """No constant is fitted here. A structure bought at c pays $1, so EV=0 at p=c; the
    wedge is the book's measured taker cost, the same number §25.4 already uses."""
    from prediction_market_macro.strategy import series_enable as se
    assert cf.FEE_WEDGE == se.ON_ROI, "PR-6 registered NO new constant"
    assert cf.bet_threshold({"cost": 0.40}) == pytest.approx(0.40 + cf.FEE_WEDGE)
    assert cf.bet_threshold({"cost": None}) is None


def test_a_contract_too_expensive_to_pay_its_own_fees_is_blocked_unconditionally():
    """cost 0.98 pays 0.02 on a win. 0.026 of fees cannot come out of that at ANY win
    rate, so a threshold above 1.0 is the right answer and must not be clipped back."""
    assert cf.bet_threshold({"cost": 0.98}) > 1.0
    assert not cf.allows({"p": 0.999, "p_star": 0.4, "cost": 0.98}, gate="per_bet")


def test_the_gate_declines_to_act_rather_than_blocking_on_a_missing_field():
    """This gate may only ever SUBTRACT bets, so anything it cannot evaluate must pass.
    An untrained fold and a costless row are both 'declined to act', not 'blocked'."""
    assert cf.allows({"p": None, "p_star": 0.9, "cost": 0.5}, gate="per_bet")
    assert cf.allows({"p": None, "p_star": 0.9, "cost": 0.5}, gate="global")
    assert cf.allows({"p": 0.01, "p_star": 0.9, "cost": None}, gate="per_bet")


def test_the_two_gates_disagree_in_the_direction_pr6_registered():
    """A cheap bet the global scalar blocked, and an expensive one it let through — the
    +0.887 price sieve, in two rows."""
    cheap = {"p": 0.45, "p_star": 0.60, "cost": 0.20}
    dear = {"p": 0.70, "p_star": 0.60, "cost": 0.90}
    assert not cf.allows(cheap, gate="global") and cf.allows(cheap, gate="per_bet")
    assert cf.allows(dear, gate="global") and not cf.allows(dear, gate="per_bet")


def test_global_gate_reproduces_pr5_verbatim():
    """PR-5's falsification has to stay reproducible. The old rule was literally
    `p is None or p >= p_star`, so recompute it by hand and demand an exact match."""
    rows = _sep_sample(80)
    scored = cf.loeo_forward(rows, hierarchical=True)
    old = [r for r in scored if r["p"] is None or r["p"] >= r["p_star"]]
    out = cf.evaluate(rows, gate="global")
    assert out["n_allowed"] == len(old)
    assert out["n_blocked"] == len(scored) - len(old)
    assert out["hybrid_roi_gated"] == pytest.approx(
        round(cf._roi(cf._hybrid(old)), 5))


def test_the_per_bet_gate_is_the_default_and_is_never_graded_on_this_window():
    """The whole point of PR-6's registration: the threshold was chosen after seeing this
    window fail, so a good ROI here is evidence about the choosing. It may not print
    PASS, and it may not print FALSIFIED either."""
    out = cf.evaluate(_sep_sample(80))
    assert out["gate"] == "per_bet"
    assert out["verdict"].startswith("SMOKE ONLY")
    assert "PASS" not in out["verdict"] and "FALSIFIED" not in out["verdict"]
    assert "FORWARD-ONLY" in out["criterion"] and "30 hybrid trades" in out["criterion"]
    # ...but the falsifiable one still grades, on the same data
    assert cf.evaluate(_sep_sample(80), gate="global")["verdict"].split(" ")[0] in (
        "PASS", "FALSIFIED", "no-data")


def test_the_price_sieve_diagnostic_is_reported_for_both_gates():
    """PR-5's located failure was corr(p, cost)=+0.887. corr(p,cost) CANNOT move — same
    model, only the threshold changed — so the number that has to be reported is
    corr(allow, cost), under both gates, off the same scored rows.

    The equality below is the property that the diagnostic is a function of the SCORED
    ROWS, not of which gate the caller asked for; it is not evidence that the two gates
    agree. On this fixture they happen to make identical decisions — `_sep_sample` is
    perfectly separable, so every fitted p is either ~0.06 or ~0.94 while both thresholds
    land in the gap. That the gates CAN diverge is pinned separately, on hand-built rows,
    by `test_the_two_gates_disagree_in_the_direction_pr6_registered`.
    """
    rows = _sep_sample(80)
    for i, r in enumerate(rows):           # give cost some spread to correlate against
        r["cost"] = 0.2 + 0.6 * (i % 5) / 4.0
    out = cf.evaluate(rows, gate="per_bet")
    assert set(out["corr_allow_cost"]) == set(cf.GATES)
    assert all(v is not None for v in out["corr_allow_cost"].values())
    assert "corr_p_cost" in out and "+0.887" in out["corr_note"]
    assert out["corr_allow_cost"] == cf.evaluate(rows, gate="global")["corr_allow_cost"]


def test_the_diagnostic_registers_a_price_sieve_when_there_is_one():
    """The number has to be able to FIRE, or reporting a small one proves nothing. This is
    PR-5's shape in miniature: a threshold that admits the expensive half and blocks the
    cheap half scores near +1; the per-bet threshold on the same rows does not."""
    dear = [{"p": 0.70, "p_star": 0.60, "cost": 0.90} for _ in range(10)]
    cheap = [{"p": 0.45, "p_star": 0.60, "cost": 0.20} for _ in range(10)]
    rows = dear + cheap
    costs = [r["cost"] for r in rows]
    g = cf._point_biserial([cf.allows(r, gate="global") for r in rows], costs)
    pb = cf._point_biserial([cf.allows(r, gate="per_bet") for r in rows], costs)
    assert g == pytest.approx(1.0, abs=1e-9), "the global gate is a pure price sieve here"
    assert pb == pytest.approx(-1.0, abs=1e-9), (
        "and the per-bet threshold reverses it — expensive bets must clear a higher bar")


def test_a_constant_column_reports_no_correlation_rather_than_zero():
    """cost fixed at 0.5 makes the correlation undefined. Printing 0.0 would read as
    'measured, and the sieve is gone'."""
    assert cf._pearson([1.0, 2.0, 3.0], [0.5, 0.5, 0.5]) is None
    assert cf._point_biserial([True, True, True], [0.1, 0.2, 0.3]) is None


def test_p_star_survives_as_a_diagnostic_under_the_new_gate():
    out = cf.evaluate(_sep_sample(80), gate="per_bet")
    assert out["p_star_full_sample"] is not None
    assert out["fee_wedge"] == cf.FEE_WEDGE


def test_an_unknown_gate_is_refused():
    with pytest.raises(ValueError):
        cf.evaluate(_sep_sample(40), gate="whatever")
    with pytest.raises(ValueError):
        cf.allows({"p": 0.5, "p_star": 0.5, "cost": 0.5}, gate="whatever")


def test_the_per_bet_gate_still_only_subtracts():
    out = cf.evaluate(_sep_sample(80), gate="per_bet")
    assert out["hybrid_n_gated"] <= out["hybrid_n_base"]
    assert out["n_allowed"] + out["n_blocked"] == out["n_scored"]
