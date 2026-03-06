"""
MRPTWalkForward.py — Walk-forward optimization for MRPT strategy.

Methodology:
  - Expanding training window (anchored at auto-computed train_start)
  - Equal trading-day OOS windows (NYSE calendar aware)
  - Each training window runs the 32-param grid search (Step 1)
  - Best param_set per pair selected using Deflated Sharpe Ratio (DSR) to
    correct for multiple-comparison bias (32 trials per pair)
  - Selected pairs/params run on the next out-of-sample test window
  - OOS windows are concatenated into a single walk-forward equity curve

Window design (default: 18mo train, 6 windows × 25 NYSE trading days):
  OOS period  = last 150 NYSE trading days ending at most-recent available date

  Expanding mode (default): train_start is fixed at 18mo before first OOS window.
    Each subsequent window trains on more data (anchor stays the same).

  Rolling mode (--mode rolling): train window is a fixed 18-month sliding window.
    train_start moves forward with each OOS window, keeping train length constant.

  Example expanding (run on 2026-03-05, last data = 2026-03-04):
    train_start = 2024-01-30  (18mo before 2025-07-30, fixed for all windows)
    Window 1: train 2024-01-30 -> 2025-07-29  |  OOS 2025-07-30 -> 2025-09-03
    Window 2: train 2024-01-30 -> 2025-09-03  |  OOS 2025-09-04 -> 2025-10-08
    ...
    Window 6: train 2024-01-30 -> 2026-01-27  |  OOS 2026-01-28 -> 2026-03-04

  Example rolling (same dates):
    Window 1: train 2024-01-30 -> 2025-07-29  |  OOS 2025-07-30 -> 2025-09-03
    Window 2: train 2024-03-06 -> 2025-09-03  |  OOS 2025-09-04 -> 2025-10-08
    ...  (train_start shifts right by one OOS window each time)

Deflated Sharpe Ratio (Bailey & López de Prado, 2014):
  Corrects the IS Sharpe ratio for the number of trials (param_sets tested),
  skewness, and kurtosis of the return series. Only param_sets with DSR > 0
  (i.e., DSR p-value < 0.5) are considered genuinely significant.

  DSR = Phi[ (SR* - SR_benchmark) * sqrt(T-1) / sqrt(1 - gamma3*SR* + (gamma4-1)/4 * SR*^2) ]

  where SR* = max IS Sharpe across N trials, adjusted for N:
    SR_benchmark = sqrt(V[SR]) * ((1-gamma)*Z(1-1/N) + gamma*Z(1-1/(N*e)))

Usage:
  export $(cat .env | xargs) && conda run -n someopark_run python MRPTWalkForward.py [options]

Options:
  --mode expanding|rolling  Window mode: expanding (fixed anchor) or rolling (fixed length) (default: expanding)
  --oos-windows N     Number of OOS windows (default: 6)
  --oos-days N        Total OOS trading days across all windows (default: 150 = 6×25)
  --train-months N    Training period length in months (default: 18)
  --last-date DATE    Last available data date (default: auto = most recent NYSE trading day <= today)
  --output-dir DIR    Where to write results (default: historical_runs/walk_forward/)
  --skip-grid         Skip grid search if summary CSV already exists for a window
"""

import os
import sys
import json
import glob
import logging
import argparse
import tempfile
import shutil
import math
from datetime import datetime, timedelta
from calendar import monthrange
from dateutil.relativedelta import relativedelta

import numpy as np
import pandas as pd
from scipy.stats import norm
import pandas_market_calendars as mcal

import PortfolioMRPTStrategyRuns as Runs
import PortfolioMRPTRun as PortfolioRun

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GRID_CONFIG = os.path.join(BASE_DIR, 'run_configs', 'runs_20260304_step1_grid32.json')

ALL_PAIRS = [
    ('MSCI', 'LII'), ('D', 'MCHP'), ('DG', 'MOS'), ('ESS', 'EXPD'), ('ACGL', 'UHS'),
    ('AAPL', 'META'), ('YUM', 'MCD'), ('GS', 'ALLY'), ('CL', 'USO'), ('ALGN', 'UAL'),
    ('ARES', 'CG'), ('AMG', 'BEN'), ('LYFT', 'UBER'), ('TW', 'CME'), ('CART', 'DASH'),
]

# Selection thresholds (same as MRPTUpdateConfigs.py)
MIN_PNL    = 0
MIN_TRADES = 3


