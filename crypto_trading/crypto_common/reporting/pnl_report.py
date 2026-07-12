"""Crypto PnL PDF report (Plan 07 §2) — style ported from /PnLReport.py
(read-only template): A4 reportlab, CJK font registration, navy/gold palette
(C_HEADER/C_POS/C_NEG/…), C/H cell wrappers, make_table / make_kv_table /
note_item helpers, auto date detection, --start/--end/--out CLI, _analyse()
narrative bullets. Data layer is crypto-native (ledger + loader + 365-day
metrics); every section renders even with thin data ("no data yet").

Sections (plan §2):
  1 Header / NAV summary          5 Benchmarks (KXBTCPERP-HODL + EW basket)
  2 Equity & daily P&L            6 Leverage & funding trend
  3 Attribution: strategy × asset × source line (trading/funding/fees/slippage)
  4 Performance (365-annualized)  7 Turnover & cost (zero vs projected)
                                  8 Notes / auto-analysis

CLI:
    … -m crypto_trading.crypto_common.reporting.pnl_report
        [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--out path.pdf]

Output: crypto_trading/trading_signals/reports/pnl_report_<ts>.pdf
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common.backtest.metrics import (TRADING_DAYS, annualized_vol,
                                                           cagr, calmar_ratio, cvar,
                                                           max_drawdown, sharpe_ratio)
from crypto_trading.crypto_common.reporting import ledger as _ledger
from crypto_trading.crypto_common.risk.metrics import cdar_block

logger = logging.getLogger(__name__)


def _reports_dir():
    return _config.SIGNALS_DIR / "reports"


# ═══════════════════════════════════════════════════════════════════════════
# Font / styles / helpers — ported from PnLReport.py (template lines 49–196)
# ═══════════════════════════════════════════════════════════════════════════

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


def S(name, **kw):
    kw.setdefault('fontName', FONT)
    return ParagraphStyle(name, **kw)


C_HEADER = colors.HexColor('#1a1a2e')
C_SUBHDR = colors.HexColor('#16213e')
C_ROW_ALT = colors.HexColor('#f7f9fc')
C_POS = colors.HexColor('#1a7a4a')
C_NEG = colors.HexColor('#c0392b')
C_GOLD = colors.HexColor('#d4a843')
C_BORDER = colors.HexColor('#cccccc')
C_GRAY = colors.HexColor('#888888')

title_style = S('TT', fontSize=15, leading=19, alignment=TA_CENTER, spaceAfter=4)
sub_style = S('ST', fontSize=8.5, leading=12, alignment=TA_CENTER,
              textColor=colors.HexColor('#555555'), spaceAfter=14)
h1_style = S('H1', fontSize=10, leading=14, textColor=C_HEADER,
             spaceBefore=12, spaceAfter=5)
body_style = S('BD', fontSize=8, leading=11)
footer_style = S('FT', fontSize=7, leading=9.5, alignment=TA_CENTER,
                 textColor=C_GRAY, spaceBefore=6)


def money(v, color=True) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 'N/A'
    s = f'+{abs(v):,.2f}' if v >= 0 else f'-{abs(v):,.2f}'
    if not color:
        return s
    c = '#1a7a4a' if v >= 0 else '#c0392b'
    return f'<font color="{c}">{s}</font>'


def pct(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 'N/A'
    s = f'+{abs(v):.2f}%' if v >= 0 else f'-{abs(v):.2f}%'
    c = '#1a7a4a' if v >= 0 else '#c0392b'
    return f'<font color="{c}"><b>{s}</b></font>'


def C(text, align='LEFT', header=False) -> Paragraph:
    s = ParagraphStyle('_cx', fontName=FONT, fontSize=7.5, leading=10.5,
                       alignment=(TA_RIGHT if align == 'RIGHT' else 0),
                       textColor=(colors.white if header else colors.black))
    content = f'<b>{text}</b>' if header else str(text)
    return Paragraph(content, s)


def H(text, align='LEFT') -> Paragraph:
    return C(text, align=align, header=True)


def make_table(data, col_widths, pnl_col=-1, header_rows=1):
    t = Table(data, colWidths=col_widths, repeatRows=header_rows)
    n = len(data)
    cmds = [
        ('FONTNAME', (0, 0), (-1, -1), FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('LEADING', (0, 0), (-1, -1), 10.5),
        ('BACKGROUND', (0, 0), (-1, header_rows - 1), C_HEADER),
        ('TEXTCOLOR', (0, 0), (-1, header_rows - 1), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('ALIGN', (-1, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.35, C_BORDER),
        ('LINEBELOW', (0, header_rows - 1), (-1, header_rows - 1), 0.9, C_GOLD),
    ]
    for i in range(header_rows, n):
        bg = C_ROW_ALT if (i - header_rows) % 2 == 1 else colors.white
        cmds.append(('BACKGROUND', (0, i), (-1, i), bg))
    t.setStyle(TableStyle(cmds))
    return t


def make_kv_table(rows, label_w=9.5 * cm, val_w=5 * cm):
    sl = ParagraphStyle('_sl', fontName=FONT, fontSize=7.5, leading=10.5)
    sv = ParagraphStyle('_sv', fontName=FONT, fontSize=7.5, leading=10.5,
                        alignment=TA_RIGHT)
    data = [[Paragraph(f'<b>{r[0]}</b>', sl), Paragraph(r[1], sv)] for r in rows]
    t = Table(data, colWidths=[label_w, val_w])
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), FONT),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('LINEABOVE', (0, 0), (-1, 0), 0.5, C_GOLD),
        ('LINEBELOW', (0, -1), (-1, -1), 0.5, C_GOLD),
        ('ROWBACKGROUNDS', (0, 0), (-1, -1),
         [colors.HexColor('#fafbfc'), colors.white]),
    ]))
    return t


def note_item(num: str, text: str, W: float):
    sl = ParagraphStyle('_nl', fontName=FONT, fontSize=7.5, leading=10.5)
    st = ParagraphStyle('_nt', fontName=FONT, fontSize=7.5, leading=10.5)
    return Table(
        [[Paragraph(num, sl), Paragraph(text, st)]],
        colWidths=[0.6 * cm, W - 0.6 * cm],
        style=[('VALIGN', (0, 0), (-1, -1), 'TOP'),
               ('TOPPADDING', (0, 0), (-1, -1), 1),
               ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
               ('LEFTPADDING', (0, 0), (-1, -1), 0),
               ('RIGHTPADDING', (0, 0), (-1, -1), 0)])


# ═══════════════════════════════════════════════════════════════════════════
# Data assembly (crypto-native; injectable for tests)
# ═══════════════════════════════════════════════════════════════════════════

def _load_bench_returns(start, end) -> dict[str, pd.Series]:
    """KXBTCPERP-HODL + equal-weight perp basket daily returns (plan §2.5)."""
    from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT
    from crypto_trading.crypto_common.loader import load_perp_candles
    out: dict[str, pd.Series] = {}
    rets = {}
    for t in ACTIVE_PERPS_SNAPSHOT:
        try:
            c = load_perp_candles(t, "1d", start=start, end=end)
            rets[t] = c.price_close.pct_change().dropna()
        except Exception:
            continue
    if "KXBTCPERP" in rets:
        out["KXBTCPERP-HODL"] = rets["KXBTCPERP"]
    if rets:
        out["EW-PERP-BASKET"] = pd.DataFrame(rets).mean(axis=1).dropna()
    return out


def _equity_series(led: dict) -> pd.Series | None:
    """Best-available NAV: live nav log (future) → latest backtest equity."""
    curves = []
    for s, entry in led["strategies"].items():
        bts = entry.get("backtests") or []
        if bts:
            # summaries only store final equity; use daily return replay when
            # the strategy module starts persisting equity curves (contract)
            pass
    return pd.Series(dtype=float) if not curves else pd.concat(curves, axis=1).sum(axis=1)


def build_report_data(start: str, end: str, *, led: dict | None = None,
                      bench: dict[str, pd.Series] | None = None,
                      returns: pd.Series | None = None) -> dict:
    """Assemble everything renderable. All inputs injectable (tests)."""
    led = led if led is not None else _ledger.build_ledger()
    if bench is None:
        try:
            bench = _load_bench_returns(start, end)
        except Exception:
            bench = {}

    perf = None
    src = returns
    if src is None or not len(src):
        for name, b in (bench or {}).items():
            pass                         # benchmarks are not the portfolio
    if src is not None and len(src) >= 5:
        mdd, mdd_days = max_drawdown(src)
        nav = (1 + src).cumprod()
        downside = src[src < 0]
        sortino = (float(src.mean() * TRADING_DAYS
                         / (downside.std(ddof=0) * np.sqrt(TRADING_DAYS)))
                   if len(downside) > 1 and downside.std(ddof=0) > 0 else None)
        perf = {"sharpe": sharpe_ratio(src), "sortino": sortino,
                "calmar": calmar_ratio(src), "cagr": cagr(src),
                "vol": annualized_vol(src), "max_dd": mdd, "max_dd_days": mdd_days,
                "cvar_95": cvar(src), "cdar": cdar_block(nav)}

    bench_stats = {}
    for name, b in (bench or {}).items():
        if len(b) >= 3:
            mdd, _ = max_drawdown(b)
            bench_stats[name] = {"total_return_pct": float(((1 + b).prod() - 1) * 100),
                                 "sharpe": sharpe_ratio(b), "max_dd_pct": mdd * 100}

    return {"start": start, "end": end, "ledger": led, "bench": bench_stats,
            "perf": perf, "returns": src,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}


def _analyse(report: dict) -> list[str]:
    """Auto-analysis bullets — the template's _analyse() narrative pattern."""
    pts: list[str] = []
    tot = report["ledger"]["totals"]
    if tot:
        direction = '盈利' if tot["net_projected"] >= 0 else '亏损'
        pts.append(f'期间合计（projected 费率口径） {money(tot["net_projected"], color=False)}，'
                   f'zero-fee 口径 {money(tot["net_zero"], color=False)}，整体{direction}。')
        fee_gap = tot["fees_projected"] - tot["fees_zero"]
        if tot["gross_trading"] and fee_gap > 0:
            pts.append(f'费率悬崖：projected 费用 {money(tot["fees_projected"], color=False)} '
                       f'占毛交易收益的 '
                       f'{abs(fee_gap / tot["gross_trading"]) * 100:.0f}%'
                       f'（零费窗口结束后的真实成本 — headline 一律用 projected）。')
        if tot["funding"]:
            pts.append(f'Funding 净额 {money(tot["funding"], color=False)}'
                       f'（正 = 收取；每 8h 04/12/20 UTC 结算）。')
        if tot["slippage"]:
            pts.append(f'已实现滑点 {money(-abs(tot["slippage"]), color=False)}'
                       f'（fill vs decision-mid；与模型滑点的差是策略健康度指标）。')
    for s, entry in report["ledger"]["strategies"].items():
        if entry.get("fill_source") == "dryrun_hypothetical" and entry["rows"]:
            pts.append(f'{s}: 当前为 DRY-RUN 假想成交口径（margin 未开通，无真实订单）。')
        for bt in (entry.get("backtests") or [])[-1:]:
            pts.append(f'{s} 最近回测：net {money(bt.get("net_pnl"), color=False)}'
                       f'（{bt.get("fee_scenario", "?")} 费率，{bt.get("n_fills", "?")} fills，'
                       f'{bt.get("note", "")[:60]}…）')
    if report["bench"]:
        parts = [f'{n} 收益 {m["total_return_pct"]:+.2f}%（Sharpe {m["sharpe"]:.2f}，'
                 f'maxDD {m["max_dd_pct"]:.2f}%）' for n, m in report["bench"].items()]
        pts.append('基准（同期）：' + '；'.join(parts) + '。')
    if not pts:
        pts.append('尚无可报告的交易数据 — 数据采集与回测阶段。')
    return pts


