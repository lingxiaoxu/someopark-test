"""Decision model — per-match knockout semantics (plan 20, corrected after probing the
live venue market structure).

FINDING: the per-match prediction market (Kalshi KXEPLGAME / KXUCLGAME / the Poly
per-match market) settles on the 90-MINUTE 3-way result — a draw is a VALID, tradeable
outcome even in a knockout leg (1-1 at 90' pays the "Tie" market; extra time then decides
who advances, which is a SEPARATE 2-way product, KXUCLADVANCE). So the per-match decision
is a 3-way value pick for BOTH stages — a draw IS allowed in knockout here.
"""
from prediction_market_soccer.ops import upcoming_export as UE
from prediction_market_soccer.strategy.decision_model import SideQuote, decide

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


# ── absurd-edge guard (DecisionConfig.max_net_edge) ───────────────────────────
def test_absurd_edge_is_refused_not_maximised():
    """An edge this far above a quoted market is far likelier to be OUR error than a
    mispricing: every early club bet at +0.25..+0.30 sat on a European qualifier whose
    prior was built from a handful of cup games, and all of them lost. The guard drops
    the absurd side rather than betting it — and must not suppress the sane sides."""
    from prediction_market_soccer.config import CONFIG
    cap = CONFIG.decision.max_net_edge
    assert cap > 0, "the guard must ship enabled"
    # 'home' claims a ~+0.55 edge (thin-prior blow-up); 'away' a believable one.
    model = {"home": 0.95, "draw": 0.03, "away": 0.02}
    quotes = {"home": SideQuote(ask=0.35, devig=0.35, venue="kalshi"),
              "draw": SideQuote(ask=0.30, devig=0.30, venue="kalshi"),
              "away": SideQuote(ask=0.30, devig=0.30, venue="kalshi")}
    out = decide(model, quotes, gate_open=True)
    assert out.side != "home"
    assert out.net_edge is None or out.net_edge <= cap


def test_a_believable_edge_still_trades():
    """Positive control: the guard is a ceiling, not an off switch."""
    model = {"home": 0.55, "draw": 0.25, "away": 0.20}
    quotes = {"home": SideQuote(ask=0.45, devig=0.45, venue="kalshi"),
              "draw": SideQuote(ask=0.30, devig=0.30, venue="kalshi"),
              "away": SideQuote(ask=0.30, devig=0.30, venue="kalshi")}
    out = decide(model, quotes, gate_open=True)
    assert out.side == "home" and out.tradable and out.stake_usd > 0


def test_guard_can_leave_no_bet_at_all():
    """When the ONLY side clearing its threshold is the absurd one, the answer is no
    bet — the runner-up must not be promoted just to have something to trade."""
    from dataclasses import replace

    from prediction_market_soccer.config import CONFIG
    model = {"home": 0.95, "draw": 0.03, "away": 0.02}
    quotes = {"home": SideQuote(ask=0.35, devig=0.35, venue="kalshi"),
              "draw": SideQuote(ask=0.05, devig=0.05, venue="kalshi"),
              "away": SideQuote(ask=0.05, devig=0.05, venue="kalshi")}
    out = decide(model, quotes, gate_open=True)
    assert out.side is None and out.stake_usd == 0.0
    # Disabling the guard (0) restores the old, unguarded behaviour — proving the
    # refusal above came from the ceiling and not from some other threshold.
    unguarded = decide(model, quotes, gate_open=True,
                       cfg=replace(CONFIG.decision, max_net_edge=0.0))
    assert unguarded.side == "home"