# ── NYSE calendar helpers ──────────────────────────────────────────────────────

def _get_nyse_trading_days(start='2023-01-01', end='2027-12-31'):
    """Return a DatetimeIndex of all NYSE trading days in [start, end], tz-naive."""
    nyse = mcal.get_calendar('NYSE')
    sched = nyse.schedule(start_date=start, end_date=end)
    tdays = mcal.date_range(sched, frequency='1D').normalize()
    return pd.DatetimeIndex([d.tz_localize(None) for d in tdays])


def _last_trading_day_on_or_before(tdays, date):
    """Return the last trading day <= date."""
    date = pd.Timestamp(date)
    idx = tdays.searchsorted(date, side='right') - 1
    if idx < 0:
        raise ValueError(f'No trading day on or before {date.date()}')
    return tdays[idx]


def next_trading_day(date_str, tdays=None):
    """First NYSE trading day strictly after date_str."""
    if tdays is None:
        tdays = _get_nyse_trading_days()
    d = pd.Timestamp(date_str)
    idx = tdays.searchsorted(d, side='right')
    if idx >= len(tdays):
        raise ValueError(f'No trading day after {date_str}')
    return tdays[idx].strftime('%Y-%m-%d')


def build_windows(last_date=None, n_windows=6, oos_days=150, train_months=18,
                  mode='expanding'):
    """
    Build walk-forward window definitions using NYSE trading days.

    Parameters
    ----------
    last_date : str or None
        Last available data date (YYYY-MM-DD). Defaults to most recent NYSE
        trading day on or before today.
    n_windows : int
        Number of OOS windows (default 6).
    oos_days : int
        Total OOS trading days across all windows (default 150 = 6×25).
    train_months : int
        Training period length in months (default 18).
    mode : str
        'expanding' (default): train_start is fixed at train_months before first OOS day.
                               Each window trains on more data as OOS progresses.
        'rolling': train window is a fixed train_months sliding window.
                   train_start moves forward with each OOS window.

    Returns
    -------
    (windows, train_start_str)
        windows: list of dicts with keys: window_idx, train_start, train_end, test_start, test_end
        train_start_str: earliest train_start across all windows
    """
    if mode not in ('expanding', 'rolling'):
        raise ValueError(f"mode must be 'expanding' or 'rolling', got {mode!r}")

    tdays = _get_nyse_trading_days()

    if last_date is None:
        today = pd.Timestamp(datetime.now().date())
        last_td = _last_trading_day_on_or_before(tdays, today)
    else:
        last_td = _last_trading_day_on_or_before(tdays, last_date)

    # OOS period = last `oos_days` trading days ending at last_td
    last_idx = tdays.get_loc(last_td)
    if last_idx < oos_days - 1:
        raise ValueError(f'Not enough trading days: need {oos_days}, have {last_idx + 1}')
    oos_tdays = tdays[last_idx - oos_days + 1 : last_idx + 1]

    # Split OOS into n_windows equal chunks
    window_size = oos_days // n_windows
    remainder   = oos_days % n_windows  # distribute remainder to last windows

    # Expanding: fixed anchor = train_months before first OOS day
    first_oos_start = oos_tdays[0]
    anchor_dt  = first_oos_start - relativedelta(months=train_months)
    anchor_td  = _last_trading_day_on_or_before(tdays, anchor_dt)
    anchor_str = anchor_td.strftime('%Y-%m-%d')

    windows = []
    offset = 0
    for i in range(n_windows):
        # Distribute remainder: last `remainder` windows get one extra day
        extra = 1 if i >= (n_windows - remainder) else 0
        w_size = window_size + extra
        w_start = oos_tdays[offset]
        w_end   = oos_tdays[offset + w_size - 1]
        offset += w_size

        # train_end = trading day immediately before w_start
        w_start_idx  = tdays.get_loc(w_start)
        train_end_td = tdays[w_start_idx - 1]

        if mode == 'expanding':
            train_start_str = anchor_str
        else:  # rolling: train_start = train_months before this window's test_start
            ts_dt  = w_start - relativedelta(months=train_months)
            ts_td  = _last_trading_day_on_or_before(tdays, ts_dt)
            train_start_str = ts_td.strftime('%Y-%m-%d')

        windows.append({
            'window_idx':  i + 1,
            'train_start': train_start_str,
            'train_end':   train_end_td.strftime('%Y-%m-%d'),
            'test_start':  w_start.strftime('%Y-%m-%d'),
            'test_end':    w_end.strftime('%Y-%m-%d'),
        })

    # Return earliest train_start (for data loading purposes)
    earliest_train_start = min(w['train_start'] for w in windows)
    return windows, earliest_train_start


