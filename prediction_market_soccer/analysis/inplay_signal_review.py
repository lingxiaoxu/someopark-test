import sqlite3, json, glob
from collections import defaultdict, Counter
from statistics import mean

c=sqlite3.connect('data/soccer.db'); c.row_factory=sqlite3.Row
prior=json.load(open('data/priors/ext_sim_v0.json'))
rank={t['team']:t['fifa_rank'] for t in prior['teams']}
styles={t['name']:t for t in json.load(open('data/output/team_styles.json'))['teams']}
prim=lambda t:(styles[t]['styles'][0]['code'] if t in styles and styles[t].get('styles') else 'unk')

# results: 3-way + total goals
res={}; tot={}; teams_of={}
fids={json.loads(l)['fixture_id'] for f in glob.glob('data/logs/inplay_review_*.jsonl') for l in open(f) if l.strip()}
for fid in fids:
    r=c.execute("SELECT home_goals hg,away_goals ag FROM fixture WHERE api_id=?",(fid,)).fetchone()
    res[fid]='home' if r['hg']>r['ag'] else ('draw' if r['hg']==r['ag'] else 'away')
    tot[fid]=r['hg']+r['ag']

rows=[]; arbs=[]; tactics=[]
for f in sorted(glob.glob('data/logs/inplay_review_*.jsonl')):
    for l in open(f):
        if not l.strip(): continue
        snap=json.loads(l); fid=snap['fixture_id']; mn=snap['minute']
        home,away=[x.strip() for x in snap['match'].replace(' vs ',' v ').split(' v ')]
        teams_of[fid]=(home,away)
        try: hg,ag=[int(x) for x in snap.get('score','0-0').split('-')]
        except: hg=ag=0
        for o in snap.get('opportunities',[]):
            side=o.get('side'); act=o.get('action'); kind=o.get('kind'); mkt=o.get('market')
            if kind=='lock_arb':
                arbs.append(dict(fid=fid,mn=mn,side=side,edge=o.get('edge'),reason=o.get('reason','')))
                continue
            # settle for this side
            if side in ('home','draw','away'): settle=100.0 if res[fid]==side else 0.0; univ='3way'
            elif side=='over':  settle=100.0 if tot[fid]>2.5 else 0.0; univ='totals'
            elif side=='under': settle=100.0 if tot[fid]<2.5 else 0.0; univ='totals'
            else: continue
            rec=dict(fid=fid,mn=mn,side=side,act=act,kind=kind,univ=univ,edge=o.get('edge'),
                     mkt=mkt,settle=settle,reason=o.get('reason',''),res=res[fid],tot=tot[fid],hg=hg,ag=ag)
            if kind=='tactic': tactics.append(rec); 
            if mkt is None: 
                rec['pnl']=None
            else:
                m_c=mkt*100.0; rec['m_c']=m_c
                rec['pnl']=(settle-m_c) if act in ('BUY',) else (m_c-settle)
            rows.append(rec)

# convergence per (fid,side) using priced rows
track=defaultdict(list)
for r in rows:
    if r.get('mkt') is not None: track[(r['fid'],r['side'])].append((r['mn'],r['mkt']))
for k in track: track[k]=sorted(track[k])
def conv(r):
    if r.get('mkt') is None: return None
    fut=[mk for mn,mk in track[(r['fid'],r['side'])] if r['mn']<mn<=r['mn']+15]
    if not fut: return None
    d=fut[0]-r['mkt']; return d if r['act']=='BUY' else -d
for r in rows: r['conv']=conv(r)

def H(v): return 100*mean(1 if r['pnl']>0 else 0 for r in v if r['pnl'] is not None)
def P(v): 
    x=[r['pnl'] for r in v if r['pnl'] is not None]; return mean(x) if x else float('nan')
def CV(v):
    x=[r['conv'] for r in v if r['conv'] is not None]; return (100*mean(1 if i>0 else 0 for i in x),mean(x),len(x)) if x else (float('nan'),float('nan'),0)
def tbl(title,keyfn,rs):
    g=defaultdict(list)
    for r in rs:
        k=keyfn(r)
        if k is not None: g[k].append(r)
    print(f"\n### {title}  (n={len(rs)})")
    print(f"{'bucket':26} {'n':>5} {'priced':>6} {'hit%':>6} {'meanPnL¢':>9} {'conv+%':>7}")
    for k,v in sorted(g.items(),key=lambda x:-len(x[1])):
        pr=[r for r in v if r['pnl'] is not None]
        cvh,_,_=CV(v)
        print(f"{str(k)[:26]:26} {len(v):>5} {len(pr):>6} {H(v) if pr else float('nan'):>6.1f} {P(v):>9.2f} {cvh:>7.1f}")

# ================= UNIVERSE A: relative_value 3-way =================
A=[r for r in rows if r['kind']=='relative_value' and r['univ']=='3way']
print("="*72)
print(f"UNIVERSE A — relative_value 3-way (1X2): {len(A)} signals, all priced")
print(f"  hit-rate {H(A):.1f}%  meanPnL {P(A):.2f}¢  conv+ {CV(A)[0]:.1f}%")
tbl("A.action",lambda r:r['act'],A)
tbl("A.direction",lambda r:r['side'],A)
def ebkt(e):
    if e is None: return None
    a=abs(e); return '0.02-05' if a<.05 else '0.05-10' if a<.10 else '0.10-20' if a<.20 else '0.20+'
