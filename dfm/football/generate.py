"""
football/generate.py — sample full matches from the conditional DFM.

Two modes:
  * prior:    condition vector only (works for pairings that were never simmed —
              the cross-fixture transfer path; covariates carry the signal)
  * guided:   moment-matching guidance toward the fixture's own sims' empirical
              moments — the "amplify 10-15 sims into thousands" path. The guidance
              potential is Gaussian, so its score is analytic (no extra network):
              g(x) = -w * (x - m_fix) / s_fix^2, annealed in as t -> t0.

Event layer: the generated segment tensor gives per-segment intensities for
goals/corners/freekicks/offsides; discrete events + minute stamps come from
Poisson realization within each segment, scorers from the corpus' per-team
scorer distribution (Laplace-smoothed). Consistency is enforced analytically
(on-target <= shots, goals <= on-target, possession shares renormalized).
"""
import json
import os
import sys

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(HERE))
from config import DIFFUSION_CONFIG                                   # noqa: E402
from model import (load_corpus, build_training_arrays, train_conditional,  # noqa: E402
                   cond_vector, transform, inverse_transform, DEVICE)

DATA = os.path.join(HERE, 'data')

# Real-tournament card rates, used to calibrate the event layer (the engine's card
# machinery fires ~12x too often: corpus 1.26 reds/match vs real ~0.1).
#   WC2022 Qatar: 64 matches, 214 yellow, 4 red  -> 3.34 / 0.0625 per match
#   WC2026 (93 finished, prediction_market wc.db fixture_event): 238 yellow, 13 red
#                                                 -> 2.56 / 0.140 per match
# Yellows: the WC2022 blend (2.88) overshoots — this tournament books fewer
# (238/93 = 2.56/match) and the user wants < 2.6, so anchor yellows to the
# CURRENT tournament only. Reds keep the two-tournament blend (0.108) — at
# 0.06-0.14 per match a single tournament is too thin to stand alone.
REAL_YELLOW_PER_MATCH = 238 / 93                    # ~2.56
REAL_RED_PER_MATCH = (4 + 13) / (64 + 93)           # ~0.108

# Optional real-anchor mode (generate_fixture(real_anchor=True) / --real-anchor):
# V6a showed real match stats sit systematically ABOVE the engine's distribution
# (the engine's known low-goal bias — sims ~1.95 goals/match vs real WC2026 2.93).
# Tournament-level intensity ratios (94 real matches vs 99-sim corpus — aggregate
# anchors, NOT fitted to any specific fixture):
REAL_ANCHOR_SCALE = {
    'goals': 2.93 / 1.95,       # 1.50
    'xg': 2.93 / 1.95,          # keep goals-xG consistency
    'corners': 8.72 / 5.20,     # 1.68
    'shots': 23.3 / 20.97,      # 1.11
}

# Corner-specific realism (aligned with the live corner signal, model/inplay_corners):
#   * STATE COUPLING (default ON). A trailing side pushes → more corners; a settled
#     blow-out → fewer. The DFM captures this implicitly (corners diffused jointly with
#     goals), but we ALSO apply it explicitly from each generated match's running score,
#     matching inplay_corners.COUPLE_ONE_GOAL / COUPLE_BLOWOUT.
#   * DISPERSION (default OFF — measured redundant). Real WC match-total corners are
#     overdispersed (mean 8.66, var 12.64 → var/mean ≈ 1.46). MEASUREMENT: the DFM's own
#     diffusion-varying λ ALREADY produces var/mean ≈ 1.59 (a compound-Poisson mixture),
#     essentially the real index — adding a per-match Gamma multiplier (Negative Binomial,
#     as the live signal uses) OVERSHOOTS to ≈3.15. So the extra NegBin is exposed but off
#     by default; it exists for a future engine change that tightens the sims below real.
CORNER_DISPERSION_K = 6.0      # Gamma shape when corner_extra_dispersion=True
CORNER_COUPLE_ONE_GOAL = 1.10
CORNER_COUPLE_BLOWOUT = 0.92


