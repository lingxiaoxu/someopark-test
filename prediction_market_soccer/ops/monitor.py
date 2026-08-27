"""Health monitoring + alerts (plan 05 §5, TRANSFORM_PLAN R6).

Reads the local store + output files and emits a health report with severity
levels (OK / WARN / ALERT) across the dimensions the plan calls out:

  * data freshness     — model run + fixture data age
  * API budget         — monthly request usage vs the 7000 cap
  * model calibration  — latest OOS Brier vs the uniform baseline
  * cross-venue health — max |Kalshi − Global| spread (data/efficiency check)
  * request error rate — share of non-2xx API calls
  * unmapped markets   — venue listings our alias tables could not resolve (R6)
  * calibration gates  — which competitions §3.5 currently lets trade

Pure read (no orders, no API calls). Designed to be run after `hourly_job` (or
standalone) and to drive paging. ``ops/health_export`` assembles + writes
`data/output/health.json` from this; ``python -m ...ops.monitor`` is the same run.

R6 — UNMAPPED MARKETS. The alias tables cover ~500 clubs across twelve
competitions and get stale mid-season (winter renames, relocations, a venue
spelling a promoted club a new way). The discovery layer resolves EXACT names
only, so an unresolvable listing is dropped — correct, because a fuzzy match on a
live trading path is worse than no match. But dropped-and-silent is how a whole
competition goes dark while every dashboard stays green: no quotes looks exactly
like no listings. So each drop is recorded in ``unmapped_market`` and surfaced
here. The table is written by whoever holds a live discovery handle (see
``record_unmapped_from``); this module only reads it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ingest import store

# Thresholds (plan 05 §5). WARN < ALERT.
MODEL_AGE_WARN_H, MODEL_AGE_ALERT_H = 2.0, 6.0
BUDGET_WARN, BUDGET_ALERT = 0.80, 0.95           # fraction of monthly budget
BRIER_BASELINE = 2.0 / 3.0                        # uniform 3-way Brier
XV_SPREAD_WARN, XV_SPREAD_ALERT = 0.03, 0.06      # |Kalshi − Global|
ERR_RATE_WARN, ERR_RATE_ALERT = 0.05, 0.15

# R6 thresholds. Only RECENT sightings count: once an alias entry lands, the old
# label stops appearing and must age out of the alert on its own, or the board
# stays red forever and gets ignored.
UNMAPPED_WINDOW_H = 48.0
UNMAPPED_WARN, UNMAPPED_ALERT = 1, 5      # distinct labels across all venues
UNMAPPED_PER_COMP_ALERT = 3               # one competition losing 3+ clubs = it is going dark

_UNMAPPED_DDL = """
CREATE TABLE IF NOT EXISTS unmapped_market (
  venue      TEXT NOT NULL,          -- kalshi | poly_us | poly_global
  comp       TEXT NOT NULL,          -- registry comp key ('' when the scan is venue-wide)
  label      TEXT NOT NULL,          -- the venue's own spelling we could not resolve
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  n_seen     INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (venue, comp, label)
)
"""


def record_unmapped(conn, venue: str, comp: str, labels) -> int:
    """Record venue listings that resolved to no club. Returns how many were recorded.

    Idempotent per (venue, comp, label): re-seeing the same label bumps ``last_seen``
    and the counter rather than piling up rows, so the alert counts DISTINCT clubs we
    are blind to — which is the number that matters — not how often the scan ran.
    """
    labels = sorted({(s or "").strip() for s in labels if (s or "").strip()})
    if not labels:
        return 0
    conn.execute(_UNMAPPED_DDL)
    now = store.utcnow()
    for s in labels:
        conn.execute(
            "INSERT INTO unmapped_market (venue, comp, label, first_seen, last_seen, n_seen) "
            "VALUES (?,?,?,?,?,1) ON CONFLICT(venue, comp, label) DO UPDATE SET "
            "last_seen=excluded.last_seen, n_seen=unmapped_market.n_seen+1",
            (venue, comp or "", s, now, now))
    conn.commit()
    return len(labels)


def record_unmapped_from(conn, venue: str, comp: str, discovery) -> int:
    """Flush a discovery object's in-process ``.unmapped`` list into the table.

    Both ``venues/kalshi/discovery`` and ``venues/polymarket_us/discovery`` already
    collect their drops on ``self.unmapped``; that list dies with the process, so
    every caller that finishes a discovery pass should end it with this one line.
    """
    return record_unmapped(conn, venue, comp, getattr(discovery, "unmapped", None) or [])


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


def _output_age_hours(*names: str) -> tuple[float | None, str]:
    """Age of the freshest of ``names`` in data/output, and which one it was."""
    best: tuple[float, str] | None = None
    for n in names:
        p = CONFIG.paths.output / n
        try:
            age = (time.time() - p.stat().st_mtime) / 3600.0
        except OSError:
            continue
        if best is None or age < best[0]:
            best = (age, n)
    return (best[0], best[1]) if best else (None, "")


def _level(value: float, warn: float, alert: float, *, higher_is_worse: bool = True) -> str:
    if higher_is_worse:
        return "ALERT" if value >= alert else "WARN" if value >= warn else "OK"
    return "ALERT" if value <= alert else "WARN" if value <= warn else "OK"


def health_report(conn=None) -> HealthReport:
    conn = conn or store.init_db()
    rep = HealthReport(ts=datetime.now(timezone.utc).isoformat())

    # 1) model freshness, from the model_run LEDGER. Kept ledger-only on purpose: the
    # ledger is the record that a run happened with known params, and in the club edition
    # it is empty in production — run_model.refresh_model writes the payload but never
    # calls store.persist_model_run. That is a real bookkeeping gap in a module this one
    # does not own, so it stays visible here rather than being papered over.
    row = conn.execute("SELECT run_ts FROM model_run ORDER BY run_ts DESC LIMIT 1").fetchone()
    age = _age_hours(row["run_ts"]) if row else None
    if age is None:
        rep.checks.append(Check("model_freshness", "ALERT", None,
                                "no model_run recorded — refresh_model writes soccer_model.json "
                                "but never persist_model_run(); see model_export_freshness for "
                                "the actual age"))
    else:
        rep.checks.append(Check("model_freshness", _level(age, MODEL_AGE_WARN_H, MODEL_AGE_ALERT_H),
                                round(age, 2), f"last model run {age:.1f}h ago"))

    # 1b) the age that is actually observable today: when the model payload was last
    # written. This is the line an operator acts on while the ledger gap above is open.
    exp_age, exp_src = _output_age_hours("soccer_model.json", "latest.json")
    if exp_age is not None:
        rep.checks.append(Check("model_export_freshness",
                                _level(exp_age, MODEL_AGE_WARN_H, MODEL_AGE_ALERT_H),
                                round(exp_age, 2), f"{exp_src} written {exp_age:.1f}h ago"))

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

    # 6) unmapped venue markets (R6)
    rep.checks.append(_unmapped_check(conn))

    # 7) per-competition calibration gates (§3.5)
    rep.checks.append(_gate_check())
    return rep


def _unmapped_check(conn) -> Check:
    """R6: venue listings the alias tables could not resolve, in the last window.

    A missing table means no scan has EVER run — reported as WARN, not OK, because
    "we found nothing" and "we never looked" must not render the same colour.
    """
    try:
        rows = conn.execute(
            "SELECT venue, comp, label, last_seen, n_seen FROM unmapped_market "
            "WHERE last_seen >= ? ORDER BY last_seen DESC",
            ((datetime.now(timezone.utc) - timedelta(hours=UNMAPPED_WINDOW_H)).isoformat(),)
        ).fetchall()
    except Exception:
        return Check("unmapped_markets", "WARN", None,
                     "no venue alias scan on record — run `ops.health_export --scan-venues` "
                     "(until then an unmapped listing is invisible, not absent)")
    if not rows:
        return Check("unmapped_markets", "OK", 0.0,
                     f"every venue listing resolved to a club in the last {UNMAPPED_WINDOW_H:.0f}h")
    per_comp: dict[tuple, int] = {}
    for r in rows:
        per_comp[(r["venue"], r["comp"])] = per_comp.get((r["venue"], r["comp"]), 0) + 1
    n = len(rows)
    worst_key, worst_n = max(per_comp.items(), key=lambda kv: kv[1])
    lvl = ("ALERT" if (n >= UNMAPPED_ALERT or worst_n >= UNMAPPED_PER_COMP_ALERT)
           else "WARN" if n >= UNMAPPED_WARN else "OK")
    sample = ", ".join(f'{r["venue"]}/{r["comp"] or "*"} "{r["label"]}"' for r in rows[:5])
    more = f" (+{n - 5} more)" if n > 5 else ""
    return Check("unmapped_markets", lvl, float(n),
                 f"{n} venue listing(s) unmapped in {UNMAPPED_WINDOW_H:.0f}h — worst "
                 f"{worst_key[0]}/{worst_key[1] or '*'} with {worst_n}: {sample}{more}")


def _gate_check() -> Check:
    """§3.5: how many competitions may currently trade, and whether any REGRESSED.

    Cold start is the expected state for a young competition, so it is not an alert.
    A competition past PER_LEAGUE_MIN_N whose calibrated Brier still loses to uniform
    is different in kind — that is the model failing on a real sample, and it is the
    one gate state worth paging on.
    """
    from prediction_market_soccer.config.leagues import active
    from prediction_market_soccer.model.probability_calibration import (
        PER_LEAGUE_MIN_N, gate_open_for, load_calibration)

    cal = load_calibration()
    if not cal:
        return Check("calibration_gates", "WARN", None,
                     "no calibration.json — every competition's gate is shut")
    per = cal.get("per_league") or {}
    keys = [c.key for c in active()]
    open_ = [k for k in keys if gate_open_for(cal, k)]
    cold = [k for k in keys if (per.get(k) or {}).get("cold_start")]
    regressed = [k for k in keys
                 if (per.get(k) or {}) and not (per.get(k) or {}).get("cold_start")
                 and not (per.get(k) or {}).get("trade_grade")]
    lvl = "ALERT" if regressed else ("WARN" if not open_ else "OK")
    detail = (f"{len(open_)}/{len(keys)} gates open; {len(cold)} cold-start "
              f"(<{PER_LEAGUE_MIN_N} settled)")
    if regressed:
        detail += f"; REGRESSED (mature but worse than uniform): {', '.join(sorted(regressed))}"
    return Check("calibration_gates", lvl, float(len(open_)), detail)


def main() -> None:
    # health.json has exactly ONE writer (ops/health_export) so the file can never
    # disagree with itself depending on which entry point last ran; `-m ops.monitor`
    # stays a working alias for the same run. Imported here, not at module scope:
    # health_export imports this module.
    from prediction_market_soccer.ops import health_export
    health_export.main()


if __name__ == "__main__":
    main()
