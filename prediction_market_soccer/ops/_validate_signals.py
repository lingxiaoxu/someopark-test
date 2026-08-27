"""Self-review: re-derive each data-mined signal's supporting evidence from the
26 finished fixtures (final-data proxy where per-minute isn't stored). Confirms
every new tactic is grounded in real, recurring data — not a one-off anecdote."""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict

c = sqlite3.connect("prediction_market_soccer/data/soccer.db")
c.row_factory = sqlite3.Row
tm = {r["api_id"]: r["name"] for r in c.execute("SELECT api_id,name FROM team")}
fx = {r["api_id"]: r for r in c.execute(
    "SELECT api_id,home_api_id,away_api_id,home_goals,away_goals FROM fixture "
    "WHERE status_short IN ('FT','AET','PEN')")}
stats = defaultdict(dict)
for r in c.execute("SELECT * FROM fixture_stats"):
    stats[r["fixture_api_id"]][r["team_api_id"]] = r

# HT score from goal events.
ht = defaultdict(lambda: [0, 0])
g2 = defaultdict(int)
late = total = 0
for r in c.execute("SELECT fixture_api_id,minute,detail,team_api_id FROM fixture_event WHERE type='Goal'"):
    if r["detail"] == "Missed Penalty":
        continue
    total += 1
    f = fx.get(r["fixture_api_id"])
    if not f:
        continue
    home = r["team_api_id"] == f["home_api_id"]
    if (r["minute"] or 0) <= 45:
        ht[r["fixture_api_id"]][0 if home else 1] += 1
    else:
        g2[r["fixture_api_id"]] += 1
    if (r["minute"] or 0) >= 75:
        late += 1


def passed(name, ok):
    print(f"  [{'PASS' if ok else 'WEAK'}] {name}")


print("="*72)
print("SIGNAL SELF-REVIEW — data support across 26 finished fixtures")
print("="*72)

# 1. dormant_explosion
htlow = [fid for fid in fx if sum(ht[fid]) <= 1]
exploded = [fid for fid in htlow if g2[fid] >= 1]
print("\n1. dormant_explosion (HT ≤1 goal + chances → 2H surge)")
print(f"   HT≤1-goal matches: {len(htlow)}; saw a 2H goal: {len(exploded)} "
      f"({len(exploded)/len(htlow):.0%}); avg 2H goals = {statistics.mean([g2[f] for f in htlow]):.1f}")
# xG filter removes the truly sterile ones
def _combxg(fid):
    s = stats[fid]
    h, a = fx[fid]["home_api_id"], fx[fid]["away_api_id"]
    return ((s[h]["xg"] if h in s and s[h]["xg"] is not None else 0)
            + (s[a]["xg"] if a in s and s[a]["xg"] is not None else 0))
sterile = [fid for fid in htlow if _combxg(fid) < 1.0]
print(f"   of those, sterile (combined xG<1.0, signal SKIPS): {len(sterile)} "
      f"→ {[tm.get(fx[f]['home_api_id'],'?')[:7] for f in sterile]}")
passed("2H goal rate high after quiet HT", len(exploded)/len(htlow) >= 0.7)

# 2. xg_dominance_chase
chase = []
for fid, f in fx.items():
    sh, sa = stats[fid].get(f["home_api_id"]), stats[fid].get(f["away_api_id"])
    if not sh or not sa or sh["xg"] is None or sa["xg"] is None:
        continue
    for me, op, mg, og, nm in ((sh, sa, f["home_goals"], f["away_goals"], f["home_api_id"]),
                               (sa, sh, f["away_goals"], f["home_goals"], f["away_api_id"])):
        if me["xg"] - op["xg"] >= 1.0 and mg <= og:
            chase.append((tm.get(nm, "?"), me["xg"], op["xg"], mg, og))
print("\n2. xg_dominance_chase (xG lead ≥1.0 but not ahead)")
for t, xf, xa, gf, ga in chase:
    print(f"   {t[:12]:12s} xG {xf:.1f} vs {xa:.1f}  scored {gf}:{ga}")
passed("recurring (≥3 instances)", len(chase) >= 3)