tbl("A.edge-magnitude",lambda r:ebkt(r['edge']),A)
mbkt=lambda m:'01-15' if m<=15 else '16-45' if m<=45 else '46-60' if m<=60 else '61-75' if m<=75 else '76-90'
tbl("A.minute",lambda r:mbkt(r['mn']),A)
def state(r):
    s=r['side']
    if s=='home': return 'side-leading' if r['hg']>r['ag'] else 'side-level' if r['hg']==r['ag'] else 'side-trailing'
    if s=='away': return 'side-leading' if r['ag']>r['hg'] else 'side-level' if r['ag']==r['hg'] else 'side-trailing'
    return 'level' if r['hg']==r['ag'] else 'notlevel'
tbl("A.score-state",state,A)
def team_of(r): h,a=teams_of[r['fid']]; return h if r['side']=='home' else a if r['side']=='away' else None
def strg(r):
    t=team_of(r); 
    if t is None: return None
    h,a=teams_of[r['fid']]; o=a if r['side']=='home' else h
    if t not in rank or o not in rank: return None
    return 'back-FAVORITE' if rank[t]<rank[o] else 'back-UNDERDOG'
tbl("A.strength",strg,A)
def rbk(r):
    t=team_of(r); rk=rank.get(t) if t else None
    return None if rk is None else 'top10' if rk<=10 else '11-25' if rk<=25 else '26+'
tbl("A.side-rank",rbk,A)
tbl("A.side-style",lambda r:(prim(team_of(r)) if team_of(r) else None),A)
tbl("A.style-matchup",lambda r:' x '.join(sorted([prim(teams_of[r['fid']][0]),prim(teams_of[r['fid']][1])])),A)

# ================= UNIVERSE B: relative_value totals =================
B=[r for r in rows if r['kind']=='relative_value' and r['univ']=='totals']
print("\n"+"="*72)
print(f"UNIVERSE B — relative_value totals (O/U 2.5): {len(B)} signals")
print(f"  hit-rate {H(B):.1f}%  meanPnL {P(B):.2f}¢  conv+ {CV(B)[0]:.1f}%")
tbl("B.side",lambda r:r['side'],B)
tbl("B.action",lambda r:r['act'],B)
tbl("B.edge-mag",lambda r:ebkt(r['edge']),B)
tbl("B.minute",lambda r:mbkt(r['mn']),B)

# ================= UNIVERSE C: tactic =================
C=[r for r in tactics]
def tac_dir_ok(r):  # backed side achieved its 3-way outcome
    return 1 if r['res']==r['side'] else 0
print("\n"+"="*72)
print(f"UNIVERSE C — tactic overlay: {len(C)} signals ({sum(1 for r in C if r['mkt'] is None)} unpriced)")
print(f"  directional success (backed 3-way side won): {100*mean(tac_dir_ok(r) for r in C if r['side'] in ('home','draw','away')):.1f}%")
tac_themes=Counter()
for r in C:
    rs=r['reason'].lower()
    th=('fade-underdog-goal' if 'over-react' in rs or 'fade' in rs else
        'back-comeback-fav' if 'comeback' in rs or 'residual' in rs else
        'other')
    tac_themes[th]+=1
print("  themes:",dict(tac_themes))
for th in ['fade-underdog-goal','back-comeback-fav']:
    sub=[r for r in C if (('over-react' in r['reason'].lower() or 'fade' in r['reason'].lower()) if th=='fade-underdog-goal' else ('comeback' in r['reason'].lower() or 'residual' in r['reason'].lower())) and r['side'] in ('home','draw','away')]
    if sub: print(f"  [{th}] n={len(sub)} backed-side-won {100*mean(tac_dir_ok(r) for r in sub):.1f}%")

# ================= UNIVERSE D: lock_arb =================
print("\n"+"="*72)
print(f"UNIVERSE D — lock_arb (cross-venue guaranteed): {len(arbs)} signals across {len({a['fid'] for a in arbs})} matches")
edges=[a['edge'] for a in arbs if a['edge'] is not None]
print(f"  locked edge: mean {mean(edges):.3f}  max {max(edges):.3f}  min {min(edges):.3f}  sum {sum(edges):.2f}")
# distinct lock windows (fid,side, rounded reason legs)
seen=set(); distinct=[]
for a in sorted(arbs,key=lambda a:(a['fid'],a['mn'])):
    key=(a['fid'],a['side'],round(a['edge'],2))
    if key in seen: continue
    seen.add(key); distinct.append(a)
print(f"  distinct lock setups (fid×side×edge): {len(distinct)}")
for a in distinct:
    h,aw=teams_of[a['fid']]; print(f"    {h[:12]:12} v {aw[:12]:12} min{a['mn']:>2} {a['side']:4} +{a['edge']:.2f}  {a['reason'][:62]}")
