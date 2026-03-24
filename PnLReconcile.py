"""
PnLReconcile.py — 通用 PnL 对账脚本

用法:
  python PnLReconcile.py                         # 默认: 自动检测最近的 inventory + report
  python PnLReconcile.py --end 2026-03-24        # 指定截止日（含）
  python PnLReconcile.py --start 2026-03-13 --end 2026-03-24
  python PnLReconcile.py --decompose             # 开启差异拆解模式（yf价差分析）
  python PnLReconcile.py --end 2026-03-24 --decompose

输入源（自动检索，无需硬编码）:
  inventory_history/inventory_mrpt_*.json
  inventory_history/inventory_mtfs_*.json
  trading_signals/daily_report_*.json
"""

import argparse
import glob
import json
import os
import sys

import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INV_DIR  = os.path.join(BASE_DIR, 'inventory_history')
SIG_DIR  = os.path.join(BASE_DIR, 'trading_signals')


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 读取 inventory_history — 提取每个 pair 的持仓生命周期
# ═══════════════════════════════════════════════════════════════════════════════

def load_positions_from_inventory(start: str | None, end: str | None) -> list[dict]:
    """
    扫描 inventory_history 快照，提取在 [start, end] 范围内曾经活跃的所有持仓。
    每个 pair 只返回一条记录（最后一次出现的快照数据）。

    返回字段:
        pair, strategy, s1, s2, s1_shares, s2_shares,
        open_date, open_s1_price, open_s2_price, direction, param_set,
        open_signal, last_pnl (最后一条 monitor_log 的 unrealized_pnl),
        last_pnl_date, first_seen_date, last_seen_date
    """
    end_ts   = pd.Timestamp(end) if end else pd.Timestamp.now()
    start_ts = pd.Timestamp(start) if start else pd.Timestamp('1970-01-01')

    # pair -> {data_dict}  (latest snapshot wins)
    positions: dict[str, dict] = {}

    files = sorted(glob.glob(os.path.join(INV_DIR, 'inventory_*.json')))
    if not files:
        sys.exit(f'[ERROR] No inventory files found in {INV_DIR}')

    for fpath in files:
        # parse date from filename: inventory_mrpt_20260324_125629.json
        fname = os.path.basename(fpath)
        parts = fname.replace('.json','').split('_')
        # parts = ['inventory', 'mrpt'/'mtfs', 'YYYYMMDD', 'HHMMSS']
        if len(parts) < 4:
            continue
        strategy = parts[1]
        try:
            file_date = pd.Timestamp(parts[2])
        except Exception:
            continue

        if file_date < start_ts or file_date > end_ts:
            continue

        with open(fpath) as fp:
            data = json.load(fp)

        for pair_name, pair_data in data.get('pairs', {}).items():
            if not isinstance(pair_data, dict):
                continue
            direction = pair_data.get('direction')
            if not direction:
                # direction=null => currently flat; still record last pnl if it was open earlier
                if pair_name in positions and positions[pair_name].get('direction'):
                    # keep existing open record
                    pass
                continue

            tickers = pair_name.split('/')
            if len(tickers) != 2:
                continue
            s1, s2 = tickers

            monitor_log = pair_data.get('monitor_log', [])
            last_pnl = monitor_log[-1]['unrealized_pnl'] if monitor_log else None
            last_pnl_date = monitor_log[-1]['date'] if monitor_log else None

            rec = {
                'pair':           pair_name,
                'strategy':       strategy,
                's1':             s1,
                's2':             s2,
                's1_shares':      pair_data.get('s1_shares', 0),
                's2_shares':      pair_data.get('s2_shares', 0),
                'open_date':      pair_data.get('open_date'),
                'open_s1_price':  pair_data.get('open_s1_price'),
                'open_s2_price':  pair_data.get('open_s2_price'),
                'direction':      direction,
                'param_set':      pair_data.get('param_set', '—'),
                'open_signal':    pair_data.get('open_signal', {}),
                'last_pnl':       last_pnl,
                'last_pnl_date':  last_pnl_date,
                'snapshot_date':  str(file_date.date()),
            }
            # Latest snapshot overwrites earlier
            positions[pair_name] = rec

    return list(positions.values())


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 读取 daily_report — 提取 CLOSE/CLOSE_STOP 的实际执行 PnL
# ═══════════════════════════════════════════════════════════════════════════════

