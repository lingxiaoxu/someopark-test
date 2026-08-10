"""#130 — a wide book must TIGHTEN the sanity gate, never switch it off.

`decide()` documents its model-vs-market gap check as UNCONDITIONAL: a model that disagrees
with the market by more than `max_model_market_gap` is presumed wrong, whatever the
structure type. It reads the reference as::

    mkt = (market_implied or {}).get(st.desc, st.cost)

so passing `market_implied=None` does not disable the gate — it silently repoints it at
`st.cost`, the ask we are about to pay. On a tight book those two are close and the
substitution is harmless. On a WIDE book the ask sits far above the devigged probability,
the gap collapses, and the check waves through precisely the trades it exists to stop.

`decide_all` used to do exactly that (`market_fairs = None` when
`median_spread > WIDE_SPREAD`). Measured on the 75-day walk-forward, the branch fired on
10 of 52 trades and 9 of them carried a devigged gap of 0.27-0.49 while the ask-based gap
read 0.06-0.24 — all under the 0.25 ceiling. The gate was loosest where the quote was
least trustworthy.

The repair keeps both references and picks, per structure, whichever is FURTHER from our
fair. Three properties are worth pinning, because each one is a way the next edit could go
wrong:

  * it can only tighten. No book, wide or tight, gains a trade from this.
  * it degrades to today's behaviour when devig is unavailable, so a book with no
    two-sided quotes is unaffected.
  * it is measured against the CALIBRATED fair, which is why the call site sits after
    `calibrate_structs` in both the live path and the harness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.strategy.decision import GATES, decide
from prediction_market_macro.strategy.edge import Leg, Struct

NOW = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)
CLOSE = NOW + timedelta(days=3)


def _struct(desc: str, fair: float, cost: float) -> Struct:
    return Struct("single", (Leg("T-1", "yes", cost, 1e9),), fair=fair, cost=cost,
                  max_loss=cost, desc=desc)


def _widen(structs, market_fairs):
    """The expression both call sites use, kept in one place so the test pins the rule
    rather than a transcription of it."""
    mf = market_fairs or {}
    return {st.desc: max(mf.get(st.desc, st.cost), st.cost,
                         key=lambda p, f=st.fair: abs(f - p))
            for st in structs}


def _decide(structs, market_fairs):
    return decide(structs, now=NOW, close_time=CLOSE, release_ts=None,
                  market_implied=market_fairs, already_open=False, bankroll=100.0,
                  gates=GATES)


# the wide-book trade the old branch admitted: model 0.94, ask 0.84, devigged 0.455.
# |0.94 - 0.84| = 0.10 clears the 0.25 gate; |0.94 - 0.455| = 0.485 does not.
# (KXJOBLESSCLAIMS 2026-05-28, median spread 0.18, realised -0.85.)
WIDE = _struct("YES T-1", fair=0.94, cost=0.84)
DEVIG = {"YES T-1": 0.455}


def test_the_old_fallback_admitted_a_trade_the_devigged_gate_rejects():
    assert _decide([WIDE], None).action == "open"          # market_fairs=None: the bug
    assert _decide([WIDE], DEVIG).action == "pass"
    assert any(r.startswith("sanity_gap") for r in _decide([WIDE], DEVIG).reasons)


def test_a_wide_book_now_keeps_the_stricter_reference():
    d = _decide([WIDE], _widen([WIDE], DEVIG))
    assert d.action == "pass"
    assert any("sanity_gap" in r for r in d.reasons)


def test_widening_falls_back_to_cost_when_devig_is_unavailable():
    # no two-sided quotes anywhere ⇒ no devigged pmf ⇒ the rule must not invent one
    assert _widen([WIDE], None) == {"YES T-1": WIDE.cost}
    assert _decide([WIDE], _widen([WIDE], None)).action == "open"


@pytest.mark.parametrize("fair,cost,devig", [
    (0.94, 0.84, 0.455),        # devigged is further — the wide-book case
    (0.70, 0.49, 0.445),        # KXAAAGASW 2026-06-01
    (0.60, 0.58, 0.62),         # cost is further, barely
    (0.50, 0.50, 0.50),         # degenerate: everything agrees
    (0.10, 0.90, 0.50),         # both references miles away
])
def test_widening_can_only_tighten_never_loosen(fair, cost, devig):
    """The chosen reference's gap is >= the gap under EITHER original rule."""
    st = _struct("YES T-1", fair, cost)
    chosen = _widen([st], {"YES T-1": devig})["YES T-1"]
    assert abs(fair - chosen) >= abs(fair - cost) - 1e-12
    assert abs(fair - chosen) >= abs(fair - devig) - 1e-12


def test_no_structure_gains_admission_from_the_change():
    """Across a book, the widened rule opens nothing the devigged rule refused."""
    structs = [_struct(f"S{i}", fair=0.05 * i, cost=0.5) for i in range(1, 20)]
    devig = {st.desc: 0.5 for st in structs}
    before = _decide(structs, devig)
    after = _decide(structs, _widen(structs, devig))
    if before.action == "pass":
        assert after.action == "pass"
    else:
        # if it still opens, it cannot have moved to a structure the strict gate barred
        assert abs(after.struct.fair - devig[after.struct.desc]) \
            <= GATES["max_model_market_gap"]


def test_both_call_sites_apply_the_rule_after_calibration():
    """Calibration moves `st.fair`, and the rule is measured against it.

    Pinned as source text because the failure mode is silent: computing the widening
    where `market_fairs` is BUILT (before `calibrate_structs`) still runs, still produces
    a dict, and diverges from production only on the wide books this gate exists for.
    """
    import inspect

    from prediction_market_macro.ops import decide_all
    from prediction_market_macro.research import walkforward

    for mod, anchor in ((decide_all, "calibrate_structs"),
                        (walkforward, "gs.calibrate_structs")):
        src = inspect.getsource(mod)
        assert "key=lambda p, f=st.fair: abs(f - p)" in src, mod.__name__
        assert src.index(anchor) < src.index("key=lambda p, f=st.fair"), mod.__name__
