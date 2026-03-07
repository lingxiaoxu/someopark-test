#!/usr/bin/env python3
"""
MTFSWalkForwardReport.py — Walk-forward OOS performance report generator for MTFS.

Automatically finds the most recent walk-forward run in historical_runs/walk_forward_mtfs/,
reads all OOS test Excel files, and produces:
  1. Window-level OOS summary (PnL, Sharpe, MaxDD per window)
  2. Pair-level OOS breakdown (PnL, Sharpe, MaxDD, WinRate, Trades, Turnover)
  3. Chained OOS equity curve CSV
  4. Full report saved to historical_runs/walk_forward_mtfs/oos_report_<ts>.txt

MTFS-specific notes:
  - Reads from historical_runs/walk_forward_mtfs/ (vs walk_forward/ for MRPT)
  - OOS windows are 20 trading days each (vs 25 for MRPT)
  - Momentum stop-loss breakdown reported: Momentum Decay, Pair P&L,
    Volatility Stop, Time-based exits (all 4 MTFS-specific mechanisms)
  - File pattern: wf_test_window* (same as MRPT)

Usage:
    conda run -n someopark_run python MTFSWalkForwardReport.py
    conda run -n someopark_run python MTFSWalkForwardReport.py --wf-dir historical_runs/walk_forward_mtfs
    conda run -n someopark_run python MTFSWalkForwardReport.py --run-prefix 2024-06-01
"""

import os
import sys
import re
import glob
import math
import json
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALL_PAIRS = [
    'MSCI/LII', 'D/MCHP', 'DG/MOS', 'ESS/EXPD', 'ACGL/UHS',
    'AAPL/META', 'YUM/MCD', 'GS/ALLY', 'CL/USO', 'ALGN/UAL',
    'ARES/CG', 'AMG/BEN', 'LYFT/UBER', 'TW/CME', 'CART/DASH',
]


# ── helpers ───────────────────────────────────────────────────────────────────

def _find_oos_windows(wf_dir, run_prefix=None):
    """
    Auto-detect OOS test windows in wf_dir.

    Returns list of dicts sorted by window index:
      {window_idx, test_start, test_end, xlsx, wdir, selected_pairs}

    If run_prefix is given (e.g. '2024-06-01'), only windows whose directory
    name contains that prefix are included. Otherwise uses the most recent run
    (highest window count, latest mtime).
    """
    # Find all windowNN_* dirs
    pattern = os.path.join(wf_dir, 'window*_????-??-??_????-??-??')
    all_dirs = sorted(glob.glob(pattern))

    # Filter by prefix if given
    if run_prefix:
        all_dirs = [d for d in all_dirs if run_prefix in os.path.basename(d)]
    else:
        # Pick the set of window dirs that share the same train_start anchor
        # Group by train_start (part after windowNN_)
        from collections import defaultdict
        groups = defaultdict(list)
        for d in all_dirs:
            m = re.search(r'window(\d+)_(\d{4}-\d{2}-\d{2})_', os.path.basename(d))
            if m:
                groups[m.group(2)].append(d)
        if not groups:
            return []
        # Pick the group with the most windows (most complete run)
        best_anchor = max(groups, key=lambda k: (len(groups[k]), max(os.path.getmtime(d) for d in groups[k])))
        all_dirs = groups[best_anchor]

    windows = []
    for wdir in sorted(all_dirs):
        m = re.search(r'window(\d+)_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})', os.path.basename(wdir))
        if not m:
            continue
        widx = int(m.group(1))

        # Find OOS test xlsx — MTFS files use portfolio_history_MTFS_wf_test_window* pattern
        xlsx_list = glob.glob(os.path.join(wdir, 'historical_runs',
                                            f'portfolio_history_MTFS_wf_test_window{widx:02d}_*.xlsx'))
        if not xlsx_list:
            # Fallback: no MTFS_ prefix
            xlsx_list = glob.glob(os.path.join(wdir, 'historical_runs',
                                               f'portfolio_history_wf_test_window{widx:02d}_*.xlsx'))
        if not xlsx_list:
            # Also check directly in window_dir
            xlsx_list = glob.glob(os.path.join(wdir,
                                               f'portfolio_history_MTFS_wf_test_window{widx:02d}_*.xlsx'))
        if not xlsx_list:
            xlsx_list = glob.glob(os.path.join(wdir,
                                               f'portfolio_history_wf_test_window{widx:02d}_*.xlsx'))
        if not xlsx_list:
            continue
        xlsx = sorted(xlsx_list, key=os.path.getmtime)[-1]

        # Parse test dates from filename
        fm = re.search(r'wf_test_window\d+_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})', xlsx)
        if not fm:
            continue

        # Load selected pairs
        sel_path = os.path.join(wdir, 'selected_pairs.json')
        selected = []
        if os.path.exists(sel_path):
            with open(sel_path) as f:
                selected = json.load(f).get('selected_pairs', [])

        windows.append({
            'window_idx':     widx,
            'test_start':     fm.group(1),
            'test_end':       fm.group(2),
            'xlsx':           xlsx,
            'wdir':           wdir,
            'selected_pairs': selected,
        })

    return sorted(windows, key=lambda w: w['window_idx'])


