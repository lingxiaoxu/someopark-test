"""Tests for the in-play confidence tier + staking gate (plan 20-22 rules).

The gate is the *betting threshold*: it never drops a signal, it only flips
`actionable`. These lock in the rescue conditions (draw>70'+level, over≤15'/1-goal,
underdog model-aligned, large-edge needs lead+top10) and the lock_arb=always-stake.
"""
from prediction_market.strategy import inplay_confidence as ic

# Argentina rank 1 (top-10), Algeria rank 28 (weak) — real prior values.
HID, AID, HN, AN = "argentina", "algeria", "Argentina", "Algeria"
_MODEL_HOME = {"home": 0.90, "draw": 0.08, "away": 0.02}   # live model favours home


def _ctx(gh, ga, minute, model=_MODEL_HOME):
    return ic.match_context(HID, AID, HN, AN, gh, ga, minute, model=model)


def test_lock_arb_always_actionable():
    g = ic.gate({"kind": "lock_arb", "side": "home", "edge": 0.1}, _ctx(0, 0, 30))
    assert g["confidence"] == "high" and g["actionable"] is True and g["stake_frac"] == 1.0


def test_top10_leading_small_edge_stakes():
    o = {"kind": "relative_value", "side": "home", "edge": 0.06}
    ic.annotate(o, _ctx(1, 0, 60), cap_usd=1.0)
    assert o["confidence"] == "high" and o["actionable"] is True and o["stake_usd"] > 0


def test_top10_leading_allows_large_edge():
    # Large edge is normally gated out — but lead AND top-10 is the rescue combo.
    g = ic.gate({"kind": "relative_value", "side": "home", "edge": 0.18}, _ctx(1, 0, 60))
    assert g["actionable"] is True


def test_non_top10_large_edge_blocked():
    # Mexico (rank 14) leading with a big edge → kept but NOT staked.
    ctx = ic.match_context("mexico", "south-africa", "Mexico", "South Africa", 1, 0, 60,
                           model={"home": 0.9, "draw": 0.08, "away": 0.02})
    g = ic.gate({"kind": "relative_value", "side": "home", "edge": 0.18}, ctx)
    assert g["actionable"] is False and "large edge" in g["gate_reason"]


def test_underdog_not_model_aligned_blocked():
    # Weak away, model favours home, away not leading → advisory only.
    g = ic.gate({"kind": "relative_value", "side": "away", "edge": 0.05}, _ctx(1, 0, 60))
    assert g["actionable"] is False


def test_draw_needs_late_and_level():
    early = ic.gate({"kind": "tactic", "side": "draw", "edge": None}, _ctx(1, 0, 60))
    assert early["actionable"] is False and "draw needs" in early["gate_reason"]
    late = ic.gate({"kind": "tactic", "side": "draw", "edge": None}, _ctx(0, 0, 80))
    assert late["actionable"] is True


def test_over_needs_early_or_one_goal():
    mid_blank = ic.gate({"kind": "tactic", "side": "over", "edge": None}, _ctx(0, 0, 60))
    assert mid_blank["actionable"] is False and "over needs" in mid_blank["gate_reason"]
    early = ic.gate({"kind": "tactic", "side": "over", "edge": None}, _ctx(0, 0, 10))
    assert early["actionable"] is True
    one_goal = ic.gate({"kind": "tactic", "side": "over", "edge": None}, _ctx(1, 0, 60))
    assert one_goal["actionable"] is True


def test_knockout_draw_softer_and_late_tie_strong():
    # KO: per-match market is still 90' 3-way, but draws are more common (→ ET).
    grp = ic.gate({"kind": "tactic", "side": "draw", "edge": None}, _ctx(0, 0, 80))
    ko = ic.match_context(HID, AID, HN, AN, 0, 0, 80, model=_MODEL_HOME, knockout=True)
    g_ko = ic.gate({"kind": "tactic", "side": "draw", "edge": None}, ko)
    # late+level draw is actionable in both, but KO scores it at least as high.
    assert grp["actionable"] is True and g_ko["actionable"] is True
    assert g_ko["confidence"] in ("high", "medium")


def test_knockout_totals_tilt_under_over():
    # KO lower-scoring → UNDER stronger, OVER weaker than the same group situation.
    ko = ic.match_context(HID, AID, HN, AN, 1, 0, 60, model=_MODEL_HOME, knockout=True)
    grp = _ctx(1, 0, 60)
    t_under_ko, _ = ic.assess({"kind": "tactic", "side": "under", "edge": None}, ko)
    t_under_grp, _ = ic.assess({"kind": "tactic", "side": "under", "edge": None}, grp)
    # under reaches a >= tier in KO (extra +1) vs group
    order = {"low": 0, "medium": 1, "high": 2}
    assert order[t_under_ko] >= order[t_under_grp]


def test_signal_is_never_dropped():
    # Even a blocked signal keeps its annotation fields (kept, just not staked).
    o = {"kind": "relative_value", "side": "draw", "edge": 0.2}
    ic.annotate(o, _ctx(1, 0, 60), cap_usd=1.0)
    assert o["actionable"] is False and o["stake_usd"] == 0.0
    assert "confidence" in o and "gate_reason" in o
