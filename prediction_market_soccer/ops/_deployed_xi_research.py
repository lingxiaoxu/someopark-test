"""Deployed-XI strength: does the talent of the ACTUALLY-fielded XI (vs the team's
best-XI baseline — capturing rotation/rest/injuries) predict the result? PIT at lineup
release (~40min pre-kickoff). Talent = EA FC `overall` matched to the starters by name."""
import statistics
from prediction_market_soccer.config.config import CONFIG
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.club_prior import load_prior
from prediction_market_soccer.model.match_pricing import price_match
from prediction_market_soccer.model.strength import build_strength, StrengthModel

conn=store.init_db(); prior=load_prior()
cmap={r["api_id"]:r["canonical_team_id"] for r in conn.execute("SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
# fc talent by (canonical_team, name) and best-11 baseline per team
fc_by_team={}
for r in conn.execute("SELECT canonical_team_id,name,overall FROM fc_player WHERE overall IS NOT NULL"):
    fc_by_team.setdefault(r["canonical_team_id"],{})[r["name"]]=r["overall"]
best11={t:statistics.mean(sorted(d.values(),reverse=True)[:11]) for t,d in fc_by_team.items() if len(d)>=11}
# deployed XI talent per (fixture, team): mean overall of matched starters
dep={}
for r in conn.execute("SELECT fixture_api_id,team_api_id,player_name FROM fixture_player_stats WHERE is_starter=1"):
    t=cmap.get(r["team_api_id"])
    ov=fc_by_team.get(t,{}).get(r["player_name"])
    if ov is not None:
        dep.setdefault((r["fixture_api_id"],r["team_api_id"]),[]).append(ov)
fx=conn.execute("SELECT api_id,home_api_id,away_api_id,home_goals,away_goals,kickoff_ts FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL ORDER BY kickoff_ts").fetchall()
M=[]
for r in fx:
    hi,ai=cmap.get(r["home_api_id"]),cmap.get(r["away_api_id"])
    if not(hi and ai): continue
    y=0 if r["home_goals"]>r["away_goals"] else (1 if r["home_goals"]==r["away_goals"] else 2)
    def delta(team_api,tid):
        s=dep.get((r["api_id"],team_api)); 
        if not s or len(s)<7 or tid not in best11: return None
        return statistics.mean(s)-best11[tid]   # deployed minus best-XI (<=0 = rotated)
    M.append({"hi":hi,"ai":ai,"y":y,"dh":delta(r["home_api_id"],hi),"da":delta(r["away_api_id"],ai)})
base=build_strength(prior,CONFIG.model,sweeps=30)
# net delta (home rotation minus away rotation), z-scored across matches with data
nets={i:(m["dh"]-m["da"]) for i,m in enumerate(M) if m["dh"] is not None and m["da"] is not None}
print(f"matches with both deployed-XI deltas: {len(nets)}/{len(M)}")
if nets:
    vals=list(nets.values()); mu=statistics.mean(vals); sd=statistics.pstdev(vals) or 1
    print(f"  net deployed-vs-best delta: mean {mu:+.1f} talent pts, range [{min(vals):+.1f},{max(vals):+.1f}]")
def brier(p,y): return sum((p[k]-(1.0 if k==y else 0.0))**2 for k in range(3))
def adj_home(i,w):
    # nudge home rating UP by w * its own deployed delta z (rotation hurts), per match
    m=M[i]
    if m["dh"] is None and m["da"] is None: return base
    nw=dict(base.ratings); b=base.cfg.rating_bound
    # talent delta in points → scale: ~ delta/5 as a rating nudge (5 overall pts ~ 1 unit)
    if m["dh"] is not None and m["hi"] in nw: nw[m["hi"]]=max(-b,min(b,nw[m["hi"]]+w*m["dh"]/5.0))
    if m["da"] is not None and m["ai"] in nw: nw[m["ai"]]=max(-b,min(b,nw[m["ai"]]+w*m["da"]/5.0))
    return StrengthModel(ratings=nw,sigma=dict(base.sigma),host_ids=base.host_ids,cfg=base.cfg)
ys=[m["y"] for m in M]; MIN=6
for w in (0.0,0.5,1.0,1.5):
    bs=0;nn=0
    for i in range(MIN,len(M)):
        sm=adj_home(i,w) if w>0 else base
        mp=price_match(sm,M[i]["hi"],M[i]["ai"]); p=[mp.p_home,mp.p_draw,mp.p_away]
        bs+=brier(p,ys[i]); nn+=1
    print(f"  deployed-XI weight {w:.1f}: OOS Brier {bs/nn:.4f}")
