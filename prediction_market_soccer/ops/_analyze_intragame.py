"""Mine the newly-pulled intra-game data (xG, per-player stats, formations,
event timing) across the 26 finished WC fixtures for NEW findings, with an eye
toward in-play signal ideas beyond the current 7 tactics."""
from __future__ import annotations

import sqlite3
from collections import defaultdict

c = sqlite3.connect("prediction_market_soccer/data/soccer.db")
c.row_factory = sqlite3.Row
tm = {r["api_id"]: r["name"] for r in c.execute("SELECT api_id,name FROM team")}

fx = {r["api_id"]: r for r in c.execute(
    "SELECT api_id,home_api_id,away_api_id,home_goals,away_goals,round "
    "FROM fixture WHERE status_short IN ('FT','AET','PEN')")}

stats = defaultdict(dict)  # fid -> team -> row
for r in c.execute("SELECT * FROM fixture_stats"):
    stats[r["fixture_api_id"]][r["team_api_id"]] = r

print("="*78)
print("FINDING 1 — xG vs actual goals (who 'deserved' more; regression signal)")
print("="*78)
big_under = []  # dominated xG but didn't win
for fid, f in fx.items():
    h, a = f["home_api_id"], f["away_api_id"]
    sh, sa = stats[fid].get(h), stats[fid].get(a)
    if not sh or not sa or sh["xg"] is None or sa["xg"] is None:
        continue
    hg, ag = f["home_goals"], f["away_goals"]
    xgh, xga = sh["xg"], sa["xg"]
    hn, an = tm.get(h, "?")[:11], tm.get(a, "?")[:11]
    res = "H" if hg > ag else ("A" if ag > hg else "D")
    # xG winner
    xgw = "H" if xgh - xga > 0.5 else ("A" if xga - xgh > 0.5 else "D")
    flag = ""
    if xgw == "H" and res != "H":
        flag = f"  <-- {hn} dominated xG ({xgh:.1f} v {xga:.1f}) but {res}"
        big_under.append((fid, hn, an, xgh, xga, res))
    elif xgw == "A" and res != "A":
        flag = f"  <-- {an} dominated xG ({xga:.1f} v {xgh:.1f}) but {res}"
        big_under.append((fid, an, hn, xga, xgh, res))
    print(f"  {hn:11s} {hg}-{ag} {an:11s} | xG {xgh:.2f}-{xga:.2f}{flag}")
print(f"\n  -> {len(big_under)} matches where the xG-dominant team failed to win the 90.")

print("\n"+"="*78)
print("FINDING 2 — match total xG vs total goals (in-play totals calibration)")
print("="*78)
overs = unders = 0
diffs = []
for fid, f in fx.items():
    h, a = f["home_api_id"], f["away_api_id"]
    sh, sa = stats[fid].get(h), stats[fid].get(a)
    if not sh or not sa or sh["xg"] is None or sa["xg"] is None:
        continue
    txg = sh["xg"] + sa["xg"]
    tg = f["home_goals"] + f["away_goals"]
    diffs.append(tg - txg)
    if tg > txg: overs += 1
    else: unders += 1
import statistics
print(f"  matches: {len(diffs)}  | goals>xG in {overs}, goals<=xG in {unders}")
print(f"  mean(goals - total_xG) = {statistics.mean(diffs):+.2f}  (｜>0 => finishing beat chances)")
print(f"  median = {statistics.median(diffs):+.2f}  stdev = {statistics.pstdev(diffs):.2f}")

print("\n"+"="*78)
print("FINDING 3 — goal timing: 1st-half vs 2nd-half goal split (event minutes)")
print("="*78)
fh = sh2 = 0
late = 0  # goals 75'+
goals_by_match = defaultdict(lambda: [0, 0])  # fid -> [1H,2H]
for r in c.execute("SELECT fixture_api_id,minute,type,detail FROM fixture_event "
                   "WHERE type='Goal'"):
    if r["detail"] == "Missed Penalty":
        continue
    m = r["minute"] or 0
    if m <= 45:
        fh += 1; goals_by_match[r["fixture_api_id"]][0] += 1
    else:
        sh2 += 1; goals_by_match[r["fixture_api_id"]][1] += 1
    if m >= 75:
        late += 1
total_g = fh + sh2
print(f"  total goals (events): {total_g}  | 1H={fh} ({fh/total_g:.0%})  2H={sh2} ({sh2/total_g:.0%})")
print(f"  goals at 75'+: {late} ({late/total_g:.0%})")
# 0-0 at HT -> how many goals in 2H?
htgoalless = [fid for fid, v in goals_by_match.items() if v[0] == 0]
print(f"\n  matches goalless at HT: {len(htgoalless)}")
if htgoalless:
    g2 = [goals_by_match[fid][1] for fid in htgoalless]
    scored2 = sum(1 for x in g2 if x > 0)
    print(f"    -> {scored2}/{len(htgoalless)} saw 2H goals; avg 2H goals = {statistics.mean(g2):.2f}")

