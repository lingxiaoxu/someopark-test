#!/usr/bin/env python3
"""低命中信号族救援挖掘:对每个族测试多个附加触发条件,报 n / 命中% / 均盈亏¢ / 收敛%。
复用基线脚本(inplay_signal_review.py)的结算/收敛口径。仅分析,不改生产代码。"""
import sqlite3, json, glob
from collections import defaultdict
from statistics import mean

c = sqlite3.connect('data/soccer.db'); c.row_factory = sqlite3.Row
prior = json.load(open('data/priors/ext_sim_v0.json'))
rank = {t['team']: t['fifa_rank'] for t in prior['teams']}
styles = {t['name']: t for t in json.load(open('data/output/team_styles.json'))['teams']}
prim = lambda t: (styles[t]['styles'][0]['code'] if t in styles and styles[t].get('styles') else 'unk')

res = {}; tot = {}; teams_of = {}
fids = {json.loads(l)['fixture_id'] for f in glob.glob('data/logs/inplay_review_*.jsonl') for l in open(f) if l.strip()}
for fid in fids:
    r = c.execute("SELECT home_goals hg,away_goals ag FROM fixture WHERE api_id=?", (fid,)).fetchone()
    res[fid] = 'home' if r['hg'] > r['ag'] else ('draw' if r['hg'] == r['ag'] else 'away')
    tot[fid] = r['hg'] + r['ag']

rows = []; tactics = []
for f in sorted(glob.glob('data/logs/inplay_review_*.jsonl')):
    for l in open(f):
        if not l.strip(): continue
        snap = json.loads(l); fid = snap['fixture_id']; mn = snap['minute']
        home, away = [x.strip() for x in snap['match'].replace(' vs ', ' v ').split(' v ')]
        teams_of[fid] = (home, away)
        try: hg, ag = [int(x) for x in snap.get('score', '0-0').split('-')]
        except: hg = ag = 0
        try: rh, ra = [int(x) for x in snap.get('reds', '0-0').split('-')]
        except: rh = ra = 0
        model = snap.get('model', {}); xg = snap.get('xg', {})
        for o in snap.get('opportunities', []):
            side = o.get('side'); act = o.get('action'); kind = o.get('kind'); mkt = o.get('market')
            if kind == 'lock_arb': continue
            if side in ('home', 'draw', 'away'): univ = '3way'
            elif side in ('over', 'under'): univ = 'totals'
            else: continue
            rec = dict(fid=fid, mn=mn, side=side, act=act, kind=kind, univ=univ, edge=o.get('edge'),
                       mkt=mkt, fair=o.get('fair'), reason=o.get('reason', ''), res=res[fid], tot=tot[fid],
                       hg=hg, ag=ag, rh=rh, ra=ra, model=model, xg=xg)
            # settle
            if side in ('home', 'draw', 'away'): settle = 100.0 if res[fid] == side else 0.0
            elif side == 'over': settle = 100.0 if tot[fid] > 2.5 else 0.0
            else: settle = 100.0 if tot[fid] < 2.5 else 0.0
            rec['settle'] = settle
            if mkt is None: rec['pnl'] = None
            else:
                m_c = mkt * 100.0
                rec['pnl'] = (settle - m_c) if act == 'BUY' else (m_c - settle)
            rows.append(rec)
            if kind == 'tactic': tactics.append(rec)

# convergence
track = defaultdict(list)
for r in rows:
    if r.get('mkt') is not None: track[(r['fid'], r['side'])].append((r['mn'], r['mkt']))
for k in track: track[k] = sorted(track[k])
def conv(r):
    if r.get('mkt') is None: return None
    fut = [mk for mn, mk in track[(r['fid'], r['side'])] if r['mn'] < mn <= r['mn'] + 15]
    if not fut: return None
    d = fut[0] - r['mkt']; return d if r['act'] == 'BUY' else -d
for r in rows: r['conv'] = conv(r)

