"""
MTFSUpdateConfigs.py — Read Step 1 grid search results and
auto-fill Step 2 (backtest) and Step 3 (forward) config files.

Step 1 runs all 15 pairs × 31 param_sets = 31 runs.
Each run's Excel output contains per-pair PnL in the acc_pair_trade_pnl_history sheet
and day-over-day PnL in the dod_pair_trade_pnl_history sheet.
This script reads every run's Excel, extracts per-pair PnL + Sharpe, applies
Deflated Sharpe Ratio correction for multiple comparisons, then picks the best
param_set per pair.

Usage:
    python MTFSUpdateConfigs.py [mtfs_strategy_summary_<ts>.csv]

    If no CSV is given, uses the most recent mtfs_strategy_summary_*.csv in historical_runs/.

Selection criteria (edit constants below):
    MIN_PNL    > 0      pair's final acc PnL in that run must be positive
    MIN_TRADES >= 3     number of open orders for that pair >= 3
    MIN_DSR    > 0.5    Deflated Sharpe Ratio p-value must exceed 0.5
                        (corrects for 30 trials per pair; DSR > 0.5 ↔ result is
                        statistically meaningful after multiple-comparison penalty)

For each pair:
    - Collect (param_set, pair_pnl, pair_sharpe, dsr_pvalue, n_trades) across all 30 runs
    - Filter by criteria (DSR replaces the old run-level sharpe filter)
    - Pick the param_set with highest pair_sharpe (primary) then pair_pnl (tiebreak)
    - Pairs with no qualifying row are excluded from Step 2/3

Outputs (overwrites):
    run_configs/mtfs_runs_step2_best_backtest.json
    run_configs/mtfs_runs_step3_forward.json
"""

import sys
import os
import json
import glob
import math
import logging

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from scipy.stats import norm

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

# ── Selection thresholds ─────────────────────────────────────────────────────
MIN_PNL    = 0      # pair-level final acc_pnl must be > this
MIN_TRADES = 3      # pair's number of open orders must be >= this
MIN_DSR    = 0.5    # Deflated Sharpe Ratio p-value must be > this (multiple-comparison correction)
N_TRIALS   = 31     # number of param_sets tested (for DSR benchmark)

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STEP2_PATH = os.path.join(BASE_DIR, 'run_configs', 'mtfs_runs_step2_best_backtest.json')
STEP3_PATH = os.path.join(BASE_DIR, 'run_configs', 'mtfs_runs_step3_forward.json')

from pair_universe import mtfs_pairs
ALL_PAIRS = mtfs_pairs()


def find_latest_summary():
    pattern = os.path.join(BASE_DIR, 'historical_runs', 'mtfs_strategy_summary_*.csv')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        raise FileNotFoundError('No mtfs_strategy_summary CSV found in historical_runs/')
    return files[0]


def next_weekday(date_str):
    d = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime('%Y-%m-%d')


# ── Deflated Sharpe Ratio (Bailey & López de Prado, 2014) ─────────────────────

def deflated_sharpe_ratio(is_sharpe, n_trials, n_obs, skew=0.0, kurt=3.0):
    """
    Deflated Sharpe Ratio — corrects IS Sharpe for number of trials, skew, kurtosis.

    Returns a p-value (0–1). Values > 0.5 indicate the Sharpe is likely genuine
    (not an artifact of searching over N param_sets).
    """
    if n_obs < 5 or n_trials < 1:
        return 0.0

    gamma = 0.5772156649  # Euler-Mascheroni constant
    e     = math.e

    def z(p):
        p = max(1e-10, min(1 - 1e-10, p))
        return norm.ppf(p)

    # Expected max SR among n_trials iid standard normals
    sr_expected  = (1 - gamma) * z(1 - 1 / n_trials) + gamma * z(1 - 1 / (n_trials * e))
    sr_benchmark = sr_expected * (1 / math.sqrt(n_obs))

    # Variance correction for non-normality
    var_factor = 1 - skew * is_sharpe + (kurt - 1) / 4 * is_sharpe ** 2
    if var_factor <= 0:
        var_factor = 1e-6

    sr_std = math.sqrt(var_factor / (n_obs - 1))
    if sr_std == 0:
        return 0.0

    return float(norm.cdf((is_sharpe - sr_benchmark) / sr_std))


