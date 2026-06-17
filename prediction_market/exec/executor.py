"""Signal generation + capped execution (plan 04 §7/§8, 08).

End-to-end: model probability → de-vig vs the tradable venue (Kalshi) → net edge
→ fractional-Kelly size → hard caps → (optional) capped order submission.

DISCIPLINE GATES (plan 04 §8 — any one blocks the trade):
  * calibration gate — if the model's own OOS Brier is WORSE than the uniform
    baseline on this market family, the model is unreliable here → BLOCK every
    signal (no trading on self-diagnosed miscalibration, plan 03 §9 / 04 §1b);
  * edge gate — net_edge must clear theta;
  * the HARD $1 test cap on every order (venues/kalshi/orders).

This makes the honest behaviour explicit: on the champion market my v1 model
disagrees with Kalshi/Global mostly because IT is miscalibrated (Brazil), so the
executor REFUSES those signals rather than trading false edge.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from prediction_market.config import CONFIG
from prediction_market.strategy.edge import compute_edge
from prediction_market.strategy.sizing import size_position
from prediction_market.strategy.xv_monitor import compare_champion

BRIER_BASELINE = 2.0 / 3.0


def cap_count(ask: float, max_usd: float | None = None) -> int:
    """Largest contract count whose notional (count×ask) stays within the test cap."""
    cap = CONFIG.risk.max_test_order_usd if max_usd is None else max_usd
    count = max(1, int(cap / max(ask, 1e-3)))
    while count > 1 and count * ask > cap + 1e-9:
        count -= 1
    return count


@dataclass
class Signal:
    market_family: str
    team_id: str
    name: str
    venue: str
    ticker_or_slug: str | None
    side: str                 # "yes"
    p_model: float
    ask: float
    net_edge: float
    target_stake_usd: float
    count: int                # contracts (test mode: sized to <= $1 notional)
    price: float
    kind: str                 # "tradable" | "blocked:model_uncalibrated" | "blocked:no_edge"
    action: str               # "BUY" | "HOLD"


def _calibration_ok(family: str = "champion", *, path=None) -> tuple[bool, str]:
    """Read the latest OOS report; block if Brier is worse than the baseline."""
    path = path or (CONFIG.paths.output / "oos_report.json")
    if not path.exists():
        return False, "no OOS report — run oos_eval first (cannot verify calibration)"
    oos = json.loads(path.read_text(encoding="utf-8"))
    brier = oos.get("brier")
    if brier is None:
        return False, "OOS report has no Brier"
    if brier > BRIER_BASELINE:
        return False, f"OOS Brier {brier:.3f} > uniform {BRIER_BASELINE:.3f} — model uncalibrated, BLOCK"
    return True, f"OOS Brier {brier:.3f} <= baseline — calibration acceptable"


def generate_champion_signals(*, bankroll: float = 1000.0, fee: float = 0.01,
                              slippage: float = 0.005, theta: float | None = None,
                              ticker_map: dict[str, str] | None = None) -> tuple[list[Signal], str]:
    """Signals from model vs Kalshi de-vigged champion prices, calibration-gated."""
    theta = CONFIG.risk.min_net_edge if theta is None else theta
    cal_ok, cal_msg = _calibration_ok("champion")
    rows = compare_champion(n_sims=30_000)

    sigs: list[Signal] = []
    for r in rows:
        if r.p_kalshi is None or r.kalshi_ask is None:
            continue
        e = compute_edge(r.p_model, r.kalshi_ask, sigma_p=0.04,
                         k=CONFIG.risk.shrink_k, fee=fee, slippage=slippage, theta=theta)
        # Size on the (shrunk) edge; then clamp to the hard $1 test notional.
        sz = size_position(e.p_eff, r.kalshi_ask, bankroll)
        count = cap_count(r.kalshi_ask)

        if not cal_ok:
            kind, action = "blocked:model_uncalibrated", "HOLD"
        elif e.tradable:
            kind, action = "tradable", "BUY"
        else:
            kind, action = "blocked:no_edge", "HOLD"

        sigs.append(Signal(
            market_family="champion", team_id=r.team_id, name=r.name, venue="kalshi",
            ticker_or_slug=(ticker_map or {}).get(r.team_id),
            side="yes", p_model=r.p_model, ask=r.kalshi_ask, net_edge=round(e.net_edge, 4),
            target_stake_usd=round(min(sz.stake, CONFIG.risk.max_test_order_usd), 2),
            count=count, price=r.kalshi_ask, kind=kind, action=action,
        ))
    sigs.sort(key=lambda s: -s.net_edge)
    return sigs, cal_msg


@dataclass
class MatchSignal:
    """One pre-match 3-way betting decision from the DECISION model (plan 20),
    capped to the $1 hard test notional. The decision (side + ideal stake) is the
    SAME decision_model.decide() the historical bet log uses, so production and the
    track record share one brain. Order submission stays gated."""
    market_family: str            # "match_3way"
    fixture_id: int | None
    match: str
    kickoff: str | None
    venue: str | None
    side: str | None              # home/draw/away (None ⇒ no bet)
    side_label: str
    p_model: float | None
    market_devig: float | None
    ask: float | None             # executable price (0-1)
    net_edge: float | None
    confidence_k: float | None
    target_stake_usd: float       # decide()'s ideal stake ($0.2–$2)
    count: int                    # contracts after the $1 hard cap
    capped_notional_usd: float    # actual $ at the cap (≤ max_test_order_usd)
    kind: str                     # "tradable" | "blocked:no_edge" | "blocked:model_uncalibrated"
    action: str                   # "BUY" | "HOLD"
    reason: str


def _quotes_from_upcoming(m: dict):
    """{side: SideQuote} from an upcoming_export row — cheapest executable ask per side
    across venues + that venue's de-vig. Same selection the bet log uses on PRE rows."""
    from prediction_market.strategy.decision_model import SideQuote
    q = {}
    for s in ("home", "draw", "away"):
        cands = []
        for ven_key, ven_label in (("kalshi", "kalshi"), ("poly_us", "poly_us")):
            blk = m.get(ven_key)
            side_q = (blk or {}).get(s) if blk else None
            if side_q and side_q.get("ask") is not None:
                cands.append((side_q["ask"], ven_label, (blk.get("devig") or {}).get(s)))
        if not cands:
            q[s] = SideQuote()
            continue
        ask, ven, dv = min(cands, key=lambda t: t[0])
        q[s] = SideQuote(ask=ask, devig=dv, venue=ven)
    return q


