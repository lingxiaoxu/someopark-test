"""Backfill per-minute market price paths (Polymarket Global prices-history) for
settled WC matches → the `price_tick` table. Unlocks fine-grained exit-timing / cash-out
research beyond the 7 milestone checkpoints (PRE/T15/T30/HT/T60/T75/FT).

For each settled fixture we find its Poly Global single-match event, map the home/away/
draw outcomes to their YES clob tokens, pull the full per-minute history, and store each
point with `rel_min` = minutes since kickoff (negative = pre-match warm-up).

Run:  python -m prediction_market_soccer.ops.backfill_price_ticks
"""
from __future__ import annotations

from datetime import datetime, timezone

_FINISHED = ("FT", "AET", "PEN")


def _kickoff_epoch(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def _map_sides(teams: dict, hi: str, ai: str) -> dict:
    """{side: token} mapping a Poly event's groupItemTitle keys to home/draw/away."""
    from prediction_market_soccer.ingest.club_prior import canonical_team_name, team_id
    out = {}
    for label, tok in teams.items():
        if label.lower().startswith("draw"):
            out["draw"] = tok
            continue
        tid = team_id(canonical_team_name(label))
        if tid == hi:
            out["home"] = tok
        elif tid == ai:
            out["away"] = tok
    return out


def backfill(conn=None, *, fidelity: int = 1, only_missing: bool = True) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.venues.polymarket_global.reader import PolymarketGlobalReader

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # CLUB SCOPE GUARD: recent fixtures of enabled comps only — the club fixture table
    # carries a whole historical season (5,000+ settled rows); paging Poly Global for
    # all of them hung the first pipeline run. 14 days ≈ every fixture whose venue
    # history is still retrievable AND relevant to the live ledger.
    from prediction_market_soccer.config.leagues import active as _active
    _lids = tuple(c.api_football_id for c in _active())
    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, kickoff_ts FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) AND kickoff_ts >= datetime('now', '-14 days') "
        "ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(_lids))),
        (*_FINISHED, *_lids)).fetchall()
    if not fixtures:
        print("[price_tick] no settled fixtures")
        return {"fixtures": 0, "ticks": 0}
    have = {r[0] for r in conn.execute("SELECT DISTINCT fixture_api_id FROM price_tick")}
    dates = [f["kickoff_ts"][:10] for f in fixtures if f["kickoff_ts"]]
    reader = PolymarketGlobalReader()
    evs = reader.list_wc_match_events(
        end_date_min=f"{min(dates)}T00:00:00Z", end_date_max=f"{max(dates)}T23:59:59Z")
    # index events by frozenset of canonical team ids parsed from their team labels
    from prediction_market_soccer.ingest.club_prior import canonical_team_name, team_id
    ev_by_pair = {}
    for e in evs:
        tids = set()
        for label in e["teams"]:
            if not label.lower().startswith("draw"):
                tids.add(team_id(canonical_team_name(label)))
        if len(tids) == 2:
            ev_by_pair[frozenset(tids)] = e

    n_fix = n_tick = 0
    for f in fixtures:
        fid = f["api_id"]
        if only_missing and fid in have:
            continue
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        ko = _kickoff_epoch(f["kickoff_ts"])
        if not (hi and ai and ko):
            continue
        ev = ev_by_pair.get(frozenset({hi, ai}))
        if not ev:
            print(f"[price_tick] no Poly event for {hi} vs {ai}")
            continue
        sides = _map_sides(ev["teams"], hi, ai)
        if "home" not in sides or "away" not in sides:
            print(f"[price_tick] incomplete side map for {ev['slug']}: {list(sides)}")
            continue
        got = 0
        for side, tok in sides.items():
            try:
                hist = reader.prices_history(tok, fidelity=fidelity, interval="max")
            except Exception as e:
                print(f"[price_tick] history failed {ev['slug']}/{side}: {e}")
                continue
            for pt in hist:
                ts = pt.get("ts") or pt.get("t")
                price = pt.get("price") if "price" in pt else pt.get("p")
                if ts is None or price is None:
                    continue
                store.upsert(conn, "price_tick", {
                    "fixture_api_id": fid, "side": side, "ts": int(ts),
                    "rel_min": int(round((int(ts) - ko) / 60.0)), "price": float(price),
                    "venue": "poly_global"}, pk=["fixture_api_id", "side", "ts"])
                got += 1
        conn.commit()
        if got:
            n_fix += 1; n_tick += got
            print(f"[price_tick] {ev['slug']}: {got} ticks ({len(sides)} sides)")
    print(f"[price_tick] backfilled {n_fix} fixtures, {n_tick} ticks")
    return {"fixtures": n_fix, "ticks": n_tick}


if __name__ == "__main__":
    backfill()
