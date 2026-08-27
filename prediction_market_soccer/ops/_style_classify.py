"""Data-driven team STYLE taxonomy from intra-game metrics (P1, coach/style classification).
Profiles each team that has played by possession / directness / pressing / shot-profile /
formation, z-scores, and KMeans-clusters into ~9 style types. A first, evidence-based cut
(to be enriched with web research on each coach's documented philosophy)."""
import json, statistics
from collections import defaultdict
import numpy as np
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from prediction_market_soccer.ingest import store

def _num(v):
    if v is None: return None
    if isinstance(v,(int,float)): return float(v)
    try: return float(str(v).replace("%","").strip())
    except: return None
_MET={"expected_goals":"xG","Shots on Goal":"sot","Total Shots":"shots","Shots insidebox":"sin","Shots outsidebox":"sout","Ball Possession":"poss","Passes %":"passpct","Total passes":"passes","Fouls":"fouls","Corner Kicks":"corners"}
conn=store.init_db(); conn.row_factory=store.sqlite3.Row if hasattr(store,'sqlite3') else None
import sqlite3; conn=sqlite3.connect('prediction_market_soccer/data/soccer.db'); conn.row_factory=sqlite3.Row
cmap={r["api_id"]:r["canonical_team_id"] for r in conn.execute("SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
prof=defaultdict(lambda: defaultdict(list))
for r in conn.execute("SELECT fixture_api_id,team_api_id,raw_json FROM fixture_stats WHERE raw_json IS NOT NULL"):
    t=cmap.get(r["team_api_id"])
    if not t: continue
    for s in json.loads(r["raw_json"]).get("statistics",[]):
        k=_MET.get(s.get("type"))
        if k:
            v=_num(s.get("value"))
            if v is not None: prof[t][k].append(v)
# formation mode per team
forms=defaultdict(list)
for r in conn.execute("SELECT team_api_id,formation FROM lineup WHERE formation IS NOT NULL"):
    t=cmap.get(r["team_api_id"])
    if t: forms[t].append(r["formation"])
teams=sorted(prof)
feats=[]; rows=[]
for t in teams:
    p=prof[t]
    def avg(k): return statistics.mean(p[k]) if p.get(k) else None
    poss=avg("poss"); passpct=avg("passpct"); shots=avg("shots"); sout=avg("sout"); sin=avg("sin")
    xg=avg("xG"); fouls=avg("fouls"); corners=avg("corners"); passes=avg("passes")
    if None in (poss,passpct,shots,xg): continue
    directness = (sout/(shots) if shots else 0)        # share of shots from distance
    press = fouls or 0                                  # fouls as a (rough) aggression proxy
    chance_q = (xg/shots) if shots else 0               # xG per shot (build-up quality)
    rows.append(t)
    feats.append([poss,passpct,passes or 0,shots,sin or 0,directness,xg,chance_q,press,corners or 0])
X=StandardScaler().fit_transform(np.array(feats))
k=9
km=KMeans(n_clusters=k,n_init=10,random_state=0).fit(X)
labels=km.labels_
clusters=defaultdict(list)
for t,l in zip(rows,labels): clusters[l].append(t)
# describe each cluster by its standout (z) features
fnames=["poss","passpct","passes","shots","sin","direct","xG","chanceQ","press","corners"]
cent=km.cluster_centers_
print(f"=== {k} STYLE CLUSTERS over {len(rows)} teams ===")
for l in sorted(clusters, key=lambda l:-len(clusters[l])):
    c=cent[l]; top=sorted(range(len(fnames)),key=lambda i:-abs(c[i]))[:3]
    desc=", ".join(f"{fnames[i]}{'+' if c[i]>0 else '-'}{abs(c[i]):.1f}" for i in top)
    print(f"  cluster {l} (n={len(clusters[l])}): {desc}")
    print(f"     teams: {', '.join(clusters[l])}")
