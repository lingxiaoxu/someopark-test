"""
DailySignal.py — 每日信号生成器（MRPT 和 MTFS 通用）
====================================================
每个交易日收盘后运行一次，输出今天对每对的操作指令。

用法：
    python DailySignal.py --strategy mrpt                  # MRPT 策略（均值回归）
    python DailySignal.py --strategy mtfs                  # MTFS 策略（动量趋势）
    python DailySignal.py --strategy mrpt --date 2026-03-04
    python DailySignal.py --strategy mtfs --dry-run

工作流程：
    1. 读取 inventory_<strategy>.json（你的实际持仓记录）
    2. 用历史数据跑模拟（warmup 建状态）
    3. 把 inventory 里的仓位注入模拟器，让模拟器与现实对齐
    4. 今天的信号（OPEN/CLOSE/HOLD/FLAT）+ 具体股数写入 signals/<strategy>_YYYYMMDD.json
    5. 如果不是 --dry-run，自动更新 inventory_<strategy>.json

inventory_<strategy>.json 格式：
    {
      "as_of": "2026-03-03",
      "capital": 500000,
      "pairs": {
        "CART/DASH": {
          "direction": "short",      // "long" / "short" / null（空仓）
          "s1_shares": -847,
          "s2_shares": 1203,
          "open_date": "2026-02-18",
          "open_s1_price": 51.20,
          "open_s2_price": 38.90,
          "days_held": 11
        },
        "GS/ALLY": { "direction": null }
      }
    }

signals/<strategy>_YYYYMMDD.json 输出格式：
    {
      "strategy": "mrpt",
      "signal_date": "2026-03-04",
      "generated_at": "2026-03-04T16:35:00",
      "signals": [
        {
          "pair": "CART/DASH",
          "action": "HOLD",
          "direction": "short",
          "signal_value": 1.85,       // z-score (MRPT) or momentum spread (MTFS)
          "entry_threshold": 2.20,
          "exit_threshold": 0.0,
          "s1_shares": -847,
          "s2_shares": 1203,
          "days_held": 12,
          "note": "..."
        }
      ]
    }
"""

import argparse
import json
import logging
import os
import sys
from copy import deepcopy
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PortfolioClasses import Portfolio, PortfolioMakeOrder, PortfolioConstruct, \
    PortfolioStopLossFunction, Execution, PortfolioAnalysis

log = logging.getLogger('DailySignal')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SIGNALS_DIR = os.path.join(BASE_DIR, 'signals')


# ── Date helpers ───────────────────────────────────────────────────────────────

def prev_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ── Inventory helpers ──────────────────────────────────────────────────────────

def inventory_path(strategy: str) -> str:
    return os.path.join(BASE_DIR, f'inventory_{strategy}.json')


def load_inventory(strategy: str) -> dict:
    path = inventory_path(strategy)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {'as_of': None, 'capital': 500000, 'pairs': {}}


def save_inventory(inv: dict, strategy: str):
    path = inventory_path(strategy)
    with open(path, 'w') as f:
        json.dump(inv, f, indent=2, default=str)
    log.info(f"inventory_{strategy}.json updated → {path}")


# ── Pair config ────────────────────────────────────────────────────────────────

def get_pair_configs_mrpt() -> list[tuple[str, str, str]]:
    """
    MRPT: 15 pairs + best param_sets.
    Update this list after running MRPTUpdateConfigs.py (mirrors forward30d config).
    Format: (s1, s2, param_set_name)
    """
    return [
        ('MSCI',  'LII',   'fast_reentry'),
        ('D',     'MCHP',  'conservative_no_leverage'),
        ('DG',    'MOS',   'slow_signal'),
        ('ESS',   'EXPD',  'fast_signal'),
        ('ACGL',  'UHS',   'deep_entry_quick_exit'),
        ('AAPL',  'META',  'long_z_short_v'),
        ('YUM',   'MCD',   'slow_signal'),
        ('GS',    'ALLY',  'long_z_short_v'),
        ('CL',    'USO',   'conservative_no_leverage'),
        ('ALGN',  'UAL',   'fast_signal_tight_stop'),
        ('ARES',  'CG',    'low_vol_specialist'),
        ('AMG',   'BEN',   'high_turnover'),
        ('LYFT',  'UBER',  'conservative_no_leverage'),
        ('TW',    'CME',   'conservative_no_leverage'),
        ('CART',  'DASH',  'static_threshold'),
    ]