def _load_window_data(w):
    """Load equity, daily PnL, pair dod PnL, and trade history for one window."""
    xlsx       = w['xlsx']
    test_start = pd.Timestamp(w['test_start'])
    test_end   = pd.Timestamp(w['test_end'])

    def _read(sheet):
        df = pd.read_excel(xlsx, sheet_name=sheet)
        df['Date'] = pd.to_datetime(df['Date'])
        return df[(df['Date'] >= test_start) & (df['Date'] <= test_end)].copy()

    xf = pd.ExcelFile(xlsx)
    sheet_names = xf.sheet_names

    eq     = _read('equity_history').rename(columns={'Value': 'Equity'}).sort_values('Date')
    dpnl   = _read('daily_pnl_history')   # true daily PnL (portfolio level)
    dod    = _read('dod_pair_trade_pnl_history')
    trades = _read('pair_trade_history')
    interest = _read('interest_expense_history') if 'interest_expense_history' in sheet_names \
        else pd.DataFrame()

    return eq, dpnl, dod, trades, interest


# ── window-level stats ────────────────────────────────────────────────────────

def _window_stats(eq, dpnl):
    """Return dict of OOS stats for one window."""
    if eq.empty:
        return {'pnl': 0, 'sharpe': 0, 'max_dd': 0, 'max_dd_pct': 0, 'n_days': 0}

    equity_vals = eq['Equity'].values
    pnl   = float(equity_vals[-1] - equity_vals[0])
    n_days = len(dpnl)

    daily = dpnl['Daily PnL'].values
    mean_d = np.mean(daily)
    std_d  = np.std(daily, ddof=1)
    sharpe = mean_d / std_d * math.sqrt(252) if std_d > 0 else 0

    peak   = np.maximum.accumulate(equity_vals)
    dd     = equity_vals - peak
    max_dd = float(dd.min())
    max_dd_pct = max_dd / equity_vals[0] if equity_vals[0] > 0 else 0

    return {'pnl': pnl, 'sharpe': sharpe, 'max_dd': max_dd,
            'max_dd_pct': max_dd_pct, 'n_days': n_days}


# ── pair-level stats ──────────────────────────────────────────────────────────

def _pair_stats(pair, all_dod, all_trades, starting_equity=500_000):
    """Compute OOS stats for one pair across all windows."""
    pdod = all_dod[all_dod['Pair'] == pair].sort_values('Date')
    pt   = all_trades[all_trades['Pair'] == pair]

    if pdod.empty:
        return None

    daily   = pdod['PnL Dollar'].values
    cum_pnl = float(np.sum(daily))
    n_days  = len(daily)

    mean_d = np.mean(daily)
    std_d  = np.std(daily, ddof=1)
    sharpe = mean_d / std_d * math.sqrt(252) if std_d > 0 else 0

    # MaxDD on cumulative PnL
    cum  = np.cumsum(daily)
    peak = np.maximum.accumulate(cum)
    dd   = cum - peak
    max_dd = float(dd.min())
    max_dd_pct = max_dd / starting_equity  # relative to starting equity

    # Round-trip trade count: each open event = one unique trade date per pair
    opens = pt[pt['Order Type'] == 'open']
    n_trades = len(opens['Date'].unique())

    # Win rate: fraction of trading days with positive PnL (days with position)
    active = daily[daily != 0]
    win_rate = float((active > 0).sum() / len(active)) if len(active) > 0 else None

    # Turnover: total shares (absolute) traded
    turnover = float(pt['Amount'].abs().sum())

    return {
        'Pair':       pair,
        'OOS_PnL':    cum_pnl,
        'Sharpe':     sharpe,
        'MaxDD':      max_dd,
        'MaxDD_pct':  max_dd_pct,
        'WinRate':    win_rate,
        'N_Trades':   n_trades,
        'Turnover':   turnover,
        'N_Days':     n_days,
    }


