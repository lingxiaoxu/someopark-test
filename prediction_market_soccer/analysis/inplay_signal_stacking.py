"""Condition-stacking深挖: 复用 inplay_signal_review.py 的结算/收敛口径,
对高命中信号族叠加额外条件,报 n / 命中% / 均盈亏¢ / 收敛+%。
只读 stdlib (sqlite3/json/glob)。不改任何生产代码。
"""
import sqlite3, json, glob
from collections import defaultdict
from statistics import mean

c = sqlite3.connect('data/soccer.db'); c.row_factory = sqlite3.Row
prior = json.load(open('data/priors/ext_sim_v0.json'))
rank = {t['team']: t['fifa_rank'] for t in prior['teams']}
styles = {t['name']: t for t in json.load(open('data/output/team_styles.json'))['teams']}
prim = lambda t: (styles[t]['styles'][0]['code'] if t in styles and styles[t].get('styles') else 'unk')
poss_metric = lambda t: (styles[t]['metrics'].get('possession') if t in styles and styles[t].get('metrics') else None)

# results
res = {}; tot = {}; teams_of = {}
fids = {json.loads(l)['fixture_id'] for f in glob.glob('data/logs/inplay_review_*.jsonl') for l in open(f) if l.strip()}
for fid in fids:
    r = c.execute("SELECT home_goals hg,away_goals ag FROM fixture WHERE api_id=?", (fid,)).fetchone()
    res[fid] = 'home' if r['hg'] > r['ag'] else ('draw' if r['hg'] == r['ag'] else 'away')
    tot[fid] = r['hg'] + r['ag']

rows = []
for f in sorted(glob.glob('data/logs/inplay_review_*.jsonl')):
    for l in open(f):
        if not l.strip(): continue
        snap = json.loads(l); fid = snap['fixture_id']; mn = snap['minute']
        home, away = [x.strip() for x in snap['match'].replace(' vs ', ' v ').split(' v ')]
        teams_of[fid] = (home, away)
        try: hg, ag = [int(x) for x in snap.get('score', '0-0').split('-')]
        except: hg = ag = 0
        reds = snap.get('reds') or {}
        for o in snap.get('opportunities', []):
            side = o.get('side'); act = o.get('action'); kind = o.get('kind'); mkt = o.get('market')
            if kind == 'lock_arb': continue
            if side in ('home', 'draw', 'away'): settle = 100.0 if res[fid] == side else 0.0; univ = '3way'
            elif side == 'over': settle = 100.0 if tot[fid] > 2.5 else 0.0; univ = 'totals'
            elif side == 'under': settle = 100.0 if tot[fid] < 2.5 else 0.0; univ = 'totals'
            else: continue
            rec = dict(fid=fid, mn=mn, side=side, act=act, kind=kind, univ=univ, edge=o.get('edge'),
                       mkt=mkt, settle=settle, reason=o.get('reason', ''), res=res[fid], tot=tot[fid],
                       hg=hg, ag=ag, reds=reds)
            if mkt is None:
                rec['pnl'] = None
            else:
                m_c = mkt * 100.0; rec['m_c'] = m_c
                rec['pnl'] = (settle - m_c) if act in ('BUY',) else (m_c - settle)
            rows.append(rec)

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
    pr = [r for r in v if r['pnl'] is not None]
    return 100 * mean(1 if r['pnl'] > 0 else 0 for r in pr) if pr else float('nan')
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
def side_rank(r):
    t = team_of(r); return rank.get(t) if t else None
def is_leading(r):
    s = r['side']
    if s == 'home': return r['hg'] > r['ag']
    if s == 'away': return r['ag'] > r['hg']
    return False
def is_trailing(r):
    s = r['side']
    if s == 'home': return r['hg'] < r['ag']
    if s == 'away': return r['ag'] < r['hg']
    return False
def _reds_ha(r):
    """reds is a 'H-A' string (home reds - away reds)."""
    rd = r['reds']
    if isinstance(rd, str) and '-' in rd:
        try:
            rh, ra = [int(x) for x in rd.split('-')]; return rh, ra
        except: return 0, 0
    return 0, 0
def red_advantage(r):
    """backed side has FEWER reds than opp (man advantage)."""
    rh, ra = _reds_ha(r)
    if r['side'] == 'home': return rh < ra
    if r['side'] == 'away': return ra < rh
    return False
def side_style(r):
    t = team_of(r); return prim(t) if t else None
def matchup(r):
    return ' x '.join(sorted([prim(teams_of[r['fid']][0]), prim(teams_of[r['fid']][1])]))

A = [r for r in rows if r['kind'] == 'relative_value' and r['univ'] == '3way']
B = [r for r in rows if r['kind'] == 'relative_value' and r['univ'] == 'totals']

def report(label, sub, note=''):
    pr = [r for r in sub if r['pnl'] is not None]
    n = len(sub); npr = len(pr)
    flag = '  <<< 小样本(n<30)' if npr < 30 else ''
    print(f"{label:52} n={n:>4} 命中={H(sub):5.1f}% 均盈亏={P(sub):7.2f}¢ 收敛+={CV(sub):5.1f}% {note}{flag}")

print("="*120)
print("基线复现 (UNIVERSE A 1X2 relative_value): n=%d 命中=%.1f%% 均盈亏=%.2f¢" % (len(A), H(A), P(A)))
print("="*120)

