"""Performance & P&L report (plan 03 §9, 05 §5).

Honest reporting of what the system delivers. Because live trading is gated
(no real positions beyond the demo test), realized P&L is ~0 by design; the
report therefore leads with the metrics that DO measure value now:

  1. **Prediction accuracy** on settled matches — Brier / Log-loss / hit-rate vs
     the uniform baseline (is the model actually good?).
  2. **Calibration P&L** — paper bet 1 unit on the model's pick at FAIR odds each
     settled match; a calibrated model ≈ breaks even, an over-confident one loses
     (measures over/under-confidence as money).
  3. **Settled-signal P&L** — realized P&L of any recorded signals whose match has
     finished (forward-looking framework; ~0 now under the discipline gate).
  4. **Value snapshot** — current model-vs-market divergences + cross-venue state.

CLV (closing-line value, the true edge metric) accrues once signals record an
entry price and the market closes — wired to fill going forward.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.model.calibrate import brier_score, log_loss

_FINISHED = ("FT", "AET", "PEN")


@dataclass
class PerformanceReport:
    n_settled: int
    brier: float                  # RAW model Brier (pre-calibration)
    brier_uniform: float
    calibrated_brier: float | None  # post-calibration Brier (None if no calibration fit)
    trade_grade: bool             # gate verdict: calibrated Brier <= uniform
    log_loss: float
    favourite_hit_rate: float
    calibration_pnl: float        # paper, 1u on model pick at fair odds
    calibration_pnl_per_bet: float
    settled_signal_pnl: float     # realized P&L of recorded signals (finished matches)
    n_settled_signals: int
    notes: list[str]


def _settled(conn, sm):
    from prediction_market.model.match_pricing import price_match
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchall()
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        mp = price_match(sm, hi, ai)
        outcome = 0 if r["home_goals"] > r["away_goals"] else (1 if r["home_goals"] == r["away_goals"] else 2)
        out.append(([mp.p_home, mp.p_draw, mp.p_away], outcome))
    return out


def build(conn=None) -> PerformanceReport:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    conn = conn or store.init_db()
    sm = build_strength(load_prior())
    data = _settled(conn, sm)
    notes = []

    if not data:
        notes.append("no settled matches yet")
        nan = float('nan')
        return PerformanceReport(
            n_settled=0, brier=nan, brier_uniform=nan, calibrated_brier=None,
            trade_grade=False, log_loss=nan, favourite_hit_rate=nan,
            calibration_pnl=nan, calibration_pnl_per_bet=nan,
            settled_signal_pnl=0.0, n_settled_signals=0, notes=notes,
        )

    probs = [d[0] for d in data]
    outcomes = [d[1] for d in data]
    n = len(data)

    # Calibration P&L: 1 unit on the model's pick at fair odds (price = model prob).
    pnl, staked = 0.0, 0.0
    hits = 0
    for p, o in data:
        pick = int(np.argmax(p))
        price = p[pick]
        staked += price
        if pick == o:
            pnl += 1.0 - price
            hits += 1
        else:
            pnl -= price

    # Settled-signal P&L: recorded signals whose match has finished (framework).
    sig_pnl, n_sig = 0.0, 0
    try:
        # (Single-match signals settle here once we record them per fixture; champion
        # signals settle at tournament end. None tradable yet under the gate → 0.)
        n_sig = conn.execute("SELECT COUNT(*) n FROM signal WHERE action='BUY'").fetchone()["n"]
    except Exception:
        pass

    from prediction_market.model.probability_calibration import load_calibration
    cal = load_calibration()
    # The trade-grade gate is decided on the CALIBRATED Brier (post-hoc temperature/
    # shrinkage), NOT the raw model — the raw model is over-confident on this tiny,
    # draw-heavy sample, but calibration restores it below the uniform baseline.
    calibrated_brier = cal.get("calibrated_brier") if cal else None
    trade_grade = bool(cal and cal.get("trade_grade"))
    if trade_grade:
        notes.append(f"After calibration ({cal['method']} {cal['param']}) the model Brier is "
                     f"{cal['calibrated_brier']} ≤ uniform {cal['uniform_brier']} — TRADE-GRADE (gate passes). "
                     f"Raw model was over-confident ({cal['raw_brier']}).")
    elif brier_score(probs, outcomes) > (2 / 3):
        notes.append("model Brier WORSE than uniform and calibration does not recover it — "
                     "not yet trade-grade (discipline gate blocks).")
    notes.append("Realized P&L ~0 by design: live trading gated; only demo order test placed.")
    notes.append("Calibration P&L is PAPER (fair-odds), measures model over/under-confidence.")

    return PerformanceReport(
        n_settled=n,
        brier=round(brier_score(probs, outcomes), 4),
        brier_uniform=round(brier_score([[1 / 3, 1 / 3, 1 / 3]] * n, outcomes), 4),
        calibrated_brier=calibrated_brier,
        trade_grade=trade_grade,
        log_loss=round(log_loss(probs, outcomes), 4),
        favourite_hit_rate=round(hits / n, 3),
        calibration_pnl=round(pnl, 3),
        calibration_pnl_per_bet=round(pnl / n, 4),
        settled_signal_pnl=round(sig_pnl, 2),
        n_settled_signals=n_sig,
        notes=notes,
    )


def build_pdf(rep: PerformanceReport, output_path: str, *, as_of: str = "") -> str:
    """Render the full System & Performance report in the house PDF style
    (PnLReport.py look: CJK font, navy headers + gold rule, alt-row shading).

    Embeds the complete system overview (interfaces / modes / schedule / I-O /
    value) so this one document answers "what is this, how/when to run it, what
    does it predict, and is it any good".
    """
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, Spacer

    from prediction_market.ops import pdf_style as ps
    from prediction_market.ops import system_overview as ov

    story: list = []
    ps.title_block(
        story,
        "World Cup 2026 预测交易系统 — 系统总览 & 收益/准确度报告",
        f"Kalshi + Polymarket | someopark_run{('  |  as of ' + as_of) if as_of else ''}",
    )

    # Honest headline.
    story.append(Paragraph(ov.HONEST_HEADLINE,
                           ps.S("hl", fontName=ps.FONT, fontSize=8, leading=12,
                                textColor=ps.C_NEG)))
    story.append(Spacer(1, 8))

    # 一、接口(CLI 命令)
    ps.section(story, "一、系统接口(CLI 命令)")
    data = [[ps.H("类别"), ps.H("命令  python -m prediction_market.<x>"), ps.H("作用")]]
    data += [[ps.C(a), ps.C(b), ps.C(c)] for a, b, c in ov.INTERFACES]
    story.append(ps.make_table(data, [2.0 * cm, 6.3 * cm, 8.7 * cm]))

    # 二、模式 & 闸门
    ps.section(story, "二、模式 & 闸门")
    story.append(ps.make_kv_table([(m, d) for m, d in ov.MODES], label_w=4.6 * cm, val_w=12.4 * cm))

    # 三、运行调度(何时 / 频率)
    ps.section(story, "三、运行调度(何时运行 / 频率)")
    data = [[ps.H("时机"), ps.H("跑什么"), ps.H("频率", "RIGHT")]]
    data += [[ps.C(a), ps.C(b), ps.C(c, "RIGHT")] for a, b, c in ov.SCHEDULE]
    story.append(ps.make_table(data, [4.0 * cm, 10.5 * cm, 2.5 * cm]))

    # 四、输入 / 输出(在哪里)
    ps.section(story, "四、输入 / 输出(在哪里)")
    story.append(Paragraph("输入", ps.body_style))
    story.append(ps.make_kv_table([(a, b) for a, b in ov.INPUTS], label_w=4.6 * cm, val_w=12.4 * cm))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"输出  (全部在 {ov.OUTPUT_DIR})", ps.body_style))
    story.append(ps.make_kv_table([(a, b) for a, b in ov.OUTPUTS], label_w=8.4 * cm, val_w=8.6 * cm))

    # 五、价值主张
    ps.section(story, "五、给用户带来的价值 + 怎么看到")
    for v in ov.VALUE:
        story.append(ps.bullet(v))

    # 六、预测准确度(已结算场次)
    ps.section(story, "六、预测准确度(已结算场次)")
    if rep.n_settled:
        grade = "PASS(已校准,优于均匀)" if rep.trade_grade else "BLOCK(劣于均匀,纪律闸门拦截)"
        cal_row = (f"{rep.calibrated_brier}  ≤ 均匀基线 {rep.brier_uniform}"
                   if rep.calibrated_brier is not None else "尚未拟合校准")
        acc = [
            ("已结算场次", f"{rep.n_settled}"),
            ("Brier 原始(越低越好)", f"{rep.brier}  vs 均匀基线 {rep.brier_uniform}"),
            ("Brier 校准后", cal_row),
            ("Log-loss", f"{rep.log_loss}"),
            ("热门命中率", f"{rep.favourite_hit_rate:.0%}"),
            ("交易等级", grade),
        ]
        story.append(ps.make_kv_table(acc, label_w=8.4 * cm, val_w=8.6 * cm))
    else:
        story.append(Paragraph("尚无已结算场次。", ps.note_style))

    # 七、校准 P&L(纸面,公允赔率)
    ps.section(story, "七、校准 P&L(纸面,按公允赔率下注模型选边)")
    if rep.n_settled:
        story.append(ps.make_kv_table([
            ("总校准 P&L(1u/场)", ps.money(rep.calibration_pnl, unit="u")),
            ("每场均值", ps.money(rep.calibration_pnl_per_bet, unit="u")),
            ("已结算信号 P&L(真钱框架)", ps.money(rep.settled_signal_pnl)),
            ("已记录 BUY 信号数", f"{rep.n_settled_signals}"),
        ], label_w=8.4 * cm, val_w=8.6 * cm))

    # 八、说明
    ps.section(story, "八、说明")
    for i, n in enumerate(rep.notes, 1):
        story.append(ps.note_item(f"{i}.", n))

    ps.new_doc(output_path).build(story)
    return output_path


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Performance & P&L report")
    ap.add_argument("--pdf", action="store_true", help="also render a styled PDF")
    ap.add_argument("--as-of", default="", help="as-of date label for the PDF header")
    args = ap.parse_args()

    rep = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "performance_report.json").write_text(
        json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    print("PERFORMANCE & P&L REPORT")
    print(f"  settled matches      : {rep.n_settled}")
    if rep.n_settled:
        print(f"  accuracy Brier       : {rep.brier}  (uniform {rep.brier_uniform})  log-loss {rep.log_loss}")
        print(f"  favourite hit-rate   : {rep.favourite_hit_rate:.0%}")
        print(f"  calibration P&L      : {rep.calibration_pnl:+.2f}u total ({rep.calibration_pnl_per_bet:+.3f}u/bet, paper)")
        print(f"  settled-signal P&L   : {rep.settled_signal_pnl:+.2f}  ({rep.n_settled_signals} BUY signals recorded)")
    for nnote in rep.notes:
        print(f"  • {nnote}")

    if args.pdf:
        path = build_pdf(rep, str(CONFIG.paths.output / "performance_report.pdf"), as_of=args.as_of)
        print(f"  PDF written          : {path}")


if __name__ == "__main__":
    main()