def generate_match_signals(conn=None, *, limit: int = 12) -> tuple[list[MatchSignal], str, bool]:
    """Pre-match 3-way signals for the next `limit` fixtures, via decision_model.decide()
    on REAL live venue quotes — value side, confidence-sized, then clamped to the $1 cap.
    Calibration-gated: if the model is not trade-grade, every signal is HOLD."""
    from prediction_market.ingest import store
    from prediction_market.model.probability_calibration import load_calibration
    from prediction_market.ops import upcoming_export
    from prediction_market.strategy.decision_model import decide

    conn = conn or store.init_db()
    cal = load_calibration()
    gate_open = bool(cal and cal.get("trade_grade"))
    _ub = (cal.get("uniform_brier") if cal else None) or BRIER_BASELINE
    _cb = cal.get("calibrated_brier") if cal else None
    calib_conf = max(-1.0, min(1.0, (_ub - _cb) / _ub)) if (_cb is not None and _ub) else 0.0
    cal_msg = (f"calibrated Brier {_cb} ≤ uniform {_ub} — trade-grade" if gate_open
               else f"calibrated Brier {_cb} vs uniform {_ub} — gate BLOCKS live orders")

    rows = upcoming_export.build(limit=limit, conn=conn)
    sigs: list[MatchSignal] = []
    for m in rows:
        if m.get("tentative") or not m.get("model"):
            continue
        quotes = _quotes_from_upcoming(m)
        d = decide(m["model"], quotes, calib_confidence=calib_conf, form=m.get("form"), gate_open=gate_open)
        label = {"home": m["home"]["name"], "draw": "Draw", "away": m["away"]["name"]}
        match_str = f'{m["home"]["name"]} v {m["away"]["name"]}'
        if d.side is None:
            kind = "blocked:model_uncalibrated" if not gate_open else "blocked:no_edge"
            sigs.append(MatchSignal(
                market_family="match_3way", fixture_id=m.get("fixture_id"), match=match_str,
                kickoff=m.get("kickoff"), venue=None, side=None, side_label="—",
                p_model=None, market_devig=None, ask=None, net_edge=d.net_edge,
                confidence_k=d.confidence_k, target_stake_usd=0.0, count=0, capped_notional_usd=0.0,
                kind=kind, action="HOLD", reason=d.reason))
            continue
        ask = quotes[d.side].ask
        count = cap_count(ask)                      # HARD $1 cap on the actual order
        sigs.append(MatchSignal(
            market_family="match_3way", fixture_id=m.get("fixture_id"), match=match_str,
            kickoff=m.get("kickoff"), venue=d.venue, side=d.side, side_label=label[d.side],
            p_model=d.model_prob, market_devig=d.market_prob, ask=ask, net_edge=d.net_edge,
            confidence_k=d.confidence_k, target_stake_usd=d.stake_usd, count=count,
            capped_notional_usd=round(count * ask, 2),
            kind="tradable" if gate_open else "blocked:model_uncalibrated",
            action="BUY" if gate_open else "HOLD", reason=d.reason))
    sigs.sort(key=lambda s: -(s.net_edge or -9))
    return sigs, cal_msg, gate_open