print("\n### 任务2 — 指定的6个叠加组合")
report("top10 ∩ 领先 ∩ 分钟>60", [r for r in A if (side_rank(r) or 99) <= 10 and is_leading(r) and r['mn'] > 60])
report("possession ∩ 边际0.03-0.10", [r for r in A if side_style(r) == 'possession' and r['edge'] is not None and 0.03 <= abs(r['edge']) < 0.10])
report("UNDER ∩ 分钟<15", [r for r in B if r['side'] == 'under' and r['mn'] < 15])
report("领先方 ∩ 红牌优势", [r for r in A if is_leading(r) and red_advantage(r)])
report("possession×possession ∩ 领先方", [r for r in A if matchup(r) == 'possession x possession' and is_leading(r)])
report("top10 ∩ 边际<0.10 (剔除大边际)", [r for r in A if (side_rank(r) or 99) <= 10 and r['edge'] is not None and abs(r['edge']) < 0.10])

print("\n### 任务2 — 自主发掘的叠加组合 (≥5个)")
report("top10 ∩ 领先", [r for r in A if (side_rank(r) or 99) <= 10 and is_leading(r)])
report("possession ∩ 领先", [r for r in A if side_style(r) == 'possession' and is_leading(r)])
report("possession ∩ 分钟>60", [r for r in A if side_style(r) == 'possession' and r['mn'] > 60])
report("领先方 ∩ 分钟>60", [r for r in A if is_leading(r) and r['mn'] > 60])
report("领先方 ∩ 分钟>75", [r for r in A if is_leading(r) and r['mn'] > 75])
report("top10 ∩ possession", [r for r in A if (side_rank(r) or 99) <= 10 and side_style(r) == 'possession'])
report("top10 ∩ possession ∩ 领先", [r for r in A if (side_rank(r) or 99) <= 10 and side_style(r) == 'possession' and is_leading(r)])
report("high_press ∩ possession对阵 ∩ 领先", [r for r in A if matchup(r) == 'high_press x possession' and is_leading(r)])
report("high_press ∩ possession对阵 ∩ 分钟>60", [r for r in A if matchup(r) == 'high_press x possession' and r['mn'] > 60])
report("back-FAVORITE ∩ 领先", [r for r in A if (lambda t,o: t in rank and o in rank and rank[t] < rank[o])(team_of(r), opp_of(r)) and is_leading(r)])
report("back-FAVORITE ∩ 领先 ∩ 分钟>60", [r for r in A if (lambda t,o: t in rank and o in rank and rank[t] < rank[o])(team_of(r), opp_of(r)) and is_leading(r) and r['mn'] > 60])
report("top10 ∩ 领先 ∩ 边际<0.10", [r for r in A if (side_rank(r) or 99) <= 10 and is_leading(r) and r['edge'] is not None and abs(r['edge']) < 0.10])
report("possession ∩ 领先 ∩ 边际0.03-0.10", [r for r in A if side_style(r) == 'possession' and is_leading(r) and r['edge'] is not None and 0.03 <= abs(r['edge']) < 0.10])
report("UNDER ∩ 分钟<15 ∩ 边际0.05-0.10", [r for r in B if r['side'] == 'under' and r['mn'] < 15 and r['edge'] is not None and 0.05 <= abs(r['edge']) < 0.10])
report("UNDER ∩ 分钟<30", [r for r in B if r['side'] == 'under' and r['mn'] < 30])
report("UNDER ∩ 分钟>60", [r for r in B if r['side'] == 'under' and r['mn'] > 60])
report("dominant_attack方", [r for r in A if side_style(r) == 'dominant_attack'])
report("dominant_attack方 ∩ top10", [r for r in A if side_style(r) == 'dominant_attack' and (side_rank(r) or 99) <= 10])

print("\n### 各单族基线 (用于映射与WHY)")
report("top10", [r for r in A if (side_rank(r) or 99) <= 10])
report("possession 风格方", [r for r in A if side_style(r) == 'possession'])
report("high_press 风格方", [r for r in A if side_style(r) == 'high_press'])
report("dominant_attack 风格方", [r for r in A if side_style(r) == 'dominant_attack'])
report("领先方(side-leading)", [r for r in A if is_leading(r)])
report("trailing(side-trailing)", [r for r in A if is_trailing(r)])
report("76-90分钟", [r for r in A if r['mn'] >= 76])
report("possession×possession对阵", [r for r in A if matchup(r) == 'possession x possession'])
report("high_press×possession对阵", [r for r in A if matchup(r) == 'high_press x possession'])
report("UNDER (totals)", [r for r in B if r['side'] == 'under'])

print("\n### 反例/警示族 (低命中,用于分级对照)")
report("direct 风格方", [r for r in A if side_style(r) == 'direct'])
report("high_volume 风格方", [r for r in A if side_style(r) == 'high_volume'])
report("draw 方向", [r for r in A if r['side'] == 'draw'])
report("level(平局态)", [r for r in A if r['hg'] == r['ag'] and r['side'] in ('home','away')])
report("OVER (totals)", [r for r in B if r['side'] == 'over'])
report("边际0.10-0.20", [r for r in A if r['edge'] is not None and 0.10 <= abs(r['edge']) < 0.20])
