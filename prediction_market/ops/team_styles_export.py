"""Team STYLE taxonomy export (P1) → data/output/team_styles.json for the frontend.

Profiles each team that has played by its intra-game metrics (possession / passing /
directness / shot profile / pressing), KMeans-clusters into named style types, and labels
each cluster from its standout features. DESCRIPTIVE (style-matchup carries no validated
predictive alpha on the current sample) — a scouting aid, not a model input.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict

from prediction_market.config import CONFIG

_MET = {"expected_goals": "xG", "Shots on Goal": "sot", "Total Shots": "shots",
        "Shots insidebox": "sin", "Shots outsidebox": "sout", "Ball Possession": "poss",
        "Passes %": "passpct", "Total passes": "passes", "Fouls": "fouls", "Corner Kicks": "corners"}

_FN = ["poss", "passpct", "passes", "shots", "sin", "direct", "xG", "chanceQ", "press", "corners"]


def _label_cluster(centroid: dict) -> str:
    """Name a cluster from its single most-extreme signed standout feature, so the 9
    KMeans clusters keep distinct, interpretable style labels (not over-merged)."""
    f = centroid
    # priority order of distinctive signatures (strong, specific ones first)
    if f["xG"] > 1.0 and f["sin"] > 0.8:
        return "碾压强攻 Dominant attack"
    if f["chanceQ"] > 0.8:
        return "高效终结 Clinical"
    if f["shots"] > 1.0 and f["corners"] > 0.8:
        return "高射门量 High volume"
    if f["direct"] > 0.6:
        return "直接/远射 Direct"
    if f["press"] > 0.6:
        return "高压逼抢 High press"
    if f["poss"] > 0.4 or f["passpct"] > 0.4:
        return "控球传导 Possession"
    if f["passpct"] < -0.8 or f["poss"] < -0.8:
        return "低控反击 Low-block"
    if f["shots"] < -0.8 or f["direct"] < -0.8:
        return "被压制 Contained"
    return "均衡 Balanced"


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").strip())
    except ValueError:
        return None


def build(conn=None) -> dict:
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior

    conn = conn or store.init_db()
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
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

    teams, feats = [], []
    for t in sorted(prof):
        p = prof[t]
        def a(k):
            return statistics.mean(p[k]) if p.get(k) else None
        poss, passpct, shots, xg = a("poss"), a("passpct"), a("shots"), a("xG")
        if None in (poss, passpct, shots, xg):
            continue
        sout, sin = a("sout") or 0, a("sin") or 0
        teams.append(t)
        feats.append([poss, passpct, a("passes") or 0, shots, sin,
                      (sout / shots if shots else 0), xg, (xg / shots if shots else 0),
                      a("fouls") or 0, a("corners") or 0])
    if len(teams) < 9:
        return {"teams": [], "n": 0}
    X = StandardScaler().fit_transform(np.array(feats))
    km = KMeans(n_clusters=9, n_init=10, random_state=0).fit(X)
    cent = km.cluster_centers_

    def label_for(cl):
        return _label_cluster(dict(zip(_FN, cent[cl])))

    out = []
    for t, raw, cl in zip(teams, feats, km.labels_):
        out.append({"team_id": t, "name": name.get(t, t), "zh": zh.get(t, ""),
                    "style": label_for(cl), "cluster": int(cl),
                    "metrics": {"possession": round(raw[0] / 100, 3), "pass_pct": round(raw[1] / 100, 3),
                                "shots": round(raw[3], 1), "xg": round(raw[6], 2),
                                "directness": round(raw[5], 2), "chance_q": round(raw[7], 2)}})
    out.sort(key=lambda x: (x["style"], -x["metrics"]["xg"]))
    from datetime import datetime, timezone
    return {"ts": datetime.now(timezone.utc).isoformat(), "n": len(out), "teams": out}


def main():
    conn = None
    doc = build(conn)
    CONFIG.paths.ensure()
    txt = json.dumps(doc, ensure_ascii=False, indent=2)
    (CONFIG.paths.output / "team_styles.json").write_text(txt, encoding="utf-8")
    try:
        (CONFIG.paths.frontend_data / "team_styles.json").write_text(txt, encoding="utf-8")
    except Exception:
        pass
    print(f"team_styles.json: {doc['n']} teams classified")
    from collections import Counter
    for style, c in Counter(t["style"] for t in doc["teams"]).most_common():
        print(f"  {style}: {c}")


if __name__ == "__main__":
    main()