def reverse_sample(model, c_z, n_samples, d, guidance=None, noise_steps=180, seed=0):
    """Euler-Maruyama reverse SDE with per-sample conditions + optional guidance score,
    finished with a Tweedie denoise at t0. c_z: (cond_dim,) or (n_samples, cond_dim)."""
    g = torch.Generator(device='cpu').manual_seed(seed)
    t0, T = DIFFUSION_CONFIG['t0'], DIFFUSION_CONFIG['T']
    x = torch.randn(n_samples, d, generator=g).to(DEVICE)
    c = torch.tensor(np.asarray(c_z), dtype=torch.float32, device=DEVICE)
    if c.dim() == 1:
        c = c.expand(n_samples, -1)
    times = torch.linspace(T, t0, noise_steps)
    dt = (T - t0) / (noise_steps - 1)
    with torch.no_grad():
        for i, t in enumerate(times[:-1]):
            tt = t.expand(n_samples).to(DEVICE)
            score = model(x, tt, c)
            if guidance is not None:
                # anneal guidance in as the sample takes shape (alpha_t -> 1)
                w_t = float(torch.exp(-0.5 * t))
                score = score + w_t * guidance(x)
            drift = 0.5 * x + score
            noise = torch.randn(x.shape, generator=g).to(DEVICE)
            x = x + drift * dt + np.sqrt(dt) * noise
        # Tweedie at t0
        tt = times[-1].expand(n_samples).to(DEVICE)
        alpha0 = float(np.exp(-0.5 * t0))
        h0 = 1 - alpha0 ** 2
        score = model(x, tt, c)
        if guidance is not None:
            score = score + guidance(x)
        x = (x + h0 * score) / alpha0
    return x.cpu().numpy()


def make_guidance(fix_Z_flat, scaler, weight=0.6):
    """Gaussian moment-matching guidance from the fixture's own sims (z-space)."""
    m = (fix_Z_flat.mean(0) - scaler['mu']) / scaler['sd']
    v = (fix_Z_flat.std(0) / scaler['sd']) ** 2 + 0.25        # variance floor: don't collapse
    m_t = torch.tensor(m, dtype=torch.float32, device=DEVICE)
    v_t = torch.tensor(v, dtype=torch.float32, device=DEVICE)

    def guidance(x):
        return -weight * (x - m_t) / v_t
    return guidance


def make_regressed_guidance(Z, C, c_z_rows, weight=0.4, lam=3.0):
    """Transfer-mode guidance: the score net's conditional mean shrinks toward the
    corpus average at n~200 (symptom: favorites' win share halved, draw share flat
    ~0.42 across all fixtures). Fix: ridge-regress the z-space match vector on the
    condition vector using TRAINING fixtures only (no leakage), and guide each sample
    toward its covariate-predicted mean with the ridge's per-dim residual variance.
    The exact analogue of own-sims moment guidance, with regressed moments."""
    d_c = C.shape[1]
    W = np.linalg.solve(C.T @ C + lam * np.eye(d_c), C.T @ Z)      # (d_c, d)
    resid = Z - C @ W
    v = resid.var(0) + 0.25                                        # same floor as guided mode
    m_rows = c_z_rows @ W                                          # (n_samples, d)
    m_t = torch.tensor(m_rows, dtype=torch.float32, device=DEVICE)
    v_t = torch.tensor(v, dtype=torch.float32, device=DEVICE)

    def factory(lo, hi):
        m_slice = m_t[lo:hi]

        def guidance(x):
            return -weight * (x - m_slice) / v_t
        return guidance
    return factory


def to_tensor_space(z_flat, scaler):
    """z-space samples -> raw segment tensors (n, seg, 2, C), consistency enforced."""
    channels, n_seg = scaler['channels'], scaler['segments']
    Z = z_flat * scaler['sd'] + scaler['mu']
    X = inverse_transform(Z.reshape(len(Z), n_seg, 2, len(channels)), channels)
    i = {ch: j for j, ch in enumerate(channels)}
    # possession shares of the two sides sum to 1
    tot = X[..., 0, i['poss_share']] + X[..., 1, i['poss_share']] + 1e-9
    X[..., 0, i['poss_share']] /= tot
    X[..., 1, i['poss_share']] /= tot
    # on-target <= shots (as intensities)
    X[..., i['shots_on_target']] = np.minimum(X[..., i['shots_on_target']], X[..., i['shots']])
    # goal intensity <= on-target intensity
    X[..., i['goals']] = np.minimum(X[..., i['goals']], X[..., i['shots_on_target']] + 1e-9)
    return X


