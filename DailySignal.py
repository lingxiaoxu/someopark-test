"""
DailySignal.py — 每日信号生成器（MRPT + MTFS 双策略，支持组合运行）
=================================================================
每个交易日收盘后运行一次，输出今天对每对的操作指令，附带：
  - 实际可执行的股数（基于 --capital 参数 scaling）
  - 预计执行市值 (est_value)
  - 市场 regime 评分和动态权重建议（VIX/信用利差/AI含量/地缘等）

用法：
    # 单策略模式
    python DailySignal.py --strategy mrpt --capital 600000
    python DailySignal.py --strategy mtfs --capital 400000 --dry-run

    # 组合模式（同时运行两个策略，自动 regime 权重分配）
    python DailySignal.py --strategy both --total-capital 1000000
    python DailySignal.py --strategy both --total-capital 1000000 --mrpt-weight 0.6
    python DailySignal.py --strategy both --total-capital 1000000 --date 2026-03-07

Capital Scaling 逻辑：
    回测基准资本 = 1,000,000 美元
    scale_factor = actual_capital / 1,000,000
    actual_shares = round(backtest_shares * scale_factor)
    actual_pnl    = backtest_pnl * scale_factor

    # 若策略通过 walk-forward 跑出 Sharpe/MaxDD，那些%指标 scale 无关
    # Dollar PnL / Dollar DD 线性 scale

Regime 权重逻辑（--strategy both 时）：
    若未指定 --mrpt-weight，自动运行 RegimeDetector：
      - VIX / MOVE / 信用利差 / 利率环境 / AI动量含量 / 地缘政治 / 宏观压力
    输出 regime_score (0-100) 和建议的 mrpt_weight / mtfs_weight

inventory_<strategy>.json 格式：
    {
      "as_of": "2026-03-03",
      "capital": 500000,           ← 回测基准资本（用于 scaling 分母）
      "pairs": {
        "CART/DASH": {
          "direction": "short",
          "s1_shares": -847,
          "s2_shares": 1203,
          "open_date": "2026-02-18",
          "open_s1_price": 51.20,
          "open_s2_price": 38.90,
          "days_held": 11
        }
      }
    }

signals/<strategy>_YYYYMMDD.json 输出格式：
    {
      "strategy": "mrpt",
      "signal_date": "2026-03-04",
      "capital": 600000,
      "scale_factor": 0.6,
      "regime": { ... },
      "signals": [
        {
          "pair": "CART/DASH",
          "action": "OPEN_LONG",
          "direction": "long",
          "z_score": 2.35,
          "entry_threshold": 2.20,
          "s1_shares": -508,         ← scaled from backtest -847
          "s2_shares": 722,
          "s1": {
            "symbol": "CART", "shares": -508, "side": "SELL_SHORT",
            "price": 51.20, "est_value": 26010
          },
          "s2": {
            "symbol": "DASH", "shares": 722, "side": "BUY",
            "price": 38.90, "est_value": 28086
          },
          "days_held": 0
        }
      ]
    }
"""

import argparse
import json
import logging
import os
import sys
import glob
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
TRADING_DIR = os.path.join(BASE_DIR, 'trading_signals')
SIGNALS_DIR = TRADING_DIR
REPORTS_DIR = TRADING_DIR

# 回测基准资本（order_target_percent 基于此，scaling 以此为分母）
BACKTEST_BASE_CAPITAL = 1_000_000.0


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
    return {'as_of': None, 'capital': 500_000, 'pairs': {}}


def save_inventory(inv: dict, strategy: str):
    path = inventory_path(strategy)
    with open(path, 'w') as f:
        json.dump(inv, f, indent=2, default=str)
    log.info(f"inventory_{strategy}.json updated → {path}")


# ── Pair config ────────────────────────────────────────────────────────────────

def get_pair_configs_mrpt() -> list[tuple[str, str, str]]:
    """
    MRPT: 10 OOS-filtered pairs (from 6-window walk-forward, updated 2026-03-08).
    Excluded (poor OOS): GS/ALLY, ESS/EXPD, MSCI/LII, AMG/BEN.
    Params = DSR-best from W6 (most recent training window).
    """
    return [
        # Tier 1: strong OOS Sharpe > 1.0
        ('ALGN',  'UAL',   'slow_reentry'),           # OOS Sharpe=+1.94, PnL=+$17K
        ('DG',    'MOS',   'deep_dislocation'),        # OOS Sharpe=+1.25, PnL=+$19K
        # Tier 2: decent OOS Sharpe 0.3-0.7
        ('ACGL',  'UHS',   'vol_agnostic'),            # OOS Sharpe=+0.67, PnL=+$3K
        ('TW',    'CME',   'conservative_no_leverage'),# OOS Sharpe=+0.53, PnL=+$1.8K
        ('ARES',  'CG',    'fast_signal'),             # OOS Sharpe=+0.38, PnL=+$2.9K
        # Tier 3: marginal but positive PnL, acceptable stability
        ('D',     'MCHP',  'conservative_no_leverage'),# OOS Sharpe=+0.14, PnL=+$388
        ('CART',  'DASH',  'static_threshold'),        # OOS Sharpe=+0.04, PnL=+$454, W3+/3-
        # Tier 4: slight negative but stable enough (W3+/1-)
        ('CL',    'USO',   'patient_hold'),            # OOS Sharpe=-0.05, W3+/1-
        ('YUM',   'MCD',   'long_z_short_v'),          # OOS Sharpe=-0.13, W3+/3-
        ('LYFT',  'UBER',  'conservative'),            # OOS Sharpe=-0.25, W3+/2-
    ]


def get_pair_configs_mtfs() -> list[tuple[str, str, str]]:
    """
    MTFS: 5 OOS-filtered pairs (from 6-window walk-forward, updated 2026-03-08).
    Excluded (poor OOS): CL/USO, ALGN/UAL, GS/ALLY, CART/DASH, ACGL/UHS,
                         D/MCHP, AAPL/META, ARES/CG (PosW=1/5 too unstable).
    Params = DSR-best from W6 (most recent training window).
    """
    return [
        # Tier 1: strong OOS Sharpe + momentum confirmed
        ('TW',    'CME',   'beta_neutral'),            # OOS Sharpe=+1.49, PnL=+$216
        ('DG',    'MOS',   'entry_threshold_weak'),    # OOS Sharpe=+0.99, PnL=+$3.8K
        # Tier 2: positive PnL, reasonable stability
        ('AMG',   'BEN',   'aggressive'),              # OOS Sharpe=+0.53, PnL=+$3.9K, W3+/2-
        ('LYFT',  'UBER',  'aggressive'),              # OOS Sharpe=+0.46, PnL=+$3.8K, W3+/1-
        ('ESS',   'EXPD',  'raw_momentum'),            # OOS Sharpe=+0.45, PnL=+$3.4K, W2+/1-
    ]


# ── Capital scaling ────────────────────────────────────────────────────────────

def compute_scale_factor(actual_capital: float, backtest_sim_capital: float) -> float:
    """
    Scale factor for converting backtest shares/PnL to actual capital.
    backtest_sim_capital = capital in inventory (what the sim was run with).
    """
    if backtest_sim_capital <= 0:
        return 1.0
    return actual_capital / backtest_sim_capital


def scale_shares(shares: int | float, scale: float) -> int:
    """Round scaled shares to nearest whole number."""
    if shares == 0:
        return 0
    return int(round(shares * scale))


def build_leg_dict(symbol: str, shares: int, price: float | None) -> dict:
    """Build the per-leg execution dict with side, shares, est_value."""
    if shares > 0:
        side = 'BUY'
    elif shares < 0:
        side = 'SELL_SHORT'
    else:
        side = 'FLAT'

    est_value = abs(shares) * price if price and not np.isnan(price) else None

    return {
        'symbol':    symbol,
        'shares':    shares,
        'side':      side,
        'price':     round(price, 4) if price and not np.isnan(price) else None,
        'est_value': round(est_value, 0) if est_value else None,
    }


# ── Inventory injection ────────────────────────────────────────────────────────

def _inject_inventory_into_context(context, inventory: dict, signal_date: pd.Timestamp):
    """
    Inject real positions from inventory into simulation state.

    In addition to setting in_long/in_short flags and portfolio.positions,
    this also injects a synthetic Trade record into pair_trade_history so
    that check_time_based_stop_loss() can correctly count days held from
    the real open_date (not just warmup history).
    """
    from PortfolioClasses import Trade

    pairs_inv = inventory.get('pairs', {})

    for pair in context.strategy_pairs:
        s1, s2 = pair[0], pair[1]
        key = f"{s1}/{s2}"
        inv_pair  = pairs_inv.get(key, {})
        direction = inv_pair.get('direction')

        if direction in ('long', 'short'):
            s1s = inv_pair.get('s1_shares', 0)
            s2s = inv_pair.get('s2_shares', 0)

            if direction == 'long':
                pair[2]['in_long']  = True
                pair[2]['in_short'] = False
            else:
                pair[2]['in_long']  = False
                pair[2]['in_short'] = True

            if s1s:
                context.portfolio.positions[s1] = s1s
            if s2s:
                context.portfolio.positions[s2] = s2s

            # ── Inject synthetic Trade so time-based stop works ──────────
            open_date_str = inv_pair.get('open_date')
            if open_date_str:
                try:
                    open_ts = pd.Timestamp(open_date_str)
                    # Find the closest date in the data index on or after open_date
                    # (handles weekends/holidays — Trade.date must be in price history)
                    all_dates = context.portfolio.processed_dates
                    valid_dates = [d for d in all_dates if d >= open_ts]
                    trade_date = valid_dates[0] if valid_dates else (
                        all_dates[-1] if all_dates else open_ts)

                    open_p1 = inv_pair.get('open_s1_price') or 0
                    # s1 amount: positive for long (bought s1), negative for short (sold s1)
                    s1_amount = abs(s1s) if direction == 'long' else -abs(s1s)
                    fake_trade = Trade(
                        date=trade_date,
                        symbol=s1,
                        amount=s1_amount,
                        price=open_p1,
                        order_type='open',
                    )
                    # Clear any stale synthetic trades and inject fresh one
                    context.portfolio.pair_trade_history[key] = [fake_trade]
                    log.debug(f"[inject] {key}: fake Trade injected at {trade_date.date()}, "
                              f"direction={direction}, s1_amount={s1_amount}")
                except Exception as e:
                    log.warning(f"[inject] {key}: could not inject fake Trade: {e}")

        else:
            pair[2]['in_long']  = False
            pair[2]['in_short'] = False
            context.portfolio.positions.pop(s1, None)
            context.portfolio.positions.pop(s2, None)


# ── Signal extraction ──────────────────────────────────────────────────────────

_SECTOR_MAP = {
    frozenset({'AAPL', 'META', 'D', 'MCHP', 'CART', 'DASH'}):                       'tech',
    frozenset({'GS', 'ALLY', 'ACGL', 'UHS', 'ARES', 'CG', 'AMG', 'BEN', 'TW', 'CME'}): 'finance',
    frozenset({'ALGN', 'UAL', 'MSCI', 'LII', 'LYFT', 'UBER'}):                      'industrial',
    frozenset({'CL', 'USO', 'ESS', 'EXPD'}):                                         'energy',
    frozenset({'DG', 'MOS', 'YUM', 'MCD'}):                                          'food',
}


def _get_sector(s1, s2) -> str:
    pair_set = frozenset({s1, s2})
    for sym_set, sec in _SECTOR_MAP.items():
        if pair_set & sym_set:
            return sec
    return 'finance'


