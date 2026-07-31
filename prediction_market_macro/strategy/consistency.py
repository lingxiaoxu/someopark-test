"""strategy/consistency.py — the model-free cross-market checks (PLAN §11 四件套).

1. KXFED ↔ KXFEDDECISION mutual implication: the rate ladder devigs to a decision
   categorical (via fed._market_prior, the same converter the model uses); compare with
   the KXFEDDECISION legs' own devigged categorical. A gap > threshold on any category
   is a market-internal mispricing — tradable without ANY model opinion.
2. CPI MoM ↔ YoY hard conversion: for the same ref month, printed index history makes
   the MoM→YoY map deterministic (up to sub-0.05 rounding slack). Push the market's
   MoM-implied pmf through the map and compare with the market's YoY pmf.
3. COMBO ↔ single legs: no COMBO series in the P0 registry — returns a documented
   no-op until one is registered.
4. Threshold-ladder monotonicity: scanned on EVERY snapshot inside decide_all
   (devig.ladder_implied violations → MONOTONE-ARB alerts); run() re-scans latest
   quotes here so a standalone consistency pass also sees them.

All findings land in alerts(source='consistency'); run() is a refresh step.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.strategy.devig import _leg_prob, ladder_implied
from prediction_market_macro.util.periods import kalshi_period_to_key

FED_GAP_ALERT = 0.07          # per-category prob gap that flags a mutual mispricing
CPI_TV_ALERT = 0.20           # total-variation gap MoM-implied-YoY vs YoY market pmf


def _alert(conn, msg: str, level: str = "info") -> None:
    """Same-day dedup: the daily rescan re-detects standing conditions (a Fed
    mutual gap persists for weeks) — one row per message per day, not per run."""
    now = datetime.now(timezone.utc)
    dup = conn.execute(
        "SELECT 1 FROM alerts WHERE source='consistency' AND message=? AND ts>=?",
        (msg, now.date().isoformat())).fetchone()
    if dup:
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now.isoformat(), level, "consistency", msg))


def _latest_legs(conn, series: str, tok: str) -> list[dict]:
    rows = conn.execute(
        "SELECT c.ticker, c.floor_strike strike, c.cap_strike, c.strike_type,"
        " q.yes_bid, q.yes_ask FROM contracts c"
        " LEFT JOIN quotes q ON q.ticker=c.ticker AND q.ts="
        "  (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker)"
        " WHERE c.series=? AND c.period=? AND c.status='active'",
        (series, tok)).fetchall()
    return [dict(r) for r in rows]


# ── 1. KXFED ↔ KXFEDDECISION ────────────────────────────────────────────────
def fed_mutual(conn, asof: datetime) -> list[dict]:
    from prediction_market_macro.model.fed import CATS, _market_prior
    findings = []
    toks = [r["period"] for r in conn.execute(
        "SELECT DISTINCT period FROM contracts WHERE series='KXFEDDECISION'"
        " AND status='active'")]
    for tok in toks:
        key = kalshi_period_to_key(tok)
        if not key:
            continue
        from_ladder, _ts = _market_prior(conn, asof, key)
        if from_ladder is None:
            continue
        legs = _latest_legs(conn, "KXFEDDECISION", tok)
        raw = {}
        for l in legs:
            cat = l["ticker"].rsplit("-", 1)[-1]
            p = _leg_prob(l.get("yes_bid"), l.get("yes_ask"))
            if cat in CATS and p is not None:
                raw[cat] = p
        if len(raw) < 3:
            continue
        tot = sum(raw.values())
        direct = {k: v / tot for k, v in raw.items()}
        gaps = {k: round(from_ladder.get(k, 0.0) - direct.get(k, 0.0), 4)
                for k in set(from_ladder) | set(direct)}
        worst = max(gaps, key=lambda k: abs(gaps[k]))
        if abs(gaps[worst]) > FED_GAP_ALERT:
            _alert(conn, f"FED-MUTUAL {tok}: ladder-implied vs direct categorical gap"
                         f" {worst}={gaps[worst]:+.3f} (all: {gaps})")
            findings.append({"period": tok, "gaps": gaps})
    return findings


# ── 2. CPI MoM ↔ YoY ────────────────────────────────────────────────────────
def cpi_mom_yoy(conn, asof: datetime) -> list[dict]:
    import pandas as pd
    findings = []
    toks = [r["period"] for r in conn.execute(
        "SELECT DISTINCT period FROM contracts WHERE series='KXCPI' AND status='active'")]
    idx_rows = conn.execute(
        "SELECT event_time, value, MAX(vintage_date) FROM fred_obs WHERE sid='CPIAUCSL'"
        " AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
        (asof.isoformat(),)).fetchall()
    idx = {pd.Timestamp(r["event_time"]).strftime("%Y-%m"): float(r["value"])
           for r in idx_rows}
    for tok in toks:
        key = kalshi_period_to_key(tok)
        if not key:
            continue
        per = pd.Period(key, freq="M")
        prev_m, base_m = str(per - 1), str(per - 12)
        if prev_m not in idx or base_m not in idx:
            continue                       # unprinted prior month → map not model-free
        mom = ladder_implied(_latest_legs(conn, "KXCPI", tok))
        yoy_tok = next((r["period"] for r in conn.execute(
            "SELECT DISTINCT period FROM contracts WHERE series='KXCPIYOY'"
            " AND status='active'") if r["period"] == tok), None)
        if not mom["pmf"] or yoy_tok is None:
            continue
        yoy = ladder_implied(_latest_legs(conn, "KXCPIYOY", yoy_tok))
        if not yoy["pmf"]:
            continue
        # push MoM pmf through the exact index map, land on the YoY 0.1 grid
        implied_yoy: dict[float, float] = {}
        for mom_x, mass in mom["pmf"].items():
            if mom_x == float("inf"):
                continue
            i_new = idx[prev_m] * (1 + mom_x / 100.0)
            y = round(round((i_new / idx[base_m] - 1) * 1000) / 10, 1)
            implied_yoy[y] = implied_yoy.get(y, 0.0) + mass
        # compare on the union of YoY strikes (survival-space, robust to grid offsets)
        yoy_strikes = [s for s in yoy["pmf"] if s != float("inf")]
        tv = 0.0
        for s in yoy_strikes:
            a = sum(m for y, m in implied_yoy.items() if y > s)
            b = sum(m for y, m in yoy["pmf"].items() if y != float("inf") and y > s) \
                + yoy["pmf"].get(float("inf"), 0.0)
            tv = max(tv, abs(a - b))
        if tv > CPI_TV_ALERT:
            _alert(conn, f"CPI-MOM-YOY {tok}: max survival gap {tv:.3f} between"
                         f" MoM-implied and direct YoY books")
            findings.append({"period": tok, "max_gap": round(tv, 4)})
    return findings


# ── 3. COMBO ↔ legs (no COMBO series registered yet) ─────────────────────────
def combo_vs_legs(conn, asof: datetime) -> list[dict]:
    return []          # P0 registry has no COMBO series; activates when one is added


# ── 4. ladder monotonicity across all ladder series (latest quotes) ─────────
def ladder_monotone(conn, asof: datetime) -> list[dict]:
    findings = []
    for spec in REGISTRY.values():
        if spec.structure != "ladder":
            continue
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)):
            legs = [l for l in _latest_legs(conn, spec.ticker, r["period"])
                    if l.get("strike_type") not in ("between", "less", "less_or_equal")]
            impl = ladder_implied(legs)
            for v in impl["violations"]:
                _alert(conn, f"MONOTONE-ARB {spec.ticker}/{r['period']}:"
                             f" buy {v['buy']['ticker']} sell {v['sell']['ticker']}"
                             f" gross {v['gross']}")
                findings.append({"series": spec.ticker, "period": r["period"], **v})
    return findings


def term_structure(conn, asof: datetime) -> list[dict]:
    """§24-C fifth check: same series, consecutive periods — the MODEL's implied
    curve slope vs the MARKET's. When the two curves disagree in DIRECTION and the
    magnitude gap is large, one side is mispricing the term structure (base
    effects across CPI months, meeting-path for Fed). Detection only (P2 trades)."""
    import json as _json
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.strategy.devig import ladder_implied
    from prediction_market_macro.util.periods import kalshi_period_to_key
    findings = []
    for spec in REGISTRY.values():
        if spec.structure != "ladder":
            continue
        toks = [r["period"] for r in conn.execute(
            "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
            (spec.ticker,)).fetchall()]
        pts = []                                   # (key, model_mean, market_mean)
        for tok in sorted(toks):
            key = kalshi_period_to_key(tok)
            if not key:
                continue
            pr = conn.execute(
                "SELECT ladder_json FROM preds WHERE series=? AND period=?"
                " AND model_version LIKE ? ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            if pr is None or not pr["ladder_json"]:
                continue
            pmf = {float(k): v for k, v in _json.loads(pr["ladder_json"]).items()}
            m_model = sum(k * v for k, v in pmf.items())
            legs = _latest_legs(conn, spec.ticker, tok)
            impl = ladder_implied(legs)
            finite = {k: v for k, v in impl["pmf"].items() if k != float("inf")}
            if sum(finite.values()) < 0.5:
                continue
            m_mkt = sum(k * v for k, v in finite.items()) / sum(finite.values())
            pts.append((key, m_model, m_mkt))
        pts.sort()
        for (k1, mo1, mk1), (k2, mo2, mk2) in zip(pts[:-1], pts[1:]):
            d_model, d_mkt = mo2 - mo1, mk2 - mk1
            scale = max(abs(mo1), abs(mk1), spec.round_rule * 10, 1e-9)
            if (d_model * d_mkt < 0
                    and abs(d_model - d_mkt) / scale > 0.10):
                msg = (f"TERM-STRUCTURE {spec.ticker} {k1}->{k2}: model slope"
                       f" {d_model:+.4g} vs market {d_mkt:+.4g}")
                _alert(conn, msg)
                findings.append({"series": spec.ticker, "from": k1, "to": k2,
                                 "d_model": round(d_model, 5),
                                 "d_market": round(d_mkt, 5)})
    return findings


def run(conn) -> dict:
    now = datetime.now(timezone.utc)
    out = {"fed_mutual": fed_mutual(conn, now), "cpi_mom_yoy": cpi_mom_yoy(conn, now),
           "combo": combo_vs_legs(conn, now), "monotone": ladder_monotone(conn, now),
           "term_structure": term_structure(conn, now)}
    conn.commit()
    return {k: len(v) for k, v in out.items()}