print("\n"+"="*78)
print("FINDING 4 — HT xG with no goals = pressure cooker? (early xG, late goals)")
print("="*78)
# Can't split xG by half from totals; proxy: high total match xG but 0 HT goals
for fid in htgoalless:
    f = fx[fid]; h, a = f["home_api_id"], f["away_api_id"]
    sh, sa = stats[fid].get(h), stats[fid].get(a)
    if not sh or not sa or sh["xg"] is None:
        continue
    txg = sh["xg"] + sa["xg"]
    tg = f["home_goals"] + f["away_goals"]
    print(f"  {tm.get(h,'?')[:11]:11s} {f['home_goals']}-{f['away_goals']} {tm.get(a,'?')[:11]:11s}"
          f" | matchxG={txg:.1f} finalGoals={tg} (0 at HT)")

print("\n"+"="*78)
print("FINDING 5 — formations: result/goal patterns by formation")
print("="*78)
form = defaultdict(lambda: [0, 0, 0, 0])  # formation -> [used, wins, goals_for, goals_against]
for r in c.execute("SELECT * FROM lineup"):
    fid, t = r["fixture_api_id"], r["team_api_id"]
    f = fx.get(fid)
    if not f or not r["formation"]:
        continue
    is_home = t == f["home_api_id"]
    gf = f["home_goals"] if is_home else f["away_goals"]
    ga = f["away_goals"] if is_home else f["home_goals"]
    won = gf > ga
    d = form[r["formation"]]
    d[0] += 1; d[1] += 1 if won else 0; d[2] += gf; d[3] += ga
for fm, d in sorted(form.items(), key=lambda x: -x[1][0]):
    print(f"  {fm:8s} used={d[0]:2d} wins={d[1]:2d} ({d[1]/d[0]:.0%}) "
          f"GF/g={d[2]/d[0]:.2f} GA/g={d[3]/d[0]:.2f}")

print("\n"+"="*78)
print("FINDING 6 — per-player intra-game: top match ratings & shot volume hogs")
print("="*78)
tops = c.execute(
    "SELECT player_name, rating, goals, assists, shots_total, minutes, fixture_api_id "
    "FROM fixture_player_stats WHERE rating IS NOT NULL ORDER BY rating DESC LIMIT 12").fetchall()
for r in tops:
    print(f"  {r['player_name'][:20]:20s} rating={r['rating']:.1f} G={r['goals'] or 0} "
          f"A={r['assists'] or 0} shots={r['shots_total'] or 0} min={r['minutes']}")

# shot concentration: matches where one team's shots came from <=2 players
print("\n  -- shot concentration (single-player shot share) --")
shot_share = []
team_shots = defaultdict(lambda: defaultdict(int))
for r in c.execute("SELECT fixture_api_id,team_api_id,player_name,shots_total FROM fixture_player_stats "
                   "WHERE shots_total IS NOT NULL AND shots_total>0"):
    team_shots[(r["fixture_api_id"], r["team_api_id"])][r["player_name"]] += r["shots_total"]
for (fid, t), pls in team_shots.items():
    tot = sum(pls.values())
    if tot < 6:
        continue
    top_pl, top_sh = max(pls.items(), key=lambda x: x[1])
    share = top_sh / tot
    if share >= 0.45:
        shot_share.append((tm.get(t, "?"), top_pl, top_sh, tot, share))
for t, p, s, tot, sh in sorted(shot_share, key=lambda x: -x[4])[:8]:
    print(f"  {t[:11]:11s} {p[:18]:18s} took {s}/{tot} shots ({sh:.0%}) — one-man threat")

print("\n"+"="*78)
print("FINDING 7 — possession vs result (does control predict the W?)")
print("="*78)
poss_lost = 0; poss_n = 0
for fid, f in fx.items():
    h, a = f["home_api_id"], f["away_api_id"]
    sh, sa = stats[fid].get(h), stats[fid].get(a)
    if not sh or not sa or sh["possession"] is None or sa["possession"] is None:
        continue
    poss_n += 1
    hg, ag = f["home_goals"], f["away_goals"]
    dom = h if sh["possession"] > sa["possession"] else a
    domg = hg if dom == h else ag
    oppg = ag if dom == h else hg
    if domg < oppg:
        poss_lost += 1
        pv = sh["possession"] if dom == h else sa["possession"]
        print(f"  {tm.get(dom,'?')[:11]:11s} had {pv:.0%} possession but LOST "
              f"({tm.get(h,'?')[:9]} {hg}-{ag} {tm.get(a,'?')[:9]})")
print(f"\n  -> dominant-possession team lost {poss_lost}/{poss_n} matches "
      f"({poss_lost/poss_n:.0%}) — possession ≠ result")
