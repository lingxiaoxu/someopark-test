"""Health monitoring + alerts (plan 05 §5).

Reads the local store + output files and emits a health report with severity
levels (OK / WARN / ALERT) across the dimensions the plan calls out:

  * data freshness     — model run + fixture data age
  * API budget         — monthly request usage vs the 7000 cap
  * model calibration  — latest OOS Brier vs the uniform baseline
  * cross-venue health — max |Kalshi − Global| spread (data/efficiency check)
  * request error rate — share of non-2xx API calls

Pure read (no orders, no API calls). Designed to be run after `hourly_job` (or
standalone) and to drive paging. Writes `data/output/health.json`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from prediction_market.config import CONFIG
from prediction_market.ingest import store

# Thresholds (plan 05 §5). WARN < ALERT.
MODEL_AGE_WARN_H, MODEL_AGE_ALERT_H = 2.0, 6.0
BUDGET_WARN, BUDGET_ALERT = 0.80, 0.95           # fraction of monthly budget
BRIER_BASELINE = 2.0 / 3.0                        # uniform 3-way Brier
XV_SPREAD_WARN, XV_SPREAD_ALERT = 0.03, 0.06      # |Kalshi − Global|
ERR_RATE_WARN, ERR_RATE_ALERT = 0.05, 0.15


@dataclass
class Check:
    name: str
    level: str            # OK | WARN | ALERT
    value: float | None
    detail: str


@dataclass
class HealthReport:
    ts: str
    checks: list[Check] = field(default_factory=list)

    @property
    def worst(self) -> str:
        order = {"OK": 0, "WARN": 1, "ALERT": 2}
        return max((c.level for c in self.checks), key=lambda l: order[l], default="OK")


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return None


def _level(value: float, warn: float, alert: float, *, higher_is_worse: bool = True) -> str:
    if higher_is_worse:
        return "ALERT" if value >= alert else "WARN" if value >= warn else "OK"
    return "ALERT" if value <= alert else "WARN" if value <= warn else "OK"


def health_report(conn=None) -> HealthReport:
    conn = conn or store.init_db()
    rep = HealthReport(ts=datetime.now(timezone.utc).isoformat())

    # 1) model freshness
    row = conn.execute("SELECT run_ts FROM model_run ORDER BY run_ts DESC LIMIT 1").fetchone()
    age = _age_hours(row["run_ts"]) if row else None
    if age is None:
        rep.checks.append(Check("model_freshness", "ALERT", None, "no model_run recorded"))
    else:
        rep.checks.append(Check("model_freshness", _level(age, MODEL_AGE_WARN_H, MODEL_AGE_ALERT_H),
                                round(age, 2), f"last model run {age:.1f}h ago"))

    # 2) API budget
    used = store.monthly_request_count(conn)
    frac = used / max(1, CONFIG.soccer.monthly_budget)
    rep.checks.append(Check("api_budget", _level(frac, BUDGET_WARN, BUDGET_ALERT), round(frac, 3),
                            f"{used}/{CONFIG.soccer.monthly_budget} requests this month"))

    # 3) calibration (latest OOS report)
    oos_path = CONFIG.paths.output / "oos_report.json"
    if oos_path.exists():
        oos = json.loads(oos_path.read_text(encoding="utf-8"))
        brier = oos.get("brier")
        if brier is not None:
            lvl = "OK" if brier <= BRIER_BASELINE else "WARN" if brier <= BRIER_BASELINE * 1.15 else "ALERT"
            rep.checks.append(Check("calibration_brier", lvl, round(brier, 4),
                                    f"OOS Brier {brier:.3f} vs uniform {BRIER_BASELINE:.3f} (n={oos.get('n_matches')})"))

    # 4) cross-venue health (latest xv_spread snapshot)
    xv = conn.execute(
        "SELECT MAX(xv_spread) mx, AVG(xv_spread) av FROM xv_spread "
        "WHERE ts = (SELECT MAX(ts) FROM xv_spread) AND xv_spread IS NOT NULL").fetchone()
    if xv and xv["mx"] is not None:
        rep.checks.append(Check("cross_venue_spread", _level(xv["mx"], XV_SPREAD_WARN, XV_SPREAD_ALERT),
                                round(xv["mx"], 4), f"max |Kalshi−Global| {xv['mx']:.3f} (avg {xv['av']:.3f})"))

    # 5) API error rate (this month)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    tot = conn.execute("SELECT COUNT(*) n FROM api_call WHERE substr(ts,1,7)=?", (month,)).fetchone()["n"]
    bad = conn.execute("SELECT COUNT(*) n FROM api_call WHERE substr(ts,1,7)=? AND http_status>=400",
                       (month,)).fetchone()["n"]
    if tot:
        er = bad / tot
        rep.checks.append(Check("api_error_rate", _level(er, ERR_RATE_WARN, ERR_RATE_ALERT), round(er, 3),
                                f"{bad}/{tot} API calls non-2xx"))
    return rep


def main() -> None:
    CONFIG.paths.ensure()
    rep = health_report()
    out = CONFIG.paths.output / "health.json"
    out.write_text(json.dumps({"ts": rep.ts, "worst": rep.worst,
                               "checks": [c.__dict__ for c in rep.checks]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    icon = {"OK": "✅", "WARN": "⚠️", "ALERT": "🚨"}
    print(f"HEALTH: {rep.worst}")
    for c in rep.checks:
        print(f"  {icon[c.level]} {c.name:<20} {c.detail}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