def _find_today_rv(context, pair_key: str, signal_date: pd.Timestamp) -> dict | None:
    """
    Find recorded_vars for signal_date.
    Falls back to the most recent available date if exact match not found
    (e.g. signal_date is a weekend or data not yet published for that day).
    """
    rv = context.recorded_vars.get(pair_key, {})
    if not rv:
        return None
    # Try exact match first
    for k, v in rv.items():
        if pd.Timestamp(k).date() == signal_date.date():
            return v
    # Fall back to most recent date before or on signal_date
    candidates = {pd.Timestamp(k): v for k, v in rv.items()
                  if pd.Timestamp(k).date() <= signal_date.date()}
    if candidates:
        latest_key = max(candidates.keys())
        log.debug(f"{pair_key}: using {latest_key.date()} as fallback for {signal_date.date()}")
        return candidates[latest_key]
    return None


def _build_signal(pair_key, s1, s2, today_rv, inventory, context,
                  prices_today, strategy, scale_factor: float = 1.0) -> dict:
    """
    Build action dict from today's recorded_vars.
    scale_factor converts backtest shares to actual-capital shares.
    """
    in_long  = bool(today_rv.get('in_long',  False))
    in_short = bool(today_rv.get('in_short', False))

    s1_price = prices_today.get(s1, float('nan'))
    s2_price = prices_today.get(s2, float('nan'))

    inv_pair      = inventory.get('pairs', {}).get(pair_key, {})
    inv_direction = inv_pair.get('direction')
    days_held     = inv_pair.get('days_held', 0)

    # Raw (backtest) shares from simulator
    s1_shares_raw = context.portfolio.positions.get(s1, 0)
    s2_shares_raw = context.portfolio.positions.get(s2, 0)

    # Scaled shares (actual capital)
    s1_shares = scale_shares(s1_shares_raw, scale_factor)
    s2_shares = scale_shares(s2_shares_raw, scale_factor)

    stop_triggered = today_rv.get('stop_loss_triggered', False)

    # ── Strategy-specific signal value ────────────────────────────────────
    if strategy == 'mrpt':
        sector         = _get_sector(s1, s2)
        signal_value   = today_rv.get(f'Z_{sector}', today_rv.get('z_score', float('nan')))
        entry_thr      = today_rv.get('Entry_Z', float('nan'))
        exit_thr       = today_rv.get('Exit_Z', 0.0)
        signal_label   = 'z_score'

        blackout        = today_rv.get('earnings_blackout', False)
        blackout_reason = today_rv.get('earnings_blackout_reason', '')
        if blackout and not in_long and not in_short:
            return {
                'pair': pair_key, 'action': 'BLACKOUT',
                signal_label: _r(signal_value),
                'entry_threshold': _r(entry_thr),
                's1': s1, 's2': s2,
                's1_price': _r(s1_price, 4), 's2_price': _r(s2_price, 4),
                'note': f'Earnings blackout: {blackout_reason}',
            }
    else:  # mtfs
        sector         = _get_sector(s1, s2)
        signal_value   = today_rv.get(f'Momentum_Spread_{sector}',
                                      today_rv.get('Momentum_Spread', float('nan')))
        entry_thr      = today_rv.get('Entry_Threshold', float('nan'))
        exit_thr       = today_rv.get('Exit_Threshold', 0.0)
        signal_label   = 'momentum_spread'

    sv  = _r(signal_value)
    et  = _r(entry_thr)
    ext = _r(exit_thr)
    p1  = _r(s1_price, 4)
    p2  = _r(s2_price, 4)

    base = {
        'pair':    pair_key,
        's1':      s1,
        's2':      s2,
        signal_label: sv,
        's1_price': p1,
        's2_price': p2,
    }

    def _legs(action_dir: str) -> dict:
        """Build execution legs for open/hold actions."""
        if action_dir == 'long':
            # long pair: buy s1, sell s2
            s1_leg = build_leg_dict(s1, s1_shares, s1_price if not np.isnan(s1_price) else None)
            s2_leg = build_leg_dict(s2, s2_shares, s2_price if not np.isnan(s2_price) else None)
        else:
            # short pair: sell s1, buy s2
            s1_leg = build_leg_dict(s1, s1_shares, s1_price if not np.isnan(s1_price) else None)
            s2_leg = build_leg_dict(s2, s2_shares, s2_price if not np.isnan(s2_price) else None)
        return {'leg_s1': s1_leg, 'leg_s2': s2_leg}

    # CLOSE (stop loss)
    if stop_triggered and (in_long or in_short) and inv_direction:
        d = {**base, 'action': 'CLOSE_STOP', 'direction': inv_direction,
             'exit_threshold': ext,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'days_held': days_held,
             'note': f'Stop loss — close {inv_direction}'}
        d.update(_legs(inv_direction))
        return d

    # CLOSE (normal exit)
    if not in_long and not in_short and inv_direction:
        d = {**base, 'action': 'CLOSE', 'direction': inv_direction,
             'exit_threshold': ext,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'days_held': days_held,
             'note': f'signal={sv} passed exit threshold {ext} — close {inv_direction}'}
        d.update(_legs(inv_direction))
        return d

    # OPEN LONG
    if in_long and not inv_direction:
        d = {**base, 'action': 'OPEN_LONG', 'direction': 'long',
             'entry_threshold': et,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'note': f'signal={sv} triggered long entry (threshold ±{et})'}
        d.update(_legs('long'))
        return d

    # OPEN SHORT
    if in_short and not inv_direction:
        d = {**base, 'action': 'OPEN_SHORT', 'direction': 'short',
             'entry_threshold': et,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'note': f'signal={sv} triggered short entry (threshold ±{et})'}
        d.update(_legs('short'))
        return d

    # HOLD
    if (in_long or in_short) and inv_direction:
        dir_label = 'long' if in_long else 'short'
        d = {**base, 'action': 'HOLD', 'direction': dir_label,
             'entry_threshold': et, 'exit_threshold': ext,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'days_held': days_held,
             'note': f'Holding {dir_label}, signal={sv}, exit at {ext}'}
        d.update(_legs(dir_label))
        return d

    # FLAT
    return {**base, 'action': 'FLAT',
            'entry_threshold': et,
            'note': f'No position, signal={sv} within ±{et} — wait'}


def _r(val, decimals=3):
    """Safe round that handles NaN/None."""
    try:
        v = float(val)
        if np.isnan(v) or np.isinf(v):
            return None
        return round(v, decimals)
    except (TypeError, ValueError):
        return None


def extract_signals(context, pair_configs, signal_ts, inventory,
                    prices_today, strategy, scale_factor: float = 1.0) -> list:
    signals = []
    for s1, s2, _ in pair_configs:
        pair_key = f"{s1}/{s2}"
        today_rv = _find_today_rv(context, pair_key, signal_ts)
        if today_rv is None:
            signals.append({
                'pair': pair_key, 'action': 'NO_DATA',
                'note': 'No recorded_vars for this date',
            })
            continue
        sig = _build_signal(pair_key, s1, s2, today_rv, inventory, context,
                            prices_today, strategy, scale_factor)
        signals.append(sig)
    return signals


# ── Inventory update ───────────────────────────────────────────────────────────

def update_inventory_from_signals(
    inventory: dict,
    signals: list,
    signal_date: str,
    strategy: str = '',
    pair_configs: list | None = None,
) -> dict:
    """
    Update inventory from today's signals.
    strategy + pair_configs are used to store param_set on open,
    so that monitor_existing_positions() can later reconstruct the run.
    """
    inv = deepcopy(inventory)
    inv['as_of'] = signal_date
    if 'pairs' not in inv:
        inv['pairs'] = {}

    # Build a lookup: pair_key -> param_set_name (from pair_configs)
    param_set_lookup = {}
    if pair_configs:
        for s1, s2, ps in pair_configs:
            param_set_lookup[f'{s1}/{s2}'] = ps

    for sig in signals:
        pair   = sig['pair']
        action = sig['action']

        if action in ('OPEN_LONG', 'OPEN_SHORT'):
            inv['pairs'][pair] = {
                'strategy':      strategy,
                'param_set':     param_set_lookup.get(pair, 'default'),
                'direction':     sig['direction'],
                's1_shares':     sig.get('s1_shares', 0),
                's2_shares':     sig.get('s2_shares', 0),
                'open_date':     signal_date,
                'open_s1_price': sig.get('s1_price'),
                'open_s2_price': sig.get('s2_price'),
                'days_held':     0,
            }

        elif action in ('CLOSE', 'CLOSE_STOP'):
            inv['pairs'][pair] = {'direction': None}

        elif action == 'HOLD':
            if pair in inv['pairs'] and inv['pairs'][pair].get('direction'):
                inv['pairs'][pair]['days_held'] = inv['pairs'][pair].get('days_held', 0) + 1
                inv['pairs'][pair]['s1_shares'] = sig.get('s1_shares',
                                                           inv['pairs'][pair].get('s1_shares', 0))
                inv['pairs'][pair]['s2_shares'] = sig.get('s2_shares',
                                                           inv['pairs'][pair].get('s2_shares', 0))
    return inv


# ── Simulation runner ──────────────────────────────────────────────────────────

def _run_simulation(strategy, pair_configs, signal_date, inventory):
    """Run simulation up to signal_date and return context."""
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

    pairs       = [[s1, s2] for s1, s2, _ in pair_configs]
    pair_params = {}
    for s1, s2, ps_name in pair_configs:
        params_dict, _ = Runs._resolve_param_set(ps_name, f'{s1}/{s2}')
        pair_params[f'{s1}/{s2}'] = params_dict

    default_params, _ = Runs._resolve_param_set('default', 'fallback')
    all_symbols = sorted(set(sym for s1, s2, _ in pair_configs for sym in (s1, s2)))
    end_date_str = signal_date.strftime('%Y-%m-%d')

    log.info(f"[{strategy.upper()}] Loading data {DATA_START}→{end_date_str} ({len(all_symbols)} symbols)...")
    historical_data = PortfolioRun.load_historical_data(DATA_START, end_date_str, all_symbols)

    log.info(f"[{strategy.upper()}] Running simulation (warmup + signal day)...")
    context = SimpleNamespace()
    context.data = None
    initialize(context, pairs=pairs, params=default_params, pair_params=pair_params)

    signal_ts = pd.Timestamp(signal_date)
    # Use the last available data date as the actual signal timestamp
    # (handles weekends and days where market data hasn't arrived yet)
    last_data_ts = historical_data.index[-1]
    effective_signal_ts = last_data_ts
    if effective_signal_ts.date() < signal_ts.date():
        log.info(f"[{strategy.upper()}] signal_date={signal_ts.date()} has no data; "
                 f"using last available: {effective_signal_ts.date()}")

    inventory_injected = False

    for date_ts in historical_data.index:
        context.portfolio.current_date = date_ts
        context.portfolio.processed_dates.append(date_ts)
        current_data  = CustomData(historical_data.loc[:date_ts])
        context.warmup_mode = date_ts < effective_signal_ts

        if not inventory_injected and date_ts >= effective_signal_ts:
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

    return context, effective_signal_ts, all_symbols


# ── Existing-position monitor (isolated from new-signal logic) ─────────────────