def build_match_signals(conn=None, *, limit: int = 12) -> dict:
    """Daily production payload: the decision model's pre-match bets, capped to $1.
    Written by refresh_all → data/output/match_signals.json (orders stay gated)."""
    from datetime import datetime, timezone
    sigs, cal_msg, gate_open = generate_match_signals(conn, limit=limit)
    bets = [s for s in sigs if s.action == "BUY"]
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "calibration": cal_msg, "gate_open": gate_open,
        "hard_cap_usd": CONFIG.risk.max_test_order_usd,
        "n": len(sigs), "n_bets": len(bets),
        "note": ("Pre-match decision_model.decide() on real venue quotes: value side, "
                 "confidence-sized $0.2–$2, then clamped to the $1 hard cap. Same decision "
                 "as the historical bet log. Orders submit only when the gate opens AND "
                 "venue trading is enabled."),
        "signals": [asdict(s) for s in sigs],
    }


def run(*, bankroll: float = 1000.0) -> dict:
    """Dry-run signal generation + report (NO order submission here)."""
    sigs, cal_msg = generate_champion_signals(bankroll=bankroll)
    tradable = [s for s in sigs if s.kind == "tradable"]
    blocked_cal = [s for s in sigs if s.kind == "blocked:model_uncalibrated"]
    CONFIG.paths.ensure()
    out = CONFIG.paths.output / "signals.json"
    out.write_text(json.dumps({
        "market": "champion", "calibration": cal_msg,
        "n_signals": len(sigs), "n_tradable": len(tradable),
        "n_blocked_uncalibrated": len(blocked_cal),
        "signals": [asdict(s) for s in sigs],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"calibration": cal_msg, "tradable": tradable, "blocked": blocked_cal,
            "top_raw_edges": sigs[:5], "out": str(out)}


if __name__ == "__main__":
    res = run()
    print("Champion strategy run (model vs Kalshi, calibration-gated)")
    print(f"  calibration: {res['calibration']}")
    print(f"  → {len(res['tradable'])} tradable, {len(res['blocked'])} blocked by calibration gate")
    print("  top raw model-vs-market edges (what a naive bot would chase):")
    for s in res["top_raw_edges"]:
        print(f"    {s.name:<14} ask={s.ask:.3f} p_model={s.p_model:.3f} "
              f"net_edge={s.net_edge:+.3f} → {s.action} [{s.kind}]")
    print(f"  wrote {res['out']}")
