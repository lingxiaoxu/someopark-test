"""
MRPTDailySignal.py — MRPT 每日信号生成器
=======================================
每个交易日收盘后运行一次，输出今天对每对的操作指令。

用法：
    python MRPTDailySignal.py                      # 使用今天日期
    python MRPTDailySignal.py --date 2026-03-04    # 指定日期（补跑）
    python MRPTDailySignal.py --dry-run            # 只看信号，不更新 inventory

工作流程：
    1. 读取 inventory.json（你的实际持仓记录）
    2. 用 2024-10-01 ~ signal_date 的历史数据跑模拟（warmup 建状态）
    3. 把 inventory.json 里的仓位注入模拟器，让模拟器与现实对齐
    4. 今天的信号（OPEN/CLOSE/HOLD/BLACKOUT）+ 具体股数写入 signals_YYYYMMDD.json
    5. 如果不是 --dry-run，自动更新 inventory.json

inventory.json 格式：
    {
      "as_of": "2026-03-03",
      "capital": 500000,
      "pairs": {
        "CART/DASH": {
          "direction": "short",      // "long" / "short" / null（空仓）
          "s1_shares": -847,         // CART股数（负=空头）
          "s2_shares": 1203,         // DASH股数（正=多头）
          "open_date": "2026-02-18",
          "open_s1_price": 51.20,
          "open_s2_price": 38.90,
          "days_held": 11
        },
        "GS/ALLY": { "direction": null }
      }
    }

signals_YYYYMMDD.json 输出格式：
    {
      "signal_date": "2026-03-04",
      "generated_at": "2026-03-04T16:35:00",
      "signals": [
        {
          "pair": "CART/DASH",
          "action": "HOLD",
          "direction": "short",
          "z_score": 1.85,
          "entry_z": 2.20,
          "exit_z": 0.0,
          "s1_shares": -847,
          "s2_shares": 1203,
          "s1_price": 51.80,
          "s2_price": 39.10,
          "days_held": 12,
          "note": "In short, z=1.85 above exit threshold 0.0 — hold"
        },
        {
          "pair": "AMG/BEN",
          "action": "OPEN_SHORT",
          "direction": "short",
          "z_score": 2.31,
          "s1_shares": -312,
          "s2_shares": 480,
          "s1_price": 42.10,
          "s2_price": 27.50,
          "note": "z=2.31 > entry 2.20 — open short"
        },
        {
          "pair": "ESS/EXPD",
          "action": "BLACKOUT",
          "note": "Earnings blackout: EXPD 2026-03-06"
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

import numpy as np
import pandas as pd

# ── project imports ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import PortfolioMRPTRun as PortfolioRun
import PortfolioMRPTStrategyRuns as Runs
from PortfolioClasses import Portfolio, PortfolioMakeOrder, PortfolioConstruct, \
    PortfolioStopLossFunction, Execution

log = logging.getLogger('MRPTDailySignal')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
INVENTORY_FILE = os.path.join(BASE_DIR, 'inventory.json')
SIGNALS_DIR   = os.path.join(BASE_DIR, 'signals')

DATA_START    = '2024-01-30'   # warmup always starts here (18mo train window)


# ── helpers ───────────────────────────────────────────────────────────────────

def prev_weekday(d: date) -> date:
    """Return the most recent weekday on or before d."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def load_inventory() -> dict:
    """Load inventory.json or return a blank skeleton."""
    if os.path.exists(INVENTORY_FILE):
        with open(INVENTORY_FILE) as f:
            return json.load(f)
    # Fresh start — no positions
    return {'as_of': None, 'capital': 500000, 'pairs': {}}


def save_inventory(inv: dict):
    with open(INVENTORY_FILE, 'w') as f:
        json.dump(inv, f, indent=2, default=str)
    log.info(f"inventory.json updated → {INVENTORY_FILE}")


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# ── pair config (mirrors MRPTWalkForward / forward30d config) ─────────────────────

