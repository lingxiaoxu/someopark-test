"""Composite team-metric form vs single xG, AND a player-level 'deployed-XI talent'
signal — both PIT walk-forward, scored on OOS Brier + the decision model's ROI."""
import json, math
from dataclasses import replace
from prediction_market_soccer.config.config import CONFIG
from prediction_market_soccer.strategy.decision_model import SideQuote, decide

_FIN=("FT","AET","PEN"); _S=("home","draw","away")
def _num(v):
    if v is None: return None
    if isinstance(v,(int,float)): return float(v)
    try: return float(str(v).replace("%","").strip())
    except: return None
_MET={"expected_goals":"xG","Shots insidebox":"shots_in","Ball Possession":"poss","Passes %":"pass_pct","Shots on Goal":"shots_on"}
def parse(blk):
    o={}
    for s in blk.get("statistics",[]):
        k=_MET.get(s.get("type"))
        if k: o[k]=_num(s.get("value"))
    return o

from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.club_prior import load_prior
from prediction_market_soccer.model.match_pricing import price_match
from prediction_market_soccer.model.strength import build_strength, StrengthModel
from prediction_market_soccer.model.probability_calibration import load_calibration, apply_calibration

conn=store.init_db(); prior=load_prior(); cal=load_calibration()
cmap={r["api_id"]:r["canonical_team_id"] for r in conn.execute("SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
fmet={}
for r in conn.execute("SELECT fixture_api_id,team_api_id,raw_json FROM fixture_stats WHERE raw_json IS NOT NULL"):
    try: fmet[(r["fixture_api_id"],r["team_api_id"])]=parse(json.loads(r["raw_json"]))
    except: pass
# player talent: each team's deployed-XI mean rating from PRIOR matches (PIT proxy for squad quality on the day)
# use fixture_player_stats rating averaged over starters, as a team 'recent player level'
pl={}  # (fixture, team) -> mean starter rating
for r in conn.execute("SELECT fixture_api_id,team_api_id,AVG(rating) ar FROM fixture_player_stats WHERE rating IS NOT NULL AND is_starter=1 GROUP BY fixture_api_id,team_api_id"):
    pl[(r["fixture_api_id"],r["team_api_id"])]=r["ar"]
fx=conn.execute("SELECT api_id,home_api_id,away_api_id,home_goals,away_goals,kickoff_ts FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL ORDER BY kickoff_ts").fetchall()
ms={r["fixture_api_id"]:r for r in conn.execute("SELECT * FROM milestone_snapshot WHERE milestone='PRE'")}
M=[]
for r in fx:
    hi,ai=cmap.get(r["home_api_id"]),cmap.get(r["away_api_id"])
    if not(hi and ai): continue
    y=0 if r["home_goals"]>r["away_goals"] else (1 if r["home_goals"]==r["away_goals"] else 2)
    M.append({"fid":r["api_id"],"hi":hi,"ai":ai,"y":y,"ko":r["kickoff_ts"],"ha":r["home_api_id"],"aa":r["away_api_id"],
              "mh":fmet.get((r["api_id"],r["home_api_id"]),{}),"ma":fmet.get((r["api_id"],r["away_api_id"]),{}),
              "plh":pl.get((r["api_id"],r["home_api_id"])),"pla":pl.get((r["api_id"],r["away_api_id"]))})
n=len(M); ys=[m["y"] for m in M]
base=build_strength(prior,CONFIG.model,sweeps=30)
def zf(d):
    xs=list(d.values())
    if len(xs)<2: return {k:0.0 for k in d}
    mu=sum(xs)/len(xs); sd=(sum((x-mu)**2 for x in xs)/len(xs))**.5 or 1.0
    return {k:(v-mu)/sd for k,v in d.items()}
def metric_netform(i,metric):
    ko=M[i]["ko"]; acc={}
    for j in range(i):
        if M[j]["ko"]>=ko: continue
        vh,va=M[j]["mh"].get(metric),M[j]["ma"].get(metric)
        if vh is not None and va is not None:
            acc.setdefault(M[j]["hi"],[]).append(vh-va); acc.setdefault(M[j]["ai"],[]).append(va-vh)
    return zf({t:sum(v)/len(v) for t,v in acc.items() if v})
def composite(i,metrics):
    zs=[metric_netform(i,m) for m in metrics]
    teams=set().union(*[set(z) for z in zs]) if zs else set()
    return {t:sum(z.get(t,0.0) for z in zs)/len(zs) for t in teams}
def player_netform(i):
    # each team's mean starter-rating in prior matches, minus 6.5 baseline, z-scored
    ko=M[i]["ko"]; acc={}
    for j in range(i):
        if M[j]["ko"]>=ko: continue
        for tid,pv in ((M[j]["hi"],M[j]["plh"]),(M[j]["ai"],M[j]["pla"])):
            if pv is not None: acc.setdefault(tid,[]).append(pv-6.5)
    return zf({t:sum(v)/len(v) for t,v in acc.items() if v})
def adj(zz,w):
    if w<=0 or not zz: return base
    b=getattr(base.cfg,"rating_bound",1.5); nw=dict(base.ratings)
    for t,zv in zz.items():
        if t in nw: nw[t]=max(-b,min(b,nw[t]+w*zv))
    return StrengthModel(ratings=nw,sigma=dict(base.sigma),host_ids=base.host_ids,cfg=base.cfg)
def brier(p,y): return sum((p[k]-(1.0 if k==y else 0.0))**2 for k in range(3))
def quotes(row):
    q={}
    for s in _S:
        a=[]
        if row[f"kalshi_{s}_ask"] is not None: a.append((row[f"kalshi_{s}_ask"],"kalshi"))
        if row[f"poly_{s}_ask"] is not None: a.append((row[f"poly_{s}_ask"],"poly_us"))
        if not a: q[s]=SideQuote(); continue
        ask,v=min(a,key=lambda t:t[0]); q[s]=SideQuote(ask=ask,devig=row[f"devig_{s}"],venue=v)
    return q
MIN=6
def evalsig(name,zfn,w):
    bs=0.0; nn=0; bets=wins=0; pnl=0.0
    for i in range(MIN,n):
        sm=adj(zfn(i),w) if w>0 else base
        mp=price_match(sm,M[i]["hi"],M[i]["ai"]); p=[mp.p_home,mp.p_draw,mp.p_away]
        bs+=brier(p,ys[i]); nn+=1
        pre=ms.get(M[i]["fid"])
        if pre is not None:
            pc=apply_calibration(p,cal); model={"home":pc[0],"draw":pc[1],"away":pc[2]}
            d=decide(model,quotes(pre),calib_confidence=0.25,gate_open=True)
            if d.side and d.price_cents:
                won=d.side==(_S[ys[i]]); bets+=1; wins+=won
                pnl+=(100-d.price_cents) if won else -d.price_cents
    roi=pnl/bets if bets else 0
    print(f"  {name:<34} Brier {bs/nn:.4f}  | bets {bets} win {wins}/{bets} ROI {roi:+.1f}% PnL {pnl:+.0f}c")
print("="*72); print(f"COMPOSITE + PLAYER ALPHA (PIT walk-forward, n={n}, OOS from {MIN+1})"); print("="*72)
evalsig("baseline (no blend)", lambda i:{}, 0.0)
evalsig("xG-form only  (current, w=.15)", lambda i:metric_netform(i,"xG"), 0.15)
evalsig("xG-form  w=.30", lambda i:metric_netform(i,"xG"), 0.30)
evalsig("composite[xG,poss,pass%,shots_in] w=.30", lambda i:composite(i,["xG","poss","pass_pct","shots_in"]), 0.30)
evalsig("composite[xG,shots_in,shots_on] w=.30", lambda i:composite(i,["xG","shots_in","shots_on"]), 0.30)
evalsig("player starter-rating form w=.30", player_netform, 0.30)
evalsig("xG + player composite w=.30", lambda i:composite(i,["xG"]) if False else {**player_netform(i)}, 0.0)  # placeholder
