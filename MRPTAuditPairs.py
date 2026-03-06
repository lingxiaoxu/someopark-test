#!/usr/bin/env python3
"""
MRPTAuditPairs.py — Post-run audit for PortfolioMRPT Step 1 grid search outputs.

Each Excel file contains all 15 pairs in a single multi-pair run (one param_set).
Reads every Excel listed in a strategy_summary CSV (or auto-finds the latest),
iterates over every pair in every file, and runs 7 correctness checks.

Usage:
    python MRPTAuditPairs.py [strategy_summary_<ts>.csv]

    If no CSV is given, uses the most recent strategy_summary_*.csv in historical_runs/.

Checks per pair per run:
  1. Z-score entry/exit validity   — entries need |z| >= entry_z, exits need |z| <= exit_z
  2. Lookahead bias                 — trade date must have matching recorded_vars row
  3. Balance sheet consistency      — equity = assets - liabilities (portfolio-level, not per-pair)
  4. PnL consistency                — acc pair PnL ≈ sum of dod daily pair PnL
  5. Cash range                     — flag anomalously low/high/jumping cash values
  6. Trade symmetry                 — every open must have a matching close (or be end-of-run)
  7. Stop-loss cooling-off          — no new open between SL trigger date and re-evaluation date

Output: per-pair detail + summary table saved to historical_runs/audit_<ts>.txt
"""

import sys
import os
import glob
import warnings
import logging
from datetime import datetime

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALL_PAIRS = [
    ('MSCI', 'LII'), ('D', 'MCHP'), ('DG', 'MOS'), ('ESS', 'EXPD'), ('ACGL', 'UHS'),
    ('AAPL', 'META'), ('YUM', 'MCD'), ('GS', 'ALLY'), ('CL', 'USO'), ('ALGN', 'UAL'),
    ('ARES', 'CG'), ('AMG', 'BEN'), ('LYFT', 'UBER'), ('TW', 'CME'), ('CART', 'DASH'),
]
ALL_PAIR_KEYS = {f'{s1}/{s2}' for s1, s2 in ALL_PAIRS}

# Sector → Z-column mapping (for the "which Z-column belongs to this pair" check)
PAIR_Z_COL = {
    'MSCI/LII':   'Z_industrial',
    'D/MCHP':     'Z_tech',
    'DG/MOS':     'Z_food',
    'ESS/EXPD':   'Z_energy',
    'ACGL/UHS':   'Z_finance',
    'AAPL/META':  'Z_tech',
    'YUM/MCD':    'Z_food',
    'GS/ALLY':    'Z_finance',
    'CL/USO':     'Z_energy',
    'ALGN/UAL':   'Z_industrial',
    'ARES/CG':    'Z_finance',
    'AMG/BEN':    'Z_finance',
    'LYFT/UBER':  'Z_industrial',
    'TW/CME':     'Z_finance',
    'CART/DASH':  'Z_tech',
}

# Cash thresholds (portfolio-level, 15-pair combined portfolio)
CASH_MIN_THRESHOLD  = 200_000
CASH_MAX_THRESHOLD  = 15_000_000
CASH_JUMP_THRESHOLD = 1_000_000


# ─── Helpers ──────────────────────────────────────────────────────────────────

def find_latest_summary():
    pattern = os.path.join(BASE_DIR, 'historical_runs', 'strategy_summary_*.csv')
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not files:
        raise FileNotFoundError('No strategy_summary CSV found in historical_runs/')
    return files[0]


def load_sheet(path, sheet_name):
    try:
        df = pd.read_excel(path, sheet_name=sheet_name)
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        return df
    except Exception:
        return pd.DataFrame()


def count_trading_days_between(sorted_dates, date_a, date_b):
    idx_a = next((i for i, d in enumerate(sorted_dates) if d >= date_a), None)
    idx_b = next((i for i, d in enumerate(sorted_dates) if d >= date_b), None)
    if idx_a is None or idx_b is None:
        return None
    return idx_b - idx_a


