"""Decision model — knockout invariants (plan 20).

A knockout tie has NO draw: the contract settles on who ADVANCES (incl. extra time
and penalties). The pre-match decision must therefore NEVER bet 'draw' in a knockout
match, and must pick the advance side. Group stage keeps the full 3-way value pick.
"""
from prediction_market.ops import upcoming_export as UE
from prediction_market.strategy.decision_model import SideQuote, decide


class _MP:
    """Minimal MatchPrice stand-in (advance prob set as for a knockout tie)."""
    p_home, p_draw, p_away = 0.40, 0.30, 0.30
    p_home_advance = 0.62


_KQ = {"home": {"ask": 0.55}, "draw": {"ask": 0.28}, "away": {"ask": 0.40}}
_PQ = {"home": {"ask": 0.56}, "draw": {"ask": 0.27}, "away": {"ask": 0.41}}
_DEV = {"home": 0.45, "draw": 0.25, "away": 0.30}
_MODEL = {"home": 0.40, "draw": 0.30, "away": 0.30}
_FORM = {"home_z": 0.0, "away_z": 0.0}


def test_knockout_decision_never_draw_and_uses_advance():
    d = UE._decision_for(_MP(), _MODEL, _KQ, _PQ, _DEV, _DEV, _FORM, 0.25, True, knockout=True)
    assert d["bet"] is True
    assert d["side"] in ("home", "away")
    assert d["side"] != "draw"
    assert d.get("knockout") and d.get("advance")
    # advance prob 0.62 → home advances
    assert d["side"] == "home"
    assert abs(d["model_prob"] - 0.62) < 1e-6


def test_knockout_respects_discipline_gate():
    d = UE._decision_for(_MP(), _MODEL, _KQ, _PQ, _DEV, _DEV, _FORM, 0.25, False, knockout=True)
    assert d["bet"] is False and d["side"] is None


def test_decide_skips_side_without_quote():
    # No draw quote → decide() can never return 'draw' (knockout maps to a 2-way market).
    quotes = {"home": SideQuote(ask=0.40, devig=0.42, venue="kalshi"),
              "draw": SideQuote(), "away": SideQuote(ask=0.55, devig=0.50, venue="kalshi")}
    out = decide({"home": 0.62, "draw": 0.0, "away": 0.38}, quotes, gate_open=True)
    assert out.side != "draw"
