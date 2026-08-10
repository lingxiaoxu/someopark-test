"""PR-1 (#118) — the claims recency candidate, judged forward.

The failure modes for a pre-registered test are not crashes. They are: a criterion that
drifts to match the result, a counter that silently counts nothing, a "paired" comparison
whose two arms were scored on different samples, and a verdict printed before the sample
size was reached. One test each.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import shadow_claims as sc

REG = datetime.fromisoformat(sc.REGISTERED)


@pytest.fixture(scope="module")
def conn():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    return init_db(load_settings().db_path)


def _fake(monkeypatch, weeks, *, cand=None, default=None, market=None, drop_cand=()):
    """Install `n` synthetic post-registration weeks with the given Brier columns."""
    closes = {f"w{i}": REG + timedelta(days=7 * (i + 1)) for i in range(weeks)}
    monkeypatch.setattr(sc, "_close_times", lambda *_a, **_k: dict(closes))

    def arm(_conn, params):
        is_cand = params is not None
        out = {}
        for i, p in enumerate(closes):
            if is_cand and p in drop_cand:
                continue
            model = (cand if is_cand else default)
            out[p] = {"model": model[i] if isinstance(model, list) else model,
                      "market": market[i] if isinstance(market, list) else market,
                      "n_legs": 10}
        return out
    monkeypatch.setattr(sc, "_arm", arm)
    return closes


# ── the registration is the contract ─────────────────────────────────────────────────

def test_the_constants_still_match_what_was_registered():
    """The single thing this whole route depends on is that nobody edited the criterion
    after seeing a number. `docs/PREREGISTER.md` is the record; these constants are what
    the code actually enforces. If they drift apart the test is the alarm.
    """
    import pathlib
    doc = (pathlib.Path(sc.__file__).resolve().parent.parent
           / "docs" / "PREREGISTER.md").read_text()
    block = doc.split("### PR-1")[1].split("### PR-2")[0]
    assert "2026-07-31" in block and sc.REGISTERED.startswith("2026-07-31")
    assert "(0.0, 0.0, 0.3, 0.7)" in block
    assert tuple(sc.CANDIDATE["level_weights"]) == (0.0, 0.0, 0.3, 0.7)
    assert re.search(r"前向\s*8\s*个已结算周", block) and sc.N_FORWARD == 8
    assert re.search(r"\|\s*K\s*\|\s*1\s*\|", block) and sc.K == 1
    # the registered comparison is against the MARKET. A future edit that quietly makes it
    # candidate-vs-default would be a materially easier bar wearing the same name.
    assert "Brier(market)" in block


def test_the_only_knob_the_candidate_actually_moves_is_the_weights():
    """`seasonal_years=10` is already `DEFAULT_PARAMS`, so the registration lists two
    changes and makes one. Worth a test, because "we changed two things" and "we changed
    one thing" are different multiplicity stories and K was registered as 1."""
    from prediction_market_macro.model.claims import DEFAULT_PARAMS

    def norm(x):
        return tuple(x) if isinstance(x, (tuple, list)) else x

    differing = {k for k, v in sc.CANDIDATE.items()
                 if norm(v) != norm(DEFAULT_PARAMS[k])}
    assert sc.CANDIDATE["seasonal_years"] == DEFAULT_PARAMS["seasonal_years"]
    assert tuple(sc.CANDIDATE["level_weights"]) != tuple(DEFAULT_PARAMS["level_weights"])
    assert differing == {"level_weights"}


# ── no verdict before the registered n ───────────────────────────────────────────────

def test_no_verdict_and_no_p_value_before_the_sample_size_is_reached(monkeypatch, conn):
    _fake(monkeypatch, sc.N_FORWARD - 1, cand=0.10, default=0.16, market=0.15)
    out = sc.run(conn, asof=REG + timedelta(days=365))
    assert out["n_forward"] == sc.N_FORWARD - 1
    assert out["verdict"].startswith("PENDING")
    assert "progress readout" in out["verdict"]


def test_a_week_that_closed_before_the_registration_is_not_counted(monkeypatch, conn):
    closes = {"before": REG - timedelta(days=1), "after": REG + timedelta(days=1)}
    monkeypatch.setattr(sc, "_close_times", lambda *_a, **_k: dict(closes))
    monkeypatch.setattr(sc, "_arm", lambda _c, _p: {
        p: {"model": 0.1, "market": 0.2, "n_legs": 9} for p in closes})
    out = sc.run(conn, asof=REG + timedelta(days=30))
    assert [e["period"] for e in out["events"]] == ["after"]