def _run_position_monitor(
    pair_key: str,
    strategy: str,
    param_set: str,
    inv_pair: dict,
    signal_date: date,
    scale_factor: float,
) -> dict:
    """
    Run a dedicated simulation for ONE already-open pair to check today's status.

    Completely isolated from the new-signal selection path.
    Uses only 150 trading-day warmup window (fast), not full 2-year history.

    Returns a signal dict with action in:
        HOLD / CLOSE / CLOSE_STOP / NO_DATA / ERROR
    """
    s1, s2 = pair_key.split('/')
    direction = inv_pair.get('direction')
    open_date_str = inv_pair.get('open_date', '')

    if strategy == 'mrpt':
        import PortfolioMRPTRun as PortfolioRun
        import PortfolioMRPTStrategyRuns as Runs
        from PortfolioMRPTRun import initialize, my_handle_data, CustomData
    else:
        import PortfolioMTFSRun as PortfolioRun
        import PortfolioMTFSStrategyRuns as Runs
        from PortfolioMTFSRun import initialize, my_handle_data, CustomData

    # ── Resolve params ────────────────────────────────────────────────────
    try:
        params_dict, _ = Runs._resolve_param_set(param_set, pair_key)
    except Exception as e:
        log.warning(f"[monitor] {pair_key}: unknown param_set '{param_set}', using default: {e}")
        params_dict, _ = Runs._resolve_param_set('default', pair_key)

    # ── Data window: 150 trading days before open_date for warmup ─────────
    try:
        open_ts = pd.Timestamp(open_date_str) if open_date_str else pd.Timestamp(signal_date)
    except Exception:
        open_ts = pd.Timestamp(signal_date)
    # 150 bdays of warmup before open_date + data through today
    warmup_start = open_ts - pd.offsets.BDay(160)
    data_start   = warmup_start.strftime('%Y-%m-%d')
    data_end     = signal_date.strftime('%Y-%m-%d')

    log.info(f"[monitor] {pair_key} ({strategy}/{param_set}) "
             f"data {data_start}→{data_end} | open={open_date_str} dir={direction}")

    try:
        historical_data = PortfolioRun.load_historical_data(data_start, data_end, [s1, s2])
    except Exception as e:
        log.error(f"[monitor] {pair_key}: data load failed: {e}")
        return {'pair': pair_key, 'action': 'NO_DATA', 'monitored': True,
                'note': f'Data load failed: {e}'}

    # ── Build single-pair context ──────────────────────────────────────────
    pairs = [[s1, s2]]
    pair_params = {pair_key: params_dict}
    context = SimpleNamespace()
    context.data = None
    initialize(context, pairs=pairs, params=params_dict, pair_params=pair_params)

    # Use last available date (handles weekends)
    last_data_ts = historical_data.index[-1]
    effective_ts = last_data_ts
    if effective_ts.date() < signal_date:
        log.info(f"[monitor] {pair_key}: no data for {signal_date}; using {effective_ts.date()}")

    # open_date as Timestamp for injection comparison
    try:
        open_ts_data = pd.Timestamp(open_date_str) if open_date_str else effective_ts
    except Exception:
        open_ts_data = effective_ts

    inventory_injected = False

    for date_ts in historical_data.index:
        context.portfolio.current_date = date_ts
        context.portfolio.processed_dates.append(date_ts)
        current_data = CustomData(historical_data.loc[:date_ts])
        context.warmup_mode = date_ts < effective_ts

        # Inject position just before the signal day runs
        if not inventory_injected and date_ts >= effective_ts:
            # Build a minimal inventory dict for _inject_inventory_into_context
            mini_inv = {'pairs': {pair_key: inv_pair}}
            _inject_inventory_into_context(context, mini_inv, date_ts)

            # ── Fix price-level stop: set using open prices as approx spread ──
            # hedge ratio at this point from hedge_history if available
            hh = context.portfolio.hedge_history.get(pair_key, [])
            hedge_approx = hh[-1][1] if hh else 1.0
            open_p1 = inv_pair.get('open_s1_price') or 0
            open_p2 = inv_pair.get('open_s2_price') or 0
            open_spread_approx = open_p1 - hedge_approx * open_p2
            if open_spread_approx != 0:
                if direction == 'long':
                    context.execution.price_level_stop_loss[pair_key] = open_spread_approx * 0.8
                else:
                    context.execution.price_level_stop_loss[pair_key] = open_spread_approx * 1.5
                log.debug(f"[monitor] {pair_key}: price_level_stop={context.execution.price_level_stop_loss[pair_key]:.4f}")

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

    # ── Extract signal from recorded_vars ─────────────────────────────────
    today_rv = _find_today_rv(context, pair_key, effective_ts)
    if today_rv is None:
        return {'pair': pair_key, 'action': 'NO_DATA', 'monitored': True,
                'note': 'No recorded_vars after monitor run'}

    # Today's price
    prices_today = {}
    for sym in (s1, s2):
        ph = context.portfolio.price_history.get(sym)
        if ph:
            prices_today[sym] = ph[-1][1]

    sig = _build_signal(pair_key, s1, s2, today_rv, {'pairs': {pair_key: inv_pair}},
                        context, prices_today, strategy, scale_factor=scale_factor)

    # Tag as coming from position monitor (not new-signal selection)
    sig['monitored']  = True
    sig['open_date']  = open_date_str
    sig['param_set']  = param_set

    # Unrealized PnL calculation
    open_p1 = inv_pair.get('open_s1_price')
    open_p2 = inv_pair.get('open_s2_price')
    p1_now  = prices_today.get(s1)
    p2_now  = prices_today.get(s2)
    s1s     = inv_pair.get('s1_shares', 0)
    s2s     = inv_pair.get('s2_shares', 0)
    if open_p1 and open_p2 and p1_now and p2_now:
        if direction == 'long':
            # long: bought s1, sold s2
            upnl = (p1_now - open_p1) * abs(s1s) - (p2_now - open_p2) * abs(s2s)
        else:
            # short: sold s1, bought s2
            upnl = (open_p1 - p1_now) * abs(s1s) + (p2_now - open_p2) * abs(s2s)
        sig['unrealized_pnl'] = round(upnl * scale_factor, 2)
        sig['unrealized_pnl_pct'] = round(
            upnl / (open_p1 * abs(s1s) + open_p2 * abs(s2s)) * 100, 3) if (
            open_p1 * abs(s1s) + open_p2 * abs(s2s)) > 0 else None

    log.info(f"[monitor] {pair_key}: action={sig['action']}  "
             f"days_held={inv_pair.get('days_held',0)}  "
             f"upnl={sig.get('unrealized_pnl','n/a')}")
    return sig


def monitor_existing_positions(
    signal_date: date,
    dry_run: bool = False,
    mrpt_capital: float | None = None,
    mtfs_capital: float | None = None,
) -> dict:
    """
    Monitor all open positions from both inventories.
    Completely isolated from new-signal selection.

    Returns:
        {
          'mrpt': [list of monitor signals],
          'mtfs': [list of monitor signals],
          'has_positions': bool,
        }
    """
    result = {'mrpt': [], 'mtfs': [], 'has_positions': False}

    for strategy in ('mrpt', 'mtfs'):
        inventory = load_inventory(strategy)
        pairs_inv = inventory.get('pairs', {})
        sim_capital = float(inventory.get('capital', BACKTEST_BASE_CAPITAL))

        # Actual capital for scaling
        if strategy == 'mrpt':
            actual_cap = mrpt_capital or sim_capital
        else:
            actual_cap = mtfs_capital or sim_capital
        scale_factor = compute_scale_factor(actual_cap, sim_capital)

        for pair_key, inv_pair in pairs_inv.items():
            direction = inv_pair.get('direction')
            if not direction:
                continue  # already closed / flat

            # Must have strategy + param_set (stored since this fix)
            inv_strategy = inv_pair.get('strategy', strategy)
            inv_param_set = inv_pair.get('param_set', 'default')

            result['has_positions'] = True
            try:
                sig = _run_position_monitor(
                    pair_key=pair_key,
                    strategy=inv_strategy,
                    param_set=inv_param_set,
                    inv_pair=inv_pair,
                    signal_date=signal_date,
                    scale_factor=scale_factor,
                )
            except Exception as e:
                log.error(f"[monitor] {pair_key}: monitor failed: {e}", exc_info=True)
                sig = {'pair': pair_key, 'action': 'ERROR', 'monitored': True,
                       'note': str(e)}

            result[strategy].append(sig)

        # Apply monitor signals to inventory (update HOLD days_held, remove CLOSE)
        if not dry_run and result[strategy]:
            updated_inv = update_inventory_from_signals(
                inventory, result[strategy], signal_date.strftime('%Y-%m-%d'),
                strategy=strategy)
            save_inventory(updated_inv, strategy)
            log.info(f"[monitor] inventory_{strategy}.json updated after position monitoring")

    return result


# ── Regime detection ───────────────────────────────────────────────────────────

def _run_regime_detection(fred_key: str | None = None,
                          min_weight: float = 0.20) -> dict:
    """
    Run RegimeDetector and return result dict.
    Falls back to neutral (50/50) if detection fails.
    """
    try:
        from RegimeDetector import RegimeDetector

        # Auto-find latest OOS equity curves
        def _latest(pattern):
            files = glob.glob(pattern)
            return sorted(files)[-1] if files else None

        mrpt_curve = _latest(os.path.join(BASE_DIR,
                             'historical_runs/walk_forward/oos_equity_curve_*.csv'))
        mtfs_curve = _latest(os.path.join(BASE_DIR,
                             'historical_runs/walk_forward_mtfs/oos_equity_curve_*.csv'))

        rd = RegimeDetector(
            fred_api_key=fred_key,
            min_weight=min_weight,
            mrpt_oos_curve=mrpt_curve,
            mtfs_oos_curve=mtfs_curve,
        )
        return rd.detect()
    except Exception as e:
        log.warning(f"Regime detection failed ({e}), using neutral 50/50")
        return {
            'regime_score':  50.0,
            'regime_label':  'neutral_fallback',
            'mrpt_weight':   0.5,
            'mtfs_weight':   0.5,
            'weight_rationale': f'Fallback (error: {e})',
            'indicators':    {},
            'component_scores': {},
        }


# ── Print summary ──────────────────────────────────────────────────────────────

def _print_signals(signals, signal_date, strategy, dry_run,
                   capital: float | None = None, scale_factor: float = 1.0,
                   regime: dict | None = None):
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
    print(f"{'='*72}")
    strat_label = strategy.upper()
    dry_tag = '  [DRY RUN]' if dry_run else ''
    print(f"  {strat_label} Daily Signal  —  {signal_date}{dry_tag}")
    if capital:
        print(f"  Capital: ${capital:,.0f}  (scale={scale_factor:.4f}× backtest)")
    if regime:
        score = regime.get('regime_score', '?')
        label = regime.get('regime_label', '?')
        mw    = regime.get('mrpt_weight', '?')
        tw    = regime.get('mtfs_weight', '?')
        if isinstance(mw, float):
            print(f"  Regime: {label}  score={score}  MRPT={mw:.0%}  MTFS={tw:.0%}")
        else:
            print(f"  Regime: {label}  score={score}")
    print(f"{'='*72}")

    grouped = {
        'OPEN':  [s for s in signals if s['action'] in ('OPEN_LONG', 'OPEN_SHORT')],
        'CLOSE': [s for s in signals if s['action'] in ('CLOSE', 'CLOSE_STOP')],
        'HOLD':  [s for s in signals if s['action'] == 'HOLD'],
        'FLAT':  [s for s in signals if s['action'] == 'FLAT'],
        'OTHER': [s for s in signals if s['action'] in ('BLACKOUT', 'NO_DATA')],
    }

    sig_key    = 'z_score' if strategy == 'mrpt' else 'momentum_spread'
    sig_prefix = 'z'       if strategy == 'mrpt' else 'ms'

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
                s1, s2   = pair.split('/')
                leg1     = sig.get('leg_s1', {})
                leg2     = sig.get('leg_s2', {})
                sh1      = leg1.get('shares', sig.get('s1_shares', 0))
                sh2      = leg2.get('shares', sig.get('s2_shares', 0))
                p1       = sig.get('s1_price', '')
                p2       = sig.get('s2_price', '')
                v1       = leg1.get('est_value')
                v2       = leg2.get('est_value')
                days     = sig.get('days_held', '')
                val_str2 = (f"${v1:,.0f}" if v1 else '') + (' / ' if v1 and v2 else '') + (f"${v2:,.0f}" if v2 else '')
                days_str = f"  {days}d held" if days else ""
                print(f"  {label}  {pair:<12}  {val_str:<10}  "
                      f"{s1} {sh1:+d}@{p1}  {s2} {sh2:+d}@{p2}  "
                      f"{val_str2}{days_str}")
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
    print(f"{'='*72}")


