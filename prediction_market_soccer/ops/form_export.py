"""ops/form_export.py — recent-form view for the frontend.

Ranks all teams by the recent-form index (model/form_strength.py: time-weighted,
friendly-discounted goal difference from recent national-team results) and lists
each team's last few results. Read-only, point-in-time.

Unlike squad strength, the form blend IMPROVED the live OOS Brier (0.7339 → 0.7175
on the settled matches), so it IS used by the live model (cfg.form_blend_weight).

TWO LAYERS (club edition): `form_z` is z-scored across the whole 12-competition
pool (model/form_strength), which puts a Faroese side's 3-0 in the same
distribution as Serie A. Every row therefore also carries `league_rank`/
`league_n`/`league_z` — the club measured inside its OWN competition. The
model-facing z is untouched; these are display fields only.

    python -m prediction_market_soccer.ops.form_export  →  data/output/form.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops.squad_export import (attach_league_rank, league_membership,
                                                       league_zscores)

# 与 model/form_strength.form_index 里的小样本收缩常数保持一致(那边是函数内局部
# 变量,import 不到)。改那边必须同步改这里,否则 league_z 和 form_z 不同量纲 ——
# 一场 3-0 的球队会重新顶上赛事内榜首。
_FORM_PRIOR_N = 3.0


def build(conn=None) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.form_strength import form_index

    conn = conn or store.init_db()
    league_of, leagues_of = league_membership(conn)
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    fifa = {t.team_id: t.fifa_rank for t in prior.teams}
    idx = form_index(conn)
    ranked = sorted(idx.values(), key=lambda s: -s.form_z)
    teams = []
    for i, s in enumerate(ranked, 1):
        teams.append({
            "rank": i, "fifa_rank": fifa.get(s.team_id),
            "team_id": s.team_id, "name": name.get(s.team_id, s.team_id), "zh": zh.get(s.team_id, ""),
            "league": league_of.get(s.team_id), "leagues": leagues_of.get(s.team_id, []),
            "form_z": s.form_z, "weighted_gd": s.weighted_gd, "n": s.n, "n_friendly": s.n_friendly,
            "recent": [f"{r['gf']}-{r['ga']}{'F' if r['friendly'] else ''}" for r in s.recent],
        })
    # rank 是跨 12 项赛事的连号(1..397),法罗群岛和意甲同池 —— 保留作参考,但读者
    # 该看的是 league_rank。
    n_by_league = attach_league_rank(teams, sort_key=lambda r: r.get("form_z"))
    # league_z:把 weighted_gd 在本赛事内重新 z 化,再套同一条收缩曲线 n/(n+3),
    # 这样它和 form_z 量纲一致、只是换了参照池 —— 下面 note 里那句 "shrunk toward the
    # league mean" 对 form_z 其实不成立(那边的 mu 是 397 队全池),到这一层才成立。
    lz = league_zscores([(t["team_id"], t["league"], t["weighted_gd"]) for t in teams])
    for t in teams:
        t["global_rank"] = t["rank"]
        z, n = lz.get(t["team_id"]), (t["n"] or 0)
        t["league_z"] = round(z * (n / (n + _FORM_PRIOR_N)), 4) if z is not None else None
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_teams": len(teams),
        "n_by_league": n_by_league,
        "teams": teams,
        "note_key": "notes.form",
        "note": ("Recent CLUB form: time-weighted goal difference over the last matches "
                 "(scores shown from the club's own view, most recent first), shrunk toward "
                 "the league mean by n/(n+3) so a single result cannot top the table. "
                 "Blended into the live model at form_blend_weight."),
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "form.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"form.json: {doc['n_teams']} teams across {len(doc['n_by_league'])} competitions")
    for t in doc["teams"][:8]:
        _lr = f"{t['league']}#{t['league_rank']}/{t['league_n']}" if t.get("league_rank") else "—"
        _lz = f"{t['league_z']:+.2f}" if t.get("league_z") is not None else "—"
        print(f"  #{t['rank']:<3} {_lr:<18} {t['name']:<14} form_z={t['form_z']:+.2f} "
              f"league_z={_lz}  wGD={t['weighted_gd']:+.2f}  recent={t['recent']}")


if __name__ == "__main__":
    main()