def load_close_pnl_from_reports(start: str | None, end: str | None) -> dict[str, dict]:
    """
    扫描 trading_signals/daily_report_*.json，收集 action=CLOSE/CLOSE_STOP 的记录。
    返回: { pair -> {action, unrealized_pnl, note, report_date, report_file} }
    最新的报告覆盖旧的（同一 pair 可能在不同日期平仓多次，各自保留）。
    实际返回最后一次 CLOSE 记录。
    """
    end_ts   = pd.Timestamp(end) if end else pd.Timestamp.now()
    start_ts = pd.Timestamp(start) if start else pd.Timestamp('1970-01-01')

    # pair -> list of (file_date, event_dict) for all CLOSE/CLOSE_STOP events
    all_events: dict[str, list] = {}

    files = sorted(glob.glob(os.path.join(SIG_DIR, 'daily_report_*.json')))
    for fpath in files:
        fname = os.path.basename(fpath)
        parts = fname.replace('.json', '').split('_')
        # parts = ['daily', 'report', 'YYYYMMDD', 'HHMMSS']
        if len(parts) < 4:
            continue
        try:
            file_date = pd.Timestamp(parts[2])                      # day-level
            file_ts   = pd.Timestamp(f'{parts[2]} {parts[3][:6]}')  # YYYYMMDD HHMMSS
        except Exception:
            continue

        if file_date < start_ts or file_date > end_ts:
            continue

        with open(fpath) as fp:
            data = json.load(fp)

        pm = data.get('position_monitor', {})
        for strat in ('mrpt', 'mtfs'):
            for entry in pm.get(strat, []):
                if not isinstance(entry, dict):
                    continue
                action = entry.get('action', '')
                if action not in ('CLOSE', 'CLOSE_STOP'):
                    continue
                pair = entry.get('pair')
                if not pair:
                    continue
                ev = {
                    'action':      action,
                    'pnl':         entry.get('unrealized_pnl'),
                    'note':        entry.get('note', ''),
                    'report_date': str(file_date.date()),
                    'report_file': fname,
                    'signal_date': data.get('signal_date', ''),
                    '_file_ts':    file_ts,
                }
                all_events.setdefault(pair, []).append(ev)

    # Also scan for HOLD events after any CLOSE — if a HOLD exists for a pair after its
    # last CLOSE, the position was re-opened and the CLOSE belongs to a prior position.
    # Use full timestamp (HHMMSS) to correctly order intraday files.
    hold_ts_map: dict[str, pd.Timestamp] = {}
    for fpath in files:
        fname = os.path.basename(fpath)
        parts = fname.replace('.json', '').split('_')
        if len(parts) < 4:
            continue
        try:
            file_date = pd.Timestamp(parts[2])
            file_ts   = pd.Timestamp(f'{parts[2]} {parts[3][:6]}')
        except Exception:
            continue
        if file_date < start_ts or file_date > end_ts:
            continue
        with open(fpath) as fp:
            data = json.load(fp)
        pm = data.get('position_monitor', {})
        for strat in ('mrpt', 'mtfs'):
            for entry in pm.get(strat, []):
                if not isinstance(entry, dict):
                    continue
                if entry.get('action') == 'HOLD':
                    pair = entry.get('pair')
                    if pair:
                        existing = hold_ts_map.get(pair, pd.Timestamp('1970-01-01'))
                        if file_ts > existing:
                            hold_ts_map[pair] = file_ts

    # For each pair, only keep CLOSE events that are NOT followed by a HOLD
    close_events: dict[str, dict] = {}
    for pair, evs in all_events.items():
        latest_hold = hold_ts_map.get(pair, pd.Timestamp('1970-01-01'))
        valid = [ev for ev in evs if ev['_file_ts'] > latest_hold]
        if valid:
            best = max(valid, key=lambda e: e['_file_ts'])
            close_events[pair] = best

    return close_events


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 读取 daily_report — 当前持仓最新 HOLD PnL
# ═══════════════════════════════════════════════════════════════════════════════

