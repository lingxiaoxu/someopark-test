"""Fine-grained (per-minute) exit-timing research on the backfilled price_tick paths.

The 7-checkpoint study said 'hold-to-FT is best for value picks' — but it could only
see 6 in-play points. With per-minute Poly Global prices we test rules it couldn't:
  * lock at first minute the pick ≥ L¢  (fine threshold scan)
  * trailing stop: sell if the pick falls ≥ D¢ from its running peak (after reaching a
    minimum peak) — captures 'it spiked then started reverting'
  * peak-capture efficiency vs the ORACLE (sell at the path max)
All Poly-consistent: entry = the pick's price at kickoff (last tick ≤ rel_min 0), exits
along the in-play path (rel_min 1..~100), settle 100/0. No look-ahead except ORACLE.
"""
from __future__ import annotations

import json

from prediction_market.config.config import CONFIG

_S = ("home", "draw", "away")


def _paths(conn, fid, side):
    rows = conn.execute(
        "SELECT rel_min, price FROM price_tick WHERE fixture_api_id=? AND side=? ORDER BY ts",
        (fid, side)).fetchall()
    return [(r["rel_min"], r["price"] * 100.0) for r in rows]


def _entry_inplay(path):
    """(entry_c at kickoff, [(rel_min, c) for the in-play window 1..~100])."""
    pre = [c for m, c in path if m <= 0]
    entry = pre[-1] if pre else (path[0][1] if path else None)
    inplay = [(m, c) for m, c in path if 1 <= m <= 125]
    return entry, inplay


def hold(entry, won):
    return (100.0 - entry) if won else -entry


def lock_at(entry, inplay, won, L):
    for m, c in inplay:
        if c >= L:
            return c - entry
    return hold(entry, won)


def trailing(entry, inplay, won, arm, drop):
    """Arm once price ≥ `arm`; then sell when it falls `drop`¢ below the running peak."""
    peak = None
    for m, c in inplay:
        if c >= arm:
            peak = c if peak is None else max(peak, c)
        if peak is not None and c <= peak - drop:
            return c - entry
    return hold(entry, won)


def oracle(entry, inplay, won):
    best = max([c for _, c in inplay], default=None)
    ft = hold(entry, won)
    return max(ft, (best - entry)) if best is not None else ft


def run():
    from prediction_market.ingest import store
    conn = store.init_db()
    # value picks from the freshly-built bet log
    rep = json.loads((CONFIG.paths.output / "performance_report.json").read_text())
    bl = [b for b in rep["bet_log"] if b.get("bet") and b.get("pick")]
    # map team-name match → fixture id via the fixture table
    fixes = {}
    for r in conn.execute("SELECT api_id, home_api_id, away_api_id FROM fixture"):
        fixes[r["api_id"]] = (r["home_api_id"], r["away_api_id"])
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    name2fid = {}
    for fid, (h, a) in fixes.items():
        name2fid[(cmap.get(h), cmap.get(a))] = fid

    recs = []
    for b in bl:
        fid = None
        # bet_log carries home_id/away_id
        key = (b.get("home_id"), b.get("away_id"))
        fid = name2fid.get(key)
        if fid is None:
            continue
        path = _paths(conn, fid, b["pick"])
        if len(path) < 10:
            continue
        entry, inplay = _entry_inplay(path)
        if entry is None or not inplay:
            continue
        recs.append({"won": bool(b["won"]), "entry": entry, "inplay": inplay,
                     "pick": b["pick"], "match": f"{b['home']} {b['score']} {b['away']}"})

    n = len(recs)
    def tot(fn, *a):
        return sum(fn(r["entry"], r["inplay"], r["won"], *a) if a else fn(r["entry"], r["won"]) for r in recs)

    print("=" * 64)
    print(f"FINE-GRAINED EXIT RESEARCH — {n} value picks, per-minute Poly paths")
    print("=" * 64)
    h = sum(hold(r["entry"], r["won"]) for r in recs)
    print(f"  {'hold-to-FT':<26}{h:>9.0f}¢  ({h/n:+.1f}¢/bet)")
    print("  -- lock at first minute pick ≥ L¢ --")
    for L in (70, 75, 80, 85, 88, 90, 92, 95):
        v = sum(lock_at(r["entry"], r["inplay"], r["won"], L) for r in recs)
        print(f"  {'  lock ≥ '+str(L):<26}{v:>9.0f}¢  ({v/n:+.1f}¢/bet)")
    print("  -- trailing stop (arm ≥ A, sell on D¢ drop from peak) --")
    for A, D in ((70, 10), (75, 8), (80, 8), (80, 12), (85, 6), (85, 10)):
        v = sum(trailing(r["entry"], r["inplay"], r["won"], A, D) for r in recs)
        print(f"  {'  arm '+str(A)+' / drop '+str(D):<26}{v:>9.0f}¢  ({v/n:+.1f}¢/bet)")
    o = sum(oracle(r["entry"], r["inplay"], r["won"]) for r in recs)
    print(f"  {'ORACLE (look-ahead max)':<26}{o:>9.0f}¢  ({o/n:+.1f}¢/bet)  <- ceiling")


if __name__ == "__main__":
    run()
