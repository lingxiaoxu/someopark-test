"""
PnLReport.py — 通用 PnL PDF 报告生成器

用法:
  python PnLReport.py                          # 自动检测最近数据
  python PnLReport.py --end 2026-03-24
  python PnLReport.py --start 2026-03-13 --end 2026-03-24
  python PnLReport.py --end 2026-03-24 --out /tmp/report.pdf

数据源全部自动从 inventory_history/ + trading_signals/ 读取。
"""

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
    KeepTogether,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_RIGHT

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
INV_DIR      = os.path.join(BASE_DIR, 'inventory_history')
SIG_DIR      = os.path.join(BASE_DIR, 'trading_signals')
CASH_CAP     = 1_000_000.0  # 最大现金（自有上限）
MAX_ACCOUNT  = 2_000_000.0  # 最大账户规模 = 现金 + 100% margin loan
# 回测参数: initial_cash=500K, initial_loan=500K（各策略独立，仅用于仓位sizing，非真实资本）


# ═══════════════════════════════════════════════════════════════════════════════
# Font
# ═══════════════════════════════════════════════════════════════════════════════

def _register_cjk() -> str:
    for path in [
        '/System/Library/Fonts/PingFang.ttc',
        '/System/Library/Fonts/STHeiti Light.ttc',
        '/Library/Fonts/Arial Unicode MS.ttf',
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
    ]:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont('CJK', path))
                return 'CJK'
            except Exception:
                continue
    return 'Helvetica'

FONT = _register_cjk()


# ═══════════════════════════════════════════════════════════════════════════════
# Styles & colors
# ═══════════════════════════════════════════════════════════════════════════════

def S(name, **kw):
    kw.setdefault('fontName', FONT)
    return ParagraphStyle(name, **kw)

C_HEADER  = colors.HexColor('#1a1a2e')
C_SUBHDR  = colors.HexColor('#16213e')
C_ROW_ALT = colors.HexColor('#f7f9fc')
C_POS     = colors.HexColor('#1a7a4a')
C_NEG     = colors.HexColor('#c0392b')
C_GOLD    = colors.HexColor('#d4a843')
C_BORDER  = colors.HexColor('#cccccc')
C_GRAY    = colors.HexColor('#888888')

title_style = S('TT', fontSize=15, leading=19, alignment=TA_CENTER, spaceAfter=4)
sub_style   = S('ST', fontSize=8.5, leading=12, alignment=TA_CENTER,
                textColor=colors.HexColor('#555555'), spaceAfter=14)
h1_style    = S('H1', fontSize=10, leading=14, textColor=C_HEADER,
                spaceBefore=12, spaceAfter=5)
body_style  = S('BD', fontSize=8, leading=11)
footer_style= S('FT', fontSize=7, leading=9.5, alignment=TA_CENTER,
                textColor=C_GRAY, spaceBefore=6)


def money(v, color=True) -> str:
    if v is None:
        return 'N/A'
    s = f'+{abs(v):,.2f}' if v >= 0 else f'-{abs(v):,.2f}'
    if not color:
        return s
    c = '#1a7a4a' if v >= 0 else '#c0392b'
    return f'<font color="{c}">{s}</font>'


def pct(v) -> str:
    if v is None:
        return 'N/A'
    s = f'+{abs(v):.2f}%' if v >= 0 else f'-{abs(v):.2f}%'
    c = '#1a7a4a' if v >= 0 else '#c0392b'
    return f'<font color="{c}"><b>{s}</b></font>'


# ═══════════════════════════════════════════════════════════════════════════════
# Table helpers
# ═══════════════════════════════════════════════════════════════════════════════

_CELL_STYLE  = ParagraphStyle('_c',  fontName='Helvetica', fontSize=7.5, leading=10.5)
_CELL_STYLE_R= ParagraphStyle('_cr', fontName='Helvetica', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)

def C(text, align='LEFT', header=False) -> Paragraph:
    """Wrap a plain string as a wrapping Paragraph for table cells.
    header=True: white bold text for dark header rows."""
    s = ParagraphStyle('_cx', fontName=FONT, fontSize=7.5, leading=10.5,
                       alignment=(TA_RIGHT if align == 'RIGHT' else 0),
                       textColor=(colors.white if header else colors.black))
    content = f'<b>{text}</b>' if header else str(text)
    return Paragraph(content, s)


def H(text, align='LEFT') -> Paragraph:
    """Header cell: white bold text for dark-background header rows."""
    return C(text, align=align, header=True)


def make_table(data, col_widths, pnl_col=-1, header_rows=1):
    """Generic styled table. pnl_col: column index whose values get PnL coloring."""
    t = Table(data, colWidths=col_widths, repeatRows=header_rows)
    n = len(data)
    cmds = [
        ('FONTNAME',      (0,0), (-1,-1), FONT),
        ('FONTSIZE',      (0,0), (-1,-1), 7.5),
        ('LEADING',       (0,0), (-1,-1), 10.5),
        ('BACKGROUND',    (0,0), (-1, header_rows-1), C_HEADER),
        ('TEXTCOLOR',     (0,0), (-1, header_rows-1), colors.white),
        ('FONTSIZE',      (0,0), (-1, header_rows-1), 7.5),
        ('ALIGN',         (0,0), (-1,-1), 'LEFT'),
        ('ALIGN',         (-1,0),(-1,-1), 'RIGHT'),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,0), (-1,-1), 3.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3.5),
        ('LEFTPADDING',   (0,0), (-1,-1), 5),
        ('RIGHTPADDING',  (0,0), (-1,-1), 5),
        ('GRID',          (0,0), (-1,-1), 0.35, C_BORDER),
        ('LINEBELOW',     (0, header_rows-1), (-1, header_rows-1), 0.9, C_GOLD),
    ]
    for i in range(header_rows, n):
        bg = C_ROW_ALT if (i - header_rows) % 2 == 1 else colors.white
        cmds.append(('BACKGROUND', (0,i), (-1,i), bg))
    t.setStyle(TableStyle(cmds))
    return t