def _stop_loss_breakdown(all_trades):
    """
    Summarise MTFS-specific stop-loss mechanism triggers across all OOS windows.

    MTFS has 4 stop-loss mechanisms:
      1. Momentum Decay  — exit_reason contains 'momentum_decay'
      2. Pair P&L Stop   — exit_reason contains 'pair_stop' or 'stop_loss'
      3. Volatility Stop — exit_reason contains 'vol_stop' or 'volatility'
      4. Time-based      — exit_reason contains 'time' or 'max_holding'

    Returns a dict with counts per mechanism, or empty dict if no exit_reason column.
    """
    if 'Exit Reason' not in all_trades.columns and 'exit_reason' not in all_trades.columns:
        return {}

    col = 'Exit Reason' if 'Exit Reason' in all_trades.columns else 'exit_reason'
    closes = all_trades[all_trades['Order Type'] == 'close'].copy()
    reasons = closes[col].fillna('').str.lower()

    counts = {
        'Momentum Decay': int(reasons.str.contains('momentum_decay|decay').sum()),
        'Pair P&L Stop':  int(reasons.str.contains('pair_stop|stop_loss|pnl_stop').sum()),
        'Volatility Stop': int(reasons.str.contains('vol_stop|volatility').sum()),
        'Time-based':     int(reasons.str.contains('time|max_holding|holding_period').sum()),
        'Other':          int(reasons[~reasons.str.contains(
            'momentum_decay|decay|pair_stop|stop_loss|pnl_stop|vol_stop|volatility|time|max_holding|holding_period'
        )].shape[0]),
    }
    counts['Total Exits'] = len(closes)
    return counts


# ── chained equity curve ──────────────────────────────────────────────────────

def _build_chained_curve(windows_data):
    """Build chained OOS equity curve across windows."""
    segments = []
    running_equity = None

    for winfo, (eq, dpnl, dod, trades, interest) in windows_data:
        if eq.empty:
            continue
        seg = eq[['Date', 'Equity']].copy()
        seg['DailyPnL'] = dpnl.set_index('Date').reindex(seg['Date'])['Daily PnL'].values
        seg['Window'] = winfo['window_idx']

        if running_equity is None:
            seg['Equity_Chained'] = seg['Equity']
        else:
            offset = running_equity - seg['Equity'].iloc[0]
            seg['Equity_Chained'] = seg['Equity'] + offset
        running_equity = seg['Equity_Chained'].iloc[-1]
        segments.append(seg)

    if not segments:
        return pd.DataFrame()
    return pd.concat(segments, ignore_index=True).sort_values('Date')


def _chained_stats(curve):
    """Compute total OOS stats from chained curve."""
    if curve.empty:
        return {}
    daily = curve['DailyPnL'].fillna(0).values
    total_pnl = float(curve['Equity_Chained'].iloc[-1] - curve['Equity_Chained'].iloc[0])
    mean_d = np.mean(daily)
    std_d  = np.std(daily, ddof=1)
    sharpe = mean_d / std_d * math.sqrt(252) if std_d > 0 else 0

    equity = curve['Equity_Chained'].values
    peak   = np.maximum.accumulate(equity)
    dd     = equity - peak
    max_dd = float(dd.min())
    max_dd_pct = max_dd / equity[0] if equity[0] > 0 else 0

    return {
        'total_pnl':   total_pnl,
        'sharpe':      sharpe,
        'max_dd':      max_dd,
        'max_dd_pct':  max_dd_pct,
        'n_days':      len(daily),
    }


# ── main report ───────────────────────────────────────────────────────────────

