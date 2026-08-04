"""
football/validate.py — the plan's hard gates.

V1  Leave-one-fixture-out (LOFO): train without a matchup, generate it UNGUIDED
    (pure covariate transfer — this is exactly the zero-sim-fixture path), compare
    against the held-out sims. Gate: beat the pooled-corpus baseline on W/D/L
    total-variation and on per-channel Wasserstein-1. The fixture's tactics vector
    is legitimately available (the box-A brain emits match_config BEFORE any sim),
    so conditioning on it is not leakage.
V2  Guidance fidelity: guided generation must put key stat means inside the
    fixture sims' bootstrap 90% CI, and cover all observed scorelines.
V3  Consistency: hard invariants on every generated sample (analytic, must be 100%).
V5  Realism bands (SFAS paper Table 1, real-football typical ranges): generated
    full-match team stats must land in-band at least as often as the engine corpus
    itself does (minus a small tolerance) — the generator must not be LESS realistic
    than the sims it amplifies. Bands are "typical", not absolute (the paper itself
    documents legitimate shot-count excursions), so the gate is relative to corpus.

Outputs football/data/validation.json + a printed verdict table.
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(HERE))
from model import load_corpus, build_training_arrays, train_conditional, transform  # noqa: E402
from generate import generate_fixture, to_tensor_space                              # noqa: E402

DATA = os.path.join(HERE, 'data')


# SFAS paper Table 1 realism bands (per team-match). Only the stats our channels
# can express; save%/sequences/xG/recovery are not in the segment tensor.
REALISM_BANDS = {
    'possession_pct': (30.0, 70.0),      # poss_share mean * 100
    'shots': (5, 20),
    'on_target_pct': (25.0, 50.0),       # shots_on_target / shots * 100
    'pass_completion_pct': (50.0, 90.0),
    'offsides': (0, 5),
    'goals': (0, 7),
}


def band_stats(X, channels):
    """(n, seg, 2, C) -> dict of per-team-match stat arrays matching REALISM_BANDS."""
    i = {ch: j for j, ch in enumerate(channels)}
    sums, means = X.sum(1), X.mean(1)
    shots = sums[..., i['shots']].ravel()
    on_t = sums[..., i['shots_on_target']].ravel()
    return {
        'possession_pct': means[..., i['poss_share']].ravel() * 100,
        'shots': shots,
        'on_target_pct': np.where(shots > 0.5, on_t / np.maximum(shots, 1e-9) * 100, np.nan),
        'pass_completion_pct': means[..., i['pass_comp_rate']].ravel() * 100,
        'offsides': sums[..., i['offsides']].ravel(),
        'goals': sums[..., i['goals']].ravel(),
    }


def in_band_rates(X, channels):
    out = {}
    for k, v in band_stats(X, channels).items():
        lo, hi = REALISM_BANDS[k]
        v = v[~np.isnan(v)]
        out[k] = float(((v >= lo) & (v <= hi)).mean()) if len(v) else float('nan')
    return out


def wdl_of(scores: np.ndarray) -> np.ndarray:
    return np.array([(scores[:, 0] > scores[:, 1]).mean(),
                     (scores[:, 0] == scores[:, 1]).mean(),
                     (scores[:, 0] < scores[:, 1]).mean()])


def tv(p, q):
    return 0.5 * float(np.abs(np.asarray(p) - np.asarray(q)).sum())


def w1(a, b):
    """1-Wasserstein between two empirical 1-d samples."""
    qs = np.linspace(0.02, 0.98, 49)
    return float(np.mean(np.abs(np.quantile(a, qs) - np.quantile(b, qs))))


def match_totals(X, channels):
    """(n, seg, 2, C) -> (n, 2*C) full-match totals (counts) / means (rates)."""
    sums = X.sum(1)
    means = X.mean(1)
    out = np.empty((X.shape[0], 2, len(channels)))
    for i, ch in enumerate(channels):
        rate = ch in ('poss_share', 'pass_comp_rate', 'field_tilt', 'save_pct', 'recovery_sec')
        out[:, :, i] = means[:, :, i] if rate else sums[:, :, i]
    return out.reshape(X.shape[0], -1)


def main(n_gen=1000, epochs=3000, arch='factor', lofo_subset=None):
    X, meta = load_corpus()
    channels = meta['channels']
    matchups = sorted({c['sim_dir'].split('/')[0] for c in meta['conds']})
    if lofo_subset:
        matchups = matchups[:lofo_subset]
    pair_of = {}
    for c in meta['conds']:
        pair_of[c['sim_dir'].split('/')[0]] = (c['home_team'], c['away_team'])

    corpus_scores = np.array([c['score'] for c in meta['conds']])
    report = {'lofo': [], 'guided': [], 'consistency': None}

    print(f'=== V1 LOFO over {len(matchups)} matchups (unguided transfer) ===')
    for mk in matchups:
        home, away = pair_of[mk]
        held_idx = [i for i, c in enumerate(meta['conds']) if c['sim_dir'].startswith(mk)]
        held_scores = corpus_scores[held_idx]
        held_tot = match_totals(X[held_idx], channels)

        out = generate_fixture(home, away, n_samples=n_gen, guided=False,
                               exclude_matchups=(mk,), epochs=epochs, seed=1, arch=arch)
        gen_tot = match_totals(out['tensors'], channels)

        # pooled baseline: every other sim, as-is
        rest_idx = [i for i in range(len(meta['conds'])) if i not in held_idx]
        pool_scores = corpus_scores[rest_idx]
        pool_tot = match_totals(X[rest_idx], channels)

        tv_model = tv(out['wdl'], wdl_of(held_scores))
        tv_pool = tv(wdl_of(pool_scores), wdl_of(held_scores))
        w1_model = np.median([w1(gen_tot[:, j], held_tot[:, j]) /
                              (held_tot[:, j].std() + 1e-9) for j in range(gen_tot.shape[1])])
        w1_pool = np.median([w1(pool_tot[:, j], held_tot[:, j]) /
                             (held_tot[:, j].std() + 1e-9) for j in range(pool_tot.shape[1])])
        # self-noise floor: distance between random halves of the held sims themselves —
        # with 10-15 target sims this is the resolution limit of the whole evaluation;
        # plan gate: model ≤ 1.5 × floor
        rng_f = np.random.default_rng(0)
        f_tv, f_w1 = [], []
        for _ in range(300):
            p = rng_f.permutation(len(held_idx))
            a, b = p[:len(held_idx) // 2], p[len(held_idx) // 2:]
            f_tv.append(tv(wdl_of(held_scores[a]), wdl_of(held_scores[b])))
            f_w1.append(np.median([w1(held_tot[a, j], held_tot[b, j]) /
                                   (held_tot[:, j].std() + 1e-9)
                                   for j in range(held_tot.shape[1])]))
        floor_tv, floor_w1 = float(np.mean(f_tv)), float(np.mean(f_w1))
        row = {'matchup': mk, 'n_held': len(held_idx),
               'wdl_gen': out['wdl'].round(3).tolist(),
               'wdl_held': wdl_of(held_scores).round(3).tolist(),
               'tv_model': round(tv_model, 3), 'tv_pool': round(tv_pool, 3),
               'w1_model': round(float(w1_model), 3), 'w1_pool': round(float(w1_pool), 3),
               'tv_floor': round(floor_tv, 3), 'w1_floor': round(floor_w1, 3),
               'win_tv': tv_model < tv_pool, 'win_w1': float(w1_model) < float(w1_pool),
               'floor_tv_ok': tv_model <= 1.5 * floor_tv,
               'floor_w1_ok': float(w1_model) <= 1.5 * floor_w1}
        report['lofo'].append(row)
        print(f"  {mk:28} TV {tv_model:.3f} pool {tv_pool:.3f} floor {floor_tv:.3f} "
              f"{'WIN ' if row['win_tv'] else 'lose'}/{'F' if row['floor_tv_ok'] else '-'} | "
              f"W1 {w1_model:.3f} pool {w1_pool:.3f} floor {floor_w1:.3f} "
              f"{'WIN' if row['win_w1'] else 'lose'}/{'F' if row['floor_w1_ok'] else '-'}")

    tv_wins = sum(r['win_tv'] for r in report['lofo'])
    w1_wins = sum(r['win_w1'] for r in report['lofo'])
    tv_floor = sum(r['floor_tv_ok'] for r in report['lofo'])
    w1_floor = sum(r['floor_w1_ok'] for r in report['lofo'])
    n = len(report['lofo'])
    print(f'  --> vs pool: TV {tv_wins}/{n}, W1 {w1_wins}/{n} (gate: majority) | '
          f'within 1.5x self-noise floor: TV {tv_floor}/{n}, W1 {w1_floor}/{n}')

    print('\n=== V2 guidance fidelity (2 fixtures) ===')
    for mk in (matchups[0], matchups[-1]):
        home, away = pair_of[mk]
        held_idx = [i for i, c in enumerate(meta['conds']) if c['sim_dir'].startswith(mk)]
        held_tot = match_totals(X[held_idx], channels)
        out = generate_fixture(home, away, n_samples=n_gen, guided=True,
                               epochs=epochs, seed=2, arch=arch)
        gen_tot = match_totals(out['tensors'], channels)
        # bootstrap 90% CI of the held-out means, count key stats inside
        key = [j for j, name in enumerate(
            [f'{s}_{c}' for s in ('h', 'a') for c in channels])
            if any(k in name for k in ('goals', 'shots', 'poss', 'passes'))]
        rng = np.random.default_rng(0)
        inside = 0
        for j in key:
            boots = [held_tot[rng.integers(0, len(held_idx), len(held_idx)), j].mean()
                     for _ in range(500)]
            lo, hi = np.quantile(boots, [0.05, 0.95])
            pad = 0.05 * max(abs(lo), abs(hi), 1e-9)
            if lo - pad <= gen_tot[:, j].mean() <= hi + pad:
                inside += 1
        held_lines = {tuple(s) for s in corpus_scores[held_idx]}
        gen_lines = {tuple(s) for s in out['scores']}
        covered = len(held_lines & gen_lines) / len(held_lines)
        row = {'matchup': mk, 'means_in_ci': f'{inside}/{len(key)}',
               'scoreline_coverage': round(covered, 3)}
        report['guided'].append(row)
        print(f"  {mk:28} key-stat means in 90% CI: {inside}/{len(key)}; "
              f"observed scorelines covered: {covered:.0%}")

    print('\n=== V3 consistency invariants ===')
    Xg = out['tensors']
    i = {ch: j for j, ch in enumerate(channels)}
    ok_ot = np.all(Xg[..., i['shots_on_target']] <= Xg[..., i['shots']] + 1e-9)
    ok_g = np.all(Xg[..., i['goals']] <= Xg[..., i['shots_on_target']] + 1e-6)
    ok_p = np.allclose(Xg[..., 0, i['poss_share']] + Xg[..., 1, i['poss_share']], 1.0)
    report['consistency'] = {'on_target<=shots': bool(ok_ot),
                             'goals<=on_target': bool(ok_g), 'poss_sums_to_1': bool(ok_p)}
    print(f'  {report["consistency"]}')

    print('\n=== V5 realism bands (SFAS Table 1; gate: gen in-band rate >= corpus - 10pp) ===')
    corpus_rates = in_band_rates(X, channels)
    gen_rates = in_band_rates(Xg, channels)
    report['bands'] = {}
    for k in REALISM_BANDS:
        cr, gr = corpus_rates[k], gen_rates[k]
        ok = gr >= cr - 0.10
        report['bands'][k] = {'corpus': round(cr, 3), 'generated': round(gr, 3), 'pass': bool(ok)}
        print(f'  {k:22} corpus {cr:5.1%}  generated {gr:5.1%}  {"PASS" if ok else "FAIL"}')

    json.dump(report, open(os.path.join(DATA, 'validation.json'), 'w'), indent=1)
    print(f'\nsaved -> {os.path.join(DATA, "validation.json")}')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--gen', type=int, default=1000)
    ap.add_argument('--epochs', type=int, default=3000)
    ap.add_argument('--arch', choices=('factor', 'plain'), default='factor',
                    help="'plain' = unstructured conditional MLP ablation")
    ap.add_argument('--lofo-subset', type=int, default=None,
                    help='only run the first N matchups (quick ablation runs)')
    a = ap.parse_args()
    main(n_gen=a.gen, epochs=a.epochs, arch=a.arch, lofo_subset=a.lofo_subset)
