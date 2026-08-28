"""League season Monte-Carlo — the club replacement for the WC tournament sim
(TRANSFORM_PLAN C4/§3.3).

Simulates the REMAINING real fixture calendar of one competition N times on top
of the CURRENT live table and produces every season-market probability:

    p_champion / p_top_n / p_relegation (direct + half-weighted playoff spot) /
    p_last / e_points / e_rank  (+ full rank distribution)

Machinery kept from the WC tournament.py: vectorized scoreline sampling from
the Dixon-Coles matrix, deterministic seeding, mathematical-lock overlay idea
(a clinched title reads 100% because every simulated path says so; we only snap
float dust). Structure changed: the 4-team group loop is replaced by the real
per-fixture calendar (every fixture carries its own home side, priced with the
per-league home advantage — C2).

Tie-breaks come from the registry's per-league ``tiebreak`` rule and are EXACT,
head-to-head included (R4 closed — La Liga/Serie A are no longer approximated by
goal difference): pts > GD > GF, or pts > H2H pts > H2H GD > GD > GF (La Liga,
Serie A), or pts > GD > GF > H2H pts > H2H GD (Bundesliga). ``SeasonSim.tiebreak``
records the rule that was actually applied, because H2H needs every already-played
scoreline and a per-path pair ledger — when either is unavailable the sim falls
back to GD and says so rather than inventing an order.

Swiss league phases (UCL/UEL/UECL) reuse this module with rank cuts at
qual_direct/qual_playoff (ucl_phase wraps it).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from prediction_market_soccer.config import CONFIG, ModelConfig
from prediction_market_soccer.config.leagues import Stage, get, stage_of
from prediction_market_soccer.model.dixon_coles import score_matrix
from prediction_market_soccer.model.strength import StrengthModel

# Decided-fixture statuses. AWD (awarded / walkover) and WO belong here: the
# official standings already carry their result, so leaving them in the
# "remaining" list re-simulated a match that had been decided off the pitch and
# double-counted its points (it invented a phantom Libertadores title chance).
_FINISHED = ("FT", "AET", "PEN", "AWD", "WO", "CANC", "ABD")

# Statuses that actually produced a scoreline. CANC/ABD are absent on purpose: they
# never had one, and the standings do not count them in ``played`` either, so the
# head-to-head ledger and its audit below stay consistent with each other.
_H2H_PLAYED = ("FT", "AET", "PEN", "AWD", "WO")

# Which criteria sit AHEAD of the head-to-head mini-table, per registry ``tiebreak``.
# Clubs share a mini-table only when everything ahead of H2H is equal, so this string
# doubles as the tie-cluster label: bare points for La Liga/Serie A, the whole
# pts/GD/GF key for the Bundesliga (H2H is its 4th criterion, not its 2nd). An empty
# value means the league has no head-to-head criterion at all.
_TIEBREAK_PRE: dict[str, str] = {
    "pts_gd_gf": "",
    "pts_h2h_gd": "pts",
    "pts_gd_gf_h2h": "pts_gd_gf",
}

# Radices for the packed rank key. Inside one tied cluster a club can bank at most
# 3 pts × (meetings) × (cluster-1) head-to-head points — 114 in a 20-club double
# round-robin — so 512 leaves room for the 4-meeting leagues (Scottish Premiership)
# too, and the ±256 goal-difference window is far outside anything a mini-table
# produces. Both are clipped rather than trusted: a key that overflows its field
# corrupts the ORDER silently, which is the one failure mode worth paying for.
_H2H_PTS_N, _H2H_GD_N, _H2H_GD_OFF = 512, 512, 256

# Ceiling on the per-path pair ledger. Exact H2H needs, for every club pair that still
# has a fixture left, that pair's points and goal difference IN EVERY simulated path —
# three int8 columns per pair. A full-calendar La Liga at the default 200k paths costs
# ~114 MiB; past this ceiling, disclosing the GD approximation beats swapping.
_H2H_MAX_BYTES = 512 * 1024 * 1024


@dataclass
class SeasonSim:
    comp: str
    n_sims: int
    club_ids: list[str]
    p_champion: dict[str, float]
    p_top_n: dict[str, float]
    p_relegation: dict[str, float]      # direct + 0.5 × playoff spot
    p_last: dict[str, float]
    e_points: dict[str, float]
    e_rank: dict[str, float]
    table_now: list[dict]               # live table snapshot the sim started from
    n_remaining: int
    rank_dist: dict[str, list[float]] = field(default_factory=dict)  # club -> P(rank k)
    tiebreak: str = "pts_gd_gf"          # the rule ACTUALLY applied (see module docstring)
    # swiss league phase extras (None for domestic leagues)
    p_qual_direct: dict[str, float] | None = None    # top-8
    p_qual_playoff: dict[str, float] | None = None   # ranks 9..24


def _live_table(conn, comp) -> dict[str, dict]:
    """club_id -> {pts, gd, gf, played} from the standings table (API-supplied);
    GF parsed from raw_json (schema stores GD but not GF as a column)."""
    import json as _json
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT team_api_id, points, goals_diff, played, raw_json FROM standing "
        "WHERE league_id=? AND season=?", (comp.api_football_id, comp.season)):
        cid = cmap.get(r["team_api_id"])
        if not cid:
            continue
        gf = 0
        zone = None
        try:
            _raw = _json.loads(r["raw_json"]) or {}
            gf = int(((_raw.get("all") or {}).get("goals") or {}).get("for") or 0)
            # Zoned competitions (Argentina Apertura/Clausura run two 15-club zones;
            # CONMEBOL group phases run 8 groups of 4) publish the zone name here.
            # Carrying it lets the table/qualification view stay per-zone instead of
            # merging 30 clubs into one league table whose "top 8" is meaningless.
            zone = _raw.get("group") or None
        except Exception:
            pass
        out[cid] = {"pts": int(r["points"] or 0), "gd": int(r["goals_diff"] or 0),
                    "gf": gf, "played": int(r["played"] or 0), "zone": zone}
    _topup_unrecorded(conn, comp, out, cmap)
    return out


def _topup_unrecorded(conn, comp, table: dict[str, dict], cmap: dict[int, str]) -> None:
    """Apply finished league matches the standings have not absorbed yet, in place.

    The two halves of the simulation read different sources: the table comes from the
    API's `standing` feed and the fixtures to simulate come from our own `fixture`
    rows. A match that has just finished is already FT in `fixture` — so it is not
    simulated — while the standings refresh on a slower cadence, so its points are not
    in the table either. The result simply vanishes from the season. Measured on La Liga
    mid-matchday: standings held 17 played pairs against 18 finished fixtures.

    Only fixtures that kicked off AFTER the standings were fetched are eligible, which
    is what "the standings have not seen this yet" means. Scoping by the fetch time
    rather than by the whole season matters for a split-season competition: Argentina
    runs Apertura and Clausura as separate tables under one league id, both tagged
    Stage.LEAGUE, so a season-wide top-up folds all 240 finished Apertura matches into
    the Clausura table (measured: 90 played pairs → 330). A three-hour lead-in covers a
    match that was already in progress when the standings were read.
    """
    fetched = conn.execute(
        "SELECT MAX(updated_at) FROM standing WHERE league_id=? AND season=?",
        (comp.api_football_id, comp.season)).fetchone()[0]
    if not fetched:
        return
    want: dict[str, list] = {}
    for r in conn.execute(
        "SELECT round, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
        "WHERE league_id=? AND season=? AND status_short IN ({}) AND home_goals IS NOT NULL "
        "AND kickoff_ts > datetime(?, '-3 hours') "
        "ORDER BY kickoff_ts DESC".format(",".join("?" * len(_FINISHED))),
            (comp.api_football_id, comp.season, *_FINISHED, fetched)):
        if stage_of(comp.key, r["round"]) != Stage.LEAGUE:
            continue
        for cid, gf, ga in ((cmap.get(r["home_api_id"]), r["home_goals"], r["away_goals"]),
                            (cmap.get(r["away_api_id"]), r["away_goals"], r["home_goals"])):
            if cid is not None and cid in table:
                want.setdefault(cid, []).append((int(gf), int(ga)))
    # Safety cap: never credit a club with more matches than it has actually finished
    # all season. The eligibility filter above is what scopes the phase; this only stops
    # a malformed feed from inflating a table.
    total: dict[str, int] = {}
    for r in conn.execute(
        "SELECT round, home_api_id, away_api_id FROM fixture "
        "WHERE league_id=? AND season=? AND status_short IN ({}) AND home_goals IS NOT NULL"
            .format(",".join("?" * len(_FINISHED))),
            (comp.api_football_id, comp.season, *_FINISHED)):
        if stage_of(comp.key, r["round"]) != Stage.LEAGUE:
            continue
        for aid in (r["home_api_id"], r["away_api_id"]):
            cid = cmap.get(aid)
            if cid is not None:
                total[cid] = total.get(cid, 0) + 1
    for cid, results in want.items():
        row = table[cid]
        headroom = total.get(cid, 0) - int(row.get("played") or 0)
        for gf, ga in results[:max(0, min(len(results), headroom))]:   # most recent first
            row["pts"] += 3 if gf > ga else (1 if gf == ga else 0)
            row["gd"] += gf - ga
            row["gf"] += gf
            row["played"] += 1


def _remaining_fixtures(conn, comp) -> list[tuple[str, str]]:
    """(home_club, away_club) for every unplayed LEAGUE-stage fixture."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    out = []
    for r in conn.execute(
        "SELECT round, home_api_id, away_api_id, status_short FROM fixture "
        "WHERE league_id=? AND season=? AND status_short NOT IN ({}) "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))),
        (comp.api_football_id, comp.season) + _FINISHED):
        if stage_of(comp.key, r["round"]) != Stage.LEAGUE:
            continue
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hi and ai:
            out.append((hi, ai))
    return out


