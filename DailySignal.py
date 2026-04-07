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
import shutil
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
INVENTORY_HISTORY_DIR = os.path.join(BASE_DIR, 'inventory_history')

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
    # Backup existing file before overwriting
    if os.path.exists(path):
        os.makedirs(INVENTORY_HISTORY_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(INVENTORY_HISTORY_DIR, f"inventory_{strategy}_{ts}.json")
        shutil.copy2(path, backup_path)
        log.info(f"inventory_{strategy}.json backed up → {backup_path}")
    with open(path, 'w') as f:
        json.dump(inv, f, indent=2, default=str)
    log.info(f"inventory_{strategy}.json updated → {path}")
    # Sync to web app public/data/ for Firebase Hosting static fallback
    web_data_dir = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data')
    if os.path.isdir(web_data_dir):
        shutil.copy2(path, os.path.join(web_data_dir, f'inventory_{strategy}.json'))
        log.info(f"inventory_{strategy}.json synced → {web_data_dir}")


# ── Walk-forward config loader (auto, no hardcoding) ──────────────────────────

# Exclusion thresholds — adjust these to change which pairs get traded
_WF_EXCLUDE_SHARPE = -0.10   # OOS Sharpe below this → exclude
_WF_EXCLUDE_PNL    = -1000   # OOS cumulative PnL below this ($1M base) → exclude


def _load_wf_configs(strategy: str) -> tuple[list, dict]:
    """
    Auto-load pair configs and OOS performance reference from the most recent
    walk-forward output files.  Runs after every new walk-forward automatically —
    no manual hardcoding of pair lists or OOS stats needed.

    Sources:
      walk_forward_summary_*.json  → W(last) selected_pairs + param_sets
      oos_pair_summary_*.csv       → cumulative OOS Sharpe/PnL/MaxDD per pair

    Returns:
      pair_configs : [(s1, s2, param_set), ...] for pairs passing filters,
                     sorted by OOS Sharpe descending
      oos_perf     : {pair_key: {oos_sharpe, oos_pnl, oos_maxdd, tier,
                                  excluded, reason}, ...}
    """
    import pandas as pd

    if strategy == 'mrpt':
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward')
    else:
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward_mtfs')

    # ── Latest walk-forward summary → W(last) param_sets ──────────────────
    # Sort by mtime (not filename) to handle inconsistent timestamp formats
    summaries = sorted(glob.glob(os.path.join(wf_dir, 'walk_forward_summary_*.json')),
                       key=os.path.getmtime)
    if not summaries:
        log.warning(f"[wf_config] No walk-forward summary in {wf_dir}")
        return [], {}
    with open(summaries[-1]) as f:
        wf = json.load(f)

    windows = wf.get('windows', [])
    if not windows:
        return [], {}
    last_w = windows[-1]
    n_windows = len(windows)
    # param_set lookup from last (most recent) window
    param_lookup = {f'{s1}/{s2}': ps for s1, s2, ps in last_w.get('selected_pairs', [])}
    # Count how many windows each pair appeared in (for pos_windows display)
    from collections import defaultdict as _dd
    pair_window_count: dict = _dd(int)
    for win in windows:
        for s1, s2, _ in win.get('selected_pairs', []):
            pair_window_count[f'{s1}/{s2}'] += 1

    log.info(f"[wf_config] {strategy.upper()}: W{last_w['window_idx']} "
             f"({last_w['test_start']}→{last_w['test_end']}) "
             f"from {os.path.basename(summaries[-1])}")

    # ── Latest OOS pair summary CSV → cumulative stats ────────────────────
    # Sort by mtime to robustly pick the newest file regardless of naming convention
    pair_csvs = sorted(glob.glob(os.path.join(wf_dir, 'oos_pair_summary_*.csv')),
                       key=os.path.getmtime)
    if not pair_csvs:
        log.warning(f"[wf_config] No oos_pair_summary CSV in {wf_dir}")
        return [], {}
    df = pd.read_csv(pair_csvs[-1])

    oos_perf = {}
    pair_configs = []

    for _, row in df.iterrows():
        pair_key  = row['Pair']
        sharpe    = float(row.get('Sharpe',   0) or 0)
        pnl       = float(row.get('OOS_PnL',  0) or 0)
        maxdd     = float(row.get('MaxDD',     0) or 0)
        maxdd_pct = float(row.get('MaxDD_pct', 0) or 0)
        n_trades  = int(row.get('N_Trades',    0) or 0)

        # Exclusion logic — OR: either condition alone triggers exclusion
        excluded, reason = False, ''
        if n_trades == 0:
            excluded, reason = True, 'No trades in OOS'
        elif sharpe < _WF_EXCLUDE_SHARPE or pnl < _WF_EXCLUDE_PNL:
            excluded = True
            parts = []
            if sharpe < _WF_EXCLUDE_SHARPE:
                parts.append(f'Sharpe={sharpe:.2f}<{_WF_EXCLUDE_SHARPE}')
            if pnl < _WF_EXCLUDE_PNL:
                parts.append(f'PnL=${pnl:,.0f}<${_WF_EXCLUDE_PNL:,.0f}')
            reason = ', '.join(parts)

        # Tier
        if excluded:
            tier = 0
        elif sharpe >= 0.8:
            tier = 1
        elif sharpe >= 0.3:
            tier = 2
        elif pnl > 0:
            tier = 3
        else:
            tier = 4

        oos_perf[pair_key] = {
            'oos_sharpe':  round(sharpe, 4),
            'oos_pnl':     round(pnl, 0),
            'oos_maxdd':   round(maxdd, 0),
            'pos_windows': f'{pair_window_count.get(pair_key, 0)}/{n_windows}',
            'tier':        tier,
            'excluded':    excluded,
            'reason':      reason,
        }

        # Only include pairs that pass filters AND appear in last window's selection
        if not excluded and pair_key in param_lookup:
            s1, s2 = pair_key.split('/')
            pair_configs.append((s1, s2, param_lookup[pair_key]))

    pair_configs.sort(
        key=lambda x: oos_perf.get(f'{x[0]}/{x[1]}', {}).get('oos_sharpe', 0),
        reverse=True,
    )
    n_excl = sum(1 for v in oos_perf.values() if v['excluded'])
    log.info(f"[wf_config] {strategy.upper()}: {len(pair_configs)} pairs selected, "
             f"{n_excl} excluded "
             f"(Sharpe<{_WF_EXCLUDE_SHARPE} AND PnL<${_WF_EXCLUDE_PNL:,})")
    return pair_configs, oos_perf


# Module-level cache — loaded once per process, refreshed on next run
_wf_cache: dict = {}


def get_pair_configs_mrpt() -> list[tuple[str, str, str]]:
    """Auto-loaded from latest MRPT walk-forward output."""
    if 'mrpt' not in _wf_cache:
        _wf_cache['mrpt'] = _load_wf_configs('mrpt')
    return _wf_cache['mrpt'][0]


def get_pair_configs_mtfs() -> list[tuple[str, str, str]]:
    """Auto-loaded from latest MTFS walk-forward output."""
    if 'mtfs' not in _wf_cache:
        _wf_cache['mtfs'] = _load_wf_configs('mtfs')
    return _wf_cache['mtfs'][0]


def _get_oos_perf(strategy: str) -> dict:
    """OOS performance reference dict used by report builders."""
    if strategy not in _wf_cache:
        _wf_cache[strategy] = _load_wf_configs(strategy)
    return _wf_cache[strategy][1]


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

            # ── Inject cost basis so PnL stop works correctly ────────────
            open_p1 = inv_pair.get('open_s1_price') or 0
            open_p2 = inv_pair.get('open_s2_price') or 0
            if open_p1 and s1s:
                context.portfolio.cost_basis_history[s1] = [(signal_date, open_p1)]
            if open_p2 and s2s:
                context.portfolio.cost_basis_history[s2] = [(signal_date, open_p2)]

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

from pair_universe import mrpt_sector_map as _build_sector_map
_SECTOR_MAP = _build_sector_map()


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
        signal_value   = today_rv.get(f'Z_{sector}', float('nan'))
        # Fallback: if sector guess missed (e.g. pair removed from pair_universe
        # but still held in inventory), scan today_rv for any Z_* key.
        if np.isnan(signal_value):
            for k, v in today_rv.items():
                if k.startswith('Z_'):
                    try:
                        candidate = float(v)
                        if not np.isnan(candidate):
                            signal_value = candidate
                            break
                    except (TypeError, ValueError):
                        pass
        if np.isnan(signal_value):
            signal_value = today_rv.get('z_score', float('nan'))
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
        signal_value   = today_rv.get(f'Momentum_Spread_{sector}', float('nan'))
        # Fallback: scan for any Momentum_Spread_* key (pair removed from universe)
        if np.isnan(signal_value):
            for k, v in today_rv.items():
                if k.startswith('Momentum_Spread_'):
                    try:
                        candidate = float(v)
                        if not np.isnan(candidate):
                            signal_value = candidate
                            break
                    except (TypeError, ValueError):
                        pass
        if np.isnan(signal_value):
            signal_value = today_rv.get('Momentum_Spread', float('nan'))
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

    # Values at open time — stored in inventory for accurate monitor restoration.
    # open_hedge_ratio: OLS hedge on open day (informational).
    # open_price_level_stop: the exact stop level set by the strategy (spread[-1]*0.8
    #   for long, *1.5 for short) — injected directly by monitor, no reconstruction.
    _hh = context.portfolio.hedge_history.get(pair_key, [])
    open_hedge_ratio = _hh[-1][1] if _hh else None
    open_price_level_stop = context.execution.price_level_stop_loss.get(pair_key)

    # OPEN LONG
    if in_long and not inv_direction:
        d = {**base, 'action': 'OPEN_LONG', 'direction': 'long',
             'entry_threshold': et,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'open_hedge_ratio': open_hedge_ratio,
             'open_price_level_stop': open_price_level_stop,
             'note': f'signal={sv} triggered long entry (threshold ±{et})'}
        d.update(_legs('long'))
        return d

    # OPEN SHORT
    if in_short and not inv_direction:
        d = {**base, 'action': 'OPEN_SHORT', 'direction': 'short',
             'entry_threshold': et,
             's1_shares': s1_shares, 's2_shares': s2_shares,
             'open_hedge_ratio': open_hedge_ratio,
             'open_price_level_stop': open_price_level_stop,
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

def _build_wf_source_for_pair(pair_key: str, strategy: str) -> dict:
    """
    Build wf_source dict for a pair at the time of entry into inventory.
    Records walk-forward summary file path and per-window param_sets / OOS stats
    for every window where this pair appeared, keyed as W1..W6.
    Default monitor window = last window (W6 or whatever the highest index is).
    """
    if strategy == 'mrpt':
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward')
    else:
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward_mtfs')

    summaries = sorted(glob.glob(os.path.join(wf_dir, 'walk_forward_summary_*.json')),
                       key=os.path.getmtime)
    if not summaries:
        return {}

    summary_file = summaries[-1]
    try:
        with open(summary_file) as f:
            wf = json.load(f)
    except Exception:
        return {}

    windows_info = {}
    default_window = None
    for win in wf.get('windows', []):
        idx = win.get('window_idx')
        wkey = f'W{idx}'
        for s1, s2, ps in win.get('selected_pairs', []):
            if f'{s1}/{s2}' == pair_key:
                windows_info[wkey] = {
                    'param_set':  ps,
                    'train_end':  win.get('train_end'),
                    'test_start': win.get('test_start'),
                    'test_end':   win.get('test_end'),
                    'oos_sharpe': round(win.get('oos_sharpe', float('nan')), 4)
                    if win.get('oos_sharpe') is not None else None,
                    'oos_pnl':    round(win.get('oos_pnl', 0), 2),
                }
                default_window = wkey  # last matching window becomes default

    return {
        'wf_summary_file': os.path.relpath(summary_file, BASE_DIR),
        'wf_dir':          os.path.relpath(wf_dir, BASE_DIR),
        'windows':         windows_info,
        'default_window':  default_window,  # W6 or last window this pair appeared in
    }


def _lookup_hedge_from_wf(pair_key: str, open_date_str: str, strategy: str) -> float | None:
    """
    Fallback: look up the hedge ratio for pair_key on open_date from the latest
    walk-forward window Excel (hedge_history sheet).

    Used when open_hedge_ratio is absent from inventory (positions opened before
    this field was introduced).  Searches all window dirs newest-first and returns
    the first match at or before open_date.
    """
    if strategy == 'mrpt':
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward')
    else:
        wf_dir = os.path.join(BASE_DIR, 'historical_runs', 'walk_forward_mtfs')

    # All window subdirs sorted newest first (by mtime)
    win_dirs = sorted(
        [d for d in glob.glob(os.path.join(wf_dir, 'window*')) if os.path.isdir(d)],
        key=os.path.getmtime, reverse=True,
    )
    if not win_dirs:
        return None

    open_ts = pd.Timestamp(open_date_str)

    for win_dir in win_dirs:
        xlsx_files = sorted(
            glob.glob(os.path.join(win_dir, 'historical_runs', 'portfolio_history_*.xlsx')),
            key=os.path.getmtime, reverse=True,
        )
        if not xlsx_files:
            continue
        xlsx = xlsx_files[0]
        try:
            hh = pd.read_excel(xlsx, sheet_name='hedge_history', parse_dates=['Date'])
            if pair_key not in hh.columns:
                continue
            # Find the row closest to (and not after) open_date
            candidates = hh[hh['Date'] <= open_ts + pd.Timedelta(days=3)]
            if candidates.empty:
                continue
            row = candidates.iloc[-1]
            val = row[pair_key]
            if pd.notna(val):
                log.debug(f"[monitor] {pair_key}: hedge fallback from {os.path.basename(win_dir)} "
                          f"date={row['Date'].date()} hedge={val:.6f}")
                return float(val)
        except Exception as e:
            log.debug(f"[monitor] hedge fallback: could not read {xlsx}: {e}")
            continue

    return None


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
            # ── Signal measures at entry ──────────────────────────────────
            open_signal = {}
            if strategy == 'mrpt':
                open_signal['z_score']         = sig.get('z_score')
                open_signal['entry_threshold'] = sig.get('entry_threshold')
                open_signal['exit_threshold']  = sig.get('exit_threshold')
            else:
                open_signal['momentum_spread'] = sig.get('momentum_spread')
                open_signal['entry_threshold'] = sig.get('entry_threshold')
                open_signal['exit_threshold']  = sig.get('exit_threshold')

            inv['pairs'][pair] = {
                'strategy':             strategy,
                'param_set':            param_set_lookup.get(pair, 'default'),
                'direction':            sig['direction'],
                's1_shares':            sig.get('s1_shares', 0),
                's2_shares':            sig.get('s2_shares', 0),
                'open_date':            signal_date,
                'open_s1_price':        sig.get('s1_price'),
                'open_s2_price':        sig.get('s2_price'),
                'open_hedge_ratio':     sig.get('open_hedge_ratio'),
                'open_price_level_stop': sig.get('open_price_level_stop'),
                'days_held':            0,
                'open_signal':          open_signal,
                'wf_source':            _build_wf_source_for_pair(pair, strategy),
                'monitor_log':          [],
            }

        elif action in ('CLOSE', 'CLOSE_STOP'):
            inv['pairs'][pair] = {'direction': None}

        elif action == 'HOLD':
            if pair in inv['pairs'] and inv['pairs'][pair].get('direction'):
                # Only increment days_held once per calendar day (idempotent re-runs)
                last_updated = inv['pairs'][pair].get('last_updated')
                if last_updated != signal_date:
                    inv['pairs'][pair]['days_held'] = inv['pairs'][pair].get('days_held', 0) + 1
                inv['pairs'][pair]['last_updated'] = signal_date
                # Do NOT update shares on HOLD — open shares are fixed at entry and
                # must not drift with regime-driven capital/scale changes each day.
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

    # ── Data window: strategy-specific warmup before open_date ────────────
    # MRPT needs z_back=36 + hedge_lag=1 = 37 bars minimum → 80 bday buffer
    # MTFS needs max_window=150 + skip_days=21 + 1 = 172 bars minimum → 220 bday buffer
    _MONITOR_WARMUP_BDAYS = {'mrpt': 80, 'mtfs': 220}
    warmup_bdays = _MONITOR_WARMUP_BDAYS.get(strategy, 220)

    try:
        open_ts = pd.Timestamp(open_date_str) if open_date_str else pd.Timestamp(signal_date)
    except Exception:
        open_ts = pd.Timestamp(signal_date)
    warmup_start = open_ts - pd.offsets.BDay(warmup_bdays)
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

    # ── Resolve open_date to first available trading day on or after open_date ──
    # Handles weekends/holidays so injection aligns with actual data index.
    try:
        open_ts_raw = pd.Timestamp(open_date_str) if open_date_str else effective_ts
    except Exception:
        open_ts_raw = effective_ts
    # Find first data date >= open_date (injection point)
    valid_open_dates = [d for d in historical_data.index if d >= open_ts_raw]
    open_ts_data = valid_open_dates[0] if valid_open_dates else effective_ts

    if open_ts_data > effective_ts:
        # open_date is after latest data — shouldn't happen, fall back to effective_ts
        log.warning(f"[monitor] {pair_key}: open_ts_data {open_ts_data.date()} > effective_ts "
                    f"{effective_ts.date()}, falling back to effective_ts injection")
        open_ts_data = effective_ts

    log.info(f"[monitor] {pair_key}: warmup ends {open_ts_data.date()}, "
             f"position held {open_ts_data.date()} → {effective_ts.date()}")

    inventory_injected = False
    # Track if the original injected position was closed by a stop or exit signal.
    # If so, we break immediately to prevent the strategy from re-opening a synthetic
    # position with different sizing that doesn't match the real inventory entry.
    original_position_closed = False
    stop_triggered_date = None
    stop_triggered_reason = None

    for date_ts in historical_data.index:
        context.portfolio.current_date = date_ts
        context.portfolio.processed_dates.append(date_ts)
        current_data = CustomData(historical_data.loc[:date_ts])
        # Warmup = everything before real open date; position runs from open_date → today
        context.warmup_mode = date_ts < open_ts_data

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

        # ── Inject position AFTER handle_data on open_ts_data ─────────────
        # Injecting after (not before) the opening bar mirrors real trading:
        # the position opens during open_ts_data, so stop checks only start
        # on the NEXT bar.  Injecting before would cause "1-day early" false
        # stop triggers (e.g. a short opened at z=1.5 could fire a 2-σ stop
        # immediately if the signal and volatility windows differ).
        if not inventory_injected and date_ts == open_ts_data:
            mini_inv = {'pairs': {pair_key: inv_pair}}
            _inject_inventory_into_context(context, mini_inv, date_ts)

            # ── Price-level stop: inject exact value from opening simulation ──
            open_plstop = inv_pair.get('open_price_level_stop')
            if open_plstop is not None:
                context.execution.price_level_stop_loss[pair_key] = open_plstop
                log.debug(f"[monitor] {pair_key}: price_level_stop={open_plstop:.4f} (from inventory)")
            else:
                # Clear any price_level_stop the strategy may have set during its
                # natural open on this bar — we cannot trust that approximate value.
                context.execution.price_level_stop_loss.pop(pair_key, None)
                log.debug(f"[monitor] {pair_key}: open_price_level_stop not in inventory — "
                          f"price_level_stop cleared (position predates this field)")

            inventory_injected = True

        # ── Detect position closure after injection ────────────────────────
        # Only check on bars AFTER open_ts_data — never on the injection bar
        # itself — to prevent false CLOSE_STOP on the opening day.
        if inventory_injected and date_ts > open_ts_data and not original_position_closed:
            pair_state = next(
                (p[2] for p in context.strategy_pairs if f"{p[0]}/{p[1]}" == pair_key),
                None
            )
            if pair_state is not None:
                if not pair_state.get('in_long', False) and not pair_state.get('in_short', False):
                    original_position_closed = True
                    slh = context.execution.stop_loss_history.get(pair_key, [])
                    if slh:
                        # Use actual stop date from history, not detection date
                        stop_triggered_date = pd.Timestamp(slh[-1]['date'])
                        stop_triggered_reason = slh[-1].get('reason', 'Stop Loss')
                    else:
                        stop_triggered_date = date_ts
                        stop_triggered_reason = 'Exit Signal'
                    log.info(f"[monitor] {pair_key}: original position closed on "
                             f"{stop_triggered_date if isinstance(stop_triggered_date, str) else stop_triggered_date.date()} "
                             f"reason='{stop_triggered_reason}' "
                             f"— stopping simulation to prevent synthetic re-entry")
                    break

    # ── If original injected position was closed during simulation ─────────
    if original_position_closed:
        slh  = context.execution.stop_loss_history.get(pair_key, [])
        action = 'CLOSE_STOP' if slh else 'CLOSE'

        # Use real inventory prices for unrealized PnL (simulation prices may differ)
        open_p1 = inv_pair.get('open_s1_price')
        open_p2 = inv_pair.get('open_s2_price')
        s1s     = inv_pair.get('s1_shares', 0)
        s2s     = inv_pair.get('s2_shares', 0)
        upnl    = None
        upnl_pct = None
        prices_today = {}
        for sym in (s1, s2):
            ph = context.portfolio.price_history.get(sym)
            if ph:
                prices_today[sym] = ph[-1][1]
        p1_now = prices_today.get(s1)
        p2_now = prices_today.get(s2)
        if open_p1 and open_p2 and p1_now and p2_now:
            if direction == 'long':
                upnl = (p1_now - open_p1) * abs(s1s) - (p2_now - open_p2) * abs(s2s)
            else:
                upnl = (open_p1 - p1_now) * abs(s1s) + (p2_now - open_p2) * abs(s2s)
            cost_basis = open_p1 * abs(s1s) + open_p2 * abs(s2s)
            upnl_pct = round(upnl / cost_basis * 100, 3) if cost_basis > 0 else None
            upnl = round(upnl * scale_factor, 2)

        # Export partial monitor history up to stop date
        monitor_history_file = None
        try:
            from PortfolioClasses import ExportExcel as _ExportExcel
            monitor_dir = os.path.join(BASE_DIR, 'trading_signals', 'monitor_history')
            os.makedirs(monitor_dir, exist_ok=True)
            pair_safe = pair_key.replace('/', '_')
            ts_file = datetime.now().strftime('%Y%m%d_%H%M%S')
            monitor_filename = os.path.join(
                monitor_dir, f'monitor_{strategy}_{pair_safe}_{ts_file}.xlsx')
            exporter = _ExportExcel(monitor_filename)
            exporter.export_portfolio_data(context.portfolio, context)
            log.info(f"[monitor] {pair_key}: partial history saved → {monitor_filename}")
            monitor_history_file = os.path.relpath(monitor_filename, BASE_DIR)
        except Exception as e:
            log.warning(f"[monitor] {pair_key}: could not export history: {e}")

        # Extract z_score / momentum_spread from recorded_vars at stop date
        last_signal_value = None
        rv_all = context.recorded_vars.get(pair_key, {})
        if rv_all:
            # Use stop_triggered_date only; do not guess if not found
            stop_rv = None
            if stop_triggered_date is not None:
                stop_rv = _find_today_rv(context, pair_key, stop_triggered_date)
            if stop_rv is not None:
                sector = _get_sector(s1, s2)
                if strategy == 'mrpt':
                    last_signal_value = stop_rv.get(f'Z_{sector}', float('nan'))
                    if np.isnan(last_signal_value):
                        for k, v in stop_rv.items():
                            if k.startswith('Z_'):
                                try:
                                    candidate = float(v)
                                    if not np.isnan(candidate):
                                        last_signal_value = candidate
                                        break
                                except (TypeError, ValueError):
                                    pass
                else:
                    last_signal_value = stop_rv.get(f'Momentum_Spread_{sector}', float('nan'))
                    if np.isnan(last_signal_value):
                        for k, v in stop_rv.items():
                            if k.startswith('Momentum_Spread_'):
                                try:
                                    candidate = float(v)
                                    if not np.isnan(candidate):
                                        last_signal_value = candidate
                                        break
                                except (TypeError, ValueError):
                                    pass
                last_signal_value = _r(last_signal_value)

        sig_key = 'z_score' if strategy == 'mrpt' else 'momentum_spread'
        sig = {
            'pair':              pair_key,
            'action':            action,
            'direction':         inv_pair.get('direction'),
            'days_held':         inv_pair.get('days_held', 0),
            'monitored':         True,
            'open_date':         open_date_str,
            'param_set':         param_set,
            sig_key:             last_signal_value,
            'unrealized_pnl':    upnl,
            'unrealized_pnl_pct': upnl_pct,
            'note': (f"Injected position closed in simulation on "
                     f"{stop_triggered_date.date()} ({stop_triggered_reason}). "
                     f"Simulation stopped — synthetic re-entry suppressed."),
        }
        if monitor_history_file:
            sig['monitor_history_file'] = monitor_history_file
        log.info(f"[monitor] {pair_key}: action={sig['action']}  "
                 f"days_held={inv_pair.get('days_held',0)}  "
                 f"upnl={upnl}  stop_date={stop_triggered_date.date()}")
        return sig

    # ── Extract signal from recorded_vars ─────────────────────────────────
    today_rv = _find_today_rv(context, pair_key, effective_ts)
    if today_rv is None:
        return {'pair': pair_key, 'action': 'NO_DATA', 'monitored': True,
                'note': 'No recorded_vars after monitor run'}

    # Guard: if insufficient data bars, momentum/z-score was not computed —
    # do NOT let _build_signal misinterpret missing in_long as CLOSE.
    data_bars     = today_rv.get('data_bars')
    required_bars = today_rv.get('required_bars')
    if data_bars is not None and required_bars is not None and data_bars < required_bars:
        return {
            'pair':      pair_key,
            'action':    'HOLD',
            'direction': inv_pair.get('direction'),
            'days_held': inv_pair.get('days_held', 0),
            'monitored': True,
            'open_date': open_date_str,
            'param_set': param_set,
            'note': f'Insufficient data ({data_bars}/{required_bars} bars) — cannot evaluate exit, holding',
        }

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

    # ── Export full monitor history to Excel ───────────────────────────────
    try:
        from PortfolioClasses import ExportExcel as _ExportExcel
        monitor_dir = os.path.join(BASE_DIR, 'trading_signals', 'monitor_history')
        os.makedirs(monitor_dir, exist_ok=True)
        pair_safe = pair_key.replace('/', '_')
        ts_file = datetime.now().strftime('%Y%m%d_%H%M%S')
        monitor_filename = os.path.join(monitor_dir,
                                        f'monitor_{strategy}_{pair_safe}_{ts_file}.xlsx')
        exporter = _ExportExcel(monitor_filename)
        exporter.export_portfolio_data(context.portfolio, context)
        log.info(f"[monitor] {pair_key}: history saved → {monitor_filename}")
        sig['monitor_history_file'] = os.path.relpath(monitor_filename, BASE_DIR)
    except Exception as e:
        log.warning(f"[monitor] {pair_key}: could not export history: {e}")

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

        if not dry_run and result[strategy]:
            # ── Append today's monitor result to each pair's monitor_log ──────
            date_str = signal_date.strftime('%Y-%m-%d')
            inv_updated = deepcopy(inventory)
            for sig in result[strategy]:
                pk = sig.get('pair')
                if pk not in inv_updated.get('pairs', {}):
                    continue
                log_entry = {
                    'date':             date_str,
                    'action':           sig.get('action'),
                    'direction':        sig.get('direction'),
                    'z_score':          sig.get('z_score'),
                    'momentum_spread':  sig.get('momentum_spread'),
                    'exit_threshold':   sig.get('exit_threshold'),
                    'unrealized_pnl':   sig.get('unrealized_pnl'),
                    'unrealized_pnl_pct': sig.get('unrealized_pnl_pct'),
                    'note':             sig.get('note', ''),
                }
                if 'monitor_log' not in inv_updated['pairs'][pk]:
                    inv_updated['pairs'][pk]['monitor_log'] = []
                # Idempotent: only append if no entry for this date yet
                existing_dates = {e.get('date') for e in inv_updated['pairs'][pk]['monitor_log']}
                if date_str not in existing_dates:
                    inv_updated['pairs'][pk]['monitor_log'].append(log_entry)

            # Apply monitor CLOSE signals to inventory.
            # days_held is incremented once per day by _run_single (Step 2).
            close_sigs = [s for s in result[strategy]
                          if s.get('action') in ('CLOSE', 'CLOSE_STOP')]
            if close_sigs:
                inv_updated = update_inventory_from_signals(
                    inv_updated, close_sigs, date_str,
                    strategy=strategy)

            save_inventory(inv_updated, strategy)
            if close_sigs:
                log.info(f"[monitor] inventory_{strategy}.json updated (closed + monitor_log)")
            else:
                log.info(f"[monitor] inventory_{strategy}.json updated (monitor_log only)")

    return result


# ── Regime detection ───────────────────────────────────────────────────────────

def _run_regime_detection(fred_key: str | None = None,
                          min_weight: float = 0.20,
                          use_vix_forecast: bool = False,
                          vix_forecast_finetune: bool = False) -> dict:
    """
    Run RegimeDetector and return result dict.
    Falls back to neutral (50/50) if detection fails.
    """
    try:
        # 增量更新 VIX/MOVE 历史数据（每日自动追加，边界随历史自动更新）
        try:
            from MacroDataStore import MacroDataStore
            MacroDataStore().update()
        except Exception as e:
            log.debug(f"MacroDataStore update skipped: {e}")

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
            use_vix_forecast=use_vix_forecast,
            vix_forecast_finetune=vix_forecast_finetune,
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
    use_vix_forecast: bool = False,
    vix_forecast_finetune: bool = False,
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
                use_vix_forecast=use_vix_forecast,
                vix_forecast_finetune=vix_forecast_finetune,
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

        # Merge Step 1 orphan closes into monitor so PnLReport can track them
        for strat, out in [('mrpt', mrpt_out), ('mtfs', mtfs_out)]:
            for close_ev in out.get('step1_closes', []):
                monitor[strat].append(close_ev)
                log.info(f"[monitor] Orphan close from Step 1: {close_ev['pair']} "
                         f"{close_ev['action']} pnl={close_ev.get('unrealized_pnl')}")

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
        ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        sig_path = os.path.join(SIGNALS_DIR,
                                f"combined_signals_{ts_str}.json")
        with open(sig_path, 'w') as f:
            json.dump(combined_out, f, indent=2, default=str)
        log.info(f"Combined signals saved → {sig_path}")

        # ── 生成详细报告 ───────────────────────────────────────────────────
        os.makedirs(REPORTS_DIR, exist_ok=True)
        report = build_full_report_json(
            strategy='both',
            signal_date=signal_date,
            total_capital=T,
            regime=regime,
            mrpt_out=mrpt_out,
            mtfs_out=mtfs_out,
            monitor=monitor,
        )
        rpt_json_path = os.path.join(REPORTS_DIR, f'daily_report_{ts_str}.json')
        with open(rpt_json_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"Report JSON saved → {rpt_json_path}")

        rpt_txt_path = os.path.join(REPORTS_DIR, f'daily_report_{ts_str}.txt')
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

    # Merge Step 1 orphan closes into monitor
    for close_ev in single_out.get('step1_closes', []):
        monitor[strategy].append(close_ev)
        log.info(f"[monitor] Orphan close from Step 1: {close_ev['pair']} "
                 f"{close_ev['action']} pnl={close_ev.get('unrealized_pnl')}")

    # ── 生成详细报告 ──────────────────────────────────────────────────────
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    report = build_full_report_json(
        strategy=strategy,
        signal_date=signal_date,
        total_capital=capital,
        regime=regime,
        single_out=single_out,
        monitor=monitor,
    )
    rpt_json_path = os.path.join(REPORTS_DIR, f'daily_report_{strategy}_{ts_str}.json')
    with open(rpt_json_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Report JSON saved → {rpt_json_path}")

    rpt_txt_path = os.path.join(REPORTS_DIR, f'daily_report_{strategy}_{ts_str}.txt')
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
                            f"{strategy}_signals_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(sig_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)
    log.info(f"[{strategy.upper()}] Signals saved → {sig_path}")

    # Track which pairs Step 1 closes (for orphan close detection)
    step1_closes = []
    if not dry_run:
        # Snapshot pre-update state to detect orphan closes
        pre_pairs = {k: v.get('direction') for k, v in inventory.get('pairs', {}).items()}
        updated_inv = update_inventory_from_signals(
            inventory, signals, end_date_str,
            strategy=strategy, pair_configs=pair_configs)
        save_inventory(updated_inv, strategy)
        print(f"\n  inventory_{strategy}.json updated for {end_date_str}")

        # Detect orphan closes: pairs that had direction before but null after,
        # where the close came from Step 1 signals (not monitor).
        # These need to be recorded so PnLReport can track them.
        for pair_key, old_dir in pre_pairs.items():
            if not old_dir:
                continue
            new_dir = updated_inv.get('pairs', {}).get(pair_key, {}).get('direction')
            if new_dir is not None:
                continue
            # This pair was closed by Step 1. Find the signal that closed it.
            close_sig = next((s for s in signals if s.get('pair') == pair_key
                              and s.get('action') in ('CLOSE', 'CLOSE_STOP')), None)
            if close_sig:
                step1_closes.append({
                    'pair':             pair_key,
                    'action':           close_sig.get('action'),
                    'direction':        old_dir,
                    'unrealized_pnl':   close_sig.get('unrealized_pnl', 0),
                    'unrealized_pnl_pct': close_sig.get('unrealized_pnl_pct'),
                    'note':             close_sig.get('note', 'Closed by Step 1 signal (not monitor)'),
                    'source':           'step1_signal',
                    'monitored':        False,
                })
    else:
        print(f"\n  [DRY RUN] inventory_{strategy}.json NOT updated")

    out['step1_closes'] = step1_closes
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
    'unrate_level':        ('失业率',                  'U.S. Unemployment Rate (monthly)',    lambda v: f'{v:.1f}%',        lambda v: '劳动市场偏紧' if v < 4.0 else ('偏松' if v > 5.0 else '正常')),
    'payems_mom':          ('非农就业变化(千人)',        'Nonfarm Payrolls MoM change (k)',     lambda v: f'{v:+.0f}k',       lambda v: '就业强劲' if v > 200 else ('就业疲弱' if v < 50 else '正常')),
    'icsa_level':          ('初领失业金(万人)',          'Initial Jobless Claims (weekly)',     lambda v: f'{v/10000:.1f}万', lambda v: '就业市场走弱' if v > 300000 else '正常'),
    'ccsa_level':          ('续领失业金(万人)',          'Continuing Claims (weekly)',          lambda v: f'{v/10000:.1f}万', lambda v: '失业延续偏高' if v > 1800000 else '正常'),
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
    oos_ref    = _get_oos_perf(strategy)
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
    ind_history = regime.get('indicator_history', {})
    enriched_indicators = {}
    for key, (name, description, fmt_fn, interp_fn) in _REGIME_INDICATOR_LABELS.items():
        val = indicators.get(key)
        if val is not None:
            try:
                entry = {
                    'name':           name,
                    'description':    description,
                    'raw_value':      round(val, 6) if isinstance(val, float) else val,
                    'formatted':      fmt_fn(val),
                    'interpretation': interp_fn(val),
                }
                if key in ind_history:
                    entry['history'] = ind_history[key]
                enriched_indicators[key] = entry
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
    key_inds = [
        'vix_level','vix_z','move_level','hy_spread_level','ig_spread_level',
        'yield_curve_level','effr_level','effr_1y_change','breakeven_10y_level',
        'fin_stress_level','nfci_level','consumer_sent_level','recession_flag',
        'nvda_20d','arkk_20d','soxx_20d','gld_20d','uso_20d','uup_20d','spy_20d',
        'unrate_level','payems_mom','icsa_level','ccsa_level',
    ]
    ind_data = regime_sec.get('indicators', {})

    def _fmt_chg(abs_v, pct_v, raw_v):
        """Format change string: show abs change and % change."""
        if abs_v is None:
            return 'n/a'
        try:
            # For small absolute values (spreads, rates) show abs; for % returns show pct only
            if pct_v is not None:
                sign = '+' if abs_v >= 0 else ''
                return f'{sign}{abs_v:.4g} ({pct_v:+.1%})'
            else:
                sign = '+' if abs_v >= 0 else ''
                return f'{sign}{abs_v:.4g}'
        except Exception:
            return 'n/a'

    def _fmt_vs(vs_pct):
        if vs_pct is None:
            return 'n/a'
        return f'{vs_pct:+.1%}'

    # ── Column layout (single line per indicator) ────────────────────────────
    # 指标(28) | 当前值+日期(18) | 前值+日期(18) | 变化(10) | 频(3) | 30均(10) | vs30(8) | 90均(10) | vs90(8) | 解读
    HDR_IND   = f'{"指标":<28}'
    HDR_CUR   = f'{"当前值(日期)":>18}'
    HDR_PREV  = f'{"前值(日期)":>18}'
    HDR_CHG   = f'{"变化":>10}'
    HDR_FREQ  = f'{"频":>3}'
    HDR_A30   = f'{"30obs均":>10}'
    HDR_D30   = f'{"vs30":>8}'
    HDR_A90   = f'{"90obs均":>10}'
    HDR_D90   = f'{"vs90":>8}'
    HDR_INTERP = '  解读'
    lines.append(f'  {HDR_IND}{HDR_CUR}{HDR_PREV}{HDR_CHG}{HDR_FREQ}{HDR_A30}{HDR_D30}{HDR_A90}{HDR_D90}{HDR_INTERP}')
    lines.append(f'  {"-"*122}')

    for k in key_inds:
        if k not in ind_data:
            continue
        d    = ind_data[k]
        name = d.get('name', k)
        fmtd = d.get('formatted', str(d.get('raw_value', '')))
        interp_str = d.get('interpretation', '')
        hist = d.get('history', {})

        if hist:
            freq      = hist.get('freq', '')
            freq_lbl  = {'daily': '日', 'weekly': '周', 'monthly': '月'}.get(freq, freq)
            prev_val  = hist.get('prev_val')
            cur_date  = hist.get('cur_date', '')[-5:]   # MM-DD
            prev_date = hist.get('prev_date', '')[-5:]  # MM-DD
            chg_abs   = hist.get('change_abs')
            avg30     = hist.get('avg30')
            avg90     = hist.get('avg90')
            raw_v     = d.get('raw_value')

            try:
                fmt_fn     = _REGIME_INDICATOR_LABELS[k][2]
                prev_fmtd  = fmt_fn(prev_val) if prev_val is not None else 'n/a'
                avg30_fmtd = fmt_fn(avg30)     if avg30   is not None else 'n/a'
                avg90_fmtd = fmt_fn(avg90)     if avg90   is not None else 'n/a'
                # Change string (formatted, with explicit + sign)
                if chg_abs is not None:
                    chg_fmtd = fmt_fn(chg_abs)
                    if chg_abs > 0 and not chg_fmtd.startswith('+'):
                        chg_fmtd = '+' + chg_fmtd
                    chg_str = chg_fmtd
                else:
                    chg_str = 'n/a'
                # vs30/vs90: absolute difference current - avg, same format
                vs30_str = vs90_str = 'n/a'
                if raw_v is not None and avg30 is not None:
                    d30 = raw_v - avg30
                    s = fmt_fn(d30)
                    vs30_str = ('+' + s if d30 > 0 and not s.startswith('+') else s)
                if raw_v is not None and avg90 is not None:
                    d90 = raw_v - avg90
                    s = fmt_fn(d90)
                    vs90_str = ('+' + s if d90 > 0 and not s.startswith('+') else s)
            except Exception:
                prev_fmtd  = str(prev_val) if prev_val is not None else 'n/a'
                avg30_fmtd = f'{avg30:.4g}' if avg30 is not None else 'n/a'
                avg90_fmtd = f'{avg90:.4g}' if avg90 is not None else 'n/a'
                chg_str  = f'{chg_abs:+.4g}' if chg_abs is not None else 'n/a'
                vs30_str = vs90_str = 'n/a'

            cur_cell  = f'{fmtd}({cur_date})'
            prev_cell = f'{prev_fmtd}({prev_date})'
            lines.append(
                f'  {name:<28}{cur_cell:>18}{prev_cell:>18}{chg_str:>10}{freq_lbl:>3}'
                f'{avg30_fmtd:>10}{vs30_str:>8}{avg90_fmtd:>10}{vs90_str:>8}  {interp_str}'
            )
        else:
            lines.append(
                f'  {name:<28}{fmtd:>18}{"":>18}{"":>10}{"":>3}{"":>10}{"":>8}{"":>10}{"":>8}  {interp_str}'
            )
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
        oos_ref_dict = _get_oos_perf(strat)
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
    _oos_mrpt = _get_oos_perf('mrpt')
    _oos_mtfs = _get_oos_perf('mtfs')
    mrpt_pnl_1m = sum(v.get('oos_pnl', 0) for v in _oos_mrpt.values() if not v.get('excluded'))
    mtfs_pnl_1m = sum(v.get('oos_pnl', 0) for v in _oos_mtfs.values() if not v.get('excluded'))
    mrpt_dd_1m  = min((v.get('oos_maxdd', 0) for v in _oos_mrpt.values() if not v.get('excluded')), default=0)
    mtfs_dd_1m  = min((v.get('oos_maxdd', 0) for v in _oos_mtfs.values() if not v.get('excluded')), default=0)

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
    reg_group.add_argument('--vix-forecast', action='store_true',
                           help='启用 VIX Chronos-2 预测信号（加权 10%% 进入 volatility score）')
    reg_group.add_argument('--vix-forecast-finetune', action='store_true',
                           help='VIX 预测使用 fine-tuned 模型（首次运行需额外约 2 分钟训练）')

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
        min_regime_weight    = args.min_regime_weight,
        skip_regime          = args.skip_regime,
        use_vix_forecast     = args.vix_forecast,
        vix_forecast_finetune= args.vix_forecast_finetune,
    )
