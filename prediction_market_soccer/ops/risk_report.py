"""Risk report (plan 04 §6, 05 §5, 07, TRANSFORM_PLAN C-2). The pre-trade risk picture.

Consolidates every guard rail and exposure into one report:
  * **Trading gates** — env (demo/prod), KALSHI/PMUS trading-enabled flags, the
    hard $1 test cap, and the calibration gate (model must beat the uniform
    baseline to trade) — so it's explicit WHY orders are or aren't allowed.
  * **Kalshi series inventory** — WHICH markets this desk is subscribed to, straight
    off the league registry. The budget line says how many requests we spend; without
    the inventory nobody can tell whether that spend covers 12 competitions or 3, and
    a competition silently missing a series family reads as "no edge there" instead of
    "we never looked". Registry-derived, so it can never drift from what the venue
    readers actually query.
  * **Venue balances** — demo Kalshi + Polymarket US (read-only). The PROD Kalshi
    key is intentionally NOT queried (standing rule: don't use it unless told).
  * **Exposure** — current $ at risk by market / theme / total vs the hard caps.
  * **API budget** — monthly request usage vs the 7000 cap.
  * **Kill-switch** — daily-loss status.

Read-only. No orders. Designed to be read before any trading decision.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from prediction_market_soccer.config import CONFIG

# Families the registry maps per competition. Ordered so the report always reads
# game → season → derivative, whatever order a registry entry happens to declare.
_FAMILY_ORDER = ("game", "advance", "champion", "top4", "top8", "top", "ro16", "ro8", "ro4",
                 "finalist", "relegation", "last", "topscorer", "total", "btts", "spread",
                 "score", "teamtotal", "corners")


@dataclass
class RiskReport:
    gates: dict
    limits: dict
    venue_balances: dict
    exposure: dict
    api_budget: dict
    calibration_gate: dict
    kill_switch: dict
    blocked_summary: list[str] = field(default_factory=list)
    kalshi_series: dict = field(default_factory=dict)


def _kalshi_demo_balance() -> float | None:
    try:
        from prediction_market_soccer.venues.kalshi.orders import KalshiOrders
        kid, kp = os.getenv("KALSHI_API_KEY_ID"), os.getenv("KALSHI_PRIVATE_KEY_PATH")
        if not (kid and kp):
            return None
        base = "https://external-api.demo.kalshi.co/trade-api/v2"
        return float(KalshiOrders(kid, kp, base_url=base).get_balance().cash)
    except Exception:
        return None


def _pmus_balance() -> float | None:
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS(key_id=os.environ["PMUS_KEY_ID"], secret_key=os.environ["PMUS_SECRET"])
        bals = c.account.balances().get("balances", [])
        return float(bals[0]["currentBalance"]) if bals else 0.0
    except Exception:
        return None


def _kalshi_series_inventory() -> dict:
    """Which Kalshi series this desk subscribes to / monitors, per competition (C-2).

    Read straight off ``config.leagues.REGISTRY`` — the same map the discovery layer
    queries — so the report cannot claim coverage the venue readers do not have.
    ``champion_event`` appends the registry's season suffix (SA competitions run on the
    calendar year, so their season events end ``-26`` while Europe's end ``-27``); that
    full event ticker is what ``venues/champion_prices`` actually asks Kalshi for.
    """
    from prediction_market_soccer.config import leagues as L

    subscribed: list[dict] = []
    unsubscribed: list[dict] = []
    gaps: list[str] = []            # blocks the core product — surfaced in blocked_summary
    coverage_notes: list[str] = []  # a season family the venue simply doesn't list — informational
    distinct: set[str] = set()
    for c in L.REGISTRY.values():
        fams = dict(c.kalshi)
        extra = sorted(k for k in fams if k not in _FAMILY_ORDER)
        ordered = [k for k in _FAMILY_ORDER if k in fams] + extra
        champ = fams.get("champion")
        row = {
            "league": c.key, "name": c.name, "zh": c.zh, "kind": c.kind,
            "game": fams.get("game"),
            "champion": champ,
            # The season event ticker, not just the series: KXPREMIERLEAGUE-27 / KXBRASILEIRO-26.
            "champion_event": f"{champ}{c.season_year_suffix}" if champ else None,
            "advance": fams.get("advance"),
            "n_families": len(fams),
            "families": {k: fams[k] for k in ordered},
        }
        if c.enabled:
            subscribed.append(row)
            distinct.update(fams.values())
            if not row["game"]:
                gaps.append(f"{c.key}: no per-match GAME series in the registry")
            if not champ:
                # Not necessarily a defect: a playoff-decided title (Argentina) has no
                # single season-champion series on the venue. Recorded so "no champion
                # signal for this competition" reads as coverage, never as a silent hole.
                coverage_notes.append(f"{c.key}: no season CHAMPION series — "
                                      f"champion price/divergence is not produced for this competition")
        else:
            unsubscribed.append({"league": c.key, "name": c.name, "zh": c.zh,
                                 "note": c.tier_note or "extension slot (enabled=False)"})
    return {
        "n_competitions": len(subscribed),
        "n_series_distinct": len(distinct),
        "n_market_families": sum(r["n_families"] for r in subscribed),
        "competitions": subscribed,
        "not_subscribed": unsubscribed,
        "gaps": gaps,
        "coverage_notes": coverage_notes,
        "source": "config/leagues.py REGISTRY (same map venues/kalshi/discovery queries)",
    }


def build(conn=None) -> RiskReport:
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    v, r = CONFIG.venue, CONFIG.risk

    gates = {
        "kalshi_env": v.kalshi_env,
        "kalshi_trading_enabled": v.kalshi_trading_enabled,
        "pmus_trading_enabled": v.pmus_trading_enabled,
        "hard_order_cap_usd": r.max_test_order_usd,
        "executable_venues": list(v.executable_venues),
    }
    limits = {
        "kelly_fraction": r.kelly_fraction,
        "max_single_market_frac": r.max_single_market_frac,
        "max_theme_frac": r.max_theme_frac,
        "min_net_edge_theta": r.min_net_edge,
        "min_net_lock_theta_arb": r.min_net_lock,
        "daily_loss_killswitch_frac": r.daily_loss_killswitch_frac,
    }
    venue_balances = {
        "kalshi_demo_usd": _kalshi_demo_balance(),
        "kalshi_prod_usd": "not queried (standing rule: prod key only on explicit instruction)",
        "polymarket_us_usd": _pmus_balance(),
    }
    # Exposure: no fills are recorded yet (live trading gated, only a demo test
    # order was ever placed and cancelled). There is no `position`/fills table by
    # design, so realized exposure is 0 until execution is enabled and tracked.
    exposure = {
        "open_positions": 0,
        "total_at_risk_usd": 0.0,
        "max_total_allowed_note": "per-market <= 5% bankroll, per-theme <= 10% (plan 04 §6)",
        "note": "no fills recorded (execution gated; demo test order placed+cancelled only)",
    }
    # API-Football bills per DAY (Pro = 7,500/day, resets 00:00 UTC) — the daily figure is
    # the real budget; monthly is a loose backstop.
    used = store.daily_request_count(conn)
    cap = CONFIG.soccer.daily_budget
    api_budget = {"used": used, "cap": cap, "pct": round(used / cap, 3) if cap else 0.0,
                  "period": "day",
                  "month_used": store.monthly_request_count(conn),
                  "month_cap": CONFIG.soccer.monthly_budget}

    # Calibration gate: is the CALIBRATED model trade-grade? The raw model is
    # over-confident; we fit a calibration map (calibration.json) and gate on the
    # calibrated Brier vs the uniform baseline.
    cal = {"status": "unknown"}
    cal_path = CONFIG.paths.output / "calibration.json"
    if cal_path.exists():
        c = json.loads(cal_path.read_text(encoding="utf-8"))
        cb, rb = c.get("calibrated_brier"), c.get("raw_brier")
        if cb is not None:
            ok = bool(c.get("trade_grade"))
            cal = {"raw_brier": rb, "calibrated_brier": cb, "uniform_baseline": round(2 / 3, 4),
                   "method": c.get("method"), "param": c.get("param"), "trade_grade": ok,
                   "status": "PASS (calibrated)" if ok else "BLOCK (model not yet calibrated)"}
    elif (CONFIG.paths.output / "oos_report.json").exists():
        brier = json.loads((CONFIG.paths.output / "oos_report.json").read_text(encoding="utf-8")).get("brier")
        if brier is not None:
            ok = brier <= 2 / 3
            cal = {"oos_brier": round(brier, 4), "uniform_baseline": round(2 / 3, 4),
                   "trade_grade": ok, "status": "PASS" if ok else "BLOCK (model not yet calibrated)"}

    kill = {"daily_loss_killswitch_frac": r.daily_loss_killswitch_frac, "triggered": False,
            "note": "no live PnL tracked yet"}

    blocked = []
    if v.kalshi_env == "prod" and not v.kalshi_trading_enabled:
        blocked.append("Kalshi PROD orders BLOCKED (KALSHI_TRADING_ENABLED=false, real money).")
    if not v.pmus_trading_enabled:
        blocked.append("Polymarket US orders BLOCKED (PMUS_TRADING_ENABLED=false, real money).")
    if cal.get("status", "").startswith("BLOCK"):
        blocked.append("All edge signals BLOCKED by calibration gate (model not trade-grade).")
    if venue_balances["polymarket_us_usd"] == 0.0:
        blocked.append("Polymarket US has $0 USDC.e — cannot trade until funded.")
    blocked.append(f"Every order hard-capped at ${r.max_test_order_usd:.2f} notional.")

    series = _kalshi_series_inventory()
    for g in series["gaps"]:
        blocked.append(f"Kalshi coverage gap — {g} (that market is never priced).")

    return RiskReport(gates, limits, venue_balances, exposure, api_budget, cal, kill, blocked,
                      kalshi_series=series)


def build_pdf(rep: RiskReport, output_path: str, *, as_of: str = "") -> str:
    """Render the risk report in the house PDF style (PnLReport.py look)."""
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer

    from prediction_market_soccer.ops import pdf_style as ps

    story: list = []
    ps.title_block(
        story,
        "俱乐部足球预测交易系统 — 风险分析报告",
        f"Kalshi + Polymarket | 12 项赛事 | someopark_run{('  |  as of ' + as_of) if as_of else ''}",
    )
    story.append(Paragraph(
        "盘前必读:本报告汇总所有护栏与敞口。只读,不下任何单。真钱实盘被双闸 + 校准闸门 + $1 硬顶全程拦截。",
        ps.S("hl", fontName=ps.FONT, fontSize=8, leading=12, textColor=ps.C_NEG)))
    story.append(Spacer(1, 8))

    # 一、交易闸门
    ps.section(story, "一、交易闸门(为什么能 / 不能下单)")
    g = rep.gates
    story.append(ps.make_kv_table([
        ("Kalshi 环境", g["kalshi_env"]),
        ("Kalshi 交易开关", g["kalshi_trading_enabled"]),
        ("Polymarket US 交易开关", g["pmus_trading_enabled"]),
        ("单单硬顶 (USD notional)", ps.money(g["hard_order_cap_usd"])),
        ("可执行场所", ", ".join(g["executable_venues"]) or "(无)"),
    ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 二、仓位限额
    ps.section(story, "二、仓位限额")
    lm = rep.limits
    story.append(ps.make_kv_table([
        ("Kelly 分数", lm["kelly_fraction"]),
        ("单市场上限", f'{lm["max_single_market_frac"]:.0%} bankroll'),
        ("单主题上限", f'{lm["max_theme_frac"]:.0%} bankroll'),
        ("最小净边缘 θ(相对价值)", lm["min_net_edge_theta"]),
        ("最小净锁定 θ(套利)", lm["min_net_lock_theta_arb"]),
        ("单日亏损熔断", f'{lm["daily_loss_killswitch_frac"]:.0%}'),
    ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 三、Kalshi 系列清单 — 我们到底订阅/监控了哪些市场(C-2)
    ks = rep.kalshi_series or {}
    ps.section(story, f'三、Kalshi 系列清单(订阅 {ks.get("n_competitions", 0)} 项赛事 / '
                      f'{ks.get("n_series_distinct", 0)} 个系列)')
    if ks.get("competitions"):
        data = [[ps.H("赛事"), ps.H("单场 3-way 系列"), ps.H("赛季冠军事件"),
                 ps.H("晋级盘"), ps.H("族数", "RIGHT")]]
        for c in ks["competitions"]:
            data.append([
                ps.C(f'{c["zh"]} {c["name"]}'),
                ps.C(c["game"] or '<font color="#c0392b">—</font>'),
                ps.C(c["champion_event"] or '<font color="#c0392b">—</font>'),
                ps.C(c["advance"] or '<font color="#888">—</font>'),
                ps.C(str(c["n_families"]), "RIGHT"),
            ])
        story.append(ps.make_table(data, [4.2 * cm, 4.3 * cm, 4.6 * cm, 3.0 * cm, 0.9 * cm]))
    if ks.get("not_subscribed"):
        story.append(Paragraph(
            "未订阅(扩展位,registry enabled=False):"
            + "、".join(f'{c["zh"]} {c["name"]}' for c in ks["not_subscribed"]), ps.note_style))
    for note in ks.get("coverage_notes", []):
        story.append(Paragraph(f"覆盖说明:{note}", ps.note_style))
    story.append(Paragraph(f'来源:{ks.get("source", "")}', ps.note_style))

    # 四、各场余额
    ps.section(story, "四、各场余额")
    vb = rep.venue_balances
    story.append(ps.make_kv_table([
        ("Kalshi demo", ps.money(vb["kalshi_demo_usd"]) if isinstance(vb["kalshi_demo_usd"], (int, float)) else str(vb["kalshi_demo_usd"])),
        ("Polymarket US", ps.money(vb["polymarket_us_usd"]) if isinstance(vb["polymarket_us_usd"], (int, float)) else str(vb["polymarket_us_usd"])),
        ("Kalshi prod", str(vb["kalshi_prod_usd"])),
    ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 五、敞口 & API 预算
    ps.section(story, "五、敞口 & API 预算")
    ex, ab = rep.exposure, rep.api_budget
    story.append(ps.make_kv_table([
        ("未平仓位", f'{ex["open_positions"]}'),
        ("总在险金额", ps.money(ex["total_at_risk_usd"])),
        ("敞口说明", ex.get("note", ex["max_total_allowed_note"])),
        ("API 月用量", f'{ab["used"]} / {ab["cap"]}  ({ab["pct"]:.0%})'),
    ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 六、校准闸门
    ps.section(story, "六、校准闸门(模型是否够格交易)")
    cg = rep.calibration_gate
    if "oos_brier" in cg:
        story.append(ps.make_kv_table([
            ("OOS Brier", cg["oos_brier"]),
            ("均匀基线", cg["uniform_baseline"]),
            ("交易等级", cg["trade_grade"]),
            ("状态", cg["status"]),
        ], label_w=8.4 * cm, val_w=8.6 * cm))
    else:
        story.append(Paragraph(f'状态:{cg.get("status")}', ps.note_style))

    # 七、拦截 / 护栏 (■ marker — PingFang has no ⛔ glyph, so use a red square)
    ps.section(story, "七、拦截 / 护栏(BLOCKED)")
    for b in rep.blocked_summary:
        story.append(ps.bullet("■ " + b, color="#c0392b"))

    ps.new_doc(output_path).build(story)
    return output_path


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Risk report")
    ap.add_argument("--pdf", action="store_true", help="also render a styled PDF")
    ap.add_argument("--as-of", default="", help="as-of date label for the PDF header")
    args = ap.parse_args()

    rep = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "risk_report.json").write_text(
        json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    print("RISK REPORT")
    print(f"  gates        : env={rep.gates['kalshi_env']} | kalshi_trading={rep.gates['kalshi_trading_enabled']} "
          f"| pmus_trading={rep.gates['pmus_trading_enabled']} | order_cap=${rep.gates['hard_order_cap_usd']}")
    print(f"  balances     : Kalshi-demo=${rep.venue_balances['kalshi_demo_usd']} | "
          f"PolyUS=${rep.venue_balances['polymarket_us_usd']} | Kalshi-prod={rep.venue_balances['kalshi_prod_usd']}")
    print(f"  exposure     : {rep.exposure['open_positions']} positions, ${rep.exposure['total_at_risk_usd']} at risk")
    print(f"  API budget   : {rep.api_budget['used']}/{rep.api_budget['cap']} ({rep.api_budget['pct']:.0%})")
    ks = rep.kalshi_series or {}
    print(f"  KX series    : {ks.get('n_competitions', 0)} competitions, "
          f"{ks.get('n_series_distinct', 0)} distinct series, "
          f"{ks.get('n_market_families', 0)} market families")
    for c in ks.get("competitions", []):
        print(f"    {c['league']:<13} game={c['game'] or '-':<22} "
              f"champion={c['champion_event'] or '-':<24} families={c['n_families']}")
    print(f"  calibration  : {rep.calibration_gate.get('status')}")
    print("  BLOCKED / guard rails:")
    for b in rep.blocked_summary:
        print(f"    ⛔ {b}")

    if args.pdf:
        path = build_pdf(rep, str(CONFIG.paths.output / "risk_report.pdf"), as_of=args.as_of)
        print(f"  PDF written  : {path}")


if __name__ == "__main__":
    main()