def get_pair_configs() -> list[tuple[str, str, str]]:
    """
    Return the 15 pairs + their best param_sets.
    Edit this list whenever your selection module updates the universe.
    Format: (s1, s2, param_set_name)
    """
    return [
        ('MSCI',  'LII',   'fast_reentry'),
        ('D',     'MCHP',  'fast_signal'),
        ('DG',    'MOS',   'slow_signal'),
        ('ESS',   'EXPD',  'fast_signal'),
        ('ACGL',  'UHS',   'patient_hold'),
        ('AAPL',  'META',  'long_z_short_v'),
        ('YUM',   'MCD',   'slow_signal'),
        ('GS',    'ALLY',  'long_z_short_v'),
        ('CL',    'USO',   'conservative_no_leverage'),
        ('ALGN',  'UAL',   'fast_signal_tight_stop'),
        ('ARES',  'CG',    'fast_signal'),
        ('AMG',   'BEN',   'fast_signal'),
        ('LYFT',  'UBER',  'conservative_no_leverage'),
        ('TW',    'CME',   'conservative_no_leverage'),
        ('CART',  'DASH',  'aggressive'),
    ]


# ── signal extraction ─────────────────────────────────────────────────────────

def _inject_inventory_into_context(context, inventory: dict, signal_date: pd.Timestamp):
    """
    Before running signal_date, inject real positions from inventory into the
    simulation's internal state so the system's in_long/in_short flags match reality.

    This handles the "previously opened position" problem:
    - If inventory says CART/DASH is short, we set pair[2]['in_short'] = True
      and load the actual share counts into context.portfolio.positions
    - The simulation then runs today's logic with the correct starting state
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
            # Inject actual share counts into portfolio.positions
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
            # No position
            pair[2]['in_long'] = False
            pair[2]['in_short'] = False
            context.portfolio.positions.pop(s1, None)
            context.portfolio.positions.pop(s2, None)


def _extract_signal_from_recorded_vars(context, pair_key: str, signal_date: pd.Timestamp,
                                        inventory: dict, prices_today: dict) -> dict:
    """
    Read the recorded_vars for this pair on signal_date and build an action dict.

    recorded_vars keys (from record_vars in PortfolioMRPTRun.py):
        Z_<sector>, Hedge_<sector>, in_long, in_short, z_score, entry_z, exit_z,
        volatility_threshold, spread, stop_loss_triggered, ...
    """
    rv = context.recorded_vars.get(pair_key, {})
    today_rv = None
    # Find the entry for signal_date (Timestamp key)
    for k, v in rv.items():
        if pd.Timestamp(k).date() == signal_date.date():
            today_rv = v
            break

    if today_rv is None:
        return {
            'pair': pair_key,
            'action': 'NO_DATA',
            'note': 'No recorded_vars for this date — data may be missing',
        }

    s1, s2 = pair_key.split('/')

    # Determine sector key for Z
    _sector_map = {
        frozenset({'AAPL','META','D','MCHP','CART','DASH'}):     'tech',
        frozenset({'GS','ALLY','ACGL','UHS','ARES','CG','AMG','BEN','TW','CME'}): 'finance',
        frozenset({'ALGN','UAL','MSCI','LII','LYFT','UBER'}):    'industrial',
        frozenset({'CL','USO','ESS','EXPD'}):                    'energy',
        frozenset({'DG','MOS','YUM','MCD'}):                     'food',
    }
    sector = 'finance'  # fallback
    pair_set = frozenset({s1, s2})
    for sym_set, sec in _sector_map.items():
        if pair_set & sym_set:
            sector = sec
            break

    z_score  = today_rv.get(f'Z_{sector}', today_rv.get('z_score', float('nan')))
    in_long  = bool(today_rv.get('in_long',  False))
    in_short = bool(today_rv.get('in_short', False))

    # Current prices
    s1_price = prices_today.get(s1, float('nan'))
    s2_price = prices_today.get(s2, float('nan'))

    # Get params for this pair to show thresholds
    pair_obj = None
    for p in context.strategy_pairs:
        if p[0] == s1 and p[1] == s2:
            pair_obj = p
            break

    # Retrieve entry/exit thresholds from context.execution (may have been per-pair overridden)
    entry_z = getattr(context.execution, 'base_entry_z', 0.75)
    exit_z  = getattr(context.execution, 'base_exit_z',  0.0)
    vol_factor = getattr(context.execution, 'entry_volatility_factor', 2.25)
    if pair_key in context.pair_params:
        pp = context.pair_params[pair_key]
        entry_z   = pp.get('base_entry_z',            entry_z)
        exit_z    = pp.get('base_exit_z',              exit_z)
        vol_factor = pp.get('entry_volatility_factor', vol_factor)

    # Effective entry threshold — recorded as Entry_Z by record_vars
    eff_entry_z = today_rv.get('Entry_Z', entry_z)

    # Inventory state
    inv_pair = inventory.get('pairs', {}).get(pair_key, {})
    inv_direction = inv_pair.get('direction')
    days_held = inv_pair.get('days_held', 0)

    # Determine shares from portfolio positions
    s1_shares = context.portfolio.positions.get(s1, 0)
    s2_shares = context.portfolio.positions.get(s2, 0)

    # Check earnings blackout
    blackout = today_rv.get('earnings_blackout', False)
    blackout_reason = today_rv.get('earnings_blackout_reason', '')

    # Determine action
    if blackout and not in_long and not in_short:
        return {
            'pair': pair_key, 'action': 'BLACKOUT',
            'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
            'entry_z': round(eff_entry_z, 3) if not np.isnan(eff_entry_z) else None,
            's1': s1, 's2': s2,
            's1_price': round(s1_price, 4) if not np.isnan(s1_price) else None,
            's2_price': round(s2_price, 4) if not np.isnan(s2_price) else None,
            'note': f'Earnings blackout: {blackout_reason}',
        }

    stop_triggered = today_rv.get('stop_loss_triggered', False)

    # CLOSE (stop loss or mean reversion)
    if stop_triggered and (in_long or in_short) and inv_direction:
        return {
            'pair': pair_key, 'action': 'CLOSE_STOP',
            'direction': inv_direction,
            'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
            's1': s1, 's2': s2,
            's1_shares': s1_shares, 's2_shares': s2_shares,
            's1_price': round(s1_price, 4), 's2_price': round(s2_price, 4),
            'days_held': days_held,
            'note': f'Stop loss triggered — close {inv_direction} position',
        }

    if not in_long and not in_short and inv_direction:
        # Simulation closed the position (mean reversion or holding period)
        return {
            'pair': pair_key, 'action': 'CLOSE',
            'direction': inv_direction,
            'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
            'exit_z': round(exit_z, 3),
            's1': s1, 's2': s2,
            's1_shares': s1_shares, 's2_shares': s2_shares,
            's1_price': round(s1_price, 4), 's2_price': round(s2_price, 4),
            'days_held': days_held,
            'note': f'z={z_score:.2f} passed exit threshold {exit_z} — close {inv_direction}',
        }

    if in_long and not inv_direction:
        return {
            'pair': pair_key, 'action': 'OPEN_LONG',
            'direction': 'long',
            'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
            'entry_z': round(eff_entry_z, 3) if not np.isnan(eff_entry_z) else None,
            's1': s1, 's2': s2,
            's1_shares': s1_shares, 's2_shares': s2_shares,
            's1_price': round(s1_price, 4), 's2_price': round(s2_price, 4),
            'note': f'z={z_score:.2f} below -{eff_entry_z:.2f} — open long  ({s1} long, {s2} short)',
        }

    if in_short and not inv_direction:
        return {
            'pair': pair_key, 'action': 'OPEN_SHORT',
            'direction': 'short',
            'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
            'entry_z': round(eff_entry_z, 3) if not np.isnan(eff_entry_z) else None,
            's1': s1, 's2': s2,
            's1_shares': s1_shares, 's2_shares': s2_shares,
            's1_price': round(s1_price, 4), 's2_price': round(s2_price, 4),
            'note': f'z={z_score:.2f} above +{eff_entry_z:.2f} — open short ({s1} short, {s2} long)',
        }

    if (in_long or in_short) and inv_direction:
        dir_label = 'long' if in_long else 'short'
        return {
            'pair': pair_key, 'action': 'HOLD',
            'direction': dir_label,
            'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
            'entry_z': round(eff_entry_z, 3) if not np.isnan(eff_entry_z) else None,
            'exit_z': round(exit_z, 3),
            's1': s1, 's2': s2,
            's1_shares': s1_shares, 's2_shares': s2_shares,
            's1_price': round(s1_price, 4), 's2_price': round(s2_price, 4),
            'days_held': days_held,
            'note': f'Holding {dir_label}, z={z_score:.2f}, exit at {exit_z}',
        }

    # Flat and no signal
    return {
        'pair': pair_key, 'action': 'FLAT',
        'z_score': round(z_score, 3) if not np.isnan(z_score) else None,
        'entry_z': round(eff_entry_z, 3) if not np.isnan(eff_entry_z) else None,
        's1': s1, 's2': s2,
        's1_price': round(s1_price, 4), 's2_price': round(s2_price, 4),
        'note': f'No position, z={z_score:.2f} within ±{eff_entry_z:.2f} — wait',
    }


# ── inventory update ──────────────────────────────────────────────────────────

def update_inventory_from_signals(inventory: dict, signals: list, signal_date: str,
                                   context) -> dict:
    """
    Apply today's signals to inventory, returning an updated copy.
    Call this only after you confirm orders were actually executed.
    """
    inv = deepcopy(inventory)
    inv['as_of'] = signal_date

    if 'pairs' not in inv:
        inv['pairs'] = {}

    for sig in signals:
        pair = sig['pair']
        action = sig['action']

        if action in ('OPEN_LONG', 'OPEN_SHORT'):
            inv['pairs'][pair] = {
                'direction': sig['direction'],
                's1_shares':  sig.get('s1_shares', 0),
                's2_shares':  sig.get('s2_shares', 0),
                'open_date':  signal_date,
                'open_s1_price': sig.get('s1_price'),
                'open_s2_price': sig.get('s2_price'),
                'days_held': 0,
            }

        elif action in ('CLOSE', 'CLOSE_STOP'):
            inv['pairs'][pair] = {'direction': None}

        elif action == 'HOLD':
            if pair in inv['pairs'] and inv['pairs'][pair].get('direction'):
                inv['pairs'][pair]['days_held'] = inv['pairs'][pair].get('days_held', 0) + 1
                inv['pairs'][pair]['s1_shares'] = sig.get('s1_shares', inv['pairs'][pair].get('s1_shares', 0))
                inv['pairs'][pair]['s2_shares'] = sig.get('s2_shares', inv['pairs'][pair].get('s2_shares', 0))

        elif action in ('FLAT', 'BLACKOUT', 'NO_DATA'):
            # No change to inventory
            pass

    return inv


# ── main ──────────────────────────────────────────────────────────────────────

def run_daily_signal(signal_date: date, dry_run: bool = False):
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    inventory = load_inventory()
    pair_configs = get_pair_configs()

    # Build pairs and per-pair params
    pairs = [[s1, s2] for s1, s2, _ in pair_configs]
    pair_params = {}
    for s1, s2, ps_name in pair_configs:
        params_dict, _ = Runs._resolve_param_set(ps_name, f'{s1}/{s2}')
        pair_params[f'{s1}/{s2}'] = params_dict

    default_params, _ = Runs._resolve_param_set('default', 'fallback')
    all_symbols = sorted(set(sym for s1, s2, _ in pair_configs for sym in (s1, s2)))

    end_date_str      = signal_date.strftime('%Y-%m-%d')
    trade_start_str   = signal_date.strftime('%Y-%m-%d')  # only "trade" on signal_date

    log.info(f"Loading data {DATA_START} → {end_date_str} ({len(all_symbols)} symbols)...")
    historical_data = PortfolioRun.load_historical_data(DATA_START, end_date_str, all_symbols)

    log.info("Running simulation (warmup + signal day)...")

    # ── Replicate _run_backtest internals so we can intercept context ──────────
    # We need access to context after the run to read recorded_vars and positions.
    # We do this by calling initialize() and the day loop directly.

    from types import SimpleNamespace
    from PortfolioClasses import Portfolio, PortfolioMakeOrder, PortfolioConstruct, \
        PortfolioStopLossFunction, Execution, PortfolioAnalysis
    from PortfolioMRPTRun import initialize, my_handle_data, CustomData

    context = SimpleNamespace()
    context.data = None

    initialize(context, pairs=pairs, params=default_params, pair_params=pair_params)

    signal_ts = pd.Timestamp(signal_date)
    trade_start_ts = signal_ts

    # Inject inventory BEFORE the signal day loop starts
    # (warmup days don't need it; injection is needed only for the signal day)
    inventory_injected = False

    portfolio_analysis = PortfolioAnalysis(context.portfolio)

    for date_ts in historical_data.index:
        context.portfolio.current_date = date_ts
        context.portfolio.processed_dates.append(date_ts)
        current_data = CustomData(historical_data.loc[:date_ts])
        context.warmup_mode = date_ts < trade_start_ts

        # Inject inventory positions right before the signal day
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

    # ── Extract today's prices ─────────────────────────────────────────────────
    prices_today = {}
    for sym in all_symbols:
        if sym in context.portfolio.price_history and context.portfolio.price_history[sym]:
            prices_today[sym] = context.portfolio.price_history[sym][-1][1]

    # ── Extract signals for each pair ──────────────────────────────────────────
    signals = []
    for s1, s2, _ in pair_configs:
        pair_key = f"{s1}/{s2}"
        sig = _extract_signal_from_recorded_vars(
            context, pair_key, signal_ts, inventory, prices_today)
        signals.append(sig)

    # ── Print summary ──────────────────────────────────────────────────────────
    print()
    print(f"{'='*70}")
    print(f"  MRPT Daily Signal  —  {signal_date}{'  [DRY RUN]' if dry_run else ''}")
    print(f"{'='*70}")

    action_colors = {
        'OPEN_LONG':  '  ▲ OPEN LONG ',
        'OPEN_SHORT': '  ▼ OPEN SHORT',
        'CLOSE':      '  ✕ CLOSE     ',
        'CLOSE_STOP': '  ✕ STOP LOSS ',
        'HOLD':       '  — HOLD      ',
        'FLAT':       '    FLAT      ',
        'BLACKOUT':   '  ◉ BLACKOUT  ',
        'NO_DATA':    '  ? NO DATA   ',
    }

    open_signals  = [s for s in signals if s['action'] in ('OPEN_LONG', 'OPEN_SHORT')]
    close_signals = [s for s in signals if s['action'] in ('CLOSE', 'CLOSE_STOP')]
    hold_signals  = [s for s in signals if s['action'] == 'HOLD']
    flat_signals  = [s for s in signals if s['action'] == 'FLAT']
    other_signals = [s for s in signals if s['action'] in ('BLACKOUT', 'NO_DATA')]

    for group_label, group in [
        ('OPEN', open_signals), ('CLOSE', close_signals),
        ('HOLD', hold_signals), ('FLAT', flat_signals), ('OTHER', other_signals),
    ]:
        if not group:
            continue
        print()
        for sig in group:
            label = action_colors.get(sig['action'], f"  {sig['action']:<13}")
            pair  = sig['pair']
            z     = sig.get('z_score')
            zstr  = f"z={z:+.2f}" if z is not None else "z=n/a"

            if sig['action'] in ('OPEN_LONG', 'OPEN_SHORT', 'CLOSE', 'CLOSE_STOP', 'HOLD'):
                s1, s2 = pair.split('/')
                s1sh = sig.get('s1_shares', 0)
                s2sh = sig.get('s2_shares', 0)
                p1   = sig.get('s1_price', '')
                p2   = sig.get('s2_price', '')
                days = sig.get('days_held', '')
                days_str = f"  {days}d held" if days else ""
                print(f"  {label}  {pair:<12}  {zstr:<10}  "
                      f"{s1} {s1sh:+d}@{p1}  {s2} {s2sh:+d}@{p2}{days_str}")
            elif sig['action'] == 'BLACKOUT':
                print(f"  {label}  {pair:<12}  {zstr:<10}  {sig['note']}")
            elif sig['action'] == 'FLAT':
                ez = sig.get('entry_z')
                ezstr = f"entry=±{ez:.2f}" if ez else ""
                print(f"  {label}  {pair:<12}  {zstr:<10}  {ezstr}")
            else:
                print(f"  {label}  {pair:<12}  {sig.get('note','')}")

    print()
    print(f"  {len(open_signals)} open  |  {len(close_signals)} close  |  "
          f"{len(hold_signals)} hold  |  {len(flat_signals)} flat  |  {len(other_signals)} other")
    print(f"{'='*70}")

    # ── Save signals JSON ──────────────────────────────────────────────────────
    out = {
        'signal_date': end_date_str,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'dry_run': dry_run,
        'signals': signals,
    }
    sig_path = os.path.join(SIGNALS_DIR, f"signals_{signal_date.strftime('%Y%m%d')}.json")
    with open(sig_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    log.info(f"Signals saved → {sig_path}")

    # ── Update inventory ───────────────────────────────────────────────────────
    if not dry_run:
        updated_inv = update_inventory_from_signals(inventory, signals, end_date_str, context)
        save_inventory(updated_inv)
        print(f"\n  inventory.json updated for {end_date_str}")
    else:
        print(f"\n  [DRY RUN] inventory.json NOT updated")

    return out


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='MRPT daily signal generator')
    parser.add_argument('--date', type=str, default=None,
                        help='Signal date YYYY-MM-DD (default: today)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print signals without updating inventory.json')
    args = parser.parse_args()

    if args.date:
        signal_date = date.fromisoformat(args.date)
    else:
        signal_date = prev_weekday(date.today())

    run_daily_signal(signal_date, dry_run=args.dry_run)