# ── Deflated Sharpe Ratio (Bailey & López de Prado 2014) ─────────────────────

def deflated_sharpe_ratio(is_sharpe, n_trials, n_obs, skew=0.0, kurt=3.0):
    """
    Compute the Deflated Sharpe Ratio.

    Parameters
    ----------
    is_sharpe : float
        In-sample Sharpe ratio (annualised not required — daily is fine,
        scaling cancels in the formula).
    n_trials : int
        Number of independent trials (param_sets tested for this pair).
    n_obs : int
        Number of observations (trading days) in the IS window.
    skew : float
        Skewness of the daily return series (default 0 = normal).
    kurt : float
        Excess kurtosis of daily return series (default 0, i.e. kurt=3 total).

    Returns
    -------
    dsr_pvalue : float
        Probability that the IS Sharpe is real (not due to selection bias).
        dsr_pvalue > 0.5  →  DSR positive  →  keep
        dsr_pvalue < 0.5  →  probably noise →  discard
    """
    if n_obs < 5 or n_trials < 1:
        return 0.0

    # Expected maximum SR among n_trials iid standard normal variables
    # approximation: E[max(Z_1,...,Z_N)] ≈ (1-gamma)*z(1-1/N) + gamma*z(1-1/(N*e))
    gamma = 0.5772156649  # Euler-Mascheroni constant
    e     = math.e

    def z(p):
        """Inverse normal CDF."""
        p = max(1e-10, min(1 - 1e-10, p))
        return norm.ppf(p)

    sr_expected = (
        (1 - gamma) * z(1 - 1 / n_trials)
        + gamma     * z(1 - 1 / (n_trials * e))
    )
    # Scale by std of SR estimator: std(SR) ≈ sqrt(1/T)
    sr_benchmark = sr_expected * (1 / math.sqrt(n_obs))

    # Adjusted SR variance accounting for non-normality
    # Var correction factor: 1 - skew*SR + (kurt-1)/4 * SR^2
    var_factor = 1 - skew * is_sharpe + (kurt - 1) / 4 * is_sharpe ** 2
    if var_factor <= 0:
        var_factor = 1e-6

    sr_std = math.sqrt(var_factor / (n_obs - 1))

    if sr_std == 0:
        return 0.0

    dsr_stat = (is_sharpe - sr_benchmark) / sr_std
    return float(norm.cdf(dsr_stat))


def compute_pair_sharpe_and_stats(xlsx_path, pair_key, train_start, train_end):
    """
    Compute IS Sharpe ratio, skew, kurt for one pair from dod_pair_trade_pnl_history.
    Only uses rows within [train_start, train_end].
    Returns (sharpe, skew, kurt, n_obs) or (None, 0, 3, 0) on failure.
    """
    try:
        dod = pd.read_excel(xlsx_path, sheet_name='dod_pair_trade_pnl_history')
    except Exception:
        return None, 0.0, 3.0, 0

    if 'Pair' not in dod.columns or 'PnL Dollar' not in dod.columns:
        return None, 0.0, 3.0, 0

    dod['Date'] = pd.to_datetime(dod['Date'], errors='coerce')
    pair_dod = dod[
        (dod['Pair'] == pair_key) &
        (dod['Date'] >= pd.Timestamp(train_start)) &
        (dod['Date'] <= pd.Timestamp(train_end))
    ]['PnL Dollar'].dropna()

    if len(pair_dod) < 5:
        return None, 0.0, 3.0, 0

    mean = pair_dod.mean()
    std  = pair_dod.std()
    if std == 0:
        return None, 0.0, 3.0, 0

    sharpe = mean / std * math.sqrt(252)
    skew   = float(pair_dod.skew())
    kurt   = float(pair_dod.kurtosis()) + 3  # scipy gives excess kurtosis; formula needs full
    n_obs  = len(pair_dod)

    return sharpe, skew, kurt, n_obs


# ── Grid search for one training window ──────────────────────────────────────

