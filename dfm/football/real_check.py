"""
football/real_check.py — V4 + V6: does the DFM output relate to REAL football?

All nine simulated matchups are real WC2026 knockout fixtures, all now played.
The box-B sims (and hence the DFM trained on them) predate the real matches, so
this is genuine out-of-sample validation against reality, three ways:

V4  W/D/L Brier (90-minute frame) of the DFM mixture vs real outcomes, against
    (a) market odds (median book, wc.db match_odds), (b) raw sim frequency
    (the 10-15 sims the DFM amplifies), (c) uniform. Plan gate: DFM ≤ raw sims.
V6a Real technical stats (wc.db fixture_stats: possession/shots/on-target/corners/
    xG + 90' goals) placed inside the DFM generated distribution: percentile per
    stat per team, coverage of the central 95% / 80% intervals.
V6b Strength monotonicity: EA FC26 squad rating gap (wc.db fc_player, the same
    data family box B used to build the sims) vs DFM home-minus-away win prob.
V6c Cards: generated yellow/red per match vs the real rate in these 9 matches.

Reads wc.db READ-ONLY. Writes only football/data/real_validation.json.
"""
import json
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(HERE))
from model import load_corpus                                    # noqa: E402
from generate import generate_fixture, wdl_skellam               # noqa: E402
from validate import match_totals                                # noqa: E402

DATA = os.path.join(HERE, 'data')
WCDB = os.path.normpath(os.path.join(HERE, '..', '..', 'prediction_market', 'data', 'wc.db'))

# sim matchup -> (home, away, fixture_api_id, fc26 canonical ids)
FIXTURES = {
    'brazil_vs_japan': ('Brazil', 'Japan', 1562344, 'brazil', 'japan'),
    'cote_divoire_vs_norway': ("Cote d'Ivoire", 'Norway', 1564789, 'cote_divoire', 'norway'),
    'mexico_vs_ecuador': ('Mexico', 'Ecuador', 1567306, 'mexico', 'ecuador'),
    'belgium_vs_senegal': ('Belgium', 'Senegal', 1567308, 'belgium', 'senegal'),
    'spain_vs_austria': ('Spain', 'Austria', 1567311, 'spain', 'austria'),
    'argentina_vs_cape_verde': ('Argentina', 'Cape Verde', 1565179, 'argentina', 'cape_verde'),
    'paraguay_vs_france': ('Paraguay', 'France', 1569870, 'paraguay', 'france'),
    'mexico_vs_england': ('Mexico', 'England', 1570714, 'mexico', 'england'),
    'united_states_vs_belgium': ('United States', 'Belgium', 1570715, 'united_states', 'belgium'),
}


def real_result_90(conn, fid, home_api, away_api):
    """90-minute-frame goals (knockouts can go to ET; sims/DFM are 90' objects)."""
    gh = ga = 0
    for e in conn.execute(
            "SELECT team_api_id, minute, extra, detail, comments FROM fixture_event "
            "WHERE fixture_api_id=? AND type='Goal'", (fid,)):
        if (e['comments'] or '') == 'Penalty Shootout' or (e['detail'] or '') == 'Missed Penalty':
            continue
        if (e['minute'] or 0) > 90:
            continue
        if e['team_api_id'] == home_api:
            gh += 1
        else:
            ga += 1
    return gh, ga


