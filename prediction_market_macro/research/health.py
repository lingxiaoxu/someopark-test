"""research/health.py — daily model health patrol (PLAN §9.6). Runs inside refresh.

Checks:
  1. data freshness per source (FRED age vs cadence, quotes age)
  2. pred freshness + ladder mass sanity + replay determinism per series
  3. break detectors (§9.6-2, any trigger ⇒ series red + circuit breaker):
     a) rolling Brier behind the market 2 consecutive replay windows
     b) CRPS rolling mean beyond own 12-run mean + 2σ (suddenly dumber)
     c) entropy convergence: <48h to release yet newer pred entropy RISES
     d) feature out-of-bounds: any first-print diff beyond its 5y z=4 envelope
     e) Chronos bridge: NaN / crossed quantiles in the latest shadow pred
  4. ledger self-check: 3 date-seeded random open decisions replayed from their own
     inputs_json — fair must match bit-for-bit (silent-code-change canary)
  5. red lights auto-trip ops.risk.circuit_breaker (→ decide_all blocks new opens,
     exits force-closes, exec stays locked) — 铁律 10.
Output: red/yellow/green per series → macro_health.json + alerts on red.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH


def _replay_name(series: str) -> str:
    return "claims_replay" if series == "KXJOBLESSCLAIMS" else f"{series[2:].lower()}_replay"


def _detect_brier_2win(conn, series: str) -> str | None:
    """(a) model behind market in the 2 most recent replay windows."""
    rows = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name=? AND series=?"
        " ORDER BY created_ts DESC LIMIT 2", (_replay_name(series), series)).fetchall()
    if len(rows) < 2:
        return None
    behind = 0
    for r in rows:
        m = json.loads(r["metrics_json"])
        bm, bk = m.get("brier_model-1h"), m.get("brier_market-1h")
        if bm is not None and bk is not None and bm > bk:
            behind += 1
    return "brier_behind_market_2win" if behind == 2 else None


def _detect_crps_spike(conn, series: str) -> str | None:
    """(b) latest replay CRPS beyond own 12-run mean + 2σ."""
    rows = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name=? AND series=?"
        " ORDER BY created_ts DESC LIMIT 13", (_replay_name(series), series)).fetchall()
    vals = []
    for r in rows:
        c = json.loads(r["metrics_json"]).get("crps-1h")
        if c is not None:
            vals.append(float(c))
    if len(vals) < 6:
        return None
    latest, hist = vals[0], vals[1:]
    mu = sum(hist) / len(hist)
    sd = math.sqrt(sum((x - mu) ** 2 for x in hist) / max(len(hist) - 1, 1))
    if sd > 0 and latest > mu + 2 * sd:
        return f"crps_spike:{latest:.1f}>mu{mu:.1f}+2sd{sd:.1f}"
    return None


def _entropy(dist: dict) -> float | None:
    probs = None
    if isinstance(dist.get("probs"), dict):
        probs = list(dist["probs"].values())
    elif isinstance(dist.get("pmf"), dict):
        probs = list(dist["pmf"].values())
    if not probs:
        return None
    return -sum(p * math.log(max(p, 1e-12)) for p in probs if p > 0)


def _detect_entropy_rise(conn, spec, now: datetime) -> str | None:
    """(c) inside 48h of a release the pred distribution should be converging
    (entropy non-increasing between the two latest preds)."""
    rel = conn.execute(
        "SELECT period, scheduled_ts FROM releases WHERE cal=? AND scheduled_ts>?"
        " ORDER BY scheduled_ts LIMIT 1", (spec.calendar, now.isoformat())).fetchone()
    if rel is None:
        return None
    ts = datetime.fromisoformat(rel["scheduled_ts"])
    if (ts - now).total_seconds() > 48 * 3600:
        return None
    rows = conn.execute(
        "SELECT ladder_json, dist_json FROM preds WHERE series=? AND period=?"
        " AND model_version LIKE ? ORDER BY asof DESC LIMIT 2",
        (spec.ticker, rel["period"], spec.model + "/%")).fetchall()
    if len(rows) < 2:
        return None
    ents = []
    for r in rows:
        src = json.loads(r["ladder_json"]) if r["ladder_json"] else None
        e = _entropy({"pmf": src} if src else json.loads(r["dist_json"]))
        if e is None:
            return None
        ents.append(e)
    newer, older = ents[0], ents[1]
    if newer > older + 0.05:
        return f"entropy_rise:{older:.3f}->{newer:.3f}<48h"
    return None


def _detect_feature_oob(conn, now: datetime) -> list[str]:
    """(d) latest first-print diff of every ingested FRED sid vs its 5y z=4 envelope."""
    flags = []
    cut5y = (now - timedelta(days=5 * 365)).date().isoformat()
    sids = [r["sid"] for r in conn.execute(
        "SELECT sid, COUNT(DISTINCT event_time) n FROM fred_obs WHERE event_time>=?"
        " GROUP BY sid HAVING n>=60", (cut5y,)).fetchall()]
    for sid in sids:
        rows = conn.execute(
            "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs"
            " WHERE sid=? AND event_time>=? GROUP BY event_time ORDER BY event_time",
            (sid, cut5y)).fetchall()
        vals = [float(r["value"]) for r in rows]
        if len(vals) < 60:
            continue
        diffs = [b - a for a, b in zip(vals[:-1], vals[1:])]
        latest, hist = diffs[-1], diffs[:-1]
        mu = sum(hist) / len(hist)
        sd = math.sqrt(sum((x - mu) ** 2 for x in hist) / max(len(hist) - 1, 1))
        if sd > 0 and abs(latest - mu) / sd > 4.0:
            flags.append(f"feature_oob:{sid}:z={(latest - mu) / sd:.1f}")
    return flags


def _detect_chronos(conn, series: str) -> str | None:
    """(e) latest chronos2 shadow pred: quantiles must be finite and monotone."""
    r = conn.execute(
        "SELECT dist_json FROM preds WHERE series=? AND model_version LIKE 'chronos2%'"
        " ORDER BY asof DESC LIMIT 1", (series,)).fetchone()
    if r is None:
        return None
    d = json.loads(r["dist_json"])
    qs = d.get("quantiles") or d.get("values")
    if not isinstance(qs, list) or not qs:
        return None
    try:
        xs = [float(x) for x in qs]
    except (TypeError, ValueError):
        return "chronos_bad_values"
    if any(math.isnan(x) or math.isinf(x) for x in xs):
        return "chronos_nan"
    if any(b < a for a, b in zip(xs[:-1], xs[1:])):
        return "chronos_quantile_crossing"
    return None


def _settle_label_check(conn, now: datetime) -> list[str]:
    """铁律 2 cross-check: every settled ladder leg's YES/NO must agree with the
    first-print label vs its strike. A mismatch means our settlement understanding
    (or the label pipe) is wrong — series halts via circuit breaker."""
    from prediction_market_macro.ops.pnl import _realized_print
    bad = []
    rows = conn.execute(
        "SELECT s.series, s.period, s.ticker, s.result, c.floor_strike, c.strike_type"
        " FROM settlements s JOIN contracts c ON c.ticker=s.ticker"
        " WHERE s.result IN ('yes','no') AND c.floor_strike IS NOT NULL"
        " ORDER BY s.settled_ts DESC LIMIT 120").fetchall()
    from prediction_market_macro.util.periods import kalshi_period_to_key
    cache: dict[tuple[str, str], float | None] = {}
    for r in rows:
        key = kalshi_period_to_key(r["period"]) if r["period"] else None
        if not key:
            continue
        ck = (r["series"], key)
        if ck not in cache:
            cache[ck] = _realized_print(conn, r["series"], key)
        y = cache[ck]
        if y is None:
            continue
        strict = (r["strike_type"] == "greater")
        expected = "yes" if (y > r["floor_strike"] if strict
                             else y >= r["floor_strike"]) else "no"
        if expected != r["result"]:
            bad.append(f"settle_label_mismatch:{r['ticker']}:label={y}"
                       f" strike={r['floor_strike']} expect={expected} got={r['result']}")
    return bad[:10]


def _ledger_selfcheck(conn, now: datetime, k: int = 3) -> list[str]:
    """(4) k date-seeded random open decisions: recompute fair from the row's own
    inputs_json dist + structure legs — mismatch means code silently changed."""
    from prediction_market_macro.model.common import leg_fair
    rows = conn.execute(
        "SELECT id, series, structure_json, inputs_json, fair FROM decisions"
        " WHERE kind='open' AND fair IS NOT NULL").fetchall()
    if not rows:
        return []
    rng = random.Random(now.date().isoformat())
    sample = rng.sample(list(rows), min(k, len(rows)))
    bad = []
    for d in sample:
        try:
            st = json.loads(d["structure_json"])
            if st.get("kind") != "single" or not st.get("legs"):
                continue                                   # only singles replay offline
            inp = json.loads(d["inputs_json"])
            leg = st["legs"][0]
            probs = inp.get("probs") if isinstance(inp.get("probs"), dict) else None
            pmf = ({float(kk): v for kk, v in inp["pmf"].items()}
                   if isinstance(inp.get("pmf"), dict) else None)
            if probs is not None:
                fair = float(probs.get(leg["ticker"].rsplit("-", 1)[-1], 0.0))
            elif pmf is not None:
                c = conn.execute(
                    "SELECT floor_strike, cap_strike, strike_type FROM contracts"
                    " WHERE ticker=?", (leg["ticker"],)).fetchone()
                if c is None or c["floor_strike"] is None:
                    continue
                fair = leg_fair(pmf, c["strike_type"] or "greater",
                                c["floor_strike"], c["cap_strike"])
            else:
                continue                                   # dist form not replayable
            if leg["side"] == "no":
                fair = 1 - fair
            if abs(fair - float(d["fair"])) > 1e-6:
                bad.append(f"ledger_replay_mismatch:id={d['id']}"
                           f":{fair:.6f}!={d['fair']:.6f}")
        except Exception as e:                            # noqa: BLE001
            bad.append(f"ledger_replay_error:id={d['id']}:{e}")
    return bad


def daily_health(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    report: dict = {"ts": now.isoformat(), "sources": {}, "series": {}, "flags": []}

    # 1. source freshness
    for sid, max_age_d in (("ICSA", 9), ("CPIAUCSL", 40), ("DFEDTARU", 5), ("UNRATE", 40)):
        r = conn.execute("SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid=?",
                         (sid,)).fetchone()
        age = None
        if r["m"]:
            age = (now - datetime.fromisoformat(r["m"])).days
        report["sources"][sid] = {"latest_kt": r["m"], "age_days": age}
        if age is None or age > max_age_d:
            report["flags"].append(f"stale_source:{sid}:{age}d")
    q = conn.execute("SELECT MAX(ts) m FROM quotes").fetchone()
    q_age_h = (now - datetime.fromisoformat(q["m"])).total_seconds() / 3600 if q["m"] else None
    report["sources"]["kalshi_quotes"] = {"age_hours": round(q_age_h, 1) if q_age_h else None}
    if q_age_h is None or q_age_h > 26:
        report["flags"].append(f"stale_quotes:{q_age_h}")

    # 2+3. per-series: pred freshness, ladder mass, replay determinism
    import importlib
    for spec in REGISTRY.values():
        s_rep = {"status": "green", "notes": []}
        # replay the PRODUCTION model's latest pred only — shadow members (chronos2/*)
        # have their own model_version and would trivially mismatch the dispatch fn
        pr = conn.execute(
            "SELECT * FROM preds WHERE series=? AND model_version LIKE ?"
            " ORDER BY asof DESC LIMIT 1",
            (spec.ticker, spec.model + "/%")).fetchone()
        if pr is None:
            s_rep = {"status": "yellow", "notes": ["no_preds_yet"]}
        else:
            age_h = (now - datetime.fromisoformat(pr["asof"])).total_seconds() / 3600
            if age_h > 26:
                s_rep["status"] = "red"
                s_rep["notes"].append(f"pred_stale:{age_h:.0f}h")
            if pr["ladder_json"]:
                mass = sum(json.loads(pr["ladder_json"]).values())
                if abs(mass - 1.0) > 0.01:
                    s_rep["status"] = "red"
                    s_rep["notes"].append(f"ladder_mass:{mass:.3f}")
            # replay determinism (dependency drift canary)
            disp = SERIES_DISPATCH.get(spec.ticker)
            if disp:
                try:
                    mod = importlib.import_module(disp[0])
                    fn = getattr(mod, disp[1])
                    re_pred = fn(conn, datetime.fromisoformat(pr["asof"]), pr["period"],
                                 series=spec.ticker)
                    if json.dumps(re_pred.dist.to_json()) != pr["dist_json"]:
                        s_rep["status"] = "red"
                        s_rep["notes"].append("replay_mismatch")
                except Exception as e:                            # noqa: BLE001
                    s_rep["status"] = "red"
                    s_rep["notes"].append(f"replay_error:{e}")
        # §9.6-2 break detectors a/b/c/e (d is source-level, runs once below)
        for det in (_detect_brier_2win(conn, spec.ticker),
                    _detect_crps_spike(conn, spec.ticker),
                    _detect_entropy_rise(conn, spec, now),
                    _detect_chronos(conn, spec.ticker)):
            if det:
                s_rep["status"] = "red"
                s_rep["notes"].append(det)
        report["series"][spec.ticker] = s_rep
        if s_rep["status"] == "red":
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "error", "health",
                          f"RED {spec.ticker}: {s_rep['notes']}"))
            # 铁律 10: red light auto-demotes — trip the breaker (blocks new opens,
            # forces exits, keeps exec locked) until a green day releases it
            from prediction_market_macro.ops import risk
            risk.circuit_breaker(conn, spec.ticker,
                                 "health_red:" + ",".join(s_rep["notes"])[:180])

    # 4. rolling OOS from the latest replay experiment
    ex = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name='claims_replay'"
        " ORDER BY created_ts DESC LIMIT 1").fetchone()
    if ex:
        m = json.loads(ex["metrics_json"])
        report["oos_claims"] = m
        if (m.get("brier_model-1h") or 0) > (m.get("brier_market-1h") or 1):
            report["series"]["KXJOBLESSCLAIMS"]["notes"].append(
                "oos_brier_behind_market — stays paper (gate)")

    # detector (d): source-level feature out-of-bounds envelope
    oob = _detect_feature_oob(conn, now)
    report["flags"].extend(oob)
    for f in oob:
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "health", f))

    # (4) ledger self-check: 3 date-seeded random decisions
    bad = _ledger_selfcheck(conn, now)
    report["ledger_selfcheck"] = {"n": 3, "mismatches": bad}
    for b in bad:
        report["flags"].append(b)
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "health", b))
        from prediction_market_macro.ops import risk
        risk.circuit_breaker(conn, "*", b[:180])

    # 铁律 2: settlement ↔ first-print label reconciliation
    slb = _settle_label_check(conn, now)
    for b in slb:
        report["flags"].append(b)
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "health", b))
        from prediction_market_macro.ops import risk
        risk.circuit_breaker(conn, "*", b[:180])

    # rolling-20 realized-PnL breaker (PLAN §12)
    from prediction_market_macro.ops import risk as _risk
    r20 = _risk.check_rolling20(conn)
    if r20:
        report["flags"].append(r20)

    conn.commit()
    path = settings.output_dir / "macro_health.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    reds = [k for k, v in report["series"].items() if v["status"] == "red"]
    return f"{len(report['series'])} series, red={reds or 'none'}, flags={len(report['flags'])}"