def run_grid_for_window(window, output_dir, skip_if_exists=False):
    """
    Run 32-param grid search over training window.
    Returns path to the strategy_summary CSV for this window.
    """
    w_label = f'wf_window{window["window_idx"]:02d}_{window["train_start"]}_{window["train_end"]}'
    window_dir = os.path.join(output_dir, w_label)
    os.makedirs(window_dir, exist_ok=True)

    summary_pattern = os.path.join(window_dir, 'strategy_summary_*.csv')
    existing = sorted(glob.glob(summary_pattern), key=os.path.getmtime, reverse=True)

    if skip_if_exists and existing:
        log.info(f'  [Window {window["window_idx"]}] Grid search already done: {existing[0]}')
        return existing[0]

    # Build a temporary config JSON for this training window
    # Load the base grid config and override start/end dates
    with open(GRID_CONFIG) as f:
        base_cfg = json.load(f)

    train_cfg = {
        '_comment': f'Walk-forward window {window["window_idx"]} training grid: '
                    f'{window["train_start"]} → {window["train_end"]}',
        'start_date': window['train_start'],
        'end_date':   window['train_end'],
        'runs': base_cfg['runs'],
    }

    tmp_cfg_path = os.path.join(window_dir, 'grid_config.json')
    with open(tmp_cfg_path, 'w') as f:
        json.dump(train_cfg, f, indent=2)

    # Monkey-patch output dirs so files land in window_dir, not the main historical_runs/
    original_output_dir = os.path.join(BASE_DIR, 'historical_runs')

    log.info(f'  [Window {window["window_idx"]}] Running grid search '
             f'{window["train_start"]} → {window["train_end"]} ...')

    # run_from_config writes to BASE_DIR/historical_runs and BASE_DIR/charts.
    # We redirect by temporarily overriding the output paths inside PortfolioRun.main
    # via a wrapper that passes output_dir explicitly.
    _run_grid_direct(train_cfg, window, window_dir)

    # Find summary CSV written by this run (latest in window_dir)
    summaries = sorted(glob.glob(os.path.join(window_dir, 'strategy_summary_*.csv')),
                       key=os.path.getmtime, reverse=True)
    if not summaries:
        raise RuntimeError(f'Grid search produced no summary CSV in {window_dir}')
    return summaries[0]


def _run_grid_direct(train_cfg, window, window_dir):
    """
    Execute each run in train_cfg directly via PortfolioRun.main(),
    writing outputs to window_dir instead of the global historical_runs/.
    Mirrors the core loop of run_from_config() but redirects output.
    """
    start_date = train_cfg['start_date']
    end_date   = train_cfg['end_date']

    # Resolve all runs from the config
    resolved_runs = []
    for i, run in enumerate(train_cfg['runs']):
        label = run.get('label', f'run_{i}')
        raw_pairs = run.get('pairs', [])
        run_ps_ref = run.get('param_set', 'default')
        params, ps_name = Runs._resolve_param_set(run_ps_ref, label)
        pairs = [[e[0], e[1]] for e in raw_pairs]
        resolved_runs.append({
            'label': label, 'pairs': pairs,
            'params': params, 'param_set_name': ps_name,
            'pair_params': {},
        })

    # Pre-load data once
    data_cache = {}
    all_symbols = sorted(set(sym for run in resolved_runs for pair in run['pairs'] for sym in pair))
    sym_key = tuple(all_symbols)
    log.info(f'    Pre-loading {len(all_symbols)} symbols ...')
    data_cache[sym_key] = PortfolioRun.load_historical_data(start_date, end_date, list(all_symbols))

    all_results = []
    charts_dir = os.path.join(window_dir, 'charts')
    os.makedirs(charts_dir, exist_ok=True)

    for run_idx, run in enumerate(resolved_runs, 1):
        label        = run['label']
        pairs        = run['pairs']
        params       = run['params']
        ps_name      = run['param_set_name']
        pair_params  = run['pair_params']
        run_label    = f'{label}_{ps_name}'

        sym_key = tuple(sorted(set(sym for pair in pairs for sym in pair)))
        historical_data = data_cache[sym_key]

        log.info(f'    [{run_idx:2d}/{len(resolved_runs)}] {ps_name}')

        try:
            result = PortfolioRun.main(config={
                'pairs': pairs, 'params': params, 'pair_params': pair_params,
                'run_label': run_label,
                'output_dir': window_dir,   # ← redirect here
                'historical_data': historical_data,
                'start_date': start_date, 'end_date': end_date,
                'trade_start_date': None,
            })
            if result:
                row = {
                    'run_idx': run_idx, 'run_name': result['run_name'],
                    'label': label, 'param_set': ps_name,
                    'pairs': Runs.make_pairs_label(pairs),
                    'final_equity': result['final_equity'],
                    'acc_pnl': result['acc_pnl'],
                    'sharpe_ratio': result['sharpe_ratio'],
                    'max_drawdown_dollar': result['max_drawdown_dollar'],
                    'max_drawdown_pct': result['max_drawdown_pct'],
                    'trading_days_pct': result['trading_days_pct'],
                    'output_file': result['output_file'],
                }
                row.update(params)
                all_results.append(row)
        except Exception as e:
            log.error(f'    Run {ps_name} failed: {e}')
            import traceback; log.error(traceback.format_exc())

    if all_results:
        summary_df = pd.DataFrame(all_results)
        summary_df = summary_df.sort_values('sharpe_ratio', ascending=False, na_position='last')
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        summary_path = os.path.join(window_dir, f'strategy_summary_{ts}.csv')
        summary_df.to_csv(summary_path, index=False)
        log.info(f'    Grid summary → {summary_path}')
    else:
        raise RuntimeError('No successful runs in grid search')


