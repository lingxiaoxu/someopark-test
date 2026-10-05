"""Read-only publisher for the isolated FAVE / PFME frontend.

python -m crypto_trading.ops.export_prediction_frontend --once
python -m crypto_trading.ops.export_prediction_frontend --watch 60

Reads source ledgers without importing a strategy or order client. The only
network capability is a Demo-host GET allowlist plus one public, unauthenticated
Kalshi market-record GET for a closed window the strike identity cannot settle
(official_result; cached, rate limited). Exact prod fees/costs come from LOCAL caches
written by the separate read-only ops/prod_fill_sync job (this publisher still makes
no prod-account call). Account snapshots are reused
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
import re
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
    n = v.get('signal_windows', v.get('windows', v.get('n_windows', 300)))
    return ('固定验收通过' if v.get('passed') else '固定验收未通过') + '；' + str(n) + ' 个窗口'


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
                    scope='MAIN 注册以来的已核验 Demo 结算。',
                    net_pnl_usd=sum(r['net_usd'] for r in closed) if account else None,
                    fees_usd=sum(r['fees_usd'] for r in closed) if account else None,
                    settled_count=len(closed) if account else None, open_count=len(positions) if account else None, unresolved_count=sum(not o['verified'] for o in orders),
                    curve=curve((r['at'],r['net_usd']) for r in closed), note='按官方成交与结算核验。')
    return p, orders, positions, closed


def _ticker_close_ts(ticker):
    """KX<COIN>15M-26SEP301330-30 → 收盘 epoch(票面为美东挂钟时间)。"""
    try:
        from zoneinfo import ZoneInfo
        part = ticker.split('-')[1]
        d = datetime.strptime(part[:-4], '%y%b%d')
        return datetime(d.year, d.month, d.day, int(part[-4:-2]), int(part[-2:]),
                        tzinfo=ZoneInfo('America/New_York')).timestamp()
    except Exception:
        return None


def _order_detail(r, rc=None, fill=None, avg=None):
    """回执+执行审计 → 订单详情;只收录真实存在的字段,None 一律剔除。"""
    body = r.get('body_sent') or {}
    fg = r.get('flow_gate') or {}
    nd = r.get('no_dump') or {}
    side = r.get('side')
    det = dict(
        client_order_id=(rc or {}).get('client_order_id') or body.get('client_order_id'),
        venue_ts=iso(float((rc or {}).get('ts_ms'))/1000.0) if (rc or {}).get('ts_ms') else None,
        avg_fill_price=(round(number(avg if side == 'yes' else 1-avg), 4)
                        if (avg is not None and fill) else None),
        remaining_count=number((rc or {}).get('remaining_count')),
        http_status=r.get('status_code') if isinstance(r.get('status_code'), int) else None,
        tif=r.get('tif') or body.get('time_in_force'),
        stp=body.get('self_trade_prevention_type'),
        role='taker' if fill else None,
        paper_price=number(r.get('paper_price_dollars')),
        buffer_c=r.get('limit_buffer_c') if isinstance(r.get('limit_buffer_c'), (int, float)) else None,
        trend_bp=number(nd.get('trend_bp', nd.get('trend60_bp'))),
        size_mult=number(nd.get('mult')),
        gate=fg.get('decision') or ('skip' if r.get('status') == 'skipped_by_flow_gate' else None),
        gate_flow=number(fg.get('aligned_observed_flow_1m') if fg.get('aligned_observed_flow_1m') is not None
                         else r.get('aligned_observed_flow_1m')),
        gate_momentum=number(fg.get('aligned_momentum_1m_bp') if fg.get('aligned_momentum_1m_bp') is not None
                             else r.get('aligned_momentum_1m_bp')),
    )
    det = {k: v for k, v in det.items() if v is not None}
    return det or None


_STRIKES = {}                     # orderbook recording path -> (size, {close_time: strike})
_CLOSE_RE = re.compile(rb'"close_time": ?"([^"]+)"'); _STRIKE_RE = re.compile(rb'"strike": ?([-+0-9.eE]+)')


def _window_strikes(series, day, crypto=CRYPTO):
    """{close_time: strike} for one series/UTC day from the strips orderbook recording
    (live .jsonl or rotated .jsonl.gz); re-read only when the file has grown."""
    folder = crypto/'price_data/kalshi/event_strips/prod'/series/'orderbook'
    path = next((folder/n for n in (f'{day}.jsonl', f'{day}.jsonl.gz') if (folder/n).exists()), None)
    if path is None: return {}
    size = path.stat().st_size
    hit = _STRIKES.get(str(path))
    if hit and hit[0] == size: return hit[1]
    out = {}
    opener = gzip.open if path.suffix == '.gz' else open
    try:
        with opener(path, 'rb') as fh:
            for line in fh:
                m = _CLOSE_RE.search(line); k = _STRIKE_RE.search(line)
                if m and k and m.group(1) not in out:
                    try: out[m.group(1).decode()] = float(k.group(1))
                    except ValueError: continue
    except OSError:
        return {}
    _STRIKES[str(path)] = (size, out)
    return out


def strike_identity_result(ticker, crypto=CRYPTO, now=None):
    """Official result for a closed 15M market the paper ledger does not hold (prod traded a
    (ticker, side) the paper never took, which the table-driven bins allow since 2026-10-02):
    the NEXT window's strike IS this window's settlement price (60 s BRTI average), so
    next > strike -> 'yes', next < strike -> 'no'. Equal strikes, a missing next window or a
    market not yet closed -> None (the leg stays open until it can be settled)."""
    cts = _ticker_close_ts(ticker)
    if cts is None or cts > (time.time() if now is None else now) - 30: return None
    series = ticker.split('-')[0]
    key = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    strikes = {}
    for t in (cts - 900, cts, cts + 900):          # a window's rows are recorded on the day it is OPEN: a 00:00 close lives in the previous day's file
        strikes.update(_window_strikes(series, key(t)[:10], crypto))
    k, kn = strikes.get(key(cts)), strikes.get(key(cts + 900))
    if k is None or kn is None or kn == k: return None
    return 'yes' if kn > k else 'no'


_OFFICIAL = {}                    # cache path -> {ticker: 'yes'|'no'}; a finalized result never changes
_OFFICIAL_RETRY = {}              # ticker -> earliest next lookup after an unfinalized / failed GET
OFFICIAL_LOOKUPS_PER_MIN = 4
_OFFICIAL_BUDGET = {'at': 0.0, 'n': 0}


def _fetch_official(ticker):
    """Public unauthenticated market record, copied from w7_noisefade.official_result with a
    short timeout so the publisher never stalls on the venue: 'yes'/'no' once finalized."""
    import requests
    from crypto_trading.crypto_common.kalshi.enums import rest_base
    try:
        r = requests.get(rest_base('prod') + f'/markets/{ticker}', timeout=4,
                         headers={'User-Agent': 'someopark-crypto/0.1'})
        if r.status_code == 200:
            res = r.json().get('market', {}).get('result')
            return res if res in ('yes', 'no') else None
    except (requests.RequestException, ValueError):
        pass
    return None


def official_result(ticker, crypto=CRYPTO, now=None, fetch=None):
    """Kalshi's own finalized result for a closed market strike_identity_result cannot settle.
    Consecutive windows carry the identical strike when the venue's reference price stalls
    (2 of 664 windows in the 14 days to 2026-10-04, both on 2026-10-03; equality settled YES),
    so the identity returns None and the leg would stay '等待官方结算' forever. Final results are
    cached in frontend_prediction/official_results.json; an unfinalized or failed lookup is
    retried after 300 s; at most OFFICIAL_LOOKUPS_PER_MIN network calls per minute."""
    now = time.time() if now is None else now
    cts = _ticker_close_ts(ticker)
    if cts is None or cts + 120 > now: return None            # finalization lags the close by minutes
    path = crypto/'trading_signals/frontend_prediction/official_results.json'
    cache = _OFFICIAL.get(str(path))
    if cache is None:
        try: cache = {k: v for k, v in json.loads(path.read_text()).items() if v in ('yes', 'no')}
        except (OSError, ValueError, AttributeError): cache = {}
        _OFFICIAL[str(path)] = cache
    if ticker in cache: return cache[ticker]
    if _OFFICIAL_RETRY.get(ticker, 0) > now: return None
    if now - _OFFICIAL_BUDGET['at'] >= 60: _OFFICIAL_BUDGET.update(at=now, n=OFFICIAL_LOOKUPS_PER_MIN)
    if _OFFICIAL_BUDGET['n'] <= 0: return None
    _OFFICIAL_BUDGET['n'] -= 1
    res = (fetch or _fetch_official)(ticker)
    if res not in ('yes', 'no'):
        _OFFICIAL_RETRY[ticker] = now + 300
        return None
    cache[ticker] = res
    try: atomic_json(path, dict(cache))
    except Exception: pass
    return res


PROD_FILLS_OWNER = PRIVATE / 'prod_fills_owner.json'


def load_exact_fills(path):
    """order_id -> {count, cost, fees} from the read-only prod fill sync (ops/prod_fill_sync).

    The receipts' average_fee_paid / average_fill_price are rounded to 4 decimals and
    under-count fees by a fraction of a cent per order; Kalshi's own fills are exact.
    Missing or unreadable cache = {} (every order keeps its receipt numbers)."""
    try:
        if not Path(path).exists(): return {}
        orders = read_json(Path(path)).get('orders')
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for oid, a in (orders if isinstance(orders, dict) else {}).items():
        try:
            out[oid] = dict(count=float(a['count']), cost=float(a['cost']), fees=float(a['fees']))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _exact_cost_fee(exact, oid, fill):
    """(cost_usd, fees_usd) from Kalshi's fills when they cover exactly this receipt's fill count."""
    x = (exact or {}).get(oid)
    if x and fill > 0 and abs(x['count']-fill) < 1e-6:
        return x['cost'], x['fees']
    return None


def prod_results(keys, state, crypto=CRYPTO, now=None):
    """(ticker, side) -> (win, closed_at). The paper ledger's official result first (same market,
    same side, as before); a prod leg the paper never held settles by strike_identity_result
    once its market has closed, timestamped at the market close; the venue's own record
    (official_result) decides the rare window the identity cannot (equal strikes)."""
    res = {(t['ticker'], t['side']): (bool(t.get('win')), t.get('closed'))
           for t in state.get('trades', []) if t.get('pnl_c') is not None}
    for tk, side in set(keys):
        if (tk, side) in res: continue
        r_ = strike_identity_result(tk, crypto, now) or official_result(tk, crypto, now)
        if r_ in ('yes', 'no'):
            res[(tk, side)] = (side == r_, iso(_ticker_close_ts(tk)))
    return res


def fave_prod_ledger(rows, state, crypto=CRYPTO, now=None, exact=None):
    """实盘账本三件套(orders/positions/settlements)——取代 FAVE 视图里的
    Demo 账本(用户指令 2026-09-30:artifact 视图全部改 prod)。回执是交易所
    同步答复;结算结果经纸面账本取官方 result,纸面没有同一 ticker/方向的腿时
    (按表下单的时点纸面可能不在价带)按下一窗行权价=本窗结算价的恒等式结算
    (见 strike_identity_result);闸口跳单以 status='skipped' 入列,保持跟踪
    差异可见;启动自检合成行(ticker 含 LIVE)不入账。"""
    sent = [r for r in rows if r.get('action') == 'live_order_result' and r.get('status') == 'live_sent']
    res = prod_results([(r.get('ticker'), r.get('side')) for r in sent], state, crypto, now)
    orders = []
    for r in rows:
        if r.get('action') != 'live_order_result': continue
        tk = r.get('ticker') or ''
        if 'LIVE' in tk: continue
        st_ = r.get('status'); ts = r['ts']
        settled = res.get((tk, r.get('side')))
        cts = _ticker_close_ts(tk)
        base = dict(ticker=tk, asset=asset(tk), at=iso(ts),
                    close_at=(iso(settled[1]) if settled else (iso(cts) if cts else None)),
                    side=r.get('side'), entry=True,
                    quantity=float(r.get('contracts') or 0),
                    price=number(r.get('price_dollars')))
        if st_ == 'skipped_by_flow_gate':
            orders.append(dict(id=hashlib.sha256((tk+str(ts)+'skip').encode()).hexdigest()[:20],
                               **{**base, 'quantity': 0.0}, filled=0.0, cost_usd=0.0,
                               fees_usd=0.0, status='skipped', verified=True, detail=_order_detail(r)))
            continue
        if st_ != 'live_sent': continue
        rc = parse_receipt(r) or {}
        code = r.get('status_code')
        oid = rc.get('order_id') or (r.get('body_sent') or {}).get('client_order_id') \
              or hashlib.sha256((tk+str(ts)).encode()).hexdigest()[:20]
        if isinstance(code, int) and 400 <= code < 500:
            orders.append(dict(id=oid, **base, filled=0.0, cost_usd=0.0,
                               fees_usd=0.0, status='rejected', verified=True, detail=_order_detail(r, rc)))
            continue
        if not rc.get('order_id'):
            orders.append(dict(id=oid, **base, filled=None, cost_usd=None,
                               fees_usd=None, status='unknown', verified=False, detail=_order_detail(r, rc)))
            continue
        fill = float(rc.get('fill_count') or 0)
        avg = float(rc.get('average_fill_price') or 0)
        fee = float(rc.get('average_fee_paid') or 0)*fill
        cost = (avg if r.get('side') == 'yes' else (1-avg))*fill if fill > 0 else 0.0
        ex = _exact_cost_fee(exact, rc.get('order_id'), fill)
        if ex: cost, fee = ex                  # Kalshi's own fills: exact to the 1/10000 dollar
        status = 'filled' if fill >= base['quantity']-1e-9 else ('partial' if fill > 0 else 'zero_fill')
        orders.append(dict(id=oid, **base, filled=fill, cost_usd=round(cost, 4),
                           fees_usd=round(fee, 4), status=status, verified=True,
                           detail=_order_detail(r, rc, fill, avg)))
    closed = []; positions = []
    by = defaultdict(list)
    for o in orders:
        if o['status'] in ('filled', 'partial') and (o['filled'] or 0) > 0:
            by[(o['ticker'], o['side'])].append(o)
    for (tk, side), os_ in sorted(by.items()):
        q = sum(o['filled'] for o in os_)
        cost = sum(o['cost_usd'] for o in os_); fees = sum(o['fees_usd'] for o in os_)
        settled = res.get((tk, side))
        if settled is not None:
            win, closed_at = settled
            payout = q if win else 0.0
            closed.append(dict(ticker=tk, asset=asset(tk), at=iso(closed_at) or os_[0]['close_at'],
                               result=(side if win else ('no' if side == 'yes' else 'yes')),
                               quantity=q, cost_usd=round(cost, 4), fees_usd=round(fees, 4),
                               payout_usd=payout, net_usd=round(payout-cost-fees, 4)))
        else:
            cts = _ticker_close_ts(tk)
            positions.append(dict(ticker=tk, asset=asset(tk), close_at=os_[0]['close_at'],
                                  yes_quantity=q if side == 'yes' else 0.0,
                                  no_quantity=q if side == 'no' else 0.0,
                                  paired_quantity=0.0, net_quantity=q if side == 'yes' else -q,
                                  cost_usd=round(cost, 4), fees_usd=round(fees, 4), verified=True,
                                  source_as_of=None,
                                  exit_status='等待官方结算' if (cts and cts < (time.time() if now is None else now)) else '持有至结算'))
    closed.sort(key=lambda x: str(x['at']))
    return orders, positions, closed


USER_KEY_DIR = Path(os.environ.get('KALSHI_USER_KEY_DIR') or (Path.home()/'.kalshi'))


def read_user_journal(journal_dir=None):
    """All per-user order rows from the PRIVATE journal (~/.kalshi/journal).

    The W7 router writes one `pending_send` row before each user order and one
    result row after, both with the same mirror_id; the last row per mirror_id
    wins. A request interrupted by a restart therefore stays visible as an
    unresolved order (no order_id) instead of disappearing.
    """
    journal_dir = journal_dir or (USER_KEY_DIR/'journal')
    rows, last = [], {}
    for f in sorted(Path(journal_dir).glob('*.jsonl')) if Path(journal_dir).is_dir() else []:
        for ln in f.read_text(errors='ignore').splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict) or not r.get('account'):
                continue
            if r.get('mirror_id'):
                last[r['mirror_id']] = r
            else:
                rows.append(r)
    for r in last.values():
        if r.get('status') == 'pending_send':
            r = dict(r, status='live_sent', status_code=None, response=None)
        rows.append(r)
    rows.sort(key=lambda r: str(r.get('ts')))
    return rows


def user_prod_rows(journal_rows, uid):
    """One user's rows, already in the OWNER's row shape (2026-10-01).

    Attribution is by account only - never by a time window - so re-verifying
    a key can never drop orders, positions or settlements the user really has.
    Gate skips are journaled only for users who were actually being mirrored.
    """
    out = []
    for r in journal_rows:
        if r.get('account') != uid:
            continue
        row = dict(r)
        if row.get('error'):
            row['status_code'] = None; row['response'] = None
        row.setdefault('body_sent', {})
        out.append(row)
    return out


def _empty_execution(label, since, note):
    return dict(label=label, since=iso(since), source_as_of=None, scope='你的 Kalshi Production 账户', note=note,
                **{k: None for k in ('signals','requested_contracts','accepted','filled_orders','full_fills','partial_fills',
                                     'zero_fills','filled_contracts','empty_side','unavailable','other_skips')})


_USER_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def write_user_ledgers(state, now, paper=(), out_dir=None, journal_dir=None, fills_dir=None):
    """Private per-user ledgers OUTSIDE the repo (~/.kalshi/ledgers, 0700/0600);
    served only by the authenticated web route to their owner."""
    out_dir = Path(out_dir or (USER_KEY_DIR/'ledgers'))
    try:
        registry = json.loads((USER_KEY_DIR/'users.json').read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(registry, dict):
        return 0
    journal = read_user_journal(journal_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    # same Supabase id everywhere: registry key == record user_id == file name
    active = {uid: rec for uid, rec in registry.items()
              if _USER_UUID.fullmatch(uid) and isinstance(rec, dict)
              and rec.get('status') == 'active' and rec.get('user_id', uid) == uid}
    for stale in out_dir.glob('*.json'):                 # disconnected users' books
        if _USER_UUID.fullmatch(stale.stem) and stale.stem not in active:
            stale.unlink(missing_ok=True)
    n = 0
    for uid, rec in active.items():
        urows = user_prod_rows(journal, uid)
        exact = load_exact_fills(Path(fills_dir or (USER_KEY_DIR/'fills'))/f'{uid}.json')
        orders, positions, settlements = fave_prod_ledger(urows, state, exact=exact)
        execution = {}
        first = min((timestamp(r['ts']) for r in urows), default=None)
        for key, label, since in (('all', '跟单开通以来', first), ('recent', '最近48h MAIN', now-48*3600)):
            if since is None:
                execution[key] = _empty_execution(label, now, '尚无跟单订单。')
                continue
            ps = [r for r in paper if timestamp(r['ts']) >= since]
            os_ = [o for o in orders if timestamp(o['at']) >= since]
            execution[key] = execution_stats(label, since, iso(now), ps, os_,
                note='分母为 MAIN 纸面信号；你的下单张数 = 平台主账户张数 × 你的跟单比例（向下取整，不足 1 张不下单）。跳单与 IOC 落空为跟踪差异。',
                scope='按你的交易所回执归属的 Prod 实盘执行。')
        payload = dict(user_id=uid, email=rec.get('email'), generated_at=iso(now),
                       validated_at=rec.get('validated_at'),
                       prod=fave_prod(urows, state, exact=exact), orders=orders, positions=positions,
                       settlements=settlements, execution=execution)
        path = out_dir/f'{uid}.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        n += 1
    return n


def fave_prod(rows, state, crypto=CRYPTO, now=None, exact=None):
    """W7 实盘腿(2026-09-28 武装)。按交易所同步下单回执计账:fill_count /
    average_fill_price(YES 侧价,no 单成本=1-px)/ average_fee_paid;官方结算
    结果取自纸面账本——其结算真值本就是 venue 官方 result;纸面没有的腿按
    strike_identity_result(下一窗行权价=本窗结算价)结算。发布器对 prod 零
    网络调用,只读日志与状态,维持"仅 Demo 网络"的姿态。IOC 落空与拒单是与
    纸面的跟踪差异,计数披露但不以纸面金额补齐。"""
    sent = [r for r in rows if r.get('action') == 'live_order_result' and r.get('status') == 'live_sent']
    res = prod_results([(r.get('ticker'), r.get('side')) for r in sent], state, crypto, now)
    if not sent:
        return performance(status='unavailable', scope='MAIN [0.78,0.98] 实盘镜像（2026-09-28 武装）。', note='武装后尚无实盘派发。')
    settled = []; open_ = miss = rej = unresolved = 0; latest = None
    for r in sent:
        latest = r['ts'] if latest is None or str(r['ts']) > str(latest) else latest
        rc = parse_receipt(r) or {}
        code = r.get('status_code')
        if isinstance(code, int) and code >= 400:
            rej += 1; continue                 # 4xx = 已知零执行(如修复前的 invalid_price)
        if not rc.get('order_id'):
            unresolved += 1; continue          # 超时/无回执:执行状态未知
        n = float(rc.get('fill_count') or 0)
        if n <= 0:
            miss += 1; continue                # IOC 落空:已知零执行
        px = float(rc.get('average_fill_price') or 0)
        total = (px if r.get('side') == 'yes' else round(1 - px, 4)) * n
        fee = float(rc.get('average_fee_paid') or 0) * n
        ex = _exact_cost_fee(exact, rc.get('order_id'), n)
        if ex: total, fee = ex                 # Kalshi's own fills: exact to the 1/10000 dollar
        key = (r.get('ticker'), r.get('side'))
        if key in res:
            win, closed = res[key]
            settled.append(dict(at=closed or r['ts'], net_usd=n - total - fee if win else -total - fee, fees_usd=fee))
        else:
            open_ += 1
    return performance(
        status='partial' if unresolved else 'verified', source_as_of=latest,
        scope='2026-09-28 起的 MAIN 实盘镜像；跳单、IOC 落空与拒单计为跟踪差异。',
        net_pnl_usd=round(sum(x['net_usd'] for x in settled), 4) if settled else 0.0,
        fees_usd=round(sum(x['fees_usd'] for x in settled), 4) if settled else 0.0,
        settled_count=len(settled), open_count=open_, unresolved_count=unresolved,
        sample_windows=len(settled), curve=curve((x['at'], x['net_usd']) for x in settled),
        note='按官方回执与结算计账；成本与手续费优先取 Kalshi 逐笔成交的精确值。')


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
        return performance(status='verified', source_as_of=iso(at), scope='MAIN 注册频段的纸面交易。',
                           net_pnl_usd=net, fees_usd=None, settled_count=len(rows), open_count=sum(main_trade(r,state['main_registered_at']) for r in state['positions'].values()),
                           curve=curve((k,float(v['sum_c'])/100*25) for k,v in windows.items()), sample_windows=len(windows), unresolved_count=0, verdict=verdict_text(state.get('verdict_main')),
                           note='含纸面模型费用。')
    book=state['books']['tilted']; rows=book['trades']; grouped=defaultdict(list)
    for r in rows: grouped[r['close_ts']].append(r)
    active={t:rs for t,rs in grouped.items() if any(r['fills'] for r in rs)}
    clean={t:rs for t,rs in active.items() if not any(r.get('unverified_order_quantity',0) for r in rs)}
    total=sum(r['net_usd'] for r in rows)
    if abs(total-book['cum_net_usd'])>1e-6: raise ValueError('PFME paper ledger mismatch')
    return performance(status='partial' if len(clean)!=len(active) else 'verified', source_as_of=iso(at),
                       scope='当前注册的 tilted 书(v10 公允闸处理组)保守排队模型；不含 paired 对照。', net_pnl_usd=total,
                       fees_usd=sum(r['fees_usd'] for r in rows), settled_count=sum(bool(r['fills']) for r in rows),
                       open_count=sum(bool(m.get('fills')) for m in book['positions'].values()),
                       curve=curve((t,sum(r['net_usd'] for r in rs)) for t,rs in grouped.items()), sample_windows=len(clean), unresolved_count=len(active)-len(clean),
                       verdict=verdict_text(state.get('verdicts',{}).get('fair_gate_diff') or state.get('verdicts',{}).get('tilted')),
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
    snap=dict(schema_version=1,snapshot_id=hashlib.sha256(str(now).encode()).hexdigest()[:16],generated_at=iso(now),execution_environment='mixed',market_data_environment='prod',prod_execution_enabled=True,issues=[],strategies={})
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
                out['demo'],_demo_orders,_demo_positions,_demo_settlements=fave_demo(attempts,account,out['issues'])
                # 2026-09-30(用户指令):FAVE 的 artifact 账本视图全部改为
                # Prod 实盘数据;Demo 仅保留「收益与回撤」里的独立评估块。
                exact=load_exact_fills(Path(private_dir)/PROD_FILLS_OWNER.name)
                out['orders'],out['positions'],out['settlements']=fave_prod_ledger(rows,state,crypto,now,exact)
                out['ledger_source']='prod'
                out['prod']=fave_prod(rows,state,crypto,now,exact)
                try:
                    write_user_ledgers(state,now,paper=paper)
                except Exception as exc:  # a user ledger can never break the public snapshot
                    out['issues'].append(issue('prod_users','WRITE_FAILED','用户实盘账本生成失败（'+type(exc).__name__+'）。'))
                cutoff=min((timestamp(o['at']) for o in out['orders']),default=now)
                for key,label,since in [('all','实盘武装以来',cutoff),('recent','最近48h MAIN',since48)]:
                    ps=[r for r in paper if timestamp(r['ts'])>=since]; os_=[o for o in out['orders'] if timestamp(o['at'])>=since]
                    out['execution'][key]=execution_stats(label,since,iso(now),ps,os_,
                        note='分母为 MAIN 纸面信号；申请张数为分时段实盘规模。跳单与 IOC 落空为跟踪差异。',scope='按交易所回执归属的 Prod 实盘执行。')
                for key,label,since in [('all','当前执行版本以来',cutoff),('recent','最近48小时',since48)]:
                    out['execution']['exits'][key]=execution_stats(label,since,(account or {}).get('as_of'),[],[],note='当前 FAVE 没有主动退出路径，持有至官方结算。',scope='退出订单数量完整为零。')
                if bad:
                    for key in ('all','recent'):
                        out['execution'][key]['signals']=None; out['execution'][key]['requested_contracts']=None; out['execution'][key]['other_skips']=None
                        out['execution'][key]['note']+=' 相关日志有无法解析的行，信号总数与分母不可确认；订单指标仅为可验证子集。'
                out['parameters']=[dict(label=k,value=v) for k,v in [('入场区间','$0.78–$0.98'),('信号规模','纸面 25 张 · 实盘分时段调整'),('入场时间','距结束 8 分钟 ± 0.6 分钟'),('执行方式','实时盘口 · IOC'),('持仓管理','持有至官方结算'),('费用','交易所官方回执费用')]]
                out['runtime']=[runtime('FAVE 状态文件','stale' if now-at>300 else 'updated',at,300,'文件更新时间，不冒充进程心跳。'),
                                runtime('Demo 账户核验','unavailable' if not account else 'degraded' if errs else 'verified',(account or {}).get('as_of'),360,'独立只读 GET；与观察进程无写入连接。')]
            else:
                p=signals/'w8_demo_mirror/state.json'; demo=read_json(p)
                if demo.get('mode')!='demo' or demo['version']!=state['version']: raise ValueError('PFME Demo environment or version mismatch')
                out['demo'],out['orders'],out['positions'],out['settlements']=pfme_demo(demo,demo['last_heartbeat'])
                out['prod']=performance(status='unavailable', scope='W8 无实盘账户。', note='实盘仅 W7；W8 处于纸面 + Demo 阶段。')
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