def _print_monitor_summary(monitor: dict, signal_date):
    """Print position monitor results to console."""
    mrpt_sigs = monitor.get('mrpt', [])
    mtfs_sigs = monitor.get('mtfs', [])
    all_sigs  = mrpt_sigs + mtfs_sigs
    if not all_sigs:
        return

    print()
    print(f"{'='*72}")
    print(f"  POSITION MONITOR  —  {signal_date}  ({len(all_sigs)} open positions)")
    print(f"{'='*72}")
    ACTION_LABELS = {
        'HOLD':       '─ HOLD      ',
        'CLOSE':      '✕ CLOSE     ',
        'CLOSE_STOP': '✕ STOP LOSS ',
        'NO_DATA':    '? NO_DATA   ',
        'ERROR':      '! ERROR     ',
    }
    for strat, sigs in (('MRPT', mrpt_sigs), ('MTFS', mtfs_sigs)):
        if not sigs:
            continue
        print(f"\n  [{strat}]")
        for sig in sigs:
            label   = ACTION_LABELS.get(sig['action'], f"  {sig['action']}")
            pair    = sig['pair']
            days    = sig.get('days_held', sig.get('open_date', ''))
            upnl    = sig.get('unrealized_pnl')
            upnl_str = f"  uPnL=${upnl:+,.0f}" if upnl is not None else ''
            sv_key  = 'z_score' if strat == 'MRPT' else 'momentum_spread'
            sv      = sig.get(sv_key)
            sv_str  = f"  {'z' if strat=='MRPT' else 'ms'}={sv:+.2f}" if sv is not None else ''
            print(f"    {label}  {pair:<12}  days={sig.get('days_held',0)}{sv_str}{upnl_str}"
                  f"  [{sig.get('param_set','')}]")
            if sig['action'] in ('CLOSE', 'CLOSE_STOP', 'ERROR', 'NO_DATA'):
                print(f"             ↳ {sig.get('note','')}")
    print(f"{'='*72}")


def _print_combined_summary(mrpt_out: dict, mtfs_out: dict, total_capital: float,
                             regime: dict | None, signal_date):
    """Print combined portfolio summary for --strategy both."""
    mrpt_w = (regime or {}).get('mrpt_weight', 0.5)
    mtfs_w = (regime or {}).get('mtfs_weight', 0.5)
    mrpt_cap = total_capital * mrpt_w
    mtfs_cap = total_capital * mtfs_w

    mrpt_sigs = mrpt_out.get('signals', [])
    mtfs_sigs = mtfs_out.get('signals', [])

    # Count opens
    mrpt_opens = [s for s in mrpt_sigs if s['action'] in ('OPEN_LONG', 'OPEN_SHORT')]
    mtfs_opens = [s for s in mtfs_sigs if s['action'] in ('OPEN_LONG', 'OPEN_SHORT')]
    mrpt_closes = [s for s in mrpt_sigs if s['action'] in ('CLOSE', 'CLOSE_STOP')]
    mtfs_closes = [s for s in mtfs_sigs if s['action'] in ('CLOSE', 'CLOSE_STOP')]

    print()
    print(f"{'='*72}")
    print(f"  COMBINED PORTFOLIO  —  {signal_date}")
    print(f"{'='*72}")
    print(f"  Total capital:    ${total_capital:>12,.0f}")
    print(f"  Regime:           {(regime or {}).get('regime_label','skip-regime')}  score={(regime or {}).get('regime_score','N/A')}")
    print()
    print(f"  {'Strategy':<10} {'Weight':>7} {'Capital':>14}  {'Opens':>6}  {'Closes':>7}")
    print(f"  {'-'*52}")
    print(f"  {'MRPT':<10} {mrpt_w:>6.0%} ${mrpt_cap:>13,.0f}  {len(mrpt_opens):>6}  {len(mrpt_closes):>7}")
    print(f"  {'MTFS':<10} {mtfs_w:>6.0%} ${mtfs_cap:>13,.0f}  {len(mtfs_opens):>6}  {len(mtfs_closes):>7}")
    print()

    # Scaling summary
    mrpt_scale = mrpt_out.get('scale_factor', 1.0)
    mtfs_scale = mtfs_out.get('scale_factor', 1.0)
    print(f"  Scaling vs $1M backtest:")
    print(f"    MRPT: ${mrpt_cap:,.0f} / $1,000,000 = {mrpt_scale:.4f}×  "
          f"(PnL/DD multiply by {mrpt_scale:.4f})")
    print(f"    MTFS: ${mtfs_cap:,.0f} / $1,000,000 = {mtfs_scale:.4f}×  "
          f"(PnL/DD multiply by {mtfs_scale:.4f})")
    print()

    # Regime rationale (compact)
    rationale = (regime or {}).get('weight_rationale', '')
    first_lines = rationale.split('\n')[:3]
    for l in first_lines:
        print(f"  {l}")
    print(f"{'='*72}")


# ── Main runner ────────────────────────────────────────────────────────────────