# helpers
def H(v):
    p = [r for r in v if r['pnl'] is not None]
    return 100 * mean(1 if r['pnl'] > 0 else 0 for r in p) if p else float('nan')
def P(v):
    x = [r['pnl'] for r in v if r['pnl'] is not None]; return mean(x) if x else float('nan')
def CV(v):
    x = [r['conv'] for r in v if r['conv'] is not None]
    return 100 * mean(1 if i > 0 else 0 for i in x) if x else float('nan')
def team_of(r):
    h, a = teams_of[r['fid']]
    return h if r['side'] == 'home' else a if r['side'] == 'away' else None
def opp_of(r):
    h, a = teams_of[r['fid']]
    return a if r['side'] == 'home' else h if r['side'] == 'away' else None
def lead_state(r):
    """该 side 当前领先/平/落后(仅 home/away)"""
    if r['side'] == 'home': d = r['hg'] - r['ag']
    elif r['side'] == 'away': d = r['ag'] - r['hg']
    else: return 'level' if r['hg'] == r['ag'] else 'notlevel'
    return 'lead' if d > 0 else 'level' if d == 0 else 'trail'

def report(label, sub):
    n = len(sub); pr = [r for r in sub if r['pnl'] is not None]
    tag = '  <SMALL n<30>' if n < 30 else ''
    print(f"  {label:46} n={n:>4}  hit={H(sub):>5.1f}%  pnl={P(sub):>7.2f}¢  conv={CV(sub):>5.1f}%{tag}")

def section(title):
    print("\n" + "=" * 78); print(title); print("=" * 78)

A = [r for r in rows if r['kind'] == 'relative_value' and r['univ'] == '3way']
B = [r for r in rows if r['kind'] == 'relative_value' and r['univ'] == 'totals']

# ============ 1. DIRECT 风格球队 side ============
section("族1 DIRECT 风格 side (基线 15.7%)")
direct = [r for r in A if team_of(r) and prim(team_of(r)) == 'direct']
report("基线: side 球队=direct", direct)
report("  + 该side当前领先(lead)", [r for r in direct if lead_state(r) == 'lead'])
report("  + 该side当前平局(level)", [r for r in direct if lead_state(r) == 'level'])
report("  + 该side当前落后(trail)", [r for r in direct if lead_state(r) == 'trail'])
report("  + 分钟>70", [r for r in direct if r['mn'] > 70])
report("  + 分钟<=45(上半场)", [r for r in direct if r['mn'] <= 45])
report("  + 对手非possession风格", [r for r in direct if opp_of(r) and prim(opp_of(r)) != 'possession'])
report("  + 对手是low_block", [r for r in direct if opp_of(r) and prim(opp_of(r)) == 'low_block'])
report("  + direct球队是FIFA favorite(rank更高)", [r for r in direct if team_of(r) in rank and opp_of(r) in rank and rank[team_of(r)] < rank[opp_of(r)]])
report("  + direct球队 top10", [r for r in direct if rank.get(team_of(r), 99) <= 10])
report("  + 领先 且 分钟>60", [r for r in direct if lead_state(r) == 'lead' and r['mn'] > 60])
report("  + edge小(<0.10)", [r for r in direct if r['edge'] is not None and abs(r['edge']) < 0.10])

# ============ 2. DRAW 方向 ============
section("族2 DRAW 方向 (基线 33.3%)")
draw = [r for r in A if r['side'] == 'draw']
report("基线: side=draw", draw)
def fdiff(r):
    h, a = teams_of[r['fid']]
    if h in rank and a in rank: return abs(rank[h] - rank[a])
    return None
