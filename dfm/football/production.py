"""
football/production.py — the official box-B → DFM production run.

One command turns the current synced box-B corpus into official DFM results for
EVERY matchup, written to the FIXED output directory with a searchable naming
scheme (repo convention: *_YYYYMMDD_HHMMSS, plus the box-B matchup key):

    dfm/football/production_runs/
      manifest_<ts>.json                     # run manifest: corpus fingerprint,
                                             #   per-matchup sim counts, params
      dfm_<matchup>_<ts>.json                # official per-matchup result
      dfm_<matchup>_<ts>_samples.npz         # the generated samples themselves
                                             #   (engine-faithful tensors + scores)

<matchup> is exactly the box-B games/ directory key (e.g. brazil_vs_japan), <ts>
is shared across one run — so files are findable by either fixture or time, and
`sorted(glob)[-1]` / mtime both give the latest run (same pattern DailySignal
uses for walk-forward outputs).

Each JSON carries BOTH views of the same generated samples:
  * engine_faithful — the DFM amplification of the sims as-is
  * real_anchored   — tournament-level intensity rescale (goals/corners/shots/xG)
                      + calibrated cards; recommended for real-match prediction
Only this script writes to production_runs/ — test/validation scripts never do.

Usage:
    python production.py [--n 5000] [--epochs 3000] [--matchup brazil_vs_japan]
Re-run after every new box-B sync (extract.py first) — a new timestamp is a new
official snapshot; old snapshots are kept for audit.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(HERE))
from model import load_corpus                                     # noqa: E402
from generate import (generate_fixture, wdl_skellam, realize_events,  # noqa: E402
                      build_scorer_table, REAL_ANCHOR_SCALE,
                      REAL_YELLOW_PER_MATCH, REAL_RED_PER_MATCH)

OUT = os.path.join(HERE, 'production_runs')
DATA = os.path.join(HERE, 'data')
# channels reconstructed as a LEVEL (mean over segments), not a per-segment SUM
RATE_CHANNELS = ('poss_share', 'pass_comp_rate', 'field_tilt', 'save_pct', 'recovery_sec')


def corpus_fingerprint(X, meta):
    h = hashlib.sha1(X.tobytes()).hexdigest()[:12]
    per = {}
    for c in meta['conds']:
        per[c['sim_dir'].split('/')[0]] = per.get(c['sim_dir'].split('/')[0], 0) + 1
    return {'sha1': h, 'n_sims': int(len(X)), 'shape': list(X.shape), 'per_matchup': per}


def summarize(Xg, matches, channels):
    """Official per-mode summary from generated tensors + realized matches."""
    wdl = wdl_skellam(Xg, channels)
    scores = np.array([m['score'] for m in matches])
    uniq, cnt = np.unique([f'{a}-{b}' for a, b in scores], return_counts=True)
    lines = sorted(zip(cnt.tolist(), uniq.tolist()), reverse=True)[:10]
    sums, means = Xg.sum(1), Xg.mean(1)
    stats = {}
    for j, ch in enumerate(channels):
        v = means[:, :, j] if ch in RATE_CHANNELS else sums[:, :, j]
        stats[ch] = {side: {'mean': round(float(v[:, s].mean()), 3),
                            'p5': round(float(np.quantile(v[:, s], 0.05)), 3),
                            'p50': round(float(np.quantile(v[:, s], 0.50)), 3),
                            'p95': round(float(np.quantile(v[:, s], 0.95)), 3)}
                     for s, side in ((0, 'home'), (1, 'away'))}
    n = len(matches)
    cards = {'yellow': round(sum(1 for m in matches for e in m['events']
                                 if e['type'] == 'yellow_card') / n, 3),
             'red': round(sum(1 for m in matches for e in m['events']
                              if e['type'] == 'red_card') / n, 3)}
    return {'wdl': {'home': round(float(wdl[0]), 4), 'draw': round(float(wdl[1]), 4),
                    'away': round(float(wdl[2]), 4)},
            'scoreline_top10': [{'score': s, 'pct': round(c / n, 4)} for c, s in lines],
            'avg_goals': f"{sums[:, 0, channels.index('goals')].mean():.2f}-"
                         f"{sums[:, 1, channels.index('goals')].mean():.2f}",
            'cards_per_match': cards, 'stats': stats,
            'events_sample': [m['events'] for m in matches[:2]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--n', type=int, default=5000)
    ap.add_argument('--epochs', type=int, default=3000)
    ap.add_argument('--matchup', default=None, help='single matchup key (default: all)')
    ap.add_argument('--keep-old', action='store_true',
                    help='keep superseded snapshots (default: prune — only the latest '
                         'full snapshot lives in production_runs/, no overlap)')
    args = ap.parse_args()

    X, meta = load_corpus()
    channels = meta['channels']
    fp = corpus_fingerprint(X, meta)
    matchups = sorted(fp['per_matchup'])
    if args.matchup:
        if args.matchup not in fp['per_matchup']:
            raise SystemExit(f'unknown matchup {args.matchup}; have: {matchups}')
        matchups = [args.matchup]
    pair_of = {c['sim_dir'].split('/')[0]: (c['home_team'], c['away_team'])
               for c in meta['conds']}

    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(OUT, exist_ok=True)
    print(f'[prod] corpus {fp["sha1"]} ({fp["n_sims"]} sims, {len(fp["per_matchup"])} '
          f'matchups) -> {len(matchups)} matchup(s), n={args.n}, ts={ts}')

    events_json = json.load(open(os.path.join(DATA, 'events.json')))
    manifest = {'ts': ts, 'corpus': fp, 'n_samples': args.n, 'epochs': args.epochs,
                'yellow_per_match': round(REAL_YELLOW_PER_MATCH, 3),
                'red_per_match': round(REAL_RED_PER_MATCH, 3),
                'real_anchor_scale': {k: round(v, 3) for k, v in REAL_ANCHOR_SCALE.items()},
                'files': []}
    cache = None
    for mk in matchups:
        home, away = pair_of[mk]
        n_src = fp['per_matchup'][mk]
        out = generate_fixture(home, away, n_samples=args.n, guided=True,
                               epochs=args.epochs, seed=42, model_cache=cache)
        cache = (out['models'], out['scaler'], None)
        engine = summarize(out['tensors'], out['matches'], channels)

        # real-anchored view of the SAME samples (post-transform + re-realized events)
        Xa = out['tensors'].copy()
        for ch, sc in REAL_ANCHOR_SCALE.items():
            Xa[..., channels.index(ch)] *= sc
        iot, ish, igl = (channels.index('shots_on_target'), channels.index('shots'),
                         channels.index('goals'))
        Xa[..., iot] = np.maximum(Xa[..., iot], Xa[..., igl])
        Xa[..., ish] = np.maximum(Xa[..., ish], Xa[..., iot])
        rng = np.random.default_rng(42)
        scorer = build_scorer_table(events_json, meta['conds'], home, away)
        corpus_red = float(X[..., channels.index('red_cards')].sum()) / len(X)
        card_rates = {'red_scale': REAL_RED_PER_MATCH / max(corpus_red, 1e-9),
                      'yellow_per_match': REAL_YELLOW_PER_MATCH}
        n_pen = sum(1 for evs in events_json for e in evs if e['type'] == 'penalty')
        n_fk = sum(1 for evs in events_json for e in evs if e['type'] == 'freekick')
        matches_a = [realize_events(Xa[j], channels, 90.0 / meta['segments'], rng,
                                    scorer, card_rates=card_rates,
                                    penalty_ratio=n_pen / max(n_fk, 1))
                     for j in range(len(Xa))]
        anchored = summarize(Xa, matches_a, channels)

        doc = {'matchup': mk, 'home': home, 'away': away, 'ts': ts,
               'n_source_sims': n_src, 'n_samples': args.n,
               'corpus_sha1': fp['sha1'], 'guided': out['guided'],
               'engine_faithful': engine, 'real_anchored': anchored}
        base = f'dfm_{mk}_{ts}'
        json.dump(doc, open(os.path.join(OUT, base + '.json'), 'w'),
                  ensure_ascii=False, indent=1)
        np.savez_compressed(os.path.join(OUT, base + '_samples.npz'),
                            tensors=out['tensors'].astype(np.float32),
                            scores=np.array([m['score'] for m in out['matches']],
                                            dtype=np.int16),
                            channels=np.array(channels))
        manifest['files'].append(base + '.json')
        w, d_, l = engine['wdl'].values()
        wa, da, la = anchored['wdl'].values()
        print(f'  {mk:28} src={n_src:2d} sims  engine W/D/L {w:.2f}/{d_:.2f}/{l:.2f}  '
              f'anchored {wa:.2f}/{da:.2f}/{la:.2f}')

    json.dump(manifest, open(os.path.join(OUT, f'manifest_{ts}.json'), 'w'), indent=1)
    # retention: a full run fully supersedes older snapshots — prune them so the
    # directory always holds exactly ONE authoritative snapshot (a --matchup partial
    # run never prunes: it complements the existing full snapshot)
    if not args.keep_old and not args.matchup:
        stale = [f for f in os.listdir(OUT) if ts not in f]
        for f in stale:
            os.remove(os.path.join(OUT, f))
        if stale:
            print(f'[prod] pruned {len(stale)} files from superseded snapshot(s)')
    print(f'[prod] {len(matchups)} matchup(s) -> {OUT}/  (manifest_{ts}.json)')


if __name__ == '__main__':
    main()