# ── Select best params per pair with DSR filter ───────────────────────────────

def select_pairs_with_dsr(summary_csv, window, n_trials=32):
    """
    Read grid summary, compute per-pair IS Sharpe + DSR, select best param_set.

    Returns list of [s1, s2, best_param_set] for pairs that pass DSR filter.
    """
    summary = pd.read_csv(summary_csv)
    train_start = window['train_start']
    train_end   = window['train_end']

    # Count IS trading days (NYSE calendar)
    nyse_tdays = _get_nyse_trading_days()
    ts_mask = (nyse_tdays >= pd.Timestamp(train_start)) & (nyse_tdays <= pd.Timestamp(train_end))
    n_obs_approx = int(ts_mask.sum())

    records = []
    for _, row in summary.iterrows():
        param_set  = row['param_set']
        run_sharpe = row.get('sharpe_ratio', 0) or 0
        xlsx_path  = row.get('output_file', '')

        if not xlsx_path or not os.path.exists(xlsx_path):
            continue

        # Read per-pair final PnL and trade count
        try:
            acc = pd.read_excel(xlsx_path, sheet_name='acc_pair_trade_pnl_history')
            pt  = pd.read_excel(xlsx_path, sheet_name='pair_trade_history')
        except Exception:
            continue

        for s1, s2 in ALL_PAIRS:
            pair_key = f'{s1}/{s2}'
            pair_acc = acc[acc['Pair'] == pair_key]
            if pair_acc.empty:
                continue
            final_pnl = pair_acc['PnL Dollar'].iloc[-1]

            pair_pt  = pt[pt['Pair'] == pair_key]
            n_trades = int((pair_pt['Order Type'] == 'open').sum())

            # Per-pair IS Sharpe from daily PnL
            pair_sharpe, skew, kurt, n_obs = compute_pair_sharpe_and_stats(
                xlsx_path, pair_key, train_start, train_end
            )
            if pair_sharpe is None:
                pair_sharpe = run_sharpe  # fallback to run-level

            # DSR
            dsr_pval = deflated_sharpe_ratio(
                is_sharpe=pair_sharpe,
                n_trials=n_trials,
                n_obs=n_obs if n_obs > 0 else n_obs_approx,
                skew=skew, kurt=kurt,
            )

            records.append({
                'pair_key': pair_key, 's1': s1, 's2': s2,
                'param_set': param_set,
                'pair_pnl': final_pnl,
                'pair_sharpe': pair_sharpe,
                'run_sharpe': run_sharpe,
                'dsr_pvalue': dsr_pval,
                'n_trades': n_trades,
            })

    if not records:
        return [], pd.DataFrame()

    df = pd.DataFrame(records)

    log.info(f'\n  ── Pair selection (DSR filter) ──')
    selected = []
    excluded = []

    for s1, s2 in ALL_PAIRS:
        pair_key = f'{s1}/{s2}'
        grp = df[df['pair_key'] == pair_key]
        if grp.empty:
            log.info(f'    {pair_key:<12s}  NO DATA')
            excluded.append(pair_key)
            continue

        # Apply filters: PnL > 0, min trades, DSR > 0.5
        eligible = grp[
            (grp['pair_pnl']   > MIN_PNL)   &
            (grp['n_trades']  >= MIN_TRADES) &
            (grp['dsr_pvalue'] > 0.5)
        ]

        if eligible.empty:
            best = grp.loc[grp['pair_sharpe'].idxmax()]
            log.info(f'    {pair_key:<12s}  EXCLUDED '
                     f'(best: {best["param_set"]}  '
                     f'PnL={best["pair_pnl"]:+,.0f}  '
                     f'pairSR={best["pair_sharpe"]:.2f}  '
                     f'DSR={best["dsr_pvalue"]:.3f})')
            excluded.append(pair_key)
            continue

        # Pick highest pair_sharpe among DSR-passing rows (more robust than raw PnL)
        best = eligible.sort_values(['pair_sharpe', 'dsr_pvalue'], ascending=False).iloc[0]
        log.info(f'    {pair_key:<12s}  {best["param_set"]:<35s}  '
                 f'PnL={best["pair_pnl"]:+,.0f}  '
                 f'pairSR={best["pair_sharpe"]:.2f}  '
                 f'DSR={best["dsr_pvalue"]:.3f}  '
                 f'trades={int(best["n_trades"])}')
        selected.append([s1, s2, best['param_set']])

    log.info(f'  Selected: {len(selected)}  Excluded: {len(excluded)}')
    return selected, df


