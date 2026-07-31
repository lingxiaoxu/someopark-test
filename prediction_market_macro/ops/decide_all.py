"""ops/decide_all.py — §8.0 step 3: daily scan/decide (paper) on every fresh pred.

Pipeline per (series, period): latest pred → contracts+latest quotes → market-implied
ladder devig (violations → alerts: free-money monotonicity arbs) → enumerate structures
→ decide() gates → ledger. Freeze windows honored via the releases table.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.jobs.scheduler import set_coverage
from prediction_market_macro.ops import ledger
from prediction_market_macro.strategy import devig
from prediction_market_macro.strategy.decision import decide
from prediction_market_macro.strategy.edge import enumerate_structs
from prediction_market_macro.util.periods import kalshi_period_to_key


def _structs_categorical(legs: list[dict], probs: dict[str, float]):
    """Single-leg YES/NO structures for mutually-exclusive category markets
    (leg category = ticker suffix after the last '-', e.g. ...-H0 → 'H0')."""
    from prediction_market_macro.strategy.edge import Leg, Struct
    out = []
    for l in legs:
        cat = l["ticker"].rsplit("-", 1)[-1]
        if cat not in probs:
            continue
        fair = float(probs[cat])
        if l.get("yes_ask") is not None and 0 < l["yes_ask"] < 1:
            out.append(Struct("single", (Leg(l["ticker"], "yes", l["yes_ask"],
                                             l["ask_depth"]),),
                              fair, l["yes_ask"], l["yes_ask"],
                              f"YES {l['ticker']} @{l['yes_ask']:.2f}"))
        if l.get("yes_bid") is not None and 0 < l["yes_bid"] < 1:
            np_ = round(1 - l["yes_bid"], 4)
            out.append(Struct("single", (Leg(l["ticker"], "no", np_, l["bid_depth"]),),
                              1 - fair, np_, np_, f"NO {l['ticker']} @{np_:.2f}"))
    return out


def _legs_meta(conn, series: str, kalshi_tok: str) -> list[dict]:
    rows = conn.execute(
        "SELECT c.ticker, c.floor_strike strike, c.cap_strike, c.strike_type, c.close_time,"
        " q.yes_bid, q.yes_ask, q.bid_depth, q.ask_depth "
        "FROM contracts c LEFT JOIN quotes q ON q.ticker=c.ticker AND q.ts="
        " (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker) "
        "WHERE c.series=? AND c.period=? AND c.status='active'",
        (series, kalshi_tok)).fetchall()
    return [dict(r) for r in rows]


def _bankroll(conn, settings) -> float:
    """Live demo-account balance (cached in db by the refresh bankroll step);
    static seed only if never fetched."""
    from prediction_market_macro.venues.kalshi.account import current_bankroll
    return current_bankroll(conn)


def run(conn, settings) -> int:
    now = datetime.now(timezone.utc)
    n = 0
    for spec in REGISTRY.values():
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)).fetchall():
            tok = r["period"]
            key = kalshi_period_to_key(tok)
            if not key:
                continue
            # production-model guard: only the registry-bound model's preds may drive
            # decisions — shadow members (chronos2/*, dfm/*) are analysis-only (§7-bis)
            pr = conn.execute(
                "SELECT * FROM preds WHERE series=? AND period=? AND model_version LIKE ?"
                " ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            if pr is None:
                continue
            legs = _legs_meta(conn, spec.ticker, tok)
            if not legs:
                continue
            # circuit breaker (铁律 10): tripped series open NOTHING new
            from prediction_market_macro.ops import risk
            trip = risk.breaker_tripped(conn, spec.ticker)
            if trip:
                from prediction_market_macro.strategy.decision import Decision, GATES
                ledger.record(conn, series=spec.ticker, period=key,
                              decision=Decision("pass", None, 0.0, 0,
                                                (f"circuit_breaker [{trip[:100]}]",),
                                                dict(GATES)),
                              pred_inputs={}, model_version=pr["model_version"])
                set_coverage(conn, spec.ticker, key, "passed")
                n += 1
                continue
            # staleness hard gate (§8.2-5): stale inputs ⇒ forced PASS, never a
            # decision on old data. Pred >26h or freshest quote >6h old ⇒ stale.
            pred_age_h = (now - datetime.fromisoformat(pr["asof"])
                          ).total_seconds() / 3600.0
            qt = conn.execute(
                "SELECT MAX(q.ts) m FROM quotes q JOIN contracts c ON c.ticker=q.ticker"
                " WHERE c.series=? AND c.period=?", (spec.ticker, tok)).fetchone()
            quote_age_h = ((now - datetime.fromisoformat(qt["m"])).total_seconds()
                           / 3600.0) if qt and qt["m"] else None
            if pred_age_h > 26 or quote_age_h is None or quote_age_h > 6:
                from prediction_market_macro.strategy.decision import Decision, GATES
                reason = (f"stale_inputs pred={pred_age_h:.0f}h"
                          f" quotes={quote_age_h if quote_age_h is None else round(quote_age_h, 1)}h")
                ledger.record(conn, series=spec.ticker, period=key,
                              decision=Decision("pass", None, 0.0, 0, (reason,),
                                                dict(GATES)),
                              pred_inputs={}, model_version=pr["model_version"])
                set_coverage(conn, spec.ticker, key, "passed")
                n += 1
                continue
            if spec.structure == "categorical":
                dist = json.loads(pr["dist_json"])
                probs = dist.get("probs") or {}
                structs, impl = _structs_categorical(legs, probs), {"pmf": None,
                                                                    "violations": []}
            else:
                is_bucket = any(l.get("strike_type") == "between" for l in legs)
                impl = (devig.bucket_implied(legs) if is_bucket
                        else devig.ladder_implied(legs))
                for v in impl["violations"]:
                    msg = (f"PARTITION-ARB {spec.ticker}/{tok}: {v['kind']}"
                           f" gross {v['gross']}" if "kind" in v else
                           f"MONOTONE-ARB {spec.ticker}/{tok}: buy {v['buy']['ticker']}"
                           f" sell {v['sell']['ticker']} gross {v['gross']}")
                    dup = conn.execute(
                        "SELECT 1 FROM alerts WHERE source='consistency' AND"
                        " message=? AND ts>=?",
                        (msg, now.date().isoformat())).fetchone()
                    if not dup:               # standing arbs re-detected per run
                        conn.execute(
                            "INSERT INTO alerts(ts, level, source, message)"
                            " VALUES(?,?,?,?)",
                            (now.isoformat(), "info", "consistency", msg))
                # §24-A: detected free money gets TRADED (paper), not just alerted
                if impl["violations"]:
                    from prediction_market_macro.strategy import arb
                    n += arb.execute(conn, spec.ticker, key, legs,
                                     impl["violations"])
                if not pr["ladder_json"]:
                    continue
                pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
                structs = enumerate_structs(legs, pmf, strict=spec.strict_gt)
            # §19-3: Kelly and every gate consume CALIBRATED probabilities
            from prediction_market_macro.strategy.calibration import calibrate_structs
            structs = calibrate_structs(conn, spec.ticker, structs)
            # §19-4 support signals: normalized entropy + devigged market fair per
            # struct + per-strike capture memory
            import math as _math
            entropy_norm, market_fairs, model_mean = None, None, None
            if spec.structure != "categorical" and pr["ladder_json"]:
                pmf_probs = [v for v in pmf.values() if v > 0]
                if len(pmf_probs) > 1:
                    h = -sum(p * _math.log(p) for p in pmf_probs)
                    entropy_norm = h / _math.log(len(pmf_probs))
                model_mean = sum(k * v for k, v in pmf.items())
                if impl.get("pmf"):
                    mk_pmf = {float(k): v for k, v in impl["pmf"].items()}
                    market_fairs = {ms.desc: ms.fair for ms in
                                    enumerate_structs(legs, mk_pmf,
                                                      strict=spec.strict_gt)}
            elif spec.structure == "categorical":
                pvals = [v for v in probs.values() if v > 0]
                if len(pvals) > 1:
                    h = -sum(p * _math.log(p) for p in pvals)
                    entropy_norm = h / _math.log(len(pvals))
            from prediction_market_macro.strategy import capture as cap_mod
            caps = cap_mod.load_strike_capture(conn, spec.ticker)
            if caps and model_mean is not None:
                strikes = {l["ticker"]: l.get("strike") for l in legs}
                structs, cap_drops = cap_mod.filter_structs(
                    structs, caps, model_mean, spec.round_rule, strikes)
                for cd in cap_drops:
                    conn.execute(
                        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                        (now.isoformat(), "info", "capture_gate",
                         f"{spec.ticker}/{key}: {cd}"))
            rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                               (spec.calendar, key)).fetchone()
            release_ts = datetime.fromisoformat(rel["scheduled_ts"]) if rel else None
            closes = [l["close_time"] for l in legs if l.get("close_time")]
            close_ts = min((datetime.fromisoformat(c.replace("Z", "+00:00")) for c in closes),
                           default=None)
            # §19-8: active structural-break flags tighten the gates for this family
            from prediction_market_macro.analysis.llm import active_flags
            from prediction_market_macro.strategy.decision import GATES as _G
            gates_eff = dict(_G)
            flags = active_flags(conn, spec.family)
            if flags:
                sev = max(f["severity"] for f in flags)
                gates_eff["max_size_usd"] = _G["max_size_usd"] * 0.5
                gates_eff["max_entropy_norm"] = _G["max_entropy_norm"] - 0.02 * sev
            # §23.2-4: ACI conformal throttle — model outside its own error
            # envelope ⇒ halve size until it re-enters
            from prediction_market_macro.strategy import conformal
            gates_eff["max_size_usd"] *= conformal.sizing_factor(conn, spec.ticker)
            # skill-aware defense: trailing OOS Brier behind the market ⇒ the
            # computed edge is mostly model error — double the bar, halve the size
            from prediction_market_macro.strategy import skill
            sk = skill.defensive(conn, spec.ticker)
            if sk is not None:
                gates_eff["min_net_edge"] *= 2.0
                gates_eff["max_size_usd"] *= 0.5
                gates_eff["fav_min_edge_per_day"] = \
                    gates_eff.get("fav_min_edge_per_day", 0.008) * 2.0
            # §23.2-3a: wide book ⇒ devigged market prob is noise ⇒ sanity gate
            # falls back to raw cost
            from prediction_market_macro.model.ensemble import WIDE_SPREAD, median_spread
            sp = median_spread(legs)
            if sp is not None and sp > WIDE_SPREAD:
                market_fairs = None
            d = decide(structs, now=now, close_time=close_ts, release_ts=release_ts,
                       market_implied=market_fairs,
                       already_open=ledger.has_open(conn, spec.ticker, key),
                       bankroll=_bankroll(conn, settings), entropy_norm=entropy_norm,
                       gates=gates_eff)
            if d.action == "open":
                from prediction_market_macro.ops import risk
                veto = risk.check(conn, spec.ticker, key, d.size_usd)
                if veto is not None:
                    from prediction_market_macro.strategy.decision import Decision
                    d = Decision("pass", None, 0.0, 0, (veto.reason,), d.gate_snapshot)
            ledger.record(conn, series=spec.ticker, period=key, decision=d,
                          pred_inputs=json.loads(pr["dist_json"]),
                          model_version=pr["model_version"])
            set_coverage(conn, spec.ticker, key,
                         "decided" if d.action == "open" else "passed")
            n += 1
    conn.commit()
    return n
