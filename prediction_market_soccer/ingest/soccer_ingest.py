"""Soccer data ingestion orchestrator — API-Football → local store (club edition).

Forked from the WC module (plan 02 §3) and re-axed for the 12-competition club
registry (TRANSFORM_PLAN §3.1). Every sync keeps the WC discipline:
  * **watermark/TTL-gated** per (resource, comp) — a re-run within TTL costs 0 requests;
  * **idempotent** — upsert on natural keys;
  * **incremental** — only changed/finished fixtures pull their heavy detail;
  * **frugal in-play** — ONE ``/fixtures?live=all`` call covers every competition
    (filtered locally by league_id), instead of one call per league (§6.1).

Scopes (CLI):
  static   teams + fixtures + standings for every enabled comp   (~3 req/comp)
  results  refresh fixtures, pull detail for newly-finished       (hourly)
  live     all in-play fixtures across comps                      (during windows)
  h2h      head-to-head for upcoming fixtures                     (long TTL)
  squads   club squads (1 req/club, deliberate)
  form     per-club recent results incl. cups/Europe (sync_club_recent)
  all      static + results + h2h

Run: conda run -n someopark_run python -m prediction_market_soccer.ingest.soccer_ingest --scope static
"""
from __future__ import annotations

import argparse
import re
import time as _time
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Competition, Stage, active, by_api_id, stage_of
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.api_football import ApiFootball, BudgetExceededError

# Finished-fixture status codes (API-Football): FT, AET, PEN.
_FINISHED = {"FT", "AET", "PEN"}
# In-play status codes: 1H, HT, 2H, ET, BT, P, LIVE, INT, SUSP.
_LIVE = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP"}


