"""model/inplay_constants.py — one home for the in-play tactical constants.

These were inherited from the World Cup module, where they were calibrated on 26
matches, and then COPIED into four files. A copy is not a constant: `OVERSHOOT_MARGIN`
was living as 0.12 in `inplay_tactics`, 0.12 in `inplay_tactics_advance`, 0.08 in
`smart_exit` and 0.08 in `smart_exit_advance`, so "the" over-reaction threshold
depended on which module happened to ask. Editing one and believing the system had
changed is the failure this module exists to prevent.

Values below are the CLUB re-derivations. Each carries the measurement that set it;
where the club data contradicts the World Cup premise the constant is disabled rather
than re-tuned, because a signal with a reversed sign is worse than no signal.
"""
from __future__ import annotations

# ── over-reaction take-profit ────────────────────────────────────────────────
# The market must sit this far above the live model fair before "the market has
# over-reacted" is worth acting on. WC used 0.12 (and 0.08 in the exit modules).
# Club measurement: over 136 fair-vs-market observations the two-sided |divergence|
# has median 0.22, so 0.12 fires on the ordinary noise of a club book — 45 firings in
# 35 of 117 observations (30%). At 0.22 the trigger sits at the median divergence,
# i.e. it fires on the half of moves that are genuinely larger than usual.
OVERSHOOT_MARGIN = 0.22
# ...but an ABSOLUTE margin cannot be reached once the fair value is high: at a fair of
# 0.79 it demands 1.01, which no contract can print, so the tactic silently switches
# itself off exactly where a take-profit matters most. The trigger is therefore the
# SMALLER of the absolute margin and a share of the room left to the 1.00 ceiling —
# 0.22 while there is space, tightening automatically as fair approaches certainty.
OVERSHOOT_HEADROOM_FRAC = 0.45


def overshoot_trigger(fair: float) -> float:
    """Price above ``fair`` (both 0-1) that counts as an over-reaction."""
    headroom = max(0.0, 1.0 - fair)
    return min(OVERSHOOT_MARGIN, OVERSHOOT_HEADROOM_FRAC * headroom)

# ── finishing uplift ─────────────────────────────────────────────────────────
# "A team out-finishing its xG keeps out-finishing it." The WC mined +0.87 goals−xG
# on 26 matches. The club distribution is centred on ZERO and slightly negative:
# n=115, mean −0.118 (se 0.128), median −0.140, 46% positive — 7.7 sigma away from the
# WC value and statistically indistinguishable from no effect. Held at 0.4 it was the
# sole reason 5 of 13 live signals fired. Disabled: finishing over-performance does
# not persist in club football on the evidence we have.
FINISHING_UPLIFT = 0.0

# ── formation fragility ──────────────────────────────────────────────────────
# The WC premise was that back-three shapes concede more. The club data reverses it:
# 3-4-2-1 concedes 1.095 per match (n=21) and 5-3-2 concedes 0.875 (n=8) against a
# 4-2-3-1 reference of 1.282 (n=156) — the two "fragile" shapes are the two SOLID
# ones. Note the reference itself reproduces the WC's 1.27 almost exactly, so the
# baseline ported fine and it is specifically the fragile SET that is wrong. Empty
# until a shape earns membership on ≥20 team-matches of club evidence.
FRAGILE_FORMATIONS: frozenset[str] = frozenset()

# ── late-match window ────────────────────────────────────────────────────────
# Two tactics borrow this one number for unrelated purposes: the draw take-profit
# ("the draw is nearly settled") and formation fragility ("stop leaning on shape once
# the game is decided"). They are separated here so re-deriving one cannot silently
# move the other — a live check showed formation_fragility flipping BUY→HOLD between
# the 74th and 76th minute purely because the draw tactic's constant sat at 75.
LATE_MINUTE = 75              # draw lifecycle: level + this late ⇒ take-profit branch
FRAGILITY_MAX_MINUTE = 75     # shape leans stand aside past this
EARLY_MINUTE = 35             # draw lifecycle: the time-value entry window

# The fair draw probability at which the level-late draw is "near max payout". This is
# a TAKE-PROFIT trigger on a held draw, not a prediction of the draw rate, so the
# empirical 64% level-at-75' figure is not the right comparison for it.
DRAW_LOCK_FAIR = 0.74

# ── corner totals ────────────────────────────────────────────────────────────
# Edge required before the corner model trades against a quote. WC 0.07. Every one of
# the 13 club firings on record sat between 0.162 and 0.463 with a median of 0.301,
# all on one fixture and all on the same side — a distribution that says the model and
# the book disagree structurally, not that 13 opportunities appeared. Raised, and
# capped: a gap wider than CORNER_EDGE_ALARM is treated as a data fault, not an edge.
MIN_CORNER_EDGE = 0.15
CORNER_EDGE_ALARM = 0.25

# ── live odds cross-validation ───────────────────────────────────────────────
# How far the bookmaker must move away from the model before it counts as information.
# WC 0.06 / 0.10. Club books disagree with the model by a median of 0.23 in the very
# branch that is supposed to detect disagreement, so the WC thresholds fire on the
# baseline state (16% of observations across just two fixtures).
CROSSVAL_VENUE_MOVE = 0.12
CROSSVAL_LEAD_MOVE = 0.20

# ── dormant explosion ────────────────────────────────────────────────────────
# "Quiet but loaded": expected remaining goals must clear this for the 'loaded' half
# of the claim to have been tested at all. WC 0.8 is below the typical live value, so
# the gate passed on almost every quiet game.
DORMANT_REMAINING_GOALS = 1.2


# ── xG dominance ─────────────────────────────────────────────────────────────
# `momentum_value` and `xg_dominance_chase` are meant to be a pair: a broad "this
# side is under-priced" lean and a narrower, higher-conviction version of the same
# read. They shipped with the SAME bar (1.0) and the same 80' cutoff, so they were
# one signal emitted twice — in replay they fired on an identical set of fixtures.
# The club distribution of |live xG lead| over 119 fixtures is p50 0.90, p75 1.48,
# p90 2.11: a 1.0 bar sits at the 55th percentile, i.e. it flags the average match.
# Split them at real percentiles — the broad lean at p75, the conviction call at p90.
MOMENTUM_XG_EDGE = 1.5        # ≈ p75 of the club |xG lead| distribution
XG_CHASE_EDGE = 2.1           # ≈ p90 — the "deserved a goal and did not get it" tail
XG_CHASE_MAX_MIN = 80         # still time for the deserved goal to arrive