# ═══════════════════════════════════════════════════════════════════════════
# PDF builder
# ═══════════════════════════════════════════════════════════════════════════

def build_pdf(report: dict, output_path: str) -> str:
    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            leftMargin=1.5 * cm, rightMargin=1.5 * cm)
    W = A4[0] - 3.0 * cm
    el: list = []
    el.append(Paragraph('Kalshi Crypto Perps — PnL Report', title_style))
    el.append(Paragraph(f'{report["start"]} → {report["end"]} · 生成 {report["generated"]}'
                        ' · headline = projected fees', sub_style))
    el.append(HRFlowable(width='100%', thickness=1, color=C_GOLD, spaceAfter=8))

    tot = report["ledger"]["totals"]

    # 1. NAV / summary
    el.append(Paragraph('1 · NAV 与期间摘要', h1_style))
    el.append(make_kv_table([
        ('净 P&L（projected 费率，headline）', money(tot.get("net_projected"))),
        ('净 P&L（zero-fee 启动窗口）', money(tot.get("net_zero"))),
        ('毛交易 P&L', money(tot.get("gross_trading"))),
        ('Funding 净额（+收取 / −支付）', money(tot.get("funding"))),
        ('费用 zero / projected',
         f'{money(tot.get("fees_zero"))} / {money(tot.get("fees_projected"))}'),
        ('已实现滑点（vs decision mid）', money(tot.get("slippage"))),
    ]))

    # 2. Equity & daily P&L
    el.append(Paragraph('2 · 权益曲线与日 P&L（24/7 UTC）', h1_style))
    src = report.get("returns")
    if src is not None and len(src) >= 2:
        rows = [[H('日期'), H('日收益', 'RIGHT'), H('累计', 'RIGHT')]]
        nav = (1 + src).cumprod()
        for dt, r in src.tail(14).items():
            rows.append([C(str(pd.Timestamp(dt).date())),
                         C(pct(r * 100), 'RIGHT'),
                         C(pct((nav.loc[dt] - 1) * 100), 'RIGHT')])
        el.append(make_table(rows, [W * 0.4, W * 0.3, W * 0.3]))
    else:
        el.append(Paragraph('（尚无组合级日收益序列 — 等待 live/nav 数据积累）', body_style))

    # 3. Attribution — strategy × asset × source line
    el.append(Paragraph('3 · P&L 归因：策略 × 资产 × 来源（trading / funding / fees / slippage）',
                        h1_style))
    rows = [[H('策略'), H('Ticker'), H('毛交易', 'RIGHT'), H('Funding', 'RIGHT'),
             H('Fees(proj)', 'RIGHT'), H('滑点', 'RIGHT'), H('净(proj)', 'RIGHT')]]
    any_row = False
    for s, entry in report["ledger"]["strategies"].items():
        for r in entry["rows"]:
            any_row = True
            rows.append([C(s), C(r["ticker"]), C(money(r["gross_trading"]), 'RIGHT'),
                         C(money(r["funding"]), 'RIGHT'),
                         C(money(-r["fees_projected"]), 'RIGHT'),
                         C(money(-r["slippage"]), 'RIGHT'),
                         C(money(r["net_projected"]), 'RIGHT')])
    if any_row:
        el.append(make_table(rows, [W * 0.16, W * 0.18, W * 0.14, W * 0.13,
                                    W * 0.13, W * 0.12, W * 0.14]))
    else:
        el.append(Paragraph('（无成交记录 — 尚在数据/回测阶段）', body_style))

    # 4. Performance
    el.append(Paragraph('4 · 绩效指标（365 天年化）', h1_style))
    perf = report.get("perf")
    if perf:
        cd = perf.get("cdar") or {}
        el.append(make_kv_table([
            ('Sharpe（365d）', f'{perf["sharpe"]:.2f}'),
            ('Sortino', f'{perf["sortino"]:.2f}' if perf.get("sortino") else 'N/A'),
            ('Calmar', f'{perf["calmar"]:.2f}'),
            ('CAGR', pct(perf["cagr"] * 100)),
            ('年化波动', f'{perf["vol"] * 100:.1f}%'),
            ('最大回撤', pct(-abs(perf["max_dd"]) * 100)),
            ('CVaR 95（日）', pct(-abs(perf["cvar_95"]) * 100)),
            ('CDaR 95', f'{cd.get("cdar_pct", float("nan")):.2f}%' if cd else 'N/A'),
        ]))
    else:
        el.append(Paragraph('（组合收益序列不足（<5 日）— 指标待数据积累）', body_style))

    # 5. Benchmarks
    el.append(Paragraph('5 · 基准对比：KXBTCPERP-HODL + 等权 perp 篮子', h1_style))
    if report["bench"]:
        rows = [[H('基准'), H('区间收益', 'RIGHT'), H('Sharpe', 'RIGHT'), H('maxDD', 'RIGHT')]]
        for n, m in report["bench"].items():
            rows.append([C(n), C(pct(m["total_return_pct"]), 'RIGHT'),
                         C(f'{m["sharpe"]:.2f}', 'RIGHT'),
                         C(f'{m["max_dd_pct"]:.2f}%', 'RIGHT')])
        el.append(make_table(rows, [W * 0.4, W * 0.2, W * 0.2, W * 0.2]))
    else:
        el.append(Paragraph('（基准数据不可用 — 检查 candle 缓存）', body_style))

    # 6. Leverage & funding trend
    el.append(Paragraph('6 · 杠杆与 Funding 趋势', h1_style))
    inv_rows = [[H('策略'), H('权益', 'RIGHT'), H('持仓', 'RIGHT'),
                 H('funding_accrued', 'RIGHT')]]
    any_inv = False
    for s, entry in report["ledger"]["strategies"].items():
        inv = entry.get("inventory")
        if inv:
            any_inv = True
            pos = inv.get("positions") or []
            inv_rows.append([C(s), C(money(inv.get("equity"), color=False), 'RIGHT'),
                             C(str(sum(abs(p.get("contracts", 0)) for p in pos)), 'RIGHT'),
                             C(money(sum(p.get("funding_accrued", 0) for p in pos)), 'RIGHT')])
    if any_inv:
        el.append(make_table(inv_rows, [W * 0.3, W * 0.25, W * 0.2, W * 0.25]))
    else:
        el.append(Paragraph('（无持仓 inventory — 全部平仓或尚未交易；杠杆 0.00×）', body_style))

    # 7. Turnover & cost
    el.append(Paragraph('7 · 换手与成本（zero vs projected）', h1_style))
    n_fills = sum(r["n_fills"] for e in report["ledger"]["strategies"].values()
                  for r in e["rows"])
    el.append(make_kv_table([
        ('总成交笔数', str(n_fills)),
        ('费用差（fee cliff）', money(-(tot.get("fees_projected", 0)
                                       - tot.get("fees_zero", 0)))),
        ('已实现滑点', money(-abs(tot.get("slippage", 0)))),
        ('模型滑点（depth-walk）', '回测口径 — 见策略 backtest json'),
    ]))

    # 8. Notes
    el.append(Paragraph('8 · 自动分析', h1_style))
    for i, b in enumerate(_analyse(report), 1):
        el.append(note_item(f'{i}.', b, W))
    el.append(Spacer(1, 6))
    el.append(Paragraph('数据源：crypto_trading/trading_signals/*（fills/dry-run/inventory/'
                        'backtests）+ price_data/kalshi/*。年化基准 365 天。'
                        'headline 口径 = projected fees（Plan 07 §1）。', footer_style))
    doc.build(el)
    return output_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    default_end = datetime.now(timezone.utc).date()
    ap.add_argument('--start', default=str(default_end - timedelta(days=30)))
    ap.add_argument('--end', default=str(default_end))
    ap.add_argument('--out', default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = build_report_data(args.start, args.end)
    out_dir = _reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    out = args.out or str(out_dir / f'pnl_report_{ts}.pdf')
    build_pdf(report, out)
    logger.info('PnL report → %s', out)
    print(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
