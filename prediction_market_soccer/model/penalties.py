"""Penalty-shootout model (plan 03 §4b, 15) — team-specific, NOT a flat average.

Research on European club-cup shootouts shows they are far from coin tosses:
  * **team quality** dominates — top-market-value quartile teams win ~60%;
  * **kick order** matters — the team kicking first wins ~59% (ABAB), but the
    order is a coin toss, so it widens the distribution without shifting the
    expectation.

So instead of a flat ``penalty_favorite_edge`` we compute a per-pair shootout
win probability from the rating gap. The kick-order coin-flip is modelled as
added variance (it averages out in the binary "advance" expectation but matters
for path/scenario risk).
"""
from __future__ import annotations

import math

from prediction_market_soccer.model.strength import StrengthModel

# Reputation overlay, additive to the favourite's shootout edge. EMPTY on purpose.
#
# The World Cup module carried ten national-team entries (Germany +0.05, Argentina +0.05,
# England -0.02 …). Those keys are canonical NATION ids; against club ids they can never
# match, so leaving them in place would be a dictionary that looks like a working prior and
# is in fact permanently inert — the failure mode that hides for a season. Worse, two of
# them ("argentina", "brazil") would start matching the moment someone canonicalised a club
# to a country-shaped id, and silently tilt a real ticket on a national-team fact.
#
# There is no club replacement to put here yet, and we will not invent one. soccer.db holds
# 80 club shootouts (CONMEBOL cups, Argentine playoffs, UEFA qualifiers) but stores only the
# final tally per side, not the kick-by-kick sequence, so neither a per-club reputation nor
# a conversion rate can be estimated from it. Empty means the model runs purely off the
# rating gap, which is the mechanism the research actually supports; a club overlay is a
# non-blocking upgrade once a kick-level shootout dataset exists.
SHOOTOUT_REPUTATION: dict[str, float] = {}

# How strongly the rating gap maps to a shootout edge (gentler than open play —
# shootouts compress quality, but quality still tilts it, research-backed).
_SO_BETA = 0.45
_SO_MIN, _SO_MAX = 0.35, 0.65          # clamp: even a mismatch is ~65/35, not 90/10
KICK_ORDER_FIRST_EDGE = 0.095          # ~59.5% first-kicker advantage (variance)


def shootout_win_prob(sm: StrengthModel, team_a: str, team_b: str) -> float:
    """P(team_a wins a shootout vs team_b): quality gap (+ reputation if ever filled),
    clamped. With SHOOTOUT_REPUTATION empty this is the pure rating-gap model."""
    ra, rb = sm.ratings.get(team_a, 0.0), sm.ratings.get(team_b, 0.0)
    quality = 0.5 + 0.5 * math.tanh(_SO_BETA * (ra - rb))
    rep = SHOOTOUT_REPUTATION.get(team_a, 0.0) - SHOOTOUT_REPUTATION.get(team_b, 0.0)
    return max(_SO_MIN, min(_SO_MAX, quality + rep))


# ── Finer (sequential) shootout model — plan 24 §2.2 ─────────────────────────
# Instead of a single edge, model the actual shootout as per-kick conversions over a
# best-of-5 (then sudden death). Used by the LIVE advance model (model/inplay_advance.py)
# and optionally by the pre-match knockout_advance_prob for a consistent口径.
# 0.75 is the long-run shootout conversion rate from the published club-cup studies, not a
# figure fitted here: soccer.db's 80 shootouts carry only the final tally, and the 66
# IN-PLAY penalty events it does have (57 scored / 9 missed) measure a different act — an
# in-play penalty against a set defence, not the eighth kick of a shootout — on a sample too
# thin to move a constant anyway. Refit when kick-level shootout data lands.
SHOOTOUT_BASE_CONV = 0.75              # club-cup shootout conversion (~75% historically)
_SO_CONV_SPREAD = 0.10                 # how far the rating gap pulls each team's conversion
_SO_CONV_MIN, _SO_CONV_MAX = 0.55, 0.92


def shootout_conversions(sm: StrengthModel, team_a: str, team_b: str) -> tuple[float, float]:
    """(c_a, c_b) per-kick conversion rates from quality gap + reputation, clamped.

    Mirrors shootout_win_prob's signal (same _SO_BETA tanh + reputation overlay) but maps it
    to a PAIR of conversion rates around SHOOTOUT_BASE_CONV, so the sequential DP reproduces
    a quality-consistent win probability."""
    ra, rb = sm.ratings.get(team_a, 0.0), sm.ratings.get(team_b, 0.0)
    shift = _SO_CONV_SPREAD * math.tanh(_SO_BETA * (ra - rb))
    rep_a = SHOOTOUT_REPUTATION.get(team_a, 0.0)
    rep_b = SHOOTOUT_REPUTATION.get(team_b, 0.0)
    c_a = max(_SO_CONV_MIN, min(_SO_CONV_MAX, SHOOTOUT_BASE_CONV + shift + rep_a))
    c_b = max(_SO_CONV_MIN, min(_SO_CONV_MAX, SHOOTOUT_BASE_CONV - shift + rep_b))
    return c_a, c_b