def get_pair_configs_mtfs() -> list[tuple[str, str, str]]:
    """
    MTFS: 13 pairs + best param_sets.
    Update this list after running MTFSUpdateConfigs.py (mirrors mtfs_runs_forward30d config).
    Format: (s1, s2, param_set_name)
    """
    return [
        ('D',     'MCHP',  'trend_leverage'),
        ('DG',    'MOS',   'beta_neutral'),
        ('ESS',   'EXPD',  'no_reversal_protection'),
        ('ACGL',  'UHS',   'short_term_tilt'),
        ('AAPL',  'META',  'sensitive_reversal'),
        ('GS',    'ALLY',  'aggressive'),
        ('CL',    'USO',   'slow_rebalance'),
        ('ALGN',  'UAL',   'no_skip_month'),
        ('ARES',  'CG',    'aggressive'),
        ('AMG',   'BEN',   'aggressive'),
        ('LYFT',  'UBER',  'aggressive'),
        ('TW',    'CME',   'weekly_aligned_windows'),
        ('CART',  'DASH',  'no_trend_filter'),
    ]


# ── Inventory injection ────────────────────────────────────────────────────────

def _inject_inventory_into_context(context, inventory: dict, signal_date: pd.Timestamp):
    """
    Before running signal_date, inject real positions from inventory into the
    simulation's internal state so the in_long/in_short flags match reality.
    """
    pairs_inv = inventory.get('pairs', {})

    for pair in context.strategy_pairs:
        s1, s2 = pair[0], pair[1]
        key = f"{s1}/{s2}"
        inv_pair = pairs_inv.get(key, {})
        direction = inv_pair.get('direction')

        if direction == 'long':
            pair[2]['in_long'] = True
            pair[2]['in_short'] = False
            s1_shares = inv_pair.get('s1_shares', 0)
            s2_shares = inv_pair.get('s2_shares', 0)
            if s1_shares:
                context.portfolio.positions[s1] = s1_shares
            if s2_shares:
                context.portfolio.positions[s2] = s2_shares

        elif direction == 'short':
            pair[2]['in_long'] = False
            pair[2]['in_short'] = True
            s1_shares = inv_pair.get('s1_shares', 0)
            s2_shares = inv_pair.get('s2_shares', 0)
            if s1_shares:
                context.portfolio.positions[s1] = s1_shares
            if s2_shares:
                context.portfolio.positions[s2] = s2_shares

        else:
            pair[2]['in_long'] = False
            pair[2]['in_short'] = False
            context.portfolio.positions.pop(s1, None)
            context.portfolio.positions.pop(s2, None)


# ── Signal extraction ──────────────────────────────────────────────────────────

# Sector mapping (same in both strategies' record_vars)
_SECTOR_MAP = {
    frozenset({'AAPL', 'META', 'D', 'MCHP', 'CART', 'DASH'}):                    'tech',
    frozenset({'GS', 'ALLY', 'ACGL', 'UHS', 'ARES', 'CG', 'AMG', 'BEN', 'TW', 'CME'}): 'finance',
    frozenset({'ALGN', 'UAL', 'MSCI', 'LII', 'LYFT', 'UBER'}):                   'industrial',
    frozenset({'CL', 'USO', 'ESS', 'EXPD'}):                                      'energy',
    frozenset({'DG', 'MOS', 'YUM', 'MCD'}):                                       'food',
}


def _get_sector(s1, s2) -> str:
    pair_set = frozenset({s1, s2})
    for sym_set, sec in _SECTOR_MAP.items():
        if pair_set & sym_set:
            return sec
    return 'finance'