def load_hold_pnl_from_reports(end: str | None) -> dict[str, dict]:
    """最新一份 daily_report 中 HOLD 状态仓位的 unrealized_pnl。"""
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.now()

    files = sorted(glob.glob(os.path.join(SIG_DIR, 'daily_report_*.json')))
    # find latest file within range (use full timestamp)
    best_file, best_ts = None, pd.Timestamp('1970-01-01')
    for fpath in files:
        fname = os.path.basename(fpath)
        parts = fname.replace('.json', '').split('_')
        if len(parts) < 4:
            continue
        try:
            file_date = pd.Timestamp(parts[2])
            file_ts   = pd.Timestamp(f'{parts[2]} {parts[3][:6]}')
        except Exception:
            continue
        if file_date <= end_ts and file_ts > best_ts:
            best_ts = file_ts
            best_file = fpath
    best_date = best_ts

    if not best_file:
        return {}

    with open(best_file) as fp:
        data = json.load(fp)

    hold_pnl: dict[str, dict] = {}
    pm = data.get('position_monitor', {})
    for strat in ('mrpt', 'mtfs'):
        for entry in pm.get(strat, []):
            if not isinstance(entry, dict):
                continue
            if entry.get('action') == 'HOLD':
                pair = entry.get('pair')
                if pair:
                    hold_pnl[pair] = {
                        'pnl':         entry.get('unrealized_pnl'),
                        'report_date': str(best_date.date()),
                    }
    return hold_pnl


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 下载 yfinance 价格
# ═══════════════════════════════════════════════════════════════════════════════

_price_cache: pd.DataFrame | None = None

def download_prices(tickers: set[str], start: str, end: str) -> pd.DataFrame:
    global _price_cache
    # extend end by 1 day so yf includes the end date
    end_plus = str((pd.Timestamp(end) + pd.Timedelta(days=1)).date())
    print(f'Downloading prices for {len(tickers)} tickers ({start} → {end})...')
    data = yf.download(sorted(tickers), start=start, end=end_plus,
                       auto_adjust=True, progress=False)
    _price_cache = data['Close']
    return _price_cache

def get_price(prices: pd.DataFrame, ticker: str, dt_str: str) -> float | None:
    dt = pd.Timestamp(dt_str)
    avail = prices.index[prices.index <= dt]
    if len(avail) == 0:
        return None
    val = prices[ticker][avail[-1]]
    return float(val) if pd.notna(val) else None


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 主逻辑
# ═══════════════════════════════════════════════════════════════════════════════