def realize_events(X, channels, seg_minutes=10.0, rng=None, scorer_table=None,
                   teams=('home', 'away'), card_rates=None, penalty_ratio=0.0,
                   corner_realism=True, corner_extra_dispersion=False):
    """One generated tensor -> discrete match: score, minute-stamped events.

    card_rates (default: real-tournament calibration; pass False for engine-faithful):
      * red cards: the tensor's red_cards intensity is scaled by
        REAL_RED_PER_MATCH / corpus-reds-per-match (shape from the model, level real)
      * yellow cards: unobservable in the engine output, so realized as a pure
        Poisson overlay at the real per-match rate, allocated by each side's share
        of fouls committed (~= freekicks WON by the opponent)
    """
    rng = rng or np.random.default_rng()
    i = {ch: j for j, ch in enumerate(channels)}
    if card_rates is None:
        card_rates = {'red_scale': 1.0, 'yellow_per_match': REAL_YELLOW_PER_MATCH}
    events = []
    goals = [0, 0]
    fk_total = max(float(X[..., i['freekicks']].sum()), 1e-9)
    # corner realism (aligned with model/inplay_corners): one per-match Gamma multiplier
    # → the total-corner count is NegBin (overdispersed), plus an explicit score-state
    # coupling using the cumulative expected goal gap at each segment's start.
    corner_gamma = 1.0
    if corner_realism and corner_extra_dispersion:
        corner_gamma = float(rng.gamma(CORNER_DISPERSION_K, 1.0 / CORNER_DISPERSION_K))
    cum_g = X[:, :, i['goals']].cumsum(0)     # (n_seg, 2) cumulative expected goals per side
    for seg in range(X.shape[0]):
        # score-state at the START of this segment (through seg-1) → corner coupling
        if corner_realism and seg > 0:
            gap = abs(cum_g[seg - 1, 0] - cum_g[seg - 1, 1])
            couple = (CORNER_COUPLE_ONE_GOAL if gap < 1.5
                      else CORNER_COUPLE_BLOWOUT) if gap >= 0.5 else 1.0
        else:
            couple = 1.0
        corner_factor = corner_gamma * couple
        for side in (0, 1):
            plan = [('goal', 'goals', 1.0), ('corner', 'corners', 1.0),
                    ('freekick', 'freekicks', 1.0), ('offside', 'offsides', 1.0)]
            if card_rates is not False:
                plan.append(('red_card', 'red_cards', card_rates.get('red_scale', 1.0)))
            else:
                plan.append(('red_card', 'red_cards', 1.0))
            for ev, ch, scale in plan:
                lam = max(float(X[seg, side, i[ch]]), 0.0) * scale
                if ev == 'corner':
                    lam *= corner_factor
                for _ in range(rng.poisson(lam)):
                    minute = seg * seg_minutes + rng.uniform(0, seg_minutes)
                    e = {'type': ev, 'min': round(minute, 1), 'side': side}
                    # penalties are a sub-intensity of freekicks (plan §5), at the
                    # corpus' observed penalty/freekick ratio
                    if ev == 'freekick' and rng.random() < penalty_ratio:
                        e['type'] = 'penalty'
                    if ev == 'goal':
                        goals[side] += 1
                        if scorer_table:
                            names, probs = scorer_table[teams[side]]
                            if len(names):
                                e['player'] = str(rng.choice(names, p=probs))
                    events.append(e)
            if card_rates is not False:
                # yellows: fouls committed by `side` ~ freekicks won by the other side
                foul_share = float(X[seg, 1 - side, i['freekicks']]) / fk_total
                lam_y = card_rates.get('yellow_per_match', REAL_YELLOW_PER_MATCH) * foul_share
                for _ in range(rng.poisson(lam_y)):
                    minute = seg * seg_minutes + rng.uniform(0, seg_minutes)
                    events.append({'type': 'yellow_card', 'min': round(minute, 1),
                                   'side': side})
    events.sort(key=lambda e: e['min'])
    return {'score': goals, 'events': events}


def build_scorer_table(events_json, conds, home_team, away_team):
    """P(player | team scored) from the corpus goal events, Laplace-smoothed."""
    counts = {home_team: {}, away_team: {}}
    for evs, cond in zip(events_json, conds):
        t = {0: cond['home_team'], 1: cond['away_team']}
        for e in evs:
            if e['type'] == 'goal' and 'player' in e and t.get(e['side']) in counts:
                c = counts[t[e['side']]]
                c[e['player']] = c.get(e['player'], 0) + 1
    table = {}
    for role, team in (('home', home_team), ('away', away_team)):
        c = counts[team]
        names = list(c.keys())
        w = np.array([c[n] + 0.5 for n in names], dtype=float)
        table[role] = (names, w / w.sum()) if names else ([], [])
    return table


