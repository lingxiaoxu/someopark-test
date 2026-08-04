"""
run_real_data.py — Diffusion Factor Model on REAL US equity returns (small-n regime).

End-to-end validation of this repo on real data, mirroring the paper's setting
(n ≈ 100-500 daily observations, d ≈ 100+ assets, k factors):

  1. Download daily grouped bars from Polygon (one API call per trading day),
     cached under dfm/data_cache/ so re-runs are free.
  2. Build a complete close-price panel: top-d tickers by dollar volume with full
     coverage; daily log returns -> (n_days, d) matrix.
  3. Train the diffusion factor model on the FIRST n_train days (train small!),
     generate synthetic return samples.
  4. Score against the HELD-OUT remainder: covariance estimation error of
     (a) diffusion-generated samples, (b) raw sample covariance, (c) Ledoit-Wolf
     shrinkage — the paper's core claim is (a) beats (b)/(c) when n_train is small.

Reads the Polygon key from the repo root .env (read-only). All writes stay in dfm/.

Usage:
  python run_real_data.py [--days 360] [--assets 128] [--factors 8] \
                          [--train-days 120] [--epochs 60]
"""
import argparse
import datetime as dt
import json
import os
import time
import urllib.request

import numpy as np
import torch

from config import DIFFUSION_CONFIG, SCORE_NETWORK_CONFIG, TRAINING_CONFIG, DEVICE
from diffusion import DiffusionProcess
from score_network import create_score_network
from score_matching import train_diffusion_model
from sampling import DiffusionSampler
from preprocessing import prepare_data_loader

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'data_cache')
os.makedirs(CACHE, exist_ok=True)


def _polygon_key() -> str:
    env = os.path.join(os.path.dirname(HERE), '.env')
    for line in open(env, encoding='utf-8'):
        line = line.strip()
        if line.startswith('POLYGON_API_KEY'):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise RuntimeError('POLYGON_API_KEY not found in repo .env')


def fetch_grouped_daily(day: str, key: str):
    """One trading day's bars for ALL US stocks (cached). Returns list or None (holiday)."""
    fp = os.path.join(CACHE, f'grouped_{day}.json')
    if os.path.exists(fp):
        return json.load(open(fp))
    url = (f'https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{day}'
           f'?adjusted=true&apiKey={key}')
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.loads(r.read())
    except Exception as e:
        print(f'  {day}: fetch failed ({e}) — skipping')
        return None
    rows = payload.get('results') or []
    out = [{'T': x['T'], 'c': x['c'], 'v': x.get('v', 0)} for x in rows] if rows else []
    json.dump(out, open(fp, 'w'))
    time.sleep(0.15)
    return out


def build_panel(days: int, assets: int):
    """(dates, tickers, close-price matrix) for the top-`assets` complete-coverage names."""
    key = _polygon_key()
    end = dt.date.today() - dt.timedelta(days=1)
    dates, by_day = [], {}
    d = end
    while len(dates) < days:
        if d.weekday() < 5:                      # trading-day candidates
            rows = fetch_grouped_daily(d.isoformat(), key)
            if rows:                             # holidays return empty
                by_day[d.isoformat()] = {r['T']: r for r in rows}
                dates.append(d.isoformat())
        d -= dt.timedelta(days=1)
    dates = dates[::-1]
    print(f'[panel] {len(dates)} trading days {dates[0]} -> {dates[-1]}')

    # tickers present every day, plain common-stock symbols only
    common = None
    for day in dates:
        names = {t for t in by_day[day] if t.isalpha() and t.isupper() and len(t) <= 5}
        common = names if common is None else common & names
    print(f'[panel] {len(common)} tickers with full coverage')

    # rank by median dollar volume, keep top `assets`
    dollar = {t: np.median([by_day[day][t]['c'] * by_day[day][t]['v'] for day in dates])
              for t in common}
    keep = sorted(sorted(common, key=lambda t: -dollar[t])[:assets])
    px = np.array([[by_day[day][t]['c'] for t in keep] for day in dates])
    return dates, keep, px


def cov_error(est_cov: np.ndarray, real_cov: np.ndarray) -> float:
    return float(np.linalg.norm(est_cov - real_cov, 'fro') / np.linalg.norm(real_cov, 'fro'))


def min_var_oos_vol(est_cov: np.ndarray, test: np.ndarray) -> float:
    """Annualized realized OOS vol of the global-minimum-variance portfolio built from
    est_cov — the paper's downstream test: a better covariance gives a lower realized vol."""
    d = est_cov.shape[0]
    ridge = 1e-8 * np.trace(est_cov) / d
    inv = np.linalg.inv(est_cov + ridge * np.eye(d))
    w = inv @ np.ones(d)
    w = w / w.sum()
    return float((test @ w).std() * np.sqrt(252))