# ── Run one OOS test window ───────────────────────────────────────────────────

def run_test_window(window, selected_pairs, window_dir):
    """
    Run selected pairs with their best params on the OOS test window.
    Returns the result dict from PortfolioRun.main().
    """
    if not selected_pairs:
        log.warning(f'  [Window {window["window_idx"]}] No pairs selected — skipping test window.')
        return None

    # Warmup: use training data as warmup, trade only in test window
    start_date       = window['train_start']
    end_date         = window['test_end']
    # trade_start_date = first NYSE trading day of test window (== test_start, already a trading day)
    trade_start_date = window['test_start']

    pairs      = [[s1, s2] for s1, s2, _ in selected_pairs]
    pair_params = {}
    for s1, s2, ps_name in selected_pairs:
        params_dict, _ = Runs._resolve_param_set(ps_name, f'{s1}/{s2}')
        pair_params[f'{s1}/{s2}'] = params_dict

    # Default run-level params (fallback, won't be used since all pairs have overrides)
    default_params, _ = Runs._resolve_param_set('default', 'fallback')

    all_symbols = sorted(set(sym for pair in pairs for sym in pair))
    log.info(f'  [Window {window["window_idx"]}] Loading data for test window '
             f'{window["test_start"]} → {window["test_end"]} ...')
    try:
        historical_data = PortfolioRun.load_historical_data(start_date, end_date, all_symbols)
    except SystemExit:
        log.warning(f'  [Window {window["window_idx"]}] Data load failed (future dates / API error) — skipping.')
        return None

    run_label = f'wf_test_window{window["window_idx"]:02d}_{window["test_start"]}_{window["test_end"]}'
    log.info(f'  [Window {window["window_idx"]}] Running OOS test: {run_label}')

    result = PortfolioRun.main(config={
        'pairs': pairs,
        'params': default_params,
        'pair_params': pair_params,
        'run_label': run_label,
        'output_dir': window_dir,
        'historical_data': historical_data,
        'start_date': start_date,
        'end_date': end_date,
        'trade_start_date': trade_start_date,
    })

    return result


# ── Concatenate OOS equity curve ──────────────────────────────────────────────