# ── the bar is the market, and the arms are the same sample ──────────────────────────

def test_beating_the_default_while_losing_to_the_market_is_a_failure(monkeypatch, conn):
    """The discovery quantity was candidate-vs-default; the registered bar is
    candidate-vs-market. A candidate that wins the first and loses the second must be
    FALSIFIED, or the registration bought nothing."""
    _fake(monkeypatch, sc.N_FORWARD, cand=0.14, default=0.16, market=0.09)
    out = sc.run(conn, asof=REG + timedelta(days=365))
    assert out["secondary"]["mean_diff"] < 0        # it does beat the default
    assert out["primary"]["mean_diff"] > 0          # and it does lose to the market
    assert out["verdict"].startswith("FALSIFIED")


def test_a_clear_win_over_the_market_passes(monkeypatch, conn):
    # varied so the signed-rank test has non-tied ranks to work with
    n = sc.N_FORWARD
    _fake(monkeypatch, n, cand=[0.05 + 0.002 * i for i in range(n)],
          default=0.16, market=[0.15 + 0.004 * i for i in range(n)])
    out = sc.run(conn, asof=REG + timedelta(days=365))
    assert out["primary"]["wilcoxon_p_one_sided"] < 0.05
    assert out["verdict"].startswith("PASSED")


def test_an_event_only_one_arm_could_score_is_dropped_and_reported(monkeypatch, conn):
    """If the candidate fails to predict on some week, it must not be graded on the easier
    remaining subset while the market keeps the full one. Intersect, and say so."""
    _fake(monkeypatch, sc.N_FORWARD, cand=0.10, default=0.16, market=0.15,
          drop_cand=("w2",))
    out = sc.run(conn, asof=REG + timedelta(days=365))
    assert out["n_dropped_asymmetric"] == 1 and out["dropped"] == ["w2"]
    assert "w2" not in [e["period"] for e in out["events"]]
    assert out["n_forward"] == sc.N_FORWARD - 1
    assert out["verdict"].startswith("PENDING"), "a short sample still gets no verdict"


# ── the two arms must agree on everything that is not the parameters ─────────────────

def test_replay_series_forwards_params_only_when_they_are_given(conn, monkeypatch):
    """`backtest.replay_series` gained a `params` argument for this module. Production
    calls it without one and must keep making the byte-identical call it always made — a
    model function that does not accept the kwarg would otherwise start raising inside the
    weekly sweep."""
    import prediction_market_macro.model.claims as claims
    seen: list[dict] = []
    real = claims.predict

    def spy(conn_, asof, period, series="KXJOBLESSCLAIMS", **kw):
        seen.append(kw)
        return real(conn_, asof, period, series=series, **kw)
    monkeypatch.setattr(claims, "predict", spy)

    from prediction_market_macro.research.backtest import replay_series
    replay_series(conn, sc.SERIES, asof_offsets=("-1h",), max_events=1)
    assert seen and all("params" not in kw for kw in seen), \
        "the production path started passing a kwarg it never used to pass"

    seen.clear()
    replay_series(conn, sc.SERIES, asof_offsets=("-1h",), max_events=1,
                  params=sc.CANDIDATE)
    assert seen and all(kw.get("params") is sc.CANDIDATE for kw in seen)


def test_the_market_column_is_identical_across_the_two_arms(conn):
    """The market's Brier cannot depend on our parameters — it depends on `asof`, and
    `asof` is params-independent. If this ever fails, something that should not be
    params-dependent is, and the pairing is broken.

    Runs on real data, and on the WHOLE settled history rather than the (currently empty)
    forward window, so it is not vacuous while the forward count is still zero.
    """
    d, c = sc._arm(conn, None), sc._arm(conn, sc.CANDIDATE)
    shared = set(d) & set(c)
    assert len(shared) >= 5, f"too few replayable claims events to test on: {len(shared)}"
    for p in shared:
        assert d[p]["market"] == pytest.approx(c[p]["market"], abs=1e-12), p


def test_the_candidate_actually_changes_the_model(conn):
    """The counterpart to the test above: the parameters must move the MODEL column. If
    `params` were silently dropped somewhere in the chain, every number this module
    produces would be a comparison of the default with itself, and the forward test would
    conclude nothing while looking like it concluded something."""
    d, c = sc._arm(conn, None), sc._arm(conn, sc.CANDIDATE)
    shared = set(d) & set(c)
    assert any(d[p]["model"] != c[p]["model"] for p in shared), \
        "the candidate parameters did not change a single event's Brier"
