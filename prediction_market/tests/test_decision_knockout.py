"""Decision model — per-match knockout semantics (plan 20, corrected after probing the
live venue market structure).

FINDING: the per-match prediction market (Kalshi KXWCGAME / Poly fwc) settles on the
90-MINUTE 3-way result — a draw is a VALID, tradeable outcome even in a knockout tie
(1-1 at 90' pays the "Tie" market; extra time then decides who advances, which is a
SEPARATE per-team reach-round market, KXWCROUND). So the per-match decision is a 3-way
value pick for BOTH stages — a draw IS allowed in knockout here.
"""
from prediction_market.ops import upcoming_export as UE
from prediction_market.strategy.decision_model import SideQuote, decide

# A scenario where the model loves the draw and the market underprices it.
_MODEL = {"home": 0.30, "draw": 0.45, "away": 0.25}
_KQ = {"home": {"ask": 0.40}, "draw": {"ask": 0.28}, "away": {"ask": 0.40}}
_PQ = {"home": {"ask": 0.41}, "draw": {"ask": 0.29}, "away": {"ask": 0.41}}
_DEV = {"home": 0.36, "draw": 0.28, "away": 0.36}
_FORM = {"home_z": 0.0, "away_z": 0.0}


def test_knockout_permatch_is_3way_draw_allowed():
    # Same call, knockout flag True vs False → identical 3-way decision (draw allowed).
    dk = UE._decision_for(_MODEL, _KQ, _PQ, _DEV, _DEV, _FORM, 0.25, True, knockout=True)
    dg = UE._decision_for(_MODEL, _KQ, _PQ, _DEV, _DEV, _FORM, 0.25, True, knockout=False)
    assert dk["bet"] and dk["side"] == "draw", dk      # draw is a valid knockout bet
    assert dg["side"] == dk["side"], (dg, dk)          # stage doesn't change the 3-way pick
    assert dk.get("knockout") is True                  # flag preserved for the reach-round product


def test_knockout_respects_gate():
    d = UE._decision_for(_MODEL, _KQ, _PQ, _DEV, _DEV, _FORM, 0.25, False, knockout=True)
    assert d["bet"] is False and d["side"] is None


def test_decide_can_pick_draw_when_underpriced():
    quotes = {"home": SideQuote(ask=0.40, devig=0.36, venue="kalshi"),
              "draw": SideQuote(ask=0.28, devig=0.28, venue="kalshi"),
              "away": SideQuote(ask=0.40, devig=0.36, venue="kalshi")}
    out = decide(_MODEL, quotes, gate_open=True)
    assert out.side == "draw"