def get_pair_data(xlsx_path, pair_key):
    """
    From a run's Excel output, return (final_pnl, pair_sharpe, skew, kurt, n_obs, n_trades).

    - final_pnl:   last value in acc_pair_trade_pnl_history
    - pair_sharpe: annualised Sharpe from dod_pair_trade_pnl_history (per-pair, not run-level)
    - n_trades:    number of 'open' rows in pair_trade_history
    """
    try:
        acc = pd.read_excel(xlsx_path, sheet_name='acc_pair_trade_pnl_history')
        dod = pd.read_excel(xlsx_path, sheet_name='dod_pair_trade_pnl_history')
        pt  = pd.read_excel(xlsx_path, sheet_name='pair_trade_history')
    except Exception as e:
        log.warning(f'  Cannot read {os.path.basename(xlsx_path)}: {e}')
        return None, None, 0.0, 3.0, 0, 0

    # Final acc PnL for this pair
    pair_acc = acc[acc['Pair'] == pair_key]
    if pair_acc.empty:
        return None, None, 0.0, 3.0, 0, 0
    final_pnl = pair_acc['PnL Dollar'].iloc[-1]

    # Trade count
    pair_pt  = pt[pt['Pair'] == pair_key]
    n_trades = int((pair_pt['Order Type'] == 'open').sum())

    # Per-pair daily PnL for Sharpe calculation
    pair_dod = dod[dod['Pair'] == pair_key]['PnL Dollar'].dropna() if 'Pair' in dod.columns else pd.Series(dtype=float)

    if len(pair_dod) < 5:
        return final_pnl, None, 0.0, 3.0, 0, n_trades

    std = pair_dod.std()
    if std == 0:
        return final_pnl, 0.0, 0.0, 3.0, len(pair_dod), n_trades

    sharpe = pair_dod.mean() / std * math.sqrt(252)
    skew   = float(pair_dod.skew())
    kurt   = float(pair_dod.kurtosis()) + 3  # scipy gives excess kurtosis; DSR needs full kurtosis
    n_obs  = len(pair_dod)

    return final_pnl, sharpe, skew, kurt, n_obs, n_trades