# ─── Per-pair audit ───────────────────────────────────────────────────────────

def audit_pair(pair_key, s1, s2, sheets):
    """Run 7 checks for one pair within a multi-pair Excel file."""

    pt_all  = sheets['pair_trade_history']
    rv_all  = sheets['recorded_vars']
    acc_all = sheets['acc_pair_trade_pnl_history']
    dod_all = sheets['dod_pair_trade_pnl_history']
    sl_all  = sheets['stop_loss_history']
    eq      = sheets['equity_history']
    ast     = sheets['asset_history']
    lib     = sheets['liability_history']
    val     = sheets['value_history']
    cash    = sheets['asset_cash_history']

    # Subset to this pair
    pt  = pt_all[pt_all['Pair'] == pair_key].copy()  if (not pt_all.empty  and 'Pair' in pt_all.columns)  else pd.DataFrame()
    rv  = rv_all[rv_all['Pair'] == pair_key].copy()  if (not rv_all.empty  and 'Pair' in rv_all.columns)  else pd.DataFrame()
    acc = acc_all[acc_all['Pair'] == pair_key].copy() if (not acc_all.empty and 'Pair' in acc_all.columns) else pd.DataFrame()
    dod = dod_all[dod_all['Pair'] == pair_key].copy() if (not dod_all.empty and 'Pair' in dod_all.columns) else pd.DataFrame()
    sl  = sl_all[sl_all['Pair'] == pair_key].copy()  if (not sl_all.empty  and 'Pair' in sl_all.columns)  else pd.DataFrame()

    # Determine which Z-column this pair should use
    expected_z_col = PAIR_Z_COL.get(pair_key, None)

    # Get the active Z-column: use expected if it has non-null values for this pair
    z_col = None
    if not rv.empty and expected_z_col and expected_z_col in rv.columns:
        if rv[expected_z_col].notna().any():
            z_col = expected_z_col
    # Fallback: find any Z_ col with non-null values
    if z_col is None and not rv.empty:
        for c in [col for col in rv.columns if col.startswith('Z_')]:
            if rv[c].notna().any():
                z_col = c
                break

    z_col_mismatch = (z_col is not None and expected_z_col is not None and z_col != expected_z_col)

    # All trading dates (portfolio-level from equity_history)
    all_trading_dates = []
    if not eq.empty and 'Date' in eq.columns:
        all_trading_dates = sorted(eq['Date'].dropna().unique())

    # Build SL infrastructure for this pair
    sl_dates_real = set()
    sl_dates_reeval = {}
    if not sl.empty and 'Date' in sl.columns:
        real_sl = sl[sl.get('Triggered By', pd.Series(dtype=str)).str.strip() != 'Re-evaluation'] \
                  if 'Triggered By' in sl.columns else sl
        re_evals = sl[sl.get('Triggered By', pd.Series(dtype=str)).str.strip() == 'Re-evaluation'] \
                   if 'Triggered By' in sl.columns else pd.DataFrame()
        sl_dates_real = set(real_sl['Date'].dropna())
        if 'Triggered By' in sl.columns:
            re_eval_dates_sorted = sorted(re_evals['Date'].dropna())
            for _, row in real_sl.iterrows():
                sd = row['Date']
                next_re = next((d for d in re_eval_dates_sorted if d > sd), pd.NaT)
                sl_dates_reeval[sd] = next_re

    # ── CHECK 1: Entry/Exit Z-score Validity ───────────────────────────────
    c1 = {'bad_opens': [], 'bad_closes': [], 'open_count': 0, 'status': 'PASS'}

    if not pt.empty and not rv.empty and z_col:
        rv_idx = rv.set_index('Date')
        opens_s1  = pt[(pt['Order Type'] == 'open')  & (pt['Symbol'] == s1)]
        closes_s1 = pt[(pt['Order Type'] == 'close') & (pt['Symbol'] == s1)]
        c1['open_count'] = len(opens_s1)

        for _, row in opens_s1.iterrows():
            d, direction = row['Date'], row['Direction']
            if d not in rv_idx.index:
                continue
            rv_row = rv_idx.loc[d]
            if isinstance(rv_row, pd.DataFrame):
                rv_row = rv_row.iloc[0]
            z = float(rv_row[z_col]) if pd.notna(rv_row[z_col]) else np.nan
            entry_z = float(rv_row['Entry_Z']) if 'Entry_Z' in rv_row and pd.notna(rv_row['Entry_Z']) else np.nan
            if np.isnan(z) or np.isnan(entry_z):
                continue
            if direction == 'long' and z >= -entry_z:
                c1['bad_opens'].append(f"{d.date()} LONG z={z:.3f} need<{-entry_z:.3f}")
                c1['status'] = 'FAIL'
            elif direction == 'short' and z <= entry_z:
                c1['bad_opens'].append(f"{d.date()} SHORT z={z:.3f} need>{entry_z:.3f}")
                c1['status'] = 'FAIL'

        for _, row in closes_s1.iterrows():
            d, direction = row['Date'], row['Direction']
            if d in sl_dates_real:   # stop-loss closes are exempt
                continue
            if d not in rv_idx.index:
                continue
            rv_row = rv_idx.loc[d]
            if isinstance(rv_row, pd.DataFrame):
                rv_row = rv_row.iloc[0]
            z = float(rv_row[z_col]) if pd.notna(rv_row[z_col]) else np.nan
            exit_z = float(rv_row['Exit_Z']) if 'Exit_Z' in rv_row and pd.notna(rv_row['Exit_Z']) else np.nan
            if np.isnan(z) or np.isnan(exit_z):
                continue
            # direction on close record is the ACTION: closing long sells (direction='short'), closing short buys (direction='long')
            if direction == 'short' and z <= -exit_z:
                c1['bad_closes'].append(f"{d.date()} CLOSE_LONG z={z:.3f} need>{-exit_z:.3f}")
                c1['status'] = 'FAIL'
            elif direction == 'long' and z >= exit_z:
                c1['bad_closes'].append(f"{d.date()} CLOSE_SHORT z={z:.3f} need<{exit_z:.3f}")
                c1['status'] = 'FAIL'

    # ── CHECK 2: Lookahead Bias ─────────────────────────────────────────────
    TOLERANCE = 0.02
    c2 = {'suspicious': [], 'status': 'PASS'}

    if not pt.empty and not rv.empty and z_col:
        rv_idx = rv.set_index('Date')
        opens_s1 = pt[(pt['Order Type'] == 'open') & (pt['Symbol'] == s1)]

        for _, row in opens_s1.iterrows():
            d, direction = row['Date'], row['Direction']
            if d not in rv_idx.index:
                c2['suspicious'].append(f"{d.date()} NO_RV_ENTRY (date missing from recorded_vars)")
                c2['status'] = 'WARN'
                continue
            rv_row = rv_idx.loc[d]
            if isinstance(rv_row, pd.DataFrame):
                rv_row = rv_row.iloc[0]
            z = rv_row[z_col]
            entry_z = rv_row['Entry_Z'] if 'Entry_Z' in rv_row else np.nan
            if pd.isna(z) or pd.isna(entry_z):
                c2['suspicious'].append(f"{d.date()} NULL_Z_OR_ENTRY_Z")
                c2['status'] = 'WARN'
                continue
            z, entry_z = float(z), float(entry_z)
            if direction == 'long' and z >= -entry_z + TOLERANCE:
                c2['suspicious'].append(
                    f"{d.date()} LONG z={z:.3f} entry_z={entry_z:.3f} "
                    f"(z should be <{-entry_z:.3f})"
                )
                c2['status'] = 'WARN'
            elif direction == 'short' and z <= entry_z - TOLERANCE:
                c2['suspicious'].append(
                    f"{d.date()} SHORT z={z:.3f} entry_z={entry_z:.3f} "
                    f"(z should be >{entry_z:.3f})"
                )
                c2['status'] = 'WARN'

    # ── CHECK 3: Balance Sheet (portfolio-level — shared across all pairs) ──
    # Marked as SKIP here; audited once per file in audit_portfolio_level()
    c3 = {'status': 'SKIP', 'note': 'Portfolio-level check — see run-level audit below'}

    # ── CHECK 4: PnL Consistency ────────────────────────────────────────────
    c4 = {'acc_pnl': None, 'sum_dod': None, 'diff': None, 'status': 'PASS'}

    if not acc.empty and 'PnL Dollar' in acc.columns:
        c4['acc_pnl'] = acc['PnL Dollar'].iloc[-1]
        if not dod.empty and 'PnL Dollar' in dod.columns:
            c4['sum_dod'] = dod['PnL Dollar'].sum()
            c4['diff'] = abs(c4['acc_pnl'] - c4['sum_dod'])
            if c4['diff'] >= 5.0:
                c4['status'] = 'FAIL'
    else:
        c4['status'] = 'NO_DATA'

    # ── CHECK 5: Cash Range (portfolio-level — shared) ──────────────────────
    # Marked as SKIP here; audited once per file in audit_portfolio_level()
    c5 = {'status': 'SKIP', 'note': 'Portfolio-level check — see run-level audit below'}

    # ── CHECK 6: Trade Symmetry ─────────────────────────────────────────────
    c6 = {'unclosed': [], 'bad_rows': [], 'status': 'PASS'}

    if not pt.empty:
        opens  = pt[pt['Order Type'] == 'open']
        closes = pt[pt['Order Type'] == 'close']

        for d, grp in opens.groupby('Date'):
            if len(grp) != 2:
                c6['bad_rows'].append(f"Open on {d.date()} has {len(grp)} rows (expect 2)")
                c6['status'] = 'WARN'

        for d, grp in closes.groupby('Date'):
            if len(grp) != 2:
                c6['bad_rows'].append(f"Close on {d.date()} has {len(grp)} rows (expect 2)")
                c6['status'] = 'WARN'

        events = ([(d, 'open') for d in opens['Date'].unique()] +
                  [(d, 'close') for d in closes['Date'].unique()])
        events.sort(key=lambda x: x[0])

        stack = []
        for d, etype in events:
            if etype == 'open':
                stack.append(d)
            elif etype == 'close':
                if stack:
                    stack.pop()
                else:
                    c6['unclosed'].append(f"Close at {d.date()} without matching open")
                    c6['status'] = 'WARN'

        for d in stack:
            note = " (end-of-backtest open)" if len(stack) == 1 else ""
            c6['unclosed'].append(f"Position opened {d.date()} has no close{note}")
            if len(stack) > 1:
                c6['status'] = 'WARN'

    # ── CHECK 7: Stop-Loss Cooling-off Compliance ───────────────────────────
    c7 = {'n_stops': 0, 'violations': [], 'status': 'PASS'}

    if not sl.empty and not pt.empty and 'Triggered By' in sl.columns:
        real_sl_df = sl[sl['Triggered By'].str.strip() != 'Re-evaluation']
        c7['n_stops'] = len(real_sl_df)

        open_dates_sorted = sorted(pt[pt['Order Type'] == 'open']['Date'].unique())

        for sl_date, re_eval_date in sl_dates_reeval.items():
            if pd.isna(re_eval_date):
                continue
            premature = [d for d in open_dates_sorted if sl_date < d < re_eval_date]
            if premature:
                days_gap = count_trading_days_between(all_trading_dates, sl_date, premature[0])
                re_gap   = count_trading_days_between(all_trading_dates, sl_date, re_eval_date)
                c7['violations'].append(
                    f"SL {sl_date.date()} (re-eval: {re_eval_date.date()}, "
                    f"{re_gap} td) → premature open {premature[0].date()} "
                    f"({days_gap} td after SL)"
                )
                c7['status'] = 'WARN'

    return {
        'pair_key': pair_key,
        'z_col': z_col,
        'expected_z_col': expected_z_col,
        'z_col_mismatch': z_col_mismatch,
        'c1': c1, 'c2': c2, 'c3': c3, 'c4': c4,
        'c5': c5, 'c6': c6, 'c7': c7,
    }


