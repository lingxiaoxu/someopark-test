"""ops/squad_export.py — squad-strength view for the frontend.

Ranks every registered club by the squad-strength index (model/squad_strength.py:
minutes-weighted club rating + attacking output) and lists each team's top
players by goals+assists. Read-only, point-in-time (club-2025 data).

TWO LAYERS (club edition): the board spans 12 competitions, so every row carries
BOTH `global_rank`/`score_z` (cross-competition reference — Arsenal vs a UECL
qualifier) and `league_rank`/`league_n`/`league_z` (the club inside its OWN
competition, which is the only comparison that means anything). `league` is the
club's home competition; `leagues` lists every competition it is registered in.

Honest note: the squad index is a heuristic quality ranking. The rating is now
LEAGUE-STRENGTH weighted (model/squad_strength._LEAGUE_STRENGTH) so a high match
rating in a weaker league doesn't inflate a squad (this fixed Portugal/Algeria/
Saudi ranking far too high). It IS blended into the live model at a small weight
(cfg.squad_blend_weight=0.15); its impact on the settled-match Brier is ≈neutral.
Re-validate via param_sweep/backtest as more data accrues.

    python -m prediction_market_soccer.ops.squad_export  →  data/output/squad.json
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

# ── 赛事内分层 (board-layer league scoping) ───────────────────────────────────
# 世界杯模块只有一个赛事,所以一张全局榜就是全部;俱乐部模块有 12 项赛事,把
# 254/397/399 支球队按同一个 z 排成一张榜,等于拿英超和欧协联资格赛直接比 ——
# rank 数字没有意义,z 也不可比(样本分布、开赛时间、对手强度全不同)。
# 所以每张球队榜都要多带一层"在自己赛事内"的名次与 z。
#
# 这三个 helper 由 squad / form / team_styles 三张榜共用,放在这里是因为
# ops 内部互相 import 是本模块的既有写法(milestone_export → performance_report.
# _row_comp);等第四张榜也要用时再抽到 util/。


# 一个俱乐部会同时注册在联赛和它打进的杯赛里(阿森纳 = epl + ucl),club_registry
# 的主键是 (club_id, comp),506 行只对应 399 个 club。"在自己赛事内排名"必须先定
# 一个唯一归属:国内联赛整赛季稳定,杯赛是会被淘汰的临时归属,所以优先联赛。
# 阿甲 kind='league_playoffs' 同样是国内联赛(只是带季后赛),排第二档 —— 否则
# 博卡(argentina/libertadores/sudamericana)会被判进解放者杯。
_HOME_KIND_ORDER = ("league", "league_playoffs")


def league_membership(conn) -> tuple[dict[str, str], dict[str, list[str]]]:
    """({club_id: 主赛事}, {club_id: [全部注册赛事]}) from club_registry.

    主赛事用于分组排名;完整列表让前端的杯赛 chip 能按"参赛"而不是"归属"筛选 ——
    归属口径下点 UCL 只会出现母联赛不在五大联赛的那十几队,而不是 52 支参赛队。
    """
    from prediction_market_soccer.config.leagues import REGISTRY
    all_of: dict[str, list[str]] = {}
    # ORDER BY 让"没有联赛归属时取第一个"是确定性的(字典序),不随行插入顺序漂移
    for r in conn.execute("SELECT club_id, comp FROM club_registry ORDER BY club_id, comp"):
        all_of.setdefault(r["club_id"], []).append(r["comp"])
    primary = {
        cid: next((c for kind in _HOME_KIND_ORDER for c in comps
                   if (REGISTRY[c].kind if c in REGISTRY else "") == kind), comps[0])
        for cid, comps in all_of.items()
    }
    return primary, all_of


def attach_league_rank(rows: list[dict], key: str = "league",
                       sort_key=None) -> dict[str, int]:
    """就地给每行写 league_rank / league_n(它在自己赛事内的名次 + 该赛事队数)。

    ``sort_key`` 给出赛事内的排序依据(越大越靠前)。**不能**直接沿用榜单的全局
    顺序重编号:squad 榜把所有 season 行整块排在 fc26_talent 行之前,在赛事内重
    编号就会让"有赛季样本的弱队"压过"还没开赛的拜仁",输出一个事实错误的名次。
    给不出可比指标的行(赛事内样本不足)排在该赛事末尾,并按其原有顺序保持稳定。
    返回 {赛事: 队数} 供榜单头部使用。
    """
    n_by: dict[str, int] = {}
    by_lg: dict[str, list] = {}
    for i, r in enumerate(rows):
        lg = r.get(key)
        if lg is None:
            # 不在 club_registry 里的球队没有赛事可归 —— 给 None,不假装它排第 1
            r["league_rank"], r["league_n"] = None, None
            continue
        n_by[lg] = n_by.get(lg, 0) + 1
        by_lg.setdefault(lg, []).append((i, r))
    for lg, items in by_lg.items():
        if sort_key is not None:
            # None 值排末尾(-inf),其余按指标降序;原顺序作稳定次键
            items = sorted(items, key=lambda t: (
                -(sort_key(t[1]) if sort_key(t[1]) is not None else float("-inf")), t[0]))
        for rank, (_i, r) in enumerate(items, 1):
            r["league_rank"], r["league_n"] = rank, n_by[lg]
    return n_by

def league_zscores(items) -> dict[str, float]:
    """{team_id: z} —— 把原始指标在**本赛事内**重新 z 化。

    items = [(team_id, 赛事, 原始值)];原始值为 None 的行跳过(它们本来就没有 z)。
    赛事内不足 2 支有数据的球队时不给 z:此时那支球队按定义就等于"赛事均值",算出
    的 0.0 会被读成"德甲中游"而其实只是没有同赛事对照(德甲开赛前只有 1 支有样本)。
    宁可留空,也不编造 —— 与本文件 fc26_talent 兜底行不编造 score_z 的口径一致。
    注意这只是导出层给前端看的字段,模型混合用的 z 仍在 model/ 里算。
    """
    by: dict[str, list] = {}
    for tid, lg, v in items:
        if v is None or lg is None:
            continue
        by.setdefault(lg, []).append((tid, float(v)))
    out: dict[str, float] = {}
    for lg, lst in by.items():
        xs = [v for _, v in lst]
        if len(xs) < 2:
            continue
        mu = statistics.mean(xs)
        sd = statistics.pstdev(xs) or 1.0
        for tid, v in lst:
            out[tid] = round((v - mu) / sd, 4)
    return out


def _top_players(conn, team_api_ids: dict, top_n: int = 4) -> dict:
    """{canonical_team_id: [{name, goals, assists, rating}]} top by goals+assists.

    CLUB EDITION: player_stat.team_api_id IS the club — no roster (squad-table)
    indirection needed, so this works without ever calling the per-club squads
    endpoint (WC needed the join because stats were club rows but squads were
    national)."""
    # Per-fixture lineup feed = every player who appeared (not just the league's top
    # 20 scorers); sum the season to date per player, then rank within the club.
    rows = conn.execute(
        "SELECT tm.canonical_team_id cid, fps.player_api_id pid, fps.player_name name, "
        "       SUM(COALESCE(fps.goals,0)) goals, SUM(COALESCE(fps.assists,0)) assists, "
        "       AVG(fps.rating) rating "
        "FROM fixture_player_stats fps JOIN team_meta tm ON tm.api_id = fps.team_api_id "
        "WHERE tm.canonical_team_id IS NOT NULL "
        "GROUP BY tm.canonical_team_id, fps.player_api_id").fetchall()
    covered = {r["cid"] for r in rows}
    rows = list(rows) + [r for r in conn.execute(
        "SELECT tm.canonical_team_id cid, p.api_id pid, p.name, ps.goals, ps.assists, ps.rating "
        "FROM player_stat ps JOIN team_meta tm ON tm.api_id = ps.team_api_id "
        "JOIN player p ON p.api_id = ps.player_api_id "
        "WHERE tm.canonical_team_id IS NOT NULL").fetchall()
        if r["cid"] not in covered]
    # A player can have multiple player_stat rows (different league/season), which would list
    # them TWICE (e.g. Canada "P. David, P. David"). Dedup by (team, player) keeping the single
    # best goals+assists row.
    best: dict[tuple, dict] = {}
    for r in rows:
        ga = (r["goals"] or 0) + (r["assists"] or 0)
        key = (r["cid"], r["pid"])
        if key not in best or ga > best[key]["_ga"]:
            best[key] = {"name": r["name"], "goals": r["goals"] or 0, "assists": r["assists"] or 0,
                         "rating": round(r["rating"], 2) if r["rating"] is not None else None, "_ga": ga}
    by_team: dict[str, list] = {}
    for (cid, _pid), v in best.items():
        by_team.setdefault(cid, []).append(v)
    for cid, lst in by_team.items():
        lst.sort(key=lambda x: -x["_ga"])
        by_team[cid] = [{k: val for k, val in p.items() if k != "_ga"} for p in lst[:top_n]]
    return by_team


def build(conn=None) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.squad_strength import squad_index

    conn = conn or store.init_db()
    league_of, leagues_of = league_membership(conn)
    prior = load_prior()
    name = {t.team_id: t.name for t in prior.teams}
    zh = {t.team_id: t.zh for t in prior.teams}
    fifa = {t.team_id: t.fifa_rank for t in prior.teams}
    idx = squad_index(conn)
    tops = _top_players(conn, {})

    # FC26 talent axis: avg of each club's top-18 overall (a "best XVIII" talent
    # index) + top names. Enriches season rows and provides an honest fallback
    # row for clubs whose season hasn't started (no API player data yet).
    fc_rows = conn.execute(
        "SELECT canonical_team_id cid, name, overall, position FROM fc_player "
        "ORDER BY canonical_team_id, overall DESC").fetchall()
    fc_by_club: dict[str, list] = {}
    for r in fc_rows:
        fc_by_club.setdefault(r["cid"], []).append(r)
    fc_talent = {cid: round(sum(x["overall"] for x in lst[:18]) / min(len(lst), 18), 1)
                 for cid, lst in fc_by_club.items() if lst}

    # A club needs a real season sample to be ranked on season data: fewer than
    # MIN_PLAYERS distinct players means the feed has not caught up (a 2-player
    # "squad" once ranked AS Roma #1 over Real Madrid), so it falls to the talent
    # block instead of polluting the top of the table.
    MIN_PLAYERS = 8
    ranked = sorted((s for s in idx.values() if s.n_players >= MIN_PLAYERS),
                    key=lambda s: -s.score_z)
    teams = []
    for i, s in enumerate(ranked, 1):
        teams.append({
            "rank": i, "fifa_rank": fifa.get(s.team_id),
            "team_id": s.team_id, "name": name.get(s.team_id, s.team_id), "zh": zh.get(s.team_id, ""),
            "league": league_of.get(s.team_id), "leagues": leagues_of.get(s.team_id, []),
            "score_z": s.score_z, "mw_rating": s.mw_rating, "ga_per90": s.ga_per90, "n_players": s.n_players,
            "talent_ovr": fc_talent.get(s.team_id),
            "source": "season",
            "top_players": tops.get(s.team_id, []),
        })
    # Talent-only fallback rows (e.g. Bundesliga pre-season): ranked after the
    # season rows, ordered by FC26 talent; no score_z is fabricated.
    seen = {t["team_id"] for t in teams}
    talent_only = sorted(((cid, tal) for cid, tal in fc_talent.items()
                          if cid not in seen and cid in name),
                         key=lambda kv: -kv[1])
    for cid, tal in talent_only:
        teams.append({
            "rank": len(teams) + 1, "fifa_rank": fifa.get(cid),
            "team_id": cid, "name": name.get(cid, cid), "zh": zh.get(cid, ""),
            "league": league_of.get(cid), "leagues": leagues_of.get(cid, []),
            "score_z": None, "mw_rating": None, "ga_per90": None,
            "n_players": (idx[cid].n_players if cid in idx else 0),
            "talent_ovr": tal,
            "source": "fc26_talent",
            # goals/assists omitted (not rendered) — these rows carry FC26 overall
            "top_players": [{"name": p["name"], "ovr": p["overall"], "position": p["position"]}
                            for p in fc_by_club.get(cid, [])[:4]],
        })
    # `rank`/`global_rank` 是跨 12 项赛事的参考号,不是"第 N 强":同一张榜里混着
    # 已踢 3 轮的联赛和一场没踢、走 fc26_talent 兜底的联赛(德甲 8/28 才开赛),
    # 全局第 194 只说明它排在 193 支样本更成熟的球队后面。真正可读的是 league_rank。
    # Within a competition the two blocks must be ranked on ONE comparable axis.
    # score_z exists only for season rows and talent_ovr only for FC26-covered
    # clubs, so rank on the FC26 talent where both have it and fall back to the
    # season z — a club with neither sinks to the bottom of its own league rather
    # than inheriting a position from the global two-block ordering.
    def _quality(r):
        if r.get("talent_ovr") is not None:
            return r["talent_ovr"]
        if r.get("score_z") is not None:
            return 60.0 + r["score_z"]          # season-only rows land mid-table
        return None
    n_by_league = attach_league_rank(teams, sort_key=_quality)
    # score_z 用全池 mu/sd(model/squad_strength.squad_index),所以再给一份把
    # 同一个 mw_rating 在本赛事内重新 z 化的 league_z;fc26_talent 兜底行没有
    # mw_rating,league_z 保持 None(不编造)。
    lz = league_zscores([(t["team_id"], t["league"], t["mw_rating"]) for t in teams])
    for t in teams:
        t["global_rank"] = t["rank"]
        t["league_z"] = lz.get(t["team_id"])
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_teams": len(teams),
        "n_by_league": n_by_league,
        "teams": teams,
        "note_key": "notes.squad",
        "note": ("Squad quality: minutes-weighted club rating, now also LEAGUE-STRENGTH "
                 "weighted (a 7.2 in a weaker league counts less than a 7.2 in a top-5 "
                 "league), plus goals/assists per 90. It IS blended into the live model at a "
                 "small weight (squad_blend_weight=0.15) — impact on the settled-match Brier is "
                 "≈neutral. A heuristic quality ranking, not a validated predictive edge."),
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "squad.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"squad.json: {doc['n_teams']} teams across {len(doc['n_by_league'])} competitions")
    for t in doc["teams"][:10]:
        tp = ", ".join(f"{p['name']}({p['goals']}g)" if p.get("goals") is not None
                       else f"{p['name']}(ovr {p.get('ovr')})" for p in t["top_players"][:2])
        _z = f"z={t['score_z']:+.2f}  rating={t['mw_rating']:.2f}  ga/90={t['ga_per90']:.2f}" \
            if t["score_z"] is not None else f"talent ovr={t.get('talent_ovr')}"
        _lr = f"{t['league']}#{t['league_rank']}/{t['league_n']}" if t.get("league_rank") else "—"
        print(f"  #{t['rank']:<3} {_lr:<18} {t['name']:<22} {_z}  | {tp}")


if __name__ == "__main__":
    main()
