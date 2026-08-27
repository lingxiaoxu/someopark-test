"""Backfill per-minute 2-way ADVANCE price paths → the `price_tick_adv` table (plan 24 §7).

The 2-WAY "advance" twin of ops/backfill_price_ticks.py. The advance market on Polymarket
Global is the per-team "reach <next round>" YES token (the two teams in a knockout tie reach
the next round iff they advance, so their YES prices ARE the 2-way advance path). For each
settled KNOCKOUT fixture we take the two teams' reach-NEXT-round tokens, pull the full
per-minute history, and store each point into `price_tick_adv` with side=home/away,
`rel_min` = minutes since kickoff. Feeds smart_exit_advance + the advance price-track.

Separate module + separate table (never touches the 3-way price_tick). Run:
    python -m prediction_market_soccer.ops.backfill_price_ticks_advance
"""
from __future__ import annotations

from datetime import datetime

# Knockout round → the round a team reaches by WINNING it (the "advance" market key).
_NEXT_ROUND = {"round of 32": "r16", "round of 16": "qf", "quarter-finals": "sf",
               "quarterfinals": "sf", "quarter finals": "sf",
               "semi-finals": "final", "semifinals": "final", "semi finals": "final"}
_FINISHED = ("FT", "AET", "PEN")


def _kickoff_epoch(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except Exception:
        return None


def backfill(conn=None, *, fidelity: int = 1, only_missing: bool = True) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.venues.polymarket_global.reader import PolymarketGlobalReader

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, kickoff_ts, round FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND lower(COALESCE(round,'')) NOT LIKE '%group%' ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
    if not fixtures:
        print("[price_tick_adv] no settled knockout fixtures")
        return {"fixtures": 0, "ticks": 0}
    have = {r[0] for r in conn.execute("SELECT DISTINCT fixture_api_id FROM price_tick_adv")}
    reader = PolymarketGlobalReader()
    idx_cache: dict[str, dict] = {}

    n_fix = n_tick = 0
    for f in fixtures:
        fid = f["api_id"]
        if only_missing and fid in have:
            continue
        hi, ai = cmap.get(f["home_api_id"]), cmap.get(f["away_api_id"])
        ko = _kickoff_epoch(f["kickoff_ts"])
        rk = _NEXT_ROUND.get((f["round"] or "").strip().lower())
        if not (hi and ai and ko and rk):
            continue
        if rk not in idx_cache:
            try:
                idx_cache[rk] = reader.reach_round_index(rk)
            except Exception as e:
                print(f"[price_tick_adv] reach_round_index({rk}) failed: {e}")
                idx_cache[rk] = {}
        idx = idx_cache[rk]
        sides = {"home": idx.get(hi), "away": idx.get(ai)}
        if not (sides["home"] and sides["away"]):
            print(f"[price_tick_adv] no reach-{rk} tokens for {hi} vs {ai}")
            continue
        got = 0
        for side, tok in sides.items():
            try:
                hist = reader.prices_history(tok, fidelity=fidelity, interval="max")
            except Exception as e:
                print(f"[price_tick_adv] history failed {hi}v{ai}/{side}: {e}")
                continue
            for pt in hist:
                ts = pt.get("ts") or pt.get("t")
                price = pt.get("price") if "price" in pt else pt.get("p")
                if ts is None or price is None:
                    continue
                store.upsert(conn, "price_tick_adv", {
                    "fixture_api_id": fid, "side": side, "ts": int(ts),
                    "rel_min": int(round((int(ts) - ko) / 60.0)), "price": float(price),
                    "venue": "poly_global"}, pk=["fixture_api_id", "side", "ts"])
                got += 1
        conn.commit()
        if got:
            n_fix += 1; n_tick += got
            print(f"[price_tick_adv] {hi} v {ai} ({rk}): {got} ticks")
    print(f"[price_tick_adv] backfilled {n_fix} fixtures, {n_tick} ticks")
    return {"fixtures": n_fix, "ticks": n_tick}


if __name__ == "__main__":
    backfill()
