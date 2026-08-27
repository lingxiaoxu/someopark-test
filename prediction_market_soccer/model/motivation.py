"""Match-motivation λ multipliers — club season-incentive psychology.

**Ships DISABLED (config ``motiv_enabled=False``, ``motiv_weight=0.0``) and is a total
no-op in that state.** The reason is not that the effect is implausible — it is that
NOTHING here has been validated on club data. The World-Cup edition of this file encoded
three-group-game progression psychology (wounded favourite bounce-back, clinched rotation)
that was tuned and sanity-checked on a 48-team tournament; none of those constants, and
none of the four club signals below, survive that provenance. A club season has ~34-38
rounds, promotion/relegation, and a parallel continental calendar — a different animal
entirely. Turning this on means first earning the numbers: a per-league PIT study over
enough settled run-in fixtures to show the tilt beats the flat model out of sample.
Until then the honest setting is off, and the base model prices every match clean.

A LIVE betting-only tilt when it IS enabled: never used in calibration / OOS / the
trade-grade gate, so the gate stays validated on the clean base model.

Five signals, all driven by a POINT-IN-TIME league table rebuilt from finished fixtures
(never the ``standing`` snapshot table, which holds today's table and would leak the
future into any replay):

  A — 保级生死战 relegation fight. Late in the season, a team in or just above the drop
      zone that can still be saved (or still fall) plays with everything: its attacking λ
      goes up AND the λ it concedes goes up (chasing the game leaves gaps behind).
  B — 争冠末段 title run-in. Late, still mathematically alive for the title, near the top:
      a modest attacking lift.
  C — 垫底摆烂 dead rubber. Late, mathematically safe, out of the European places, and out
      of (or already clinched) the title: intensity drops.
  D — 欧战赛前轮换 pre-continental rotation. A DOMESTIC league match played within a few
      days before that club's UCL/UEL/UECL/Libertadores/Sudamericana tie — the classic
      rested-XI game. (Reading the fixture CALENDAR is not a look-ahead: the schedule is
      published months in advance. Only reading future RESULTS would be.)
  E — 杯赛已晋级后的轮换 clinched cup rotation. The direct club heir of the WC's factor 2:
      in the FINAL Swiss league-phase round, a club whose points already clear the
      qualification cut with a cushion no rival can close rotates for the knockout.

Registry-driven throughout (``config/leagues.py``): the competition, its stage, its
relegation/European/qualification cuts and its season length all come from the registry
or the fixture calendar — never from a round-name substring guess. That guess is exactly
the WC bug class C1 that let CONMEBOL's "Group Stage - N" rounds fall into the World Cup
group-progression branch.

Returns (mult_home, mult_away, info) — multipliers on each side's attacking λ, plus a small
dict describing what fired (for transparency in upcoming.json). (1.0, 1.0, None) when
nothing applies, when disabled, or when the fixture cannot be resolved (fail-closed).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Continental competitions whose midweek ties trigger signal D's domestic rotation.
CONTINENTAL = frozenset({"ucl", "uel", "uecl", "libertadores", "sudamericana"})

# Competition kinds that own a single, meaningful points table. CONMEBOL's "cup_two_leg"
# comps are excluded on purpose: their "Group Stage - N" rounds are EIGHT parallel
# four-team groups, so one merged table would rank teams that never play each other.
TABLE_KINDS = frozenset({"league", "league_playoffs", "swiss_ucl"})

_FINISHED = ("FT", "AET", "PEN")


def _round_num(round_name) -> int | None:
    """Round number of a NUMBERED league round, else None.

    Matches the trailing ``- N`` that API-Football uses for every league-shaped round
    ("Regular Season - 12", "League Stage - 3", "Apertura - 7", "Group Stage - 4") and
    deliberately does NOT match a knockout round that merely contains a number
    ("Round of 32", "Round of 16", "Apertura - Round of 16" → None).
    """
    m = re.search(r"-\s*(\d+)\s*$", str(round_name or ""))
    return int(m.group(1)) if m else None


def _round_key(round_name) -> str | None:
    """The tournament a numbered round belongs to ("Regular Season", "Apertura", …).

    Argentina runs Apertura and Clausura as two SEPARATE tournaments with two separate
    tables under one league_id, so a table must never merge rounds across this key.
    """
    s = str(round_name or "")
    m = re.match(r"^(.*?)\s*-\s*\d+\s*$", s)
    return m.group(1).strip().lower() if m else None


class _Standing:
    __slots__ = ("played", "points", "gd")

    def __init__(self) -> None:
        self.played = self.points = self.gd = 0


def _pit_table(conn, league_id: int, season: int, round_key: str, *, before_ts: str):
    """PIT points table: [(api_id, _Standing)] ranked, from that tournament's FINISHED
    fixtures that kicked off strictly BEFORE ``before_ts``.

    PIT guard (inherited from the WC ``before_round`` guard): a match being replayed sees
    only what had actually been played when it kicked off — never its own result, never a
    later round's. Ranking is points → goal difference; the registry's per-league
    head-to-head tie-breaks are NOT applied, which can misplace teams level on points.
    That is acceptable for a zone/slack test and is one more reason this ships off.
    """
    table: dict[int, _Standing] = {}
    for r in conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, round FROM fixture "
        "WHERE league_id=? AND season=? AND status_short IN (?,?,?) AND home_goals IS NOT NULL "
        "AND kickoff_ts IS NOT NULL AND kickoff_ts < ?",
            (league_id, season, *_FINISHED, before_ts)):
        if _round_key(r["round"]) != round_key:
            continue
        hg, ag = r["home_goals"], r["away_goals"]
        for tid, gf, ga in ((r["home_api_id"], hg, ag), (r["away_api_id"], ag, hg)):
            st = table.setdefault(tid, _Standing())
            st.played += 1
            st.gd += gf - ga
            st.points += 3 if gf > ga else (1 if gf == ga else 0)
    return sorted(table.items(), key=lambda kv: (-kv[1].points, -kv[1].gd))


def _scheduled_rounds(conn, league_id: int, season: int, round_key: str, api_id: int) -> int:
    """How many rounds of this tournament the club is scheduled to play, all statuses.

    Read from the published calendar rather than assumed as 2×(n_teams−1): Argentina's
    zoned Apertura/Clausura and the Swiss league phase are not double round-robins.
    """
    row = conn.execute(
        "SELECT round FROM fixture WHERE league_id=? AND season=? "
        "AND (home_api_id=? OR away_api_id=?)", (league_id, season, api_id, api_id)).fetchall()
    return sum(1 for r in row if _round_key(r["round"]) == round_key)


def _has_continental_tie(conn, api_id: int, kickoff_ts: str, days: int) -> bool:
    """Does this club have a continental fixture inside the ``days`` after this kickoff?

    Compares kickoff_ts lexically, which is exact here because ingest stores every one in
    the same 25-char UTC form ("2026-08-27T16:00:00+00:00") — the same assumption the PIT
    ``before_ts`` guard rests on.
    """
    from prediction_market_soccer.config.leagues import REGISTRY

    ids = [REGISTRY[k].api_football_id for k in CONTINENTAL if k in REGISTRY]
    try:
        ko = datetime.fromisoformat(kickoff_ts)
    except (TypeError, ValueError):
        return False
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    hi = (ko + timedelta(days=days)).isoformat()
    marks = ",".join("?" * len(ids))
    row = conn.execute(
        f"SELECT 1 FROM fixture WHERE league_id IN ({marks}) AND (home_api_id=? OR away_api_id=?) "
        "AND kickoff_ts > ? AND kickoff_ts <= ? LIMIT 1",
        (*ids, api_id, api_id, kickoff_ts, hi)).fetchone()
    return row is not None


def _resolve_fixture(conn, api_home: set[int], api_away: set[int], round_name):
    """The stored fixture row for this pairing+round → (league_id, season, kickoff_ts).

    Returns None when the pairing is not in the store or the round is ambiguous; every
    caller path then falls back to neutral. Fail-closed by design: a motivation tilt is a
    nicety, and guessing a competition would reintroduce the very bug class C1 removed.
    """
    if not api_home or not api_away or not round_name:
        return None
    hm, am = ",".join("?" * len(api_home)), ",".join("?" * len(api_away))
    return conn.execute(
        f"SELECT league_id, season, kickoff_ts FROM fixture WHERE home_api_id IN ({hm}) "
        f"AND away_api_id IN ({am}) AND round=? AND kickoff_ts IS NOT NULL "
        "ORDER BY kickoff_ts DESC LIMIT 1",
        (*api_home, *api_away, str(round_name))).fetchone()


def motivation_multipliers(conn, fifa_rank: dict, home_id: str, away_id: str,
                           round_name, cfg) -> tuple[float, float, dict | None]:
    """(mult_home, mult_away, info) on each side's attacking λ. Neutral unless enabled.

    ``fifa_rank`` is accepted for signature compatibility with the existing callers and is
    IGNORED: clubs have no FIFA ranking (``ops/performance_report._fifa_ranks()`` returns an
    empty map here), and the PIT league table replaces it as the strength/incentive anchor.
    """
    # Two independent kill switches, checked before ANY work: the flag, and the weight that
    # scales every tilt. Off means off — no query, no import, no allocation.
    weight = float(getattr(cfg, "motiv_weight", 0.0) or 0.0)
    if not getattr(cfg, "motiv_enabled", False) or weight <= 0.0 or conn is None:
        return 1.0, 1.0, None

    from prediction_market_soccer.config.leagues import Stage, by_api_id, stage_of

    api_of: dict[str, set[int]] = {}
    for r in conn.execute("SELECT api_id, canonical_team_id FROM team_meta "
                          "WHERE canonical_team_id IN (?,?)", (home_id, away_id)):
        api_of.setdefault(r["canonical_team_id"], set()).add(r["api_id"])
    fx = _resolve_fixture(conn, api_of.get(home_id, set()), api_of.get(away_id, set()), round_name)
    if fx is None:
        return 1.0, 1.0, None

    comp = by_api_id(fx["league_id"])
    round_key = _round_key(round_name)
    # Only a league-shaped round of a comp that owns one real table can carry these signals.
    if (comp is None or comp.kind not in TABLE_KINDS or round_key is None
            or stage_of(comp.key, round_name) != Stage.LEAGUE):
        return 1.0, 1.0, None

    table = _pit_table(conn, fx["league_id"], fx["season"], round_key, before_ts=fx["kickoff_ts"])
    if len(table) < 4:
        return 1.0, 1.0, None
    pos = {tid: i + 1 for i, (tid, _) in enumerate(table)}
    pts_at = [st.points for _, st in table]      # points by rank-1 index
    n = len(table)
    leader_pts = pts_at[0]

    # Registry cuts. releg_playoff counts as a place worth fighting from (a play-off spot is
    # still relegation-adjacent), and top_n is the European cut the season markets trade.
    releg_cut = n - (comp.releg_direct + comp.releg_playoff) if comp.releg_direct or comp.releg_playoff else 0
    euro_cut = comp.top_n if comp.kind in ("league", "league_playoffs") else 0
    slack = int(getattr(cfg, "motiv_zone_slack", 2))
    late_frac = float(getattr(cfg, "motiv_late_frac", 0.75))
    min_played = int(getattr(cfg, "motiv_min_played", 6))

    def _pts_of_rank(rank: int) -> int | None:
        return pts_at[rank - 1] if 1 <= rank <= n else None

    mult = {home_id: 1.0, away_id: 1.0}
    reason: dict[str, str] = {}

    for X, O in ((home_id, away_id), (away_id, home_id)):
        tid = next((a for a in api_of.get(X, ()) if a in pos), None)
        if tid is None:
            continue
        st = table[pos[tid] - 1][1]
        rank = pos[tid]
        total = _scheduled_rounds(conn, fx["league_id"], fx["season"], round_key, tid)
        remaining = total - st.played
        if st.played < min_played or remaining < 1:
            continue
        gain = 3 * remaining          # the most this club can still add to its own points
        late = st.played >= late_frac * total

        # ── D — pre-continental rotation (domestic league only; independent of the table) ──
        if (comp.kind in ("league", "league_playoffs")
                and _has_continental_tie(conn, tid, fx["kickoff_ts"],
                                         int(getattr(cfg, "motiv_europe_window_days", 3)))):
            mult[X] *= float(getattr(cfg, "motiv_rotation_attack", 0.93))
            reason[X] = "continental_rotation"

        # ── E — clinched cup rotation: final Swiss league-phase round, qualification safe ──
        if comp.kind == "swiss_ucl" and _round_num(round_name) == total:
            cut_pts = _pts_of_rank(comp.qual_playoff + 1)
            if cut_pts is not None and st.points - cut_pts > 3:
                mult[X] *= float(getattr(cfg, "motiv_rotation_attack", 0.93))
                reason[X] = f"cup_clinched_rotation(pts={st.points})"

        if not late:
            continue

        drop_line = _pts_of_rank(releg_cut + 1) if releg_cut > 0 else None
        euro_line = _pts_of_rank(euro_cut) if euro_cut > 0 else None
        # "In the fight" spans the drop zone itself plus `slack` places above it.
        in_drop_fight = releg_cut > 0 and rank > releg_cut - slack
        safe_from_drop = (drop_line is None) or (st.points - drop_line > gain)
        alive_for_title = (leader_pts - st.points) <= gain and rank <= 1 + slack
        clinched_title = rank == 1 and (_pts_of_rank(2) is not None
                                        and st.points - _pts_of_rank(2) > gain)
        out_of_europe = euro_line is not None and (euro_line - st.points) > gain

        # ── A — relegation fight: attack up, AND concede more (chasing leaves gaps) ──
        if in_drop_fight and not safe_from_drop:
            mult[X] *= float(getattr(cfg, "motiv_relegation_attack", 1.06))
            mult[O] *= float(getattr(cfg, "motiv_relegation_def", 1.04))
            reason[X] = f"relegation_fight(rank={rank}/{n},left={remaining})"

        # ── B — title run-in ──
        elif alive_for_title and not clinched_title:
            mult[X] *= float(getattr(cfg, "motiv_title_attack", 1.05))
            reason[X] = f"title_runin(rank={rank},gap={leader_pts - st.points})"

        # ── C — dead rubber: nothing left to win or lose ──
        elif safe_from_drop and out_of_europe and (clinched_title or (leader_pts - st.points) > gain):
            mult[X] *= float(getattr(cfg, "motiv_dead_rubber_attack", 0.95))
            reason[X] = f"dead_rubber(rank={rank}/{n},left={remaining})"

    # Blend by weight, then clamp: signals stack multiplicatively (a dead-rubber side also
    # resting for a European tie hits two), and no plausible motivation story justifies
    # moving a λ by more than motiv_clamp.
    clamp = float(getattr(cfg, "motiv_clamp", 0.15))
    def _finish(m: float) -> float:
        return round(min(1.0 + clamp, max(1.0 - clamp, 1.0 + (m - 1.0) * weight)), 4)

    mh, ma = _finish(mult[home_id]), _finish(mult[away_id])
    if mh == 1.0 and ma == 1.0:
        return 1.0, 1.0, None
    # conviction_side stays None: the WC version promoted its wounded-favourite signal into a
    # forced BET (DecisionConfig.motiv_conviction), justified by tournament examples. There is
    # no club evidence for a decision-level override, so this layer only ever tilts the price.
    return mh, ma, {"mult_home": mh, "mult_away": ma, "reason": reason, "conviction_side": None}
