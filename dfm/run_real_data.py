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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=360)
    ap.add_argument('--assets', type=int, default=128)
    ap.add_argument('--factors', type=int, default=8)
    ap.add_argument('--train-days', type=int, default=120)
    ap.add_argument('--epochs', type=int, default=1500)
    ap.add_argument('--gen-samples', type=int, default=4096)
    args = ap.parse_args()

    dates, tickers, px = build_panel(args.days, args.assets)
    rets = np.diff(np.log(px), axis=0)                     # (n_days-1, d)
    n, d = rets.shape
    n_train = args.train_days
    train, test = rets[:n_train], rets[n_train:]
    print(f'[data] returns {rets.shape}; train {train.shape}, held-out {test.shape} '
          f'(small-n regime: n_train={n_train} vs d={d})')

    # standardize on train stats only
    mu, sd = train.mean(0), train.std(0) + 1e-12
    train_z = (train - mu) / sd

    # ---- train the diffusion factor model on the small train slice ----
    torch.manual_seed(0)
    diffusion = DiffusionProcess(
        data_dim=d, factor_dim=args.factors,
        T=DIFFUSION_CONFIG['T'], t0=DIFFUSION_CONFIG['t0'],
        eta=DIFFUSION_CONFIG['eta'], num_steps=DIFFUSION_CONFIG['num_steps'], device=DEVICE)
    net_cfg = SCORE_NETWORK_CONFIG.copy()
    net_cfg['use_2d_unet'] = False                          # MLP encoder-decoder path
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
                           noise_steps=180, batch_size=256).cpu().numpy()
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

    res = {
        'n_train': n_train, 'n_test': int(test.shape[0]), 'd': d, 'k': args.factors,
        'cov_error_sample': cov_error(sample_cov, real_cov),
        'cov_error_ledoit_wolf': cov_error(lw_cov, real_cov),
        'cov_error_diffusion': cov_error(gen_cov, real_cov),
        'cov_error_diffusion_raw': cov_error(gen_cov_raw, real_cov),
        'mean_abs_corr_real': float(np.mean(np.abs(np.corrcoef(test, rowvar=False)))),
        'mean_abs_corr_gen': float(np.mean(np.abs(np.corrcoef(gen, rowvar=False)))),
        'gen_std_ratio': float(np.mean(gen.std(0) / (test.std(0) + 1e-12))),
        'dates': [dates[0], dates[-1]], 'tickers_head': tickers[:10],
    }
    # top-k eigenvalue share comparison (factor structure captured?)
    for name, C in (('real', real_cov), ('diffusion', gen_cov), ('sample', sample_cov)):
        ev = np.sort(np.linalg.eigvalsh(C))[::-1]
        res[f'topk_evr_{name}'] = float(ev[:args.factors].sum() / ev.sum())

    out = os.path.join(HERE, 'results_real')
    os.makedirs(out, exist_ok=True)
    json.dump(res, open(os.path.join(out, 'summary.json'), 'w'), indent=1)
    np.save(os.path.join(out, 'generated_returns.npy'), gen[:1000])

    print('\n================ REAL-DATA SCOREBOARD ================')
    for k_, v in res.items():
        if isinstance(v, float):
            print(f'  {k_:28} {v:.4f}')
    print(f'  -> saved to {out}/summary.json')
    win = res['cov_error_diffusion'] < min(res['cov_error_sample'], res['cov_error_ledoit_wolf'])
    print(f'  diffusion beats sample+LW covariance: {win}')


if __name__ == '__main__':
    main()