def _sudden_death_win(c_a: float, c_b: float) -> float:
    """P(A wins sudden death): each round A wins iff (A scores, B misses), B wins iff
    (B scores, A misses), else repeat → geometric closed form."""
    a_win = c_a * (1.0 - c_b)
    b_win = (1.0 - c_a) * c_b
    denom = a_win + b_win
    return a_win / denom if denom > 0 else 0.5


def shootout_win_prob_dp(c_a: float, c_b: float, *, rounds: int = 5,
                         taken_a: int = 0, scored_a: int = 0,
                         taken_b: int = 0, scored_b: int = 0) -> float:
    """Exact P(A wins) for a best-of-`rounds` shootout (then sudden death), from per-kick
    conversions. Supports LIVE conditioning: pass kicks already taken/scored by each side
    (defaults 0 → pre-shootout). Early "clinch" stops don't change the winner distribution,
    so the remaining scheduled kicks are modelled as independent binomials and combined with
    the current tally; an exact tie after the regulation rounds goes to sudden death."""
    import numpy as np
    from scipy.stats import binom
    rem_a = max(0, rounds - taken_a)
    rem_b = max(0, rounds - taken_b)
    pa = binom.pmf(np.arange(rem_a + 1), rem_a, c_a) if rem_a > 0 else np.array([1.0])
    pb = binom.pmf(np.arange(rem_b + 1), rem_b, c_b) if rem_b > 0 else np.array([1.0])
    m = np.outer(pa, pb)
    fa = scored_a + np.arange(rem_a + 1)[:, None]   # final A goals after regulation rounds
    fb = scored_b + np.arange(rem_b + 1)[None, :]
    p_a_more = float(m[fa > fb].sum())
    p_level = float(m[fa == fb].sum())
    return p_a_more + p_level * _sudden_death_win(c_a, c_b)


def shootout_win_prob_detailed(sm: StrengthModel, team_a: str, team_b: str,
                               *, taken_a: int = 0, scored_a: int = 0,
                               taken_b: int = 0, scored_b: int = 0) -> float:
    """P(team_a wins the shootout) via the sequential DP from per-kick conversions.
    Optional live tally conditions on kicks already taken."""
    c_a, c_b = shootout_conversions(sm, team_a, team_b)
    return shootout_win_prob_dp(c_a, c_b, taken_a=taken_a, scored_a=scored_a,
                                taken_b=taken_b, scored_b=scored_b)


def kick_order_band(p_shootout: float) -> tuple[float, float]:
    """The (first-kicker, second-kicker) shootout probabilities for ``p_shootout``.

    The coin toss decides which applies. Used for scenario/variance analysis
    (e.g. a favourite who loses the toss is meaningfully more vulnerable).
    """
    first = min(0.95, p_shootout + KICK_ORDER_FIRST_EDGE / 2)
    second = max(0.05, p_shootout - KICK_ORDER_FIRST_EDGE / 2)
    return first, second


if __name__ == "__main__":
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    sm = build_strength(load_prior())
    # Pair the strongest clubs against a mid-table and a bottom club so the clamp and the
    # rating-gap slope are both visible — picked FROM the prior, never a hardcoded list, so
    # this smoke test keeps working as the club field changes.
    ranked = sorted(sm.ratings, key=lambda t: -sm.ratings[t])
    if len(ranked) < 6:
        raise SystemExit("club prior too small for a shootout smoke test")
    mid = len(ranked) // 2
    pairs = [(ranked[0], ranked[1]), (ranked[0], ranked[mid]), (ranked[0], ranked[-1]),
             (ranked[mid], ranked[mid + 1]), (ranked[-1], ranked[0])]
    print("shootout win prob (club A vs B): rating gap only — SHOOTOUT_REPUTATION is empty")
    for a, b in pairs:
        p = shootout_win_prob(sm, a, b)
        first, second = kick_order_band(p)
        print(f"  {a:<10} vs {b:<12} P(A)={p:.3f}  (kick-order: {first:.2f} if A kicks first / {second:.2f} if second)")