def _find_today_rv(context, pair_key: str, signal_date: pd.Timestamp) -> dict | None:
    rv = context.recorded_vars.get(pair_key, {})
    for k, v in rv.items():
        if pd.Timestamp(k).date() == signal_date.date():
            return v
    return None


def _build_signal(pair_key, s1, s2, today_rv, inventory, context, prices_today, strategy) -> dict:
    """Build action dict from today's recorded_vars. Strategy-agnostic where possible."""
    in_long  = bool(today_rv.get('in_long',  False))
    in_short = bool(today_rv.get('in_short', False))

    s1_price = prices_today.get(s1, float('nan'))
    s2_price = prices_today.get(s2, float('nan'))

    inv_pair      = inventory.get('pairs', {}).get(pair_key, {})
    inv_direction = inv_pair.get('direction')
    days_held     = inv_pair.get('days_held', 0)

    s1_shares = context.portfolio.positions.get(s1, 0)
    s2_shares = context.portfolio.positions.get(s2, 0)

    stop_triggered = today_rv.get('stop_loss_triggered', False)

    # ── Strategy-specific signal value and thresholds ─────────────────────
    if strategy == 'mrpt':
        sector = _get_sector(s1, s2)
        signal_value   = today_rv.get(f'Z_{sector}', today_rv.get('z_score', float('nan')))
        entry_threshold = today_rv.get('Entry_Z', float('nan'))
        exit_threshold  = today_rv.get('Exit_Z', 0.0)
        signal_label   = 'z_score'

        # Earnings blackout (MRPT only)
        blackout        = today_rv.get('earnings_blackout', False)
        blackout_reason = today_rv.get('earnings_blackout_reason', '')
        if blackout and not in_long and not in_short:
            return {
                'pair': pair_key, 'action': 'BLACKOUT',
                signal_label: round(signal_value, 3) if not np.isnan(signal_value) else None,
                'entry_threshold': round(entry_threshold, 3) if not np.isnan(entry_threshold) else None,
                's1': s1, 's2': s2,
                's1_price': round(s1_price, 4) if not np.isnan(s1_price) else None,
                's2_price': round(s2_price, 4) if not np.isnan(s2_price) else None,
                'note': f'Earnings blackout: {blackout_reason}',
            }
    else:
        sector = _get_sector(s1, s2)
        signal_value    = today_rv.get(f'Momentum_Spread_{sector}',
                                       today_rv.get('Momentum_Spread', float('nan')))
        entry_threshold = today_rv.get('Entry_Threshold', float('nan'))
        exit_threshold  = today_rv.get('Exit_Threshold', 0.0)
        signal_label   = 'momentum_spread'

    sig_val_r  = round(float(signal_value),    3) if not np.isnan(float(signal_value))    else None
    entry_r    = round(float(entry_threshold), 3) if not np.isnan(float(entry_threshold)) else None
    exit_r     = round(float(exit_threshold),  3) if not np.isnan(float(exit_threshold))  else None
    s1_price_r = round(s1_price, 4)             if not np.isnan(s1_price)                 else None
    s2_price_r = round(s2_price, 4)             if not np.isnan(s2_price)                 else None

    base = {
        'pair': pair_key,
        signal_label: sig_val_r,
        's1': s1, 's2': s2,
        's1_price': s1_price_r, 's2_price': s2_price_r,
    }

    # CLOSE (stop loss)
    if stop_triggered and (in_long or in_short) and inv_direction:
        return {**base, 'action': 'CLOSE_STOP', 'direction': inv_direction,
                's1_shares': s1_shares, 's2_shares': s2_shares, 'days_held': days_held,
                'note': f'Stop loss triggered — close {inv_direction} position'}

    # CLOSE (normal mean-reversion / momentum decay)
    if not in_long and not in_short and inv_direction:
        return {**base, 'action': 'CLOSE', 'direction': inv_direction,
                'exit_threshold': exit_r,
                's1_shares': s1_shares, 's2_shares': s2_shares, 'days_held': days_held,
                'note': f'signal={sig_val_r} passed exit threshold {exit_r} — close {inv_direction}'}

    # OPEN LONG
    if in_long and not inv_direction:
        return {**base, 'action': 'OPEN_LONG', 'direction': 'long',
                'entry_threshold': entry_r,
                's1_shares': s1_shares, 's2_shares': s2_shares,
                'note': f'signal={sig_val_r} triggered long entry (threshold ±{entry_r})'}

    # OPEN SHORT
    if in_short and not inv_direction:
        return {**base, 'action': 'OPEN_SHORT', 'direction': 'short',
                'entry_threshold': entry_r,
                's1_shares': s1_shares, 's2_shares': s2_shares,
                'note': f'signal={sig_val_r} triggered short entry (threshold ±{entry_r})'}

    # HOLD
    if (in_long or in_short) and inv_direction:
        dir_label = 'long' if in_long else 'short'
        return {**base, 'action': 'HOLD', 'direction': dir_label,
                'entry_threshold': entry_r, 'exit_threshold': exit_r,
                's1_shares': s1_shares, 's2_shares': s2_shares, 'days_held': days_held,
                'note': f'Holding {dir_label}, signal={sig_val_r}, exit at {exit_r}'}

    # FLAT
    return {**base, 'action': 'FLAT',
            'entry_threshold': entry_r,
            'note': f'No position, signal={sig_val_r} within ±{entry_r} — wait'}


