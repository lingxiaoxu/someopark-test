#!/usr/bin/env python3
"""
CorporateActions.py — 检测并应用 corporate actions（拆股/合股）到持仓 inventory。

背景（2026-06-12 KLAC 1:10 拆股事故）：
  Polygon / yfinance / Mongo 的价格在 split 后对全历史回溯调整（永远是"当前口径"），
  但 inventory_{strategy}.json 中的 s1_shares / open_s1_price 是开仓那一刻的口径，
  永不自动更新。两者相减的所有地方（monitor PnL、PnLReport MTM、RiskManager 敞口、
  UpdateStrategyPerformance 权益曲线）在 split 后全部失效 → 幻影巨亏/巨盈，
  可能触发假 Pair PnL Stop 强制平仓。

设计原则：
  修正 inventory 文件本身（一次性、持久化、留痕、幂等），所有下游自动正确——
  而不是在每个读取点做内存调整。

调整规则（split on leg X，factor = split_to / split_from）：
  sX_shares            × factor      （67 → 670）
  open_sX_price        ÷ factor      （2011.39 → 201.139）→ 成本基数与 PnL 美元值完全不变
  open_hedge_ratio     s1 拆股 ÷ factor；s2 拆股 × factor
                       （OLS slope 对全调整历史线性缩放）
  open_price_level_stop s1 拆股 ÷ factor；s2 拆股不变
                       （spread = p1 − hr×p2：s1 拆股 → spread ÷ factor；
                         s2 拆股 → hr×p2 乘除抵消 → spread 不变）
  peak_unrealized_pnl  不变（美元值）
  days_held / open_date / monitor_log  不变（历史记录保持原样）

幂等：每次应用在 pair 的 applied_corporate_actions 列表追加记录（含 polygon_id），
  应用前检查 id → 重跑绝不二次调整。

V1 范围：仅 splits（Polygon /v3/reference/splits）。现金分红不影响 Adj Close 体系
  下的对账口径；spinoff/换股合并等检测不到（API 为 splits 专用端点），
  如未来扩展用 detect_other_actions() 接口。

用法：
  python CorporateActions.py [--dry-run] [--strategy mrpt|mtfs|both]
  （DailySignal.run_daily_signal() 在 Step 1 monitor 之前自动调用，非致命降级）
"""

import os
import json
import shutil
import logging
import argparse
from copy import deepcopy
from datetime import datetime, timezone, date

import requests

log = logging.getLogger('corporate_actions')
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPLITS_CACHE_PATH = os.path.join(BASE_DIR, 'price_data', 'splits_cache.json')
CA_LOG_PATH = os.path.join(BASE_DIR, 'trading_signals', 'corporate_actions.log')
INVENTORY_HISTORY_DIR = os.path.join(BASE_DIR, 'inventory_history')
WEB_DATA_DIR = os.path.join(BASE_DIR, 'someo-park-investment-management', 'public', 'data')

_POLYGON_SPLITS_URL = 'https://api.polygon.io/v3/reference/splits'

# ── Mongo stock_data 口径分界（实测确定，2026-06-12）────────────────────────────
# Mongo 历史为批量载入（载入时已含 split 调整），之后 feeder 按 as-traded 追加。
# 实测探针：TSCO 2024-12-20 拆股处无断崖（已调整）；FAST 2025-05-22 起
# （ORLY/IBKR/HON/DD/NFLX/NOW/TPL/AMCR/BKNG/CVNA/KLAC）全部为断崖（as-traded）。
# → 只对 execution_date >= 此日期的 splits 做价格序列回溯调整，
#   更早的 splits 已体现在批量载入数据中，再调整 = 双重调整（灾难）。
# 若未来重新批量载入 Mongo，必须同步更新此常量。
MONGO_AS_TRADED_SINCE = '2025-05-01'

# 忽略微型"拆股"（股票股利，如 SCCO 1:1.0073、LEN 1000:1033）：
# 幅度 ≤5% 与日常波动同量级，且多落在口径分界模糊窗口内，misapply 风险 > 收益。
_MIN_SPLIT_DEVIATION = 0.05


# ── Inventory I/O（与 DailySignal 同约定：备份到 inventory_history + 同步前端） ──

