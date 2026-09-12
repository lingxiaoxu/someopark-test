"""UEFA 36-club league phase and seeded knockout paths, using the DC score kernel.

Rules: UEFA 2026/27 regulations Articles 17–22 and Annex B (UCL, UEL, UECL):
https://documents.uefa.com/r/Regulations-of-the-UEFA-Champions-League-2026/27/Annex-B-UEFA-Champions-League-Competition-System-Online
Unknown draws sample only legal adjacent-rank-pair slots. A seed's home-second-leg
privilege belongs to its bracket path, including after that seed is eliminated.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import numpy as np

from prediction_market_soccer.config.leagues import Stage, get, stage_of
from prediction_market_soccer.model.dixon_coles import score_matrix
from prediction_market_soccer.model.league_season import SeasonSim

_DONE = {"FT", "AET", "PEN", "AWD", "WO"}
_PENDING = {"NS", "TBD", "PST"}
_FAMILIES = ("champion", "top_n", "qual_direct", "qual_playoff", "ro16", "ro8", "ro4", "finalist")
# Top-to-bottom R16 slots in EACH half, exactly as UEFA Annex B.
_TOP = (5, 3, 7, 1)
_PO = ((11, 21), (13, 19), (9, 23), (15, 17))


@dataclass
class SwissResult:
    sim: SeasonSim
    ladder: dict
    coverage: dict
    states: dict
    reason: str | None
    notes: list[str]
    source_as_of: str | None


def bracket_slots(order: np.ndarray, bits: np.ndarray):
    """Return top-8, playoff-seeded and playoff-unseeded clubs in 8 fixed R16 slots.

    Twelve independent flips split each adjacent rank pair across opposite halves.
    This function also accepts all 4096 draw variants for conditioning known draws.
    """
    rows = np.arange(len(order))
    top = np.empty((len(order), 8), dtype=np.int16)
    seeded = np.empty_like(top); unseeded = np.empty_like(top)
    for s, (r, (a, b)) in enumerate(zip(_TOP, _PO)):
        for half in (0, 1):
            slot = half * 4 + s
            top[:, slot] = order[rows, r - 1 + (bits[:, s] ^ half)]
            seeded[:, slot] = order[rows, a - 1 + (bits[:, 4 + s] ^ half)]
            unseeded[:, slot] = order[rows, b - 1 + (bits[:, 8 + s] ^ half)]
    return top, seeded, unseeded


def _round_number(name: str) -> int | None:
    r = (name or "").lower()
    if r.strip() == "round of 32" or ("knockout" in r and "play" in r):
        return 0
    if "round of 16" in r:
        return 1
    if re.search(r"quarter.?final", r):
        return 2
    if re.search(r"semi.?final", r):
        return 3
    if r.strip() == "final":
        return 4
    return None


def _draw_bits(order, actual, rng):
    """Uniform legal draws, conditioned on already published round pairings."""
    if not actual:
        return rng.integers(0, 2, (len(order), 12), dtype=np.int16)
    # Published knockout matches require a completed, authoritative league order.
    variants = ((np.arange(4096)[:, None] >> np.arange(12)) & 1).astype(np.int16)
    tops, seeds, lows = bracket_slots(np.repeat(order[:1], 4096, axis=0), variants)
    valid = np.ones(4096, dtype=bool)
    for (rnd, a, b), fixtures in actual.items():
        entrants = (np.stack((seeds, lows), axis=2) if rnd == 0 else
                    np.stack((tops, seeds, lows), axis=2))
        slot_a = np.where((entrants == a).any(axis=2), np.arange(8), -1).max(axis=1)
        slot_b = np.where((entrants == b).any(axis=2), np.arange(8), -1).max(axis=1)
        if rnd <= 1:
            allowed = slot_a == slot_b
        else:
            divisor = 2 ** (rnd - 1)
            allowed = ((slot_a // divisor == slot_b // divisor)
                       & (slot_a // (divisor // 2) != slot_b // (divisor // 2)))
        valid &= (slot_a >= 0) & (slot_b >= 0) & allowed
    variants = variants[valid]
    if len(variants) == 0:
        raise ValueError("knockout_draw_conflicts_with_league_seeding")
    return variants[rng.integers(0, len(variants), len(order))]


class _Scores:
    """Vectorized DC draws, carrying each path's same club rating shock throughout."""
    def __init__(self, sm, clubs, eps, rng, sigma):
        self.sm, self.clubs, self.eps, self.rng = sm, clubs, eps, rng
        self.cdf = {}
        self.shifts = np.array([-1.2816, -.5244, 0, .5244, 1.2816]) * sigma * np.sqrt(2)
        self.edges = (self.shifts[1:] + self.shifts[:-1]) / 2

    def draw(self, home, away, rows, *, ko=False, neutral=False, et=False):
        n = len(rows); width = self.sm.cfg.score_matrix_kmax + 1
        bins = np.digitize(self.eps[rows, home] - self.eps[rows, away], self.edges)
        code = (home * len(self.clubs) + away) * 5 + bins
        result = np.empty(n, dtype=int)
        uniform = self.rng.random(n)
        sort = np.argsort(code)
        boundaries = np.r_[0, np.flatnonzero(np.diff(code[sort])) + 1, n]
        for lo, hi in zip(boundaries[:-1], boundaries[1:]):
            mask = sort[lo:hi]; pos = mask[0]
            h, a, b = int(home[pos]), int(away[pos]), int(bins[pos])
            cache_key = (h, a, b, ko, neutral, et)
            if cache_key not in self.cdf:
                lh, la = self.sm.pair_lambdas(self.clubs[h], self.clubs[a], knockout=ko,
                                            neutral=neutral, rating_shift=float(self.shifts[b]))
                if et:
                    lh *= self.sm.cfg.extra_time_fraction; la *= self.sm.cfg.extra_time_fraction
                self.cdf[cache_key] = np.cumsum(score_matrix(
                    lh, la, self.sm.cfg.dc_rho, self.sm.cfg.score_matrix_kmax).ravel())
                self.cdf[cache_key][-1] = 1.0
            result[mask] = np.searchsorted(self.cdf[cache_key], uniform[mask])
        return result // width, result % width


