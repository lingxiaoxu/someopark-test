"""Read-only publisher for the isolated FAVE / PFME frontend.

python -m crypto_trading.ops.export_prediction_frontend --once
python -m crypto_trading.ops.export_prediction_frontend --watch 60

Reads source ledgers without importing a strategy or order client. The only
network capability is a Demo-host GET allowlist. Account snapshots are reused
for at least 180 seconds with their original source timestamp. Writes are
restricted to this publisher's private directory and its public namespace.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
CRYPTO = ROOT / 'crypto_trading'
SIGNALS = CRYPTO / 'trading_signals'
PRIVATE = SIGNALS / 'frontend_prediction'
PUBLIC = ROOT / 'someo-park-investment-management/public/data/crypto_prediction'
DEMO_BASE = 'https://external-api.demo.kalshi.co/trade-api/v2'
SERIES = {'fave': ('BTC', 'ETH', 'SOL', 'DOGE', 'XRP'), 'pfme': ('BTC', 'ETH', 'DOGE', 'XRP')}
ACCOUNT_INTERVAL = 180


def timestamp(value):
    if value is None: return None
    if isinstance(value, (int, float)): return float(value)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def iso(value):
    if value is None: return None
    return datetime.fromtimestamp(timestamp(value), timezone.utc).isoformat()


def number(value):
    if value is None: return None
    f = float(value)
    if not math.isfinite(f): raise ValueError('nonfinite numeric source')
    return f


def dec(value):
    n = Decimal(str(value))
    if not n.is_finite(): raise ValueError('nonfinite source amount')
    return n


def asset(ticker):
    return ticker.split('15M', 1)[0].removeprefix('KX')


def issue(source, code, detail):
    return dict(source=source, code=code, detail=detail)


def read_json(path):
    return json.loads(path.read_text())


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.tmp')
    try:
        with temp.open('w') as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            stream.flush(); os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def curve(rows):
    grouped = defaultdict(Decimal)
    for at, pnl in rows:
        if at is not None: grouped[iso(at)] += dec(pnl)
    total = Decimal(0); peak = Decimal(0); result = []
    for at, pnl in sorted(grouped.items()):
        total += pnl; peak = max(peak, total)
        result.append(dict(at=at, net_usd=float(pnl), cumulative_usd=float(total), drawdown_usd=float(total-peak)))
    return result


def performance(**kw):
    data = dict(status='unavailable', source_as_of=None, scope='', net_pnl_usd=None,
                fees_usd=None, settled_count=None, open_count=None, curve=[],
                sample_windows=None, verdict=None, unresolved_count=None, note='')
    data.update(kw); return data


def runtime(source, status, at, stale=180, detail=''):
    return dict(source=source, status=status, as_of=iso(at), stale_after_seconds=stale, detail=detail)


def verdict_text(v):
    if not v: return '固定 300 个独立窗口验收尚未形成结论'
    return ('固定验收通过' if v.get('passed') else '固定验收未通过') + '；' + str(v.get('windows', v.get('n_windows', 300))) + ' 个窗口'


def main_trade(row, registered):
    return (row.get('leg') == 'band' and .78 <= float(row['cost']) <= .98
            and row.get('opened') is not None and timestamp(row['opened']) >= timestamp(registered))


def read_w7_logs(log_dir, since):
    """Complete date range, including compressed rotations; no arbitrary row cap."""
    result = []; bad = 0
    day = datetime.fromtimestamp(since, timezone.utc).strftime('%Y-%m-%d')
    paths = {p.name[4:14]: p for p in sorted(log_dir.glob('log_*.jsonl.gz'))}
    paths.update({p.name[4:14]: p for p in sorted(log_dir.glob('log_*.jsonl'))})
    for d, p in sorted(paths.items()):
        if d < day: continue
        opener = gzip.open if p.suffix == '.gz' else open
        with opener(p, 'rt') as stream:
            for line in stream:
                if '"w7_noisefade"' not in line: continue
                try:
                    row = json.loads(line)
                    if row.get('strategy') == 'w7_noisefade' and timestamp(row['ts']) >= since:
                        result.append(row)
                except (ValueError, KeyError, TypeError): bad += 1
    return result, bad


def parse_receipt(row):
    try: receipt = json.loads(row.get('response', '{}'))
    except (ValueError, TypeError): return None
    return receipt if isinstance(receipt, dict) else None


def w7_sources(state, rows):
    paper = [r for r in rows if r.get('action') == 'paper_entry' and r.get('leg') == 'band' and .78 <= float(r['cost']) <= .98]
    by_ticker = {r['ticker']: r for r in paper}
    attempts = []
    for row in rows:
        if row.get('action') != 'demo_mirror_result': continue
        ticker = row.get('demo_ticker') or row.get('body_sent', {}).get('ticker')
        if ticker not in by_ticker: continue
        source = by_ticker[ticker]
        if row.get('env', 'demo') != 'demo': continue
        side = row.get('side') or source['side']
        body = row.get('body_sent', {})
        if body and (body.get('ticker') != ticker or body.get('side') != ('bid' if side == 'yes' else 'ask')):
            raise ValueError('FAVE demo order identity differs from its signal')
        receipt = parse_receipt(row)
        attempts.append({**row, '_ticker': ticker, '_side': side, '_source': source, '_receipt': receipt})
    return paper, attempts


class DemoReads:
    """GET-only, fixed Demo origin; credentials never leave request headers."""
    def __init__(self):
        import requests
        from crypto_trading.crypto_common.config import kalshi_key
        from crypto_trading.crypto_common.kalshi.auth import load_private_key
        self.session = requests.Session()
        self.key = kalshi_key('margin', borrowed_ok=True)
        self.private_key = load_private_key(self.key.expanded_path())
        self.last = 0.0

    def page(self, path, params):
        if path not in ('/portfolio/fills', '/portfolio/settlements'):
            raise ValueError('endpoint is outside the read-only allowlist')
        from crypto_trading.crypto_common.kalshi.auth import auth_headers
        delay = 1.0-(time.monotonic()-self.last)
        if delay > 0: time.sleep(delay)
        url_path = path + '?' + urlencode(params)
        headers = auth_headers(self.private_key, self.key.key_id, 'GET', '/trade-api/v2'+url_path)
        self.last = time.monotonic()
        response = self.session.get(DEMO_BASE+url_path, headers=headers, timeout=12)
        if response.status_code != 200:
            raise RuntimeError('Demo account GET returned HTTP '+str(response.status_code))
        return response.json()


def paged(reader, path, key, since):
    result = []; seen = set(); cursor = None
    for _ in range(100):
        params = dict(limit=1000, min_ts=int(since))
        if cursor: params['cursor'] = cursor
        data = reader.page(path, params)
        if not isinstance(data.get(key), list) or 'cursor' not in data:
            raise ValueError('account response shape is incomplete')
        result.extend(data[key]); cursor = data['cursor']
        if not cursor: return result
        if not isinstance(cursor, str) or cursor in seen: raise ValueError('account pagination cursor repeated')
        seen.add(cursor)
    raise ValueError('account pagination limit reached; incomplete results rejected')


def account_snapshot(attempts, since, now, private_dir=PRIVATE, reader_factory=DemoReads, offline=False):
    path = private_dir / 'verified_account.json'
    previous = read_json(path) if path.exists() else None
    ids = {r['_receipt']['order_id'] for r in attempts if r.get('_receipt') and r['_receipt'].get('order_id')}
    if previous and now-timestamp(previous['as_of']) < ACCOUNT_INTERVAL:
        return previous, []
    if offline:
        return previous, [issue('fave_account', 'OFFLINE', '本次没有请求账户接口；仅可查看保留原时间的已核验数据。')]
    control_path = private_dir / 'account_read_control.json'
    control = read_json(control_path) if control_path.exists() else {}
    if control.get('last_attempt') and now-timestamp(control['last_attempt']) < ACCOUNT_INTERVAL:
        return previous, [issue('fave_account', 'RETRY_WAIT', '上次核验尚未成功；至少间隔 180 秒再请求，已核验数据保留原时间。')]
    atomic_json(control_path, dict(last_attempt=iso(now)))
    try:
        reader = reader_factory()
        fills = [f for f in paged(reader, '/portfolio/fills', 'fills', since) if f.get('order_id') in ids]
        tickers = {f.get('ticker') or f.get('market_ticker') for f in fills}
        settlements = [s for s in paged(reader, '/portfolio/settlements', 'settlements', since) if s.get('ticker') in tickers]
        snapshot = dict(as_of=iso(time.time()), since=iso(since), order_ids=sorted(ids), fills=fills, settlements=settlements)
        atomic_json(path, snapshot)
        return snapshot, []
    except Exception as exc:
        # Do not include exception text from auth/files/network: it may contain paths or keys.
        return previous, [issue('fave_account', 'READ_FAILED', 'Demo 账户核验未完成（'+type(exc).__name__+'）；保留核验源时间，未核验记录不计收益。')]


def execution_stats(label, since, asof, signals, orders, attempts=(), note='', scope=''):
    accepted = [o for o in orders if o['status'] in ('filled','partial','zero_fill','unverified','resting','canceled','executed','accepted_unverified')]
    verified = [o for o in accepted if o['verified']]
    unknown = [o for o in orders if o['status'] in ('unknown','pending_send')]
    incomplete = len(verified) != len(accepted) or bool(unknown)
    filled = [o for o in verified if o['filled'] and o['filled'] > 0]
    quantity = sum(o['filled'] for o in filled)
    statuses = [r.get('status') for r in attempts]
    if incomplete: note += ' 尚有订单未完成官方核验；成交指标仅计已核验子集。'
    if unknown: note += f' {len(unknown)} 笔发送结果未知，不能解释成未成交或普通跳过。'
    return dict(label=label, since=iso(since), source_as_of=iso(asof), scope=scope, note=note,
                signals=len(signals), requested_contracts=sum(float(r.get('contracts', r.get('quantity', 0))) for r in signals),
                accepted=len(accepted), filled_orders=len(filled), full_fills=sum(abs(o['filled']-o['quantity']) < 1e-7 for o in filled),
                partial_fills=sum(o['filled'] < o['quantity']-1e-7 for o in filled),
                zero_fills=sum(o['filled'] == 0 for o in verified), filled_contracts=quantity,
                empty_side=statuses.count('empty_side'),
                unavailable=len(unknown)+sum(s in ('orderbook_unavailable', 'orderbook_stale', 'market_list_unavailable') for s in statuses),
                other_skips=max(0, len(signals)-len(accepted)-len(unknown)-statuses.count('empty_side')-sum(s in ('orderbook_unavailable', 'orderbook_stale', 'market_list_unavailable') for s in statuses)))


def attributable_fills(attempts, account):
    own = {}
    for r in attempts:
        receipt = r.get('_receipt') or {}
        if receipt.get('order_id'):
            if receipt['order_id'] in own: raise ValueError('duplicate FAVE order identity')
            own[receipt['order_id']] = r
    grouped = defaultdict(list); seen_fills = set()
    for f in (account or {}).get('fills', []):
        oid = f.get('order_id')
        if oid not in own: continue
        a = own[oid]; ticker = f.get('ticker') or f.get('market_ticker')
        if ticker != a['_ticker'] or f.get('outcome_side', f.get('side')) != a['_side']:
            raise ValueError('fill identity mismatch')
        identity = f.get('fill_id') or f.get('trade_id')
        if identity and identity in seen_fills: raise ValueError('duplicate official fill identity')
        if identity: seen_fills.add(identity)
        if dec(f['count_fp']) <= 0 or dec(f['fee_cost']) < 0 or not 0 <= dec(f[a['_side']+'_price_dollars']) <= 1:
            raise ValueError('invalid official fill values')
        grouped[oid].append(f)
    return own, grouped


def fave_demo(attempts, account, errors):
    own, grouped = attributable_fills(attempts, account)
    cached_ids = set((account or {}).get('order_ids', [])); asof = (account or {}).get('as_of')
    orders = []; by_market = defaultdict(list)
    settlements = {s['ticker']: s for s in (account or {}).get('settlements', [])}
    for oid, r in own.items():
        fills = grouped[oid]; qty = sum((dec(f['count_fp']) for f in fills), Decimal(0))
        expected = r['_receipt'].get('fill_count')
        verified = (oid in cached_ids and timestamp(r['ts']) < timestamp(asof)-30
                    and expected is not None and qty == dec(expected))
        cost = sum((dec(f['count_fp'])*dec(f[r['_side']+'_price_dollars']) for f in fills), Decimal(0))
        fees = sum((dec(f['fee_cost']) for f in fills), Decimal(0))
        price = r.get('price_dollars')
        if price is None and r.get('price_cents') is not None: price = float(r['price_cents'])/100
        quantity = float(r.get('contracts', 25))
        o = dict(id=oid, ticker=r['_ticker'], asset=asset(r['_ticker']), at=iso(r['ts']),
                 close_at=iso(r.get('prod_close') or r.get('close')), side=r['_side'], entry=True,
                 quantity=quantity, filled=float(qty) if verified else None, price=number(price),
                 cost_usd=float(cost) if verified else None, fees_usd=float(fees) if verified else None,
                 status=('filled' if qty == dec(quantity) else 'partial' if qty else 'zero_fill') if verified else 'unverified', verified=verified)
        orders.append(o)
        if verified: by_market[o['ticker']].append(o)
    # A timeout / HTTP 5xx after submission is not proof of zero execution.
    # Preserve identities without an exchange order_id as unresolved records.
    for r in attempts:
        if (r.get('_receipt') or {}).get('order_id') or not r.get('body_sent'): continue
        body = r['body_sent']; code = r.get('status_code')
        refused = isinstance(code, int) and 400 <= code < 500 and code not in (409, 429)
        quantity = float(r.get('contracts', body.get('count', 25)))
        price = r.get('price_dollars')
        if price is None and r.get('price_cents') is not None: price = float(r['price_cents'])/100
        orders.append(dict(id=body.get('client_order_id') or hashlib.sha256((r['_ticker']+r['ts']).encode()).hexdigest()[:20],
                           ticker=r['_ticker'], asset=asset(r['_ticker']), at=iso(r['ts']),
                           close_at=iso(r.get('prod_close') or r.get('close')), side=r['_side'], entry=True,
                           quantity=quantity, filled=0.0 if refused else None, price=number(price),
                           cost_usd=0.0 if refused else None, fees_usd=0.0 if refused else None,
                           status='rejected' if refused else 'unknown', verified=refused))
    closed = []; positions = []
    for ticker, os_ in by_market.items():
        quantity = sum(o['filled'] for o in os_)
        if not quantity: continue
        yes = sum(o['filled'] for o in os_ if o['side'] == 'yes'); no = quantity-yes
        cost = sum(o['cost_usd'] for o in os_); fees = sum(o['fees_usd'] for o in os_)
        s = settlements.get(ticker)
        if s and s.get('market_result') in ('yes', 'no'):
            payout = yes if s['market_result'] == 'yes' else no
            closed.append(dict(ticker=ticker, asset=asset(ticker), at=iso(s['settled_time']), result=s['market_result'],
                               quantity=quantity, cost_usd=cost, fees_usd=fees, payout_usd=payout, net_usd=payout-cost-fees))
        else:
            positions.append(dict(ticker=ticker, asset=asset(ticker), close_at=os_[0]['close_at'], yes_quantity=yes, no_quantity=no,
                                  paired_quantity=min(yes,no), net_quantity=yes-no, cost_usd=cost, fees_usd=fees, verified=True, source_as_of=asof, exit_status='等待官方结算' if timestamp(os_[0]['close_at']) < time.time() else '持有至结算'))
    uncertain_positions = {o['ticker']: o for o in orders if not o['verified']}
    for ticker, o in uncertain_positions.items():
        positions = [p for p in positions if p['ticker'] != ticker]
        positions.append(dict(ticker=ticker, asset=asset(ticker), close_at=o['close_at'], yes_quantity=None, no_quantity=None, paired_quantity=None, net_quantity=None, cost_usd=None, fees_usd=None, verified=False, source_as_of=asof, exit_status='发送或成交尚未核验；风险敞口未知'))
    incomplete = any(not o['verified'] for o in orders)
    p = performance(status='unavailable' if not account else 'partial' if incomplete or errors else 'verified', source_as_of=asof,
                    scope='MAIN 注册以来'+('（'+account['since']+' 起）' if account and account.get('since') else '')+'，按本策略 order_id 归属的 Demo 已结算成交；排除未核验订单与未结算仓位。',
                    net_pnl_usd=sum(r['net_usd'] for r in closed) if account else None,
                    fees_usd=sum(r['fees_usd'] for r in closed) if account else None,
                    settled_count=len(closed) if account else None, open_count=len(positions) if account else None, unresolved_count=sum(not o['verified'] for o in orders),
                    curve=curve((r['at'],r['net_usd']) for r in closed), note='官方 fills 核验数量、费用；官方 Demo settlement 提供结果。未核验记录不以纸面或回执金额补齐。')
    return p, orders, positions, closed


def pfme_demo(state, at):
    orders=[]; positions=[]; closed=[]
    for ticker, m in state['markets'].items():
        for o in m['orders']:
            verified = not o.get('read_error') and o.get('terminal') is True and (not o.get('filled') or bool(o.get('fills')))
            orders.append(dict(id=o.get('order_id') or o['client_order_id'], ticker=ticker, asset=asset(ticker), at=iso(o['submitted_ts']),
                               close_at=iso(m['close_ts']), side=o['side'], entry=bool(o['entry']), quantity=number(o['quantity']),
                               filled=number(o.get('filled')) if verified else None, price=number(o['price']), cost_usd=number(o.get('cost')) if verified else None,
                               fees_usd=number(o.get('fees')) if verified else None, status=o['status'], verified=verified))
        if m.get('settled'):
            quantity=sum(o.get('filled',0) for o in m['orders'])
            if quantity:
                closed.append(dict(ticker=ticker, asset=asset(ticker), at=iso(m['settled_ts']), result=m['result'], quantity=quantity,
                                   cost_usd=number(m['cost']), fees_usd=number(m['fees']), payout_usd=number(m['payout']), net_usd=number(m['net_pnl_usd'])))
        else:
            yes=sum(o.get('filled',0) for o in m['orders'] if o['side']=='yes'); no=sum(o.get('filled',0) for o in m['orders'] if o['side']=='no')
            if yes or no:
                positions.append(dict(ticker=ticker, asset=asset(ticker), close_at=iso(m['close_ts']), yes_quantity=yes, no_quantity=no,
                                      paired_quantity=min(yes,no), net_quantity=yes-no, cost_usd=sum(o['cost'] for o in m['orders']), fees_usd=sum(o['fees'] for o in m['orders']), verified=all(not o.get('read_error') and o.get('terminal') for o in m['orders']), source_as_of=iso(at),
                                      exit_status='退出持续重试' if m.get('exit_requested') else '持有至结算'))
    for ticker, o in {o['ticker']: o for o in orders if not o['verified']}.items():
        existing = next((p for p in positions if p['ticker']==ticker), None)
        if existing is not None:
            existing.update(yes_quantity=None,no_quantity=None,paired_quantity=None,net_quantity=None,cost_usd=None,fees_usd=None,verified=False,exit_status='订单核验未完成；风险敞口未知')
        else:
            positions.append(dict(ticker=ticker,asset=asset(ticker),close_at=o['close_at'],yes_quantity=None,no_quantity=None,paired_quantity=None,net_quantity=None,cost_usd=None,fees_usd=None,verified=False,source_as_of=iso(at),exit_status='订单核验未完成；风险敞口未知'))
    total=sum(r['net_usd'] for r in closed)
    if abs(total-state['net_pnl_usd']) > 1e-6: raise ValueError('PFME realized ledger identity mismatch')
    p=performance(status='partial' if any(not o['verified'] for o in orders) else 'verified', source_as_of=iso(at),
                  scope='当前 Demo 注册以来本策略全部已结算真实成交；入场与买对侧退出均计实际成本。', net_pnl_usd=total,
                  fees_usd=sum(r['fees_usd'] for r in closed), settled_count=len(closed), open_count=len(positions), unresolved_count=sum(not o['verified'] for o in orders),
                  curve=curve((r['at'],r['net_usd']) for r in closed), note='独立 Demo 镜像账本已按官方订单 / fills 对账，并使用 Demo 官方结算结果。')
    return p, orders, positions, closed


def paper_performance(sid, state, at):
    if sid=='fave':
        rows=[r for r in state['trades'] if main_trade(r,state['main_registered_at'])]
        windows=state['windows_main']; net=sum(float(w['sum_c'])/100*25 for w in windows.values())
        if abs(net-sum(float(r['pnl_c'])/100*25 for r in rows)) > 1e-5: raise ValueError('FAVE MAIN window ledger mismatch')
        return performance(status='verified', source_as_of=iso(at), scope='MAIN 注册后 $0.78–$0.98 的25张纸面交易；完全排除宽区间与观察组。',
                           net_pnl_usd=net, fees_usd=None, settled_count=len(rows), open_count=sum(main_trade(r,state['main_registered_at']) for r in state['positions'].values()),
                           curve=curve((k,float(v['sum_c'])/100*25) for k,v in windows.items()), sample_windows=len(windows), unresolved_count=0, verdict=verdict_text(state.get('verdict_main')),
                           note='Prod 盘口纸面成本和模型手续费；不是 Demo 已实现收益。源账本未逐笔单列手续费，不另造总费用。')
    book=state['books']['tilted']; rows=book['trades']; grouped=defaultdict(list)
    for r in rows: grouped[r['close_ts']].append(r)
    active={t:rs for t,rs in grouped.items() if any(r['fills'] for r in rs)}
    clean={t:rs for t,rs in active.items() if not any(r.get('unverified_order_quantity',0) for r in rs)}
    total=sum(r['net_usd'] for r in rows)
    if abs(total-book['cum_net_usd'])>1e-6: raise ValueError('PFME paper ledger mismatch')
    return performance(status='partial' if len(clean)!=len(active) else 'verified', source_as_of=iso(at),
                       scope='当前 tilted 注册的保守排队模型；不包含 paired 对照。', net_pnl_usd=total,
                       fees_usd=sum(r['fees_usd'] for r in rows), settled_count=sum(bool(r['fills']) for r in rows),
                       open_count=sum(bool(m.get('fills')) for m in book['positions'].values()),
                       curve=curve((t,sum(r['net_usd'] for r in rs)) for t,rs in grouped.items()), sample_windows=len(clean), unresolved_count=len(active)-len(clean),
                       verdict=verdict_text(state.get('verdicts',{}).get('tilted')),
                       note=f'有效窗口 {len(clean)}/{len(active)}；管理迟到但成交完整的窗口仍计验收。收益为全部已记账纸面交易，未核验窗口存在时标部分可验证。')


def pfme_observation_health(state, at, crypto=CRYPTO, *, now=None):
    """Describe retained observer evidence; never inspect/import its running code."""
    records=[]; issues=[]
    source_at=timestamp(state['last_tick_ts'])
    if source_at is None or not math.isfinite(source_at):
        raise ValueError('invalid observer health source time')
    now=time.time() if now is None else now
    source_fresh=0<=now-source_at<=300

    def report(label, status, detail, code=None, *, issue_detail=None):
        records.append(runtime(label,status,source_at,300,detail))
        if code: issues.append(issue('pfme_observer',code,detail if issue_detail is None else issue_detail))

    # Only this fixed local file is readable. Do not follow paths from state or
    # expose absolute paths / exception messages in the public health report.
    kernel=crypto/'crypto_strategies/event_binary/complete_set.py'
    try:
        registered=state.get('source_sha256',{}).get(str(kernel))
        if not isinstance(registered,str) or len(registered)!=64 or any(c not in '0123456789abcdef' for c in registered.lower()):
            raise ValueError('registered hash unavailable')
        current=hashlib.sha256(kernel.read_bytes()).hexdigest()
        changed=current!=registered.lower()
        detail=(f"注册于 {iso(state.get('registered_at'))} 的内核与当前磁盘文件"
                + ('字节哈希不同' if changed else '字节哈希一致')
                + f"；磁盘修改时间 {iso(kernel.stat().st_mtime)}；状态源时间 {iso(at)}。"
                + '未读取进程内存，不能据此确认运行代码，也不能推断差异仅为注释或交易逻辑变更。'
                + (' 应保留原注册记录；切换版本前需分开验证样本，不能覆盖哈希或直接重启消除此提示。' if changed else ''))
        report('PFME 源码一致性','partial' if changed else 'verified',detail,
               'SOURCE_HASH_MISMATCH' if changed else None)
    except (OSError,ValueError,TypeError,AttributeError):
        report('PFME 源码一致性','unavailable',
               f'注册哈希或固定内核文件无法核验；状态源时间 {iso(at)}。未推断运行代码版本。',
               'SOURCE_HASH_UNAVAILABLE')

    gaps=state.get('gaps')
    if not isinstance(gaps,list):
        report('PFME 最近一小时缺口','unavailable','源状态没有可验证的缺口列表；不将缺失记录解释为零缺口。','GAP_HISTORY_UNAVAILABLE')
    else:
        valid=[]; invalid=0
        for row in gaps:
            try:
                start=timestamp(row['start']); end=timestamp(row['end'])
                if start is None or end is None or not math.isfinite(start) or not math.isfinite(end) or not 0<=start<=end<=source_at:
                    raise ValueError('invalid gap time')
                if end>=source_at-3600: valid.append((start,end))
            except (KeyError,ValueError,TypeError,OverflowError): invalid+=1
        detail=(f'截至源心跳 {iso(source_at)}，按结束时间统计此前一小时：保留记录 {len(valid)} 条，'
                f'最长单条完整跨度 {max((end-start for start,end in valid),default=0):.2f} 秒。'
                + (f'最后缺口结束于 {iso(max(end for _,end in valid))}。' if valid else '')
                + '这是历史观察记录，包含管理迟到，不等于当前停机或已确认漏记成交。'
                + '源仅保留最近 1000 条，重叠记录未合并，不能当作完整历史或总停机时长；零条不证明行情无缺口。')
        if invalid: detail+=f' 另有 {invalid} 条时间不可验证，未计入上述数字。'
        historical_detail=detail
        current=[]
        cycle=state.get('last_cycle',{})
        # Inspect only the markets in the latest cycle. Old inputs are retained
        # after settlement and must never make a recovered market look broken.
        for series,market in (cycle.get('markets',{}) if isinstance(cycle,dict) else {}).items():
            if not isinstance(market,dict) or market.get('status') in ('NO_ACTIVE_MARKET','AWAITING_SETTLEMENT'): continue
            inp=state.get('inputs',{}).get(market.get('ticker'),{})
            delayed=False
            for key in ('last_book_ts','last_cycle_ts'):
                try:
                    observed=timestamp(inp.get(key))
                    delayed |= observed is not None and math.isfinite(observed) and 30<source_at-observed
                except (ValueError,TypeError,OverflowError): pass
            if (market.get('status') in ('DATA_ERROR','DISCOVERY_STALE','CATCHING_UP')
                    or inp.get('interval_complete') is False or delayed):
                current.append(series)
        if source_fresh and current:
            current_detail=(f'截至源心跳 {iso(source_at)}，当前周期 {", ".join(current)} 仍存在读取异常、'
                            '补读未完成或管理间隔超过 30 秒。应等待下一次完整读取确认；此状态本身不证明成交已漏记。')
            issues.append(issue('pfme_observer','CURRENT_OBSERVATION_GAP',current_detail))
            detail+=' '+current_detail
        report('PFME 最近一小时缺口','degraded' if source_fresh and current else 'partial' if invalid else 'recent_event' if valid else 'verified',detail,
               'GAP_HISTORY_PARTIAL' if invalid else 'RECENT_OBSERVATION_GAPS' if valid else None,issue_detail=historical_detail)

    control=state.get('http_read_control',{})
    try:
        requests=control['requests']; limited=control['rate_limited']
        if (not isinstance(requests,int) or isinstance(requests,bool) or not isinstance(limited,int)
                or isinstance(limited,bool) or not 0<=limited<=requests):
            raise ValueError('invalid HTTP counters')
        last=timestamp(control.get('last_rate_ts'))
        if limited and (last is None or not math.isfinite(last) or not 0<last<=source_at):
            raise ValueError('invalid HTTP event time')
        if not limited and last not in (None,0): raise ValueError('inconsistent HTTP event time')
        recent=bool(limited and last>=source_at-3600)
        resume=timestamp(state.get('http_resume_ts'))
        cooling=bool(source_fresh and state.get('status')=='RATE_LIMIT_BACKOFF'
                     and resume is not None and math.isfinite(resume) and resume>now)
        success=timestamp(control.get('last_success_ts'))
        success_known=success is not None and math.isfinite(success) and 0<success<=source_at
        detail=(f'截至源心跳 {iso(source_at)}，源进程累计请求 {requests} 次、限流 {limited} 次。'
                + (f'最后一次限流时间 {iso(last)}。' if limited else '源计数未记录限流。')
                + '累计计数不是最近一小时次数；源未提供逐次时间，不能恢复小时总次数。')
        historical_detail=detail
        if cooling:
            current_detail=f'截至快照检查，源进程处于限流冷却，计划等待至 {iso(resume)}；尚未确认后续成功读取。'
            issues.append(issue('pfme_observer','CURRENT_RATE_LIMIT_BACKOFF',current_detail))
            detail+=' '+current_detail
        elif recent:
            detail+= (' 最近一次限流后已记录成功读取：'+iso(success)+'；历史限流记录继续保留。'
                      if success_known and success>last else
                      ' 当前没有可确认的有效限流冷却；来源尚未提供限流后的成功读取时间，不据此宣称已恢复。')
            historical_detail=detail
        report('PFME HTTP 限流','degraded' if cooling else 'recent_event' if recent else 'verified',detail,'RECENT_RATE_LIMIT' if recent else None,issue_detail=historical_detail)
    except (KeyError,ValueError,TypeError,OverflowError):
        report('PFME HTTP 限流','unavailable','源限流计数或事件时间无法核验；未将缺失信息解释为零次限流。','RATE_LIMIT_HISTORY_UNAVAILABLE')
    return records,issues


def tail_json(path, max_bytes=1_500_000):
    if not path.exists(): return []
    with path.open('rb') as stream:
        size=path.stat().st_size; stream.seek(max(0,size-max_bytes)); lines=stream.read().splitlines()
    if size>max_bytes: lines=lines[1:]
    out=[]
    for line in lines:
        try: out.append(json.loads(line))
        except ValueError: continue
    return out


def best_ask(raw, side):
    ladder=raw['orderbook_fp']['no_dollars' if side=='yes' else 'yes_dollars']
    valid=[]
    for p,q in ladder:
        p=dec(p); q=dec(q)
        if not 0<p<1 or q<0: raise ValueError('invalid orderbook level')
        if q: valid.append((p,q))
    if not valid: return None, 0.0
    best=max(p for p,q in valid)
    return float(1-best), float(sum(q for p,q in valid if p==best))


def markets(sid, now, crypto=CRYPTO):
    result=[]; issues=[]
    for coin in SERIES[sid]:
        series='KX'+coin+'15M'; folder=crypto/'price_data/kalshi/event_strips/prod'/series
        days=[datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d'),datetime.fromtimestamp(now-86400,timezone.utc).strftime('%Y-%m-%d')]
        mrows=[]; brows=[]
        for day in days:
            mrows.extend(tail_json(folder/'markets'/f'{day}.jsonl')); brows.extend(tail_json(folder/'orderbook'/f'{day}.jsonl'))
        if not mrows:
            issues.append(issue(series,'MISSING_RECORDING','没有可读取的市场录制；未生成替代合约。')); continue
        latest=max(mrows,key=lambda r:r['recv_ts'])
        for m in latest.get('markets',[]):
            ticker=m['ticker']; close=m['close_time']; bookrows=[r for r in brows if r.get('ticker')==ticker and r.get('close_time')==close]
            quote=max(bookrows,key=lambda r:r['recv_ts']) if bookrows else None
            yes=no=yq=nq=None
            if quote:
                yes,yq=best_ask(quote['ob'],'yes'); no,nq=best_ask(quote['ob'],'no')
            source_at=quote['recv_ts'] if quote else latest['recv_ts']
            status=m.get('status','unknown')
            if now-source_at>240: status='stale_recording'
            result.append(dict(ticker=ticker,asset=coin,close_at=iso(close),threshold=number(m.get('floor_strike')),spot=number(latest.get('spot_est')),
                               yes_ask=yes,no_ask=no,yes_quantity=yq,no_quantity=nq,quote_at=iso(quote['recv_ts']) if quote else None,
                               quote_source='orderbook' if quote else 'unavailable',quote_status=('available' if yes is not None and no is not None else 'one_sided' if yes is not None or no is not None else 'empty') if quote else 'unavailable',status=status,result=m.get('result') if m.get('result') in ('yes','no') else None))
    return result,issues


def strategy_shell(sid):
    return dict(id=sid,acronym=sid.upper(),name='优势侧价值入场' if sid=='fave' else '被动优势侧入场',
                english_name='Favorite Value Entry' if sid=='fave' else 'Passive Favorite Market Entry',
                description='15分钟二元合约，在距结束约8分钟时买入优势侧；实时盘口定价，持有至官方结算。' if sid=='fave' else '15分钟二元合约，在注册时间和价格区间内被动挂优势侧；当前版本不执行完整集补腿。',
                version='unavailable',parameters=[],runtime=[],demo=performance(),paper=performance(),execution={'exits':{}},orders=[],positions=[],settlements=[],markets=[],issues=[])


def build_snapshot(*, now=None, signals=SIGNALS, crypto=CRYPTO, private_dir=PRIVATE, reader_factory=DemoReads, offline=False):
    live_clock=now is None
    now=time.time() if now is None else now; since48=now-48*3600
    snap=dict(schema_version=1,snapshot_id=hashlib.sha256(str(now).encode()).hexdigest()[:16],generated_at=iso(now),execution_environment='demo',market_data_environment='prod',prod_execution_enabled=False,issues=[],strategies={})
    for sid in ('fave','pfme'):
        out=strategy_shell(sid); snap['strategies'][sid]=out
        try:
            path=signals/'live_watch'/('w7_noisefade_state.json' if sid=='fave' else 'w8_complete_set_state.json')
            state=read_json(path); at=path.stat().st_mtime; out['version']=state['version']
            out['paper']=paper_performance(sid,state,at)
            if sid=='fave':
                start=timestamp(state['main_registered_at']); rows,bad=read_w7_logs(signals/'live_watch',start)
                paper,attempts=w7_sources(state,rows)
                if bad: out['issues'].append(issue('fave_logs','INVALID_LINES',f'{bad} 条相关日志无法解析；统计覆盖不完整。'))
                account,errs=account_snapshot(attempts,start,now,private_dir,reader_factory,offline)
                out['issues'].extend(errs)
                out['demo'],out['orders'],out['positions'],out['settlements']=fave_demo(attempts,account,out['issues'])
                allnew=[r for r in attempts if r.get('execution_version')=='w7_demo_book_v1']
                cutoff=min((timestamp(r['_source']['ts']) for r in allnew),default=now)
                for key,label,since in [('all','当前执行版本以来',cutoff),('recent','最近48h MAIN',since48)]:
                    ps=[r for r in paper if timestamp(r['ts'])>=since]; os_=[o for o in out['orders'] if timestamp(o['at'])>=since]
                    ats=[r for r in attempts if timestamp(r['ts'])>=since]
                    out['execution'][key]=execution_stats(label,since,(account or {}).get('as_of'),ps,os_,ats,
                        note='分母为 MAIN 入场信号。旧无报价日志缺少合约身份，只列未分类跳过；不可当作确认空盘口。',scope='MAIN paper 信号与 exact ticker / order_id 对应的 Demo 执行。')
                for key,label,since in [('all','当前执行版本以来',cutoff),('recent','最近48小时',since48)]:
                    out['execution']['exits'][key]=execution_stats(label,since,(account or {}).get('as_of'),[],[],note='当前 FAVE 没有主动退出路径，持有至官方结算。',scope='退出订单数量完整为零。')
                if bad:
                    for key in ('all','recent'):
                        out['execution'][key]['signals']=None; out['execution'][key]['requested_contracts']=None; out['execution'][key]['other_skips']=None
                        out['execution'][key]['note']+=' 相关日志有无法解析的行，信号总数与分母不可确认；订单指标仅为可验证子集。'
                out['parameters']=[dict(label=k,value=v) for k,v in [('入场区间','$0.78–$0.98'),('信号规模','25 张'),('入场时间','距结束 8 分钟 ± 0.6 分钟'),('执行方式','实时盘口 · IOC'),('持仓管理','持有至官方结算'),('费用','Demo 官方实际成交费用')]]
                out['runtime']=[runtime('FAVE 状态文件','stale' if now-at>300 else 'updated',at,300,'文件更新时间，不冒充进程心跳。'),
                                runtime('Demo 账户核验','unavailable' if not account else 'degraded' if errs else 'verified',(account or {}).get('as_of'),360,'独立只读 GET；与观察进程无写入连接。')]
            else:
                p=signals/'w8_demo_mirror/state.json'; demo=read_json(p)
                if demo.get('mode')!='demo' or demo['version']!=state['version']: raise ValueError('PFME Demo environment or version mismatch')
                out['demo'],out['orders'],out['positions'],out['settlements']=pfme_demo(demo,demo['last_heartbeat'])
                for key,label,since in [('all','当前注册全部',timestamp(demo['started_at'])),('recent','最近48小时',since48)]:
                    os_=[o for o in out['orders'] if o['entry'] and timestamp(o['at'])>=since]
                    sig=[dict(quantity=o['quantity']) for o in os_]
                    out['execution'][key]=execution_stats(label,since,demo['last_heartbeat'],sig,os_,note='分母为镜像实际入场发送尝试；不把未镜像的纸面挂单当发单。买对侧退出单独显示在订单账本。',scope='PFME Demo 入场订单；退出不计入入场成交率。')
                    out['execution'][key]['empty_side']=None; out['execution'][key]['unavailable']=None; out['execution'][key]['other_skips']=None
                    exits=[o for o in out['orders'] if not o['entry'] and timestamp(o['at'])>=since]
                    out['execution']['exits'][key]=execution_stats(label,since,demo['last_heartbeat'],[dict(quantity=o['quantity']) for o in exits],exits,note='仅实际发送的买对侧退出订单；空簿重试检查未发送订单，不列为发单。',scope='PFME Demo 主动退出发送记录。')
                    out['execution']['exits'][key]['empty_side']=None; out['execution']['exits'][key]['unavailable']=None; out['execution']['exits'][key]['other_skips']=None
                heartbeat=timestamp(state['last_tick_ts']); status=state.get('status','unknown')
                if now-heartbeat>300: status='STALE'
                detail='实际观察状态；限流等待期间不能理解为新盘口更新。'
                if state.get('http_resume_ts',0)>now: detail+=' 冷却至 '+iso(state['http_resume_ts'])
                out['runtime']=[runtime('PFME 观察',status,heartbeat,300,detail),runtime('PFME Demo 镜像',demo.get('observer_status','unknown'),demo['last_heartbeat'],90,'真实订单核验账本；未确认订单继续保留。')]
                # FAVE's account read can take seconds before we read PFME.
                # Compare PFME's heartbeat to the actual check time, not the
                # earlier snapshot start. Explicit test clocks remain fixed.
                health,health_issues=pfme_observation_health(state,at,crypto,now=time.time() if live_clock else now)
                out['runtime'].extend(health); out['issues'].extend(health_issues)
                params=state['parameters']
                out['parameters']=[dict(label=k,value=v) for k,v in [('入场区间',f"${params['entry_band_lo']:.2f}–${params['entry_band_hi']:.2f}"),('入场时间',f"距结束 {params['entry_rem_lo_s']/60:g}–{params['entry_rem_hi_s']/60:g} 分钟"),('每单规模',f"{params['clip']:g} 张"),('单市场净仓上限',f"{params['max_net']:g} 张"),('挂单有效期',f"{params['quote_ttl_s']:g} 秒"),('执行方式','post-only 被动入场；风险退出 IOC'),('完整集补腿','当前主策略关闭'),('结算','Demo 官方结果；持续重试风险退出')]]
            out['markets'],market_issues=markets(sid,now,crypto); out['issues'].extend(market_issues)
        except Exception as exc:
            out['issues'].append(issue(sid,'SOURCE_INVALID','数据源无法完整验证（'+type(exc).__name__+'）；没有使用替代数据。'))
        for key,label,since in [('all','全部可验证期间',None),('recent','最近48小时',since48)]:
            if key not in out['execution']:
                out['execution'][key]=dict(label=label,since=iso(since),source_as_of=None,scope='不可用',note='源数据未通过验证。',**{k:None for k in ('signals','requested_contracts','accepted','filled_orders','full_fills','partial_fills','zero_fills','filled_contracts','empty_side','unavailable','other_skips')})
        for key in ('all','recent'):
            if key not in out['execution']['exits']:
                out['execution']['exits'][key]=dict(out['execution'][key],note='退出数据不可用。')
        out['orders'].sort(key=lambda r:r['at'] or '',reverse=True); out['settlements'].sort(key=lambda r:r['at'] or '',reverse=True)
    return snap


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true'); parser.add_argument('--watch',type=int,metavar='SECONDS')
    parser.add_argument('--offline',action='store_true',help='Explicitly disable account GETs; retain any cache source time and mark it offline.')
    args=parser.parse_args(argv)
    if args.watch is not None and args.watch<60: parser.error('--watch must be at least 60 seconds')
    while True:
        started=time.monotonic(); snapshot=build_snapshot(offline=args.offline)
        atomic_json(PRIVATE/'snapshot.json',snapshot); atomic_json(PUBLIC/'snapshot.json',snapshot)
        print(json.dumps(dict(generated_at=snapshot['generated_at'],strategies={k:dict(status=v['demo']['status'],orders=len(v['orders']),issues=len(v['issues'])) for k,v in snapshot['strategies'].items()})),flush=True)
        if not args.watch: return
        time.sleep(max(1,args.watch-(time.monotonic()-started)))


if __name__=='__main__': main()
