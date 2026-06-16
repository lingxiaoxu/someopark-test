"""Recent-form feature (plan 17 B.3) — national-team momentum from recent results.

Squad strength says how good a team is on paper; FORM says how it is actually
playing right now. From each team's recent national-team matches (mostly pre-WC
friendlies, stored in nt_recent) we build a time-weighted goal-difference score,
with friendlies DISCOUNTED (low intensity, rotated line-ups, less signal), then
z-score it across the 48 teams.

`form_adjusted_ratings` blends it into the model ratings behind a weight (a fourth
anchor beside FIFA rank, prior points and squad strength); PIT-safe since it only
uses matches played before the tournament.

Opponent strength is not yet factored in (most friendly opponents are outside the
48-team field, so we lack ratings for them) — a documented v2 refinement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

FRIENDLY_DISCOUNT = 0.5   # a friendly counts half a competitive game
DECAY_XI = 0.008          # exp(-xi * days_ago); ~87-day half-life
GD_CLIP = 3.0             # cap a single result's goal difference


@dataclass(frozen=True)
class FormSummary:
    team_id: str
    n: int
    n_friendly: int
    weighted_gd: float        # time+friendly weighted mean goal difference
    form_z: float             # z-scored across all teams (the index)
    recent: list              # [{opp, gf, ga, friendly, date}] most-recent first


def _mean_std(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return 0.0, 1.0
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return m, (math.sqrt(var) or 1.0)


def form_index(conn, ref: datetime | None = None) -> dict[str, FormSummary]:
    """{canonical_team_id: FormSummary} from nt_recent (recent NT results)."""
    ref = ref or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, n.opp_api_id, n.kickoff_ts, n.is_friendly, "
        "       n.gf, n.ga FROM nt_recent n "
        "JOIN team_meta tm ON tm.api_id = n.team_api_id "
        "WHERE tm.canonical_team_id IS NOT NULL").fetchall()
    agg: dict[str, dict] = {}
    for r in rows:
        d = agg.setdefault(r["cid"], {"num": 0.0, "den": 0.0, "n": 0, "nf": 0, "recent": []})
        try:
            days = max(0.0, (ref - datetime.fromisoformat(r["kickoff_ts"])).total_seconds() / 86400.0)
        except Exception:
            days = 0.0
        gd = max(-GD_CLIP, min(GD_CLIP, (r["gf"] or 0) - (r["ga"] or 0)))
        w = math.exp(-DECAY_XI * days) * (FRIENDLY_DISCOUNT if r["is_friendly"] else 1.0)
        d["num"] += w * gd
        d["den"] += w
        d["n"] += 1
        d["nf"] += 1 if r["is_friendly"] else 0
        d["recent"].append({"opp": r["opp_api_id"], "gf": r["gf"], "ga": r["ga"],
                            "friendly": bool(r["is_friendly"]), "date": r["kickoff_ts"], "_days": days})

    wgd = {cid: (d["num"] / d["den"] if d["den"] > 0 else 0.0) for cid, d in agg.items()}
    mu, sd = _mean_std(list(wgd.values()))
    out: dict[str, FormSummary] = {}
    for cid, d in agg.items():
        rec = sorted(d["recent"], key=lambda x: x["_days"])[:5]
        for x in rec:
            x.pop("_days", None)
        out[cid] = FormSummary(team_id=cid, n=d["n"], n_friendly=d["nf"],
                               weighted_gd=round(wgd[cid], 3),
                               form_z=round((wgd[cid] - mu) / sd, 4), recent=rec)
    return out


def form_adjusted_ratings(sm, idx: dict[str, FormSummary], weight: float):
    """Blend the form z-index into model ratings. weight=0 ⇒ unchanged."""
    from prediction_market.model.strength import StrengthModel
    if weight <= 0 or not idx:
        return sm
    b = sm.cfg.rating_bound
    new = dict(sm.ratings)
    for tid, s in idx.items():
        if tid in new:
            new[tid] = max(-b, min(b, new[tid] + weight * s.form_z))
    return StrengthModel(ratings=new, sigma=dict(sm.sigma), host_ids=sm.host_ids, cfg=sm.cfg)


if __name__ == "__main__":
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    conn = store.init_db()
    name = {t.team_id: t.name for t in load_prior().teams}
    idx = form_index(conn)
    ranked = sorted(idx.values(), key=lambda s: -s.form_z)
    print(f"recent form (top 10 / bottom 5 of {len(idx)}):")
    for s in ranked[:10] + ranked[-5:]:
        print(f"  {name.get(s.team_id, s.team_id):<14} form_z={s.form_z:+.2f}  wGD={s.weighted_gd:+.2f}  ({s.n} games, {s.n_friendly} friendly)")