def generate_report(wf_dir=None, run_prefix=None):
    if wf_dir is None:
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward_mtfs')

    windows = _find_oos_windows(wf_dir, run_prefix)
    if not windows:
        print(f'ERROR: No OOS windows found in {wf_dir}')
        sys.exit(1)

    print(f'Found {len(windows)} MTFS OOS windows in: {wf_dir}')
    anchor = re.search(r'window01_(\d{4}-\d{2}-\d{2})', windows[0]['wdir'])
    if anchor:
        print(f'Run anchor (train_start): {anchor.group(1)}')

    # Load all window data
    windows_data = []
    for w in windows:
        try:
            data = _load_window_data(w)
            windows_data.append((w, data))
        except Exception as e:
            print(f'  WARNING: Could not load W{w["window_idx"]}: {e}')

    # ── Section 1: Window-level summary ──────────────────────────────────────
    lines = []
    lines.append('=' * 72)
    lines.append('MTFS WALK-FORWARD OOS REPORT')
    lines.append(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'Strategy:  MTFS (Momentum Trend Following)')
    lines.append(f'Windows:   {len(windows_data)}  (× 20 NYSE trading days each)')
    lines.append('=' * 72)
    lines.append('')

    lines.append('WINDOW-LEVEL OOS SUMMARY')
    lines.append('-' * 72)
    lines.append(f"{'Window':<8} {'OOS Period':<28} {'PnL':>10} {'Sharpe':>7} {'MaxDD':>10} {'MaxDD%':>7} {'Days':>5} {'Pairs':>6}")
    lines.append('-' * 72)

    window_stats_list = []
    for w, (eq, dpnl, dod, trades, interest) in windows_data:
        ws = _window_stats(eq, dpnl)
        n_pairs = len(w['selected_pairs'])
        period = f"{w['test_start']} → {w['test_end']}"
        lines.append(
            f"  W{w['window_idx']:<5} {period:<28} {ws['pnl']:>+10,.0f} "
            f"{ws['sharpe']:>7.2f} {ws['max_dd']:>10,.0f} "
            f"{ws['max_dd_pct']:>7.1%} {ws['n_days']:>5} {n_pairs:>6}"
        )
        window_stats_list.append((w, ws))

    lines.append('-' * 72)

    # Chained curve & overall stats
    curve = _build_chained_curve(windows_data)
    cs    = _chained_stats(curve)

    n_pos = sum(1 for _, ws in window_stats_list if ws['pnl'] > 0)
    n_neg = len(window_stats_list) - n_pos
    lines.append('')
    lines.append('CHAINED OOS SUMMARY')
    lines.append('-' * 72)
    lines.append(f"  Total OOS PnL:      ${cs.get('total_pnl', 0):>+12,.0f}  (equity-based, net of interest)")
    lines.append(f"  OOS Sharpe:         {cs.get('sharpe', 0):>8.3f}")
    lines.append(f"  OOS Max Drawdown:   ${cs.get('max_dd', 0):>12,.0f}  ({cs.get('max_dd_pct', 0):.2%})")
    lines.append(f"  OOS Trading Days:   {cs.get('n_days', 0):>8}")
    lines.append(f"  Windows positive:   {n_pos}/{len(window_stats_list)}")
    lines.append('')

    # ── Section 2: Interest expense ───────────────────────────────────────────
    total_interest = 0.0
    for w, (eq, dpnl, dod, trades, interest) in windows_data:
        if not interest.empty and 'Value' in interest.columns:
            total_interest += float(interest['Value'].sum())

    if total_interest != 0:
        lines.append(f"  Interest expense:   ${-total_interest:>12,.0f}  (margin borrowing cost)")
        lines.append(f"  Gross trading PnL:  ${cs.get('total_pnl', 0) + total_interest:>+12,.0f}  (before interest)")
        lines.append('')

    # ── Section 3: MTFS stop-loss breakdown ──────────────────────────────────
    all_trades_combined = pd.concat(
        [trades for _, (eq, dpnl, dod, trades, interest) in windows_data],
        ignore_index=True
    )
    sl_breakdown = _stop_loss_breakdown(all_trades_combined)

    if sl_breakdown:
        lines.append('MTFS STOP-LOSS MECHANISM BREAKDOWN  (OOS exits)')
        lines.append('-' * 72)
        total_exits = sl_breakdown.get('Total Exits', 1) or 1
        for mechanism in ['Momentum Decay', 'Pair P&L Stop', 'Volatility Stop', 'Time-based', 'Other']:
            count = sl_breakdown.get(mechanism, 0)
            pct = count / total_exits if total_exits > 0 else 0
            lines.append(f"  {mechanism:<20s}  {count:>5}  ({pct:.0%})")
        lines.append(f"  {'Total exits':<20s}  {total_exits:>5}")
        lines.append('')

    # ── Section 4: Pair-level breakdown ──────────────────────────────────────
    all_dod    = pd.concat([dod    for _, (eq, dpnl, dod, trades, interest) in windows_data], ignore_index=True)

    pair_rows = []
    for pair in ALL_PAIRS:
        ps = _pair_stats(pair, all_dod, all_trades_combined)
        if ps:
            pair_rows.append(ps)
        else:
            pair_rows.append({'Pair': pair, 'OOS_PnL': 0, 'Sharpe': 0,
                               'MaxDD': 0, 'MaxDD_pct': 0, 'WinRate': None,
                               'N_Trades': 0, 'Turnover': 0, 'N_Days': 0})

    pair_df = pd.DataFrame(pair_rows).sort_values('OOS_PnL', ascending=False)

    lines.append('PAIR-LEVEL OOS BREAKDOWN  (gross trading PnL, excl. interest)')
    lines.append('-' * 72)
    lines.append(f"{'Pair':<12} {'OOS PnL':>10} {'Sharpe':>7} {'MaxDD':>10} {'MaxDD%':>7} {'WinRate':>8} {'Trades':>7} {'Turnover':>10}")
    lines.append('-' * 72)

    for _, r in pair_df.iterrows():
        wr = f"{r['WinRate']:.0%}" if r['WinRate'] is not None else '   n/a'
        lines.append(
            f"{r['Pair']:<12} {r['OOS_PnL']:>+10,.0f} {r['Sharpe']:>7.2f} "
            f"{r['MaxDD']:>10,.0f} {r['MaxDD_pct']:>7.1%} {wr:>8} "
            f"{int(r['N_Trades']):>7} {r['Turnover']:>10,.0f}"
        )

    lines.append('-' * 72)
    totals = pair_df.agg({'OOS_PnL': 'sum', 'N_Trades': 'sum', 'Turnover': 'sum'})
    lines.append(
        f"{'GROSS TOTAL':<12} {totals['OOS_PnL']:>+10,.0f} {'':>7} {'':>10} {'':>7} {'':>8} "
        f"{int(totals['N_Trades']):>7} {totals['Turnover']:>10,.0f}"
    )
    if total_interest != 0:
        lines.append(
            f"{'Interest':<12} {-total_interest:>+10,.0f}"
        )
        lines.append(
            f"{'NET TOTAL':<12} {cs.get('total_pnl', 0):>+10,.0f}"
        )
    lines.append('')

    # ── Section 5: Per-window pair selection ─────────────────────────────────
    lines.append('PER-WINDOW PAIR SELECTION  (DSR-filtered MTFS params)')
    lines.append('-' * 72)
    for w, _ in windows_data:
        sel = w['selected_pairs']
        lines.append(f"  W{w['window_idx']} ({w['test_start']} → {w['test_end']}): {len(sel)} pairs")
        for s in sel:
            lines.append(f"      {s[0]}/{s[1]}  →  {s[2]}")
    lines.append('')

    # ── Print & save ──────────────────────────────────────────────────────────
    report_text = '\n'.join(lines)
    print(report_text)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_path = os.path.join(wf_dir, f'oos_report_{ts}.txt')
    with open(report_path, 'w') as f:
        f.write(report_text)
    print(f'\nReport saved: {report_path}')

    # Save CSVs
    if not curve.empty:
        curve_path = os.path.join(wf_dir, f'oos_equity_curve_{ts}.csv')
        curve.to_csv(curve_path, index=False)
        print(f'OOS curve:    {curve_path}')

    pair_path = os.path.join(wf_dir, f'oos_pair_summary_{ts}.csv')
    pair_df.to_csv(pair_path, index=False)
    print(f'Pair summary: {pair_path}')

    return pair_df, curve, cs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MTFS walk-forward OOS report generator')
    parser.add_argument('--wf-dir', default=None,
                        help='Path to walk_forward_mtfs output dir (default: historical_runs/walk_forward_mtfs/)')
    parser.add_argument('--run-prefix', default=None,
                        help='Filter windows by train_start prefix e.g. "2024-06-01"')
    args = parser.parse_args()

    generate_report(wf_dir=args.wf_dir, run_prefix=args.run_prefix)