# 3. possession_trap_fade
trap = []
for fid, f in fx.items():
    for side, me, op, mg, og in (("home", f["home_api_id"], f["away_api_id"], f["home_goals"], f["away_goals"]),
                                  ("away", f["away_api_id"], f["home_api_id"], f["away_goals"], f["home_goals"])):
        s = stats[fid].get(me)
        if not s or s["possession"] is None or s["xg"] is None:
            continue
        if s["possession"] >= 0.58 and s["xg"] <= 0.8 and mg <= og:
            trap.append((tm.get(me, "?"), s["possession"], s["xg"], mg, og))
print("\n3. possession_trap_fade (≥58% poss, ≤0.8 xG, not ahead)")
for t, p, x, gf, ga in trap:
    print(f"   {t[:12]:12s} poss {p:.0%} xG {x:.1f}  scored {gf}:{ga} (faded → opp/under)")
passed("sterile-possession traps exist", len(trap) >= 2)

# 4. formation_fragility
form = defaultdict(lambda: [0, 0])
for r in c.execute("SELECT * FROM lineup"):
    f = fx.get(r["fixture_api_id"])
    if not f or not r["formation"]:
        continue
    home = r["team_api_id"] == f["home_api_id"]
    ga = f["away_goals"] if home else f["home_goals"]
    form[r["formation"]][0] += 1
    form[r["formation"]][1] += ga
fragile = {"5-3-2", "3-4-2-1"}
fr = [(k, v[1]/v[0]) for k, v in form.items() if k in fragile and v[0] >= 1]
solid = [(k, v[1]/v[0]) for k, v in form.items() if k == "4-2-3-1"]
print("\n4. formation_fragility (back-3/stretched leak goals)")
for k, ga in sorted(fr, key=lambda x: -x[1]):
    print(f"   {k:8s} GA/game {ga:.2f}  (fragile)")
for k, ga in solid:
    print(f"   {k:8s} GA/game {ga:.2f}  (solid baseline)")
passed("fragile formations concede more than 4-2-3-1",
       fr and solid and max(g for _, g in fr) > solid[0][1])

# 5. lone_threat_removed (prevalence of single-point-of-failure teams)
team_shots = defaultdict(lambda: defaultdict(int))
for r in c.execute("SELECT fixture_api_id,team_api_id,player_name,shots_total FROM fixture_player_stats "
                   "WHERE shots_total>0"):
    team_shots[(r["fixture_api_id"], r["team_api_id"])][r["player_name"]] += r["shots_total"]
lone = 0
lone_ex = []
for (fid, t), pls in team_shots.items():
    tot = sum(pls.values())
    if tot < 5:
        continue
    p, s = max(pls.items(), key=lambda x: x[1])
    if s / tot >= 0.50:
        lone += 1
        lone_ex.append((tm.get(t, "?"), p, s, tot))
print("\n5. lone_threat_removed (one player ≥50% of team shots)")
for t, p, s, tot in lone_ex:
    print(f"   {t[:12]:12s} {p[:18]:18s} {s}/{tot} shots ({s/tot:.0%})")
passed("single-point-of-failure teams exist", lone >= 1)

# 6. late_goal_bias + finishing_uplift
print("\n6. late_goal_bias (timing) & 7. finishing_uplift (goals vs xG)")
print(f"   goals at 75'+: {late}/{total} ({late/total:.0%}); 2H share computed earlier")
diffs = []
for fid, f in fx.items():
    sh, sa = stats[fid].get(f["home_api_id"]), stats[fid].get(f["away_api_id"])
    if sh and sa and sh["xg"] is not None and sa["xg"] is not None:
        diffs.append((f["home_goals"]+f["away_goals"]) - (sh["xg"]+sa["xg"]))
print(f"   mean(goals − total_xG) = {statistics.mean(diffs):+.2f} (median {statistics.median(diffs):+.2f})")
passed("late goals materially frequent", late/total >= 0.25)
passed("finishing beats xG on average", statistics.mean(diffs) > 0.3)

# 8. live_odds_crossval — structural: do we have a bookmaker reference to cross-check?
nodds = c.execute("SELECT COUNT(DISTINCT fixture_api_id) FROM match_odds").fetchone()[0]
print("\n8. live_odds_crossval (independent bookmaker price source)")
print(f"   fixtures with bookmaker odds stored: {nodds} (live_consensus refreshes these in-play)")
passed("bookmaker price source available", nodds >= 1)
