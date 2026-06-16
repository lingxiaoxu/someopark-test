"""Soccer data ingestion orchestrator (plan 02 §3) — API-Football → local store.

Decides WHETHER to call the API (the client decides HOW). Every sync is:
  * **watermark/TTL-gated** — a re-run within the resource's TTL makes ZERO API
    calls (the user's "pull once, never re-pull" constraint);
  * **idempotent** — upsert on natural keys, so re-runs never duplicate rows;
  * **incremental** — only changed/finished fixtures pull their events/lineups;
  * **coverage-aware** — skips endpoints the season doesn't cover (e.g. injuries
    is False for WC 2026) instead of burning requests on guaranteed-empty calls.

Scopes (CLI):
  static   teams + fixtures + standings              (≈3 requests, run once / TTL days)
  results  refresh fixtures, pull events for newly-finished matches   (hourly)
  live     in-play fixtures + their events            (only while matches running)
  h2h      head-to-head history for upcoming fixtures (rarely; long TTL)
  squads   national-team squads (48 requests)         (deliberate, once)
  all      static + results + h2h

Run: conda run -n someopark_run python -m prediction_market.ingest.soccer_ingest --scope static
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from prediction_market.config import CONFIG
from prediction_market.ingest import store
from prediction_market.ingest.api_football import ApiFootball, BudgetExceededError
from prediction_market.ingest.prior_ingest import canonical_team_name, load_prior, team_id

LEAGUE = CONFIG.soccer.league_id
SEASON = CONFIG.soccer.season

# Finished-fixture status codes (API-Football): FT, AET, PEN.
_FINISHED = {"FT", "AET", "PEN"}
# In-play status codes: 1H, HT, 2H, ET, BT, P, LIVE, INT, SUSP.
_LIVE = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP"}


def _canonical_id_for(name: str) -> str | None:
    """Map an API-Football team name to our prior canonical team_id (or None)."""
    prior_ids = {t.team_id for t in load_prior().teams}
    tid = team_id(canonical_team_name(name))
    return tid if tid in prior_ids else None


# ── parsing helpers (API-Football envelope → store rows) ─────────────────────
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
    """Minimal team rows discovered from a fixture (full info via sync_teams)."""
    rows = []
    for side in ("home", "away"):
        t = item["teams"][side]
        rows.append({
            "api_id": t["id"], "name": t.get("name"), "code": None, "country": None,
            "founded": None, "national": 1, "logo": t.get("logo"),
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


# ── syncs ─────────────────────────────────────────────────────────────────────
def sync_fixtures(api: ApiFootball, conn, *, force: bool = False) -> int:
    if not force and store.is_fresh(conn, "fixtures", CONFIG.soccer.ttl_fixtures):
        print("[fixtures] fresh — skipped (0 requests)")
        return 0
    items = api.fixtures(league=LEAGUE, season=SEASON)
    for it in items:
        store.upsert(conn, "fixture", _fixture_row(it), pk=["api_id"])
        for tr in _team_stub_rows(it):
            store.upsert(conn, "team", tr, pk=["api_id"])
    store.set_watermark(conn, "fixtures", note=f"{len(items)} fixtures")
    conn.commit()
    rounds = sorted({it["league"].get("round") for it in items})
    print(f"[fixtures] upserted {len(items)} fixtures; rounds: {rounds}")
    return len(items)


def sync_teams(api: ApiFootball, conn, *, force: bool = False) -> int:
    if not force and store.is_fresh(conn, "teams", CONFIG.soccer.ttl_static):
        print("[teams] fresh — skipped (0 requests)")
        return 0
    items = api.teams(league=LEAGUE, season=SEASON)
    for it in items:
        t, v = it["team"], it.get("venue") or {}
        store.upsert(conn, "team", {
            "api_id": t["id"], "name": t.get("name"), "code": t.get("code"),
            "country": t.get("country"), "founded": t.get("founded"),
            "national": 1 if t.get("national") else 0, "logo": t.get("logo"),
            "raw_json": store.json.dumps(it, ensure_ascii=False), "updated_at": store.utcnow(),
        }, pk=["api_id"])
        cid = _canonical_id_for(t.get("name", ""))
        store.upsert(conn, "team_meta", {
            "api_id": t["id"], "group_code": None, "fifa_rank": None,
            "canonical_team_id": cid, "updated_at": store.utcnow(),
        }, pk=["api_id"])
    store.set_watermark(conn, "teams", note=f"{len(items)} teams")
    conn.commit()
    mapped = conn.execute("SELECT COUNT(*) n FROM team_meta WHERE canonical_team_id IS NOT NULL").fetchone()["n"]
    print(f"[teams] upserted {len(items)} teams; {mapped} mapped to prior canonical ids")
    return len(items)


def sync_standings(api: ApiFootball, conn, *, force: bool = False) -> int:
    if not force and store.is_fresh(conn, "standings", CONFIG.soccer.ttl_standings):
        print("[standings] fresh — skipped (0 requests)")
        return 0
    items = api.standings(league=LEAGUE, season=SEASON)
    n = 0
    for it in items:
        groups = (it.get("league") or {}).get("standings") or []
        for group in groups:
            for entry in group:
                team = entry.get("team") or {}
                allrec = entry.get("all") or {}
                gc = entry.get("group")
                store.upsert(conn, "standing", {
                    "league_id": LEAGUE, "season": SEASON, "group_code": gc,
                    "team_api_id": team.get("id"), "rank": entry.get("rank"),
                    "points": entry.get("points"), "goals_diff": entry.get("goalsDiff"),
                    "played": allrec.get("played"), "win": allrec.get("win"),
                    "draw": allrec.get("draw"), "lose": allrec.get("lose"),
                    "form": entry.get("form"), "raw_json": store.json.dumps(entry, ensure_ascii=False),
                    "updated_at": store.utcnow(),
                }, pk=["league_id", "season", "team_api_id"])
                # Backfill group_code onto team_meta.
                store.upsert(conn, "team_meta", {
                    "api_id": team.get("id"), "group_code": gc, "fifa_rank": None,
                    "canonical_team_id": _canonical_id_for(team.get("name", "")),
                    "updated_at": store.utcnow(),
                }, pk=["api_id"])
                n += 1
    store.set_watermark(conn, "standings", note=f"{n} rows")
    conn.commit()
    print(f"[standings] upserted {n} standing rows")
    return n


def _store_detailed(conn, item: dict) -> None:
    """Upsert a fixture item that carries EMBEDDED events (from /fixtures?ids=)."""
    fid = item["fixture"]["id"]
    store.upsert(conn, "fixture", _fixture_row(item), pk=["api_id"])
    events = item.get("events") or []
    store.upsert_many(conn, "fixture_event", _event_rows(fid, events), pk=["fixture_api_id", "seq"])


def sync_results(api: ApiFootball, conn, *, force: bool = False) -> int:
    """Refresh fixtures, then batch-pull events for finished fixtures missing them.

    Frugal + incremental: finished fixtures without stored events are fetched in
    batches of 20 via /fixtures?ids= (events embedded), so the whole group stage
    costs ~4 requests, and a re-run with nothing new costs only the fixture-list
    refresh.
    """
    sync_fixtures(api, conn, force=force)
    rows = conn.execute(
        "SELECT api_id FROM fixture WHERE status_short IN ({}) "
        "AND api_id NOT IN (SELECT DISTINCT fixture_api_id FROM fixture_event)".format(
            ",".join("?" * len(_FINISHED))), tuple(_FINISHED)).fetchall()
    ids = [r["api_id"] for r in rows]
    if not ids:
        print("[results] no newly-finished fixtures (0 requests)")
        return 0
    try:
        detailed = api.fixtures_by_ids(ids)
    except BudgetExceededError as e:
        print(f"[results] stopping early: {e}")
        return 0
    for item in detailed:
        _store_detailed(conn, item)
    conn.commit()
    print(f"[results] pulled events for {len(detailed)} newly-finished fixtures "
          f"(batched from {len(ids)} ids)")
    return len(detailed)


def sync_live(api: ApiFootball, conn) -> int:
    """In-play fixtures + their live events (call only during match windows).

    Two requests max per poll: one status-filtered fixtures call to find live
    matches, one batched /fixtures?ids= to refresh their embedded events.
    """
    status = "-".join(sorted(_LIVE))
    live = api.fixtures(league=LEAGUE, season=SEASON, status=status)
    if not live:
        print("[live] no World Cup matches in play")
        return 0
    ids = [it["fixture"]["id"] for it in live]
    detailed = api.fixtures_by_ids(ids)
    for item in detailed:
        _store_detailed(conn, item)
    conn.commit()
    # Also capture live team stats (xG, shots) for the momentum tactic (plan 03 §4b).
    sync_fixture_stats(api, conn, ids)
    print(f"[live] {len(detailed)} fixture(s) in play, events + xG refreshed")
    return len(detailed)


def sync_h2h(api: ApiFootball, conn, *, force: bool = False) -> int:
    """Head-to-head history for upcoming (not-yet-finished) fixtures."""
    if not force and store.is_fresh(conn, "h2h", CONFIG.soccer.ttl_h2h):
        print("[h2h] fresh — skipped (0 requests)")
        return 0
    pairs = conn.execute(
        "SELECT DISTINCT home_api_id, away_api_id FROM fixture "
        "WHERE status_short NOT IN ({})".format(",".join("?" * len(_FINISHED))), tuple(_FINISHED)).fetchall()
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
    """Parse an API-Football percent string like '45%' → 0.45."""
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
    """Per-fixture team statistics incl. expected_goals (plan 03 §4b momentum).

    Works for live OR finished fixtures (xG updates during the match). One
    request per fixture; caller decides which fixtures (live poller / results).
    """
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


def sync_predictions(api: ApiFootball, conn, *, limit: int = 5, force: bool = False) -> int:
    """Algorithmic predictions for the next `limit` upcoming fixtures (reference, 02 §2b).

    Frugal: 1 request per fixture, capped by `limit`, skipped if already stored.
    """
    rows = conn.execute(
        "SELECT api_id FROM fixture WHERE status_short = 'NS' "
        "AND api_id NOT IN (SELECT fixture_api_id FROM prediction) "
        "ORDER BY kickoff_ts LIMIT ?", (limit,)).fetchall()
    pulled = 0
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


def sync_odds(api: ApiFootball, conn, *, limit: int = 5, force: bool = False) -> int:
    """Pre-match Match-Winner odds → de-vigged probabilities (market consensus, 04 §1)."""
    rows = conn.execute(
        "SELECT api_id FROM fixture WHERE status_short = 'NS' "
        "AND api_id NOT IN (SELECT fixture_api_id FROM match_odds) "
        "ORDER BY kickoff_ts LIMIT ?", (limit,)).fetchall()
    pulled = 0
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
        pulled += 1
    conn.commit()
    print(f"[odds] pulled odds for {pulled} upcoming fixtures")
    return pulled


def sync_topscorers(api: ApiFootball, conn, *, force: bool = False) -> int:
    """Top scorers + their stats → player / player_stat (real golden-boot input)."""
    if not force and store.is_fresh(conn, "topscorers", CONFIG.soccer.ttl_results):
        print("[topscorers] fresh — skipped (0 requests)")
        return 0
    items = api.topscorers(league=LEAGUE, season=SEASON)
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
            "player_api_id": pl["id"], "league_id": LEAGUE, "season": SEASON,
            "team_api_id": team.get("id"), "appearances": games.get("appearences"),
            "minutes": games.get("minutes"), "goals": goals.get("total"),
            "assists": goals.get("assists"), "shots_total": shots.get("total"),
            "shots_on": shots.get("on"), "penalty_scored": pen.get("scored"),
            "rating": float(games["rating"]) if games.get("rating") else None,
            "raw_json": store.json.dumps(it, ensure_ascii=False), "updated_at": store.utcnow(),
        }, pk=["player_api_id", "league_id", "season"])
        n += 1
    store.set_watermark(conn, "topscorers", note=f"{n} scorers")
    conn.commit()
    print(f"[topscorers] upserted {n} scorers + stats")
    return n


def sync_player_club_stats(api: ApiFootball, conn, *, club_season: int = 2025,
                           max_players: int = 0) -> int:
    """Club-season stats for squad players (plan 03 §1c). DELIBERATE, 1 req/player.

    Pulls ``/players?id=&season=`` for squad players missing a club-season row.
    ``max_players`` caps the pull (0 = no cap). Requires squads ingested first.
    """
    rows = conn.execute(
        "SELECT DISTINCT s.player_api_id FROM squad s "
        "WHERE s.player_api_id NOT IN (SELECT player_api_id FROM player_stat WHERE season=?) "
        "ORDER BY s.player_api_id" + (" LIMIT ?" if max_players else ""),
        ((club_season, max_players) if max_players else (club_season,))).fetchall()
    import time as _time

    pulled = 0
    for r in rows:
        pid = r["player_api_id"]
        try:
            res = api.players(id=pid, season=club_season)
        except BudgetExceededError as e:
            print(f"[club_stats] stopping early: {e}")
            break
        except Exception as e:  # rate-limit / transient → pace down and skip, don't crash
            print(f"[club_stats] skip player {pid}: {type(e).__name__}")
            _time.sleep(1.0)
            continue
        _time.sleep(0.15)  # pacing to stay under the per-minute rate limit
        if not res:
            continue
        stats = res[0].get("statistics") or []
        # Pick the club entry with the most minutes (their main club that season).
        best = max(stats, key=lambda s: (s.get("games") or {}).get("minutes") or 0, default=None)
        if not best:
            continue
        g, sh = best.get("goals") or {}, best.get("shots") or {}
        games, team = best.get("games") or {}, best.get("team") or {}
        store.upsert(conn, "player_stat", {
            "player_api_id": pid, "league_id": (best.get("league") or {}).get("id"),
            "season": club_season, "team_api_id": team.get("id"),
            "appearances": games.get("appearences"), "minutes": games.get("minutes"),
            "goals": g.get("total"), "assists": g.get("assists"),
            "shots_total": sh.get("total"), "shots_on": sh.get("on"),
            "penalty_scored": (best.get("penalty") or {}).get("scored"),
            "rating": float(games["rating"]) if games.get("rating") else None,
            "raw_json": store.json.dumps(best, ensure_ascii=False), "updated_at": store.utcnow(),
        }, pk=["player_api_id", "league_id", "season"])
        pulled += 1
    conn.commit()
    print(f"[club_stats] pulled club-{club_season} stats for {pulled} players")
    return pulled


def sync_squads(api: ApiFootball, conn, *, force: bool = False) -> int:
    """National-team squads (48 requests). Deliberate, run once / long TTL."""
    if not force and store.is_fresh(conn, "squads", CONFIG.soccer.ttl_static):
        print("[squads] fresh — skipped (0 requests)")
        return 0
    team_ids = [r["api_id"] for r in conn.execute("SELECT api_id FROM team WHERE national=1").fetchall()]
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
                    "team_api_id": tid, "player_api_id": pl["id"], "season": SEASON,
                    "position": pl.get("position"), "number": pl.get("number"),
                    "updated_at": store.utcnow(),
                }, pk=["team_api_id", "player_api_id", "season"])
        conn.commit()
        pulled += 1
    store.set_watermark(conn, "squads", note=f"{pulled} teams")
    print(f"[squads] pulled squads for {pulled} teams")
    return pulled


def run(scope: str, *, force: bool = False) -> None:
    conn = store.init_db()
    api = ApiFootball(conn)
    used_before = api.requests_used_this_month()
    try:
        if scope in ("static", "all"):
            sync_teams(api, conn, force=force)
            sync_fixtures(api, conn, force=force)
            sync_standings(api, conn, force=force)
        if scope in ("results", "all"):
            sync_results(api, conn, force=force)
        if scope in ("topscorers", "results", "all"):
            sync_topscorers(api, conn, force=force)
        if scope in ("h2h", "all"):
            sync_h2h(api, conn, force=force)
        if scope in ("predictions", "all"):
            sync_predictions(api, conn, force=force)
        if scope in ("odds", "all"):
            sync_odds(api, conn, force=force)
        if scope == "live":
            sync_live(api, conn)
        if scope == "squads":
            sync_squads(api, conn, force=force)
    except BudgetExceededError as e:
        print(f"[budget] {e}")
    used_after = api.requests_used_this_month()
    print(f"\nrequests used this run: {used_after - used_before} "
          f"| month total: {used_after}/{CONFIG.soccer.monthly_budget}")


def main() -> None:
    ap = argparse.ArgumentParser(description="World Cup soccer data ingestion (API-Football)")
    ap.add_argument("--scope", default="static",
                    choices=["static", "results", "live", "h2h", "squads", "topscorers",
                             "predictions", "odds", "all"])
    ap.add_argument("--force", action="store_true", help="ignore TTL and re-pull")
    args = ap.parse_args()
    run(args.scope, force=args.force)


if __name__ == "__main__":
    main()
