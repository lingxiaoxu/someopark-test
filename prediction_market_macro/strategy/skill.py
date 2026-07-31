"""strategy/skill.py — skill-aware defensive gating (improvement B, 2026-07-31).

The decision replay showed the failure mode plainly: when the model's OOS Brier is
WORSE than the market's, computed net_edge is mostly model error — the market is
right about the disagreement, and betting it realizes negative edge_capture.

Rule: if the series' trailing paired Brier ratio (model/market) exceeds RATIO_MAX,
decide_all doubles min_net_edge and halves size for that series. The bar drops back
automatically the moment the model earns parity — no manual switch.
"""
from __future__ import annotations

TRAIL = 20
RATIO_MAX = 1.05          # behind by >5%: defensive (double bar, half size)
BLOCK_RATIO = 1.50        # behind by >50%: model-path bets are fee donations —
                          # BLOCKED entirely (arb/snipe/favorite paths unaffected;
                          # OOS scoring continues from preds, which need no bets)
MIN_PAIRED = 6


def skill_ratio(conn, series: str, offset: str = "-1h") -> float | None:
    """Trailing mean Brier(model)/Brier(market) over PAIRED periods (same event
    scored for both sources). None until MIN_PAIRED pairs exist."""
    rows = conn.execute(
        "SELECT period, source, brier FROM source_scores WHERE series=? AND offset=?"
        " AND source IN ('model','market') ORDER BY period DESC LIMIT ?",
        (series, offset, TRAIL * 4)).fetchall()
    per: dict[str, dict[str, float]] = {}
    for r in rows:
        if r["brier"] is not None:
            per.setdefault(r["period"], {})[r["source"]] = float(r["brier"])
    pairs = [(v["model"], v["market"]) for v in per.values()
             if "model" in v and "market" in v][:TRAIL]
    if len(pairs) < MIN_PAIRED:
        return None
    mm = sum(p[0] for p in pairs) / len(pairs)
    mk = sum(p[1] for p in pairs) / len(pairs)
    if mk <= 1e-9:
        return None
    return mm / mk


def defensive(conn, series: str) -> float | None:
    """Ratio if the series should trade defensively, else None."""
    r = skill_ratio(conn, series)
    return r if (r is not None and r > RATIO_MAX) else None


def blocked(conn, series: str) -> float | None:
    """Ratio if model-path bets should be blocked outright (way behind the
    market — every 'edge' is model error; a calibrated-but-not-better model
    betting disagreements is a coin flip minus fees)."""
    r = skill_ratio(conn, series)
    return r if (r is not None and r > BLOCK_RATIO) else None
