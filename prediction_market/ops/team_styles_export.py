"""Team STYLE taxonomy export (P1) → data/output/team_styles.json for the frontend.

A team can play TWO styles (e.g. Spain = possession + high-press), so this is a MATRIX:
48 teams × 10 fixed styles. Each team is assigned 1–2 styles; for each style it has, the
cell carries the team's possession (`poss`) — within a style column teams are ranked by
poss (cells do NOT sum to 100%). Refreshed WEEKLY (descriptive, slow-moving).

Robust data source = a curated research PRIOR (each WC team's known football style, from
football knowledge) BLENDED with live API-Football intra-game metrics (possession /
passing / directness / shot profile). The prior covers teams that haven't played yet and
stabilises small live samples; the live metrics refine it as matches are played.

DESCRIPTIVE — a scouting aid, not a model input. The output stays a SUPERSET of the old
schema: a `_with_legacy()` adapter keeps the single `style` / `cluster` / `metrics` fields
so every existing downstream reader (agent tool, etc.) works UNCHANGED.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from prediction_market.config import CONFIG

# ── 10 fixed styles (matrix columns) ─────────────────────────────────────────
STYLES: list[tuple[str, str]] = [
    ("possession",      "控球传导 Possession"),
    ("direct",          "直接快攻 Direct"),
    ("high_press",      "高压逼抢 High press"),
    ("low_block",       "低位防反 Low-block"),
    ("dominant_attack", "碾压强攻 Dominant attack"),
    ("clinical",        "高效终结 Clinical"),
    ("high_volume",     "高射门量 High volume"),
    ("set_piece",       "定位球 Set-piece"),
    ("balanced",        "稳健均衡 Balanced"),
    ("contained",       "低效被压制 Contained"),
]
STYLE_LABEL = dict(STYLES)
STYLE_CODES = [c for c, _ in STYLES]

# Default possession by a team's PRIMARY style, for teams with no live data yet, so the
# matrix isn't a flat 0.50 before a team has played.
_DEFAULT_POSS = {
    "possession": 0.58, "direct": 0.45, "high_press": 0.54, "low_block": 0.40,
    "dominant_attack": 0.56, "clinical": 0.50, "high_volume": 0.53, "set_piece": 0.47,
    "balanced": 0.50, "contained": 0.42,
}

# ── Curated research prior: team_id → [(style_code, weight)], primary first ───
# Football-knowledge styles for the 48 WC-2026 teams (blended with live metrics below).
PRIOR: dict[str, list[tuple[str, float]]] = {
    "algeria":                [("direct", 1.0), ("high_press", 0.6)],
    "argentina":              [("possession", 1.0), ("clinical", 0.7)],
    "australia":              [("direct", 1.0), ("high_volume", 0.6)],
    "austria":                [("high_press", 1.0), ("direct", 0.7)],
    "belgium":                [("possession", 1.0), ("dominant_attack", 0.7)],
    "bosnia_and_herzegovina": [("direct", 1.0), ("set_piece", 0.6)],
    "brazil":                 [("possession", 1.0), ("dominant_attack", 0.8)],
    "canada":                 [("direct", 1.0), ("high_press", 0.6)],
    "cape_verde":             [("low_block", 1.0), ("direct", 0.6)],
    "colombia":               [("possession", 1.0), ("direct", 0.6)],
    "cote_divoire":           [("possession", 1.0), ("direct", 0.6)],
    "croatia":                [("possession", 1.0), ("balanced", 0.6)],
    "curacao":                [("low_block", 1.0), ("contained", 0.6)],
    "czechia":                [("direct", 1.0), ("set_piece", 0.6)],
    "dr_congo":               [("direct", 1.0), ("high_press", 0.6)],
    "ecuador":                [("high_press", 1.0), ("direct", 0.7)],
    "egypt":                  [("low_block", 1.0), ("direct", 0.7)],
    "england":                [("possession", 1.0), ("high_volume", 0.7)],
    "france":                 [("dominant_attack", 1.0), ("direct", 0.7)],
    "germany":                [("possession", 1.0), ("high_press", 0.7)],
    "ghana":                  [("direct", 1.0), ("high_volume", 0.6)],
    "haiti":                  [("low_block", 1.0), ("contained", 0.6)],
    "iran":                   [("low_block", 1.0), ("direct", 0.6)],
    "iraq":                   [("low_block", 1.0), ("direct", 0.6)],
    "japan":                  [("possession", 1.0), ("high_press", 0.7)],
    "jordan":                 [("low_block", 1.0), ("set_piece", 0.6)],
    "korea_republic":         [("high_press", 1.0), ("direct", 0.7)],
    "mexico":                 [("possession", 1.0), ("high_volume", 0.6)],
    "morocco":                [("low_block", 1.0), ("clinical", 0.7)],
    "netherlands":            [("possession", 1.0), ("dominant_attack", 0.7)],
    "new_zealand":            [("direct", 1.0), ("set_piece", 0.7)],
    "norway":                 [("direct", 1.0), ("dominant_attack", 0.7)],
    "panama":                 [("low_block", 1.0), ("direct", 0.6)],
    "paraguay":               [("low_block", 1.0), ("set_piece", 0.6)],
    "portugal":               [("possession", 1.0), ("dominant_attack", 0.7)],
    "qatar":                  [("possession", 1.0), ("low_block", 0.6)],
    "saudi_arabia":           [("high_press", 1.0), ("direct", 0.7)],
    "scotland":               [("direct", 1.0), ("set_piece", 0.7)],
    "senegal":                [("dominant_attack", 1.0), ("direct", 0.7)],
    "south_africa":           [("high_press", 1.0), ("direct", 0.6)],
    "spain":                  [("possession", 1.0), ("high_press", 0.8)],
    "sweden":                 [("direct", 1.0), ("set_piece", 0.7)],
    "switzerland":            [("balanced", 1.0), ("low_block", 0.6)],
    "tunisia":                [("low_block", 1.0), ("direct", 0.6)],
    "turkey":                 [("direct", 1.0), ("high_volume", 0.6)],
    "united_states":          [("high_press", 1.0), ("direct", 0.7)],
    "uruguay":                [("direct", 1.0), ("clinical", 0.7)],
    "uzbekistan":             [("low_block", 1.0), ("direct", 0.6)],
}

# How strongly a 2nd style must score (vs the top) to be assigned — a genuine dual style.
_SECOND_STYLE_FRAC = 0.62
_PRIOR_WEIGHT = 0.55   # prior vs live blend (prior dominates until live samples accrue)
_LIVE_WEIGHT = 0.45

_MET = {"expected_goals": "xG", "Shots on Goal": "sot", "Total Shots": "shots",
        "Shots insidebox": "sin", "Shots outsidebox": "sout", "Ball Possession": "poss",
        "Passes %": "passpct", "Total passes": "passes", "Fouls": "fouls", "Corner Kicks": "corners"}


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").strip())
    except ValueError:
        return None


def _zmap(vals: dict[str, float]) -> dict[str, float]:
    """z-score a {team: value} map (robust to <2 teams)."""
    xs = list(vals.values())
    if len(xs) < 2:
        return {k: 0.0 for k in vals}
    mu = statistics.mean(xs)
    sd = statistics.pstdev(xs) or 1.0
    return {k: (v - mu) / sd for k, v in vals.items()}


def _live_style_scores(z: dict[str, float]) -> dict[str, float]:
    """Per-style affinity from a team's z-scored live features (hand-defined signatures).
    Styles that are weakly observable from box stats (e.g. high_press) lean on the prior."""
    g = lambda k: z.get(k, 0.0)
    return {
        "possession":      g("poss") + g("passpct"),
        "direct":          g("direct") - 0.7 * g("poss"),
        "high_press":      0.5 * g("fouls") + 0.4 * g("shots") - 0.3 * g("poss"),
        "low_block":       -g("poss") - 0.6 * g("shots"),
        "dominant_attack": g("xG") + g("sin"),
        "clinical":        g("chanceQ"),
        "high_volume":     g("shots") + 0.7 * g("corners"),
        "set_piece":       0.7 * g("corners") + 0.5 * g("fouls"),
        "balanced":        -0.5 * (abs(g("poss")) + abs(g("xG")) + abs(g("shots"))),
        "contained":       -g("shots") - g("xG"),
    }


def build(conn=None) -> dict:
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    all_teams = sorted(name)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    # 1) aggregate live metrics per team (mean over played fixtures)
    prof = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT team_api_id, raw_json FROM fixture_stats WHERE raw_json IS NOT NULL"):
        t = cmap.get(r["team_api_id"])
        if not t:
            continue
        for s in json.loads(r["raw_json"]).get("statistics", []):
            k = _MET.get(s.get("type"))
            if k:
                v = _num(s.get("value"))
                if v is not None:
                    prof[t][k].append(v)

    def agg(t, k):
        return statistics.mean(prof[t][k]) if prof.get(t) and prof[t].get(k) else None

    # 2) raw live features per played team → derived metrics
    raw: dict[str, dict[str, float]] = {}
    for t in prof:
        poss, passpct, shots, xg = agg(t, "poss"), agg(t, "passpct"), agg(t, "shots"), agg(t, "xG")
        if None in (poss, passpct, shots, xg):
            continue
        sout = agg(t, "sout") or 0.0
        sin = agg(t, "sin") or 0.0
        raw[t] = {
            "poss": poss, "passpct": passpct, "passes": agg(t, "passes") or 0.0,
            "shots": shots, "sin": sin, "direct": (sout / shots if shots else 0.0),
            "xG": xg, "chanceQ": (xg / shots if shots else 0.0),
            "fouls": agg(t, "fouls") or 0.0, "corners": agg(t, "corners") or 0.0,
        }

    # 3) z-score each live feature across played teams, then per-style live affinity
    feat_keys = ["poss", "passpct", "passes", "shots", "sin", "direct", "xG", "chanceQ", "fouls", "corners"]
    zfeat = {k: _zmap({t: raw[t][k] for t in raw}) for k in feat_keys}
    live_scores = {t: _live_style_scores({k: zfeat[k][t] for k in feat_keys}) for t in raw}

    # 4) prior score per team (weight on the named styles, 0 elsewhere)
    def prior_scores(t):
        sc = {c: 0.0 for c in STYLE_CODES}
        for code, w in PRIOR.get(t, [("balanced", 1.0)]):
            sc[code] = w
        return sc

    # 5) blend → assign 1–2 styles
    teams_out = []
    for t in all_teams:
        ps = prior_scores(t)
        ls = live_scores.get(t)
        if ls:
            lo, hi = min(ls.values()), max(ls.values())
            rng = (hi - lo) or 1.0
            lsn = {c: (ls[c] - lo) / rng for c in STYLE_CODES}   # → ~[0,1], comparable to prior
            blend = {c: _PRIOR_WEIGHT * ps[c] + _LIVE_WEIGHT * lsn[c] for c in STYLE_CODES}
        else:
            blend = ps
        ranked = sorted(STYLE_CODES, key=lambda c: -blend[c])
        chosen = [ranked[0]]
        if blend[ranked[1]] >= _SECOND_STYLE_FRAC * blend[ranked[0]] and blend[ranked[1]] > 0:
            chosen.append(ranked[1])
        poss = round(raw[t]["poss"] / 100.0, 3) if t in raw else _DEFAULT_POSS[chosen[0]]
        teams_out.append({
            "team_id": t, "name": name.get(t, t), "zh": zh.get(t, ""),
            "poss": poss, "played": t in raw,
            "styles": [{"code": c} for c in chosen],
            "_raw": raw.get(t),
        })

    # 6) within each style column, rank teams by poss (desc); attach poss+rank to each cell
    for c in STYLE_CODES:
        members = [tm for tm in teams_out if any(s["code"] == c for s in tm["styles"])]
        members.sort(key=lambda tm: -tm["poss"])
        for i, tm in enumerate(members):
            for s in tm["styles"]:
                if s["code"] == c:
                    s["rank"] = i + 1

    # 7) legacy projection so existing downstream readers work unchanged
    teams_final = [_with_legacy(tm) for tm in teams_out]
    teams_final.sort(key=lambda x: (x["style"], -x["poss"]))
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n": len(teams_final),
        "styles": [{"code": c, "label": STYLE_LABEL[c]} for c in STYLE_CODES],
        "teams": teams_final,
    }


def _with_legacy(tm: dict) -> dict:
    """Adapter: project the new multi-style team onto the OLD single-style schema so every
    existing downstream reader (agent tool, etc.) keeps working without any change."""
    primary = tm["styles"][0]["code"]
    rawm = tm.get("_raw") or {}
    metrics = {
        "possession": tm["poss"],
        "pass_pct": round(rawm["passpct"] / 100.0, 3) if rawm.get("passpct") is not None else None,
        "shots": round(rawm["shots"], 1) if rawm.get("shots") is not None else None,
        "xg": round(rawm["xG"], 2) if rawm.get("xG") is not None else None,
        "directness": round(rawm["direct"], 2) if rawm.get("direct") is not None else None,
        "chance_q": round(rawm["chanceQ"], 2) if rawm.get("chanceQ") is not None else None,
    }
    return {
        "team_id": tm["team_id"], "name": tm["name"], "zh": tm["zh"],
        "poss": tm["poss"], "played": tm["played"],
        # NEW: 1–2 styles, each with poss + within-style rank
        "styles": [{"code": s["code"], "label": STYLE_LABEL[s["code"]], "poss": tm["poss"], "rank": s["rank"]}
                   for s in tm["styles"]],
        # LEGACY (backward-compatible): single primary style + cluster index + metrics
        "style": STYLE_LABEL[primary],
        "cluster": STYLE_CODES.index(primary),
        "metrics": metrics,
    }


def main(force: bool = False):
    # Weekly cadence: team styles are descriptive and slow-moving, so rebuild at most once
    # every 7 days. Lets refresh_all call this every run without re-clustering each time.
    out_file = CONFIG.paths.output / "team_styles.json"
    if not force and out_file.exists():
        try:
            prev = json.loads(out_file.read_text(encoding="utf-8"))
            age_d = (datetime.now(timezone.utc) - datetime.fromisoformat(prev["ts"])).total_seconds() / 86400
            if age_d < 7:
                print(f"team_styles.json: fresh ({age_d:.1f}d < 7d) — weekly rebuild skipped")
                return
        except Exception:
            pass
    doc = build()
    CONFIG.paths.ensure()
    txt = json.dumps(doc, ensure_ascii=False, indent=2)
    (CONFIG.paths.output / "team_styles.json").write_text(txt, encoding="utf-8")
    try:
        (CONFIG.paths.frontend_data / "team_styles.json").write_text(txt, encoding="utf-8")
    except Exception:
        pass
    print(f"team_styles.json: {doc['n']} teams × {len(STYLES)} styles "
          f"({sum(len(t['styles']) for t in doc['teams'])} style assignments; "
          f"{sum(t['played'] for t in doc['teams'])} played)")


if __name__ == "__main__":
    main()