def main(epochs=3000, n_gen=2000, anchor=False):
    conn = sqlite3.connect(WCDB)
    conn.row_factory = sqlite3.Row
    X, meta = load_corpus()
    channels = meta['channels']
    i = {ch: j for j, ch in enumerate(channels)}

    # one shared ensemble on the full corpus, then guided generation per fixture
    print('[real] training shared ensemble + generating 9 fixtures')
    briers = {'dfm': [], 'market': [], 'sims': [], 'uniform': []}
    coverage95, coverage80, percentiles = [], [], {}
    gaps, dfm_edge = [], []
    cards_real = {'yellow': 0, 'red': 0}
    gen_cards = {'yellow': 0.0, 'red': 0.0, 'n': 0}
    rows = []
    cache = None
    for mk, (home, away, fid, fch, fca) in FIXTURES.items():
        out = generate_fixture(home, away, n_samples=n_gen, guided=True,
                               epochs=epochs, seed=11, model_cache=cache,
                               real_anchor=anchor)
        cache = (out['models'], out['scaler'], None)
        wdl = out['wdl']

        frow = conn.execute("SELECT home_api_id, away_api_id, status_short FROM fixture "
                            "WHERE api_id=?", (fid,)).fetchone()
        gh, ga = real_result_90(conn, fid, frow['home_api_id'], frow['away_api_id'])
        onehot = np.array([gh > ga, gh == ga, gh < ga], dtype=float)

        # V4 briers
        odds = conn.execute("SELECT AVG(p_home) h, AVG(p_draw) d, AVG(p_away) a "
                            "FROM match_odds WHERE fixture_api_id=?", (fid,)).fetchone()
        pm = np.array([odds['h'], odds['d'], odds['a']], dtype=float)
        pm = pm / pm.sum()
        sim_scores = np.array([c['score'] for c in meta['conds']
                               if c['home_team'] == home and c['away_team'] == away])
        ps = np.array([(sim_scores[:, 0] > sim_scores[:, 1]).mean(),
                       (sim_scores[:, 0] == sim_scores[:, 1]).mean(),
                       (sim_scores[:, 0] < sim_scores[:, 1]).mean()])
        for name, p in (('dfm', wdl), ('market', pm), ('sims', ps),
                        ('uniform', np.full(3, 1 / 3))):
            briers[name].append(float(((p - onehot) ** 2).sum()))

        # V6a real stats inside generated distribution
        gen_tot = match_totals(out['tensors'], channels)          # (n, 2*C)
        names = [f'{s}_{c}' for s in ('h', 'a') for c in channels]
        col = {nm: j for j, nm in enumerate(names)}
        st = {r['team_api_id']: r for r in conn.execute(
            "SELECT * FROM fixture_stats WHERE fixture_api_id=?", (fid,))}
        checks = []
        for side, api, g90 in (('h', frow['home_api_id'], gh), ('a', frow['away_api_id'], ga)):
            s = st.get(api)
            if s is None:
                continue
            pairs = [('poss_share', (s['possession'] or 0)),
                     ('shots', s['shots_total']), ('shots_on_target', s['shots_on']),
                     ('corners', s['corners']), ('xg', s['xg']), ('goals', g90)]
            for ch, real_v in pairs:
                if real_v is None:
                    continue
                gvals = gen_tot[:, col[f'{side}_{ch}']]
                pct = float((gvals <= float(real_v)).mean())
                percentiles[f'{mk}:{side}:{ch}'] = round(pct, 3)
                checks.append(pct)
        coverage95 += [0.025 <= p <= 0.975 for p in checks]
        coverage80 += [0.10 <= p <= 0.90 for p in checks]

        # V6b strength gap (FC26 top-18 mean overall, the sims' own data family)
        fc = {}
        for cid in (fch, fca):
            r = conn.execute("SELECT AVG(overall) v FROM (SELECT overall FROM fc_player "
                             "WHERE canonical_team_id=? ORDER BY overall DESC LIMIT 18)",
                             (cid,)).fetchone()
            fc[cid] = r['v'] or 0.0
        gaps.append(fc[fch] - fc[fca])
        dfm_edge.append(float(wdl[0] - wdl[2]))

        # V6c cards
        for e in conn.execute("SELECT detail, minute FROM fixture_event "
                              "WHERE fixture_api_id=? AND type='Card'", (fid,)):
            if (e['minute'] or 0) <= 90:
                key = 'red' if 'Red' in (e['detail'] or '') else 'yellow'
                cards_real[key] += 1
        for m in out['matches']:
            for e in m['events']:
                if e['type'] == 'yellow_card':
                    gen_cards['yellow'] += 1
                elif e['type'] == 'red_card':
                    gen_cards['red'] += 1
        gen_cards['n'] += len(out['matches'])

        rows.append({'matchup': mk, 'real_90': f'{gh}-{ga}',
                     'wdl_dfm': wdl.round(3).tolist(), 'wdl_market': pm.round(3).tolist(),
                     'wdl_sims': ps.round(3).tolist(),
                     'fc26_gap': round(gaps[-1], 2)})
        print(f"  {mk:28} real90 {gh}-{ga}  dfm {wdl.round(2)}  mkt {pm.round(2)}  "
              f"sims {ps.round(2)}  fc26gap {gaps[-1]:+.1f}")

    from scipy.stats import spearmanr
    rho, pval = spearmanr(gaps, dfm_edge)
    res = {
        'brier': {k: round(float(np.mean(v)), 4) for k, v in briers.items()},
        'stat_coverage_95': round(float(np.mean(coverage95)), 3),
        'stat_coverage_80': round(float(np.mean(coverage80)), 3),
        'n_stat_checks': len(coverage95),
        'fc26_spearman_rho': round(float(rho), 3), 'fc26_spearman_p': round(float(pval), 4),
        'cards_real_per_match': {k: round(v / len(FIXTURES), 3) for k, v in cards_real.items()},
        'cards_gen_per_match': {k: round(gen_cards[k] / gen_cards['n'], 3)
                                for k in ('yellow', 'red')},
        'percentiles': percentiles, 'fixtures': rows,
    }
    res['real_anchor'] = anchor
    fname = 'real_validation_anchored.json' if anchor else 'real_validation.json'
    json.dump(res, open(os.path.join(DATA, fname), 'w'), indent=1)

    print('\n================ REAL-WORLD ASSOCIATION ================')
    print(f"  V4 Brier (9 real matches, 90'): dfm {res['brier']['dfm']}  "
          f"market {res['brier']['market']}  raw-sims {res['brier']['sims']}  "
          f"uniform {res['brier']['uniform']}")
    print(f"  V4 gate (dfm <= raw-sims): {res['brier']['dfm'] <= res['brier']['sims']}")
    print(f"  V6a real stats inside generated central 95%: {res['stat_coverage_95']:.1%} "
          f"(80%: {res['stat_coverage_80']:.1%}, n={res['n_stat_checks']})")
    print(f"  V6b FC26 gap vs DFM edge: Spearman rho={rho:.3f} (p={pval:.4f})")
    print(f"  V6c cards/match — real: {res['cards_real_per_match']}, "
          f"generated: {res['cards_gen_per_match']}")
    print(f"  -> saved to {os.path.join(DATA, fname)}")


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--epochs', type=int, default=3000)
    ap.add_argument('--gen', type=int, default=2000)
    ap.add_argument('--anchor', action='store_true',
                    help='generate in real-anchor mode (tournament-level intensity rescale)')
    a = ap.parse_args()
    main(epochs=a.epochs, n_gen=a.gen, anchor=a.anchor)