def wdl_skellam(Xg, channels):
    """Analytic W/D/L: goal intensities per side -> Skellam mixture over samples.
    Removes the Poisson-realization Monte-Carlo noise from the W/D/L estimate."""
    from scipy.stats import skellam
    ig = channels.index('goals')
    lam_h = np.maximum(Xg[..., 0, ig].sum(1), 1e-9)
    lam_a = np.maximum(Xg[..., 1, ig].sum(1), 1e-9)
    p_draw = skellam.pmf(0, lam_h, lam_a)
    p_home = skellam.sf(0, lam_h, lam_a)
    return np.array([p_home.mean(), p_draw.mean(), (1 - p_home - p_draw).mean()])


def generate_fixture(home_team, away_team, n_samples=2000, guided=True,
                     exclude_matchups=(), factor_dim=10, epochs=4000, seed=0,
                     guidance_weight=0.6, model_cache=None, realistic_cards=True,
                     arch='factor', n_seeds=3, real_anchor=False, cfa_penalty=0.002):
    """End-to-end: train an ensemble of conditional models, sample a fixture.
    Returns dict with the raw tensors, per-sample scores and a W/D/L summary.
    n_seeds: independent trainings pooled at sample level (n=198 training is noisy;
    the ensemble is a variance reduction on the learned distribution itself)."""
    X, meta = load_corpus()
    channels = meta['channels']
    if model_cache is None:
        Z, C, scaler, keep = build_training_arrays(X, meta, exclude_matchups)
        models = [train_conditional(Z, C, factor_dim=factor_dim, epochs=epochs,
                                    seed=seed + 1000 * s, verbose=False, arch=arch,
                                    channels=channels, n_seg=meta['segments'],
                                    cfa_penalty=cfa_penalty)
                  for s in range(n_seeds)]
    else:
        models, scaler, keep = model_cache

    # conditions: sample per-generation from the fixture's OWN config pool (the brain
    # emits varied tactics across sims of one fixture — a median collapses that source
    # of between-match diversity); for never-simmed pairings, ridge strength->tactics
    fix_idx = [j for j, c in enumerate(meta['conds'])
               if c['home_team'] == home_team and c['away_team'] == away_team]
    rng = np.random.default_rng(seed)
    if fix_idx:
        pool = np.stack([cond_vector(meta['conds'][j]) for j in fix_idx])
        c_raw = pool[rng.integers(0, len(pool), n_samples)]
    else:
        from extract import TEAM_STRENGTH
        rh, ra = TEAM_STRENGTH.get(home_team, 0.0), TEAM_STRENGTH.get(away_team, 0.0)
        Call = np.stack([cond_vector(c) for c in meta['conds']])
        # ridge fit: (strength_h, strength_a) -> tactics(10), trained on the corpus
        S = np.stack([[TEAM_STRENGTH.get(c['home_team'], 0.0),
                       TEAM_STRENGTH.get(c['away_team'], 0.0), 1.0]
                      for c in meta['conds']])
        W = np.linalg.solve(S.T @ S + 1e-3 * np.eye(3), S.T @ Call[:, :10])
        tactics = np.array([rh, ra, 1.0]) @ W
        one = np.concatenate([tactics, tactics[:5] - tactics[5:10]])
        # inject the corpus' within-fixture tactic spread so the unseen fixture keeps
        # realistic between-match config diversity
        spread = Call.std(0) * 0.5
        c_raw = one + rng.normal(0, 1, (n_samples, len(one))) * spread
    c_z = (c_raw - scaler['cmu']) / scaler['csd']

    guidance_factory = None
    if guided and fix_idx:
        fixZ = transform(X[fix_idx], channels).reshape(len(fix_idx), -1)
        g_own = make_guidance(fixZ, scaler, weight=guidance_weight)
        guidance_factory = lambda lo, hi: g_own                     # noqa: E731
    else:
        # transfer mode (LOFO / never-simmed pairing): regressed-moment guidance
        if model_cache is None:
            guidance_factory = make_regressed_guidance(Z, C, c_z)

    d = X.shape[1] * X.shape[2] * X.shape[3]
    per = int(np.ceil(n_samples / len(models)))
    zs = []
    for s, m in enumerate(models):
        lo, hi = s * per, min((s + 1) * per, n_samples)
        if lo >= hi:
            break
        g = guidance_factory(lo, hi) if guidance_factory else None
        zs.append(reverse_sample(m, c_z[lo:hi], hi - lo, d, guidance=g,
                                 seed=seed + 7 * s))
    z = np.concatenate(zs)[:n_samples]
    Xg = to_tensor_space(z, scaler)
    if real_anchor:
        # rescale event intensities to real-tournament level (see REAL_ANCHOR_SCALE),
        # then restore the finishing-chain invariants upward
        for ch, sc in REAL_ANCHOR_SCALE.items():
            Xg[..., channels.index(ch)] *= sc
        iot, ish, igl = (channels.index('shots_on_target'), channels.index('shots'),
                         channels.index('goals'))
        Xg[..., iot] = np.maximum(Xg[..., iot], Xg[..., igl])
        Xg[..., ish] = np.maximum(Xg[..., ish], Xg[..., iot])

    events_json = json.load(open(os.path.join(DATA, 'events.json')))
    scorer_table = build_scorer_table(events_json, meta['conds'], home_team, away_team)
    rng = np.random.default_rng(seed)
    # card calibration: model keeps the engine's red-card *shape* (who/when), the
    # *level* is rescaled to the real-tournament rate; yellows are a real-rate overlay
    corpus_red = float(X[..., channels.index('red_cards')].sum()) / len(X)
    card_rates = ({'red_scale': REAL_RED_PER_MATCH / max(corpus_red, 1e-9),
                   'yellow_per_match': REAL_YELLOW_PER_MATCH}
                  if realistic_cards else False)
    n_pen = sum(1 for evs in events_json for e in evs if e['type'] == 'penalty')
    n_fk = sum(1 for evs in events_json for e in evs if e['type'] == 'freekick')
    pen_ratio = n_pen / max(n_fk, 1)
    matches = [realize_events(Xg[j], channels, 90.0 / meta['segments'], rng, scorer_table,
                              card_rates=card_rates, penalty_ratio=pen_ratio)
               for j in range(n_samples)]
    scores = np.array([m['score'] for m in matches])
    wdl = wdl_skellam(Xg, channels)
    return {'tensors': Xg, 'matches': matches, 'scores': scores, 'wdl': wdl,
            'guided': bool(guided and fix_idx), 'models': models, 'scaler': scaler}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--home', required=True)
    ap.add_argument('--away', required=True)
    ap.add_argument('--n', type=int, default=2000)
    ap.add_argument('--no-guide', action='store_true')
    ap.add_argument('--engine-cards', action='store_true',
                    help='keep the engine-faithful card rate (~1.26 reds/match) instead '
                         'of the real-tournament calibration')
    ap.add_argument('--real-anchor', action='store_true',
                    help='rescale goal/corner/shot/xG intensities to real-tournament '
                         'level (fixes the engine low-goal bias; W/D/L shifts accordingly)')
    ap.add_argument('--epochs', type=int, default=4000)
    args = ap.parse_args()
    out = generate_fixture(args.home, args.away, n_samples=args.n,
                           guided=not args.no_guide, epochs=args.epochs,
                           realistic_cards=not args.engine_cards,
                           real_anchor=args.real_anchor)
    w, d_, l = out['wdl']
    print(f"{args.home} vs {args.away}  (guided={out['guided']}, n={args.n})")
    print(f"  W/D/L: {w:.3f}/{d_:.3f}/{l:.3f}")
    n_red = sum(1 for m in out['matches'] for e in m['events'] if e['type'] == 'red_card')
    n_yel = sum(1 for m in out['matches'] for e in m['events'] if e['type'] == 'yellow_card')
    print(f"  cards/match: yellow {n_yel / args.n:.2f}, red {n_red / args.n:.3f} "
          f"(real WC22+26 benchmark: {REAL_YELLOW_PER_MATCH:.2f} / {REAL_RED_PER_MATCH:.3f})")
    uniq, cnt = np.unique([f'{a}-{b}' for a, b in out['scores']], return_counts=True)
    top = sorted(zip(cnt, uniq), reverse=True)[:8]
    print('  top scorelines:', ', '.join(f'{s}({c / args.n:.0%})' for c, s in top))
    avg = out['tensors'].sum(1)     # (n, 2, C)
    print(f"  avg goals {avg[:, 0, 5].mean():.2f}-{avg[:, 1, 5].mean():.2f}, "
          f"avg shots {avg[:, 0, 3].mean():.1f}-{avg[:, 1, 3].mean():.1f}")
