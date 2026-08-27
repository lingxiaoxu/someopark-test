"""knockout_late_draw.py — how much a level scoreline is worth late, per match FORMAT.

The World Cup module carried a single tactic with a single premise: level knockout at 75'
→ BUY the 90' draw, because "both teams settle for extra time". It had no league branch at
all, and the branch it did have was selected by a round-name guess (`_is_knockout(round)`),
the exact C1 bug class TRANSFORM_PLAN §3.0 exists to kill. Club football needs both
branches, and the selector must be the fixture's `caps`, never its round string.

WHAT THE CLUB DATA ACTUALLY SAYS (fitted here, not assumed)
-----------------------------------------------------------
For every finished fixture in soccer.db that was LEVEL AT HALF-TIME we compared the
realized P(match ends level) against a per-match independent-Poisson (Skellam) baseline.
The baseline is built from a multiplicative attack/defence fit inside each (competition,
season) — that control matters, because knockout ties pair wildly mismatched clubs and
mismatch alone depresses the draw rate; without it you would read schedule composition as
a tactical effect. `persistence` below is empirical / baseline: >1 = the level state is
STICKIER than chance (teams settle → back the draw), <1 = it breaks open MORE than chance
(teams push → fade the draw).

    family              n     empirical   baseline   ratio    z
    domestic_league   1511      0.3971     0.3813    1.041   +1.26
    uefa_league_phase  157      0.3185     0.3584    0.889   -1.07
    cup_leg1           269      0.3420     0.3912    0.874   -1.70
    cup_decider        185      0.2541     0.3687    0.689   -3.58

Two conclusions, both against the inherited World Cup intuition:

  * The knockout sign is BACKWARDS for clubs. A club knockout decider that is level
    resolves far MORE often than chance (z=-3.58) — it does not drift to extra time. The
    WC "back the level knockout" premise is not a club premise; the tactic must FADE the
    draw there, not buy it.
  * The league branch is NOT the mirror image. A level domestic-league match tracks the
    Poisson baseline almost exactly (ratio 1.041, z=+1.26, not significant). There is no
    tactical distortion to trade in either direction — the correct league stance is
    "stand aside", and saying so is the deliverable. The standings-dependent case the
    plan describes (a point suits both sides) is real but is a MOTIVATION feature; it
    lives in motivation.py at weight 0 and cannot be read off the scoreline alone.

MEASUREMENT CAVEAT (read before raising any weight). The fit conditions on the HALF-TIME
state, because that is the only level-state marker stored for all 5,085 finished fixtures.
The tactic fires at ~75'. The direct 75' sample — the 191 fixtures with minute-level goal
events — is n=48 split across formats and points the other way (cup 0.60 vs league 0.43),
which at that n is noise, not a contradiction. So `LATE_STATE_VERIFIED` is False and every
consumer should treat the fade as advisory until minute-level events accumulate.

Shrinkage: each family's persistence is pulled toward 1.0 (no effect) by its own
significance, z²/(z²+z0²). A family that has not earned its estimate therefore returns
~1.0 automatically instead of needing a hand-placed "off" switch — and tightens on its own
as fixtures accumulate. Only `cup_decider` currently clears the band.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:                          # avoid importing the registry at module load
    from prediction_market_soccer.config.leagues import StageCaps

LEAGUE = "domestic_league"
UEFA_LEAGUE_PHASE = "uefa_league_phase"
CUP_LEG1 = "cup_leg1"
CUP_DECIDER = "cup_decider"

# The European league phase is a league round by caps, but its own fit sits below the
# domestic one, so it is kept as a separate family rather than folded in.
_UEFA_LEAGUE_PHASE_COMPS = frozenset({"ucl", "uel", "uecl"})

# family -> (n, empirical/baseline ratio, z). Regenerate with fit_late_draw_persistence().
# Refitted 2026-08-27 after removing a survivorship bias that produced the original
# table. The first fit read only status_short='FT'; a knockout that is LEVEL at 90'
# goes to extra time and settles AET/PEN, so the query dropped precisely the outcome
# it was measuring — and dropped it only in the knockout families (of 137 AET/PEN
# fixtures exactly one is a league game). Including them, and reading the REGULATION
# score (fixture.home_goals carries extra-time goals on an AET row), the headline
# result evaporates: cup_decider went from n=185 / ratio 0.689 / z=-3.58 to
# n=249 / 0.939 / z=-0.77. The league family is bit-identical, which is what a real
# bias looks like. NO family is now significant, so every stance shrinks to neutral —
# the honest answer is that this tactic has no edge on club data either way.
_FIT: dict[str, tuple[int, float, float]] = {
    LEAGUE:            (1511, 1.039, +1.19),
    UEFA_LEAGUE_PHASE: (157,  0.886, -1.10),
    CUP_LEG1:          (277,  0.904, -1.33),
    CUP_DECIDER:       (249,  0.939, -0.77),
}
_SHRINK_Z0 = 2.0        # a family needs |z| ≈ 2 before half its raw effect survives
# A stance must clear this much of a mispricing to be worth a live ticket: below it the
# fair move is inside the spread the corner/draw books quote anyway.
STANCE_BAND = 0.08
LATE_MINUTE = 75        # kept in sync with strategy/inplay_tactics.LATE_MINUTE (that
                        # module owns the canonical value; this is the model-side default)
# False until the fit can be redone conditioning on the ~75' state rather than half-time.
LATE_STATE_VERIFIED = False


def _shrunk(family: str) -> float:
    n, ratio, z = _FIT.get(family, (0, 1.0, 0.0))
    if n <= 0:
        return 1.0
    w = (z * z) / (z * z + _SHRINK_Z0 * _SHRINK_Z0)
    return 1.0 + (ratio - 1.0) * w


PERSISTENCE: dict[str, float] = {f: round(_shrunk(f), 4) for f in _FIT}


@dataclass(frozen=True)
class LateDrawStance:
    family: str
    persistence: float       # multiplier on the model's P(90' draw); 1.0 = no view
    direction: str           # "back" | "fade" | "neutral"
    n: int                   # fitted sample behind the family
    z: float                 # significance of the fitted deviation from the baseline
    verified_late: bool      # False → fitted at the half-time state, not at `late_minute`
    reason: str


def family_for(caps: "StageCaps | None", comp_key: str | None = None) -> str:
    """Match format from the fixture's caps — the §3.0 contract, no round-name guessing.

    `caps.advance` is the single discriminator between a league round and a knockout: a
    league round has no advance market by construction, in any of the 12 competitions.
    Within a tie, leg 1 cannot go to extra time and leg 2 can, so they are different
    animals and `caps.leg` (resolved from the tie pairing, never from the round string)
    separates them. A two-leg fixture whose leg is unresolved is treated as leg 1, the
    weaker-signal side — an unresolved leg must never buy its way into the stronger stance.
    """
    if caps is None or not getattr(caps, "advance", False):
        return UEFA_LEAGUE_PHASE if (comp_key or "") in _UEFA_LEAGUE_PHASE_COMPS else LEAGUE
    if getattr(caps, "two_leg", False):
        return CUP_DECIDER if getattr(caps, "leg", None) == 2 else CUP_LEG1
    return CUP_DECIDER          # single-match knockout: this fixture decides the tie


def draw_persistence(caps: "StageCaps | None", comp_key: str | None = None) -> float:
    """Multiplier to apply to a model P(90' draw) once the match is level and late."""
    return PERSISTENCE.get(family_for(caps, comp_key), 1.0)


def adjusted_draw_prob(p_draw: float, caps: "StageCaps | None",
                       comp_key: str | None = None) -> float:
    """`p_draw` corrected for the format's fitted draw persistence, clamped to (0, 1).

    The other two legs are NOT renormalised here: the caller owns the 3-way book and knows
    whether it is repricing one leg against a quote or rebuilding the whole triple."""
    p = max(0.0, min(1.0, float(p_draw))) * draw_persistence(caps, comp_key)
    return max(1e-6, min(1.0 - 1e-6, p))


def late_draw_stance(caps: "StageCaps | None", *, minute: int, home_goals: int,
                     away_goals: int, comp_key: str | None = None,
                     late_minute: int = LATE_MINUTE) -> LateDrawStance:
    """The format-aware replacement for the WC `knockout_late_draw(lp, knockout=...)`.

    Returns a neutral stance unless the match is actually level and late AND the format's
    fitted persistence clears STANCE_BAND. Every family whose fit is not significant lands
    on "neutral" through the shrinkage, so a stand-aside is produced by the evidence rather
    than by a special case."""
    fam = family_for(caps, comp_key)
    n, _, z = _FIT.get(fam, (0, 1.0, 0.0))
    p = PERSISTENCE.get(fam, 1.0)
    if home_goals != away_goals:
        return LateDrawStance(fam, p, "neutral", n, z, LATE_STATE_VERIFIED, "not level")
    if minute < late_minute:
        return LateDrawStance(fam, p, "neutral", n, z, LATE_STATE_VERIFIED, "not late yet")
    if p <= 1.0 - STANCE_BAND:
        return LateDrawStance(
            fam, p, "fade", n, z, LATE_STATE_VERIFIED,
            f"level {fam} at {minute}' — level states in this format resolve "
            f"{1.0 - p:.0%} more often than the Poisson baseline (n={n}, z={z:+.2f})")
    if p >= 1.0 + STANCE_BAND:
        return LateDrawStance(
            fam, p, "back", n, z, LATE_STATE_VERIFIED,
            f"level {fam} at {minute}' — level states in this format stick "
            f"{p - 1.0:.0%} beyond the Poisson baseline (n={n}, z={z:+.2f})")
    return LateDrawStance(fam, p, "neutral", n, z, LATE_STATE_VERIFIED,
                          f"level {fam} at {minute}' tracks the Poisson baseline "
                          f"(persistence {p:.3f}, z={z:+.2f}) — no edge, stand aside")


# ── offline refit ────────────────────────────────────────────────────────────
def fit_late_draw_persistence(conn) -> dict:
    """Regenerate `_FIT` from soccer.db. See the module docstring for the design.

    Baseline: inside each (competition, season) fit multiplicative attack/defence factors
    and a symmetric home factor from that group's own finished matches, shrunk toward the
    group mean, then evaluate P(level) per match with a Skellam at those rates scaled by
    the family's observed second-half share of goals. Empirical: the realized share of
    level-at-half-time matches that finished level."""
    import collections
    import json
    import math

    from prediction_market_soccer.config import leagues as _lg

    # Historical round names that predate the current registry rules (the 2024-25 UEFA
    # "Round of 32", CONMEBOL "1st/2nd/3rd Round") are knockout ties; classifying them as
    # UNKNOWN would silently drop a third of the cup sample.
    legacy_ko = ("Round of 32", "1st Round", "2nd Round", "3rd Round")

    def stage(lid, rnd):
        comp = _lg.by_api_id(lid)
        if comp is None:
            return None, None
        st = _lg.stage_of(comp.key, rnd)
        if st == _lg.Stage.UNKNOWN:
            return (comp.key, _lg.Stage.CUP_TWO_LEG) if rnd in legacy_ko else (None, None)
        return comp.key, st

    from prediction_market_soccer.util.pricing import reg_score
    rows = conn.execute(
        "SELECT api_id, league_id, season, round, kickoff_ts, home_api_id, away_api_id, "
        "       home_goals, away_goals, raw_json FROM fixture "
        "WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts").fetchall()
    recs, ties = [], collections.defaultdict(list)
    for r in rows:
        key, st = stage(r["league_id"], r["round"])
        if key is None:
            continue
        ht = {}
        if r["raw_json"]:
            try:
                ht = (json.loads(r["raw_json"]).get("score") or {}).get("halftime") or {}
            except Exception:
                ht = {}
        # SURVIVORSHIP: a knockout that is LEVEL at 90' goes to extra time, so its status
        # becomes AET/PEN and never FT — restricting the fit to FT dropped exactly the
        # outcome being measured, and dropped it only in the knockout families (137 AET/PEN
        # fixtures, 1 of them a league game). That bias alone produced the "club knockouts
        # resolve level states 31% more often" result. Include them, and read the
        # REGULATION score: fixture.home_goals carries extra-time goals on an AET row.
        gh90, ga90 = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
        r = dict(r)
        r["home_goals"], r["away_goals"] = gh90, ga90
        recs.append((r, key, st, ht))
        if st == _lg.Stage.CUP_TWO_LEG:
            ties[(key, r["season"], r["round"],
                  tuple(sorted((r["home_api_id"], r["away_api_id"]))))].append(r["api_id"])
    leg = {fid: i + 1 for ids in ties.values() for i, fid in enumerate(ids[:2])}

    def famof(key, st, fid):
        if st == _lg.Stage.LEAGUE:
            return UEFA_LEAGUE_PHASE if key in _UEFA_LEAGUE_PHASE_COMPS else LEAGUE
        if st == _lg.Stage.CUP_SINGLE:
            return CUP_DECIDER
        return CUP_DECIDER if leg.get(fid) == 2 else CUP_LEG1

    groups = collections.defaultdict(list)
    for r, key, st, ht in recs:
        groups[(key, r["season"])].append(r)
    att, dfn, mu_g, hf_g = {}, {}, {}, {}
    PRIOR = 4.0                      # matches of pull toward the competition mean
    for g, fs in groups.items():
        if len(fs) < 40:             # too few to identify per-club factors
            continue
        m = sum((f["home_goals"] or 0) + (f["away_goals"] or 0) for f in fs) / (2 * len(fs))
        hg = sum(f["home_goals"] or 0 for f in fs) / len(fs)
        ag = sum(f["away_goals"] or 0 for f in fs) / len(fs)
        hf = math.sqrt(max(hg, 1e-6) / max(ag, 1e-6))
        sc = collections.defaultdict(lambda: [0, 0, 0])
        for f in fs:
            for t, gf, ga in ((f["home_api_id"], f["home_goals"], f["away_goals"]),
                              (f["away_api_id"], f["away_goals"], f["home_goals"])):
                s = sc[t]
                s[0] += gf or 0
                s[1] += ga or 0
                s[2] += 1
        for t, (gf, ga, n) in sc.items():
            att[(g, t)] = ((gf + PRIOR * m) / (n + PRIOR)) / m
            dfn[(g, t)] = ((ga + PRIOR * m) / (n + PRIOR)) / m
        mu_g[g], hf_g[g] = m, hf

    share = collections.defaultdict(lambda: [0.0, 0.0])
    for r, key, st, ht in recs:
        if ht.get("home") is None:
            continue
        f = famof(key, st, r["api_id"])
        share[f][0] += (r["home_goals"] - ht["home"]) + (r["away_goals"] - ht["away"])
        share[f][1] += (r["home_goals"] or 0) + (r["away_goals"] or 0)

    def skellam_zero(mh, ma):
        return sum(math.exp(-mh) * mh ** k / math.factorial(k) *
                   math.exp(-ma) * ma ** k / math.factorial(k) for k in range(30))

    out = collections.defaultdict(lambda: {"n": 0, "emp": 0, "base": 0.0})
    for r, key, st, ht in recs:
        if ht.get("home") is None or ht["home"] != ht["away"]:
            continue
        g = (key, r["season"])
        if g not in mu_g:
            continue
        ah, dh = att.get((g, r["home_api_id"])), dfn.get((g, r["home_api_id"]))
        aa, da = att.get((g, r["away_api_id"])), dfn.get((g, r["away_api_id"]))
        if None in (ah, dh, aa, da):
            continue
        f = famof(key, st, r["api_id"])
        sh = share[f][0] / share[f][1] if share[f][1] else 0.55
        d = out[f]
        d["n"] += 1
        d["emp"] += (r["home_goals"] == r["away_goals"])
        d["base"] += skellam_zero(mu_g[g] * ah * da * hf_g[g] * sh,
                                  mu_g[g] * aa * dh / hf_g[g] * sh)
    fit = {}
    for f, d in out.items():
        n = d["n"]
        if not n:
            continue
        emp, base = d["emp"] / n, d["base"] / n
        se = math.sqrt(max(emp * (1 - emp), 1e-9) / n)
        fit[f] = (n, round(emp / base, 3), round((emp - base) / se, 2))
    return fit


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store

    fit = fit_late_draw_persistence(store.init_db())
    print(f"{'family':20s} {'n':>5s} {'ratio':>7s} {'z':>7s} {'shrunk':>8s} {'stance':>8s}")
    for f, (n, ratio, z) in sorted(fit.items(), key=lambda kv: -kv[1][0]):
        w = (z * z) / (z * z + _SHRINK_Z0 ** 2)
        p = 1.0 + (ratio - 1.0) * w
        st = "fade" if p <= 1 - STANCE_BAND else ("back" if p >= 1 + STANCE_BAND else "neutral")
        print(f"{f:20s} {n:5d} {ratio:7.3f} {z:+7.2f} {p:8.4f} {st:>8s}")