def run(start: str | None, end: str | None, decompose: bool = False):
    print()
    print('=' * 80)
    print(f'  Someo Park — PnL 对账  |  范围: {start or "earliest"} → {end or "latest"}')
    print('=' * 80)

    # --- Load positions ---
    positions = load_positions_from_inventory(start, end)
    if not positions:
        print('[WARN] No active positions found in inventory for the given date range.')
        return

    close_events = load_close_pnl_from_reports(start, end)
    hold_pnl_map = load_hold_pnl_from_reports(end)

    # --- Determine evaluation date ---
    eval_date = end if end else str(pd.Timestamp.now().date())

    # --- Download prices ---
    all_tickers: set[str] = set()
    all_open_dates: list[str] = []
    for p in positions:
        all_tickers.add(p['s1'])
        all_tickers.add(p['s2'])
        if p['open_date']:
            all_open_dates.append(p['open_date'])

    price_start = min(all_open_dates) if all_open_dates else (start or '2026-01-01')
    prices = download_prices(all_tickers, price_start, eval_date)

    # --- Main table ---
    COL = [14, 6, 10, 6, 12, 12, 12, 12, 11]
    HDR = ('仓位', '策略', '状态', '方向', '开仓日', '系统PnL', 'yf重算PnL', '差异', '来源')

    print()
    print('  ' + '  '.join(h.ljust(w) for h, w in zip(HDR, COL)))
    print('  ' + '─' * (sum(COL) + 2 * len(COL)))

    totals = {'mrpt_realized': 0.0, 'mrpt_unrealized': 0.0,
              'mtfs_realized': 0.0, 'mtfs_unrealized': 0.0}

    decompose_rows: list[dict] = []

    for p in sorted(positions, key=lambda x: (x['strategy'], x['open_date'] or '')):
        pair      = p['pair']
        strategy  = p['strategy']
        s1, s2    = p['s1'], p['s2']
        s1_sh     = p['s1_shares']
        s2_sh     = p['s2_shares']
        op1       = p['open_s1_price']
        op2       = p['open_s2_price']
        open_dt   = p['open_date'] or '?'

        # Determine status and sys_pnl
        close_ev = close_events.get(pair)
        hold_ev  = hold_pnl_map.get(pair)

        if close_ev:
            status   = f"平仓({close_ev['report_date']})"
            sys_pnl  = close_ev['pnl']
            exit_dt  = close_ev['report_date']
            src      = close_ev['action']
        elif hold_ev:
            status   = f"持仓({hold_ev['report_date']})"
            sys_pnl  = hold_ev['pnl']
            exit_dt  = hold_ev['report_date']
            src      = 'HOLD'
        else:
            # Fallback to monitor_log last entry
            status   = f"持仓(inv)"
            sys_pnl  = p['last_pnl']
            exit_dt  = p['last_pnl_date'] or eval_date
            src      = 'monitor_log'

        # yf PnL (open at system price, close at yf close on exit_dt)
        exit_p1 = get_price(prices, s1, exit_dt)
        exit_p2 = get_price(prices, s2, exit_dt)

        yf_pnl: float | None = None
        if op1 and op2 and exit_p1 and exit_p2:
            open_val  = s1_sh * op1 + s2_sh * op2
            close_val = s1_sh * exit_p1 + s2_sh * exit_p2
            yf_pnl    = close_val - open_val

        diff: float | None = (sys_pnl - yf_pnl) if (sys_pnl is not None and yf_pnl is not None) else None

        # Format columns
        def fmt(v):
            if v is None: return 'N/A'
            return f'{v:+,.2f}'

        row = (
            pair,
            strategy.upper(),
            status,
            p['direction'] or '?',
            open_dt[5:] if open_dt and len(open_dt) >= 7 else open_dt,
            fmt(sys_pnl),
            fmt(yf_pnl),
            fmt(diff),
            src,
        )
        print('  ' + '  '.join(str(v).ljust(w) for v, w in zip(row, COL)))

        # Accumulate totals
        pnl_for_total = sys_pnl if sys_pnl is not None else (yf_pnl or 0.0)
        key_r = f'{strategy}_realized'
        key_u = f'{strategy}_unrealized'
        if close_ev:
            totals[key_r] = totals.get(key_r, 0.0) + pnl_for_total
        else:
            totals[key_u] = totals.get(key_u, 0.0) + pnl_for_total

        if decompose and op1 and op2:
            yf_op1 = get_price(prices, s1, open_dt)
            yf_op2 = get_price(prices, s2, open_dt)
            if yf_op1 and yf_op2 and exit_p1 and exit_p2:
                sys_open = s1_sh * op1 + s2_sh * op2
                yf_open  = s1_sh * yf_op1 + s2_sh * yf_op2
                close_val_yf = s1_sh * exit_p1 + s2_sh * exit_p2
                open_impact   = yf_open - sys_open
                pnl_yf_rebase = close_val_yf - sys_open
                settle_impact = (sys_pnl - pnl_yf_rebase) if sys_pnl is not None else None
                decompose_rows.append({
                    'pair': pair,
                    'open_impact':    open_impact,
                    'settle_impact':  settle_impact,
                    'sys_pnl':        sys_pnl,
                    'yf_rebase_pnl':  pnl_yf_rebase,
                })

    # --- Totals ---
    print('  ' + '─' * (sum(COL) + 2 * len(COL)))
    for strat in ('mrpt', 'mtfs'):
        r = totals.get(f'{strat}_realized', 0.0)
        u = totals.get(f'{strat}_unrealized', 0.0)
        subtotal = r + u
        print(f'  {strat.upper():<6}  已实现: {r:>+11,.2f}   未实现: {u:>+11,.2f}   小计: {subtotal:>+11,.2f}')

    grand = sum(totals.values())
    capital = 500000.0
    print(f'  {"合计":<6}  {grand:>+40,.2f}   占资本: {grand/capital*100:+.2f}%')

    # --- 开仓价格核对 ---
    print()
    print('=== 开仓价 核对（系统执行价 vs yf当日收盘）===')
    print()
    hdr2 = ('仓位', '开仓日', 'S1', '系统价', 'yf收盘', '差', 'S2', '系统价', 'yf收盘', '差')
    cw2  = (12, 10, 6, 9, 9, 8, 6, 9, 9, 8)
    print('  ' + '  '.join(h.ljust(w) for h, w in zip(hdr2, cw2)))
    print('  ' + '─' * (sum(cw2) + 2 * len(cw2)))

    for p in sorted(positions, key=lambda x: x['open_date'] or ''):
        if not p['open_date'] or not p['open_s1_price'] or not p['open_s2_price']:
            continue
        yf_p1 = get_price(prices, p['s1'], p['open_date'])
        yf_p2 = get_price(prices, p['s2'], p['open_date'])
        if yf_p1 is None or yf_p2 is None:
            continue
        d1 = p['open_s1_price'] - yf_p1
        d2 = p['open_s2_price'] - yf_p2
        row2 = (
            p['pair'],
            p['open_date'][5:],
            p['s1'],
            f"{p['open_s1_price']:.4f}",
            f'{yf_p1:.4f}',
            f'{d1:+.4f}',
            p['s2'],
            f"{p['open_s2_price']:.4f}",
            f'{yf_p2:.4f}',
            f'{d2:+.4f}',
        )
        print('  ' + '  '.join(str(v).ljust(w) for v, w in zip(row2, cw2)))

    # --- Decompose section ---
    if decompose and decompose_rows:
        print()
        print('=== 差异拆解（开仓价差 vs 结算时刻差）===')
        print()
        print('  方法：固定系统开仓价，只换结算价（yf收盘 vs 系统记录），隔离唯一变量')
        print()
        hdr3 = ('仓位', '开仓价差影响', '结算时刻差', '系统PnL', 'yf重算PnL')
        cw3  = (14, 14, 14, 13, 13)
        print('  ' + '  '.join(h.ljust(w) for h, w in zip(hdr3, cw3)))
        print('  ' + '─' * (sum(cw3) + 2 * len(cw3)))
        for row in decompose_rows:
            def fmtd(v): return f'{v:+,.2f}' if v is not None else 'N/A'
            cols = (
                row['pair'],
                fmtd(row['open_impact']),
                fmtd(row['settle_impact']),
                fmtd(row['sys_pnl']),
                fmtd(row['yf_rebase_pnl']),
            )
            print('  ' + '  '.join(str(c).ljust(w) for c, w in zip(cols, cw3)))

    print()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='通用 PnL 对账脚本：自动读取 inventory_history + daily_report')
    parser.add_argument('--start', default=None,
                        help='起始日期 YYYY-MM-DD（含，默认=最早可用）')
    parser.add_argument('--end',   default=None,
                        help='截止日期 YYYY-MM-DD（含，默认=今天）')
    parser.add_argument('--decompose', action='store_true',
                        help='启用差异拆解（开仓价差 vs 结算时刻差）')
    args = parser.parse_args()
    run(start=args.start, end=args.end, decompose=args.decompose)


if __name__ == '__main__':
    main()
