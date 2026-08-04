"""compare_real.py — the re-run 5000-sample production vs the REAL 9 completed matches,
per stat, to surface what's worth improving.

Reads the latest production snapshot (production_runs/) and the real results from wc.db
(READ-ONLY): 90-minute goals + fixture_stats (possession/shots/on-target/corners/xG).
For every matchup and every stat, compares the real value against the generated
distribution (percentile, and whether it's inside the central 80/95%), and aggregates
the DIRECTION of the miss (generated too high / too low) — that direction is the
actionable signal for what to fix (mean anchor, dispersion, a missing effect).
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, 'production_runs')
WCDB = os.path.normpath(os.path.join(HERE, '..', '..', 'prediction_market', 'data', 'wc.db'))

# matchup key -> (home, away, wc.db fixture_api_id)
FIX = {
    'brazil_vs_japan': ('Brazil', 'Japan', 1562344),
    'cote_divoire_vs_norway': ("Cote d'Ivoire", 'Norway', 1564789),
    'mexico_vs_ecuador': ('Mexico', 'Ecuador', 1567306),
    'belgium_vs_senegal': ('Belgium', 'Senegal', 1567308),
    'spain_vs_austria': ('Spain', 'Austria', 1567311),
    'argentina_vs_cape_verde': ('Argentina', 'Cape Verde', 1565179),
    'paraguay_vs_france': ('Paraguay', 'France', 1569870),
    'mexico_vs_england': ('Mexico', 'England', 1570714),
    'united_states_vs_belgium': ('United States', 'Belgium', 1570715),
}
# stat -> (channel, aggregation) ; agg='sum' counts, 'mean' rates
STАTS = None
STATMAP = {
    'goals': ('goals', 'sum'),
    'shots': ('shots', 'sum'),
    'shots_on_target': ('shots_on_target', 'sum'),
    'corners': ('corners', 'sum'),
    'xg': ('xg', 'sum'),
    'possession_pct': ('poss_share', 'mean_pct'),
}


def latest_manifest():
    ms = sorted(glob.glob(os.path.join(RUNS, 'manifest*.json')))
    return json.load(open(ms[-1]))


def real_result_90(conn, fid, home_api, away_api):
    gh = ga = 0
    for e in conn.execute("SELECT team_api_id, minute, extra, detail, comments FROM fixture_event "
                          "WHERE fixture_api_id=? AND type='Goal'", (fid,)):
        if (e['comments'] or '') == 'Penalty Shootout' or (e['detail'] or '') == 'Missed Penalty':
            continue
        if (e['minute'] or 0) > 90:
            continue
        gh += 1 if e['team_api_id'] == home_api else 0
        ga += 1 if e['team_api_id'] != home_api else 0
    return gh, ga


def gen_side_series(npz, channels, ch, agg):
    """(n_samples, 2) generated per-side match aggregate for a channel."""
    X = npz['tensors']                                  # (n, seg, 2, C)
    j = channels.index(ch)
    if agg == 'sum':
        return X[:, :, :, j].sum(1)                     # (n, 2)
    means = X[:, :, :, j].mean(1)
    return means * (100.0 if agg == 'mean_pct' else 1.0)


def main():
    conn = sqlite3.connect(f'file:{os.path.abspath(WCDB)}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    man = latest_manifest()
    ts = man['ts']
    print(f'[compare] production snapshot {ts} vs {len(FIX)} real completed matches\n')

    rows = []
    dir_by_stat = defaultdict(list)         # stat -> list of signed percentile-centered miss
    for mk, (home, away, fid) in FIX.items():
        npz = np.load(os.path.join(RUNS, f'dfm_{mk}_{ts}_samples.npz'), allow_pickle=True)
        channels = list(npz['channels'])
        f = conn.execute("SELECT home_api_id, away_api_id FROM fixture WHERE api_id=?",
                         (fid,)).fetchone()
        gh, ga = real_result_90(conn, fid, f['home_api_id'], f['away_api_id'])
        st = {r['team_api_id']: r for r in conn.execute(
            "SELECT * FROM fixture_stats WHERE fixture_api_id=?", (fid,))}
        real = {'goals': (gh, ga)}
        for tid_key, api in (('home', f['home_api_id']), ('away', f['away_api_id'])):
            s = st.get(api)
            if s:
                real.setdefault('shots', [None, None])
        # assemble real per-side dict
        rmap = {}
        for name, (ch, agg) in STATMAP.items():
            if name == 'goals':
                rmap[name] = (gh, ga)
                continue
            h = st.get(f['home_api_id']); a = st.get(f['away_api_id'])
            key = {'shots': 'shots_total', 'shots_on_target': 'shots_on',
                   'corners': 'corners', 'xg': 'xg', 'possession_pct': 'possession'}[name]
            hv = h[key] if h else None
            av = a[key] if a else None
            if name == 'possession_pct' and hv is not None:
                hv, av = hv * 100, (av * 100 if av is not None else None)
            rmap[name] = (hv, av)

        for name, (ch, agg) in STATMAP.items():
            g = gen_side_series(npz, channels, ch, agg)         # (n,2)
            for si, side in enumerate(('home', 'away')):
                rv = rmap[name][si]
                if rv is None:
                    continue
                col = g[:, si]
                pct = float((col <= rv).mean())
                inside95 = 0.025 <= pct <= 0.975
                rows.append({'matchup': mk, 'stat': name, 'side': side,
                             'real': round(float(rv), 2), 'gen_p50': round(float(np.median(col)), 2),
                             'pct': round(pct, 3), 'in95': inside95})
                dir_by_stat[name].append(pct)

    # ---- per-stat verdict: coverage + systematic direction ----
    print(f"{'stat':16} {'n':>3} {'in95%':>6} {'median-pctile':>13}  read")
    for name in STATMAP:
        ps = np.array(dir_by_stat[name])
        if len(ps) == 0:
            continue
        cov = float(((ps >= 0.025) & (ps <= 0.975)).mean())
        medp = float(np.median(ps))
        # median percentile >0.5 → real sits ABOVE the generated median → generator too LOW
        read = ('gen ≈ real' if 0.35 <= medp <= 0.65 else
                ('gen too LOW (real above)' if medp > 0.65 else 'gen too HIGH (real below)'))
        print(f'  {name:14} {len(ps):3d} {cov:6.0%} {medp:13.2f}  {read}')

    out = os.path.join(HERE, 'data', 'compare_real.json')
    json.dump({'ts': ts, 'rows': rows,
               'per_stat_median_pctile': {k: round(float(np.median(v)), 3)
                                          for k, v in dir_by_stat.items()}},
              open(out, 'w'), indent=1)
    print(f'\nsaved → {out}')


if __name__ == '__main__':
    main()