def extract_signals(context, pair_configs, signal_ts, inventory, prices_today, strategy) -> list:
    signals = []
    for s1, s2, _ in pair_configs:
        pair_key = f"{s1}/{s2}"
        today_rv = _find_today_rv(context, pair_key, signal_ts)
        if today_rv is None:
            signals.append({
                'pair': pair_key, 'action': 'NO_DATA',
                'note': 'No recorded_vars for this date — data may be missing',
            })
            continue
        sig = _build_signal(pair_key, s1, s2, today_rv, inventory, context, prices_today, strategy)
        signals.append(sig)
    return signals


# ── Inventory update ───────────────────────────────────────────────────────────

def update_inventory_from_signals(inventory: dict, signals: list, signal_date: str) -> dict:
    inv = deepcopy(inventory)
    inv['as_of'] = signal_date
    if 'pairs' not in inv:
        inv['pairs'] = {}

    for sig in signals:
        pair   = sig['pair']
        action = sig['action']

        if action in ('OPEN_LONG', 'OPEN_SHORT'):
            inv['pairs'][pair] = {
                'direction':      sig['direction'],
                's1_shares':      sig.get('s1_shares', 0),
                's2_shares':      sig.get('s2_shares', 0),
                'open_date':      signal_date,
                'open_s1_price':  sig.get('s1_price'),
                'open_s2_price':  sig.get('s2_price'),
                'days_held':      0,
            }

        elif action in ('CLOSE', 'CLOSE_STOP'):
            inv['pairs'][pair] = {'direction': None}

        elif action == 'HOLD':
            if pair in inv['pairs'] and inv['pairs'][pair].get('direction'):
                inv['pairs'][pair]['days_held'] = inv['pairs'][pair].get('days_held', 0) + 1
                inv['pairs'][pair]['s1_shares'] = sig.get('s1_shares', inv['pairs'][pair].get('s1_shares', 0))
                inv['pairs'][pair]['s2_shares'] = sig.get('s2_shares', inv['pairs'][pair].get('s2_shares', 0))

        # FLAT / BLACKOUT / NO_DATA: no inventory change

    return inv


# ── Simulation runner ──────────────────────────────────────────────────────────

