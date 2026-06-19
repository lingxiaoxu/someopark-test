"""P2-P4 consolidated PIT walk-forward: h2h goal-diff, GK quality (goals_prevented),
and star-reliance — does any lower OOS Brier vs baseline? (Expect mostly negative on 26.)"""
import json, statistics
from collections import defaultdict
from prediction_market.config.config import CONFIG
from prediction_market.ingest import store
from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.match_pricing import price_match
from prediction_market.model.strength import build_strength, StrengthModel

conn=store.init_db(); prior=load_prior()
cmap={r["api_id"]:r["canonical_team_id"] for r in conn.execute("SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
def _num(v):
    if v is None: return None
    try: return float(str(v).replace("%","").strip())
    except: return None
# goals_prevented per (fixture,team) from raw_json
gp={}
for r in conn.execute("SELECT fixture_api_id,team_api_id,raw_json FROM fixture_stats WHERE raw_json IS NOT NULL"):
    for s in json.loads(r["raw_json"]).get("statistics",[]):
        if s.get("type")=="goals_prevented":
            gp[(r["fixture_api_id"],r["team_api_id"])]=_num(s.get("value"))
# h2h: avg GD (home-persp by api home/away) keyed by pair
h2h=defaultdict(list)
for r in conn.execute("SELECT pair_key,home_api_id,away_api_id,home_goals,away_goals FROM h2h WHERE home_goals IS NOT NULL"):
    h2h[r["pair_key"]].append((r["home_api_id"],r["away_api_id"],r["home_goals"],r["away_goals"]))
# shot concentration (star reliance): max single-player shot share per (fixture,team)
shots=defaultdict(lambda: defaultdict(int))
for r in conn.execute("SELECT fixture_api_id,team_api_id,player_name,shots_total FROM fixture_player_stats WHERE shots_total>0"):
    shots[(r["fixture_api_id"],r["team_api_id"])][r["player_name"]]+=r["shots_total"]
def concentration(fid,tid):
    d=shots.get((fid,tid))
    if not d: return None
    tot=sum(d.values())
    return max(d.values())/tot if tot>=5 else None

fx=conn.execute("SELECT api_id,home_api_id,away_api_id,home_goals,away_goals,kickoff_ts FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL ORDER BY kickoff_ts").fetchall()
M=[]
for r in fx:
    hi,ai=cmap.get(r["home_api_id"]),cmap.get(r["away_api_id"])
    if not(hi and ai): continue
    y=0 if r["home_goals"]>r["away_goals"] else (1 if r["home_goals"]==r["away_goals"] else 2)
    M.append({"fid":r["api_id"],"ha":r["home_api_id"],"aa":r["away_api_id"],"hi":hi,"ai":ai,"y":y,"ko":r["kickoff_ts"]})
base=build_strength(prior,CONFIG.model,sweeps=30)
def z(d):
    xs=list(d.values())
    if len(xs)<2: return {k:0.0 for k in d}
    mu=statistics.mean(xs); sd=statistics.pstdev(xs) or 1
    return {k:(v-mu)/sd for k,v in d.items()}
def brier(p,y): return sum((p[k]-(1.0 if k==y else 0.0))**2 for k in range(3))
def adj(zz,w):
    if w<=0 or not zz: return base
    nw=dict(base.ratings); b=base.cfg.rating_bound
    for t,zv in zz.items():
        if t in nw: nw[t]=max(-b,min(b,nw[t]+w*zv))
    return StrengthModel(ratings=nw,sigma=dict(base.sigma),host_ids=base.host_ids,cfg=base.cfg)
# signal builders (PIT)
def gk_form(i):
    ko=M[i]["ko"]; acc=defaultdict(list)
    for j in range(i):
        if M[j]["ko"]>=ko: continue
        for tid,api in ((M[j]["hi"],M[j]["ha"]),(M[j]["ai"],M[j]["aa"])):
            v=gp.get((M[j]["fid"],api))
            if v is not None: acc[tid].append(v)
    return z({t:statistics.mean(v) for t,v in acc.items() if v})
def conc_form(i):  # higher concentration = more fragile (negative)
    ko=M[i]["ko"]; acc=defaultdict(list)
    for j in range(i):
        if M[j]["ko"]>=ko: continue
        for tid,api in ((M[j]["hi"],M[j]["ha"]),(M[j]["ai"],M[j]["aa"])):
            c=concentration(M[j]["fid"],api)
            if c is not None: acc[tid].append(-c)
    return z({t:statistics.mean(v) for t,v in acc.items() if v})
def h2h_sig(i):
    # per-match direct nudge: home rating + w*z of avg historical GD (home persp)
    key=f"{M[i]['ha']}-{M[i]['aa']}"; meets=h2h.get(key,[])
    if not meets: return {}
    gd=statistics.mean([ (hg-ag) if h==M[i]["ha"] else (ag-hg) for (h,a,hg,ag) in meets])
    return {M[i]["hi"]: gd}  # raw GD as nudge (not z; single pair)
ys=[m["y"] for m in M]; MIN=6
def score(fn,w):
    bs=0;nn=0
    for i in range(MIN,len(M)):
        sm=adj(fn(i),w) if w>0 else base
        mp=price_match(sm,M[i]["hi"],M[i]["ai"]); p=[mp.p_home,mp.p_draw,mp.p_away]
        bs+=brier(p,ys[i]); nn+=1
    return bs/nn
b0=score(lambda i:{},0.0)
print(f"baseline Brier {b0:.4f}")
n_h2h=sum(1 for i in range(MIN,len(M)) if h2h.get(f"{M[i]['ha']}-{M[i]['aa']}"))
print(f"P2 h2h: {n_h2h}/{len(M)-MIN} OOS matches have prior meetings")
for w in (0.1,0.2): print(f"  h2h GD nudge w={w}: Brier {score(h2h_sig,w):.4f}  Δ{score(h2h_sig,w)-b0:+.4f}")
for w in (0.15,0.30): print(f"P3 GK goals_prevented form w={w}: Brier {score(gk_form,w):.4f}  Δ{score(gk_form,w)-b0:+.4f}")
for w in (0.15,0.30): print(f"P4 star-reliance(neg conc) w={w}: Brier {score(conc_form,w):.4f}  Δ{score(conc_form,w)-b0:+.4f}")
