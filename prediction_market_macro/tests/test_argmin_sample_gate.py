"""param_argmin's sample gate — a search may not be wider than its sample can resolve.

The standing user policy (2026-08-11) is raw argmin with no DSR deflation, and this gate
does not revisit it. What it bars is the degenerate case the policy never contemplated:
picking a winner out of ~100 candidate sets on 2 settled events. The argmin of K trials
on n events overstates the winner by ~sqrt(2*ln K / n) per-event sd even when every set
is identical, so on 2026-08-20 the live board read KXPAYROLLS at n=2/K=97 = 2.14 sd of
pure selection bias, against KXWTIW at n=10/K=21 = 0.78.

These tests pin the arithmetic, the two behaviours that make the gate safe to leave
running unattended (weekly markets untouched; a gated market adopts NOTHING rather than
adopting `{}` over an existing row), and the honest-logging requirement.
"""
from __future__ import annotations

import math

import pytest

from prediction_market_macro.research import param_argmin as pa


def _bias(k_trials: int, n: int) -> float:
    return math.sqrt(2 * math.log(k_trials) / n)


@pytest.mark.parametrize("n, want_width", [(0, 0), (1, 0), (2, 1), (3, 3), (4, 6)])
def test_sample_cap_is_the_documented_inversion(n, want_width):
    """K <= exp(n*t^2/2) at t=1.0, returned as a WIDTH (trials minus the default row)."""
    assert pa.sample_cap(n) == want_width


def test_the_cap_holds_the_bias_under_the_tolerance():
    """The point of the constant: whatever width survives, the implied bias is <= 1 sd.
    Checked on the trial count the argmin actually chooses among, width+1."""
    for n in range(2, 30):
        trials = pa.sample_cap(n) + 1
        if trials < 2:
            continue
        assert _bias(trials, n) <= pa.ARGMIN_TOLERANCE + 1e-9, f"n={n} trials={trials}"


def test_weekly_markets_are_untouched_by_the_gate():
    """n=10-11 is the weekly regime. The gate must sit far above every static CAP there,
    so those markets keep searching exactly as wide as they did before 2026-08-21."""
    for n in (10, 11):
        assert pa.sample_cap(n) >= max(pa.CAP.values()), f"gate bites at n={n}"


def test_monthly_markets_collapse_to_a_couple_of_sets():
    """n=2 supports no search at all; n=3 supports one narrow key. This is the intended
    bite — the four markets that were running 1.5-2.1 sd of bias."""
    assert pa.sample_cap(2) < 2, "n=2 must not support a multi-valued key"
    assert pa.sample_cap(3) < 4, "n=3 must not support a 4-valued key"


def test_the_tolerance_is_looser_than_the_dsr_lane():
    """The gate is a floor on an intentionally-aggressive lane, not a back-door revert
    of the user's policy. If these ever equalise, that choice has been undone silently."""
    from prediction_market_macro.research.param_space import SELECTION_TOLERANCE
    assert pa.ARGMIN_TOLERANCE > SELECTION_TOLERANCE


def test_build_without_n_events_keeps_the_old_static_cap(monkeypatch):
    """Study scripts reproduce the 2026-08-11 spaces by passing no sample size. That
    path must not silently acquire a gate, or the reproduction is not one."""
    seen = {}

    def fake_live_keys(conn, series, fn, probes, pre):
        seen["probes"] = probes
        return list(probes), []

    monkeypatch.setattr(pa, "live_keys", fake_live_keys)
    monkeypatch.setattr(pa, "settled_events", lambda *a, **k: [])
    monkeypatch.setattr(pa, "_predict_fn", lambda s: None)
    _, rep = pa.build(None, "KXPAYROLLS", None)
    assert rep["cap"] == pa.CAP["KXPAYROLLS"]
    assert rep["sample_cap"] is None
    _, rep2 = pa.build(None, "KXPAYROLLS", None, n_events=2)
    assert rep2["cap"] == pa.sample_cap(2) < pa.CAP["KXPAYROLLS"]


def test_a_gated_market_does_not_adopt_empty_over_an_existing_row(monkeypatch):
    """The failure this prevents: `best_params` is {} when gated, so a naive `changed`
    comparison would call set_manual({}) and silently revert a live adoption. The gate
    bars NEW selection; undoing an old one is a separate, explicit act."""
    adopted = []
    monkeypatch.setattr(pa, "set_manual",
                        lambda *a, **k: adopted.append(a))
    monkeypatch.setattr(pa, "manual_params",
                        lambda *a, **k: ({"w_base": 0.75}, "2026-08-11T00:00:00+00:00"))
    monkeypatch.setattr(pa, "_fingerprint", lambda *a, **k: "fp")
    monkeypatch.setattr(pa, "_last_log", lambda *a, **k: None)
    monkeypatch.setattr(pa, "MARKETS", ["KXPAYROLLS"])
    monkeypatch.setattr(pa, "rescore", lambda *a, **k: {
        "grid": [{}], "grid_report": {"cap": 1, "sample_cap": 1}, "n_events": 2,
        "best_idx": 0, "best_params": {}, "pnl_best": None, "pnl_default": None,
        "gated": True})

    class _Conn:
        def execute(self, *a):
            return None

        def commit(self):
            return None

    from datetime import datetime, timezone
    out = pa.daily(_Conn(), now=datetime(2026, 8, 21, tzinfo=timezone.utc), log=None)
    assert adopted == [], "a gated market must not write a manual_params row"
    assert out["KXPAYROLLS"].startswith("GATED")
    assert "w_base" in out["KXPAYROLLS"], "the log must say what is still in force"