def _base_key(pts, gd, gf):
    """pts > GD > GF packed into one sortable int64 (the universal fallback order)."""
    return (pts.astype(np.int64) * 1_000_000
            + (gd.astype(np.int64) + 500) * 1_000 + gf.astype(np.int64))


def _played_h2h(conn, comp, idx: dict[str, int]) -> tuple[dict, int, int]:
    """The deterministic half of the head-to-head ledger: what these clubs have
    ALREADY done to each other, identical in every simulated path.

    Returns ``{(lo, hi): [pts_lo, pts_hi, gd_lo, meetings]}`` keyed by the pair's
    column indices (lower first), how many meetings were found, and how many
    finished meetings had no scoreline on file. The standings' own ``played``
    counter is what the caller audits the first number against: a ledger with holes
    would silently invent a tie-break, which is strictly worse than the
    goal-difference approximation it replaced.
    """
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    acc: dict[tuple[int, int], list[int]] = {}
    found = blank = 0
    for r in conn.execute(
        "SELECT round, home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE league_id=? AND season=? AND status_short IN ({})".format(
            ",".join("?" * len(_H2H_PLAYED))),
            (comp.api_football_id, comp.season) + _H2H_PLAYED):
        if stage_of(comp.key, r["round"]) != Stage.LEAGUE:
            continue
        h, a = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if h not in idx or a not in idx:
            continue
        hg, ag = r["home_goals"], r["away_goals"]
        if hg is None or ag is None:
            blank += 1
            continue
        found += 1
        h_i, a_i = idx[h], idx[a]
        hp = 3 if hg > ag else (1 if hg == ag else 0)
        ap = 3 if ag > hg else (1 if hg == ag else 0)
        lo, hi_ = (h_i, a_i) if h_i < a_i else (a_i, h_i)
        cell = acc.setdefault((lo, hi_), [0, 0, 0, 0])
        cell[3] += 1
        if h_i == lo:
            cell[0] += hp; cell[1] += ap; cell[2] += hg - ag
        else:
            cell[0] += ap; cell[1] += hp; cell[2] += ag - hg
    return acc, found, blank