# ─── Portfolio-level checks (once per Excel file) ────────────────────────────

def audit_portfolio_level(sheets, pt_all):
    """Check 3 (balance sheet) and Check 5 (cash) — portfolio-wide."""
    eq   = sheets['equity_history']
    ast  = sheets['asset_history']
    lib  = sheets['liability_history']
    val  = sheets['value_history']
    cash = sheets['asset_cash_history']

    # CHECK 3: Balance sheet
    c3 = {'errors': [], 'status': 'PASS'}
    if not eq.empty and not ast.empty and not lib.empty and not val.empty:
        eq2  = eq.rename(columns={'Value': 'equity'})
        ast2 = ast.rename(columns={'Value': 'assets'})
        lib2 = lib.rename(columns={'Value': 'liabilities'})
        val2 = val.rename(columns={'Value': 'value'})
        bs = eq2.merge(ast2, on='Date').merge(lib2, on='Date').merge(val2, on='Date')
        bs = bs.dropna(subset=['equity', 'assets', 'liabilities', 'value'])

        if not pt_all.empty and 'Date' in pt_all.columns:
            first_trade = pt_all['Date'].min()
            bs = bs[bs['Date'] >= first_trade].copy()

        if bs.empty:
            c3['status'] = 'NO_DATA'
        else:
            bs['calc_eq']  = bs['assets'] - bs['liabilities']
            bs['eq_diff']  = (bs['equity'] - bs['calc_eq']).abs()
            bs['val_diff'] = (bs['value'] - bs['equity']).abs()

            for _, r in bs[bs['eq_diff'] >= 1.0].iterrows():
                c3['errors'].append(
                    f"{r['Date'].date()} equity≠assets-liab: "
                    f"equity={r['equity']:.2f}, assets={r['assets']:.2f}, "
                    f"liab={r['liabilities']:.2f}, diff={r['eq_diff']:.2f}"
                )
                c3['status'] = 'FAIL'
            for _, r in bs[bs['val_diff'] >= 1.0].iterrows():
                c3['errors'].append(
                    f"{r['Date'].date()} value≠equity: "
                    f"value={r['value']:.2f}, equity={r['equity']:.2f}, diff={r['val_diff']:.2f}"
                )
                c3['status'] = 'FAIL'

            if not eq.empty and not pt_all.empty:
                eq_after = eq[eq['Date'] >= pt_all['Date'].min()]
                if eq_after['Value'].isna().any():
                    c3['errors'].append("NaN values in equity_history after first trade")
                    c3['status'] = 'FAIL'

    # CHECK 5: Cash range
    c5 = {'min_cash': None, 'max_cash': None, 'max_jump': None,
          'max_jump_date': None, 'status': 'PASS', 'warnings': []}
    if not cash.empty and 'Value' in cash.columns:
        vals = cash['Value'].dropna()
        if len(vals):
            c5['min_cash'] = vals.min()
            c5['max_cash'] = vals.max()
            jumps = vals.diff().abs()
            c5['max_jump'] = jumps.max()
            if pd.notna(c5['max_jump']) and c5['max_jump'] > 0:
                idx = jumps.idxmax()
                c5['max_jump_date'] = str(cash.loc[idx, 'Date'].date()) if idx < len(cash) else '?'
            if c5['min_cash'] < CASH_MIN_THRESHOLD:
                c5['warnings'].append(f"Cash below {CASH_MIN_THRESHOLD:,}: {c5['min_cash']:.0f}")
                c5['status'] = 'WARN'
            if c5['max_cash'] > CASH_MAX_THRESHOLD:
                c5['warnings'].append(f"Cash above {CASH_MAX_THRESHOLD:,}: {c5['max_cash']:.0f}")
                c5['status'] = 'WARN'
            if c5['max_jump'] > CASH_JUMP_THRESHOLD:
                c5['warnings'].append(
                    f"Daily jump>{CASH_JUMP_THRESHOLD:,} on {c5['max_jump_date']}: {c5['max_jump']:.0f}")
                c5['status'] = 'WARN'

    return c3, c5