def _run_simulation(strategy, pair_configs, signal_date, inventory):
    """Initialize and step through the simulation up to signal_date."""
    if strategy == 'mrpt':
        import PortfolioMRPTRun as PortfolioRun
        import PortfolioMRPTStrategyRuns as Runs
        from PortfolioMRPTRun import initialize, my_handle_data, CustomData
        DATA_START = '2024-01-30'
    else:
        import PortfolioMTFSRun as PortfolioRun
        import PortfolioMTFSStrategyRuns as Runs
        from PortfolioMTFSRun import initialize, my_handle_data, CustomData
        DATA_START = '2024-03-15'

    pairs      = [[s1, s2] for s1, s2, _ in pair_configs]
    pair_params = {}
    for s1, s2, ps_name in pair_configs:
        params_dict, _ = Runs._resolve_param_set(ps_name, f'{s1}/{s2}')
        pair_params[f'{s1}/{s2}'] = params_dict

    default_params, _ = Runs._resolve_param_set('default', 'fallback')
    all_symbols = sorted(set(sym for s1, s2, _ in pair_configs for sym in (s1, s2)))
    end_date_str = signal_date.strftime('%Y-%m-%d')

    log.info(f"Loading data {DATA_START} → {end_date_str} ({len(all_symbols)} symbols)...")
    historical_data = PortfolioRun.load_historical_data(DATA_START, end_date_str, all_symbols)

    log.info("Running simulation (warmup + signal day)...")

    context = SimpleNamespace()
    context.data = None
    initialize(context, pairs=pairs, params=default_params, pair_params=pair_params)

    signal_ts     = pd.Timestamp(signal_date)
    trade_start_ts = signal_ts
    inventory_injected = False

    for date_ts in historical_data.index:
        context.portfolio.current_date = date_ts
        context.portfolio.processed_dates.append(date_ts)
        current_data = CustomData(historical_data.loc[:date_ts])
        context.warmup_mode = date_ts < trade_start_ts

        if not inventory_injected and date_ts >= trade_start_ts:
            _inject_inventory_into_context(context, inventory, date_ts)
            inventory_injected = True

        if context.warmup_mode:
            _saved_rate = context.portfolio.interest_rate
            context.portfolio.interest_rate = 0.0

        my_handle_data(context, current_data)

        if context.warmup_mode:
            context.portfolio.interest_rate = _saved_rate
            for hist_attr in (
                'asset_cash_history', 'liability_loan_history',
                'asset_history', 'liability_history',
                'equity_history', 'value_history',
                'daily_pnl_history', 'interest_expense_history', 'acc_interest_history',
                'acc_daily_pnl_history',
            ):
                lst_ref = getattr(context.portfolio, hist_attr)
                if lst_ref:
                    lst_ref.pop()

    return context, signal_ts, all_symbols


# ── Print summary ──────────────────────────────────────────────────────────────

