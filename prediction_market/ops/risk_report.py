"""Risk report (plan 04 §6, 05 §5, 07). The pre-trade risk picture.

Consolidates every guard rail and exposure into one report:
  * **Trading gates** — env (demo/prod), KALSHI/PMUS trading-enabled flags, the
    hard $1 test cap, and the calibration gate (model must beat the uniform
    baseline to trade) — so it's explicit WHY orders are or aren't allowed.
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

from prediction_market.config import CONFIG


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


def _kalshi_demo_balance() -> float | None:
    try:
        from prediction_market.venues.kalshi.orders import KalshiOrders
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


def build(conn=None) -> RiskReport:
    from prediction_market.ingest import store

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
    used = store.monthly_request_count(conn)
    api_budget = {"used": used, "cap": CONFIG.soccer.monthly_budget,
                  "pct": round(used / CONFIG.soccer.monthly_budget, 3)}

    # Calibration gate: is the model trade-grade?
    cal = {"status": "unknown"}
    oos_path = CONFIG.paths.output / "oos_report.json"
    if oos_path.exists():
        brier = json.loads(oos_path.read_text(encoding="utf-8")).get("brier")
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

    return RiskReport(gates, limits, venue_balances, exposure, api_budget, cal, kill, blocked)


def build_pdf(rep: RiskReport, output_path: str, *, as_of: str = "") -> str:
    """Render the risk report in the house PDF style (PnLReport.py look)."""
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer

    from prediction_market.ops import pdf_style as ps

    story: list = []
    ps.title_block(
        story,
        "World Cup 2026 预测交易系统 — 风险分析报告",
        f"Kalshi + Polymarket | someopark_run{('  |  as of ' + as_of) if as_of else ''}",
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

    # 三、各场余额
    ps.section(story, "三、各场余额")
    vb = rep.venue_balances
    story.append(ps.make_kv_table([
        ("Kalshi demo", ps.money(vb["kalshi_demo_usd"]) if isinstance(vb["kalshi_demo_usd"], (int, float)) else str(vb["kalshi_demo_usd"])),
        ("Polymarket US", ps.money(vb["polymarket_us_usd"]) if isinstance(vb["polymarket_us_usd"], (int, float)) else str(vb["polymarket_us_usd"])),
        ("Kalshi prod", str(vb["kalshi_prod_usd"])),
    ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 四、敞口 & API 预算
    ps.section(story, "四、敞口 & API 预算")
    ex, ab = rep.exposure, rep.api_budget
    story.append(ps.make_kv_table([
        ("未平仓位", f'{ex["open_positions"]}'),
        ("总在险金额", ps.money(ex["total_at_risk_usd"])),
        ("敞口说明", ex.get("note", ex["max_total_allowed_note"])),
        ("API 月用量", f'{ab["used"]} / {ab["cap"]}  ({ab["pct"]:.0%})'),
    ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 五、校准闸门
    ps.section(story, "五、校准闸门(模型是否够格交易)")
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

    # 六、拦截 / 护栏 (■ marker — PingFang has no ⛔ glyph, so use a red square)
    ps.section(story, "六、拦截 / 护栏(BLOCKED)")
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
    print(f"  calibration  : {rep.calibration_gate.get('status')}")
    print("  BLOCKED / guard rails:")
    for b in rep.blocked_summary:
        print(f"    ⛔ {b}")

    if args.pdf:
        path = build_pdf(rep, str(CONFIG.paths.output / "risk_report.pdf"), as_of=args.as_of)
        print(f"  PDF written  : {path}")


if __name__ == "__main__":
    main()