def build_oos_curve(window_results):
    """
    Stitch together the OOS equity curves from all test windows.
    Returns a DataFrame with columns: Date, OOS_Equity, OOS_DailyPnL, Window.
    """
    segments = []
    for winfo, result in window_results:
        if result is None:
            continue
        xlsx = result.get('output_file')
        if not xlsx or not os.path.exists(xlsx):
            continue

        try:
            eq = pd.read_excel(xlsx, sheet_name='equity_history')
        except Exception as e:
            log.warning(f'  Cannot read equity from {xlsx}: {e}')
            continue

        eq['Date'] = pd.to_datetime(eq['Date'])

        # Keep only the OOS test period
        test_start = pd.Timestamp(winfo['test_start'])
        test_end   = pd.Timestamp(winfo['test_end'])
        eq_oos = eq[(eq['Date'] >= test_start) & (eq['Date'] <= test_end)].copy()

        if eq_oos.empty:
            continue

        eq_oos = eq_oos.rename(columns={'Value': 'OOS_Equity'}).sort_values('Date')
        # Derive true daily PnL from equity differences (acc_daily_pnl_history is cumulative)
        eq_oos['OOS_DailyPnL'] = eq_oos['OOS_Equity'].diff().fillna(
            eq_oos['OOS_Equity'].iloc[0] - 500_000
        )
        merged = eq_oos
        merged['Window'] = winfo['window_idx']
        merged['n_pairs'] = len(winfo.get('selected_pairs', []))
        segments.append(merged)

    if not segments:
        return pd.DataFrame()

    oos = pd.concat(segments, ignore_index=True).sort_values('Date')

    # Re-base equity: carry forward across windows
    # Each window starts with 500k initial equity; rescale to chain them
    rebased = []
    running_equity = None
    for seg in segments:
        seg = seg.sort_values('Date').copy()
        if running_equity is None:
            seg['OOS_Equity_Chained'] = seg['OOS_Equity']
            running_equity = seg['OOS_Equity'].iloc[-1]
        else:
            # Scale this segment so it starts where the previous ended
            seg_start_equity = seg['OOS_Equity'].iloc[0]
            offset = running_equity - seg_start_equity
            seg['OOS_Equity_Chained'] = seg['OOS_Equity'] + offset
            running_equity = seg['OOS_Equity_Chained'].iloc[-1]
        rebased.append(seg)

    oos_chained = pd.concat(rebased, ignore_index=True).sort_values('Date')
    return oos_chained


