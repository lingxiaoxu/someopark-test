"""ops/report.py — daily / weekly PDF reports (PLAN §12).

Daily: decisions + marks + COVERAGE MATRIX + MISSED alerts + 7-day release calendar.
Weekly (adds): llm narrative (nemotron, degradable) + calibration (settled Brier by
series) + backtest/OOS snapshot + DFM gate status.
House style copied from prediction_market/ops/pdf_style.py (visual only, 铁律: copy
not import). Output: data/output/reports/macro_{daily,weekly}_YYYYMMDD.pdf.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reportlab.platypus import Spacer

from prediction_market_macro.ops.pdf_style import (
    C, H, PAGE_W, bullet, make_kv_table, make_table, money, new_doc, note_style,
    section, title_block,
)


def _reports_dir(settings) -> Path:
    d = settings.output_dir / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decisions_since(conn, since: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ts_utc, series, period, kind, fair, ask, net_edge, size_usd, note"
        " FROM decisions WHERE ts_utc>=? ORDER BY ts_utc DESC LIMIT 60", (since,))]


def _coverage_matrix(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT series, period, state, updated_ts FROM coverage"
        " ORDER BY series, period LIMIT 120")]


def _missed(conn, since: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ts, message FROM alerts WHERE source='watchdog' AND ts>=?"
        " ORDER BY ts DESC LIMIT 30", (since,))]


def _calendar_7d(conn) -> list[dict]:
    now = datetime.now(timezone.utc)
    end = (now + timedelta(days=7)).isoformat()
    return [dict(r) for r in conn.execute(
        "SELECT cal, period, scheduled_ts FROM releases WHERE scheduled_ts BETWEEN ? AND ?"
        " ORDER BY scheduled_ts", (now.isoformat(), end))]


def _open_marks(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT m.ticker, m.mid, m.pnl_usd, d.series, d.period FROM marks m"
        " JOIN decisions d ON d.id=m.decision_id WHERE m.ts="
        " (SELECT MAX(ts) FROM marks) ORDER BY m.pnl_usd LIMIT 40")]


def _story_common(conn, settings, story: list, since: str) -> None:
    dec = _decisions_since(conn, since)
    section(story, "Decisions")
    if dec:
        data = [[H("ts"), H("series"), H("period"), H("kind"), H("fair", "RIGHT"),
                 H("ask", "RIGHT"), H("edge", "RIGHT"), H("$", "RIGHT")]]
        for d in dec[:30]:
            data.append([C(d["ts_utc"][:16]), C(d["series"]), C(d["period"]),
                         C(d["kind"]), C(f"{(d['fair'] or 0):.3f}", "RIGHT"),
                         C(f"{(d['ask'] or 0):.2f}", "RIGHT"),
                         C(f"{(d['net_edge'] or 0):+.3f}", "RIGHT"),
                         C(f"{(d['size_usd'] or 0):.2f}", "RIGHT")])
        story.append(make_table(data, [PAGE_W * w for w in
                                       (0.16, 0.17, 0.12, 0.11, 0.11, 0.10, 0.12, 0.11)]))
    else:
        story.append(bullet("No decisions in window."))

    marks = _open_marks(conn)
    section(story, "Open positions (latest marks)")
    if marks:
        data = [[H("ticker"), H("series"), H("mid", "RIGHT"), H("uPnL $", "RIGHT")]]
        for mk in marks[:25]:
            data.append([C(mk["ticker"]), C(mk["series"]),
                         C(f"{(mk['mid'] or 0):.2f}", "RIGHT"),
                         C(money(mk["pnl_usd"] or 0.0), "RIGHT")])
        story.append(make_table(data, [PAGE_W * w for w in (0.40, 0.24, 0.16, 0.20)]))
    else:
        story.append(bullet("No open positions."))

    section(story, "Coverage matrix")
    cov = _coverage_matrix(conn)
    by_state: dict[str, int] = {}
    for c in cov:
        by_state[c["state"]] = by_state.get(c["state"], 0) + 1
    story.append(make_kv_table([(k, v) for k, v in sorted(by_state.items())]))
    stuck = [c for c in cov if c["state"] in ("scheduled", "MISSED")][:15]
    for c in stuck:
        story.append(bullet(f"{c['series']} / {c['period']} — {c['state']}"))

    section(story, "Watchdog MISSED / SLA")
    missed = _missed(conn, since)
    if missed:
        for mrow in missed[:15]:
            story.append(bullet(f"{mrow['ts'][:16]}  {mrow['message'][:110]}", "#c0392b"))
    else:
        story.append(bullet("None — clean window."))

    section(story, "Next 7 days")
    data = [[H("calendar"), H("period"), H("scheduled (UTC)")]]
    for e in _calendar_7d(conn)[:20]:
        data.append([C(e["cal"]), C(e["period"]), C(e["scheduled_ts"][:16])])
    story.append(make_table(data, [PAGE_W * w for w in (0.33, 0.33, 0.34)]))


def daily_pdf(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat()
    path = _reports_dir(settings) / f"macro_daily_{now.strftime('%Y%m%d')}.pdf"
    doc = new_doc(str(path))
    story: list = []
    title_block(story, "Someo Park — Macro Daily",
                f"Kalshi macro paper desk · {now.strftime('%Y-%m-%d %H:%M UTC')}")
    _story_common(conn, settings, story, since)
    doc.build(story)
    return str(path)


def weekly_pdf(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat()
    path = _reports_dir(settings) / f"macro_weekly_{now.strftime('%Y%m%d')}.pdf"
    doc = new_doc(str(path))
    story: list = []
    title_block(story, "Someo Park — Macro Weekly",
                f"Kalshi macro paper desk · week ending {now.strftime('%Y-%m-%d')}")

    # narrative (nemotron, degradable — a None just skips the section)
    try:
        from prediction_market_macro.analysis.llm import weekly_narrative
        txt = weekly_narrative(conn, settings)
    except Exception:                              # noqa: BLE001
        txt = None
    if txt:
        section(story, "Narrative (nemotron)")
        from reportlab.platypus import Paragraph
        for para in txt.split("\n\n")[:4]:
            story.append(Paragraph(para.strip(), note_style))
            story.append(Spacer(1, 4))

    # calibration / OOS snapshot
    section(story, "Calibration & OOS")
    ex = conn.execute(
        "SELECT name, metrics_json, created_ts FROM experiments"
        " WHERE name IN ('claims_replay','dfm_gate','bankroll')"
        " ORDER BY created_ts DESC LIMIT 6").fetchall()
    rows = []
    for r in ex:
        try:
            mm = json.loads(r["metrics_json"] or "{}")
        except Exception:                          # noqa: BLE001
            mm = {}
        rows.append((f"{r['name']} ({r['created_ts'][:10]})",
                     "; ".join(f"{k}={v}" for k, v in list(mm.items())[:4]) or "—"))
    story.append(make_kv_table(rows or [("no experiments", "—")],
                               label_w=PAGE_W * 0.45, val_w=PAGE_W * 0.55))
    story.append(Spacer(1, 6))

    _story_common(conn, settings, story, since)
    doc.build(story)
    return str(path)