def club_id_of(name: str) -> str:
    """Canonical club_id: normalized snake_case of the API-Football club name.

    Venue spellings map onto this via the per-comp alias tables (§3.6); this
    function is only for the PRIMARY (API-Football) axis.
    """
    s = (name or "").strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[''´`\.\-/]", " ", s)
    s = re.sub(r"\s+", "_", s.strip())
    return re.sub(r"[^a-z0-9_]", "", s)


# ── parsing helpers (unchanged envelope → store rows) ────────────────────────
def _fixture_row(item: dict) -> dict:
    fx, lg = item["fixture"], item["league"]
    teams, goals = item["teams"], item.get("goals", {})
    venue = fx.get("venue") or {}
    st = fx.get("status") or {}
    return {
        "api_id": fx["id"], "league_id": lg["id"], "season": lg["season"], "round": lg.get("round"),
        "kickoff_ts": fx.get("date"), "status_short": st.get("short"), "status_long": st.get("long"),
        "elapsed": st.get("elapsed"),
        "home_api_id": teams["home"]["id"], "away_api_id": teams["away"]["id"],
        "home_goals": goals.get("home"), "away_goals": goals.get("away"),
        "venue_name": venue.get("name"), "venue_city": venue.get("city"),
        "raw_json": store.json.dumps(item, ensure_ascii=False), "updated_at": store.utcnow(),
    }


def _team_stub_rows(item: dict) -> list[dict]:
    """Minimal CLUB rows discovered from a fixture (full info via sync_teams)."""
    rows = []
    for side in ("home", "away"):
        t = item["teams"][side]
        rows.append({
            "api_id": t["id"], "name": t.get("name"), "code": None, "country": None,
            "founded": None, "national": 0, "logo": t.get("logo"),
            "raw_json": store.json.dumps(t, ensure_ascii=False), "updated_at": store.utcnow(),
        })
    return rows


def _event_rows(fixture_id: int, events: list) -> list[dict]:
    rows = []
    for seq, e in enumerate(events):
        tm = e.get("time") or {}
        team = e.get("team") or {}
        player = e.get("player") or {}
        assist = e.get("assist") or {}
        rows.append({
            "fixture_api_id": fixture_id, "seq": seq,
            "minute": tm.get("elapsed"), "extra": tm.get("extra"),
            "team_api_id": team.get("id"), "player_api_id": player.get("id"),
            "assist_api_id": assist.get("id"),
            "type": e.get("type"), "detail": e.get("detail"), "comments": e.get("comments"),
            "raw_json": store.json.dumps(e, ensure_ascii=False),
        })
    return rows


def _upsert_club_registry(conn, comp: Competition, api_id: int, name: str, logo: str | None) -> str:
    cid = club_id_of(name)
    store.upsert(conn, "club_registry", {
        "club_id": cid, "comp": comp.key, "api_team_id": api_id, "name": name,
        "zh": None, "kalshi_code": None, "kalshi_name": None, "poly_code": None,
        "logo": logo, "valid_from": store.utcnow()[:10], "valid_to": None,
        "updated_at": store.utcnow(),
    }, pk=["club_id", "comp"])
    return cid


# ── syncs (all per-competition, watermark key = "<resource>:<comp>") ──────────
def sync_fixtures(api: ApiFootball, conn, comp: Competition, *, force: bool = False) -> int:
    wm = f"fixtures:{comp.key}"
    if not force and store.is_fresh(conn, wm, CONFIG.soccer.ttl_fixtures):
        print(f"[fixtures:{comp.key}] fresh — skipped (0 requests)")
        return 0
    items = api.fixtures(league=comp.api_football_id, season=comp.season)
    for it in items:
        store.upsert(conn, "fixture", _fixture_row(it), pk=["api_id"])
        for tr in _team_stub_rows(it):
            store.upsert(conn, "team", tr, pk=["api_id"])
    store.set_watermark(conn, wm, note=f"{len(items)} fixtures")
    conn.commit()
    rounds = sorted({(it["league"].get("round") or "") for it in items})
    unknown = [r for r in rounds if stage_of(comp.key, r) == Stage.UNKNOWN]
    print(f"[fixtures:{comp.key}] upserted {len(items)}; rounds={len(rounds)}"
          + (f"; ⚠ UNKNOWN stage rounds: {unknown}" if unknown else ""))
    return len(items)


def sync_teams(api: ApiFootball, conn, comp: Competition, *, force: bool = False) -> int:
    wm = f"teams:{comp.key}"
    if not force and store.is_fresh(conn, wm, CONFIG.soccer.ttl_static):
        print(f"[teams:{comp.key}] fresh — skipped (0 requests)")
        return 0
    items = api.teams(league=comp.api_football_id, season=comp.season)
    for it in items:
        t, v = it["team"], it.get("venue") or {}
        store.upsert(conn, "team", {
            "api_id": t["id"], "name": t.get("name"), "code": t.get("code"),
            "country": t.get("country"), "founded": t.get("founded"),
            "national": 1 if t.get("national") else 0, "logo": t.get("logo"),
            "raw_json": store.json.dumps(it, ensure_ascii=False), "updated_at": store.utcnow(),
        }, pk=["api_id"])
        cid = _upsert_club_registry(conn, comp, t["id"], t.get("name", ""), t.get("logo"))
        store.upsert(conn, "team_meta", {
            "api_id": t["id"], "group_code": None, "fifa_rank": None,
            "canonical_team_id": cid, "updated_at": store.utcnow(),
        }, pk=["api_id"])
    store.set_watermark(conn, wm, note=f"{len(items)} teams")
    conn.commit()
    print(f"[teams:{comp.key}] upserted {len(items)} clubs into team/club_registry")
    return len(items)


def sync_standings(api: ApiFootball, conn, comp: Competition, *, force: bool = False,
                   season: int | None = None) -> int:
    season = season or comp.season
    wm = f"standings:{comp.key}:{season}"
    if not force and store.is_fresh(conn, wm, CONFIG.soccer.ttl_standings):
        print(f"[standings:{comp.key}] fresh — skipped (0 requests)")
        return 0
    items = api.standings(league=comp.api_football_id, season=season)
    n = 0
    for it in items:
        groups = (it.get("league") or {}).get("standings") or []
        for group in groups:
            for entry in group:
                team = entry.get("team") or {}
                allrec = entry.get("all") or {}
                gc = entry.get("group")
                store.upsert(conn, "standing", {
                    "league_id": comp.api_football_id, "season": season, "group_code": gc,
                    "team_api_id": team.get("id"), "rank": entry.get("rank"),
                    "points": entry.get("points"), "goals_diff": entry.get("goalsDiff"),
                    "played": allrec.get("played"), "win": allrec.get("win"),
                    "draw": allrec.get("draw"), "lose": allrec.get("lose"),
                    "form": entry.get("form"), "raw_json": store.json.dumps(entry, ensure_ascii=False),
                    "updated_at": store.utcnow(),
                }, pk=["league_id", "season", "team_api_id"])
                n += 1
    store.set_watermark(conn, wm, note=f"{n} rows")
    conn.commit()
    print(f"[standings:{comp.key} s{season}] upserted {n} rows")
    return n


def _store_detailed(conn, item: dict) -> None:
    fid = item["fixture"]["id"]
    store.upsert(conn, "fixture", _fixture_row(item), pk=["api_id"])
    events = item.get("events") or []
    store.upsert_many(conn, "fixture_event", _event_rows(fid, events), pk=["fixture_api_id", "seq"])


def _our_league_ids() -> set[int]:
    return {c.api_football_id for c in active()}


def sync_results(api: ApiFootball, conn, *, force: bool = False) -> int:
    """Refresh fixtures for every enabled comp, then batch-pull detail for
    finished fixtures missing events (20 ids/request, events embedded)."""
    for comp in active():
        sync_fixtures(api, conn, comp, force=force)
    lids = _our_league_ids()
    # Detail (events/lineups/players) is only consumed by recent-window features
    # (form/signals/milestones/settlement); scores for model fits come from the
    # fixtures list itself. Scope the backfill to 14 days, newest first — the
    # full-season club universe is ~5,000 finished fixtures (WC had 104) and an
    # unscoped scan here would eat 2+ days of API budget (TRANSFORM_PLAN §6.1).
    # kickoff_ts is an ISO-8601 TEXT column — compare with an ISO string
    # (TEXT >= INTEGER is always true in SQLite and would void the filter).
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT api_id FROM fixture WHERE status_short IN ({}) "
        "AND league_id IN ({}) AND kickoff_ts >= ? "
        "AND api_id NOT IN (SELECT DISTINCT fixture_api_id FROM fixture_event) "
        "ORDER BY kickoff_ts DESC".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(lids))),
        tuple(_FINISHED) + tuple(lids) + (cutoff,)).fetchall()
    ids = [r["api_id"] for r in rows]
    if not ids:
        print("[results] no newly-finished fixtures (0 requests)")
        sync_ties(conn)
        return 0
    try:
        detailed = api.fixtures_by_ids(ids)
    except BudgetExceededError as e:
        print(f"[results] stopping early: {e}")
        return 0
    for item in detailed:
        _store_detailed(conn, item)
    conn.commit()
    sync_fixture_stats(api, conn, ids)
    sync_lineups(api, conn, ids)
    sync_fixture_players(api, conn, ids)
    sync_ties(conn)
    print(f"[results] pulled detail for {len(detailed)} newly-finished fixtures")
    return len(detailed)


def sync_live(api: ApiFootball, conn, *, skip_players: bool = False,
              skip_odds: bool = False) -> int:
    """All in-play fixtures across our 12 comps via ONE ``live=all`` call (§6.1).

    Per-fixture statistics refresh every poll; lineups once per fixture.
    ``skip_players``/``skip_odds`` come from the live_refresh budget governor —
    at high daily usage the heaviest per-fixture extras are shed first (only the
    lone-threat tactic degrades; 3-way pricing keeps full inputs)."""
    live_all = api.fixtures(live="all")
    lids = _our_league_ids()
    ours = [it for it in live_all if (it.get("league") or {}).get("id") in lids]
    if not ours:
        print("[live] no fixtures from our competitions in play")
        return 0
    ids = [it["fixture"]["id"] for it in ours]
    detailed = api.fixtures_by_ids(ids)
    for item in detailed:
        _store_detailed(conn, item)
    conn.commit()
    sync_fixture_stats(api, conn, ids)
    have_lineup = {r["fixture_api_id"] for r in conn.execute(
        "SELECT DISTINCT fixture_api_id FROM lineup WHERE fixture_api_id IN ({})".format(
            ",".join("?" * len(ids))), ids)} if ids else set()
    sync_lineups(api, conn, [i for i in ids if i not in have_lineup])
    if not skip_players:
        sync_fixture_players(api, conn, ids)
    if not skip_odds:
        sync_live_odds(api, conn, ids)
    print(f"[live] {len(detailed)} in play; players={'skip' if skip_players else 'ok'} "
          f"odds={'skip' if skip_odds else 'ok'}")
    return len(detailed)


def sync_ties(conn) -> int:
    """Derive/refresh two-legged ties: pair the two fixtures of a CUP_TWO_LEG round
    sharing the same unordered team pair; compute the aggregate from finished legs.

    Leg order = kickoff order. Idempotent; costs 0 API requests (§3.0 tie layer)."""
    n = 0
    for comp in active():
        rows = conn.execute(
            "SELECT api_id, round, kickoff_ts, home_api_id, away_api_id, home_goals, away_goals, "
            "status_short FROM fixture WHERE league_id=? AND season=? ORDER BY kickoff_ts",
            (comp.api_football_id, comp.season)).fetchall()
        by_pair: dict[tuple, list] = {}
        for r in rows:
            if stage_of(comp.key, r["round"]) != Stage.CUP_TWO_LEG:
                continue
            key = (r["round"], frozenset((r["home_api_id"], r["away_api_id"])))
            by_pair.setdefault(key, []).append(r)
        for (rnd, pair), legs in by_pair.items():
            if len(legs) != 2:
                continue  # second leg not yet scheduled/published
            l1, l2 = legs[0], legs[1]
            a, b = l1["home_api_id"], l1["away_api_id"]  # tie sides = leg-1 home/away
            def goals_for(team, leg):
                if leg["home_goals"] is None:
                    return None
                return leg["home_goals"] if leg["home_api_id"] == team else leg["away_goals"]
            agg_a = agg_b = None
            g1a, g1b = goals_for(a, l1), goals_for(b, l1)
            g2a, g2b = goals_for(a, l2), goals_for(b, l2)
            if g1a is not None:
                agg_a, agg_b = g1a, g1b
                if g2a is not None and l2["status_short"] in _FINISHED | _LIVE:
                    agg_a, agg_b = agg_a + g2a, agg_b + g2b
            decided = 1 if (l2["status_short"] in _FINISHED) else 0
            tie_key = f"{comp.key}:{rnd}:{min(a,b)}-{max(a,b)}"
            store.upsert(conn, "tie", {
                "tie_key": tie_key, "comp": comp.key, "round": rnd,
                "leg1_fixture_id": l1["api_id"], "leg2_fixture_id": l2["api_id"],
                "team_a_api_id": a, "team_b_api_id": b,
                "agg_a": agg_a, "agg_b": agg_b, "decided": decided,
                "updated_at": store.utcnow(),
            }, pk=["tie_key"])
            n += 1
    conn.commit()
    if n:
        print(f"[ties] derived/refreshed {n} two-legged ties")
    return n


def leg_of(conn, fixture_api_id: int) -> tuple[int | None, str | None]:
    """(leg, 'aggA-aggB') for a fixture inside a two-legged tie, else (None, None).

    ``agg`` is the RUNNING aggregate: once leg 2 is live or finished its goals are
    already inside it. That is what a results view wants, and it is exactly wrong for
    a LIVE model, which is separately told the current leg-2 score — adding this on
    top counts leg-2 goals twice. Live callers want :func:`carry_of` instead."""
    r = conn.execute("SELECT * FROM tie WHERE leg1_fixture_id=? OR leg2_fixture_id=?",
                     (fixture_api_id, fixture_api_id)).fetchone()
    if not r:
        return None, None
    leg = 1 if r["leg1_fixture_id"] == fixture_api_id else 2
    agg = None
    if r["agg_a"] is not None:
        agg = f"{r['agg_a']}-{r['agg_b']}"
    return leg, agg


def carry_of(conn, fixture_api_id: int) -> tuple[int | None, str | None]:
    """(leg, 'leg1A-leg1B') — the FIRST-LEG-ONLY score a live leg-2 model should carry.

    Read straight off leg 1 rather than off the stored aggregate, so it cannot drift
    into double-counting when leg 2 kicks off."""
    r = conn.execute("SELECT * FROM tie WHERE leg1_fixture_id=? OR leg2_fixture_id=?",
                     (fixture_api_id, fixture_api_id)).fetchone()
    if not r:
        return None, None
    leg = 1 if r["leg1_fixture_id"] == fixture_api_id else 2
    l1 = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, status_short "
        "FROM fixture WHERE api_id=?", (r["leg1_fixture_id"],)).fetchone()
    if not l1 or l1["home_goals"] is None:
        return leg, None
    # tie.team_a is the leg-1 HOME side; express the carry in that same a-b order.
    a_is_home = l1["home_api_id"] == r["team_a_api_id"]
    ga_, gb_ = ((l1["home_goals"], l1["away_goals"]) if a_is_home
                else (l1["away_goals"], l1["home_goals"]))
    return leg, f"{ga_}-{gb_}"


def project_results_to_club_recent(conn) -> int:
    """Project finished fixtures (already in `fixture`) into `nt_recent` (club-recent
    semantics) so the form feature reflects results IMMEDIATELY at 0 API cost.
    Cross-competition results for our clubs arrive via sync_club_recent (weekly)."""
    lids = _our_league_ids()
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, kickoff_ts, league_id, home_goals, away_goals "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({})".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(lids))),
        tuple(_FINISHED) + tuple(lids)).fetchall()
    n = 0
    for r in rows:
        for tid, opp, gf, ga, is_home in (
            (r["home_api_id"], r["away_api_id"], r["home_goals"], r["away_goals"], 1),
            (r["away_api_id"], r["home_api_id"], r["away_goals"], r["home_goals"], 0),
        ):
            store.upsert(conn, "nt_recent", {
                "fixture_api_id": r["api_id"], "team_api_id": tid, "opp_api_id": opp,
                "kickoff_ts": r["kickoff_ts"], "league_id": r["league_id"], "is_friendly": 0,
                "gf": gf, "ga": ga, "is_home": is_home, "fetched_at": store.utcnow(),
            }, pk=["fixture_api_id", "team_api_id"])
            n += 1
    conn.commit()
    return n


# Back-compat alias for copied callers (refresh_all/live_refresh) until Phase 4 rewires.
project_wc_results_to_nt_recent = project_results_to_club_recent


def sync_club_recent(api: ApiFootball, conn, *, last: int = 8, limit: int = 400) -> int:
    """Per-club recent results across ALL competitions (cups/Europe included) for
    the form feature. ~1 request per club; weekly cadence (refresh --with-form)."""
    teams = conn.execute(
        "SELECT DISTINCT api_team_id AS api_id FROM club_registry "
        "WHERE comp IN ({}) ORDER BY api_id LIMIT ?".format(
            ",".join("?" * len(active()))),
        tuple(c.key for c in active()) + (limit,)).fetchall()
    pulled = 0
    for trow in teams:
        tid = trow["api_id"]
        try:
            fx = api.fixtures(team=tid, last=last)
        except BudgetExceededError as e:
            print(f"[club_recent] stopping early: {e}")
            break
        for it in fx:
            fi = it.get("fixture", {}); lg = it.get("league", {})
            tm = it.get("teams", {}); go = it.get("goals", {})
            status = (fi.get("status") or {}).get("short")
            if status not in _FINISHED:
                continue
            hid = (tm.get("home") or {}).get("id"); aid = (tm.get("away") or {}).get("id")
            gh, ga = go.get("home"), go.get("away")
            if hid is None or aid is None or gh is None or ga is None:
                continue
            is_home = 1 if hid == tid else 0
            opp = aid if is_home else hid
            gf, gag = (gh, ga) if is_home else (ga, gh)
            lname = (lg.get("name") or "").lower()
            is_friendly = 1 if "friendl" in lname else 0
            store.upsert(conn, "nt_recent", {
                "fixture_api_id": fi.get("id"), "team_api_id": tid, "opp_api_id": opp,
                "kickoff_ts": fi.get("date"), "league_id": lg.get("id"), "is_friendly": is_friendly,
                "gf": gf, "ga": gag, "is_home": is_home, "fetched_at": store.utcnow(),
            }, pk=["fixture_api_id", "team_api_id"])
        pulled += 1
    conn.commit()
    print(f"[club_recent] pulled recent results for {pulled} clubs")
    return pulled


# Back-compat alias (WC name) until Phase 4 rewires callers.
sync_nt_recent = sync_club_recent


def sync_h2h(api: ApiFootball, conn, *, force: bool = False, days_ahead: int = 10) -> int:
    """Head-to-head history for upcoming fixtures within the next N days (clubs
    have year-round calendars — the WC 'all not-finished' scan would be 3,000+ pairs)."""
    if not force and store.is_fresh(conn, "h2h", CONFIG.soccer.ttl_h2h):
        print("[h2h] fresh — skipped (0 requests)")
        return 0
    lids = _our_league_ids()
    pairs = conn.execute(
        "SELECT DISTINCT home_api_id, away_api_id FROM fixture "
        "WHERE status_short NOT IN ({}) AND league_id IN ({}) "
        "AND kickoff_ts <= datetime('now', ?)".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(lids))),
        tuple(_FINISHED) + tuple(lids) + (f"+{days_ahead} days",)).fetchall()
    pulled = 0
    for p in pairs:
        key = f"{p['home_api_id']}-{p['away_api_id']}"
        try:
            hist = api.head_to_head(key, last=10)
        except BudgetExceededError as e:
            print(f"[h2h] stopping early: {e}")
            break
        for it in hist:
            fx = it["fixture"]; teams = it["teams"]; goals = it.get("goals", {})
            store.upsert(conn, "h2h", {
                "pair_key": key, "fixture_api_id": fx["id"], "kickoff_ts": fx.get("date"),
                "home_api_id": teams["home"]["id"], "away_api_id": teams["away"]["id"],
                "home_goals": goals.get("home"), "away_goals": goals.get("away"),
                "raw_json": store.json.dumps(it, ensure_ascii=False), "updated_at": store.utcnow(),
            }, pk=["pair_key", "fixture_api_id"])
        conn.commit()
        pulled += 1
    store.set_watermark(conn, "h2h", note=f"{pulled} pairs")
    print(f"[h2h] pulled head-to-head for {pulled} upcoming pairings")
    return pulled


def _pct(s) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace("%", "").strip()) / 100.0
    except ValueError:
        return None


def _stat(stats: list, key: str):
    for s in stats:
        if s.get("type") == key:
            return s.get("value")
    return None


def sync_fixture_stats(api: ApiFootball, conn, fixture_ids: list[int]) -> int:
    pulled = 0
    for fid in fixture_ids:
        try:
            res = api.get("fixtures/statistics", {"fixture": fid}, paginate=False)
        except BudgetExceededError as e:
            print(f"[fixture_stats] stopping early: {e}")
            break
        for block in res:
            st = block.get("statistics") or []
            team = block.get("team") or {}
            poss = _stat(st, "Ball Possession")
            store.upsert(conn, "fixture_stats", {
                "fixture_api_id": fid, "team_api_id": team.get("id"),
                "xg": float(_stat(st, "expected_goals")) if _stat(st, "expected_goals") else None,
                "goals_prevented": float(_stat(st, "goals_prevented")) if _stat(st, "goals_prevented") else None,
                "shots_total": _stat(st, "Total Shots"), "shots_on": _stat(st, "Shots on Goal"),
                "possession": float(str(poss).replace("%", "")) / 100 if poss else None,
                "corners": _stat(st, "Corner Kicks"),
                "raw_json": store.json.dumps(block, ensure_ascii=False), "fetched_at": store.utcnow(),
            }, pk=["fixture_api_id", "team_api_id"])
        pulled += 1
    conn.commit()
    print(f"[fixture_stats] pulled stats (incl. xG) for {pulled} fixtures")
    return pulled


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace("%", "").strip())
    except ValueError:
        return None


def sync_lineups(api: ApiFootball, conn, fixture_ids: list[int]) -> int:
    pulled = 0
    for fid in fixture_ids:
        try:
            res = api.lineups(fid)
        except BudgetExceededError as e:
            print(f"[lineups] stopping early: {e}")
            break
        for block in res:
            team = block.get("team") or {}
            coach = block.get("coach") or {}
            store.upsert(conn, "lineup", {
                "fixture_api_id": fid, "team_api_id": team.get("id"),
                "formation": block.get("formation"), "coach": coach.get("name"),
                "raw_json": store.json.dumps(block, ensure_ascii=False), "fetched_at": store.utcnow(),
            }, pk=["fixture_api_id", "team_api_id"])
        if res:
            pulled += 1
    conn.commit()
    print(f"[lineups] pulled formations for {pulled} fixtures")
    return pulled


def sync_fixture_players(api: ApiFootball, conn, fixture_ids: list[int]) -> int:
    pulled = 0
    for fid in fixture_ids:
        try:
            res = api.fixture_players(fid)
        except BudgetExceededError as e:
            print(f"[fixture_players] stopping early: {e}")
            break
        n_rows = 0
        for block in res:
            team = block.get("team") or {}
            for p in block.get("players") or []:
                player = p.get("player") or {}
                st = (p.get("statistics") or [{}])[0]
                games = st.get("games") or {}
                shots = st.get("shots") or {}
                goals = st.get("goals") or {}
                passes = st.get("passes") or {}
                dribbles = st.get("dribbles") or {}
                duels = st.get("duels") or {}
                tackles = st.get("tackles") or {}
                fouls = st.get("fouls") or {}
                store.upsert(conn, "fixture_player_stats", {
                    "fixture_api_id": fid, "team_api_id": team.get("id"),
                    "player_api_id": player.get("id"), "player_name": player.get("name"),
                    "position": games.get("position"),
                    "is_starter": 0 if games.get("substitute") else 1,
                    "captain": 1 if games.get("captain") else 0,
                    "minutes": games.get("minutes"), "rating": _num(games.get("rating")),
                    "shots_total": shots.get("total"), "shots_on": shots.get("on"),
                    "goals": goals.get("total"), "assists": goals.get("assists"),
                    "passes_total": passes.get("total"), "passes_key": passes.get("key"),
                    "pass_accuracy": _num(passes.get("accuracy")),
                    "dribbles_attempts": dribbles.get("attempts"), "dribbles_success": dribbles.get("success"),
                    "duels_total": duels.get("total"), "duels_won": duels.get("won"),
                    "tackles": tackles.get("total"), "interceptions": tackles.get("interceptions"),
                    "fouls_drawn": fouls.get("drawn"), "fouls_committed": fouls.get("committed"),
                    "offsides": st.get("offsides"),
                    "raw_json": store.json.dumps(p, ensure_ascii=False), "fetched_at": store.utcnow(),
                }, pk=["fixture_api_id", "player_api_id"])
                n_rows += 1
        if n_rows:
            pulled += 1
    conn.commit()
    print(f"[fixture_players] pulled per-player match stats for {pulled} fixtures")
    return pulled


def sync_injuries(api: ApiFootball, conn, comp: Competition, *, force: bool = False) -> int:
    wm = f"injury:{comp.key}"
    if not force and store.is_fresh(conn, wm, CONFIG.soccer.ttl_injuries * 24):
        print(f"[injury:{comp.key}] fresh — skipped (0 requests)")
        return 0
    try:
        res = api.injuries(league=comp.api_football_id, season=comp.season)
    except BudgetExceededError as e:
        print(f"[injury] stopping early: {e}")
        return 0
    n = 0
    for it in res:
        fx = it.get("fixture") or {}
        team = it.get("team") or {}
        player = it.get("player") or {}
        store.upsert(conn, "injury", {
            "fixture_api_id": fx.get("id"), "team_api_id": team.get("id"),
            "player_api_id": player.get("id"),
            "type": player.get("type"), "reason": player.get("reason"),
            "raw_json": store.json.dumps(it, ensure_ascii=False), "fetched_at": store.utcnow(),
        }, pk=["fixture_api_id", "player_api_id"])
        n += 1
    store.set_watermark(conn, wm, note=f"{n} injury rows")
    conn.commit()
    print(f"[injury:{comp.key}] pulled {n} rows")
    return n


def sync_predictions(api: ApiFootball, conn, *, limit: int = 5, force: bool = False) -> int:
    lids = _our_league_ids()
    rows = conn.execute(
        "SELECT api_id FROM fixture WHERE status_short = 'NS' AND league_id IN ({}) "
        "AND api_id NOT IN (SELECT fixture_api_id FROM prediction) "
        "ORDER BY kickoff_ts LIMIT ?".format(",".join("?" * len(lids))),
        tuple(lids) + (limit,)).fetchall()
    pulled = stored = 0
    for r in rows:
        fid = r["api_id"]
        try:
            res = api.predictions(fid)
        except BudgetExceededError as e:
            print(f"[predictions] stopping early: {e}")
            break
        if not res:
            continue
        pr = res[0].get("predictions", {})
        pct = pr.get("percent", {})
        winner = (pr.get("winner") or {}).get("name")
        store.upsert(conn, "prediction", {
            "fixture_api_id": fid, "p_home": _pct(pct.get("home")),
            "p_draw": _pct(pct.get("draw")), "p_away": _pct(pct.get("away")),
            "advice": pr.get("advice"), "winner_name": winner,
            "raw_json": store.json.dumps(res[0], ensure_ascii=False), "fetched_at": store.utcnow(),
        }, pk=["fixture_api_id"])
        pulled += 1
    conn.commit()
    print(f"[predictions] pulled {pulled} upcoming-fixture predictions")
    return pulled


def sync_odds(api: ApiFootball, conn, *, limit: int = 30, force: bool = False,
              include_settled: bool = True) -> int:
    statuses = "('NS','FT','AET','PEN')" if include_settled else "('NS')"
    lids = _our_league_ids()
    rows = conn.execute(
        f"SELECT api_id FROM fixture WHERE status_short IN {statuses} "
        "AND league_id IN ({}) "
        # A fixture counts as "has odds" only if a REAL bookmaker row exists: the
        # in-play `live_consensus` row (written by the live loop) would otherwise
        # permanently block the pre-match odds pull for that fixture — and it is
        # useless as a pre-match reference (it was captured at 4-1 up).
        "AND api_id NOT IN (SELECT fixture_api_id FROM match_odds "
        "                   WHERE bookmaker <> 'live_consensus') "
        "ORDER BY kickoff_ts LIMIT ?".format(",".join("?" * len(lids))),
        tuple(lids) + (limit,)).fetchall()
    pulled = stored = 0
    for r in rows:
        fid = r["api_id"]
        try:
            res = api.odds(fid)
        except BudgetExceededError as e:
            print(f"[odds] stopping early: {e}")
            break
        for entry in res:
            for bk in entry.get("bookmakers", []):
                mw = next((b for b in bk.get("bets", []) if b.get("name") == "Match Winner"), None)
                if not mw:
                    continue
                vals = {v["value"]: float(v["odd"]) for v in mw.get("values", []) if v.get("odd")}
                if not {"Home", "Draw", "Away"} <= set(vals):
                    continue
                inv = {k: 1.0 / vals[k] for k in ("Home", "Draw", "Away")}
                s = sum(inv.values())
                store.upsert(conn, "match_odds", {
                    "fixture_api_id": fid, "bookmaker": bk.get("name"),
                    "p_home": inv["Home"] / s, "p_draw": inv["Draw"] / s, "p_away": inv["Away"] / s,
                    "overround": s - 1.0, "raw_json": store.json.dumps(mw, ensure_ascii=False),
                    "fetched_at": store.utcnow(),
                }, pk=["fixture_api_id", "bookmaker"])
                stored += 1
        pulled += 1
    conn.commit()
    # Count what was actually STORED, not just queried: API-Football answers
    # results=0 (no error) for club fixtures outside its odds coverage, and the
    # old message reported those as "pulled odds", hiding an empty book column.
    print(f"[odds] queried {pulled} fixtures → stored {stored} bookmaker rows"
          + ("  (no pre-match odds coverage for these fixtures)" if not stored else ""))
    return stored


def sync_live_odds(api: ApiFootball, conn, fixture_ids: list[int] | None = None) -> int:
    """In-play bookmaker 1X2 odds via ONE un-filtered /odds/live call (§6.1),
    filtered locally to our fixtures."""
    try:
        res = api.odds_live()
    except BudgetExceededError as e:
        print(f"[live_odds] stopping early: {e}")
        return 0
    except Exception as e:
        print(f"[live_odds] skipped ({type(e).__name__}: {e})")
        return 0
    want = set(fixture_ids or [])
    pulled = 0
    for entry in res:
        fid = (entry.get("fixture") or {}).get("id")
        if fid is None or (want and fid not in want):
            continue
        probs = []
        for od in entry.get("odds", []):
            name = (od.get("name") or "")
            if name not in ("Match Winner", "Fulltime Result", "1X2"):
                continue
            vals = {}
            for v in od.get("values", []):
                key = str(v.get("value"))
                odd = v.get("odd")
                if odd in (None, "", "0"):
                    continue
                key = {"1": "Home", "X": "Draw", "2": "Away"}.get(key, key)
                try:
                    vals[key] = float(odd)
                except (TypeError, ValueError):
                    continue
            if {"Home", "Draw", "Away"} <= set(vals):
                inv = {k: 1.0 / vals[k] for k in ("Home", "Draw", "Away")}
                s = sum(inv.values())
                probs.append((inv["Home"] / s, inv["Draw"] / s, inv["Away"] / s, s - 1.0))
        if not probs:
            continue
        ph = sum(p[0] for p in probs) / len(probs)
        pd = sum(p[1] for p in probs) / len(probs)
        pa = sum(p[2] for p in probs) / len(probs)
        ov = sum(p[3] for p in probs) / len(probs)
        store.upsert(conn, "match_odds", {
            "fixture_api_id": fid, "bookmaker": "live_consensus",
            "p_home": ph, "p_draw": pd, "p_away": pa, "overround": ov,
            "raw_json": store.json.dumps(entry, ensure_ascii=False)[:20000], "fetched_at": store.utcnow(),
        }, pk=["fixture_api_id", "bookmaker"])
        pulled += 1
    conn.commit()
    print(f"[live_odds] refreshed in-play 1X2 consensus for {pulled} fixtures")
    return pulled


def sync_topscorers(api: ApiFootball, conn, comp: Competition, *, force: bool = False,
                    season: int | None = None) -> int:
    season = season or comp.season
    wm = f"topscorers:{comp.key}:{season}"
    if not force and store.is_fresh(conn, wm, CONFIG.soccer.ttl_results * 24):
        print(f"[topscorers:{comp.key}] fresh — skipped (0 requests)")
        return 0
    items = api.topscorers(league=comp.api_football_id, season=season)
    n = 0
    for it in items:
        pl = it["player"]
        st = (it.get("statistics") or [{}])[0]
        games, goals = st.get("games") or {}, st.get("goals") or {}
        shots, pen = st.get("shots") or {}, st.get("penalty") or {}
        team = st.get("team") or {}
        store.upsert(conn, "player", {
            "api_id": pl["id"], "name": pl.get("name"), "firstname": pl.get("firstname"),
            "lastname": pl.get("lastname"), "age": pl.get("age"), "nationality": pl.get("nationality"),
            "position": games.get("position"), "photo": pl.get("photo"),
            "raw_json": store.json.dumps(pl, ensure_ascii=False), "updated_at": store.utcnow(),
        }, pk=["api_id"])
        store.upsert(conn, "player_stat", {
            "player_api_id": pl["id"], "league_id": comp.api_football_id, "season": season,
            "team_api_id": team.get("id"), "appearances": games.get("appearences"),
            "minutes": games.get("minutes"), "goals": goals.get("total"),
            "assists": goals.get("assists"), "shots_total": shots.get("total"),
            "shots_on": shots.get("on"), "penalty_scored": pen.get("scored"),
            "rating": float(games["rating"]) if games.get("rating") else None,
            "raw_json": store.json.dumps(it, ensure_ascii=False), "updated_at": store.utcnow(),
        }, pk=["player_api_id", "league_id", "season"])
        n += 1
    store.set_watermark(conn, wm, note=f"{n} scorers")
    conn.commit()
    print(f"[topscorers:{comp.key} s{season}] upserted {n} scorers")
    return n


def sync_squads(api: ApiFootball, conn, *, force: bool = False, limit: int = 400) -> int:
    """Club squads (1 request/club). Club list comes from club_registry — the WC
    version's ``WHERE national=1`` would select ZERO clubs (plan §2.2 trap)."""
    if not force and store.is_fresh(conn, "squads", CONFIG.soccer.ttl_static):
        print("[squads] fresh — skipped (0 requests)")
        return 0
    team_ids = [r["api_id"] for r in conn.execute(
        "SELECT DISTINCT api_team_id AS api_id FROM club_registry ORDER BY api_id LIMIT ?",
        (limit,)).fetchall()]
    pulled = 0
    for tid in team_ids:
        try:
            res = api.squads(tid)
        except BudgetExceededError as e:
            print(f"[squads] stopping early: {e}")
            break
        for grp in res:
            for pl in grp.get("players", []):
                store.upsert(conn, "player", {
                    "api_id": pl["id"], "name": pl.get("name"), "firstname": None, "lastname": None,
                    "age": pl.get("age"), "nationality": None, "position": pl.get("position"),
                    "photo": pl.get("photo"), "raw_json": store.json.dumps(pl, ensure_ascii=False),
                    "updated_at": store.utcnow(),
                }, pk=["api_id"])
                store.upsert(conn, "squad", {
                    "team_api_id": tid, "player_api_id": pl["id"], "season": CONFIG.soccer.season,
                    "position": pl.get("position"), "number": pl.get("number"),
                    "updated_at": store.utcnow(),
                }, pk=["team_api_id", "player_api_id", "season"])
        conn.commit()
        pulled += 1
    store.set_watermark(conn, "squads", note=f"{pulled} clubs")
    print(f"[squads] pulled squads for {pulled} clubs")
    return pulled


def run(scope: str, *, force: bool = False) -> None:
    conn = store.init_db()
    api = ApiFootball(conn)
    used_before = api.requests_used_this_month()
    try:
        if scope in ("static", "all"):
            for comp in active():
                sync_teams(api, conn, comp, force=force)
                sync_fixtures(api, conn, comp, force=force)
                sync_standings(api, conn, comp, force=force)
            sync_ties(conn)
        if scope in ("results", "all"):
            sync_results(api, conn, force=force)
            project_results_to_club_recent(conn)
        if scope in ("topscorers", "results", "all"):
            for comp in active():
                sync_topscorers(api, conn, comp, force=force)
        if scope in ("h2h", "all"):
            sync_h2h(api, conn, force=force)
        if scope in ("predictions",):
            sync_predictions(api, conn, force=force)
        if scope in ("odds", "all"):
            sync_odds(api, conn, force=force)
        if scope == "live":
            sync_live(api, conn)
        if scope == "squads":
            sync_squads(api, conn, force=force)
        if scope == "form":
            sync_club_recent(api, conn)
    except BudgetExceededError as e:
        print(f"[budget] {e}")
    used_after = api.requests_used_this_month()
    print(f"\nrequests used this run: {used_after - used_before} "
          f"| month total: {used_after}/{CONFIG.soccer.monthly_budget}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Club soccer data ingestion (API-Football, 12-comp registry)")
    ap.add_argument("--scope", default="static",
                    choices=["static", "results", "live", "h2h", "squads", "topscorers",
                             "predictions", "odds", "form", "all"])
    ap.add_argument("--force", action="store_true", help="ignore TTL and re-pull")
    args = ap.parse_args()
    run(args.scope, force=args.force)


if __name__ == "__main__":
    main()
