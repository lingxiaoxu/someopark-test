"""ops/risk.py — exposure limits + circuit breaker (PLAN §12, §9.6-4).

check() is called by decide_all BEFORE recording an open; a Veto turns the decision into
a pass with the veto reason in the ledger note. Clusters: all contracts of the same
(family, period) count together (CPI MoM/YoY/COMBO of one print move together).

circuit_breaker(): trips a series (or '*' for global) by writing an alerts row with
source='circuit_breaker'. Consumers: exec.trading_allowed (blocks real orders 7d),
decide_all (blocks NEW paper opens 24h), exits.run (forces position exit). Trip paths:
health red lights (research/health.py §9.6-4) and the rolling-20 realized-PnL check.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY

LIMITS = {
    "per_event_usd": 5.0,       # one (series, period)
    "per_family_usd": 20.0,
    "per_cluster_usd": 8.0,     # same (family, period) across series — correlated prints
                                # (plan said $40; kept at $8 deliberately — more
                                # conservative until any series passes the gate)
    "per_release_day_usd": 30.0,  # total NEW exposure opened per calendar day (§12)
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
    today = datetime.now(timezone.utc).date().isoformat()
    opened_today = conn.execute(
        "SELECT COALESCE(SUM(size_usd),0) s FROM decisions WHERE kind='open'"
        " AND ts_utc>=?", (today,)).fetchone()["s"]
    if opened_today + size_usd > LIMITS["per_release_day_usd"]:
        return Veto(f"risk_release_day {opened_today + size_usd:.2f}"
                    f">{LIMITS['per_release_day_usd']}")
    return None


def circuit_breaker(conn, series: str, reason: str) -> None:
    """Trip the breaker for `series` ('*' = global). Idempotent per (series, reason,
    day): re-tripping the same day is a no-op so daily health runs don't spam."""
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    msg = f"{series}: {reason}"
    dup = conn.execute(
        "SELECT 1 FROM alerts WHERE source='circuit_breaker' AND message=? AND ts>=?",
        (msg, day)).fetchone()
    if dup:
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now.isoformat(), "error", "circuit_breaker", msg))
    conn.commit()


def breaker_tripped(conn, series: str, within_hours: float = 24.0) -> str | None:
    """Reason string if `series` (or the global '*') tripped within the window.
    Acked alerts are released — acking a circuit_breaker alert IS the manual
    operator release (人工复核才复活, 铁律 10)."""
    cut = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    r = conn.execute(
        "SELECT message FROM alerts WHERE source='circuit_breaker' AND ts>=? AND"
        " acked=0 AND (message LIKE ? OR message LIKE '*:%')"
        " ORDER BY ts DESC LIMIT 1",
        (cut, f"{series}:%")).fetchone()
    return r["message"] if r else None


def check_rolling20(conn) -> str | None:
    """PLAN §12: rolling-20-settlement drawdown breaker. If the last 20 settle_note
    rows sum to a realized loss beyond 2x per_event limit, trip globally."""
    rows = conn.execute(
        "SELECT size_usd FROM (SELECT series, period, size_usd, MIN(id) mid"
        " FROM decisions WHERE kind='settle_note' GROUP BY series, period)"
        " ORDER BY mid DESC LIMIT 20").fetchall()
    if len(rows) < 20:
        return None
    total = sum(r["size_usd"] or 0.0 for r in rows)
    thresh = -2.0 * LIMITS["per_event_usd"]
    if total < thresh:
        reason = f"rolling20_pnl {total:.2f} < {thresh:.2f}"
        circuit_breaker(conn, "*", reason)
        return reason
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
