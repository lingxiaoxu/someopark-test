"""ops/risk.py — exposure limits + circuit breaker (PLAN §12).

check() is called by decide_all BEFORE recording an open; a Veto turns the decision into
a pass with the veto reason in the ledger note. Clusters: all contracts of the same
(family, period) count together (CPI MoM/YoY/COMBO of one print move together).
"""
from __future__ import annotations

from dataclasses import dataclass

from prediction_market_macro.config.registry import REGISTRY

LIMITS = {
    "per_event_usd": 5.0,       # one (series, period)
    "per_family_usd": 20.0,
    "per_cluster_usd": 8.0,     # same (family, period) across series — correlated prints
    "gross_usd": 100.0,
}


@dataclass(frozen=True)
class Veto:
    reason: str


def _open_exposure(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT d.series, d.period, d.size_usd FROM decisions d WHERE d.kind='open'"
        " AND NOT EXISTS (SELECT 1 FROM decisions e WHERE e.series=d.series AND"
        " e.period=d.period AND e.kind IN ('exit','cancel','settle_note') AND e.id>d.id)"
    ).fetchall()
    return [dict(r) for r in rows]


def check(conn, series: str, period: str, size_usd: float) -> Veto | None:
    fam = REGISTRY[series].family if series in REGISTRY else "other"
    month = period[:7]
    ev = fam_ex = cl = gross = 0.0
    for p in _open_exposure(conn):
        s = p["size_usd"] or 0.0
        gross += s
        p_fam = REGISTRY[p["series"]].family if p["series"] in REGISTRY else "other"
        if p["series"] == series and p["period"] == period:
            ev += s
        if p_fam == fam:
            fam_ex += s
            if p["period"][:7] == month:
                cl += s
    if ev + size_usd > LIMITS["per_event_usd"]:
        return Veto(f"risk_per_event {ev + size_usd:.2f}>{LIMITS['per_event_usd']}")
    if fam_ex + size_usd > LIMITS["per_family_usd"]:
        return Veto(f"risk_per_family {fam_ex + size_usd:.2f}>{LIMITS['per_family_usd']}")
    if cl + size_usd > LIMITS["per_cluster_usd"]:
        return Veto(f"risk_cluster {cl + size_usd:.2f}>{LIMITS['per_cluster_usd']}")
    if gross + size_usd > LIMITS["gross_usd"]:
        return Veto(f"risk_gross {gross + size_usd:.2f}>{LIMITS['gross_usd']}")
    return None


def scenario_var(conn) -> dict:
    """Baseline: independent stake-sum worst case. Upgrades itself to the DFM joint
    scenario engine once model/dfm_bridge passes its §7-bis adoption gate (weekly
    gate_check) and the scenario cache is fresh — reversible: gate FAIL or stale cache
    falls straight back here."""
    try:
        from prediction_market_macro.model.dfm_bridge import scenario_var_dfm
        joint = scenario_var_dfm(conn)
    except Exception:                             # noqa: BLE001 — bridge is optional
        joint = None
    worst = 0.0
    for p in _open_exposure(conn):
        worst += p["size_usd"] or 0.0            # binary max loss = stake (paper $1 cap)
    base = {"max_loss_all_events_usd": round(worst, 2), "mode": "independent_stake_sum"}
    if joint is not None:
        return {**joint, "max_loss_all_events_usd": base["max_loss_all_events_usd"]}
    return base
