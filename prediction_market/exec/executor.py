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