def simulate_swiss(conn, comp_key, sm, *, n_sims=20_000, seed=None) -> SwissResult:
    if n_sims <= 0:
        raise ValueError("n_sims must be positive")
    comp = get(comp_key)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    all_fx = [dict(r) for r in conn.execute(
        "SELECT * FROM fixture WHERE league_id=? AND season=? ORDER BY kickoff_ts,api_id",
        (comp.api_football_id, comp.season))]
    fixtures = [f for f in all_fx if stage_of(comp_key, f["round"]) == Stage.LEAGUE]
    clubs = sorted({cmap[t] for f in fixtures for t in (f["home_api_id"], f["away_api_id"]) if t in cmap})
    index = {c: i for i, c in enumerate(clubs)}
    games = 6 if comp_key == "uecl" else 8
    missing = sorted(set(clubs) - set(sm.ratings))
    unpriced = [f["api_id"] for f in fixtures if any(cmap.get(f[k]) not in sm.ratings
                 for k in ("home_api_id", "away_api_id"))]
    coverage = {"expected_teams": 36, "modeled_teams": len(set(clubs) & set(sm.ratings)),
                "fixtures_total": len(fixtures), "fixtures_expected": 36 * games // 2,
                "fixtures_priced": len(fixtures) - len(unpriced), "missing_club_ids": missing,
                "missing_fixture_ids": unpriced}
    sim = SeasonSim(comp_key, n_sims, clubs, {}, {}, {}, {}, {}, {}, [],
                    sum(f["status_short"] not in _DONE for f in fixtures))
    states = {f: "unavailable" for f in (*_FAMILIES, "relegation", "last")}
    notes = ["UEFA 2026/27 Articles 17–22 and Annex B: legal adjacent-rank-pair draws; two-legged knockout rounds; neutral single-match final.",
             "Undrawn bracket slots are sampled uniformly within UEFA seed constraints; discipline/coefficient ties use a random final tie-break when no final official ranking is available.",
             "Unresolved shootouts use the existing model convention of 50/50; played shootouts use their recorded verdict.",
             "The same sampled club rating uncertainty is carried from league phase through the final; goal distributions use five rating-gap bins."]
    result = SwissResult(sim, {}, coverage, states, None, notes,
                         max((f.get("updated_at") or "" for f in all_fx), default="") or None)
    def fail(reason):
        result.reason = reason
        return result
    if not fixtures:
        return fail("league_phase_draw_unavailable")
    if len(clubs) != 36 or len(fixtures) != 36 * games // 2:
        return fail("incomplete_league_phase_schedule")
    if unpriced:
        return fail("missing_team_mapping_or_strength")
    adjacency = np.zeros((36, 36), dtype=np.int16)
    home_count = np.zeros(36, dtype=int); away_count = np.zeros(36, dtype=int)
    for f in fixtures:
        h, a = index[cmap[f["home_api_id"]]], index[cmap[f["away_api_id"]]]
        if h == a or adjacency[h, a]:
            return fail("invalid_league_phase_opponents")
        adjacency[h, a] = adjacency[a, h] = 1
        home_count[h] += 1; away_count[a] += 1
        if f["status_short"] not in _DONE | _PENDING:
            return fail("league_phase_live_or_unresolved_fixture")
        if f["status_short"] in _DONE and (f["home_goals"] is None or f["away_goals"] is None):
            return fail("missing_final_score")
    if not (np.all(home_count == games // 2) and np.all(away_count == games // 2)):
        return fail("incomplete_league_phase_schedule")
    rng = np.random.default_rng(sm.cfg.random_seed if seed is None else seed)
    played = sum(f["status_short"] in _DONE for f in fixtures) * 2 / 36
    sigma = sm.cfg.season_rating_sigma * np.sqrt(6 / (6 + played))
    eps = rng.normal(0, sigma, (n_sims, 36))
    score = _Scores(sm, clubs, eps, rng, sigma)
    rows = np.arange(n_sims)
    # pts, goal difference, goals for, away goals, wins, away wins.
    stats = [np.zeros((n_sims, 36), dtype=np.int16) for _ in range(6)]
    now = np.zeros((36, 7), dtype=int)
    for f in fixtures:
        h, a = index[cmap[f["home_api_id"]]], index[cmap[f["away_api_id"]]]
        done = f["status_short"] in _DONE
        if done:
            gh, ga = int(f["home_goals"]), int(f["away_goals"])
        else:
            gh, ga = score.draw(np.full(n_sims, h), np.full(n_sims, a), rows)
        wh, wa, draw = gh > ga, ga > gh, gh == ga
        for target, value_h, value_a in zip(stats,
                (3*wh+draw, gh-ga, gh, 0, wh, 0),
                (3*wa+draw, ga-gh, ga, ga, wa, wa)):
            target[:, h] += value_h; target[:, a] += value_a
        if done:
            now[h] += [3*wh+draw, gh-ga, gh, 0, wh, 0, 1]
            now[a] += [3*wa+draw, ga-gh, ga, ga, wa, wa, 1]
    opponents = [x @ adjacency for x in stats[:3]]
    order = np.lexsort(tuple([rng.random((n_sims, 36))] + [-x for x in (stats + opponents)[::-1]]), axis=1)
    if sim.n_remaining == 0:
        official = [dict(r) for r in conn.execute(
            "SELECT team_api_id,rank,played,points,goals_diff FROM standing WHERE league_id=? AND season=? ORDER BY rank",
            (comp.api_football_id, comp.season))]
        if (len(official) == 36 and {r["rank"] for r in official} == set(range(1, 37))
                and {cmap.get(r["team_api_id"]) for r in official} == set(clubs)
                and all(r["played"] == games and r["points"] == now[index[cmap[r["team_api_id"]]], 0]
                        and r["goals_diff"] == now[index[cmap[r["team_api_id"]]], 1] for r in official)):
            order[:] = [index[cmap[r["team_api_id"]]] for r in official]
        elif len({tuple(x[0, i] for x in stats + opponents) for i in range(36)}) < 36:
            # Once the draw is real, uncertainty about a decisive official tie-break
            # cannot be randomized and still claim to condition on that draw.
            return fail("final_official_league_ranking_required")
    rank = np.argsort(order, axis=1) + 1
    sim.p_top_n = sim.p_qual_direct = {c: float(np.mean(rank[:, i] <= 8)) for i, c in enumerate(clubs)}
    sim.p_qual_playoff = {c: float(np.mean((rank[:, i] >= 9) & (rank[:, i] <= 24))) for i, c in enumerate(clubs)}
    sim.e_points = {c: float(stats[0][:, i].mean()) for i, c in enumerate(clubs)}
    sim.e_rank = {c: float(rank[:, i].mean()) for i, c in enumerate(clubs)}
    sim.table_now = [{"club_id": c, "pts": int(now[i, 0]), "gd": int(now[i, 1]),
                      "gf": int(now[i, 2]), "played": int(now[i, 6])} for i, c in enumerate(clubs)]
    sim.table_now.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["club_id"]))
    for f in ("top_n", "qual_direct", "qual_playoff"):
        states[f] = "ok"
    actual = {}
    league_start = min(f["kickoff_ts"] for f in fixtures)
    for f in all_fx:
        if f in fixtures or f["kickoff_ts"] <= league_start:
            continue
        rnd = _round_number(f["round"])
        if rnd is None:
            return fail("unknown_knockout_round")
        if sim.n_remaining:
            return fail("knockout_draw_before_league_completion")
        if f["status_short"] not in _DONE | _PENDING:
            return fail("knockout_live_or_unresolved_fixture")
        try:
            a, b = sorted((index[cmap[f["home_api_id"]]], index[cmap[f["away_api_id"]]]))
        except KeyError:
            return fail("knockout_club_outside_league_field")
        actual.setdefault((rnd, a, b), []).append(f)
    for key, legs in actual.items():
        if len(legs) != (1 if key[0] == 4 else 2):
            return fail("incomplete_knockout_tie_schedule")
        if len(legs) == 2:
            first, second = legs
            if (first["home_api_id"], first["away_api_id"]) != (second["away_api_id"], second["home_api_id"]):
                return fail("invalid_knockout_home_away_schedule")
            if second["status_short"] in _DONE and first["status_short"] not in _DONE:
                return fail("missing_prior_leg_result")
    try:
        bits = _draw_bits(order, actual, rng)
    except ValueError as e:
        return fail(str(e))
    top, seeded, low = bracket_slots(order, bits)
    reach = {}
    def record(fam, entrants):
        counts = np.bincount(entrants.ravel(), minlength=36)
        reach[fam] = {c: float(counts[i] / n_sims) for i, c in enumerate(clubs)}

    def decide(x, y, rnd, *, second_home_y=True):
        """x/y are path entrants. Default y hosts leg 2; observed fixtures override."""
        if not actual:
            if rnd == 4:
                total_a, total_b = score.draw(x, y, rows, ko=True, neutral=True)
            else:
                h1, a1 = (x, y) if second_home_y else (y, x)
                g1h, g1a = score.draw(h1, a1, rows, ko=True)
                g2h, g2a = score.draw(a1, h1, rows, ko=True)
                total_a, total_b = ((g1h+g2a, g1a+g2h) if second_home_y else (g1a+g2h, g1h+g2a))
            level = total_a == total_b
            win_a = total_a > total_b
            if np.any(level):
                eh, ea = (x[level], y[level]) if rnd == 4 or not second_home_y else (y[level], x[level])
                gh, ga = score.draw(eh, ea, rows[level], ko=True, neutral=rnd == 4, et=True)
                hw = (gh > ga) | ((gh == ga) & (rng.random(len(gh)) < .5))
                win_a[level] = hw if rnd == 4 or not second_home_y else ~hw
            return np.where(win_a, x, y)
        winner = np.empty(n_sims, dtype=np.int16)
        codes = x * 36 + y
        for code in np.unique(codes):
            mask = codes == code; rr = rows[mask]; a, b = int(code // 36), int(code % 36)
            xx, yy = x[mask], y[mask]
            legs = actual.get((rnd, min(a, b), max(a, b)))
            if legs:
                total_a = np.zeros(len(rr), dtype=int); total_b = np.zeros(len(rr), dtype=int)
                last_home = None
                for leg in legs:
                    h = index[cmap[leg["home_api_id"]]]; aw = index[cmap[leg["away_api_id"]]]
                    last_home = h
                    if leg["status_short"] in _DONE:
                        if leg["home_goals"] is None or leg["away_goals"] is None:
                            raise ValueError("missing_final_knockout_score")
                        gh, ga = int(leg["home_goals"]), int(leg["away_goals"])
                    else:
                        gh, ga = score.draw(np.full(len(rr), h), np.full(len(rr), aw), rr, ko=True, neutral=rnd == 4)
                    total_a += gh if h == a else ga; total_b += ga if h == a else gh
                if legs[-1]["status_short"] in _DONE:
                    if np.any(total_a == total_b):
                        raw = json.loads(legs[-1].get("raw_json") or "{}")
                        pen = (raw.get("score") or {}).get("penalty") or {}
                        ph, pa = pen.get("home"), pen.get("away")
                        if ph is None or pa is None or ph == pa:
                            raise ValueError("missing_knockout_shootout_verdict")
                        won = (a if ph > pa else b) if last_home == a else (b if ph > pa else a)
                        winner[mask] = won
                    else:
                        winner[mask] = np.where(total_a > total_b, a, b)
                    continue
                second_y = last_home == b
            else:
                second_y = second_home_y
                if rnd == 4:
                    total_a, total_b = score.draw(xx, yy, rr, ko=True, neutral=True)
                else:
                    h1, a1 = (xx, yy) if second_y else (yy, xx)
                    g1h, g1a = score.draw(h1, a1, rr, ko=True)
                    g2h, g2a = score.draw(a1, h1, rr, ko=True)
                    total_a, total_b = ((g1h+g2a, g1a+g2h) if second_y else (g1a+g2h, g1h+g2a))
            level = total_a == total_b
            win_a = total_a > total_b
            if np.any(level):
                eh, ea = (xx[level], yy[level]) if rnd == 4 or not second_y else (yy[level], xx[level])
                gh, ga = score.draw(eh, ea, rr[level], ko=True, neutral=rnd == 4, et=True)
                home_wins = (gh > ga) | ((gh == ga) & (rng.random(len(gh)) < .5))
                win_a[level] = home_wins if rnd == 4 or not second_y else ~home_wins
            winner[mask] = np.where(win_a, a, b)
        # A club named in a later published round has already advanced. Condition
        # earlier paths on that evidence even if earlier result ingestion is delayed.
        later = {c for (r, a, b) in actual if r > rnd for c in (a, b)}
        if later:
            xa, ya = np.isin(x, list(later)), np.isin(y, list(later))
            if np.any(xa & ya):
                raise ValueError("knockout_results_conflict_with_bracket")
            for (r, a, b), legs in actual.items():
                if r == rnd and legs[-1]["status_short"] in _DONE:
                    mask = ((x == a) & (y == b)) | ((x == b) & (y == a))
                    required = np.where(xa, x, y)
                    if np.any(mask & (xa | ya) & (winner != required)):
                        raise ValueError("knockout_results_conflict_with_bracket")
            winner = np.where(xa, x, np.where(ya, y, winner))
        return winner

    try:
        # Seeded playoff and R16 teams host the second leg.
        po = np.stack([decide(low[:, s], seeded[:, s], 0) for s in range(8)], axis=1)
        record("ro16", np.concatenate((top, po), axis=1))
        r16 = np.stack([decide(po[:, s], top[:, s], 1) for s in range(8)], axis=1)
        record("ro8", r16)
        # Per Annex B: slots 1/3 in each half carry top-4 home-second-leg rights.
        qf = np.stack([decide(r16[:, s], r16[:, s+1], 2) for s in (0, 2, 4, 6)], axis=1)
        record("ro4", qf)
        # The bottom QF in each half carries the top-2 path's home-second-leg right.
        sf = np.stack([decide(qf[:, s], qf[:, s+1], 3) for s in (0, 2)], axis=1)
        record("finalist", sf)
        champ = decide(sf[:, 0], sf[:, 1], 4)
        record("champion", champ[:, None])
    except (ValueError, TypeError) as e:
        return fail(str(e))
    sim.p_champion = reach.pop("champion")
    result.ladder = reach
    for f in ("champion", "ro16", "ro8", "ro4", "finalist"):
        states[f] = "ok"
    return result