report("  + FIFA排名差<8(实力接近)", [r for r in draw if (fdiff(r) is not None and fdiff(r) < 8)])
report("  + FIFA排名差>=20(实力悬殊)", [r for r in draw if (fdiff(r) is not None and fdiff(r) >= 20)])
report("  + 分钟>70", [r for r in draw if r['mn'] > 70])
report("  + 分钟>70 且 当前平局", [r for r in draw if r['mn'] > 70 and r['hg'] == r['ag']])
report("  + 当前平局(场上比分level)", [r for r in draw if r['hg'] == r['ag']])
report("  + 比分0-0或1-1", [r for r in draw if r['hg'] == r['ag'] and r['hg'] <= 1])
report("  + 分钟>70 且 比分0-0/1-1", [r for r in draw if r['mn'] > 70 and r['hg'] == r['ag'] and r['hg'] <= 1])
report("  + 模型draw概率>0.30", [r for r in draw if r['model'].get('draw', 0) > 0.30])
report("  + 模型draw概率>0.35", [r for r in draw if r['model'].get('draw', 0) > 0.35])
report("  + edge小(<0.05)", [r for r in draw if r['edge'] is not None and abs(r['edge']) < 0.05])
report("  + 排名差<8 且 分钟>70", [r for r in draw if (fdiff(r) is not None and fdiff(r) < 8) and r['mn'] > 70])

# ============ 3. UNDERDOG (押弱队) ============
section("族3 押 UNDERDOG (基线 26.3%)")
und = [r for r in A if team_of(r) and opp_of(r) and team_of(r) in rank and opp_of(r) in rank and rank[team_of(r)] > rank[opp_of(r)]]
report("基线: 押FIFA underdog", und)
report("  + underdog当前领先", [r for r in und if lead_state(r) == 'lead'])
report("  + underdog当前平局", [r for r in und if lead_state(r) == 'level'])
report("  + underdog当前落后", [r for r in und if lead_state(r) == 'trail'])
report("  + 对手吃红牌(opp side red>0)", [r for r in und if (r['ra'] > 0 if r['side'] == 'home' else r['rh'] > 0)])
report("  + 分钟>70", [r for r in und if r['mn'] > 70])
report("  + underdog领先 且 分钟>70", [r for r in und if lead_state(r) == 'lead' and r['mn'] > 70])
report("  + 排名差<10(小underdog)", [r for r in und if abs(rank[team_of(r)] - rank[opp_of(r)]) < 10])
report("  + 排名差>=25(大underdog)", [r for r in und if abs(rank[team_of(r)] - rank[opp_of(r)]) >= 25])
report("  + 模型也偏好该side(model>对手model)", [r for r in und if r['model'] and r['model'].get(r['side'], 0) > max(r['model'].get('home', 0) if r['side'] != 'home' else 0, r['model'].get('away', 0) if r['side'] != 'away' else 0)])
report("  + edge小(<0.05)", [r for r in und if r['edge'] is not None and abs(r['edge']) < 0.05])
report("  + underdog领先 且 对手红牌", [r for r in und if lead_state(r) == 'lead' and (r['ra'] > 0 if r['side'] == 'home' else r['rh'] > 0)])

# ============ 4. OVER 大小球 ============
section("族4 OVER (基线 49.9%)")
over = [r for r in B if r['side'] == 'over']
report("基线: side=over", over)
report("  + 分钟<=15(开场前15分钟)", [r for r in over if r['mn'] <= 15])
report("  + 分钟<=30", [r for r in over if r['mn'] <= 30])
report("  + 分钟>70(晚盘)", [r for r in over if r['mn'] > 70])
report("  + 当前已>=1球", [r for r in over if r['hg'] + r['ag'] >= 1])
report("  + 当前已>=2球", [r for r in over if r['hg'] + r['ag'] >= 2])
def cxg(r): return (r['xg'].get('home', 0) or 0) + (r['xg'].get('away', 0) or 0)
report("  + combined_xg>=1.0", [r for r in over if cxg(r) >= 1.0])
report("  + combined_xg>=1.5", [r for r in over if cxg(r) >= 1.5])
report("  + 已>=1球 且 combined_xg>=1.0", [r for r in over if r['hg'] + r['ag'] >= 1 and cxg(r) >= 1.0])
report("  + exp_remaining_goals>=1.5", [r for r in over if r['model'].get('exp_remaining_goals', 0) >= 1.5])
report("  + 模型over_2_5>=0.5", [r for r in over if r['model'].get('over_2_5', 0) >= 0.5])
report("  + 当前已>=2球 且 分钟<=70", [r for r in over if r['hg'] + r['ag'] >= 2 and r['mn'] <= 70])
report("  + 开场15min 且 combined_xg>=0.5", [r for r in over if r['mn'] <= 15 and cxg(r) >= 0.5])