def make_kv_table(rows, label_w=9.5*cm, val_w=5*cm):
    """Two-column label/value summary box."""
    sl = ParagraphStyle('_sl', fontName=FONT, fontSize=7.5, leading=10.5)
    sv = ParagraphStyle('_sv', fontName=FONT, fontSize=7.5, leading=10.5, alignment=TA_RIGHT)
    data = [[Paragraph(f'<b>{r[0]}</b>', sl), Paragraph(r[1], sv)] for r in rows]
    t = Table(data, colWidths=[label_w, val_w])
    t.setStyle(TableStyle([
        ('FONTNAME',       (0,0), (-1,-1), FONT),
        ('ALIGN',          (1,0), (1,-1), 'RIGHT'),
        ('VALIGN',         (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',     (0,0), (-1,-1), 3),
        ('BOTTOMPADDING',  (0,0), (-1,-1), 3),
        ('LEFTPADDING',    (0,0), (-1,-1), 7),
        ('RIGHTPADDING',   (0,0), (-1,-1), 7),
        ('LINEABOVE',      (0,0), (-1,0), 0.5, C_GOLD),
        ('LINEBELOW',      (0,-1),(-1,-1), 0.5, C_GOLD),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#fafbfc'), colors.white]),
    ]))
    return t


def note_item(num: str, text: str, W: float):
    sl = ParagraphStyle('_nl', fontName=FONT, fontSize=7.5, leading=10.5)
    st = ParagraphStyle('_nt', fontName=FONT, fontSize=7.5, leading=10.5)
    return Table(
        [[Paragraph(num, sl), Paragraph(text, st)]],
        colWidths=[0.6*cm, W - 0.6*cm],
        style=[
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING',    (0,0),(-1,-1), 1),
            ('BOTTOMPADDING', (0,0),(-1,-1), 1),
            ('LEFTPADDING',   (0,0),(-1,-1), 0),
            ('RIGHTPADDING',  (0,0),(-1,-1), 0),
        ]
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading (reuses logic from reconcile.py, inlined for self-containedness)
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_ts(fname: str):
    """Extract (day_ts, full_ts) from inventory/report filename."""
    parts = os.path.basename(fname).replace('.json', '').split('_')
    if len(parts) < 4:
        return None, None
    try:
        day  = pd.Timestamp(parts[-2])
        full = pd.Timestamp(f"{parts[-2]} {parts[-1][:6]}")
        return day, full
    except Exception:
        return None, None


def load_positions(start_ts, end_ts) -> list[dict]:
    """All pair positions seen in inventory snapshots within date range.
    Tracks the last active state of each pair. Pairs that were later closed
    (direction=null in a subsequent snapshot) are marked _closed_in_snapshot
    so the caller can distinguish truly-open from closed-mid-period."""
    positions: dict[str, dict] = {}
    closed_pairs: set[str] = set()   # pairs seen with direction=null AFTER being active
    for fpath in sorted(glob.glob(os.path.join(INV_DIR, 'inventory_*.json'))):
        day, _ = _parse_ts(fpath)
        if day is None or day < start_ts or day > end_ts:
            continue
        strategy = os.path.basename(fpath).split('_')[1]
        with open(fpath) as f:
            data = json.load(f)
        for pair_name, p in data.get('pairs', {}).items():
            if not isinstance(p, dict):
                continue
            if '/' not in pair_name:
                continue
            if not p.get('direction'):
                # Pair was closed in this snapshot — mark it but keep the record
                if pair_name in positions:
                    closed_pairs.add(pair_name)
                continue
            # Active snapshot — record (or update) position, and un-mark closed
            # in case it was re-opened after a previous close
            closed_pairs.discard(pair_name)
            s1, s2 = pair_name.split('/', 1)
            ml = p.get('monitor_log', [])
            positions[pair_name] = {
                'pair':          pair_name,
                'strategy':      strategy,
                's1':            s1,
                's2':            s2,
                's1_shares':     p.get('s1_shares', 0),
                's2_shares':     p.get('s2_shares', 0),
                'open_date':     p.get('open_date'),
                'open_s1_price': p.get('open_s1_price'),
                'open_s2_price': p.get('open_s2_price'),
                'direction':     p.get('direction'),
                'param_set':     p.get('param_set', '—'),
                'last_pnl':      ml[-1]['unrealized_pnl'] if ml else None,
                'last_pnl_date': ml[-1]['date'] if ml else None,
            }
    # Mark pairs that were closed in a later snapshot
    for pair_name in closed_pairs:
        if pair_name in positions:
            positions[pair_name]['_closed_in_snapshot'] = True
    return list(positions.values())


def load_close_events(start_ts, end_ts) -> dict[str, dict]:
    """Latest CLOSE/CLOSE_STOP per pair, excluding those superseded by a later HOLD."""
    all_ev: dict[str, list] = {}
    hold_ts: dict[str, pd.Timestamp] = {}

    for fpath in sorted(glob.glob(os.path.join(SIG_DIR, 'daily_report_*.json'))):
        day, full = _parse_ts(fpath)
        if day is None or day < start_ts or day > end_ts:
            continue
        with open(fpath) as f:
            data = json.load(f)
        signal_date_str = data.get('signal_date', str(day.date()))
        pm = data.get('position_monitor', {})
        for strat in ('mrpt', 'mtfs'):
            for e in pm.get(strat, []):
                if not isinstance(e, dict):
                    continue
                pair = e.get('pair')
                if not pair:
                    continue
                action = e.get('action', '')
                if action in ('CLOSE', 'CLOSE_STOP'):
                    ev = {
                        'action':      action,
                        'pnl':         e.get('unrealized_pnl'),
                        'note':        e.get('note', ''),
                        'report_date': str(day.date()),
                        'signal_date': signal_date_str,
                        '_ts':         full,
                    }
                    all_ev.setdefault(pair, []).append(ev)
                elif action == 'HOLD':
                    if full > hold_ts.get(pair, pd.Timestamp('1970-01-01')):
                        hold_ts[pair] = full

    result: dict[str, dict] = {}
    for pair, evs in all_ev.items():
        latest_hold = hold_ts.get(pair, pd.Timestamp('1970-01-01'))
        valid = [ev for ev in evs if ev['_ts'] > latest_hold]
        if valid:
            result[pair] = max(valid, key=lambda e: e['_ts'])
    return result


def load_hold_pnl(end_ts) -> dict[str, dict]:
    """HOLD PnL from the latest daily_report within range."""
    best_f, best_ts = None, pd.Timestamp('1970-01-01')
    for fpath in sorted(glob.glob(os.path.join(SIG_DIR, 'daily_report_*.json'))):
        day, full = _parse_ts(fpath)
        if day is None or day > end_ts:
            continue
        if full > best_ts:
            best_ts, best_f = full, fpath
    if not best_f:
        return {}
    with open(best_f) as f:
        data = json.load(f)
    signal_date_str = data.get('signal_date', str(best_ts.date()))
    result: dict[str, dict] = {}
    pm = data.get('position_monitor', {})
    for strat in ('mrpt', 'mtfs'):
        for e in pm.get(strat, []):
            if isinstance(e, dict) and e.get('action') == 'HOLD' and e.get('pair'):
                result[e['pair']] = {
                    'pnl':         e.get('unrealized_pnl'),
                    'report_date': str(best_ts.date()),
                    'signal_date': signal_date_str,
                }
    return result


def download_prices_mongo(tickers: set, price_start: str, price_end: str) -> pd.DataFrame:
    """Load close prices from MongoDB stock_data (deterministic, no yfinance drift)."""
    from db.connection import get_main_db
    db = get_main_db()
    col = db["stock_data"]

    start_ms = int(pd.Timestamp(price_start).timestamp() * 1000)
    end_ms = int((pd.Timestamp(price_end) + pd.Timedelta(days=1)).timestamp() * 1000)

    frames = {}
    for sym in sorted(tickers):
        docs = list(col.find(
            {"symbol": sym, "t": {"$gte": start_ms, "$lte": end_ms}},
            {"c": 1, "t": 1, "_id": 0}
        ).sort("t", 1))
        if docs:
            dates = [pd.Timestamp(d["t"], unit="ms").normalize() for d in docs]
            closes = [d["c"] for d in docs]
            frames[sym] = pd.Series(closes, index=dates, name=sym)

    if not frames:
        return pd.DataFrame()
    df = pd.DataFrame(frames)
    df.index.name = "Date"
    return df


def download_prices(tickers: set, price_start: str, price_end: str) -> pd.DataFrame:
    end_plus = str((pd.Timestamp(price_end) + pd.Timedelta(days=1)).date())
    print(f'  Downloading prices ({len(tickers)} tickers, {price_start}→{price_end})...')
    raw = yf.download(sorted(tickers), start=price_start, end=end_plus,
                      auto_adjust=True, progress=False)
    return raw['Close']


def download_open_prices(tickers: set, price_start: str, price_end: str) -> pd.DataFrame:
    """Download Open prices from yfinance (for execution-price comparison)."""
    end_plus = str((pd.Timestamp(price_end) + pd.Timedelta(days=2)).date())  # +2 for exec day
    raw = yf.download(sorted(tickers), start=price_start, end=end_plus,
                      auto_adjust=True, progress=False)
    return raw['Open']


def get_price(prices: pd.DataFrame, ticker: str, dt: str):
    ts = pd.Timestamp(dt)
    avail = prices.index[prices.index <= ts]
    if len(avail) == 0:
        return None
    v = prices[ticker][avail[-1]]
    return float(v) if pd.notna(v) else None


# ═══════════════════════════════════════════════════════════════════════════════
# Analysis
# ═══════════════════════════════════════════════════════════════════════════════

def _compute_leverage(start_ts, end_ts) -> dict:
    """
    Compute gross / single-side / net leverage ratios from inventory snapshots.
    Denominators:
      MAX_ACCOUNT ($2M) = 现金$1M + 100% margin loan，用于毛杠杆（是否超出账户上限）
      CASH_CAP    ($1M) = 纯现金上限，用于 ROE / 现金利用率
    """
    from collections import defaultdict

    def latest_by_date(pat):
        by_date: dict = defaultdict(list)
        for fpath in sorted(glob.glob(os.path.join(INV_DIR, pat))):
            day, _ = _parse_ts(fpath)
            if day is None or day < start_ts or day > end_ts:
                continue
            by_date[str(day.date())].append(fpath)
        return {d: sorted(fps)[-1] for d, fps in by_date.items()}

    mrpt_snap = latest_by_date('inventory_mrpt_*.json')
    mtfs_snap = latest_by_date('inventory_mtfs_*.json')
    all_dates = sorted(set(list(mrpt_snap.keys()) + list(mtfs_snap.keys())))

    gross_vals, ss_vals, net_vals = [], [], []
    daily_rows = []

    for date in all_dates:
        gross = net = 0.0
        for snap_map in (mrpt_snap, mtfs_snap):
            if date not in snap_map:
                continue
            with open(snap_map[date]) as f:
                data = json.load(f)
            for pos in data.get('pairs', {}).values():
                if not pos.get('direction'):
                    continue
                for shk, prk in (('s1_shares', 'open_s1_price'), ('s2_shares', 'open_s2_price')):
                    sh = pos.get(shk) or 0
                    pr = pos.get(prk) or 0
                    val = sh * pr
                    gross += abs(val)
                    net   += val
        ss = gross / 2
        gross_vals.append(gross)
        ss_vals.append(ss)
        net_vals.append(abs(net))
        daily_rows.append({
            'date': date, 'gross': gross, 'ss': ss, 'net': abs(net),
            # vs MAX_ACCOUNT ($2M): 是否超出账户上限
            'gross_lev': gross / MAX_ACCOUNT,
            'ss_lev':    ss    / MAX_ACCOUNT,
            'net_lev':   abs(net) / MAX_ACCOUNT,
            'over_limit': gross > MAX_ACCOUNT,
        })

    if not gross_vals:
        return {}

    peak_gross = max(gross_vals)
    return {
        'daily':          daily_rows,
        'avg_gross':      sum(gross_vals) / len(gross_vals),
        'avg_ss':         sum(ss_vals)    / len(ss_vals),
        'avg_net':        sum(net_vals)   / len(net_vals),
        'peak_gross':     peak_gross,
        'peak_ss':        max(ss_vals),
        # vs MAX_ACCOUNT ($2M = $1M现金 + $1M借款)
        'avg_gross_lev':  sum(gross_vals) / len(gross_vals) / MAX_ACCOUNT,
        'avg_ss_lev':     sum(ss_vals)    / len(ss_vals)    / MAX_ACCOUNT,
        'avg_net_lev':    sum(net_vals)   / len(net_vals)   / MAX_ACCOUNT,
        'peak_gross_lev': peak_gross / MAX_ACCOUNT,
        'peak_ss_lev':    max(ss_vals) / MAX_ACCOUNT,
        # 是否超出账户上限，需要多大的 scale down
        'peak_over_limit': peak_gross > MAX_ACCOUNT,
        'scale_needed':    min(1.0, MAX_ACCOUNT / peak_gross) if peak_gross > 0 else 1.0,
        'days_over_limit': sum(1 for g in gross_vals if g > MAX_ACCOUNT),
        'n_days':          len(gross_vals),
        # ROE: PnL vs 现金上限
        'cash_cap':        CASH_CAP,
        'max_account':     MAX_ACCOUNT,
    }


def _compute_portfolio_metrics(start_ts, end_ts, positions, prices) -> dict:
    """
    Build daily portfolio PnL time series from inventory snapshots + market prices,
    then compute max drawdown, Sharpe ratio, and benchmark comparison (SP500, Russell 3000).
    """
    from collections import defaultdict

    # ── Step 1: Identify all positions and their lifecycles ──
    # For each trading day in range, mark-to-market all open positions
    trade_days = prices.index[
        (prices.index >= start_ts) & (prices.index <= end_ts)
    ]
    if len(trade_days) < 2:
        return {}

    # Build position map: pair -> {s1, s2, s1_shares, s2_shares, open_s1_price, open_s2_price, open_date, close_date}
    # Use inventory snapshots to know which positions are open on which dates
    def latest_inv_by_date(pat):
        by_date: dict = defaultdict(list)
        for fpath in sorted(glob.glob(os.path.join(INV_DIR, pat))):
            day, _ = _parse_ts(fpath)
            if day is None:
                continue
            by_date[str(day.date())].append(fpath)
        return {d: sorted(fps)[-1] for d, fps in by_date.items()}

    mrpt_snap = latest_inv_by_date('inventory_mrpt_*.json')
    mtfs_snap = latest_inv_by_date('inventory_mtfs_*.json')

    # For each trade day, find the latest inventory snapshot on or before that day
    all_snap_dates = sorted(set(list(mrpt_snap.keys()) + list(mtfs_snap.keys())))

    def get_snap_on_or_before(snap_map, date_str):
        candidates = [d for d in sorted(snap_map.keys()) if d <= date_str]
        return snap_map[candidates[-1]] if candidates else None

    # ── Step 1b: Collect realized PnL from close events ──
    # When a position closes, it disappears from inventory (direction=null).
    # Its realized PnL must be locked in and carried forward, otherwise the
    # cumulative PnL curve has discontinuities (realized profit evaporates).
    #
    # De-dup strategy: group CLOSE events by (pair, signal_date).
    #   - Same signal_date = same DailySignal run (or re-run) → keep latest ts only
    #   - Different signal_date = different open→close lifecycle → each counts
    # This correctly handles:
    #   (a) Multiple DailySignal runs on the same day producing duplicate CLOSEs
    #   (b) Step 2 re-opening a position that Step 1 just closed, leading to
    #       consecutive CLOSEs on different dates with no HOLD in between
    _all_close_ev: list[tuple] = []   # (pair, signal_date, ts, pnl, file_date_str)
    for fpath in sorted(glob.glob(os.path.join(SIG_DIR, 'daily_report_*.json'))):
        day, full = _parse_ts(fpath)
        if day is None or day < start_ts or day > end_ts:
            continue
        with open(fpath) as f:
            dr = json.load(f)
        signal_date_str = dr.get('signal_date', str(day.date()))
        file_date_str = str(day.date())
        pm = dr.get('position_monitor', {})
        for strat in ('mrpt', 'mtfs'):
            for e in pm.get(strat, []):
                if not isinstance(e, dict):
                    continue
                action = e.get('action', '')
                pair = e.get('pair')
                if not pair:
                    continue
                if action in ('CLOSE', 'CLOSE_STOP'):
                    pnl = e.get('unrealized_pnl', 0) or 0
                    _all_close_ev.append((pair, signal_date_str, full, pnl, file_date_str))

    # Group by (pair, signal_date) and keep only the latest CLOSE per group
    _close_by_pair_date: dict[tuple, list] = {}  # (pair, signal_date) -> [(ts, pnl, file_date)]
    for pair, sig_date, ts, pnl, fdate in _all_close_ev:
        _close_by_pair_date.setdefault((pair, sig_date), []).append((ts, pnl, fdate))

    realized_by_date: dict[str, float] = defaultdict(float)
    _seen_close: set[tuple] = set()  # (pair, exec_date) for MTM guard

    # Helper: map signal_date → execution_date.
    # - Same-day run  (file_date == signal_date): close already executed and
    #   inventory updated on signal_date → exec_date = signal_date.
    # - Overnight run (file_date >  signal_date): close signal generated after
    #   signal_date's close, execution on next trading day → exec_date = T+1.
    _td_strs = sorted(set(str(td.date()) for td in trade_days))
    def _next_td(sig_date_str: str) -> str | None:
        for t in _td_strs:
            if t > sig_date_str:
                return t
        return None

    for (pair, sig_date), entries in _close_by_pair_date.items():
        best_ts, best_pnl, best_fdate = max(entries, key=lambda x: x[0])
        if best_fdate == sig_date:
            # Same-day run: close already reflected in inventory on signal_date
            exec_date = sig_date
        else:
            # Overnight run: execution happens next trading day
            exec_date = _next_td(sig_date)
            if exec_date is None:
                continue  # close is beyond report range → stay MTM
        _seen_close.add((pair, exec_date))
        realized_by_date[exec_date] += best_pnl

    # ── Step 2: Daily portfolio PnL = cum_realized + open_positions_mtm ──
    daily_pnl = []
    prev_total = 0.0
    cum_realized = 0.0   # running sum of all locked-in realized PnL

    # Pending realized PnL keyed by date (pop as consumed)
    _pending_realized = dict(realized_by_date)

    for td in trade_days:
        td_str = str(td.date())

        # Accumulate realized PnL from close events on or before this trade day
        for rd in sorted(list(_pending_realized.keys())):
            if rd <= td_str:
                cum_realized += _pending_realized.pop(rd)

        # Mark-to-market all currently open positions
        total_mtm = 0.0
        has_pos = False

        for snap_map in (mrpt_snap, mtfs_snap):
            fpath = get_snap_on_or_before(snap_map, td_str)
            if not fpath:
                continue
            with open(fpath) as f:
                data = json.load(f)
            for pair_name, pos in data.get('pairs', {}).items():
                if not pos.get('direction') or '/' not in pair_name:
                    continue

                # Skip positions that closed on this date — their PnL is
                # already captured in realized_by_date (avoid double-counting).
                if (pair_name, td_str) in _seen_close:
                    continue

                s1, s2 = pair_name.split('/', 1)
                s1_sh = pos.get('s1_shares', 0)
                s2_sh = pos.get('s2_shares', 0)
                op1 = pos.get('open_s1_price', 0)
                op2 = pos.get('open_s2_price', 0)

                try:
                    cp1 = prices[s1].loc[:td].dropna().iloc[-1] if s1 in prices.columns else None
                    cp2 = prices[s2].loc[:td].dropna().iloc[-1] if s2 in prices.columns else None
                except (IndexError, KeyError):
                    cp1 = cp2 = None

                if cp1 is not None and cp2 is not None:
                    pair_pnl = (s1_sh * float(cp1) + s2_sh * float(cp2)) - (s1_sh * op1 + s2_sh * op2)
                    total_mtm += pair_pnl
                    has_pos = True

        # Total PnL = locked realized + current unrealized
        total_pnl = cum_realized + total_mtm
        if has_pos or cum_realized != 0:
            daily_pnl.append({'date': td, 'cum_pnl': total_pnl, 'daily_chg': total_pnl - prev_total})
            prev_total = total_pnl

    if len(daily_pnl) < 2:
        return {}

    # ── Step 3: Portfolio metrics ──
    cum_pnl = np.array([d['cum_pnl'] for d in daily_pnl])
    daily_chg = np.array([d['daily_chg'] for d in daily_pnl])

    # Max drawdown on the equity curve (CASH_CAP + cum_pnl)
    equity = CASH_CAP + cum_pnl
    equity_peak = np.maximum.accumulate(equity)
    drawdowns = equity - equity_peak          # always <= 0
    max_dd = float(drawdowns.min())           # dollar drawdown
    max_dd_idx = int(np.argmin(drawdowns))
    peak_idx = int(np.argmax(equity[:max_dd_idx + 1])) if max_dd_idx > 0 else 0

    # Max drawdown as % of equity peak (industry standard)
    max_dd_pct = max_dd / float(equity_peak[max_dd_idx]) * 100

    # Sharpe ratio (annualized, daily returns as % of CASH_CAP)
    daily_ret = daily_chg / CASH_CAP
    if np.std(daily_ret) > 0:
        sharpe = float(np.mean(daily_ret) / np.std(daily_ret) * np.sqrt(252))
    else:
        sharpe = 0.0

    # ── Step 4: Benchmark comparison ──
    bench_start = str(trade_days[0].date())
    bench_end = str((trade_days[-1] + pd.Timedelta(days=1)).date())
    benchmarks = {}
    try:
        bench_tickers = {'^GSPC': 'S&P 500', '^RUA': 'Russell 3000'}
        bench_raw = yf.download(list(bench_tickers.keys()), start=bench_start, end=bench_end,
                                auto_adjust=True, progress=False)
        bench_close = bench_raw['Close'] if len(bench_tickers) > 1 else bench_raw[['Close']]

        for ticker, name in bench_tickers.items():
            col = ticker if ticker in bench_close.columns else None
            if col is None:
                continue
            bprices = bench_close[col].dropna()
            if len(bprices) < 2:
                continue
            b_ret = bprices.pct_change().dropna()
            b_cum_ret = (1 + b_ret).cumprod() - 1
            total_ret = float(b_cum_ret.iloc[-1]) * 100
            # Benchmark max drawdown
            b_cum = (1 + b_ret).cumprod()
            b_running_max = b_cum.cummax()
            b_dd = (b_cum / b_running_max - 1)
            b_max_dd = float(b_dd.min()) * 100
            # Benchmark Sharpe
            b_sharpe = float(b_ret.mean() / b_ret.std() * np.sqrt(252)) if b_ret.std() > 0 else 0.0
            benchmarks[name] = {
                'total_return_pct': total_ret,
                'max_dd_pct': b_max_dd,
                'sharpe': b_sharpe,
            }
    except Exception as e:
        print(f'  [WARN] Benchmark download failed: {e}')

    return {
        'daily_pnl': daily_pnl,
        'total_pnl': float(cum_pnl[-1]),
        'max_dd': max_dd,
        'max_dd_pct': max_dd_pct,
        'max_dd_peak_date': str(daily_pnl[peak_idx]['date'].date()),
        'max_dd_trough_date': str(daily_pnl[max_dd_idx]['date'].date()),
        'sharpe': sharpe,
        'n_days': len(daily_pnl),
        'portfolio_return_pct': float(cum_pnl[-1]) / CASH_CAP * 100,
        'benchmarks': benchmarks,
    }


def build_report_data(start: str, end: str) -> dict:
    """Collect all data needed for the PDF."""
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)

    positions   = load_positions(start_ts, end_ts)
    close_evs   = load_close_events(start_ts, end_ts)
    hold_pnl    = load_hold_pnl(end_ts)

    if not positions:
        sys.exit('[ERROR] No positions found for the given date range.')

    # Download prices
    tickers = {p['s1'] for p in positions} | {p['s2'] for p in positions}
    open_dates = [p['open_date'] for p in positions if p['open_date']]
    price_start = min(open_dates) if open_dates else start
    prices = download_prices(tickers, price_start, end)

    # Download Open prices for execution-price comparison (Section 六)
    try:
        exec_open_prices = download_open_prices(tickers, price_start, end)
    except Exception:
        exec_open_prices = None

    rows = []
    for p in sorted(positions, key=lambda x: (x['strategy'], x['open_date'] or '')):
        pair   = p['pair']
        s1, s2 = p['s1'], p['s2']
        s1_sh, s2_sh = p['s1_shares'], p['s2_shares']
        op1, op2     = p['open_s1_price'], p['open_s2_price']
        open_dt      = p['open_date'] or '?'

        # Determine close vs hold
        close_ev = close_evs.get(pair)
        hold_ev  = hold_pnl.get(pair)

        if close_ev:
            is_open    = False
            sys_pnl    = close_ev['pnl']
            exit_dt    = close_ev['report_date']
            action     = close_ev['action']
            close_note = close_ev['note']
        elif p.get('_closed_in_snapshot'):
            # Pair was closed in inventory (direction→null) but no monitor
            # close event was recorded (orphan close from Step 1 signal).
            # Treat as closed with last known PnL from monitor_log.
            is_open    = False
            sys_pnl    = p['last_pnl']
            exit_dt    = p['last_pnl_date'] or end
            action     = 'CLOSE'
            close_note = 'Closed by signal (no monitor event)'
        elif hold_ev:
            is_open    = True
            sys_pnl    = hold_ev['pnl']
            exit_dt    = hold_ev['report_date']
            action     = 'HOLD'
            close_note = ''
        else:
            is_open    = True
            sys_pnl    = p['last_pnl']
            exit_dt    = p['last_pnl_date'] or end
            action     = 'HOLD'
            close_note = ''

        # yf PnL — execution-price comparison
        # CLOSE: use Open price on exec_date (signal_date + 1 trading day)
        # HOLD:  use Close price on report end_date (current market value)
        yf_pnl = None
        if op1 and op2:
            if not is_open and exec_open_prices is not None:
                # CLOSE: find next trading day after signal_date, use Open price
                sig_dt = None
                if close_ev and close_ev.get('signal_date'):
                    sig_dt = close_ev['signal_date']
                elif p.get('_closed_in_snapshot'):
                    sig_dt = exit_dt  # fallback
                if sig_dt:
                    sig_ts = pd.Timestamp(sig_dt)
                    future = exec_open_prices.index[exec_open_prices.index > sig_ts]
                    if len(future) > 0:
                        exec_day = future[0]
                        ep1 = exec_open_prices[s1][exec_day] if s1 in exec_open_prices.columns else None
                        ep2 = exec_open_prices[s2][exec_day] if s2 in exec_open_prices.columns else None
                        if ep1 is not None and ep2 is not None and not (pd.isna(ep1) or pd.isna(ep2)):
                            yf_pnl = (s1_sh * float(ep1) + s2_sh * float(ep2)) - (s1_sh * op1 + s2_sh * op2)
            else:
                # HOLD: use Close price on end_date
                ep1 = get_price(prices, s1, end)
                ep2 = get_price(prices, s2, end)
                if ep1 and ep2:
                    yf_pnl = (s1_sh * ep1 + s2_sh * ep2) - (s1_sh * op1 + s2_sh * op2)

        # Extract close reason from note
        reason = ''
        if close_note:
            note_l = close_note.lower()
            if 'price level stop' in note_l:
                reason = 'Price Level Stop'
            elif 'time-based stop' in note_l or 'time based stop' in note_l:
                reason = 'Time-based Stop'
            elif 'pair p&l stop' in note_l or 'pair pnl stop' in note_l:
                pct_match = ''
                import re
                m = re.search(r'\((-[\d.]+%)\)', close_note)
                if m:
                    pct_match = f' ({m.group(1)})'
                reason = f'Pair PnL Stop{pct_match}'
            elif 'momentum decay' in note_l:
                reason = 'Momentum Decay Stop'
            elif 'stop loss' in note_l:
                reason = 'Stop Loss'
            elif 'passed exit threshold' in note_l:
                reason = '信号退出'
            else:
                reason = action

        # Single-side notional (industry standard denominator for pairs PnL%)
        gross_notional = abs(s1_sh * op1) + abs(s2_sh * op2) if (op1 and op2) else None
        ss_notional    = gross_notional / 2 if gross_notional else None

        rows.append({
            'pair':           pair,
            'strategy':       p['strategy'],
            's1':             s1, 's2': s2,
            'direction':      p['direction'],
            'param_set':      p['param_set'],
            'open_date':      open_dt,
            'is_open':        is_open,
            'action':         action,
            'exit_date':      exit_dt,
            'reason':         reason,
            'sys_pnl':        sys_pnl,
            'yf_pnl':         yf_pnl,
            'gross_notional': gross_notional,
            'ss_notional':    ss_notional,
        })

    # Compute totals
    totals = {}
    for strat in ('mrpt', 'mtfs'):
        r      = sum(x['sys_pnl'] or 0 for x in rows if x['strategy'] == strat and not x['is_open'])
        u      = sum(x['sys_pnl'] or 0 for x in rows if x['strategy'] == strat and x['is_open'])
        ss_c   = sum(x['ss_notional'] or 0 for x in rows if x['strategy'] == strat and not x['is_open'])
        ss_o   = sum(x['ss_notional'] or 0 for x in rows if x['strategy'] == strat and x['is_open'])
        totals[strat] = {
            'realized': r, 'unrealized': u, 'subtotal': r + u,
            'ss_closed': ss_c, 'ss_open': ss_o, 'ss_total': ss_c + ss_o,
        }
    totals['grand']    = sum(v['subtotal'] for v in totals.values())
    totals['ss_closed'] = sum(totals[s]['ss_closed'] for s in ('mrpt', 'mtfs'))
    totals['ss_open']   = sum(totals[s]['ss_open']   for s in ('mrpt', 'mtfs'))
    totals['ss_total']  = totals['ss_closed'] + totals['ss_open']

    # Leverage metrics (denominator = equity capital)
    # Computed from inventory snapshots across all dates in range
    lev_rows = _compute_leverage(start_ts, end_ts)
    totals['leverage'] = lev_rows

    # Portfolio metrics: daily PnL series, max drawdown, Sharpe, benchmark comparison
    # Use MongoDB prices for equity curve (deterministic, not affected by yfinance drift)
    print('  Computing portfolio metrics (drawdown, Sharpe, benchmarks)...')
    try:
        mongo_prices = download_prices_mongo(tickers, price_start, end)
        if not mongo_prices.empty and len(mongo_prices) >= 2:
            print(f'  Using MongoDB prices for equity curve ({len(mongo_prices)} rows)')
            metrics_prices = mongo_prices
        else:
            print('  MongoDB prices insufficient, falling back to yfinance')
            metrics_prices = prices
    except Exception as e:
        print(f'  MongoDB prices failed ({e}), falling back to yfinance')
        metrics_prices = prices
    port_metrics = _compute_portfolio_metrics(start_ts, end_ts, positions, metrics_prices)
    totals['portfolio'] = port_metrics

    return {
        'start':  start,
        'end':    end,
        'rows':   rows,
        'totals': totals,
    }


def _analyse(rows: list[dict], totals: dict) -> list[str]:
    """Generate simple analysis bullets from data."""
    points = []
    grand = totals['grand']

    # Overall — use single-side notional as denominator
    direction = '盈利' if grand >= 0 else '亏损'
    ss_total = totals.get('ss_total') or 0
    ss_pct_str = f'，PnL/单边名义 {grand/ss_total*100:+.2f}%' if ss_total > 0 else ''
    roe_str = f'，ROE(现金) {grand/CASH_CAP*100:+.2f}%' if CASH_CAP > 0 else ''
    points.append(
        f'期间合计 {money(grand, color=False)}{ss_pct_str}{roe_str}，整体{direction}。'
    )

    # Best and worst
    closed = [r for r in rows if not r['is_open'] and r['sys_pnl'] is not None]
    open_  = [r for r in rows if r['is_open']     and r['sys_pnl'] is not None]
    if closed:
        best  = max(closed, key=lambda r: r['sys_pnl'])
        worst = min(closed, key=lambda r: r['sys_pnl'])
        if best['sys_pnl'] > 0:
            points.append(
                f'已实现最优：{best["pair"]}（{money(best["sys_pnl"], color=False)}，{best["reason"] or best["action"]}）。'
            )
        if worst['sys_pnl'] < 0:
            points.append(
                f'已实现最差：{worst["pair"]}（{money(worst["sys_pnl"], color=False)}，{worst["reason"] or worst["action"]}）。'
            )
    if open_:
        best_o = max(open_, key=lambda r: r['sys_pnl'])
        if best_o['sys_pnl'] > 0:
            points.append(
                f'最大浮盈：{best_o["pair"]}（当前 {money(best_o["sys_pnl"], color=False)}，仍持仓）。'
            )

    # Strategy breakdown
    for strat in ('mrpt', 'mtfs'):
        t = totals[strat]
        points.append(
            f'{strat.upper()} 小计 {money(t["subtotal"], color=False)}'
            f'（已实现 {money(t["realized"], color=False)}，未实现 {money(t["unrealized"], color=False)}）。'
        )

    # Stop loss count
    n_stops = sum(1 for r in rows if r['action'] == 'CLOSE_STOP')
    n_close = sum(1 for r in rows if r['action'] == 'CLOSE')
    if n_stops or n_close:
        points.append(
            f'期间共触发止损 {n_stops} 笔，正常信号退出 {n_close} 笔。'
        )

    # Portfolio metrics: max drawdown, Sharpe
    port = totals.get('portfolio', {})
    if port:
        points.append(
            f'组合最大回撤 {money(port["max_dd"], color=False)}'
            f'（{port["max_dd_pct"]:+.2f}% of 现金），'
            f'峰值 {port["max_dd_peak_date"]} → 谷值 {port["max_dd_trough_date"]}。'
        )
        points.append(
            f'年化 Sharpe Ratio（基于日收益/现金）：{port["sharpe"]:.2f}。'
        )
        # Benchmark comparison
        bm = port.get('benchmarks', {})
        port_ret = port.get('portfolio_return_pct', 0)
        if bm:
            bm_parts = []
            for name, m in bm.items():
                bm_parts.append(
                    f'{name} 收益 {m["total_return_pct"]:+.2f}%，'
                    f'最大回撤 {m["max_dd_pct"]:.2f}%，Sharpe {m["sharpe"]:.2f}'
                )
            points.append(
                f'基准对比（同期）：组合收益 {port_ret:+.2f}%；'
                + '；'.join(bm_parts) + '。'
            )

    # yf vs sys divergence note
    big_div = [r for r in rows
               if r['sys_pnl'] is not None and r['yf_pnl'] is not None
               and abs(r['sys_pnl'] - r['yf_pnl']) > 3000]
    if big_div:
        names = '、'.join(r['pair'] for r in big_div)
        points.append(
            f'系统价与参考执行价差异较大（>$3,000）的仓位：{names}，'
            f'差异来源于 signal_date 收盘价 vs 执行日开盘价的隔夜漂移。'
        )

    return points


# ═══════════════════════════════════════════════════════════════════════════════
# PDF builder
# ═══════════════════════════════════════════════════════════════════════════════

def build_pdf(report: dict, output_path: str, yf_compare: bool = True):
    start = report['start']
    end   = report['end']
    rows  = report['rows']
    totals= report['totals']

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )
    W = A4[0] - 4*cm
    story = []

    # ── Title ────────────────────────────────────────────────────────────────
    story.append(Paragraph('Someo Park — 组合 PnL 分析报告', title_style))
    story.append(Paragraph(
        f'报告区间：{start} ～ {end} | MRPT + MTFS 策略',
        sub_style
    ))
    story.append(HRFlowable(width='100%', thickness=1, color=C_GOLD, spaceAfter=10))

    # ── Section 1: Summary totals ─────────────────────────────────────────────
    story.append(Paragraph('一、汇总', h1_style))

    grand = totals['grand']
    total_data = [
        [H('策略'), H('已实现', 'RIGHT'), H('未实现', 'RIGHT'), H('小计', 'RIGHT')],
    ]
    for strat in ('mrpt', 'mtfs'):
        t = totals[strat]
        total_data.append([
            C(strat.upper()),
            Paragraph(money(t['realized']),   S('_', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(money(t['unrealized']),  S('_', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(money(t['subtotal']),    S('_', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        ])
    total_data.append([
        H('合计'),
        Paragraph(money(totals['mrpt']['realized'] + totals['mtfs']['realized']),
                  S('_', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        Paragraph(money(totals['mrpt']['unrealized'] + totals['mtfs']['unrealized']),
                  S('_', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        Paragraph(money(grand),
                  S('_g', fontSize=8.5, leading=11.5, alignment=TA_RIGHT)),
    ])

    cw_sum = [3*cm, 4*cm, 4*cm, 4.5*cm]
    t_sum = Table(total_data, colWidths=cw_sum)
    t_sum.setStyle(TableStyle([
        ('FONTNAME',       (0,0), (-1,-1), FONT),
        ('FONTSIZE',       (0,0), (-1,-1), 8),
        ('LEADING',        (0,0), (-1,-1), 11),
        ('BACKGROUND',     (0,0), (-1,0), C_SUBHDR),
        ('TEXTCOLOR',      (0,0), (-1,0), colors.white),
        ('BACKGROUND',     (0,-1),(-1,-1), colors.HexColor('#1a1a2e')),
        ('TEXTCOLOR',      (0,-1),(-1,-1), C_GOLD),
        ('ALIGN',          (1,0), (-1,-1), 'RIGHT'),
        ('ALIGN',          (0,0), (0,-1), 'LEFT'),
        ('VALIGN',         (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING',     (0,0), (-1,-1), 5),
        ('BOTTOMPADDING',  (0,0), (-1,-1), 5),
        ('LEFTPADDING',    (0,0), (-1,-1), 7),
        ('RIGHTPADDING',   (0,0), (-1,-1), 7),
        ('GRID',           (0,0), (-1,-1), 0.35, C_BORDER),
        ('LINEABOVE',      (0,-1),(-1,-1), 1.2, C_GOLD),
        ('LINEBELOW',      (0,0), (-1,0), 0.9, C_GOLD),
    ]))
    story.append(t_sum)
    story.append(Spacer(1, 6))

    # PnL metrics row
    ss_total  = totals.get('ss_total') or 0
    ss_closed = totals.get('ss_closed') or 0
    r_pnl = totals['mrpt']['realized'] + totals['mtfs']['realized']
    lev   = totals.get('leverage', {})

    metric_parts = []
    if ss_total > 0:
        metric_parts.append(
            f'PnL / 单边名义 = {money(grand, color=False)} / {ss_total:,.0f} = {pct(grand/ss_total*100)}'
        )
    if ss_closed > 0:
        metric_parts.append(
            f'（已实现 {r_pnl:+,.0f} / {ss_closed:,.0f} = {pct(r_pnl/ss_closed*100)}）'
        )
    if grand != 0:
        metric_parts.append(
            f'ROE(现金) = {money(grand, color=False)} / {CASH_CAP:,.0f} = {pct(grand/CASH_CAP*100)}'
        )
    story.append(Paragraph(
        '    '.join(metric_parts),
        S('cap', fontSize=7.2, leading=10.8, alignment=TA_CENTER, spaceBefore=2)
    ))

    # Leverage metrics box
    if lev:
        story.append(Spacer(1, 5))
        warn = ' ⚠' if lev.get('peak_over_limit') else ''
        scale_str = (f'  →  需缩仓至 {lev["scale_needed"]*100:.1f}%'
                     if lev.get('peak_over_limit') else '  ✓ 在账户上限内')
        lev_rows_kv = [
            ('现金上限 / 最大账户规模（含100%融资）',
             f'${CASH_CAP:,.0f}  /  ${MAX_ACCOUNT:,.0f} <super>¹</super>'),
            (f'毛杠杆 vs 账户上限（均值 / 峰值{warn}）',
             f'{lev["avg_gross_lev"]:.2f}x  /  {lev["peak_gross_lev"]:.2f}x{scale_str}'),
            ('单边杠杆 Single-Side（均值 / 峰值）',
             f'{lev["avg_ss_lev"]:.2f}x  /  {lev["peak_ss_lev"]:.2f}x'),
            ('净杠杆 Net Exposure（均值）',
             f'{lev["avg_net_lev"]:.3f}x  ≈ 市场中性'),
            ('分母说明',
             f'杠杆率 ÷ 账户上限 ${MAX_ACCOUNT:,.0f}；PnL% ÷ 单边名义；ROE ÷ 现金 ${CASH_CAP:,.0f}'),
        ]
        story.append(make_kv_table(lev_rows_kv, label_w=10*cm, val_w=5.5*cm))
        if lev.get('peak_over_limit'):
            n_over = lev.get('days_over_limit', 0)
            n_total = lev.get('n_days', 1)
            story.append(Paragraph(
                f'<super>¹</super> 峰值毛名义 ${lev["peak_gross"]:,.0f} 超出账户上限 ${MAX_ACCOUNT:,.0f}，'
                f'共 {n_over}/{n_total} 个交易日超出。'
                f'若以 ${CASH_CAP:,.0f} 现金实盘，需将所有仓位缩小至 {lev["scale_needed"]*100:.1f}%。',
                S('_lnote', fontSize=7, leading=10,
                  textColor=colors.HexColor('#c0392b'), spaceBefore=3,
                  leftIndent=(W - 15.5*cm) / 2)
            ))

    # ── Section 2: Closed positions ──────────────────────────────────────────
    closed_rows = [r for r in rows if not r['is_open']]
    if closed_rows:
        story.append(Paragraph(
            '二、已平仓明细（来源：daily_report CLOSE / CLOSE_STOP 实际执行价）',
            h1_style
        ))
        hdr = [H('仓位'), H('策略'), H('方向'), H('平仓日'), H('方式'), H('原因'), H('单边名义','RIGHT'), H('PnL','RIGHT'), H('PnL/SS','RIGHT')]
        data = [hdr]
        for r in sorted(closed_rows, key=lambda x: x['exit_date'] or ''):
            ss = r.get('ss_notional')
            pnl_v = r['sys_pnl']
            pct_v  = pnl_v / ss * 100 if (ss and pnl_v is not None) else None
            data.append([
                C(r['pair']),
                C(r['strategy'].upper()),
                C(r['direction'].capitalize()),
                C(r['exit_date'][5:] if r['exit_date'] else '?'),
                C(r['action']),
                C(r['reason'] or '—'),
                C(f'{ss:,.0f}' if ss else '—', 'RIGHT'),
                Paragraph(money(pnl_v), S('_p', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
                Paragraph(pct(pct_v) if pct_v is not None else '—',
                          S('_pp', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            ])
        cw = [2.0*cm, 1.1*cm, 1.2*cm, 1.4*cm, 2.1*cm, 3.3*cm, 1.9*cm, 1.9*cm, 1.8*cm]
        story.append(make_table(data, cw))

    # ── Section 3: MRPT ───────────────────────────────────────────────────────
    mrpt_rows = [r for r in rows if r['strategy'] == 'mrpt']
    story.append(Paragraph('三、MRPT 策略明细', h1_style))
    hdr_mrpt = [H('仓位'), H('方向'), H('状态'), H('Param Set'), H('开仓日'), H('单边名义','RIGHT'), H('PnL','RIGHT'), H('PnL/SS','RIGHT')]
    data_mrpt = [hdr_mrpt]
    for r in mrpt_rows:
        if r['is_open']:
            status = f'持仓 ({r["exit_date"][5:] if r["exit_date"] else end[5:]})'
        else:
            short_dt = r['exit_date'][5:] if r['exit_date'] else '?'
            reason_short = r['reason'] or r['action']
            status = f'已平仓 {short_dt}（{reason_short}）'
        ss = r.get('ss_notional')
        pnl_v = r['sys_pnl']
        pct_v = pnl_v / ss * 100 if (ss and pnl_v is not None) else None
        data_mrpt.append([
            C(r['pair']),
            C(r['direction'].capitalize()),
            C(status),
            C(r['param_set']),
            C(r['open_date'][5:] if r['open_date'] else '?'),
            C(f'{ss:,.0f}' if ss else '—', 'RIGHT'),
            Paragraph(money(pnl_v), S('_m', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(pct(pct_v) if pct_v is not None else '—',
                      S('_mp', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        ])
    cw_mrpt = [1.9*cm, 1.2*cm, 3.8*cm, 3.3*cm, 1.2*cm, 1.8*cm, 1.8*cm, 1.5*cm]
    story.append(make_table(data_mrpt, cw_mrpt))
    story.append(Spacer(1, 7))

    tm = totals['mrpt']
    r_pairs = [r for r in mrpt_rows if not r['is_open'] and r['sys_pnl'] is not None]
    u_pairs = [r for r in mrpt_rows if r['is_open']     and r['sys_pnl'] is not None]
    r_expr = ' + '.join(f"{r['sys_pnl']:+,.2f}" for r in r_pairs) or '0'
    u_expr = ' + '.join(f"{r['sys_pnl']:+,.2f}" for r in u_pairs) or '0'
    story.append(make_kv_table([
        (f'已实现  {r_expr}', f'<font color="{"#1a7a4a" if tm["realized"]>=0 else "#c0392b"}"><b>{tm["realized"]:+,.2f}</b></font>'),
        (f'未实现  {u_expr}', f'<font color="{"#1a7a4a" if tm["unrealized"]>=0 else "#c0392b"}"><b>{tm["unrealized"]:+,.2f}</b></font>'),
        ('MRPT 小计', f'<font color="{"#1a7a4a" if tm["subtotal"]>=0 else "#c0392b"}"><b>{tm["subtotal"]:+,.2f}</b></font>'),
    ]))

    # ── Section 4: MTFS ───────────────────────────────────────────────────────
    mtfs_rows = [r for r in rows if r['strategy'] == 'mtfs']
    story.append(Paragraph('四、MTFS 策略明细', h1_style))
    hdr_mtfs = [H('仓位'), H('方向'), H('状态'), H('开仓日'), H('单边名义','RIGHT'), H('PnL','RIGHT'), H('PnL/SS','RIGHT')]
    data_mtfs = [hdr_mtfs]
    for r in mtfs_rows:
        if r['is_open']:
            status = f'持仓 ({r["exit_date"][5:] if r["exit_date"] else end[5:]})'
        else:
            short_dt = r['exit_date'][5:] if r['exit_date'] else '?'
            reason_short = r['reason'] or r['action']
            status = f'已平仓 {short_dt}（{reason_short}）'
        ss = r.get('ss_notional')
        pnl_v = r['sys_pnl']
        pct_v = pnl_v / ss * 100 if (ss and pnl_v is not None) else None
        data_mtfs.append([
            C(r['pair']),
            C(r['direction'].capitalize()),
            C(status),
            C(r['open_date'][5:] if r['open_date'] else '?'),
            C(f'{ss:,.0f}' if ss else '—', 'RIGHT'),
            Paragraph(money(pnl_v), S('_n', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(pct(pct_v) if pct_v is not None else '—',
                      S('_np', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        ])
    cw_mtfs = [1.9*cm, 1.2*cm, 5.2*cm, 1.2*cm, 1.8*cm, 1.8*cm, 1.6*cm]
    story.append(make_table(data_mtfs, cw_mtfs))
    story.append(Spacer(1, 7))

    tmt = totals['mtfs']
    r_pairs_m = [r for r in mtfs_rows if not r['is_open'] and r['sys_pnl'] is not None]
    u_pairs_m = [r for r in mtfs_rows if r['is_open']     and r['sys_pnl'] is not None]
    r_expr_m = ' + '.join(f"{r['sys_pnl']:+,.2f}" for r in r_pairs_m) or '0'
    u_expr_m = ' + '.join(f"{r['sys_pnl']:+,.2f}" for r in u_pairs_m) or '0'
    story.append(make_kv_table([
        (f'已实现  {r_expr_m}', f'<font color="{"#1a7a4a" if tmt["realized"]>=0 else "#c0392b"}"><b>{tmt["realized"]:+,.2f}</b></font>'),
        (f'未实现  {u_expr_m}', f'<font color="{"#1a7a4a" if tmt["unrealized"]>=0 else "#c0392b"}"><b>{tmt["unrealized"]:+,.2f}</b></font>'),
        ('MTFS 小计', f'<font color="{"#1a7a4a" if tmt["subtotal"]>=0 else "#c0392b"}"><b>{tmt["subtotal"]:+,.2f}</b></font>'),
    ]))

    # ── Section 5: Analysis ───────────────────────────────────────────────────
    story.append(Paragraph('五、简要分析', h1_style))
    bullets = _analyse(rows, totals)
    for i, b in enumerate(bullets, 1):
        story.append(note_item(f'{i}.', b, W))
        story.append(Spacer(1, 3))

    # ── Section 5b: Portfolio metrics & benchmark table ─────────────────────
    port = totals.get('portfolio', {})
    if port:
        story.append(Spacer(1, 4))
        story.append(Paragraph('五(b)、组合风险指标 & 基准对比 <super>²</super>', h1_style))
        bench_data = [
            [H('指标'), H('组合', 'RIGHT'), H('S&P 500', 'RIGHT'), H('Russell 3000', 'RIGHT')],
        ]
        bm = port.get('benchmarks', {})
        sp = bm.get('S&P 500', {})
        ru = bm.get('Russell 3000', {})

        bench_data.append([
            C('期间收益率'),
            Paragraph(pct(port.get('portfolio_return_pct')),
                      S('_br1', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(pct(sp.get('total_return_pct')) if sp else '—',
                      S('_br2', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(pct(ru.get('total_return_pct')) if ru else '—',
                      S('_br3', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        ])
        bench_data.append([
            C('最大回撤'),
            Paragraph(f'{port["max_dd_pct"]:.2f}%',
                      S('_bd1', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(f'{sp["max_dd_pct"]:.2f}%' if sp else '—',
                      S('_bd2', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(f'{ru["max_dd_pct"]:.2f}%' if ru else '—',
                      S('_bd3', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        ])
        bench_data.append([
            C('年化 Sharpe'),
            Paragraph(f'{port["sharpe"]:.2f}',
                      S('_bs1', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(f'{sp["sharpe"]:.2f}' if sp else '—',
                      S('_bs2', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            Paragraph(f'{ru["sharpe"]:.2f}' if ru else '—',
                      S('_bs3', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
        ])
        bench_data.append([
            C('最大回撤（$）'),
            Paragraph(money(port['max_dd']),
                      S('_bdd', fontSize=7.5, leading=10.5, alignment=TA_RIGHT)),
            C('—', 'RIGHT'), C('—', 'RIGHT'),
        ])

        cw_bench = [4*cm, 3.5*cm, 3.5*cm, 3.5*cm]
        story.append(make_table(bench_data, cw_bench))
        story.append(Paragraph(
            f'<super>²</super> 组合收益率 = PnL / 现金 ${CASH_CAP:,.0f}；Sharpe = 日收益均值/标准差 × √252；'
            f'回撤区间 {port["max_dd_peak_date"]} → {port["max_dd_trough_date"]}（{port["n_days"]} 个交易日）',
            S('_bnote', fontSize=7, leading=10, textColor=C_GRAY, spaceBefore=3,
              leftIndent=(W - 14.5*cm) / 2)
        ))

    # ── Section 6: yf vs sys comparison ──────────────────────────────────────
    if yf_compare:
        story.append(Paragraph('六、系统成交价 vs 参考执行价对照 <super>³</super>', h1_style))
        story.append(Paragraph(
            '<super>³</super> 系统成交价为 signal_date 收盘价（策略模拟）；'
            '参考执行价为执行日（signal_date 次一交易日）开盘价（yfinance），即最早可成交价格。'
            '持仓仓位参考价为报告截止日收盘价。差异反映隔夜价格漂移。',
            S('_note', fontSize=7.5, leading=10.5, textColor=C_GRAY, spaceAfter=5,
              leftIndent=(W - 13.8*cm) / 2)
        ))
        _yp = ParagraphStyle('_yp', fontName=FONT, fontSize=7.2, leading=9.5, alignment=TA_RIGHT)
        _yf_s = ParagraphStyle('_yf', fontName=FONT, fontSize=7, leading=9.5)
        hdr_yf = [H('仓位'), H('结算日'), H('系统\n成交价PnL','RIGHT'), H('参考\n执行价PnL','RIGHT'), H('差异\n(系统−参考)','RIGHT'), H('说明')]
        data_yf = [hdr_yf]
        valid_yf = [r for r in rows if not (r['sys_pnl'] is None and r['yf_pnl'] is None)]
        tot_sys = tot_yf = tot_diff = 0.0
        has_sys = has_yf = has_diff = True
        for r in valid_yf:
            diff = (r['sys_pnl'] - r['yf_pnl']) if (r['sys_pnl'] is not None and r['yf_pnl'] is not None) else None
            flag = '⚠ 差异>$3k' if (diff is not None and abs(diff) > 3000) else ''
            if r['sys_pnl'] is not None: tot_sys  += r['sys_pnl']
            else: has_sys = False
            if r['yf_pnl'] is not None: tot_yf   += r['yf_pnl']
            else: has_yf = False
            if diff is not None: tot_diff += diff
            else: has_diff = False
            data_yf.append([
                C(r['pair']),
                C(r['exit_date'][5:] if r['exit_date'] else '?'),
                Paragraph(money(r['sys_pnl']), _yp),
                Paragraph(money(r['yf_pnl']),  _yp),
                Paragraph(money(diff),          _yp),
                Paragraph(f'<font color="#888888">{flag}</font>', _yf_s),
            ])
        # Totals row
        _ps = ParagraphStyle('_yp2', fontName=FONT, fontSize=7.2, leading=9.5, alignment=TA_RIGHT)
        _ytl = ParagraphStyle('_ytl', fontName=FONT, fontSize=7.2, leading=9.5)
        _yfl = ParagraphStyle('_yfl', fontName=FONT, fontSize=7, leading=9.5)
        data_yf.append([
            Paragraph('<b>合计 <super>⁴</super></b>', _ytl),
            Paragraph('', _ytl),
            Paragraph(f'<b>{money(tot_sys if has_sys else None)}</b>', _ps),
            Paragraph(f'<b>{money(tot_yf  if has_yf  else None)}</b>', _ps),
            Paragraph(f'<b>{money(tot_diff if has_diff else None)}</b>', _ps),
            Paragraph(
                ('<font color="#555555">系统多算</font>' if (has_diff and tot_diff > 0)
                 else ('<font color="#555555">系统少算</font>' if (has_diff and tot_diff < 0)
                       else '<font color="#555555">—</font>')),
                _yfl
            ),
        ])
        cw_yf = [2.0*cm, 1.4*cm, 2.8*cm, 2.8*cm, 2.8*cm, 2.0*cm]
        t_yf = make_table(data_yf, cw_yf)
        # Style the totals row differently
        n_yf = len(data_yf)
        t_yf.setStyle(TableStyle([
            ('BACKGROUND',    (0, n_yf-1), (-1, n_yf-1), colors.HexColor('#f0f0f0')),
            ('LINEABOVE',     (0, n_yf-1), (-1, n_yf-1), 0.8, C_GOLD),
            ('FONTNAME',      (0, n_yf-1), (-1, n_yf-1), FONT),
        ]))
        story.append(t_yf)
        # Interpretation note
        if has_diff:
            interp = (f'<super>⁴</super> 差异合计 {tot_diff:+,.2f}：系统成交价总计比参考执行价{"多算" if tot_diff > 0 else "少算"}'
                      f' {abs(tot_diff):,.2f}，来源于 signal_date 收盘 vs 执行日开盘的隔夜价格漂移。')
            story.append(Paragraph(interp,
                S('_yi', fontSize=7.2, leading=10, textColor=C_GRAY, spaceBefore=4,
                  leftIndent=(W - 13.8*cm) / 2)))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width='100%', thickness=0.4, color=C_BORDER))
    story.append(Paragraph(
        f'Generated by Someo Park System · {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        footer_style
    ))

    doc.build(story)
    print(f'PDF saved: {output_path}')


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='生成 PnL PDF 报告')
    parser.add_argument('--start',  default=None, help='起始日期 YYYY-MM-DD')
    parser.add_argument('--end',    default=None, help='截止日期 YYYY-MM-DD（默认今天）')
    parser.add_argument('--out',    default=None, help='输出路径（默认自动命名）')
    parser.add_argument('--no-yf',  action='store_true',
                        help='不生成「系统成交价 vs 参考收盘价对照」章节')
    args = parser.parse_args()

    if args.end:
        end = args.end
    else:
        # Use US Eastern time; if before market close (16:00 ET), use previous day.
        # Then snap to the most recent NYSE trading day (skip weekends + holidays).
        try:
            from zoneinfo import ZoneInfo
            from datetime import timedelta
            us_now = pd.Timestamp.now(tz='America/New_York')
            if us_now.hour < 16:
                us_ref = (us_now - timedelta(days=1)).date()
            else:
                us_ref = us_now.date()
            # Snap to NYSE trading day
            import pandas_market_calendars as mcal
            nyse = mcal.get_calendar('NYSE')
            valid = nyse.valid_days(
                (pd.Timestamp(us_ref) - pd.Timedelta(days=10)).strftime('%Y-%m-%d'),
                pd.Timestamp(us_ref).strftime('%Y-%m-%d'))
            end = str(valid[-1].date()) if len(valid) > 0 else str(us_ref)
        except Exception:
            end = str(pd.Timestamp.now().date())
    if args.start:
        start = args.start
    else:
        # Auto-detect: earliest inventory snapshot date (first position ever opened)
        inv_files = sorted(glob.glob(os.path.join(INV_DIR, 'inventory_*.json')))
        if inv_files:
            first_day, _ = _parse_ts(inv_files[0])
            start = str(first_day.date()) if first_day else str((pd.Timestamp(end) - pd.Timedelta(days=30)).date())
        else:
            start = str((pd.Timestamp(end) - pd.Timedelta(days=30)).date())

    reports_dir = os.path.join(BASE_DIR, 'trading_signals', 'pnl_reports')
    os.makedirs(reports_dir, exist_ok=True)
    run_date = datetime.now().strftime('%Y%m%d')
    if args.out:
        out = args.out
    elif args.no_yf:
        gen_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        out = os.path.join(reports_dir, f'pnl_report_{run_date}_{gen_ts}.pdf')
    else:
        out = os.path.join(reports_dir, f'pnl_report_{run_date}.pdf')

    print(f'\nBuilding report: {start} → {end}  (yf_compare={"off" if args.no_yf else "on"})')
    report = build_report_data(start, end)
    build_pdf(report, out, yf_compare=not args.no_yf)


if __name__ == '__main__':
    main()
