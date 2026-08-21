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
from datetime import datetime, timezone

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


def test_the_gate_binds_on_the_scored_sample_not_the_quotable_one(monkeypatch):
    """The hole in the first version, found 2026-08-21 while calibrating the DFM sample.

    `quotable_events` and `event_pnl` disagree about what is scoreable: an event the live
    rule would not have traded at all — KXJOBLESSCLAIMS has been skill-BLOCKED since
    2026-07-09 — is quotable and unscoreable, and `score_matrix` drops it for every
    candidate at once. Sizing the grid on the universe therefore let 91 sets be ranked on
    4 events (1.50 sd of selection bias, worse than the KXPAYROLLS case that motivated the
    gate) while the log reported n=10 and looked healthy.
    """
    sizes = []

    def fake_build(conn, series, lo, n_events=None, probed=None):
        sizes.append(n_events)
        width = min(90, pa.sample_cap(n_events))
        return [{}] + [{"w": i} for i in range(width)], {"cap": width}

    def fake_score(conn, series, grid, uni, log=None):
        # only 4 of the 10 quotable events replay, whatever the grid
        kept = uni[:4]
        return kept, [[float(j) for j in range(len(grid))] for _ in kept], []

    monkeypatch.setattr(pa, "probe", lambda *a, **k: ({}, [], []))
    monkeypatch.setattr(pa, "build", fake_build)
    monkeypatch.setattr(pa._ps, "score_matrix", fake_score)
    monkeypatch.setattr(pa._ps, "quotable_events", lambda *a, **k: [
        {"tok": f"t{i}", "close_ts": datetime(2026, 8, 1, tzinfo=timezone.utc)}
        for i in range(10)])
    monkeypatch.setattr(pa, "kalshi_period_to_key", lambda t: "2026-08-01")
    monkeypatch.setattr(pa, "read_synth", lambda *a, **k: (None, {}))

    r = pa.rescore(None, "KXJOBLESSCLAIMS", datetime(2026, 8, 21, tzinfo=timezone.utc))
    assert sizes[0] == 10, "the first grid is necessarily sized on the universe"
    assert sizes[-1] == 4, "and then resized on the sample that was actually ranked"
    assert len(r["grid"]) - 1 <= pa.sample_cap(4)
    assert r["grid_report"]["renarrowed_from"] > len(r["grid"])


def test_narrowing_never_turns_into_widening(monkeypatch):
    """A smaller grid keeps MORE events (fewer sets can fail), so a loop that also widened
    would make the sample size a function of the grid that the grid is a function of."""
    widths = []

    def fake_build(conn, series, lo, n_events=None, probed=None):
        widths.append(n_events)
        return [{}] + [{"w": i} for i in range(min(5, pa.sample_cap(n_events)))], {}

    def fake_score(conn, series, grid, uni, log=None):
        kept = uni[:4] if len(grid) > 3 else uni          # narrow grid -> everything keeps
        return kept, [[1.0] * len(grid) for _ in kept], []

    monkeypatch.setattr(pa, "probe", lambda *a, **k: ({}, [], []))
    monkeypatch.setattr(pa, "build", fake_build)
    monkeypatch.setattr(pa._ps, "score_matrix", fake_score)
    monkeypatch.setattr(pa._ps, "quotable_events", lambda *a, **k: [
        {"tok": f"t{i}", "close_ts": datetime(2026, 8, 1, tzinfo=timezone.utc)}
        for i in range(10)])
    monkeypatch.setattr(pa, "kalshi_period_to_key", lambda t: "2026-08-01")
    monkeypatch.setattr(pa, "read_synth", lambda *a, **k: (None, {}))

    r = pa.rescore(None, "KXWTIW", datetime(2026, 8, 21, tzinfo=timezone.utc))
    assert len(r["grid"]) <= 6, "the grid must not grow back once it has narrowed"


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


# ── the ladder (2026-08-21, for the DFM sample) ─────────────────────────────
def _ladder_probe(monkeypatch):
    monkeypatch.setattr(pa, "probe", lambda *a, **k: ({}, [], []))


def test_grid_ladder_enumerates_every_reachable_grid_once(monkeypatch):
    """`regen` scores the ladder because it cannot know which grid the morning will pick:
    n_eff moves with both settlements and lambda between the two jobs. The ladder must
    therefore be complete (every width the cap can produce) and deduplicated (widths
    repeat across the half-event steps, and each grid costs a full score matrix)."""
    _ladder_probe(monkeypatch)

    def fake_build(conn, series, lo, n_events=None, probed=None):
        width = min(20, pa.sample_cap(n_events))
        return [{}] + [{"w": i} for i in range(width)], {}

    monkeypatch.setattr(pa, "build", fake_build)
    grids, union = pa.grid_ladder(None, "KXWTIW", None)
    widths = [len(g) for g in grids]
    assert widths == sorted(widths), "the ladder must climb"
    assert len(set(widths)) == len(widths), "each reachable width appears once"
    assert widths[-1] == 21, "the ladder runs until the static CAP is reachable"
    assert len(union) == 21, "nested grids union to the widest"


def test_the_union_covers_grids_that_are_not_subsets_of_each_other(monkeypatch):
    """Why a union and not just the widest grid. Narrowing drops whole KEYS, so
    {'vol_window': 26} and {'vol_window': 26, 'clip': 0.15} are different dicts with
    different set_hashes — the narrow grid is NOT contained in the wide one, and scoring
    only the wide one would leave the narrow morning grid unscored."""
    _ladder_probe(monkeypatch)

    def fake_build(conn, series, lo, n_events=None, probed=None):
        if pa.sample_cap(n_events) < 4:
            return [{}, {"a": 1}, {"a": 2}], {}            # one key
        return [{}] + [{"a": i, "b": j} for i in (1, 2) for j in (1, 2)], {}  # two keys

    monkeypatch.setattr(pa, "build", fake_build)
    grids, union = pa.grid_ladder(None, "KXWTIW", None)
    uh = {pa.set_hash(p) for p in union}
    for g in grids:
        assert {pa.set_hash(p) for p in g} <= uh, "a reachable grid fell outside the union"
    assert len(union) == len({pa.set_hash(p) for p in union}), "union must be deduplicated"
    assert len(union) < sum(len(g) for g in grids), "and must share the common rows"


def test_the_ladder_is_probed_once_not_once_per_rung(monkeypatch):
    """`probe` runs the live-key check, which replays 8 settled events through the real
    predictor. Re-running it per rung would make the ladder cost ~15x the widest grid
    instead of ~1.5x, which is the whole reason `probed` is threaded through."""
    calls = []
    monkeypatch.setattr(pa, "probe", lambda *a, **k: (calls.append(1), ({}, [], []))[1])
    monkeypatch.setattr(pa, "build", lambda c, s, lo, n_events=None, probed=None:
                        ([{}] + [{"w": i} for i in range(min(20, pa.sample_cap(n_events)))], {}))
    pa.grid_ladder(None, "KXWTIW", None)
    assert len(calls) == 1


def test_the_ladder_terminates_when_the_cap_is_unreachable(monkeypatch):
    """A degenerate space whose width never grows must not spin to the n>40 backstop
    silently — it should stop as soon as the static cap is reachable, which for a
    one-row grid is immediately."""
    _ladder_probe(monkeypatch)
    monkeypatch.setattr(pa, "build",
                        lambda c, s, lo, n_events=None, probed=None: ([{}], {}))
    grids, union = pa.grid_ladder(None, "KXWTIW", None)
    assert len(union) == 1 and len(grids) == 1