# ─── Full file audit ──────────────────────────────────────────────────────────

def audit_file(xlsx_path, param_set):
    fname = os.path.basename(xlsx_path)
    log.info(f"  Loading sheets from {fname} ...")

    sheets = {
        'pair_trade_history':       load_sheet(xlsx_path, 'pair_trade_history'),
        'recorded_vars':            load_sheet(xlsx_path, 'recorded_vars'),
        'acc_pair_trade_pnl_history': load_sheet(xlsx_path, 'acc_pair_trade_pnl_history'),
        'dod_pair_trade_pnl_history': load_sheet(xlsx_path, 'dod_pair_trade_pnl_history'),
        'stop_loss_history':        load_sheet(xlsx_path, 'stop_loss_history'),
        'equity_history':           load_sheet(xlsx_path, 'equity_history'),
        'asset_history':            load_sheet(xlsx_path, 'asset_history'),
        'liability_history':        load_sheet(xlsx_path, 'liability_history'),
        'value_history':            load_sheet(xlsx_path, 'value_history'),
        'asset_cash_history':       load_sheet(xlsx_path, 'asset_cash_history'),
    }

    pt_all = sheets['pair_trade_history']

    # Portfolio-level checks (once)
    c3_portfolio, c5_portfolio = audit_portfolio_level(sheets, pt_all)

    # Determine which pairs are actually present in this file
    pairs_present = set()
    if not pt_all.empty and 'Pair' in pt_all.columns:
        pairs_present = set(pt_all['Pair'].unique())
    elif not sheets['acc_pair_trade_pnl_history'].empty and 'Pair' in sheets['acc_pair_trade_pnl_history'].columns:
        pairs_present = set(sheets['acc_pair_trade_pnl_history']['Pair'].unique())

    results = []
    for s1, s2 in ALL_PAIRS:
        pair_key = f'{s1}/{s2}'
        try:
            pr = audit_pair(pair_key, s1, s2, sheets)
            # Inject portfolio-level checks
            pr['c3'] = c3_portfolio
            pr['c5'] = c5_portfolio
            pr['param_set'] = param_set
            pr['file'] = fname
            pr['has_trades'] = pair_key in pairs_present
            results.append(pr)
        except Exception as e:
            import traceback
            log.warning(f"    ERROR auditing {pair_key}: {e}")
            results.append({
                'pair_key': pair_key, 'param_set': param_set, 'file': fname,
                'has_trades': pair_key in pairs_present,
                'z_col': None, 'expected_z_col': None, 'z_col_mismatch': False,
                'c1': {'status': 'ERROR', 'bad_opens': [str(e)], 'bad_closes': [], 'open_count': 0},
                'c2': {'status': 'ERROR', 'suspicious': []},
                'c3': {'status': 'ERROR', 'errors': [traceback.format_exc()[:300]]},
                'c4': {'status': 'ERROR', 'acc_pnl': None, 'sum_dod': None, 'diff': None},
                'c5': {'status': 'ERROR', 'min_cash': None, 'max_cash': None,
                       'max_jump': None, 'max_jump_date': None, 'warnings': []},
                'c6': {'status': 'ERROR', 'unclosed': [], 'bad_rows': []},
                'c7': {'status': 'ERROR', 'n_stops': 0, 'violations': []},
            })

    return results