def run_daily_signal(
    strategy: str,
    signal_date: date,
    dry_run: bool = False,
    capital: float | None = None,
    total_capital: float | None = None,
    mrpt_weight: float | None = None,
    fred_key: str | None = None,
    min_regime_weight: float = 0.20,
    skip_regime: bool = False,
) -> dict:
    """
    Core runner. Returns output dict(s).

    strategy: 'mrpt' | 'mtfs' | 'both'
    capital:  explicit capital for single strategy mode
    total_capital: used in 'both' mode (split by regime weights)
    mrpt_weight: override regime detection (0-1)
    """
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    # ── Regime detection ──────────────────────────────────────────────────
    regime = None
    if strategy == 'both' or (strategy in ('mrpt', 'mtfs') and not skip_regime):
        if mrpt_weight is not None:
            regime = {
                'regime_score':   mrpt_weight * 100 if strategy == 'both' else 50.0,
                'regime_label':   'manual_override',
                'mrpt_weight':    mrpt_weight,
                'mtfs_weight':    round(1.0 - mrpt_weight, 3),
                'weight_rationale': f'Manual override: MRPT={mrpt_weight:.0%}',
                'indicators':    {},
                'component_scores': {},
            }
        elif not skip_regime:
            log.info("Running regime detection...")
            regime = _run_regime_detection(
                fred_key=fred_key or os.getenv('FRED_API_KEY', ''),
                min_weight=min_regime_weight,
            )

    # ── BOTH mode ─────────────────────────────────────────────────────────
    if strategy == 'both':
        T = total_capital or 1_000_000.0
        mw = regime['mrpt_weight'] if regime else 0.5
        tw = regime['mtfs_weight'] if regime else 0.5
        mrpt_cap = T * mw
        mtfs_cap = T * tw

        # ── Step 1: Monitor existing positions (isolated, runs first) ──────
        log.info("── Monitoring existing positions ──")
        monitor = monitor_existing_positions(
            signal_date=signal_date,
            dry_run=dry_run,
            mrpt_capital=mrpt_cap,
            mtfs_capital=mtfs_cap,
        )
        _print_monitor_summary(monitor, signal_date)

        # ── Step 2: New signal selection (completely independent) ───────────
        mrpt_out = _run_single(
            strategy='mrpt',
            signal_date=signal_date,
            dry_run=dry_run,
            capital=mrpt_cap,
            regime=regime,
        )
        mtfs_out = _run_single(
            strategy='mtfs',
            signal_date=signal_date,
            dry_run=dry_run,
            capital=mtfs_cap,
            regime=regime,
        )

        _print_combined_summary(mrpt_out, mtfs_out, T, regime, signal_date)

        # Save combined output
        combined_out = {
            'mode':              'combined',
            'signal_date':       signal_date.strftime('%Y-%m-%d'),
            'generated_at':      datetime.now().isoformat(timespec='seconds'),
            'total_capital':     T,
            'regime':            _clean_for_json(regime),
            'position_monitor':  _clean_for_json(monitor),
            'mrpt':              mrpt_out,
            'mtfs':              mtfs_out,
        }
        sig_path = os.path.join(SIGNALS_DIR,
                                f"combined_signals_{signal_date.strftime('%Y%m%d')}.json")
        with open(sig_path, 'w') as f:
            json.dump(combined_out, f, indent=2, default=str)
        log.info(f"Combined signals saved → {sig_path}")

        # ── 生成详细报告 ───────────────────────────────────────────────────
        os.makedirs(REPORTS_DIR, exist_ok=True)
        date_str = signal_date.strftime('%Y%m%d')
        report = build_full_report_json(
            strategy='both',
            signal_date=signal_date,
            total_capital=T,
            regime=regime,
            mrpt_out=mrpt_out,
            mtfs_out=mtfs_out,
            monitor=monitor,
        )
        rpt_json_path = os.path.join(REPORTS_DIR, f'daily_report_{date_str}.json')
        with open(rpt_json_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"Report JSON saved → {rpt_json_path}")

        rpt_txt_path = os.path.join(REPORTS_DIR, f'daily_report_{date_str}.txt')
        write_report_txt(report, rpt_txt_path)
        print(f"\n  详细报告: {rpt_json_path}")
        print(f"  文字报告: {rpt_txt_path}")

        return combined_out

    # ── Single strategy mode ──────────────────────────────────────────────
    if capital is None:
        inv = load_inventory(strategy)
        capital = float(inv.get('capital', 500_000))

    # Monitor existing positions for this single strategy
    log.info("── Monitoring existing positions ──")
    mon_cap = capital
    monitor = monitor_existing_positions(
        signal_date=signal_date,
        dry_run=dry_run,
        mrpt_capital=mon_cap if strategy == 'mrpt' else None,
        mtfs_capital=mon_cap if strategy == 'mtfs' else None,
    )
    _print_monitor_summary(monitor, signal_date)

    single_out = _run_single(
        strategy=strategy,
        signal_date=signal_date,
        dry_run=dry_run,
        capital=capital,
        regime=regime,
    )

    # ── 生成详细报告 ──────────────────────────────────────────────────────
    os.makedirs(REPORTS_DIR, exist_ok=True)
    date_str = signal_date.strftime('%Y%m%d')
    report = build_full_report_json(
        strategy=strategy,
        signal_date=signal_date,
        total_capital=capital,
        regime=regime,
        single_out=single_out,
        monitor=monitor,
    )
    rpt_json_path = os.path.join(REPORTS_DIR, f'daily_report_{strategy}_{date_str}.json')
    with open(rpt_json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Report JSON saved → {rpt_json_path}")

    rpt_txt_path = os.path.join(REPORTS_DIR, f'daily_report_{strategy}_{date_str}.txt')
    write_report_txt(report, rpt_txt_path)
    print(f"\n  详细报告: {rpt_json_path}")
    print(f"  文字报告: {rpt_txt_path}")

    return single_out


def _run_single(strategy: str, signal_date: date, dry_run: bool,
                capital: float, regime: dict | None) -> dict:
    """Run one strategy, return output dict."""
    pair_configs = get_pair_configs_mrpt() if strategy == 'mrpt' else get_pair_configs_mtfs()
    inventory    = load_inventory(strategy)
    sim_capital  = float(inventory.get('capital', BACKTEST_BASE_CAPITAL))
    scale_factor = compute_scale_factor(capital, sim_capital)

    log.info(f"[{strategy.upper()}] capital=${capital:,.0f}  sim_capital=${sim_capital:,.0f}  "
             f"scale={scale_factor:.4f}")

    context, signal_ts, all_symbols = _run_simulation(
        strategy, pair_configs, signal_date, inventory)

    # Extract today's prices
    prices_today = {}
    for sym in all_symbols:
        ph = context.portfolio.price_history.get(sym)
        if ph:
            prices_today[sym] = ph[-1][1]

    signals = extract_signals(
        context, pair_configs, signal_ts, inventory,
        prices_today, strategy, scale_factor=scale_factor)

    _print_signals(signals, signal_date, strategy, dry_run,
                   capital=capital, scale_factor=scale_factor, regime=regime)

    end_date_str = signal_date.strftime('%Y-%m-%d')
    out = {
        'strategy':     strategy,
        'signal_date':  end_date_str,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'dry_run':      dry_run,
        'capital':      capital,
        'sim_capital':  sim_capital,
        'scale_factor': round(scale_factor, 6),
        'regime':       _clean_for_json(regime) if regime else None,
        'signals':      signals,
    }

    sig_path = os.path.join(SIGNALS_DIR,
                            f"{strategy}_signals_{signal_date.strftime('%Y%m%d')}.json")
    with open(sig_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    log.info(f"[{strategy.upper()}] Signals saved → {sig_path}")

    if not dry_run:
        updated_inv = update_inventory_from_signals(
            inventory, signals, end_date_str,
            strategy=strategy, pair_configs=pair_configs)
        save_inventory(updated_inv, strategy)
        print(f"\n  inventory_{strategy}.json updated for {end_date_str}")
    else:
        print(f"\n  [DRY RUN] inventory_{strategy}.json NOT updated")

    return out


def _clean_for_json(obj):
    """Remove NaN/inf and non-serializable types for JSON output."""
    import math
    if obj is None:
        return None
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_for_json(v) for v in obj]
    return obj


# ── Report builders ────────────────────────────────────────────────────────────

# OOS performance reference (from 6-window walk-forward, updated 2026-03-08)
_OOS_PERF_MRPT = {
    'ALGN/UAL':  {'oos_sharpe': 1.937, 'oos_pnl': 17192,  'oos_maxdd': -3895,  'pos_windows': '3/6', 'tier': 1},
    'DG/MOS':    {'oos_sharpe': 1.247, 'oos_pnl': 18901,  'oos_maxdd': -17260, 'pos_windows': '4/6', 'tier': 1},
    'ACGL/UHS':  {'oos_sharpe': 0.668, 'oos_pnl': 3013,   'oos_maxdd': -4631,  'pos_windows': '3/6', 'tier': 2},
    'TW/CME':    {'oos_sharpe': 0.528, 'oos_pnl': 1814,   'oos_maxdd': -3955,  'pos_windows': '1/6', 'tier': 2},
    'ARES/CG':   {'oos_sharpe': 0.379, 'oos_pnl': 2885,   'oos_maxdd': -9230,  'pos_windows': '3/6', 'tier': 2},
    'D/MCHP':    {'oos_sharpe': 0.136, 'oos_pnl': 388,    'oos_maxdd': -2275,  'pos_windows': '3/6', 'tier': 3},
    'CART/DASH': {'oos_sharpe': 0.036, 'oos_pnl': 454,    'oos_maxdd': -21585, 'pos_windows': '3/6', 'tier': 3},
    'CL/USO':    {'oos_sharpe': -0.055,'oos_pnl': -273,   'oos_maxdd': -4153,  'pos_windows': '3/6', 'tier': 4},
    'YUM/MCD':   {'oos_sharpe': -0.125,'oos_pnl': -478,   'oos_maxdd': -4020,  'pos_windows': '3/6', 'tier': 4},
    'LYFT/UBER': {'oos_sharpe': -0.252,'oos_pnl': -1626,  'oos_maxdd': -8966,  'pos_windows': '3/6', 'tier': 4},
    # Excluded pairs (for transparency in report)
    'GS/ALLY':   {'oos_sharpe': -1.099,'oos_pnl': -5958,  'oos_maxdd': -7436,  'pos_windows': '1/6', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K, PosW=1/6 unstable'},
    'ESS/EXPD':  {'oos_sharpe': -0.921,'oos_pnl': -11769, 'oos_maxdd': -23380, 'pos_windows': '4/6', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K'},
    'MSCI/LII':  {'oos_sharpe': -0.474,'oos_pnl': -4118,  'oos_maxdd': -15184, 'pos_windows': '2/6', 'tier': 0, 'excluded': True, 'reason': 'PnL<-$3K'},
    'AMG/BEN':   {'oos_sharpe': 0.301, 'oos_pnl': 1258,   'oos_maxdd': -6372,  'pos_windows': '1/6', 'tier': 0, 'excluded': True, 'reason': 'PosW=1/6 unstable'},
    'AAPL/META': {'oos_sharpe': 0.000, 'oos_pnl': 0,      'oos_maxdd': 0,      'pos_windows': '0/6', 'tier': 0, 'excluded': True, 'reason': 'No trades in OOS'},
}

_OOS_PERF_MTFS = {
    'TW/CME':    {'oos_sharpe': 1.487, 'oos_pnl': 216,    'oos_maxdd': 0,      'pos_windows': '0/5', 'tier': 1},
    'DG/MOS':    {'oos_sharpe': 0.989, 'oos_pnl': 3784,   'oos_maxdd': -3463,  'pos_windows': '0/5', 'tier': 1},
    'AMG/BEN':   {'oos_sharpe': 0.532, 'oos_pnl': 3894,   'oos_maxdd': -8205,  'pos_windows': '3/5', 'tier': 2},
    'LYFT/UBER': {'oos_sharpe': 0.463, 'oos_pnl': 3849,   'oos_maxdd': -13672, 'pos_windows': '3/5', 'tier': 2},
    'ESS/EXPD':  {'oos_sharpe': 0.448, 'oos_pnl': 3372,   'oos_maxdd': -6515,  'pos_windows': '2/5', 'tier': 2},
    # Excluded pairs
    'CL/USO':    {'oos_sharpe': -2.801,'oos_pnl': -4063,  'oos_maxdd': -4063,  'pos_windows': '0/5', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K'},
    'ALGN/UAL':  {'oos_sharpe': -2.589,'oos_pnl': -17906, 'oos_maxdd': -20683, 'pos_windows': '0/5', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K, PosW=0/5'},
    'GS/ALLY':   {'oos_sharpe': -2.411,'oos_pnl': -14843, 'oos_maxdd': -15508, 'pos_windows': '0/5', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K, PosW=0/5'},
    'CART/DASH': {'oos_sharpe': -1.890,'oos_pnl': -10828, 'oos_maxdd': -12558, 'pos_windows': '0/5', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K, PosW=0/5'},
    'ACGL/UHS':  {'oos_sharpe': -1.120,'oos_pnl': -1169,  'oos_maxdd': -1478,  'pos_windows': '0/3', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5'},
    'D/MCHP':    {'oos_sharpe': -0.730,'oos_pnl': -5576,  'oos_maxdd': -12987, 'pos_windows': '0/5', 'tier': 0, 'excluded': True, 'reason': 'Sharpe<-0.5, PnL<-$3K, PosW=0/5'},
    'AAPL/META': {'oos_sharpe': -0.030,'oos_pnl': -108,   'oos_maxdd': -3922,  'pos_windows': '1/5', 'tier': 0, 'excluded': True, 'reason': 'PosW=1/5 unstable'},
    'ARES/CG':   {'oos_sharpe': 1.497, 'oos_pnl': 9607,   'oos_maxdd': -6866,  'pos_windows': '1/5', 'tier': 0, 'excluded': True, 'reason': 'PosW=1/5 unstable (1 large win W1)'},
}

_REGIME_INDICATOR_LABELS = {
    'vix_level':           ('VIX',                  'Equity volatility index',             lambda v: f'{v:.1f}',        lambda v: '偏高 (>25=警戒)' if v > 25 else ('正常' if v > 15 else '极低')),
    'vix_z':               ('VIX z-score',           '相对252天历史',                         lambda v: f'{v:+.2f}',       lambda v: '历史高位' if v > 1.5 else ('偏高' if v > 0.5 else '正常')),
    'move_level':          ('MOVE指数',               'Bond vol',                            lambda v: f'{v:.1f}',        lambda v: '债券波动偏高' if v > 100 else '债券波动正常'),
    'hy_spread_level':     ('HY利差',                 'High-yield credit spread (OAS)',      lambda v: f'{v:.2f}%',       lambda v: '信用风险低' if v < 3.5 else ('警戒' if v < 5 else '信用紧张')),
    'ig_spread_level':     ('IG利差',                 'Investment-grade spread (OAS)',       lambda v: f'{v:.2f}%',       lambda v: '极低' if v < 1.0 else '正常'),
    'yield_curve_level':   ('收益率曲线(10Y-2Y)',     'Treasury yield curve spread',         lambda v: f'{v:+.2f}%',      lambda v: '倒挂(衰退信号)' if v < 0 else ('平坦' if v < 0.5 else '正常陡峭')),
    'effr_level':          ('联储基金利率',            'Effective Fed Funds Rate',            lambda v: f'{v:.2f}%',       lambda v: '降息周期' if v < 3.5 else ('高利率' if v > 4.5 else '中性')),
    'effr_1y_change':      ('EFFR年变化',              '过去1年利率变化',                       lambda v: f'{v:+.2f}%',      lambda v: '降息' if v < -0.25 else ('加息' if v > 0.25 else '持平')),
    'breakeven_10y_level': ('10Y盈亏平衡通胀',         '10yr inflation breakeven',            lambda v: f'{v:.2f}%',       lambda v: '通胀预期偏高' if v > 2.5 else '通胀预期正常'),
    'fin_stress_level':    ('圣路易斯金融压力指数',    'St. Louis Financial Stress Index',    lambda v: f'{v:.4f}',        lambda v: '金融宽松' if v < -0.3 else ('中性' if v < 0.5 else '金融压力')),
    'nfci_level':          ('国家金融状况指数(NFCI)',   'National Financial Conditions Index', lambda v: f'{v:.4f}',        lambda v: '金融宽松' if v < -0.3 else '中性'),
    'consumer_sent_level': ('密歇根消费者信心',        'U. Michigan Consumer Sentiment',      lambda v: f'{v:.1f}',        lambda v: '消费者悲观' if v < 60 else ('中性' if v < 80 else '乐观')),
    'recession_flag':      ('NBER衰退指标',            '0=非衰退, 1=衰退',                     lambda v: f'{int(v)}',       lambda v: '⚠ 衰退期' if v else '非衰退'),
    'nvda_20d':            ('NVDA 20日涨跌',           'NVIDIA 20d return (AI sentiment)',    lambda v: f'{v:+.1%}',       lambda v: 'AI强劲动量' if v > 0.15 else ('AI回调' if v < -0.1 else 'AI中性')),
    'arkk_20d':            ('ARKK 20日涨跌',           'ARK Innovation (投机情绪)',            lambda v: f'{v:+.1%}',       lambda v: '投机热情高' if v > 0.1 else ('投机降温' if v < -0.1 else '中性')),
    'soxx_20d':            ('半导体(SOXX) 20日',       'Semiconductor 20d return',            lambda v: f'{v:+.1%}',       lambda v: '科技上行' if v > 0.05 else ('科技下行' if v < -0.05 else '中性')),
    'gld_20d':             ('黄金(GLD) 20日涨跌',      '避险资产 / 地缘政治代理',               lambda v: f'{v:+.1%}',       lambda v: '地缘风险高/避险买入' if v > 0.05 else '无明显避险'),
    'uso_20d':             ('原油(USO) 20日涨跌',      '地缘政治 / 通胀代理',                  lambda v: f'{v:+.1%}',       lambda v: '供给冲击/地缘紧张' if v > 0.1 else ('油价回落' if v < -0.1 else '正常')),
    'uup_20d':             ('美元(UUP) 20日涨跌',      'Dollar index 20d return',             lambda v: f'{v:+.1%}',       lambda v: '美元走强(risk-off)' if v > 0.02 else ('美元弱(risk-on)' if v < -0.02 else '中性')),
    'spy_20d':             ('SPY 20日涨跌',            'S&P 500 20d momentum',                lambda v: f'{v:+.1%}',       lambda v: '市场上行' if v > 0.03 else ('市场下跌' if v < -0.03 else '横盘')),
    'tnx_level':           ('10Y美债收益率',           '10-year Treasury yield',              lambda v: f'{v:.2f}%',       lambda v: '收益率偏高' if v > 4.5 else '正常'),
}


def _build_strategy_report_section(strategy: str, out: dict) -> dict:
    """
    Build rich per-strategy report section including:
    - capital/scaling info
    - all active signals (open/close/hold) with full execution details
    - all flat signals with why they didn't trigger
    - excluded pairs (OOS-filtered) with OOS stats
    - OOS performance summary for selected pairs
    """
    signals    = out.get('signals', [])
    capital    = out.get('capital', 0)
    sim_cap    = out.get('sim_capital', 1_000_000)
    scale      = out.get('scale_factor', 1.0)
    sig_date   = out.get('signal_date', '')
    oos_ref    = _OOS_PERF_MRPT if strategy == 'mrpt' else _OOS_PERF_MTFS
    sig_key    = 'z_score' if strategy == 'mrpt' else 'momentum_spread'
    sig_label  = 'Z-score' if strategy == 'mrpt' else 'Momentum Spread'

    active   = [s for s in signals if s['action'] in ('OPEN_LONG', 'OPEN_SHORT', 'CLOSE', 'CLOSE_STOP', 'HOLD')]
    flat     = [s for s in signals if s['action'] == 'FLAT']
    blackout = [s for s in signals if s['action'] == 'BLACKOUT']
    no_data  = [s for s in signals if s['action'] == 'NO_DATA']
    excluded = {p: d for p, d in oos_ref.items() if d.get('excluded')}

    # Active signals: enrich with OOS stats
    active_rich = []
    for s in active:
        pair = s['pair']
        perf = oos_ref.get(pair, {})
        entry = {
            'pair':              pair,
            'action':            s['action'],
            'direction':         s.get('direction'),
            sig_key:             s.get(sig_key),
            'entry_threshold':   s.get('entry_threshold'),
            'exit_threshold':    s.get('exit_threshold'),
            'days_held':         s.get('days_held', 0),
            's1':                s.get('leg_s1', {}),
            's2':                s.get('leg_s2', {}),
            's1_price':          s.get('s1_price'),
            's2_price':          s.get('s2_price'),
            's1_shares':         s.get('s1_shares'),
            's2_shares':         s.get('s2_shares'),
            'total_notional':    (
                (s.get('leg_s1', {}).get('est_value') or 0) +
                (s.get('leg_s2', {}).get('est_value') or 0)
            ),
            'note':              s.get('note', ''),
            'oos_sharpe':        perf.get('oos_sharpe'),
            'oos_pnl_$1m':       perf.get('oos_pnl'),
            'oos_maxdd_$1m':     perf.get('oos_maxdd'),
            'oos_pos_windows':   perf.get('pos_windows'),
            'oos_tier':          perf.get('tier'),
        }
        active_rich.append(entry)

    # Flat signals: enrich with how far signal is from threshold
    flat_rich = []
    for s in flat:
        pair = s['pair']
        perf = oos_ref.get(pair, {})
        sv   = s.get(sig_key)
        et   = s.get('entry_threshold')
        gap  = None
        if sv is not None and et is not None:
            gap = round(abs(abs(sv) - et), 4)  # distance from triggering
        entry = {
            'pair':              pair,
            'action':            'FLAT',
            sig_key:             sv,
            'entry_threshold':   et,
            'distance_to_entry': gap,
            's1_price':          s.get('s1_price'),
            's2_price':          s.get('s2_price'),
            'note':              s.get('note', ''),
            'oos_sharpe':        perf.get('oos_sharpe'),
            'oos_pnl_$1m':       perf.get('oos_pnl'),
            'oos_pos_windows':   perf.get('pos_windows'),
            'oos_tier':          perf.get('tier'),
        }
        flat_rich.append(entry)
    # Sort flat by distance_to_entry ascending (closest to triggering first)
    flat_rich.sort(key=lambda x: x.get('distance_to_entry') or 999)

    # Excluded pairs section
    excluded_rich = []
    for pair, perf in sorted(excluded.items(), key=lambda x: x[1]['oos_sharpe']):
        excluded_rich.append({
            'pair':           pair,
            'oos_sharpe':     perf['oos_sharpe'],
            'oos_pnl_$1m':    perf['oos_pnl'],
            'oos_maxdd_$1m':  perf['oos_maxdd'],
            'pos_windows':    perf['pos_windows'],
            'exclusion_reason': perf['reason'],
        })

    return {
        'strategy':         strategy.upper(),
        'capital':          capital,
        'sim_capital':      sim_cap,
        'scale_factor':     scale,
        'scaling_note':     f'${capital:,.0f} / $1,000,000 backtest = {scale:.4f}× | PnL/DD × {scale:.4f}',
        'signal_date':      sig_date,
        'summary': {
            'n_open':    len([s for s in active if 'OPEN' in s['action']]),
            'n_close':   len([s for s in active if 'CLOSE' in s['action']]),
            'n_hold':    len([s for s in active if s['action'] == 'HOLD']),
            'n_flat':    len(flat),
            'n_blackout': len(blackout),
            'n_no_data': len(no_data),
            'n_excluded_pairs': len(excluded),
        },
        'active_signals':   active_rich,
        'flat_signals':     flat_rich,
        'blackout_signals': [{'pair': s['pair'], 'note': s.get('note','')} for s in blackout],
        'no_data_signals':  [{'pair': s['pair'], 'note': s.get('note','')} for s in no_data],
        'excluded_pairs':   excluded_rich,
    }


def _build_regime_report_section(regime: dict) -> dict:
    """Build enriched regime section with human-readable labels for all indicators."""
    if not regime:
        return {}

    indicators = regime.get('indicators', {})
    enriched_indicators = {}
    for key, (name, description, fmt_fn, interp_fn) in _REGIME_INDICATOR_LABELS.items():
        val = indicators.get(key)
        if val is not None:
            try:
                enriched_indicators[key] = {
                    'name':           name,
                    'description':    description,
                    'raw_value':      round(val, 6) if isinstance(val, float) else val,
                    'formatted':      fmt_fn(val),
                    'interpretation': interp_fn(val),
                }
            except Exception:
                enriched_indicators[key] = {'name': name, 'raw_value': val}

    component_scores = regime.get('component_scores', {})
    enriched_components = {}
    for cat, info in component_scores.items():
        agg = info.get('aggregate', 0.5)
        enriched_components[cat] = {
            'aggregate_score':  round(agg, 4),
            'bias':             'MTFS-favoring' if agg > 0.55 else ('MRPT-favoring' if agg < 0.45 else 'neutral'),
            'weight_in_regime': info.get('weight', 0),
            'contribution':     round(agg * info.get('weight', 0), 4),
            'sub_scores':       info.get('sub', {}),
        }

    return {
        'regime_score':        regime.get('regime_score'),
        'regime_label':        regime.get('regime_label'),
        'mrpt_weight':         regime.get('mrpt_weight'),
        'mtfs_weight':         regime.get('mtfs_weight'),
        'interpretation': (
            'MRPT主导: 低波动/均值回归环境，做空波动率有利' if (regime.get('regime_score') or 50) < 35
            else ('MTFS主导: 高波动/动量趋势环境，做多波动率有利' if (regime.get('regime_score') or 50) > 65
                  else '中性: 两策略均可运行，按历史权重分配')
        ),
        'indicators':          enriched_indicators,
        'component_scores':    enriched_components,
        'strategy_vol_ratio':  regime.get('strategy_vol', {}),
        'weight_rationale':    regime.get('weight_rationale', ''),
    }


def _build_monitor_report_section(monitor: dict) -> dict:
    """Build position monitor section for the full report."""
    if not monitor or not monitor.get('has_positions'):
        return {'has_positions': False, 'mrpt': [], 'mtfs': []}

    def _enrich(sigs):
        out = []
        for s in sigs:
            entry = {
                'pair':            s.get('pair'),
                'action':          s.get('action'),
                'direction':       s.get('direction'),
                'open_date':       s.get('open_date'),
                'days_held':       s.get('days_held', 0),
                'param_set':       s.get('param_set', ''),
                'z_score':         s.get('z_score'),
                'momentum_spread': s.get('momentum_spread'),
                'entry_threshold': s.get('entry_threshold'),
                'exit_threshold':  s.get('exit_threshold'),
                's1':              s.get('leg_s1', {}),
                's2':              s.get('leg_s2', {}),
                'unrealized_pnl':  s.get('unrealized_pnl'),
                'unrealized_pnl_pct': s.get('unrealized_pnl_pct'),
                'note':            s.get('note', ''),
            }
            out.append(entry)
        return out

    return {
        'has_positions': monitor.get('has_positions', False),
        'mrpt': _enrich(monitor.get('mrpt', [])),
        'mtfs': _enrich(monitor.get('mtfs', [])),
        'total_open':  len([s for s in monitor.get('mrpt', []) + monitor.get('mtfs', [])
                            if s.get('action') in ('HOLD',)]),
        'total_close': len([s for s in monitor.get('mrpt', []) + monitor.get('mtfs', [])
                            if 'CLOSE' in s.get('action', '')]),
    }


def build_full_report_json(
    strategy: str,
    signal_date: date,
    total_capital: float,
    regime: dict,
    mrpt_out: dict | None = None,
    mtfs_out: dict | None = None,
    single_out: dict | None = None,
    monitor: dict | None = None,
) -> dict:
    """
    Build the complete report JSON for saving as daily_report_YYYYMMDD.json.
    Covers single-strategy or combined mode.
    """
    report = {
        'report_type':        'combined' if strategy == 'both' else f'single_{strategy}',
        'signal_date':        signal_date.strftime('%Y-%m-%d'),
        'generated_at':       datetime.now().isoformat(timespec='seconds'),
        'total_capital':      total_capital,
        'regime':             _build_regime_report_section(regime),
        'position_monitor':   _build_monitor_report_section(monitor or {}),
    }

    if strategy == 'both' and mrpt_out and mtfs_out:
        mrpt_w = regime.get('mrpt_weight', 0.5) if regime else 0.5
        mtfs_w = regime.get('mtfs_weight', 0.5) if regime else 0.5
        report['portfolio'] = {
            'total_capital':     total_capital,
            'mrpt_allocation':   round(total_capital * mrpt_w, 0),
            'mtfs_allocation':   round(total_capital * mtfs_w, 0),
            'mrpt_weight':       mrpt_w,
            'mtfs_weight':       mtfs_w,
            'total_open_count':  (
                len([s for s in mrpt_out.get('signals',[]) if 'OPEN' in s.get('action','')]) +
                len([s for s in mtfs_out.get('signals',[]) if 'OPEN' in s.get('action','')])
            ),
            'total_close_count': (
                len([s for s in mrpt_out.get('signals',[]) if 'CLOSE' in s.get('action','')]) +
                len([s for s in mtfs_out.get('signals',[]) if 'CLOSE' in s.get('action','')])
            ),
        }
        report['mrpt'] = _build_strategy_report_section('mrpt', mrpt_out)
        report['mtfs'] = _build_strategy_report_section('mtfs', mtfs_out)

    elif single_out:
        report[strategy] = _build_strategy_report_section(strategy, single_out)

    return _clean_for_json(report)


def write_report_txt(report: dict, path: str):
    """
    Write a comprehensive human-readable TXT report from the report JSON.
    """
    sig_date   = report.get('signal_date', '?')
    rtype      = report.get('report_type', '?')
    total_cap  = report.get('total_capital', 0)
    regime_sec = report.get('regime', {})
    portfolio  = report.get('portfolio', {})

    lines = []
    W = 76  # line width

    def sep(char='='):   lines.append(char * W)
    def blank():         lines.append('')
    def h(title):        sep(); lines.append(f'  {title}'); sep()
    def h2(title):       sep('-'); lines.append(f'  {title}'); sep('-')
    def row(k, v, w1=32):lines.append(f'  {k:<{w1}}{v}')

    # ── Header ──────────────────────────────────────────────────────────────
    sep('=')
    lines.append(f'  双策略配对交易  每日信号报告')
    lines.append(f'  Signal Date  : {sig_date}')
    lines.append(f'  Generated    : {report.get("generated_at","?")}')
    lines.append(f'  Report Type  : {rtype}')
    lines.append(f'  Total Capital: ${total_cap:>12,.0f}')
    sep('=')
    blank()

    # ── Regime ──────────────────────────────────────────────────────────────
    h('市场 REGIME 分析')
    blank()
    rs = regime_sec.get('regime_score', '?')
    rl = regime_sec.get('regime_label', '?')
    mw = regime_sec.get('mrpt_weight', '?')
    tw = regime_sec.get('mtfs_weight', '?')
    interp = regime_sec.get('interpretation', '')
    row('综合 Regime 评分:', f'{rs}/100')
    row('Regime 标签:', rl)
    row('MRPT 建议权重:', f'{mw:.0%}' if isinstance(mw, float) else str(mw))
    row('MTFS 建议权重:', f'{tw:.0%}' if isinstance(tw, float) else str(tw))
    row('解读:', interp)
    blank()

    # Key indicators table
    lines.append(f'  {"指标":<28}{"当前值":>12}  {"解读":<30}')
    lines.append(f'  {"-"*68}')
    key_inds = [
        'vix_level','vix_z','move_level','hy_spread_level','ig_spread_level',
        'yield_curve_level','effr_level','effr_1y_change','breakeven_10y_level',
        'fin_stress_level','nfci_level','consumer_sent_level','recession_flag',
        'nvda_20d','arkk_20d','soxx_20d','gld_20d','uso_20d','uup_20d','spy_20d',
    ]
    ind_data = regime_sec.get('indicators', {})
    for k in key_inds:
        if k in ind_data:
            d = ind_data[k]
            name  = d.get('name', k)
            fmtd  = d.get('formatted', str(d.get('raw_value','')))
            interp_str = d.get('interpretation', '')
            lines.append(f'  {name:<28}{fmtd:>12}  {interp_str}')
    blank()

    # Component scores bar chart
    lines.append(f'  {"类别":<18}{"得分":>6}  {"偏向":>18}  权重  贡献  [分布图]')
    lines.append(f'  {"-"*68}')
    for cat, cd in regime_sec.get('component_scores', {}).items():
        agg  = cd.get('aggregate_score', 0.5)
        bias = cd.get('bias', '')
        wt   = cd.get('weight_in_regime', 0)
        cont = cd.get('contribution', 0)
        bar  = '▓' * int(agg * 20) + '░' * (20 - int(agg * 20))
        lines.append(f'  {cat:<18}{agg:>6.3f}  {bias:>18}  {wt:.0%}  {cont:.3f}  [{bar}]')
    blank()

    # ── Portfolio Allocation ─────────────────────────────────────────────────
    if portfolio:
        h('组合资金分配')
        blank()
        row('总资金:', f'${portfolio.get("total_capital",0):>12,.0f}')
        blank()
        lines.append(f'  {"策略":<8}{"权重":>7}{"资金分配":>15}{"开仓数":>8}{"平仓数":>8}{"scaling":>12}')
        lines.append(f'  {"-"*58}')
        for strat in ('mrpt', 'mtfs'):
            sec = report.get(strat, {})
            w   = portfolio.get(f'{strat}_weight', 0)
            cap = portfolio.get(f'{strat}_allocation', 0)
            sf  = sec.get('scale_factor', 1)
            sm  = sec.get('summary', {})
            lines.append(f'  {strat.upper():<8}{w:>6.0%} ${cap:>13,.0f}'
                         f'{sm.get("n_open",0):>8}{sm.get("n_close",0):>8}'
                         f'  {sf:.4f}×')
        blank()
        lines.append(f'  Scaling 说明: 所有回测的 Dollar PnL / MaxDD 乘以对应 scale 因子')
        lines.append(f'  得到实际资金规模下的预期值。Sharpe / MaxDD% 不受影响。')
        blank()

    # ── Position Monitor section ──────────────────────────────────────────────
    mon_sec = report.get('position_monitor', {})
    if mon_sec.get('has_positions'):
        h('当前持仓监测 (POSITION MONITOR)')
        blank()
        lines.append(f'  说明: 以下配对为昨日或更早开仓、今日继续持有的真实头寸。')
        lines.append(f'        运行专属回测（参数与开仓时相同）重新计算今日z-score/止损。')
        blank()
        mon_hold  = [s for s in mon_sec.get('mrpt',[]) + mon_sec.get('mtfs',[]) if s.get('action') == 'HOLD']
        mon_close = [s for s in mon_sec.get('mrpt',[]) + mon_sec.get('mtfs',[]) if 'CLOSE' in s.get('action','')]

        if mon_hold:
            h2(f'  ─ 持仓中 HOLD ({len(mon_hold)} 个)')
            blank()
            lines.append(f'  {"策略":<6}{"配对":<14}{"方向":<7}{"持有天":>6}{"z/ms":>8}{"uPnL($)":>12}{"uPnL%":>8}  参数集')
            lines.append(f'  {"-"*72}')
            for strat_key in ('mrpt', 'mtfs'):
                for s in mon_sec.get(strat_key, []):
                    if s.get('action') != 'HOLD':
                        continue
                    pair   = s.get('pair', '')
                    dirn   = s.get('direction', '')
                    days   = s.get('days_held', 0)
                    sv_key = 'z_score' if strat_key == 'mrpt' else 'momentum_spread'
                    sv     = s.get(sv_key)
                    sv_str = f'{sv:+.3f}' if sv is not None else 'n/a'
                    upnl   = s.get('unrealized_pnl')
                    upnl_pct = s.get('unrealized_pnl_pct')
                    upnl_str = f'${upnl:+,.0f}' if upnl is not None else 'n/a'
                    upct_str = f'{upnl_pct:+.2f}%' if upnl_pct is not None else ''
                    ps = s.get('param_set', '')
                    lines.append(f'  {strat_key.upper():<6}{pair:<14}{dirn:<7}{days:>6}{sv_str:>8}'
                                 f'{upnl_str:>12}{upct_str:>8}  {ps}')
            blank()

        if mon_close:
            h2(f'  ✕ 今日关闭 CLOSE ({len(mon_close)} 个) — 明日开盘执行')
            blank()
            for strat_key in ('mrpt', 'mtfs'):
                for s in mon_sec.get(strat_key, []):
                    if 'CLOSE' not in s.get('action', ''):
                        continue
                    pair = s.get('pair', '')
                    act  = s.get('action', '')
                    leg1 = s.get('s1', {})
                    leg2 = s.get('s2', {})
                    upnl = s.get('unrealized_pnl')
                    lines.append(f'  ┌─ {act:<14}  {strat_key.upper()} {pair}  持有{s.get("days_held",0)}天')
                    lines.append(f'  │  原因: {s.get("note","")}')
                    if leg1:
                        lines.append(f'  │  腿1: {leg1.get("side",""):<12} {leg1.get("symbol","")}  '
                                     f'{leg1.get("shares",0):+d}股 @${leg1.get("price","")}')
                    if leg2:
                        lines.append(f'  │  腿2: {leg2.get("side",""):<12} {leg2.get("symbol","")}  '
                                     f'{leg2.get("shares",0):+d}股 @${leg2.get("price","")}')
                    if upnl is not None:
                        lines.append(f'  │  实现盈亏估算: ${upnl:+,.0f}')
                    lines.append(f'  └{"─"*60}')
                    blank()

    # ── Per-strategy sections ─────────────────────────────────────────────────
    strats = ['mrpt', 'mtfs'] if rtype == 'combined' else [rtype.replace('single_', '')]
    for strat in strats:
        sec = report.get(strat, {})
        if not sec:
            continue
        cap    = sec.get('capital', 0)
        sf     = sec.get('scale_factor', 1)
        sm     = sec.get('summary', {})
        sig_label = 'Z-score' if strat == 'mrpt' else 'Momentum Spread'
        sig_key   = 'z_score' if strat == 'mrpt' else 'momentum_spread'

        h(f'{strat.upper()} 策略信号  (资金 ${cap:,.0f}  scale={sf:.4f}×)')
        blank()

        strat_desc = 'MRPT (均值回归 / 做空波动率): 价差 z-score 超过阈值时入场，回归时平仓' if strat == 'mrpt' \
                     else 'MTFS (动量趋势 / 做多波动率): 动量价差超过阈值时追入趋势，衰减时退出'
        lines.append(f'  策略性质: {strat_desc}')
        blank()
        row('有效信号数:', f'{sm.get("n_open",0)} 开仓  {sm.get("n_close",0)} 平仓  '
                          f'{sm.get("n_hold",0)} 持有  {sm.get("n_flat",0)} 观望')
        row('已排除配对数 (OOS过滤):', str(sm.get('n_excluded_pairs', 0)))
        blank()

        # ── ACTIVE signals ────────────────────────────────────────────────
        active = sec.get('active_signals', [])
        if active:
            h2(f'  ▶ 今日有效信号 ({len(active)} 个)')
            blank()
            for s in active:
                action  = s['action']
                pair    = s['pair']
                sv      = s.get(sig_key)
                et      = s.get('entry_threshold')
                ext     = s.get('exit_threshold')
                leg1    = s.get('s1', {})
                leg2    = s.get('s2', {})
                days    = s.get('days_held', 0)
                notional = s.get('total_notional', 0)
                oos_sh  = s.get('oos_sharpe')
                oos_pnl = s.get('oos_pnl_$1m')
                oos_dd  = s.get('oos_maxdd_$1m')
                oos_pw  = s.get('oos_pos_windows')
                tier    = s.get('oos_tier', '')

                action_symbols = {
                    'OPEN_LONG': '▲ OPEN LONG ', 'OPEN_SHORT': '▼ OPEN SHORT',
                    'CLOSE': '✕ CLOSE     ', 'CLOSE_STOP': '✕ STOP LOSS ',
                    'HOLD':  '— HOLD      ',
                }
                asym = action_symbols.get(action, action)

                lines.append(f'  ┌─ {asym}  {pair}')
                lines.append(f'  │  {sig_label:<22}: {sv:+.4f}  (entry threshold: ±{et})' if sv is not None else f'  │  {sig_label}: N/A')
                if ext is not None and action not in ('OPEN_LONG', 'OPEN_SHORT'):
                    lines.append(f'  │  Exit threshold      : ±{ext}')
                if days:
                    lines.append(f'  │  Days held           : {days}')
                blank_leg = '  │'

                # Leg details
                s1sym  = leg1.get('symbol', pair.split('/')[0])
                s2sym  = leg2.get('symbol', pair.split('/')[1])
                s1sh   = leg1.get('shares', s.get('s1_shares', 0))
                s2sh   = leg2.get('shares', s.get('s2_shares', 0))
                s1p    = leg1.get('price', s.get('s1_price', ''))
                s2p    = leg2.get('price', s.get('s2_price', ''))
                s1v    = leg1.get('est_value')
                s2v    = leg2.get('est_value')
                s1side = leg1.get('side', '')
                s2side = leg2.get('side', '')

                lines.append(f'  │  执行腿 1            : {s1side:<12} {s1sym}  {s1sh:+d}股 @${s1p}'
                             + (f'  ≈ ${s1v:,.0f}' if s1v else ''))
                lines.append(f'  │  执行腿 2            : {s2side:<12} {s2sym}  {s2sh:+d}股 @${s2p}'
                             + (f'  ≈ ${s2v:,.0f}' if s2v else ''))
                if notional:
                    lines.append(f'  │  合计名义市值        : ${notional:,.0f}')
                # OOS reference
                if oos_sh is not None:
                    tier_str = f'Tier {tier}' if tier else ''
                    lines.append(f'  │  OOS参考 ({tier_str:<6})  : Sharpe={oos_sh:+.3f}  PnL={oos_pnl:+,.0f}  '
                                 f'MaxDD={oos_dd:,.0f}  正收益窗口={oos_pw}')
                lines.append(f'  │  备注                : {s.get("note","")}')
                lines.append(f'  └{"─"*60}')
                blank()

        # ── FLAT signals ──────────────────────────────────────────────────
        flat_sigs = sec.get('flat_signals', [])
        if flat_sigs:
            h2(f'  ○ 观望配对 ({len(flat_sigs)} 个，按距触发距离排序)')
            blank()
            lines.append(f'  {"配对":<12}  {sig_label:<22}  {"阈值":>8}  {"距触发":>8}  '
                         f'{"OOS Sharpe":>11}  {"OOS盈亏窗口":>12}  备注')
            lines.append(f'  {"-"*95}')
            for s in flat_sigs:
                pair   = s['pair']
                sv     = s.get(sig_key)
                et     = s.get('entry_threshold')
                gap    = s.get('distance_to_entry')
                oos_sh = s.get('oos_sharpe')
                oos_pw = s.get('oos_pos_windows', '')
                sv_str = f'{sv:+.4f}' if sv is not None else 'n/a'
                et_str = f'±{et:.4f}' if et is not None else 'n/a'
                gap_str = f'{gap:.4f}' if gap is not None else 'n/a'
                oos_str = f'{oos_sh:+.3f}' if oos_sh is not None else 'n/a'
                lines.append(f'  {pair:<12}  {sv_str:<22}  {et_str:>8}  {gap_str:>8}  '
                             f'{oos_str:>11}  {str(oos_pw):>12}  '
                             f'差{gap_str}触发')
            blank()

        # ── EXCLUDED pairs ────────────────────────────────────────────────
        excl = sec.get('excluded_pairs', [])
        if excl:
            h2(f'  ✗ OOS过滤淘汰配对 ({len(excl)} 个，不参与今日交易)')
            blank()
            lines.append(f'  {"配对":<12}  {"OOS Sharpe":>11}  {"OOS PnL($1M)":>13}  '
                         f'{"MaxDD($1M)":>11}  {"盈利窗口":>9}  淘汰原因')
            lines.append(f'  {"-"*90}')
            for e in excl:
                pair   = e['pair']
                oos_sh = e.get('oos_sharpe', 0)
                oos_pl = e.get('oos_pnl_$1m', 0)
                oos_dd = e.get('oos_maxdd_$1m', 0)
                oos_pw = e.get('pos_windows', '')
                reason = e.get('exclusion_reason', '')
                lines.append(f'  {pair:<12}  {oos_sh:>+11.3f}  ${oos_pl:>+12,.0f}  '
                             f'${oos_dd:>+10,.0f}  {str(oos_pw):>9}  {reason}')
            blank()

        # ── OOS performance ref for active pairs ──────────────────────────
        h2(f'  📊 {strat.upper()} 在用配对 OOS 历史表现参考 (6窗口 Walk-Forward)')
        blank()
        lines.append(f'  {"配对":<12}  {"Tier":>5}  {"OOS Sharpe":>11}  {"OOS PnL($1M)":>13}  '
                     f'{"MaxDD($1M)":>11}  {"盈利窗口/总窗口":>14}  DSR参数集')
        lines.append(f'  {"-"*95}')
        oos_ref_dict = _OOS_PERF_MRPT if strat == 'mrpt' else _OOS_PERF_MTFS
        pair_configs_fn = get_pair_configs_mrpt if strat == 'mrpt' else get_pair_configs_mtfs
        for s1, s2, ps in pair_configs_fn():
            pair = f'{s1}/{s2}'
            perf = oos_ref_dict.get(pair, {})
            oos_sh = perf.get('oos_sharpe', 'n/a')
            oos_pl = perf.get('oos_pnl', 0)
            oos_dd = perf.get('oos_maxdd', 0)
            oos_pw = perf.get('pos_windows', '')
            tier   = perf.get('tier', '')
            oos_sh_str = f'{oos_sh:+.3f}' if isinstance(oos_sh, float) else str(oos_sh)
            lines.append(f'  {pair:<12}  {str(tier):>5}  {oos_sh_str:>11}  ${oos_pl:>+12,.0f}  '
                         f'${oos_dd:>+10,.0f}  {str(oos_pw):>14}  {ps}')
        blank()

    # ── Scaling reference table ───────────────────────────────────────────
    h('资金 Scaling 参考表 (Sharpe/MaxDD% 不受影响)')
    blank()
    lines.append(f'  {"实际资金":>14}  {"相对$1M scale":>14}  {"MRPT预期PnL":>14}  {"MTFS预期PnL":>14}  {"预期MaxDD":>12}')
    lines.append(f'  {"-"*70}')

    # Use latest OOS PnL sums as reference for selected pairs
    mrpt_sec = report.get('mrpt', {})
    mtfs_sec = report.get('mtfs', {})
    mrpt_pnl_1m = sum(v.get('oos_pnl', 0) for v in _OOS_PERF_MRPT.values() if not v.get('excluded'))
    mtfs_pnl_1m = sum(v.get('oos_pnl', 0) for v in _OOS_PERF_MTFS.values() if not v.get('excluded'))
    mrpt_dd_1m  = min(v.get('oos_maxdd', 0) for v in _OOS_PERF_MRPT.values() if not v.get('excluded'))
    mtfs_dd_1m  = min(v.get('oos_maxdd', 0) for v in _OOS_PERF_MTFS.values() if not v.get('excluded'))

    for cap in [250_000, 500_000, 1_000_000, 2_000_000, 5_000_000]:
        sf = cap / 1_000_000
        lines.append(f'  ${cap:>13,.0f}  {sf:>14.4f}×  '
                     f'${mrpt_pnl_1m*sf:>+13,.0f}  ${mtfs_pnl_1m*sf:>+13,.0f}  '
                     f'${mrpt_dd_1m*sf:>+11,.0f}')
    blank()
    lines.append(f'  注: 以上基于 OOS walk-forward 6窗口历史表现，非未来保证')
    blank()

    sep()
    lines.append(f'  报告结束  |  {report.get("generated_at","?")}')
    sep()

    txt = '\n'.join(lines)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(txt)
    log.info(f"Report saved → {path}")
    return txt


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Daily signal generator — MRPT (mean reversion) + MTFS (momentum)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single strategy with actual capital
  python DailySignal.py --strategy mrpt --capital 600000
  python DailySignal.py --strategy mtfs --capital 400000

  # Combined mode: auto regime weighting
  python DailySignal.py --strategy both --total-capital 1000000

  # Combined mode: manual 60/40 split
  python DailySignal.py --strategy both --total-capital 1000000 --mrpt-weight 0.6

  # Dry run (no inventory update)
  python DailySignal.py --strategy both --total-capital 1000000 --dry-run

  # Just regime report (no signal generation)
  python RegimeDetector.py --fred-key <KEY>
        """)

    parser.add_argument('--strategy', choices=['mrpt', 'mtfs', 'both'], required=True,
                        help='Strategy: mrpt | mtfs | both (combined)')
    parser.add_argument('--date', type=str, default=None,
                        help='Signal date YYYY-MM-DD (default: most recent weekday)')

    # Capital arguments
    cap_group = parser.add_argument_group('Capital')
    cap_group.add_argument('--capital', type=float, default=None,
                           help='Actual capital for single strategy mode (USD)')
    cap_group.add_argument('--total-capital', type=float, default=None,
                           help='Total capital for --strategy both (split by regime weights)')
    cap_group.add_argument('--mrpt-weight', type=float, default=None,
                           help='Override regime: MRPT weight 0.0-1.0 (e.g. 0.6 = 60%% MRPT)')

    # Regime arguments
    reg_group = parser.add_argument_group('Regime detection')
    reg_group.add_argument('--fred-key', type=str, default=None,
                           help='FRED API key (or set FRED_API_KEY env var)')
    reg_group.add_argument('--min-regime-weight', type=float, default=0.20,
                           help='Min weight for any strategy (default 0.20)')
    reg_group.add_argument('--skip-regime', action='store_true',
                           help='Skip regime detection, use equal weights')

    parser.add_argument('--dry-run', action='store_true',
                        help='Print signals without updating inventory JSON')

    args = parser.parse_args()

    signal_date = date.fromisoformat(args.date) if args.date else prev_weekday(date.today())

    run_daily_signal(
        strategy        = args.strategy,
        signal_date     = signal_date,
        dry_run         = args.dry_run,
        capital         = args.capital,
        total_capital   = args.total_capital,
        mrpt_weight     = args.mrpt_weight,
        fred_key        = args.fred_key or os.getenv('FRED_API_KEY', ''),
        min_regime_weight = args.min_regime_weight,
        skip_regime     = args.skip_regime,
    )