def main(csv_path=None, train_start=None):
    if csv_path is None:
        csv_path = find_latest_summary()
    log.info(f'Reading summary: {csv_path}')

    summary = pd.read_csv(csv_path)
    log.info(f'Total runs in summary: {len(summary)}')

    # Build records: for each (pair, param_set) extract pair-level stats from Excel
    records = []

    total = len(summary)
    for idx, row in summary.iterrows():
        param_set = row['param_set']
        xlsx_path = row.get('output_file', '')

        log.info(f'[{idx+1:3d}/{total}] param_set={param_set:<35s}  file={os.path.basename(xlsx_path)}')

        if not xlsx_path or not os.path.exists(xlsx_path):
            log.warning(f'  output_file missing or not found: {xlsx_path}')
            continue

        for s1, s2 in ALL_PAIRS:
            pair_key = f'{s1}/{s2}'
            final_pnl, pair_sharpe, skew, kurt, n_obs, n_trades = get_pair_data(xlsx_path, pair_key)
            if final_pnl is None:
                continue

            dsr_pvalue = deflated_sharpe_ratio(
                is_sharpe=pair_sharpe if pair_sharpe is not None else 0.0,
                n_trials=N_TRIALS,
                n_obs=n_obs if n_obs > 0 else 1,
                skew=skew,
                kurt=kurt,
            ) if pair_sharpe is not None else 0.0

            records.append({
                'pair_key':    pair_key,
                's1':          s1,
                's2':          s2,
                'param_set':   param_set,
                'pair_pnl':    final_pnl,
                'pair_sharpe': pair_sharpe if pair_sharpe is not None else float('nan'),
                'dsr_pvalue':  dsr_pvalue,
                'n_trades':    n_trades,
                'n_obs':       n_obs,
            })

    if not records:
        log.error('No records extracted — check that output_file paths in the CSV are valid.')
        return

    df = pd.DataFrame(records)
    log.info(f'\nExtracted {len(df)} pair×param_set records')

    # Save the per-pair breakdown for inspection
    breakdown_path = os.path.join(BASE_DIR, 'historical_runs',
                                  f'mtfs_grid_pair_breakdown_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv')
    df.to_csv(breakdown_path, index=False)
    log.info(f'Per-pair breakdown saved: {breakdown_path}')

    # Find best param_set per pair
    log.info('')
    log.info('── Best param set per pair (DSR-filtered) ───────────────────────────────')
    selected_pairs = []
    excluded = []

    for s1, s2 in ALL_PAIRS:
        pair_key = f'{s1}/{s2}'
        grp = df[df['pair_key'] == pair_key]
        if grp.empty:
            log.info(f'  {pair_key:<12s}  NO DATA — excluded')
            excluded.append(pair_key)
            continue

        eligible = grp[
            (grp['pair_pnl']   > MIN_PNL)    &
            (grp['n_trades']  >= MIN_TRADES)  &
            (grp['dsr_pvalue'] > MIN_DSR)
        ]

        if eligible.empty:
            best = grp.loc[grp['pair_pnl'].idxmax()]
            dsr_str = f'{best["dsr_pvalue"]:.3f}' if not pd.isna(best['dsr_pvalue']) else 'N/A'
            log.info(f'  {pair_key:<12s}  no row passes DSR filter '
                     f'(best: {best["param_set"]}  PnL={best["pair_pnl"]:+,.0f}  '
                     f'trades={int(best["n_trades"])}  DSR={dsr_str})  ← EXCLUDED')
            excluded.append(pair_key)
            continue

        # Primary sort: pair_sharpe desc; tiebreak: pair_pnl desc
        best = eligible.sort_values(['pair_sharpe', 'pair_pnl'], ascending=False).iloc[0]
        log.info(f'  {pair_key:<12s}  best={best["param_set"]:<35s}  '
                 f'PnL={best["pair_pnl"]:+,.0f}  Sharpe={best["pair_sharpe"]:.3f}  '
                 f'DSR={best["dsr_pvalue"]:.3f}  trades={int(best["n_trades"])}')
        selected_pairs.append([s1, s2, best['param_set']])

    log.info('')
    log.info(f'Selected: {len(selected_pairs)} pairs   Excluded: {len(excluded)} pairs')
    if excluded:
        log.info(f'  Excluded: {excluded}')

    if not selected_pairs:
        log.error('No pairs selected — aborting config update.')
        return

    # Resolve effective train_start (passed in or auto-computed as 18 months before today)
    if train_start is None:
        from dateutil.relativedelta import relativedelta
        train_start = (datetime.now() - relativedelta(months=18)).strftime('%Y-%m-%d')
        log.info(f'  (auto-computed train_start = {train_start})')

    log.info(f'\nBacktest end:        auto_minus_70d')
    log.info(f'Forward trade_start: auto_minus_70d')
    if train_start:
        log.info(f'Train start:         {train_start}')

    # Write Step 2 config — ONE run, all selected pairs together, each with its own best param_set.
    # pairs format: [[s1, s2, best_param_set], ...] — the third element is the per-pair override.
    # run-level "param_set" is a fallback only; each pair ignores it and uses its own.
    step2 = {
        '_comment': (
            f'Step 2 — Backtest: {len(selected_pairs)} selected pairs in ONE combined portfolio run. '
            f'Each pair entry [s1, s2, param_set] uses its own best param_set from Step 1. '
            f'Auto-generated {datetime.now().strftime("%Y-%m-%d %H:%M")}. '
            f'Criteria: pair_pnl>{MIN_PNL}, trades>={MIN_TRADES}, DSR>{MIN_DSR} (N={N_TRIALS} trials).'
        ),
        'start_date': train_start,
        'end_date': 'auto_minus_70d',
        'runs': [
            {
                'label': 'step2_best_per_pair',
                'param_set': 'default',
                'pairs': selected_pairs,
            }
        ]
    }
    with open(STEP2_PATH, 'w') as f:
        json.dump(step2, f, indent=2)
    log.info(f'\nWritten: {STEP2_PATH}')

    # Write Step 3 config — identical structure to Step 2 (one run, same pairs + per-pair params).
    # Only differences: end_date=auto (today) and trade_start_date (forward window start).
    step3 = {
        '_comment': (
            f'Step 3 — Forward/validation: same {len(selected_pairs)} pairs + per-pair param_sets as Step 2. '
            f'ONE combined portfolio run. '
            f'trade_start_date=auto_minus_70d (forward window start). '
            f'Auto-generated {datetime.now().strftime("%Y-%m-%d %H:%M")}.'
        ),
        'start_date': train_start,
        'end_date': 'auto',
        'trade_start_date': 'auto_minus_70d',
        'runs': [
            {
                'label': 'step3_forward',
                'param_set': 'default',
                'pairs': selected_pairs,
            }
        ]
    }
    with open(STEP3_PATH, 'w') as f:
        json.dump(step3, f, indent=2)
    log.info(f'Written: {STEP3_PATH}')

    # Auto-update PARAM_MAP in MTFSGenerateReport.py so the report shows correct per-pair params
    report_path = os.path.join(BASE_DIR, 'MTFSGenerateReport.py')
    if os.path.exists(report_path):
        # Build new PARAM_MAP: selected pairs use best param, excluded pairs fall back to 'default'
        all_pair_keys = [f'{s1}/{s2}' for s1, s2 in ALL_PAIRS]
        best_map = {f'{s1}/{s2}': ps for s1, s2, ps in selected_pairs}
        lines = []
        for i, pk in enumerate(all_pair_keys):
            param = best_map.get(pk, 'default')
            sep = ',' if i < len(all_pair_keys) - 1 else ''
            lines.append(f"    '{pk}':  '{param}'{sep}")
        new_block = 'PARAM_MAP = {\n' + '\n'.join(lines) + '\n}'

        with open(report_path, 'r') as f:
            src = f.read()

        import re
        src = re.sub(r'PARAM_MAP\s*=\s*\{[^}]*\}', new_block, src, count=1)
        with open(report_path, 'w') as f:
            f.write(src)
        log.info(f'Updated PARAM_MAP in MTFSGenerateReport.py')
        for pk in all_pair_keys:
            log.info(f'  {pk:<12s}  →  {best_map.get(pk, "default (excluded)")}')
    else:
        log.warning(f'MTFSGenerateReport.py not found at {report_path} — PARAM_MAP not updated')

    log.info('')
    log.info('Next steps:')
    log.info(f'  python PortfolioMTFSStrategyRuns.py {STEP2_PATH}')
    log.info(f'  python PortfolioMTFSStrategyRuns.py {STEP3_PATH}')
    log.info(f'  python MTFSGenerateReport.py <step2_output.xlsx> <step3_output.xlsx>')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Update step2/step3 configs from MTFS grid search results')
    parser.add_argument('csv', nargs='?', default=None,
                        help='mtfs_strategy_summary CSV (default: latest in historical_runs/)')
    parser.add_argument('--train-start', default=None,
                        help='Training start date YYYY-MM-DD (default: auto = 18mo before today)')
    pargs = parser.parse_args()
    main(pargs.csv, train_start=pargs.train_start)