def _print_signals(signals, signal_date, strategy, dry_run):
    ACTION_LABELS = {
        'OPEN_LONG':  '  ▲ OPEN LONG ',
        'OPEN_SHORT': '  ▼ OPEN SHORT',
        'CLOSE':      '  ✕ CLOSE     ',
        'CLOSE_STOP': '  ✕ STOP LOSS ',
        'HOLD':       '  — HOLD      ',
        'FLAT':       '    FLAT      ',
        'BLACKOUT':   '  ◉ BLACKOUT  ',
        'NO_DATA':    '  ? NO DATA   ',
    }

    print()
    print(f"{'='*70}")
    strat_label = strategy.upper()
    print(f"  {strat_label} Daily Signal  —  {signal_date}{'  [DRY RUN]' if dry_run else ''}")
    print(f"{'='*70}")

    grouped = {
        'OPEN':  [s for s in signals if s['action'] in ('OPEN_LONG', 'OPEN_SHORT')],
        'CLOSE': [s for s in signals if s['action'] in ('CLOSE', 'CLOSE_STOP')],
        'HOLD':  [s for s in signals if s['action'] == 'HOLD'],
        'FLAT':  [s for s in signals if s['action'] == 'FLAT'],
        'OTHER': [s for s in signals if s['action'] in ('BLACKOUT', 'NO_DATA')],
    }

    # Determine which key to show for signal value
    sig_key = 'z_score' if strategy == 'mrpt' else 'momentum_spread'
    sig_prefix = 'z' if strategy == 'mrpt' else 'ms'

    for group_label, group in grouped.items():
        if not group:
            continue
        print()
        for sig in group:
            label = ACTION_LABELS.get(sig['action'], f"  {sig['action']:<13}")
            pair  = sig['pair']
            val   = sig.get(sig_key)
            val_str = f"{sig_prefix}={val:+.2f}" if val is not None else f"{sig_prefix}=n/a"

            if sig['action'] in ('OPEN_LONG', 'OPEN_SHORT', 'CLOSE', 'CLOSE_STOP', 'HOLD'):
                s1, s2 = pair.split('/')
                s1sh = sig.get('s1_shares', 0)
                s2sh = sig.get('s2_shares', 0)
                p1   = sig.get('s1_price', '')
                p2   = sig.get('s2_price', '')
                days = sig.get('days_held', '')
                days_str = f"  {days}d held" if days else ""
                print(f"  {label}  {pair:<12}  {val_str:<10}  "
                      f"{s1} {s1sh:+d}@{p1}  {s2} {s2sh:+d}@{p2}{days_str}")
            elif sig['action'] == 'BLACKOUT':
                print(f"  {label}  {pair:<12}  {val_str:<10}  {sig.get('note','')}")
            elif sig['action'] == 'FLAT':
                thr = sig.get('entry_threshold')
                thr_str = f"entry=±{thr:.2f}" if thr else ""
                print(f"  {label}  {pair:<12}  {val_str:<10}  {thr_str}")
            else:
                print(f"  {label}  {pair:<12}  {sig.get('note','')}")

    n = {k: len(v) for k, v in grouped.items()}
    print()
    print(f"  {n['OPEN']} open  |  {n['CLOSE']} close  |  "
          f"{n['HOLD']} hold  |  {n['FLAT']} flat  |  {n['OTHER']} other")
    print(f"{'='*70}")


# ── Main ───────────────────────────────────────────────────────────────────────

def run_daily_signal(strategy: str, signal_date: date, dry_run: bool = False):
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    pair_configs = get_pair_configs_mrpt() if strategy == 'mrpt' else get_pair_configs_mtfs()
    inventory    = load_inventory(strategy)

    context, signal_ts, all_symbols = _run_simulation(strategy, pair_configs, signal_date, inventory)

    # Extract today's prices
    prices_today = {}
    for sym in all_symbols:
        if sym in context.portfolio.price_history and context.portfolio.price_history[sym]:
            prices_today[sym] = context.portfolio.price_history[sym][-1][1]

    signals = extract_signals(context, pair_configs, signal_ts, inventory, prices_today, strategy)

    _print_signals(signals, signal_date, strategy, dry_run)

    # Save signals JSON
    end_date_str = signal_date.strftime('%Y-%m-%d')
    out = {
        'strategy':     strategy,
        'signal_date':  end_date_str,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'dry_run':      dry_run,
        'signals':      signals,
    }
    sig_path = os.path.join(SIGNALS_DIR, f"{strategy}_signals_{signal_date.strftime('%Y%m%d')}.json")
    with open(sig_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    log.info(f"Signals saved → {sig_path}")

    # Update inventory
    if not dry_run:
        updated_inv = update_inventory_from_signals(inventory, signals, end_date_str)
        save_inventory(updated_inv, strategy)
        print(f"\n  inventory_{strategy}.json updated for {end_date_str}")
    else:
        print(f"\n  [DRY RUN] inventory_{strategy}.json NOT updated")

    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Daily signal generator for MRPT and MTFS strategies')
    parser.add_argument('--strategy', choices=['mrpt', 'mtfs'], required=True,
                        help='Strategy to run: mrpt (mean reversion) or mtfs (momentum trend)')
    parser.add_argument('--date', type=str, default=None,
                        help='Signal date YYYY-MM-DD (default: most recent weekday)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print signals without updating inventory JSON')
    args = parser.parse_args()

    signal_date = date.fromisoformat(args.date) if args.date else prev_weekday(date.today())
    run_daily_signal(args.strategy, signal_date, dry_run=args.dry_run)