def simulate_season(conn, comp_key: str, sm: StrengthModel, *,
                    n_sims: int | None = None, seed: int | None = None,
                    cfg: ModelConfig | None = None) -> SeasonSim:
    comp = get(comp_key)
    cfg = cfg or sm.cfg or CONFIG.model
    n_sims = n_sims or 200_000
    seed = seed if seed is not None else cfg.random_seed
    rng = np.random.default_rng(seed)

    table = _live_table(conn, comp)
    fixtures = [(h, a) for h, a in _remaining_fixtures(conn, comp)
                if h in sm.ratings and a in sm.ratings]
    if not fixtures and not any((t.get("played") or 0) > 0 for t in table.values()):
        # cup-state / pre-draw comp: nothing to simulate — return an empty light
        # result instead of ranking 200k copies of an all-zero table (perf + the
        # alphabet-champion nonsense both die here; run_model swaps in ko_champion).
        clubs = sorted(sm.ratings)
        empty = {c: 0.0 for c in clubs}
        return SeasonSim(comp=comp_key, n_sims=0, club_ids=clubs,
                         p_champion={}, p_top_n={}, p_relegation={}, p_last={},
                         e_points={}, e_rank={}, table_now=[], n_remaining=0,
                         rank_dist={}, p_qual_direct=None, p_qual_playoff=None)

    clubs = sorted({c for c in sm.ratings
                    if c in table or any(c in fx for fx in fixtures)})
    # swiss league phases pre-draw have no table rows yet; fall back to rated clubs
    if not clubs:
        clubs = sorted(sm.ratings)
    idx = {c: i for i, c in enumerate(clubs)}
    n = len(clubs)

    pts = np.zeros((n_sims, n), dtype=np.int32)
    gd = np.zeros((n_sims, n), dtype=np.int32)
    gf = np.zeros((n_sims, n), dtype=np.int32)
    for c, i in idx.items():
        t = table.get(c) or {"pts": 0, "gd": 0, "gf": 0}
        pts[:, i] = t["pts"]; gd[:, i] = t["gd"]; gf[:, i] = t["gf"]

    # ── exact head-to-head tie-break (R4) ────────────────────────────────────
    # The mini-table among tied clubs cannot be read off the finished table — it needs
    # the individual results — so it is carried as a PAIR ledger: the played half is a
    # constant, the unplayed half accrues per simulated path while the fixtures are
    # sampled below. Only pairs with a fixture left need per-path columns, which is why
    # a March run costs a fraction of an August one.
    pre_mode = _TIEBREAK_PRE.get(comp.tiebreak, "")
    want_h2h = bool(pre_mode)
    tiebreak_applied = comp.tiebreak or "pts_gd_gf"
    pair_base: dict[tuple[int, int], list[int]] = {}
    h2h_cols: dict[tuple[int, int], int] = {}
    sim_lo = sim_hi = sim_gd = None
    if want_h2h:
        rem_pairs = sorted({(min(idx[h], idx[a]), max(idx[h], idx[a])) for h, a in fixtures})
        need = n_sims * len(rem_pairs) * 3
        pair_base, n_found, n_blank = _played_h2h(conn, comp, idx)
        n_expected = int(round(sum((table.get(c) or {}).get("played") or 0 for c in idx) / 2.0))
        # Every league that puts H2H ahead of goal difference plays a uniform double
        # round-robin, so "each pair meets the same number of times" is a cheap
        # integrity check on the CALENDAR (the audit above only covers results): a
        # half-ingested fixture list would otherwise hand the mini-table a pair that
        # met once, and rank the table on it without ever saying so.
        meetings: dict[tuple[int, int], int] = {p: c[3] for p, c in pair_base.items()}
        for h, a in fixtures:
            p = (min(idx[h], idx[a]), max(idx[h], idx[a]))
            meetings[p] = meetings.get(p, 0) + 1
        uniform = (len(meetings) == n * (n - 1) // 2 and len(set(meetings.values())) == 1)
        if n_blank or n_found < n_expected:
            want_h2h = False
            tiebreak_applied = (f"pts_gd_gf (h2h unavailable: {n_found}/{n_expected} played "
                                f"meetings on file, {n_blank} without a score)")
        elif not uniform:
            want_h2h = False
            tiebreak_applied = (f"pts_gd_gf (h2h unavailable: calendar has {len(meetings)} of "
                                f"{n * (n - 1) // 2} club pairs, meeting counts "
                                f"{sorted(set(meetings.values()))})")
        elif need > _H2H_MAX_BYTES:
            want_h2h = False
            tiebreak_applied = (f"pts_gd_gf (h2h skipped: pair ledger needs "
                                f"{need / 2 ** 20:.0f} MiB > {_H2H_MAX_BYTES / 2 ** 20:.0f} MiB)")
        elif rem_pairs:
            h2h_cols = {p: k for k, p in enumerate(rem_pairs)}
            sim_lo = np.zeros((n_sims, len(rem_pairs)), dtype=np.int8)   # pts of the lower-indexed club
            sim_hi = np.zeros_like(sim_lo)                               # pts of the higher-indexed club
            sim_gd = np.zeros_like(sim_lo)                               # goal diff, lower club's view

    kmax = cfg.score_matrix_kmax
    k1 = kmax + 1

    # ── season-long rating uncertainty (parameter risk) ──────────────────────
    # Without it every simulated path uses the SAME point-estimate ratings, so a
    # favourite's per-match edge compounds deterministically over a 34-round
    # season: Bayern read 99.99% champion before a ball was kicked while the
    # market paid 87¢. Each simulation path instead draws ONE rating offset per
    # club, held for the whole season — "our rating could be wrong / squads and
    # form shift" — which widens the title race to a credible spread without
    # touching any single-match price (this sim is the only consumer).
    # σ shrinks as evidence accrues: before a ball is kicked our strength estimate
    # rests entirely on last season + talent priors, so a whole-season shock (an
    # injury run, a transfer, a managerial change) can still decide the title; by
    # mid-season the table itself has answered most of that. σ_eff = σ·√(P0/(P0+played)).
    _P0 = 6.0
    _played = [t.get("played") or 0 for t in table.values()]
    _avg_played = (sum(_played) / len(_played)) if _played else 0.0
    sigma = float(getattr(cfg, "season_rating_sigma", 0.0) or 0.0) * \
        (_P0 / (_P0 + _avg_played)) ** 0.5
    if sigma > 0 and fixtures:
        eps = rng.normal(0.0, sigma, size=(n_sims, n))
        # bucket each fixture's per-sim rating-gap shift into a few bins so the
        # score matrix is built per bin (5 × 11×11) instead of per simulation.
        # 5-point discretisation of the standardised gap shift (bin representatives at
        # the 10/25/50/75/90th percentiles, split at their midpoints). It understates
        # the extreme tails slightly; σ was calibrated against the market WITH this
        # discretisation, so the pair is tuned together.
        _BINS = np.array([-1.2816, -0.5244, 0.0, 0.5244, 1.2816])
    else:
        eps = None

    for h, a in fixtures:
        lam_h, lam_a = sm.pair_lambdas(h, a)
        draw = rng.random(n_sims)
        if eps is None:
            m = score_matrix(lam_h, lam_a, cfg.dc_rho, kmax).ravel()
            cdf = np.cumsum(m)
            cdf[-1] = 1.0
            cell = np.searchsorted(cdf, draw, side="right")
        else:
            hi_, ai_ = idx[h], idx[a]
            d = eps[:, hi_] - eps[:, ai_]              # per-sim rating-gap shift
            sd = sigma * np.sqrt(2.0)
            edges = _BINS[:-1] * sd + np.diff(_BINS) * sd / 2.0
            bid = np.searchsorted(edges, d, side="right")
            cell = np.empty(n_sims, dtype=np.int64)
            for b, z in enumerate(_BINS):
                mask = bid == b
                if not mask.any():
                    continue
                lh, la = sm.pair_lambdas(h, a, rating_shift=float(z * sd))
                mb = score_matrix(lh, la, cfg.dc_rho, kmax).ravel()
                cdfb = np.cumsum(mb)
                cdfb[-1] = 1.0
                cell[mask] = np.searchsorted(cdfb, draw[mask], side="right")
        gh = (cell // k1).astype(np.int32)
        ga = (cell % k1).astype(np.int32)
        hi, ai = idx[h], idx[a]
        home_w = gh > ga
        draw_m = gh == ga
        hpts = (3 * home_w + draw_m).astype(np.int32)
        apts = (3 * (~home_w & ~draw_m) + draw_m).astype(np.int32)
        pts[:, hi] += hpts
        pts[:, ai] += apts
        diff = gh - ga
        gd[:, hi] += diff; gd[:, ai] -= diff
        gf[:, hi] += gh;  gf[:, ai] += ga
        if sim_lo is not None:
            # same numbers again, but booked into the two clubs' private mini-table
            col = h2h_cols[(hi, ai) if hi < ai else (ai, hi)]
            lo_pts, hi_pts = (hpts, apts) if hi < ai else (apts, hpts)
            sim_lo[:, col] += lo_pts.astype(np.int8)
            sim_hi[:, col] += hi_pts.astype(np.int8)
            sim_gd[:, col] += diff.astype(np.int8) if hi < ai else (-diff).astype(np.int8)

    if not want_h2h:
        key = _base_key(pts, gd, gf)
    else:
        # Tie clusters, vectorized: the pre-key IS the cluster label (clubs share a
        # mini-table exactly when every criterion ahead of H2H is equal), so the whole
        # pass is a loop over club PAIRS — at most 190 of them — instead of over the
        # 200k paths, which no Python loop could touch. Each pair only writes into the
        # paths where those two clubs actually finished level (single-digit percent of
        # them), so `flatnonzero` keeps the arithmetic proportional to the ties that
        # exist rather than to the table size.
        pre = pts if pre_mode == "pts" else _base_key(pts, gd, gf)
        h2h_p = np.zeros((n_sims, n), dtype=np.int16)
        h2h_g = np.zeros((n_sims, n), dtype=np.int16)
        for lo, hi_ in sorted(set(pair_base) | set(h2h_cols)):
            sel = np.flatnonzero(pre[:, lo] == pre[:, hi_])
            if sel.size == 0:
                continue
            b_lo, b_hi, b_gd, _ = pair_base.get((lo, hi_)) or (0, 0, 0, 0)
            col = h2h_cols.get((lo, hi_))
            if col is None:                      # pair already done playing each other
                p_lo, p_hi, g_lo = b_lo, b_hi, b_gd
            else:
                p_lo = sim_lo[sel, col].astype(np.int16) + b_lo
                p_hi = sim_hi[sel, col].astype(np.int16) + b_hi
                g_lo = sim_gd[sel, col].astype(np.int16) + b_gd
            h2h_p[sel, lo] += p_lo
            h2h_p[sel, hi_] += p_hi
            h2h_g[sel, lo] += g_lo
            h2h_g[sel, hi_] -= g_lo
        del sim_lo, sim_hi, sim_gd           # ~114 MiB at 200k paths — drop before argsort
        hp = np.clip(h2h_p, 0, _H2H_PTS_N - 1).astype(np.int64)
        hg = np.clip(h2h_g, -_H2H_GD_OFF, _H2H_GD_OFF - 1).astype(np.int64) + _H2H_GD_OFF
        if pre_mode == "pts":                # pts > H2H pts > H2H GD > GD > GF
            key = ((((pts.astype(np.int64) * _H2H_PTS_N + hp) * _H2H_GD_N + hg) * 1_000
                    + np.clip(gd, -499, 499).astype(np.int64) + 500) * 1_000
                   + np.clip(gf, 0, 999).astype(np.int64))
        else:                                # pts > GD > GF > H2H pts > H2H GD
            key = (pre * _H2H_PTS_N + hp) * _H2H_GD_N + hg
    order = np.argsort(-key, axis=1, kind="stable")
    ranks = np.empty_like(order)
    rows = np.arange(n_sims)[:, None]
    ranks[rows, order] = np.arange(n)[None, :] + 1   # 1 = champion

    def _p(mask: np.ndarray) -> dict[str, float]:
        frac = mask.mean(axis=0)
        return {c: float(frac[i]) for c, i in idx.items()}

    def _snap(d: dict[str, float]) -> dict[str, float]:
        return {k: (1.0 if v > 0.99995 else 0.0 if v < 0.00005 else round(v, 5))
                for k, v in d.items()}

    p_champ = _snap(_p(ranks == 1))
    p_top = _snap(_p(ranks <= comp.top_n))
    rl_lo = n - comp.releg_direct + 1
    p_rel_direct = _p(ranks >= rl_lo)
    p_rel = p_rel_direct
    if comp.releg_playoff:
        p_play = _p(ranks == rl_lo - comp.releg_playoff)
        p_rel = {c: p_rel_direct[c] + 0.5 * p_play[c] for c in p_rel_direct}
    p_rel = _snap(p_rel)
    p_last = _snap(_p(ranks == n))

    e_pts = {c: float(pts[:, i].mean()) for c, i in idx.items()}
    e_rank = {c: float(ranks[:, i].mean()) for c, i in idx.items()}
    # bincount, not an O(n²·sims) comprehension — a 153-club comp made the naive
    # version do billions of element-ops (second perf bomb caught on pipeline v3).
    rank_dist = {}
    for c, i in idx.items():
        bc = np.bincount(ranks[:, i], minlength=n + 1)[1:n + 1]
        rank_dist[c] = [float(x) for x in (bc / n_sims)]

    p_qd = p_qp = None
    if comp.kind == "swiss_ucl":
        p_qd = _snap(_p(ranks <= comp.qual_direct))
        p_qp = _snap(_p((ranks > comp.qual_direct) & (ranks <= comp.qual_playoff)))

    # Zone first (so a two-zone competition reads as two tables, not one merged
    # 30-club league whose ranks mean nothing), then the usual pts > GD > GF.
    table_now = sorted(
        [{"club_id": c, **(table.get(c) or {"pts": 0, "gd": 0, "gf": 0, "played": 0, "zone": None})}
         for c in clubs],
        key=lambda r: (str(r.get("zone") or ""), -r["pts"], -r["gd"], -r["gf"]))

    return SeasonSim(
        comp=comp_key, n_sims=n_sims, club_ids=clubs,
        p_champion=p_champ, p_top_n=p_top, p_relegation=p_rel, p_last=p_last,
        e_points={c: round(v, 2) for c, v in e_pts.items()},
        e_rank={c: round(v, 2) for c, v in e_rank.items()},
        table_now=table_now, n_remaining=len(fixtures), rank_dist=rank_dist,
        tiebreak=tiebreak_applied, p_qual_direct=p_qd, p_qual_playoff=p_qp,
    )


def h2h_selfcheck(seed: int = 3, n_sims: int = 200) -> str:
    """Prove the head-to-head rule on a complete synthetic season. Touches no file.

    Builds a six-club double round-robin in an in-memory store, then ranks the finished
    table twice with a slow but obviously-correct reference — goal difference only, and
    the real La Liga/Serie A rule — and asserts that (a) the two disagree, so the check
    has teeth, and (b) ``simulate_season`` reproduces the exact one. With nothing left
    to play the Monte-Carlo collapses to its ranking function, which is the part under
    test; the simulated half of the ledger rides the same pair columns.
    """
    import sqlite3
    from types import SimpleNamespace

    from prediction_market_soccer.ingest import store

    comp = get("laliga")                       # pts > H2H pts > H2H GD > GD > GF
    ids = [(9000 + i, f"h2h_club_{i}") for i in range(6)]
    rng = np.random.default_rng(seed)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    for api, cid in ids:
        store.upsert(conn, "team_meta", {"api_id": api, "canonical_team_id": cid,
                                         "updated_at": store.utcnow()}, pk=["api_id"])
    tot = {cid: [0, 0, 0, 0] for _, cid in ids}          # pts, gd, gf, played
    meet: dict[tuple[str, str], list[int]] = {}          # (lo,hi) -> pts_lo, pts_hi, gd_lo
    fid = 0
    for hx, (h_api, h_cid) in enumerate(ids):
        for ax, (a_api, a_cid) in enumerate(ids):
            if hx == ax:
                continue
            hg, ag = int(rng.integers(0, 4)), int(rng.integers(0, 4))
            fid += 1
            store.upsert(conn, "fixture", {
                "api_id": fid, "league_id": comp.api_football_id, "season": comp.season,
                "round": "Regular Season - 1", "status_short": "FT", "kickoff_ts": "2026-01-01",
                "home_api_id": h_api, "away_api_id": a_api, "home_goals": hg,
                "away_goals": ag, "updated_at": store.utcnow()}, pk=["api_id"])
            hp = 3 if hg > ag else (1 if hg == ag else 0)
            ap = 3 if ag > hg else (1 if hg == ag else 0)
            for cid, p, gfor, gag in ((h_cid, hp, hg, ag), (a_cid, ap, ag, hg)):
                tot[cid][0] += p; tot[cid][1] += gfor - gag
                tot[cid][2] += gfor; tot[cid][3] += 1
            lo, hi_ = sorted((h_cid, a_cid))
            cell = meet.setdefault((lo, hi_), [0, 0, 0])
            if lo == h_cid:
                cell[0] += hp; cell[1] += ap; cell[2] += hg - ag
            else:
                cell[0] += ap; cell[1] += hp; cell[2] += ag - hg
    for rank, (api, cid) in enumerate(ids, start=1):
        p, d, f_, pl = tot[cid]
        store.upsert(conn, "standing", {
            "league_id": comp.api_football_id, "season": comp.season, "team_api_id": api,
            "rank": rank, "points": p, "goals_diff": d, "played": pl,
            "raw_json": '{"all":{"goals":{"for":%d}}}' % f_, "updated_at": store.utcnow()},
            pk=["league_id", "season", "team_api_id"])

    def _reference(with_h2h: bool) -> list[str]:
        """Rank by the book, one club at a time — the slow twin of the packed key."""
        def sort_key(cid: str):
            p, d, f_, _ = tot[cid]
            if not with_h2h:
                return (p, d, f_)
            hp = hg = 0
            for other in tot:
                if other == cid or tot[other][0] != p:
                    continue                    # different points → different mini-table
                lo, hi_ = sorted((cid, other))
                cell = meet.get((lo, hi_)) or [0, 0, 0]
                hp += cell[0] if lo == cid else cell[1]
                hg += cell[2] if lo == cid else -cell[2]
            return (p, hp, hg, d, f_)
        return sorted(tot, key=sort_key, reverse=True)

    exact, gd_only = _reference(True), _reference(False)
    assert exact != gd_only, f"seed {seed} has no tie where H2H and GD disagree"
    sm = SimpleNamespace(ratings={cid: 0.0 for _, cid in ids}, cfg=None)
    sim = simulate_season(conn, "laliga", sm, n_sims=n_sims, seed=1)
    got = sorted(sim.e_rank, key=lambda c: sim.e_rank[c])
    assert got == exact, f"H2H order wrong\n  got   {got}\n  want  {exact}"
    return (f"h2h selfcheck OK (rule={sim.tiebreak}) — exact {exact} "
            f"vs GD-only {gd_only}")


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    print(h2h_selfcheck())
    conn = store.init_db()
    for lg in ("epl", "brasileirao"):
        sm = build_strength(load_prior(lg), league=lg)
        sim = simulate_season(conn, lg, sm, n_sims=50_000)
        top = sorted(sim.p_champion.items(), key=lambda kv: -kv[1])[:6]
        rel = sorted(sim.p_relegation.items(), key=lambda kv: -kv[1])[:4]
        print(f"— {lg}: {sim.n_remaining} remaining fixtures, N={sim.n_sims} —")
        print("  champion:", ", ".join(f"{c} {p:.1%}" for c, p in top))
        print("  relegation:", ", ".join(f"{c} {p:.1%}" for c, p in rel))
        s = sum(sim.p_champion.values())
        print(f"  Σp_champion = {s:.4f} (identity gate)")
        print(f"  tie-break applied: {sim.tiebreak}")