def run_split(train, test, args, seed=0):
    """Train the DFM on `train`, generate, and score every estimator against the
    held-out covariance of `test`. Returns the per-split metric dict."""
    n_train, d = train.shape
    mu, sd = train.mean(0), train.std(0) + 1e-12
    train_z = (train - mu) / sd

    # PCA of the train correlation structure: warm start for the network's loading
    # matrix V and idiosyncratic variances (random init cannot find the subspace at
    # n=120), and the POET ablation baseline below.
    corr_train = np.cov(train_z, rowvar=False)
    evals, evecs = np.linalg.eigh(corr_train)
    evals, evecs = evals[::-1], evecs[:, ::-1]
    beta_hat = evecs[:, :args.factors]                       # (d, k)
    lam_hat = evals[:args.factors]
    resid = train_z - (train_z @ beta_hat) @ beta_hat.T
    sigma2_hat = resid.var(0) + 1e-6                         # (d,) idio variances

    # ---- train the diffusion factor model on the small train slice ----
    torch.manual_seed(seed)
    diffusion = DiffusionProcess(
        data_dim=d, factor_dim=args.factors,
        T=DIFFUSION_CONFIG['T'], t0=DIFFUSION_CONFIG['t0'],
        eta=DIFFUSION_CONFIG['eta'], num_steps=DIFFUSION_CONFIG['num_steps'], device=DEVICE)
    net_cfg = SCORE_NETWORK_CONFIG.copy()
    net_cfg['use_2d_unet'] = False                          # MLP encoder-decoder path
    net_cfg['init_beta'] = torch.tensor(beta_hat.copy())
    net_cfg['init_sigma_diag'] = torch.tensor(sigma2_hat.copy())
    model = create_score_network(net_cfg, asset_dim=d, factor_dim=args.factors, device=DEVICE)

    tr_cfg = TRAINING_CONFIG.copy()
    tr_cfg['num_epochs'] = args.epochs
    tr_cfg['batch_size'] = min(64, n_train)
    # Small-n regime: only ~2 batches/epoch and the score-matching val loss is very noisy
    # (random t draws), so the default patience=10 stops after ~20 gradient steps and the
    # model never learns the correlation structure. Scale patience & LR decay to the run.
    tr_cfg['early_stopping_patience'] = max(200, args.epochs // 5)
    tr_cfg['lr_scheduler_step'] = max(100, args.epochs // 4)
    X = torch.tensor(train_z, dtype=torch.float32)
    loader = prepare_data_loader(X, batch_size=tr_cfg['batch_size'], shuffle=True)
    vloader = prepare_data_loader(X[: max(8, n_train // 5)], batch_size=tr_cfg['batch_size'],
                                  shuffle=False)
    model, hist = train_diffusion_model(model=model, diffusion_process=diffusion,
                                        train_loader=loader, val_loader=vloader,
                                        config=tr_cfg, device=DEVICE)

    # ---- generate and de-standardize ----
    sampler = DiffusionSampler(model=model, diffusion_process=diffusion, device=DEVICE)
    gen_z = sampler.sample(num_samples=args.gen_samples, data_shape=(d,),
                           noise_steps=180, batch_size=256,
                           final_denoise=True).cpu().numpy()
    gen_raw = gen_z * sd + mu
    # Marginal calibration: with n_train this small the learned score under-contracts and
    # inflates marginal variance (correlation structure survives, scale does not). Rescale
    # each asset's std back to 1 in z-space (= train marginal std after de-standardizing) —
    # correlations come from the diffusion model, variances from the train data.
    gen_cal = gen_z / (gen_z.std(0) + 1e-12) * sd + mu
    gen = gen_cal

    # ---- scoreboard: covariance estimation vs held-out realized covariance ----
    real_cov = np.cov(test, rowvar=False)
    sample_cov = np.cov(train, rowvar=False)
    from sklearn.covariance import LedoitWolf
    lw_cov = LedoitWolf().fit(train).covariance_
    gen_cov = np.cov(gen, rowvar=False)
    gen_cov_raw = np.cov(gen_raw, rowvar=False)
    # POET ablation: the same PCA factor structure the network is warm-started with,
    # but with Gaussian-implied covariance only (no diffusion). Separates "factor
    # structure helps" from "diffusion helps beyond the structure".
    poet_z = beta_hat @ np.diag(lam_hat) @ beta_hat.T + np.diag(sigma2_hat)
    poet_cov = poet_z * np.outer(sd, sd)

    estimators = {
        'sample': sample_cov, 'ledoit_wolf': lw_cov, 'poet': poet_cov,
        'diffusion': gen_cov, 'diffusion_raw': gen_cov_raw,
    }
    res = {
        'mean_abs_corr_real': float(np.mean(np.abs(np.corrcoef(test, rowvar=False)))),
        'mean_abs_corr_gen': float(np.mean(np.abs(np.corrcoef(gen, rowvar=False)))),
        'gen_std_ratio': float(np.mean(gen.std(0) / (test.std(0) + 1e-12))),
    }
    for name, C in estimators.items():
        res[f'cov_error_{name}'] = cov_error(C, real_cov)
        res[f'minvar_oos_vol_{name}'] = min_var_oos_vol(C, test)
    res['minvar_oos_vol_equal_weight'] = float(test.mean(1).std() * np.sqrt(252))
    # top-k eigenvalue share comparison (factor structure captured?)
    for name, C in (('real', real_cov), ('diffusion', gen_cov), ('sample', sample_cov)):
        ev = np.sort(np.linalg.eigvalsh(C))[::-1]
        res[f'topk_evr_{name}'] = float(ev[:args.factors].sum() / ev.sum())
    return res, gen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=360)
    ap.add_argument('--assets', type=int, default=128)
    ap.add_argument('--factors', type=int, default=8)
    ap.add_argument('--train-days', type=int, default=120)
    ap.add_argument('--test-days', type=int, default=120)
    ap.add_argument('--splits', type=int, default=1,
                    help='rolling train/test splits; stride spreads them over the panel')
    ap.add_argument('--epochs', type=int, default=1500)
    ap.add_argument('--gen-samples', type=int, default=4096)
    args = ap.parse_args()

    dates, tickers, px = build_panel(args.days, args.assets)
    rets = np.diff(np.log(px), axis=0)                     # (n_days-1, d)
    n, d = rets.shape
    win = args.train_days + args.test_days
    if n < win:
        raise SystemExit(f'need at least {win} return days, have {n}')
    stride = max(1, (n - win) // max(1, args.splits - 1)) if args.splits > 1 else 0
    offsets = [i * stride for i in range(args.splits)]
    print(f'[data] {n} return days x {d} assets; {args.splits} rolling split(s), '
          f'train {args.train_days} / test {args.test_days}, offsets {offsets} '
          f'(small-n regime: n_train={args.train_days} vs d={d})')

    all_res, gen_last = [], None
    for si, o in enumerate(offsets):
        train = rets[o:o + args.train_days]
        test = rets[o + args.train_days:o + win]
        print(f'\n[split {si + 1}/{args.splits}] train {dates[o]}..{dates[o + args.train_days]} '
              f'test ..{dates[min(o + win, len(dates) - 1)]}')
        res, gen_last = run_split(train, test, args, seed=si)
        all_res.append(res)
        for key in ('cov_error_diffusion', 'cov_error_ledoit_wolf', 'minvar_oos_vol_diffusion',
                    'minvar_oos_vol_ledoit_wolf'):
            print(f'    {key:32} {res[key]:.4f}')

    mean_res = {k: float(np.mean([r[k] for r in all_res])) for k in all_res[0]}
    summary = {
        'n_train': args.train_days, 'n_test': args.test_days, 'd': d, 'k': args.factors,
        'splits': args.splits, 'dates': [dates[0], dates[-1]], 'tickers_head': tickers[:10],
        'mean': mean_res, 'per_split': all_res,
    }
    out = os.path.join(HERE, 'results_real')
    os.makedirs(out, exist_ok=True)
    json.dump(summary, open(os.path.join(out, 'summary.json'), 'w'), indent=1)
    if gen_last is not None:
        np.save(os.path.join(out, 'generated_returns.npy'), gen_last[:1000])

    print(f'\n========= REAL-DATA SCOREBOARD (mean of {args.splits} split(s)) =========')
    for k_, v in mean_res.items():
        print(f'  {k_:32} {v:.4f}')
    print(f'  -> saved to {out}/summary.json')
    win_cov = mean_res['cov_error_diffusion'] < min(mean_res['cov_error_sample'],
                                                    mean_res['cov_error_ledoit_wolf'])
    win_pf = mean_res['minvar_oos_vol_diffusion'] < min(mean_res['minvar_oos_vol_sample'],
                                                        mean_res['minvar_oos_vol_ledoit_wolf'])
    n_cov = sum(r['cov_error_diffusion'] < min(r['cov_error_sample'], r['cov_error_ledoit_wolf'])
                for r in all_res)
    n_pf = sum(r['minvar_oos_vol_diffusion'] < min(r['minvar_oos_vol_sample'],
                                                   r['minvar_oos_vol_ledoit_wolf'])
               for r in all_res)
    print(f'  diffusion beats sample+LW covariance error: {win_cov} ({n_cov}/{args.splits} splits)')
    print(f'  diffusion beats sample+LW min-var OOS vol:  {win_pf} ({n_pf}/{args.splits} splits)')


if __name__ == '__main__':
    main()