def compute_oos_stats(oos_df):
    """Compute summary stats on the chained OOS equity curve."""
    if oos_df.empty or 'OOS_DailyPnL' not in oos_df.columns:
        return {}

    pnl = oos_df['OOS_DailyPnL'].fillna(0)
    total_pnl = float(oos_df['OOS_Equity_Chained'].iloc[-1] - oos_df['OOS_Equity_Chained'].iloc[0])
    n_days    = len(pnl)
    mean_d    = pnl.mean()
    std_d     = pnl.std()
    sharpe    = mean_d / std_d * math.sqrt(252) if std_d > 0 else 0

    equity = oos_df['OOS_Equity_Chained']
    peak   = equity.cummax()
    dd     = (equity - peak)
    max_dd_dollar = float(dd.min())
    max_dd_pct    = float((dd / peak).min()) if peak.max() > 0 else 0

    return {
        'oos_total_pnl': total_pnl,
        'oos_trading_days': n_days,
        'oos_sharpe': sharpe,
        'oos_max_dd_dollar': max_dd_dollar,
        'oos_max_dd_pct': max_dd_pct,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Walk-forward optimization for MRPT')
    parser.add_argument('--mode', default='expanding', choices=['expanding', 'rolling'],
                        help='Window mode: expanding (fixed anchor) or rolling (fixed length) (default: expanding)')
    parser.add_argument('--windows',    type=int, default=4,
                        help='Number of OOS monthly windows (default: 4)')
    parser.add_argument('--oos-windows', type=int, default=6,
                        help='Number of OOS windows (default: 6)')
    parser.add_argument('--oos-days',    type=int, default=150,
                        help='Total OOS NYSE trading days across all windows (default: 150 = 6×25)')
    parser.add_argument('--train-months', type=int, default=18,
                        help='Training period length in months (default: 18)')
    parser.add_argument('--last-date', default=None,
                        help='Last available data date YYYY-MM-DD (default: auto = most recent NYSE trading day <= today)')
    parser.add_argument('--output-dir', default=None,
                        help='Output directory (default: historical_runs/walk_forward/)')
    parser.add_argument('--skip-grid',  action='store_true',
                        help='Skip grid search if summary CSV already exists for a window')
    args = parser.parse_args()

    output_dir = args.output_dir or os.path.join(BASE_DIR, 'historical_runs', 'walk_forward')
    os.makedirs(output_dir, exist_ok=True)

    windows, train_start = build_windows(
        last_date=args.last_date,
        n_windows=args.oos_windows,
        oos_days=args.oos_days,
        train_months=args.train_months,
        mode=args.mode,
    )

    log.info('=' * 65)
    log.info('Walk-Forward Optimization')
    log.info(f'  Mode:        {args.mode}')
    log.info(f'  Train start: {train_start}  ({args.train_months}mo training)')
    log.info(f'  OOS windows: {args.oos_windows} × {args.oos_days // args.oos_windows} NYSE trading days')
    log.info(f'  OOS period:  {windows[0]["test_start"]} → {windows[-1]["test_end"]}')
    log.info(f'  Output dir:  {output_dir}')
    log.info('=' * 65)

    log.info('\nWindow plan:')
    for w in windows:
        log.info(f'  Window {w["window_idx"]}: '
                 f'train {w["train_start"]} → {w["train_end"]}  |  '
                 f'test  {w["test_start"]} → {w["test_end"]}')

    window_results = []
    all_selection_dfs = []

    for window in windows:
        log.info(f'\n{"=" * 65}')
        log.info(f'WINDOW {window["window_idx"]}/{len(windows)}: '
                 f'train {window["train_start"]}→{window["train_end"]}  '
                 f'test {window["test_start"]}→{window["test_end"]}')
        log.info('=' * 65)

        window_dir = os.path.join(output_dir,
                                  f'window{window["window_idx"]:02d}_'
                                  f'{window["train_start"]}_{window["train_end"]}')
        os.makedirs(window_dir, exist_ok=True)

        # Step A: grid search on training window
        summary_csv = run_grid_for_window(window, window_dir, skip_if_exists=args.skip_grid)

        # Step B: DSR-filtered pair selection
        selected_pairs, selection_df = select_pairs_with_dsr(
            summary_csv, window, n_trials=len(Runs.PARAM_SETS)
        )
        window['selected_pairs'] = selected_pairs

        if not selection_df.empty:
            selection_df['window_idx'] = window['window_idx']
            selection_df['train_end']  = window['train_end']
            all_selection_dfs.append(selection_df)

        # Save selection for this window
        if selected_pairs:
            sel_path = os.path.join(window_dir, 'selected_pairs.json')
            with open(sel_path, 'w') as f:
                json.dump({'window': window, 'selected_pairs': selected_pairs}, f, indent=2)

        # Step C: run OOS test window
        result = run_test_window(window, selected_pairs, window_dir)
        window_results.append((window, result))

        if result:
            log.info(f'\n  OOS result: Equity={result["final_equity"]:.0f}  '
                     f'PnL={result["acc_pnl"]:.0f}  Sharpe={result["sharpe_ratio"]:.3f}')

    # ── Build and save chained OOS curve ──────────────────────────────────────
    log.info(f'\n{"=" * 65}')
    log.info('CHAINED OOS EQUITY CURVE')
    log.info('=' * 65)

    oos_df = build_oos_curve(window_results)
    stats  = compute_oos_stats(oos_df)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    if not oos_df.empty:
        oos_path = os.path.join(output_dir, f'oos_equity_curve_{ts}.csv')
        oos_df.to_csv(oos_path, index=False)
        log.info(f'OOS curve saved: {oos_path}')

        log.info(f'\nOOS Summary:')
        log.info(f'  Total OOS PnL:    ${stats["oos_total_pnl"]:+,.0f}')
        log.info(f'  OOS Sharpe:       {stats["oos_sharpe"]:.3f}')
        log.info(f'  OOS Max Drawdown: ${stats["oos_max_dd_dollar"]:,.0f} '
                 f'({stats["oos_max_dd_pct"]:.2%})')
        log.info(f'  OOS Trading Days: {stats["oos_trading_days"]}')

    # ── Save full DSR selection log ────────────────────────────────────────────
    if all_selection_dfs:
        dsr_log_path = os.path.join(output_dir, f'dsr_selection_log_{ts}.csv')
        pd.concat(all_selection_dfs, ignore_index=True).to_csv(dsr_log_path, index=False)
        log.info(f'DSR selection log: {dsr_log_path}')

    # ── Save overall summary ───────────────────────────────────────────────────
    summary = {
        'generated_at': datetime.now().isoformat(),
        'mode': args.mode,
        'train_start': train_start,
        'train_months': args.train_months,
        'oos_windows': args.oos_windows,
        'oos_days': args.oos_days,
        'windows': [
            {
                'window_idx':     w['window_idx'],
                'train_start':    w['train_start'],
                'train_end':      w['train_end'],
                'test_start':     w['test_start'],
                'test_end':       w['test_end'],
                'n_selected_pairs': len(w.get('selected_pairs', [])),
                'selected_pairs': w.get('selected_pairs', []),
                'oos_sharpe': result['sharpe_ratio'] if result else None,
                'oos_pnl':    result['acc_pnl'] if result else None,
            }
            for w, result in window_results
        ],
        'oos_stats': stats,
    }
    summary_path = os.path.join(output_dir, f'walk_forward_summary_{ts}.json')
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    log.info(f'Walk-forward summary: {summary_path}')

    log.info('\nDone.')
    return oos_df, stats


if __name__ == '__main__':
    main()