# ============ 5. 1X2 大边际 >0.10 ============
section("族5 1X2 edge 0.10-0.20 (基线 40.2%)")
big = [r for r in A if r['edge'] is not None and 0.10 <= abs(r['edge']) < 0.20]
report("基线: 边际0.10-0.20", big)
report("  + 该side领先", [r for r in big if lead_state(r) == 'lead'])
report("  + 该side平局", [r for r in big if lead_state(r) == 'level'])
report("  + 该side落后", [r for r in big if lead_state(r) == 'trail'])
report("  + side top10", [r for r in big if rank.get(team_of(r), 99) <= 10])
report("  + 押favorite(FIFA)", [r for r in big if team_of(r) and opp_of(r) and team_of(r) in rank and opp_of(r) in rank and rank[team_of(r)] < rank[opp_of(r)]])
report("  + 分钟>60(晚盘)", [r for r in big if r['mn'] > 60])
report("  + 领先 且 分钟>60", [r for r in big if lead_state(r) == 'lead' and r['mn'] > 60])
report("  + 领先 且 top10", [r for r in big if lead_state(r) == 'lead' and rank.get(team_of(r), 99) <= 10])
report("  + side球队非direct风格", [r for r in big if team_of(r) and prim(team_of(r)) != 'direct'])
report("  + 押favorite 且 领先", [r for r in big if team_of(r) in rank and opp_of(r) in rank and rank.get(team_of(r), 99) < rank.get(opp_of(r), 99) and lead_state(r) == 'lead'])
report("  + draw方向排除后(仅home/away)", [r for r in big if r['side'] != 'draw'])

# 也看更大边际 0.20+
print("\n  --- 对照: edge 0.20+ (基线 55.9%) ---")
big2 = [r for r in A if r['edge'] is not None and abs(r['edge']) >= 0.20]
report("基线: 边际0.20+", big2)
report("  + 领先", [r for r in big2 if lead_state(r) == 'lead'])
report("  + 押favorite", [r for r in big2 if team_of(r) in rank and opp_of(r) in rank and rank.get(team_of(r), 99) < rank.get(opp_of(r), 99)])

# ============ 6. 风格对阵 ============
section("族6 风格对阵 direct×direct (12%) / balanced×direct (0%)")
def matchup(r):
    h, a = teams_of[r['fid']]
    return ' x '.join(sorted([prim(h), prim(a)]))
dd = [r for r in A if matchup(r) == 'direct x direct']
report("基线: direct x direct", dd)
report("  dd + side领先", [r for r in dd if lead_state(r) == 'lead'])
report("  dd + side平局", [r for r in dd if lead_state(r) == 'level'])
report("  dd + 分钟>70", [r for r in dd if r['mn'] > 70])
report("  dd + 押favorite", [r for r in dd if team_of(r) in rank and opp_of(r) in rank and rank.get(team_of(r), 99) < rank.get(opp_of(r), 99)])
report("  dd + side=over/under(改打totals?)", [r for r in dd if r['side'] in ('over', 'under')])  # 空(这是3way子集)
bd = [r for r in A if matchup(r) == 'balanced x direct']
report("基线: balanced x direct", bd)
report("  bd + side领先", [r for r in bd if lead_state(r) == 'lead'])
report("  bd + 分钟>70", [r for r in bd if r['mn'] > 70])
report("  bd + 押favorite", [r for r in bd if team_of(r) in rank and opp_of(r) in rank and rank.get(team_of(r), 99) < rank.get(opp_of(r), 99)])
# 这些对阵改打 totals 的表现(同一批 fixture 的 over/under 命中)
dd_fids = {r['fid'] for r in dd}
bd_fids = {r['fid'] for r in bd}
print("\n  --- 同批fixture改用 totals 表达 ---")
report("  dd-fixture 的 under 信号", [r for r in B if r['fid'] in dd_fids and r['side'] == 'under'])
report("  dd-fixture 的 over 信号", [r for r in B if r['fid'] in dd_fids and r['side'] == 'over'])