# ─── Formatting ──────────────────────────────────────────────────────────────

def verdict_of(r):
    statuses = [r['c1']['status'], r['c2']['status'], r['c3']['status'],
                r['c4']['status'], r['c5']['status'], r['c6']['status'],
                r['c7']['status']]
    if any(s == 'ERROR' for s in statuses):
        return 'ERROR'
    if any(s == 'FAIL' for s in statuses):
        v = 'FAIL'
    elif any(s in ('WARN', 'NO_DATA') for s in statuses):
        v = 'WARN'
    else:
        v = 'CLEAN'
    if r.get('z_col_mismatch'):
        v += '+Z_MISMATCH'
    return v


def format_pair_result(r):
    pair     = r['pair_key']
    param    = r['param_set']
    z_col    = r.get('z_col', 'N/A')
    exp_z    = r.get('expected_z_col', 'N/A')
    mismatch = r.get('z_col_mismatch', False)
    no_trades = not r.get('has_trades', True)

    lines = []
    lines.append(f"\n{'─'*65}")
    lines.append(f"  {pair}  [{param}]{'  (NO TRADES)' if no_trades else ''}")
    z_note = f"  ← MISMATCH (expected {exp_z})" if mismatch else ""
    lines.append(f"  Z-col used: {z_col}{z_note}")

    c1 = r['c1']
    bad1 = c1.get('bad_opens', []) + c1.get('bad_closes', [])
    lines.append(f"  1. Z-score:      {c1['open_count']} opens, {len(bad1)} bad → {c1['status']}")
    for b in bad1[:3]:
        lines.append(f"       {b}")
    if len(bad1) > 3:
        lines.append(f"       ... +{len(bad1)-3} more")

    c2 = r['c2']
    susp = c2.get('suspicious', [])
    lines.append(f"  2. Lookahead:    {c2['status']}{f' ({len(susp)} suspicious)' if susp else ''}")
    for s in susp[:3]:
        lines.append(f"       {s}")

    c3 = r['c3']
    errs3 = c3.get('errors', [])
    note3 = c3.get('note', '')
    lines.append(f"  3. Balance sht:  {c3['status']}{f' ({len(errs3)} errors)' if errs3 else ''}"
                 f"{f'  [{note3}]' if c3['status'] == 'SKIP' else ''}")
    for e in errs3[:2]:
        lines.append(f"       {e}")
    if len(errs3) > 2:
        lines.append(f"       ... +{len(errs3)-2} more")

    c4 = r['c4']
    acc_s  = f"{c4['acc_pnl']:+,.2f}" if c4.get('acc_pnl') is not None else "N/A"
    dod_s  = f"{c4['sum_dod']:+,.2f}" if c4.get('sum_dod') is not None else "N/A"
    diff_s = f"{c4['diff']:.2f}" if c4.get('diff') is not None else "N/A"
    lines.append(f"  4. PnL consist:  {c4['status']}  acc={acc_s}  sum_dod={dod_s}  Δ={diff_s}")

    c5 = r['c5']
    note5 = c5.get('note', '')
    mn_s = f"{c5['min_cash']:,.0f}" if c5.get('min_cash') is not None else "N/A"
    mx_s = f"{c5['max_cash']:,.0f}" if c5.get('max_cash') is not None else "N/A"
    mj_s = f"{c5['max_jump']:,.0f}" if c5.get('max_jump') is not None else "N/A"
    lines.append(f"  5. Cash range:   {c5['status']}  min={mn_s}  max={mx_s}  jump={mj_s}"
                 f"{f'  [{note5}]' if c5['status'] == 'SKIP' else ''}")
    for w in c5.get('warnings', []):
        lines.append(f"       {w}")

    c6 = r['c6']
    issues6 = c6.get('unclosed', []) + c6.get('bad_rows', [])
    lines.append(f"  6. Trade sym:    {c6['status']}{f' ({len(issues6)} issues)' if issues6 else ''}")
    for u in issues6[:3]:
        lines.append(f"       {u}")

    c7 = r['c7']
    lines.append(f"  7. SL cooloff:   {c7['n_stops']} stops, {len(c7.get('violations',[]))} violations → {c7['status']}")
    for v in c7.get('violations', [])[:3]:
        lines.append(f"       {v}")

    verdict = verdict_of(r)
    lines.append(f"  VERDICT: {verdict}")
    return '\n'.join(lines), verdict


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(csv_path=None):
    if csv_path is None:
        csv_path = find_latest_summary()

    log.info(f"Reading summary: {csv_path}")
    summary = pd.read_csv(csv_path)
    log.info(f"Total runs: {len(summary)}")

    output_lines = []
    all_pair_results = []   # list of (param_set, pair_key, verdict) for summary table

    for idx, row in summary.iterrows():
        param_set  = row.get('param_set', f'run_{idx}')
        xlsx_path  = row.get('output_file', '')

        if not xlsx_path or not os.path.exists(xlsx_path):
            log.warning(f"[{idx+1}/{len(summary)}] output_file missing or not found: {xlsx_path}")
            continue

        log.info(f"[{idx+1}/{len(summary)}] Auditing {param_set} ...")

        header = f"\n{'='*70}\nRUN: {param_set}\nFILE: {os.path.basename(xlsx_path)}\n{'='*70}"
        output_lines.append(header)

        try:
            pair_results = audit_file(xlsx_path, param_set)
        except Exception as e:
            import traceback
            log.error(f"  Fatal error auditing {xlsx_path}: {e}")
            output_lines.append(f"  FATAL ERROR: {traceback.format_exc()[:500]}")
            continue

        for pr in pair_results:
            text, verdict = format_pair_result(pr)
            output_lines.append(text)
            all_pair_results.append((param_set, pr['pair_key'], verdict))

    # ── Summary table ──────────────────────────────────────────────────────
    output_lines.append(f"\n\n{'='*80}")
    output_lines.append("SUMMARY TABLE  (param_set × pair → verdict)")
    output_lines.append('='*80)

    # Group by param_set
    from collections import defaultdict
    by_run = defaultdict(dict)
    for param, pair, verdict in all_pair_results:
        by_run[param][pair] = verdict

    pair_order = [f'{s1}/{s2}' for s1, s2 in ALL_PAIRS]

    # Header row
    hdr = f"{'param_set':<35s}"
    for pk in pair_order:
        hdr += f" {pk:<12s}"
    output_lines.append(hdr)
    output_lines.append('-'*80)

    for param in summary['param_set'].tolist():
        if param not in by_run:
            continue
        row_str = f"{param:<35s}"
        for pk in pair_order:
            v = by_run[param].get(pk, 'SKIP')
            icon = {'CLEAN': 'OK', 'FAIL': 'FAIL', 'WARN': 'warn',
                    'ERROR': 'ERR!', 'SKIP': '----'}.get(v.split('+')[0], v[:4])
            row_str += f" {icon:<12s}"
        output_lines.append(row_str)

    # Aggregate counts
    counts = {'CLEAN': 0, 'WARN': 0, 'FAIL': 0, 'ERROR': 0}
    for _, _, v in all_pair_results:
        key = v.split('+')[0]
        if key in counts:
            counts[key] += 1

    total = len(all_pair_results)
    output_lines.append(f"\nTotal pair×run results: {total}")
    output_lines.append(
        f"  CLEAN={counts['CLEAN']}  WARN={counts['WARN']}  "
        f"FAIL={counts['FAIL']}  ERROR={counts['ERROR']}"
    )

    # ── Failures detail ────────────────────────────────────────────────────
    fails = [(p, pk, v) for p, pk, v in all_pair_results if 'FAIL' in v]
    errs  = [(p, pk, v) for p, pk, v in all_pair_results if 'ERROR' in v]
    if fails or errs:
        output_lines.append(f"\nFAILURES ({len(fails)}) + ERRORS ({len(errs)}):")
        for p, pk, v in fails + errs:
            output_lines.append(f"  {p:<35s} {pk:<12s} {v}")

    full_output = '\n'.join(output_lines)
    print(full_output)

    # Save to file
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    audit_dir = os.path.join(BASE_DIR, 'historical_runs', 'audit')
    os.makedirs(audit_dir, exist_ok=True)
    out_path = os.path.join(audit_dir, f'audit_{ts}.txt')
    with open(out_path, 'w') as f:
        f.write(full_output)
    log.info(f"\nAudit saved to: {out_path}")


if __name__ == '__main__':
    csv_path = sys.argv[1] if len(sys.argv) > 1 else None
    main(csv_path)
