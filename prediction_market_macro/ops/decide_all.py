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
                    conn.execute(
                        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                        (now.isoformat(), "info", "consistency", msg))
                if not pr["ladder_json"]:
                    continue
                pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
                structs = enumerate_structs(legs, pmf, strict=spec.strict_gt)
            rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                               (spec.calendar, key)).fetchone()
            release_ts = datetime.fromisoformat(rel["scheduled_ts"]) if rel else None
            closes = [l["close_time"] for l in legs if l.get("close_time")]
            close_ts = min((datetime.fromisoformat(c.replace("Z", "+00:00")) for c in closes),
                           default=None)
            d = decide(structs, now=now, close_time=close_ts, release_ts=release_ts,
                       market_implied=impl["pmf"] or None,
                       already_open=ledger.has_open(conn, spec.ticker, key),
                       bankroll=_bankroll(conn, settings))
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