# ============ 7. goal_overreaction_fade(转表达) ============
section("族7 goal_overreaction_fade '弱队进球→反买强队'(背靠方真赢仅39%)")
fade = [r for r in tactics if ('over-react' in r['reason'].lower() or 'fade' in r['reason'].lower()) and r['side'] in ('home', 'away')]
fade_fids_side = [(r['fid'], r['side']) for r in fade]
def dir_won(r): return 100 * mean(1 if rr['res'] == rr['side'] else 0 for rr in [r])
n = len(fade)
print(f"  基线 fade 信号 n={n}")
print(f"    押强队取胜(原表达) 命中率 = {100*mean(1 if r['res']==r['side'] else 0 for r in fade):.1f}%")
# 转表达:同样这些(fid),改用 draw 结算
print(f"    改押 DRAW(平局)      命中率 = {100*mean(1 if r['res']=='draw' else 0 for r in fade):.1f}%")
# 转表达:改押强队"不败"(win or draw, 即双重机会 1X)
print(f"    改押 强队不败(1X双重) 命中率 = {100*mean(1 if r['res'] in (r['side'],'draw') else 0 for r in fade):.1f}%")
# 转表达:改押 under(小球) -- 需要 fid 的 totals
print(f"    改押 UNDER(<2.5)     命中率 = {100*mean(1 if r['tot']<2.5 else 0 for r in fade):.1f}%")
print(f"    改押 OVER(>2.5)      命中率 = {100*mean(1 if r['tot']>2.5 else 0 for r in fade):.1f}%")
# 限定子条件下押强队取胜
def fade_minssince(r):
    import re
    m = re.search(r"scored\s*(?:(\d+)'?\s*ago|just now)", r['reason'].lower())
    if 'just now' in r['reason'].lower(): return 0
    if m and m.group(1): return int(m.group(1))
    return None
print("\n  --- 限定子条件下 '押强队取胜' 命中率 ---")
for cond, fn in [
    ("强队是FIFA favorite", lambda r: team_of(r) in rank and opp_of(r) in rank and rank[team_of(r)] < rank[opp_of(r)]),
    ("分钟<=45(早盘进球)", lambda r: r['mn'] <= 45),
    ("分钟>45", lambda r: r['mn'] > 45),
    ("强队是top10", lambda r: rank.get(team_of(r), 99) <= 10),
    ("强队模型概率>0.5", lambda r: r['model'].get(r['side'], 0) > 0.5),
    ("强队当前未落后", lambda r: lead_state(r) != 'trail'),
]:
    sub = [r for r in fade if fn(r)]
    if sub:
        print(f"    {cond:28} n={len(sub):>4}  押强队赢={100*mean(1 if r['res']==r['side'] else 0 for r in sub):>5.1f}%  "
              f"押draw={100*mean(1 if r['res']=='draw' else 0 for r in sub):>5.1f}%  "
              f"押under={100*mean(1 if r['tot']<2.5 else 0 for r in sub):>5.1f}%")

# favourite_comeback 对照
print("\n  --- 对照 favourite_comeback(背靠方真赢62%)---")
cb = [r for r in tactics if ('comeback' in r['reason'].lower() or 'residual' in r['reason'].lower()) and r['side'] in ('home', 'away')]
print(f"    押强队赢={100*mean(1 if r['res']==r['side'] else 0 for r in cb):.1f}%  "
      f"押不败1X={100*mean(1 if r['res'] in (r['side'],'draw') else 0 for r in cb):.1f}%  "
      f"押under={100*mean(1 if r['tot']<2.5 else 0 for r in cb):.1f}%  n={len(cb)}")

print("\n[DONE]")