def _inventory_path(strategy: str) -> str:
    return os.path.join(BASE_DIR, f'inventory_{strategy}.json')


def _load_inventory(strategy: str) -> dict:
    path = _inventory_path(strategy)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {'as_of': None, 'capital': 500_000, 'pairs': {}}


def _save_inventory(inv: dict, strategy: str):
    path = _inventory_path(strategy)
    if os.path.exists(path):
        os.makedirs(INVENTORY_HISTORY_DIR, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(INVENTORY_HISTORY_DIR, f'inventory_{strategy}_{ts}.json')
        shutil.copy2(path, backup)
        log.info(f'inventory_{strategy}.json backed up → {backup}')
    with open(path, 'w') as f:
        json.dump(inv, f, indent=2, default=str)
    log.info(f'inventory_{strategy}.json updated → {path}')
    if os.path.isdir(WEB_DATA_DIR):
        shutil.copy2(path, os.path.join(WEB_DATA_DIR, f'inventory_{strategy}.json'))
        log.info(f'inventory_{strategy}.json synced → {WEB_DATA_DIR}')


# ── 留痕日志（与 event_risk_heartbeat 同模式：每次检测一行，有动作详细记录） ──

def _ca_log(line: str):
    os.makedirs(os.path.dirname(CA_LOG_PATH), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(CA_LOG_PATH, 'a') as f:
        f.write(f'{ts} {line}\n')


# ── Splits 获取（Polygon）+ 本地 cache ─────────────────────────────────────────

def _load_splits_cache() -> dict:
    if os.path.exists(SPLITS_CACHE_PATH):
        try:
            with open(SPLITS_CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning('splits_cache.json unreadable — starting fresh')
    # cache 格式：{'fetched_at': 'YYYY-MM-DD', 'since': 'YYYY-MM-DD', 'results': [...]}
    return {'fetched_at': None, 'since': None, 'results': []}


def _save_splits_cache(cache: dict):
    os.makedirs(os.path.dirname(SPLITS_CACHE_PATH), exist_ok=True)
    with open(SPLITS_CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)


def fetch_all_splits(since_date: str, api_key: str | None = None,
                     use_cache: bool = True) -> list:
    """
    一次调用拿到 since_date 起的**全市场** splits（Polygon 支持无 ticker 查询，
    分页合并；含未来 execution_date 的预告 → 提前预警）。

    cache 策略：当天已查过且覆盖 since_date 则不重查（splits 公告提前数周，
    日检足够）。Polygon 查询失败 → 返回 cache 已有数据并告警（degrade gracefully）。
    """
    # Floor：cache 必须至少覆盖 Mongo as-traded 分界日，否则 adjust_price_df
    # 在长回看窗口（如 walk-forward 2 年）下会漏掉历史 splits。
    since_date = min(since_date, MONGO_AS_TRADED_SINCE)
    cache = _load_splits_cache() if use_cache else {}
    today_str = str(date.today())
    if (use_cache and cache.get('fetched_at') == today_str
            and cache.get('since', '9999') <= since_date):
        return cache.get('results', [])

    api_key = api_key or os.environ.get('POLYGON_API_KEY')
    if not api_key:
        log.warning('POLYGON_API_KEY missing — using cache only')
        return cache.get('results', [])

    results = []
    try:
        url = _POLYGON_SPLITS_URL
        params = {'execution_date.gte': since_date, 'limit': 1000, 'apiKey': api_key}
        while url:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            body = resp.json()
            results.extend(body.get('results', []))
            url = body.get('next_url')
            params = {'apiKey': api_key}  # next_url already carries the cursor
        if use_cache:
            _save_splits_cache({'fetched_at': today_str, 'since': since_date,
                                'results': results})
    except Exception as e:
        log.warning(f'Polygon market-wide splits query failed: {e} — using cached data')
        return cache.get('results', [])
    return results


def fetch_splits(tickers, since_date: str, api_key: str | None = None,
                 use_cache: bool = True) -> dict:
    """返回 {ticker: [split, ...]}（按 ticker 过滤的市场级结果）。"""
    all_splits = fetch_all_splits(since_date, api_key=api_key, use_cache=use_cache)
    tickers = set(tickers)
    out = {t: [] for t in tickers}
    for sp in all_splits:
        t = sp.get('ticker')
        if t in tickers:
            out[t].append(sp)
            log.warning(f'[SPLIT DETECTED] {t}: execution={sp.get("execution_date")} '
                        f'{sp.get("split_from")}:{sp.get("split_to")}')
    return out


def adjust_price_df(df, ticker: str, splits: list | None = None,
                    price_cols=('o', 'h', 'l', 'c'), volume_col: str = 'v'):
    """
    把 as-traded 价格序列（如 MongoDB stock_data：每日原样插入、split 后不回溯）
    调整为当前口径：对每个 execution_date 在序列范围内的 split，
    execution_date 之前的行 价格 ÷ factor、成交量 × factor。

    与 Polygon/yfinance 的回溯调整口径对齐 → 序列无断崖、与 Polygon fallback 一致。
    df 需以 DatetimeIndex 为索引；原地修改并返回 (df, n_applied)。
    splits 为 None 时读本地 cache（不发 API 请求）。
    """
    import pandas as pd
    if splits is None:
        all_cached = _load_splits_cache().get('results', [])
        splits = [s for s in all_cached if s.get('ticker') == ticker]
    n_applied = 0
    today_str = str(date.today())
    for sp in splits:
        ed = sp.get('execution_date')
        if not ed:
            continue
        if ed > today_str:
            continue  # 未来生效的 split：市场价格尚未换口径，绝不能提前应用
        if ed < MONGO_AS_TRADED_SINCE:
            continue  # 批量载入时代的 split 已体现在数据中 → 再调整 = 双重调整
        factor = float(sp['split_to']) / float(sp['split_from'])
        if abs(factor - 1.0) < _MIN_SPLIT_DEVIATION:
            continue  # 微型股票股利（≤5%）：与日常波动同量级，忽略
        mask = df.index < pd.Timestamp(ed)
        if not mask.any():
            continue  # 序列全部在 execution 当日及之后 → 已是新口径
        for col in price_cols:
            if col in df.columns:
                df.loc[mask, col] = df.loc[mask, col] / factor
        if volume_col and volume_col in df.columns:
            df.loc[mask, volume_col] = df.loc[mask, volume_col] * factor
        n_applied += 1
        log.warning(f'[SPLIT ADJUSTED] {ticker} price series: rows before {ed} '
                    f'÷{factor} (volume ×{factor})')
    return df, n_applied


# ── 检测与应用 ─────────────────────────────────────────────────────────────────

def _applied_ids(pair: dict) -> set:
    return {r.get('polygon_id') for r in pair.get('applied_corporate_actions', [])}


def detect_pending(inv: dict, splits_by_ticker: dict, today: str | None = None) -> list:
    """
    找出需要应用的 (pair_key, leg, split)：
      开仓日 < execution_date <= today，且 polygon_id 未在该 pair 留痕中。

    注意边界：execution_date 当天起价格源已是新口径（Polygon 全历史回溯调整
    在 execution 日生效，且我们的价格库重新拉取后整条历史变为新口径），
    因此 execution_date <= today 即应调整。
    """
    today = today or str(date.today())
    pending = []
    for pair_key, p in inv.get('pairs', {}).items():
        if not p.get('direction'):
            continue
        if '/' not in pair_key:
            continue
        s1, s2 = pair_key.split('/', 1)
        open_date = p.get('open_date', '')
        done = _applied_ids(p)
        for leg, ticker in (('s1', s1), ('s2', s2)):
            for sp in splits_by_ticker.get(ticker, []):
                ed = sp.get('execution_date', '')
                if not ed or sp.get('id') in done:
                    continue
                if open_date < ed <= today:
                    pending.append({'pair': pair_key, 'leg': leg,
                                    'ticker': ticker, 'split': sp})
    return pending


def _apply_split_to_pair(pair: dict, leg: str, ticker: str, split: dict,
                         applied_by: str = 'CorporateActions') -> dict:
    """对单个 pair 应用单个 split（原地修改），返回留痕记录。"""
    factor = float(split['split_to']) / float(split['split_from'])
    shares_key = f'{leg}_shares'
    price_key = f'open_{leg}_price'

    before = {shares_key: pair.get(shares_key),
              price_key: pair.get(price_key),
              'open_hedge_ratio': pair.get('open_hedge_ratio'),
              'open_price_level_stop': pair.get('open_price_level_stop')}

    # shares × factor（合股可能产生碎股 → 四舍五入并告警，现实中为 cash-in-lieu）
    old_sh = pair.get(shares_key, 0) or 0
    new_sh_f = old_sh * factor
    new_sh = int(round(new_sh_f))
    if abs(new_sh_f - new_sh) > 1e-9:
        log.warning(f'{ticker} split produces fractional shares '
                    f'({old_sh} × {factor} = {new_sh_f}) — rounded to {new_sh} '
                    f'(cash-in-lieu not modeled)')
    pair[shares_key] = new_sh

    # open price ÷ factor
    if pair.get(price_key):
        pair[price_key] = round(pair[price_key] / factor, 6)

    # hedge ratio：spread = p1 − hr×p2 → s1 拆股 hr ÷ factor；s2 拆股 hr × factor
    hr = pair.get('open_hedge_ratio')
    if hr is not None:
        pair['open_hedge_ratio'] = hr / factor if leg == 's1' else hr * factor

    # price level stop（spread 空间）：s1 拆股 → spread ÷ factor；s2 拆股 → 不变
    pls = pair.get('open_price_level_stop')
    if pls is not None and leg == 's1':
        pair['open_price_level_stop'] = pls / factor

    record = {
        'ticker': ticker, 'type': 'split',
        'execution_date': split.get('execution_date'),
        'factor': factor, 'polygon_id': split.get('id'),
        'applied_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'applied_by': applied_by,
        'before': before,
        'after': {shares_key: pair.get(shares_key),
                  price_key: pair.get(price_key),
                  'open_hedge_ratio': pair.get('open_hedge_ratio'),
                  'open_price_level_stop': pair.get('open_price_level_stop')},
    }
    pair.setdefault('applied_corporate_actions', []).append(record)
    return record


def apply_to_inventory(strategy: str, dry_run: bool = False,
                       api_key: str | None = None) -> dict:
    """
    检测并应用 splits 到 inventory_{strategy}.json。幂等。

    返回 report：{'strategy', 'checked_tickers', 'pending', 'applied', 'dry_run'}
    """
    inv = _load_inventory(strategy)
    open_pairs = {k: v for k, v in inv.get('pairs', {}).items() if v.get('direction')}
    if not open_pairs:
        _ca_log(f'{strategy}: no open positions — skip')
        return {'strategy': strategy, 'checked_tickers': [], 'pending': [],
                'applied': [], 'dry_run': dry_run}

    tickers = set()
    earliest_open = str(date.today())
    for pair_key, p in open_pairs.items():
        s1, s2 = pair_key.split('/', 1)
        tickers.update((s1, s2))
        od = p.get('open_date') or earliest_open
        earliest_open = min(earliest_open, od)

    splits = fetch_splits(tickers, since_date=earliest_open, api_key=api_key)
    pending = detect_pending(inv, splits)

    applied = []
    if pending:
        for item in pending:
            pair = inv['pairs'][item['pair']]
            if dry_run:
                factor = float(item['split']['split_to']) / float(item['split']['split_from'])
                log.info(f"[DRY-RUN] would apply {item['ticker']} split "
                         f"factor={factor} to {item['pair']} ({item['leg']})")
                continue
            rec = _apply_split_to_pair(pair, item['leg'], item['ticker'], item['split'])
            applied.append({'pair': item['pair'], **rec})
            log.warning(f"[CORPORATE_ACTION APPLIED] {item['pair']} {item['ticker']} "
                        f"split factor={rec['factor']}: "
                        f"{rec['before']} → {rec['after']}")
            _ca_log(f"{strategy}: APPLIED split {item['ticker']} "
                    f"factor={rec['factor']} pair={item['pair']} "
                    f"before={rec['before']} after={rec['after']}")
        if applied:
            _save_inventory(inv, strategy)
    else:
        _ca_log(f'{strategy}: checked {len(tickers)} tickers — no pending actions')

    return {'strategy': strategy, 'checked_tickers': sorted(tickers),
            'pending': pending, 'applied': applied, 'dry_run': dry_run}


# ── 历史快照读取时调整（不重写 inventory_history 文件） ───────────────────────

def adjust_position_view(pos: dict, s1: str, s2: str, snapshot_as_of: str = '',
                         splits_by_ticker: dict | None = None) -> dict:
    """
    返回 pos 的副本，把（历史快照中）旧口径的 shares/open_price 换算为当前
    （回溯调整后）价格源同口径。供 RiskManager.positions_on()（T-1D/W/M 对比）、
    PnLReport equity curve、UpdateStrategyPerformance.compute_pnl_mongo() 在用
    当前口径价格 MTM 历史快照仓位时调用。原快照文件不修改（审计完整性）。

    判定一个 split 是否需要应用（三重守卫，比单纯比较快照日期更稳健——
    execution 当日早晨的快照仍是旧口径，日期粒度无法区分调整前后）：
      1. polygon_id 不在 pos 的 applied_corporate_actions 留痕中
         （已被 apply_to_inventory 持久化调整过的快照自带 marker → 跳过）
      2. open_date < execution_date（仓位在拆股前开仓；拆股后开的仓天然新口径）
      3. execution_date <= today（未来生效的 split 价格源尚未调整，不能提前应用）

    snapshot_as_of 仅作日志参考；splits_by_ticker 为 None 时读本地 cache
    （不发 API 请求——cache 由当日 DailySignal 入口的 apply_to_inventory 维护）。
    """
    if splits_by_ticker is None:
        splits_by_ticker = {}
        for sp in _load_splits_cache().get('results', []):
            splits_by_ticker.setdefault(sp.get('ticker'), []).append(sp)
    out = deepcopy(pos)
    today_str = str(date.today())
    done = _applied_ids(pos)
    open_date = pos.get('open_date') or ''
    for leg, ticker in (('s1', s1), ('s2', s2)):
        for sp in splits_by_ticker.get(ticker, []):
            ed = sp.get('execution_date', '')
            if not ed or ed > today_str:
                continue                       # 未来生效 → 价格源还未调整
            if sp.get('id') and sp['id'] in done:
                continue                       # 该快照已是调整后口径
            if open_date and open_date >= ed:
                continue                       # 拆股后开仓 → 天然新口径
            factor = float(sp['split_to']) / float(sp['split_from'])
            sk, pk = f'{leg}_shares', f'open_{leg}_price'
            if out.get(sk):
                out[sk] = int(round(out[sk] * factor))
            if out.get(pk):
                out[pk] = out[pk] / factor
    return out


# ── 扩展接口（V2 占位）：非 split 类 corporate actions ─────────────────────────

def detect_other_actions(tickers, since_date: str) -> list:
    """
    V2 扩展点：spinoff / 换股合并 / 特殊分红等（需要 Polygon 其它端点或人工日历）。
    V1 不实现——检测到此类事件时应只告警、人工处理，绝不自动改仓。
    """
    return []


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Detect & apply stock splits to inventory')
    parser.add_argument('--strategy', choices=['mrpt', 'mtfs', 'both'], default='both')
    parser.add_argument('--dry-run', action='store_true',
                        help='Detect and report only; do not modify inventory')
    args = parser.parse_args()

    strategies = ['mrpt', 'mtfs'] if args.strategy == 'both' else [args.strategy]
    for strat in strategies:
        report = apply_to_inventory(strat, dry_run=args.dry_run)
        n_open = len(report['checked_tickers'])
        print(f"[{strat}] tickers={n_open} pending={len(report['pending'])} "
              f"applied={len(report['applied'])}{' (dry-run)' if args.dry_run else ''}")
        for a in report['applied']:
            print(f"  APPLIED {a['pair']} {a['ticker']} factor={a['factor']}")
        for p in report['pending'] if args.dry_run else []:
            print(f"  PENDING {p['pair']} {p['ticker']} {p['split']}")


if __name__ == '__main__':
    main()
