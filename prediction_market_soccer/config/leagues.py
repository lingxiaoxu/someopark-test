"""League Registry — the single new configuration hub of prediction_market_soccer.

TRANSFORM_PLAN.md §3.0/§3.1: every competition the module trades is ONE entry here.
Everything else (ingest loops, stage/caps dispatch, venue series, season sims,
frontend visibility) reads this registry — adding a league = adding one entry +
one alias JSON, zero code.

Stage taxonomy (§3.0): every fixture belongs to exactly one stage, decided by
``stage_of(comp_key, round_name)``; market capabilities come from ``caps_for``.
Backend logic and the frontend both consume ``caps`` — never the competition
name, never a round-name substring guess (the WC module's ``"group" not in
round`` bug class, C1).

NOTE on two-leg legs: API-Football gives BOTH legs of a UEFA/CONMEBOL tie the
same round name ("Round of 16"), so leg 1 vs leg 2 cannot come from the round
string — it is resolved from the tie pairing (ingest/store ``tie`` table) and
passed to ``caps_for(..., leg=)``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Stage(str, Enum):
    LEAGUE = "league"              # league round OR European league-phase (single 3-way, draw terminal)
    CUP_TWO_LEG = "cup_two_leg"    # one leg of a two-legged tie (leg resolved from tie pairing)
    CUP_SINGLE = "cup_single"      # single-match knockout (incl. finals, ARG playoffs)
    UNKNOWN = "unknown"            # round name matched no rule — must be surfaced, never guessed


@dataclass(frozen=True)
class StageCaps:
    """What markets/model paths exist for a fixture (frontend mirrors this 1:1)."""
    advance: bool           # 2-way advance market exists (tie- or match-level)
    two_leg: bool           # part of a two-legged tie (agg state applies)
    leg: int | None         # 1 | 2 when two_leg and resolvable, else None
    et_then_pens: bool      # deciding leg/match: ET before pens (UEFA) vs straight pens (CONMEBOL, ARG playoffs)
    neutral: bool           # neutral venue (finals)
    ko_draw_semantics: bool # knockout_late_draw tactic direction: True = KO version


@dataclass(frozen=True)
class Competition:
    key: str                       # canonical id used across DB/JSON/frontend
    name: str
    zh: str
    api_football_id: int
    season: int                    # API-Football season (Europe 2026 = 2026-27; SA calendar 2026)
    kind: str                      # "league" | "league_playoffs" | "swiss_ucl" | "cup_two_leg"
    enabled: bool                  # 12 launch comps true; extension slots false (enable = full standard)
    et_in_ties: bool               # deciding leg of a tie: ET before pens? (UEFA True; CONMEBOL False)
    final_neutral_et: bool         # single-match final: neutral venue with ET->pens
    n_teams: int
    tiebreak: str                  # "pts_gd_gf" | "pts_h2h_gd" | "pts_gd_gf_h2h" | "" (cups)
    home_adv: float                # per-league log-lambda home advantage (bootstrap-fitted; 0.0 until fitted)
    kalshi: dict = field(default_factory=dict)      # market-family -> series ticker
    # ── Polymarket slug vocabulary (all three LIVE-MEASURED 2026-08-26, see below) ──
    # The two venues run SEPARATE prefix vocabularies for the same competition
    # (ligue1 = "fl1" on Global but "lg1" on US; uecl = "col" vs "uecl"; argentina =
    # "arg" vs "lpa"), so one shared field would silently mis-key half the venues.
    poly_slug_prefix: str | None = None             # Poly Global per-match slug: <pfx>-<h>-<a>-<YYYY-MM-DD>
    pmus_slug_prefix: str | None = None             # Poly US per-match slug + its `teams[].league` value
    # Gamma tags carrying this competition's season/qualify events. Those slugs end in a
    # random numeric suffix (uefa-champions-league-2027-champion-20260701202025549), so
    # they can only be resolved by tag+title at runtime — never hardcoded. It is a TUPLE
    # because Gamma splits one competition across tags: EPL's champion event sits under
    # "EPL" while its Euro-spot events sit under "premier-league", and Serie A's matches
    # are tagged "sea" while its champion event is tagged "serie-a".
    poly_tag_slugs: tuple[str, ...] = ()
    season_year_suffix: str = "-27"                 # Kalshi season-event suffix (SA comps "-26")
    stage_rules: tuple = ()        # ((compiled_regex, Stage), ...) checked in order
    tier_note: str = ""
    # season-market slots (league kinds): direct relegation spots, relegation-playoff
    # spots (half-weighted in p_relegation), and the "top-N" European cut the venue
    # trades (KXEPLTOP4 etc.). swiss_ucl uses qual_direct/qual_playoff instead.
    releg_direct: int = 0
    releg_playoff: int = 0
    top_n: int = 4
    qual_direct: int = 8           # swiss league phase: top-8 straight to R16
    qual_playoff: int = 24        # ranks 9..24 into the KO play-off


def _rules(*pairs: tuple[str, Stage]) -> tuple:
    return tuple((re.compile(rx, re.IGNORECASE), st) for rx, st in pairs)


# Round-name rule sets (validated against live API-Football round names during
# Phase 1 first ingest — G1 requires zero UNKNOWN across all stored fixtures).
_LEAGUE_ONLY = _rules((r"^Regular Season\b", Stage.LEAGUE))

# UEFA UCL/UEL/UECL: qualifying (two-leg) -> league phase (single) -> KO (two-leg) -> final (single).
_UEFA_RULES = _rules(
    (r"League (Stage|Phase)\b", Stage.LEAGUE),
    (r"Qualifying|Preliminary|Play-?offs?", Stage.CUP_TWO_LEG),
    (r"Knockout Round Play-?offs?", Stage.CUP_TWO_LEG),
    (r"Round of 16|Quarter-?finals?|Semi-?finals?", Stage.CUP_TWO_LEG),
    (r"^Final$", Stage.CUP_SINGLE),
)

# CONMEBOL Libertadores/Sudamericana — live round names (D1 ingest 2026-08-26):
# "Qualification Round 1..3" (two-leg), "Group Stage - N" (single, league-shaped),
# "Round of 32" (Sud: 16 fixtures = 8 ties × 2 legs), "Round of 16"/"Quarter-finals"/
# "Semi-finals" (two-leg), "Final" (single neutral).
_CONMEBOL_RULES = _rules(
    (r"Group Stage", Stage.LEAGUE),
    (r"Qualification Round|Knockout Round Play-?offs?|First Stage|Second Stage|Third Stage", Stage.CUP_TWO_LEG),
    (r"Round of 32|Round of 16|Quarter-?finals?|Semi-?finals?", Stage.CUP_TWO_LEG),
    (r"^Final$", Stage.CUP_SINGLE),
)

# Argentina LPF (league_playoffs) — live round names (D1-7 executed on real data
# 2026-08-26): zone rounds are "Apertura - N"/"Clausura - N"; playoff rounds carry
# the SAME tournament prefix ("Apertura - Round of 16" … "Apertura - Final"), so
# KNOCKOUT PATTERNS MUST COME FIRST (rules are checked in order — the naive
# "Apertura → league" rule would swallow the playoffs; caught by the UNKNOWN gate).
# Playoffs are single-match, straight pens (R11 conservative default).
_ARG_RULES = _rules(
    (r"Round of 32|Round of 16|Quarter-?finals?|Semi-?finals?|Final|8th Finals|4th Finals", Stage.CUP_SINGLE),
    (r"(Apertura|Clausura)\s*-\s*\d+", Stage.LEAGUE),
    (r"1st Phase|2nd Phase|Group|Regular Season", Stage.LEAGUE),
)

# Brasileirão: pure double round-robin.
_BRA_RULES = _LEAGUE_ONLY

# The rest of South America (extension slots). Same shape as Argentina — Apertura /
# Clausura zone rounds plus a knockout post-season — with the two extra round families
# the live API actually publishes for those countries (measured 2026-08-26 via
# /fixtures/rounds): Uruguay's "Torneo Intermedio - N" mid-season mini-tournament and
# Venezuela's "Apertura - Quadrangular - N" group phase. Both are LEAGUE-shaped
# (mini-tables, draws terminal) and both would otherwise fall through to UNKNOWN and
# trip the G1 gate — note "Torneo Intermedio - Final" still lands on CUP_SINGLE because
# _ARG_RULES' knockout patterns are checked first, which is exactly right.
_LATAM_RULES = _ARG_RULES + _rules(
    (r"Torneo Intermedio\s*-\s*\d+", Stage.LEAGUE),
    (r"Quadrangular|Hexagonal|Cuadrangular", Stage.LEAGUE),
)

# Domestic cups (extension slots). Round names measured 2026-08-26 across FA Cup /
# EFL Cup / Copa del Rey / Coppa Italia / DFB Pokal / Coupe de France: they share one
# vocabulary — qualifying and preliminary rounds (plus England's "… Replays"), then
# "Round of 128…16" or the French/Spanish "1/128-finals", then QF/SF/Final.
# Everything is a single match; the two cups that still play their semi-finals over
# two legs (Copa del Rey, EFL Cup) get the same set with the semi-final overridden,
# which is why the override has to sit FIRST (rules are checked in order).
_CUP_SINGLE_RULES = _rules(
    (r"Round of \d+|1/\d+\s*-?\s*finals?|Quarter-?finals?|Semi-?finals?|"
     r"Round Qualifying|Preliminary Round|Replays?|^Final$", Stage.CUP_SINGLE),
)
_CUP_TWO_LEG_SEMI_RULES = _rules((r"Semi-?finals?", Stage.CUP_TWO_LEG)) + _CUP_SINGLE_RULES


def _kalshi(prefix: str, **extra: str) -> dict:
    """Standard Kalshi family map from a series prefix + explicit extras/overrides."""
    base = {
        "game": f"{prefix}GAME",
        "total": f"{prefix}TOTAL",
        "btts": f"{prefix}BTTS",
        "spread": f"{prefix}SPREAD",
        "score": f"{prefix}SCORE",
        "teamtotal": f"{prefix}TEAMTOTAL",
        "corners": f"{prefix}CORNERS",
    }
    base.update(extra)
    return base


REGISTRY: dict[str, Competition] = {
    # ── A 组:五大联赛 + 欧冠 ────────────────────────────────────────────────
    "epl": Competition(
        key="epl", name="Premier League", zh="英超", api_football_id=39, season=2026,
        kind="league", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=20, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXEPL", champion="KXPREMIERLEAGUE", top4="KXEPLTOP4",
                       relegation="KXEPLRELEGATION", last="KXEPLLAST"),
        poly_slug_prefix="epl", pmus_slug_prefix="epl", poly_tag_slugs=("EPL", "premier-league"),
        releg_direct=3, top_n=4, stage_rules=_LEAGUE_ONLY,
    ),
    "laliga": Competition(
        key="laliga", name="La Liga", zh="西甲", api_football_id=140, season=2026,
        kind="league", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=20, tiebreak="pts_h2h_gd", home_adv=0.0,
        kalshi=_kalshi("KXLALIGA", champion="KXLALIGA", top4="KXLALIGATOP4",
                       relegation="KXLALIGARELEGATION", last="KXLALIGALAST"),
        poly_slug_prefix="lal", pmus_slug_prefix="lal", poly_tag_slugs=("la-liga",),
        releg_direct=3, top_n=4, stage_rules=_LEAGUE_ONLY,
    ),
    "seriea": Competition(
        key="seriea", name="Serie A", zh="意甲", api_football_id=135, season=2026,
        kind="league", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=20, tiebreak="pts_h2h_gd", home_adv=0.0,
        kalshi=_kalshi("KXSERIEA", champion="KXSERIEA", top4="KXSERIEATOP4",
                       relegation="KXSERIEARELEGATION", last="KXSERIEALAST"),
        poly_slug_prefix="sea", pmus_slug_prefix="sea", poly_tag_slugs=("serie-a", "sea"),
        releg_direct=3, top_n=4, stage_rules=_LEAGUE_ONLY,
    ),
    "bundesliga": Competition(
        key="bundesliga", name="Bundesliga", zh="德甲", api_football_id=78, season=2026,
        kind="league", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_gd_gf_h2h", home_adv=0.0,
        kalshi=_kalshi("KXBUNDESLIGA", champion="KXBUNDESLIGA", top4="KXBUNDESLIGATOP4",
                       relegation="KXBUNDESLIGARELEGATION", last="KXBUNDESLIGALAST"),
        poly_slug_prefix="bun", pmus_slug_prefix="bun", poly_tag_slugs=("bundesliga",),
        releg_direct=2, releg_playoff=1, top_n=4, stage_rules=_LEAGUE_ONLY,
    ),
    "ligue1": Competition(
        key="ligue1", name="Ligue 1", zh="法甲", api_football_id=61, season=2026,
        kind="league", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXLIGUE1", champion="KXLIGUE1", top4="KXLIGUE1TOP4",
                       relegation="KXLIGUE1RELEGATION", last="KXLIGUE1LAST"),
        # "fl1" (French Ligue 1) on Global but "lg1" on US — the clearest case of the
        # two venues NOT sharing a vocabulary.
        poly_slug_prefix="fl1", pmus_slug_prefix="lg1", poly_tag_slugs=("ligue-1",),
        releg_direct=2, releg_playoff=1, top_n=4, stage_rules=_LEAGUE_ONLY,
    ),
    "ucl": Competition(
        key="ucl", name="UEFA Champions League", zh="欧冠", api_football_id=2, season=2026,
        kind="swiss_ucl", enabled=True, et_in_ties=True, final_neutral_et=True,
        n_teams=36, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXUCL", champion="KXUCL", top8="KXUCLTOP8", ro16="KXUCLRO16",
                       ro8="KXUCLRO8", ro4="KXUCLRO4", finalist="KXUCLFINALIST",
                       advance="KXUCLADVANCE", topscorer="KXUEFACLTOPGOAL"),
        poly_slug_prefix="ucl", pmus_slug_prefix="ucl", poly_tag_slugs=("ucl",),
        stage_rules=_UEFA_RULES,
    ),
    # ── B 组:欧联/欧协联/南美(同一标准) ─────────────────────────────────────
    "uel": Competition(
        key="uel", name="UEFA Europa League", zh="欧联", api_football_id=3, season=2026,
        kind="swiss_ucl", enabled=True, et_in_ties=True, final_neutral_et=True,
        n_teams=36, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXUEL", champion="KXUEL", advance="KXUELADVANCE"),
        poly_slug_prefix="uel", pmus_slug_prefix="uel", poly_tag_slugs=("uel",),
        stage_rules=_UEFA_RULES,
    ),
    "uecl": Competition(
        key="uecl", name="UEFA Conference League", zh="欧协联", api_football_id=848, season=2026,
        kind="swiss_ucl", enabled=True, et_in_ties=True, final_neutral_et=True,
        n_teams=36, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXUECL", champion="KXUECL", advance="KXUECLADVANCE"),
        # Global calls the Conference League "col" (col-rap-hmi-2026-08-26, tag
        # europa-conference-league) — NOT "uecl", and NOT the Colombian "col1"/"col2".
        poly_slug_prefix="col", pmus_slug_prefix="uecl",
        poly_tag_slugs=("europa-conference-league",), stage_rules=_UEFA_RULES,
    ),
    "libertadores": Competition(
        key="libertadores", name="Copa Libertadores", zh="解放者杯", api_football_id=13, season=2026,
        kind="cup_two_leg", enabled=True, et_in_ties=False, final_neutral_et=True,
        n_teams=16, tiebreak="", home_adv=0.0,
        kalshi=_kalshi("KXCONMEBOLLIB", champion="KXCONMEBOLLIB", advance="KXCONMEBOLLIBADVANCE"),
        poly_slug_prefix="lib", pmus_slug_prefix="lib", poly_tag_slugs=("lib",),
        season_year_suffix="-26", stage_rules=_CONMEBOL_RULES,
    ),
    "sudamericana": Competition(
        key="sudamericana", name="Copa Sudamericana", zh="南美杯", api_football_id=11, season=2026,
        kind="cup_two_leg", enabled=True, et_in_ties=False, final_neutral_et=True,
        n_teams=16, tiebreak="", home_adv=0.0,
        kalshi=_kalshi("KXCONMEBOLSUD", champion="KXCONMEBOLSUD", advance="KXCONMEBOLSUDADVANCE"),
        poly_slug_prefix="sud", pmus_slug_prefix="sud", poly_tag_slugs=("sud",),
        season_year_suffix="-26", stage_rules=_CONMEBOL_RULES,
    ),
    "brasileirao": Competition(
        key="brasileirao", name="Brasileirão Série A", zh="巴甲", api_football_id=71, season=2026,
        kind="league", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=20, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXBRASILEIRO", champion="KXBRASILEIRO", top="KXBRASILEIROTOP",
                       relegation="KXBRASILEIRORELEGATION"),
        # "bra" is Série A only — "bra2"/"bra3" (Série B/C) and "brco" (Copa do Brasil)
        # are separate Poly prefixes and must not be swept in.
        poly_slug_prefix="bra", pmus_slug_prefix="bra", poly_tag_slugs=("brazil-serie-a",),
        season_year_suffix="-26", releg_direct=4, top_n=4, stage_rules=_BRA_RULES,
    ),
    "argentina": Competition(
        key="argentina", name="Liga Profesional Argentina", zh="阿甲", api_football_id=128, season=2026,
        kind="league_playoffs", enabled=True, et_in_ties=False, final_neutral_et=False,
        n_teams=30, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXARGPREMDIV", game="KXARGPREMDIVGAME", advance="KXARGPREMDIVADVANCE"),
        # Global "arg" / US "lpa" = the top flight; "argpn"/"arg2" are Primera Nacional.
        poly_slug_prefix="arg", pmus_slug_prefix="lpa", poly_tag_slugs=("arg",),
        season_year_suffix="-26", releg_direct=0, top_n=8, stage_rules=_ARG_RULES,
    ),
    # ── 扩展位(enabled=False;启用即全标准) ─────────────────────────────────
    # §1.2 的 17+ 项扩展清单,全部落位。每条的 api_football_id / n_teams / 升降级与欧战
    # 名额都是**实测**的,不是记忆:league id 来自 /leagues 全量拉取(1241 项,2026-08-26),
    # 队数与名额来自该赛事 /standings 的 description 字段(权威——"Relegation - Liga
    # Portugal 2"/"Promotion - Champions League (Qualification)" 是 API 自己标的),
    # 停赛期没有 description 的赛事回看上一季终表。round 名来自 /fixtures/rounds。
    #
    # 两处**故意留空**,留空比猜错便宜:
    #   * poly_slug_prefix / pmus_slug_prefix / poly_tag_slugs —— Polymarket 的两套
    #     词表只能实测(reader 的 _base_re 用 include_disabled 把扩展位也编进正则,
    #     猜一个前缀 = 把 col1/col2、bra2/brco 这类近邻错扫进来)。
    #   * kalshi —— 只有计划 §1.2 逐条验过 GAME 系列的六个欧洲联赛写了 _kalshi(),
    #     其余家族名是 Kalshi 的通用构词法、启用前须 discovery 跑一遍确认;南美其余与
    #     国内杯计划没验过任何系列,所以整个 kalshi 留空而不是编一个。
    # 翻 enabled=True 之前要补的只有这两样 + 一份 clubs_<key>.json / aliases_<key>.json。

    # —— 欧洲联赛(Kalshi GAME 系列见计划 §1.2)——
    "portugal": Competition(
        key="portugal", name="Liga Portugal", zh="葡超", api_football_id=94, season=2026,
        kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_h2h_gd", home_adv=0.0,
        kalshi=_kalshi("KXLIGAPORTUGAL", champion="KXLIGAPORTUGAL"),
        poly_slug_prefix="por", pmus_slug_prefix="ligpor", poly_tag_slugs=("primeira-liga",),
        # 17/18 降级,16 打附加赛;冠军+亚军进欧冠联赛阶段,季军打欧冠资格赛 → top_n=3。
        releg_direct=2, releg_playoff=1, top_n=3,
        stage_rules=_LEAGUE_ONLY, tier_note="extension slot",
    ),
    "eredivisie": Competition(
        key="eredivisie", name="Eredivisie", zh="荷甲", api_football_id=88, season=2026,
        kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXEREDIVISIE", champion="KXEREDIVISIE"),
        # Deliberately left unresolved: Global carries both "ere" and "ned2" and the
        # sampled fixtures did not separate top flight from Eerste Divisie, so filling
        # a guess here would key the venue readers onto the wrong division. Measure
        # before enabling this slot.
        releg_direct=2, releg_playoff=1, top_n=2,
        stage_rules=_LEAGUE_ONLY, tier_note="extension slot",
    ),
    "turkey": Competition(
        key="turkey", name="Süper Lig", zh="土超", api_football_id=203, season=2026,
        kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_h2h_gd", home_adv=0.0,
        kalshi=_kalshi("KXSUPERLIG", champion="KXSUPERLIG"),
        # 2025-26 终表:16/17/18 降级(3 个),无附加赛;冠军进欧冠联赛阶段、亚军打资格赛。
        releg_direct=3, top_n=2,
        stage_rules=_LEAGUE_ONLY, tier_note="extension slot",
    ),
    "scotland": Competition(
        key="scotland", name="Scottish Premiership", zh="苏超", api_football_id=179, season=2026,
        kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=12, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXSCOTTISHPREM", champion="KXSCOTTISHPREM"),
        # 12 队打到第 33 轮后拆分上下半区(API 仍叫 "Regular Season - N",所以 stage 规则
        # 不用动);末位直降、倒数第二打附加赛。**拆分后 standings 会变成两个 group**,
        # 赛季模拟的单表口径届时要按 zone 复核 —— 启用前必须验这一条。
        releg_direct=1, releg_playoff=1, top_n=2,
        stage_rules=_LEAGUE_ONLY, tier_note="extension slot (post-split zoned table unverified)",
    ),
    "belgium": Competition(
        key="belgium", name="Belgian Pro League", zh="比甲", api_football_id=144, season=2026,
        kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXBELGIANPL", champion="KXBELGIANPL"),
        # 2026-27 是 18 队 34 轮平表(2025-26 还是 16 队 + 季后赛分区),17/18 直降。
        # 官方并列判定第二顺位是**胜场数**,本模块的 tiebreak 词表没有这一档,
        # 用 pts_gd_gf 近似 —— 启用前要么扩词表要么在 MODEL_NOTES 披露。
        releg_direct=2, top_n=2,
        stage_rules=_LEAGUE_ONLY, tier_note="extension slot (tiebreak approximates the wins criterion)",
    ),
    "efl_championship": Competition(
        key="efl_championship", name="EFL Championship", zh="英冠", api_football_id=40, season=2026,
        kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=24, tiebreak="pts_gd_gf", home_adv=0.0,
        kalshi=_kalshi("KXEFLCHAMPIONSHIP", champion="KXEFLCHAMPIONSHIP"),
        # 22/23/24 降级;没有欧战,所以 top_n 装的是这里唯一有意义的区间切口:
        # 前 2 直升 + 3-6 打升级附加赛 = 前 6。
        releg_direct=3, top_n=6,
        stage_rules=_LEAGUE_ONLY, tier_note="extension slot (top_n = promotion play-off cut, not a European cut)",
    ),

    # —— 南美其余(日历年赛季,故 season_year_suffix="-26")——
    # 这一组多数是 Apertura/Clausura + 季后赛,和阿甲同形,所以 kind=league_playoffs。
    # **降级普遍不看本赛季单表**(乌拉圭看 promedios 多季平均、哥伦比亚看三年平均、
    # 委内瑞拉/秘鲁看年度合并表),赛季模拟排的不是那张表,所以按阿甲的既有先例把
    # releg_direct 留 0 —— 宁可不出降级市场,也不出一个排错表算出来的降级概率。
    "uruguay": Competition(
        key="uruguay", name="Primera División Uruguaya", zh="乌拉圭甲", api_football_id=268,
        season=2026, kind="league_playoffs", enabled=False, et_in_ties=False,
        final_neutral_et=False, n_teams=16, tiebreak="pts_gd_gf", home_adv=0.0,
        # Tabla Anual 前 2 进解放者杯小组赛、3-4 打资格赛 → top_n=4。降级看 Promedios。
        releg_direct=0, top_n=4, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot (relegation runs off the promedios table)",
    ),
    "chile": Competition(
        key="chile", name="Primera División de Chile", zh="智利甲", api_football_id=265,
        season=2026, kind="league", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=16, tiebreak="pts_gd_gf", home_adv=0.0,
        # 单表 30 轮(2026 轮名就是 "Regular Season - N");2025 终表 15/16 降级,
        # 前 2 进解放者杯小组赛、第 3 打资格赛。
        releg_direct=2, top_n=3, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot",
    ),
    "peru": Competition(
        key="peru", name="Liga 1 Perú", zh="秘鲁甲", api_football_id=281, season=2026,
        kind="league_playoffs", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=18, tiebreak="pts_gd_gf", home_adv=0.0,
        releg_direct=0, top_n=4, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot (relegation runs off the Tabla Anual)",
    ),
    "ecuador": Competition(
        key="ecuador", name="LigaPro Ecuador", zh="厄瓜多尔甲", api_football_id=242, season=2026,
        kind="league_playoffs", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=16, tiebreak="pts_gd_gf", home_adv=0.0,
        # 第一阶段前 6 进冠军组(top_n=6),后 6 进保级组,降级在保级组里决出。
        releg_direct=0, top_n=6, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot (relegation decided in the second-stage group)",
    ),
    "colombia": Competition(
        key="colombia", name="Categoría Primera A", zh="哥伦比亚甲", api_football_id=239,
        season=2026, kind="league_playoffs", enabled=False, et_in_ties=False,
        final_neutral_et=False, n_teams=20, tiebreak="pts_gd_gf", home_adv=0.0,
        # 每个半程 20 队 19 轮,前 8 进季后赛("Post season qualification")→ top_n=8。
        releg_direct=0, top_n=8, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot (relegation runs off a three-year average)",
    ),
    "bolivia": Competition(
        key="bolivia", name="División Profesional", zh="玻利维亚甲", api_football_id=344,
        season=2026, kind="league_playoffs", enabled=False, et_in_ties=False,
        final_neutral_et=False, n_teams=16, tiebreak="pts_gd_gf", home_adv=0.0,
        # 名额取自 2025 终表(末位直降、倒数第二打附加赛;前 2 进解放者杯小组赛、
        # 第 3 打资格赛)。2026 改成了 Apertura 制,启用前重验这三个数。
        releg_direct=1, releg_playoff=1, top_n=3, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot (slots read off the 2025 final table; 2026 moved to Apertura)",
    ),
    "venezuela": Competition(
        key="venezuela", name="Liga FUTVE", zh="委内瑞拉甲", api_football_id=299, season=2026,
        kind="league_playoffs", enabled=False, et_in_ties=False, final_neutral_et=False,
        n_teams=14, tiebreak="pts_gd_gf", home_adv=0.0,
        # 14 队,半程前 8 进 Quadrangular(两组 4 队的小联赛,故 _LATAM_RULES 把它判成
        # LEAGUE 而不是淘汰赛)→ top_n=8。
        releg_direct=0, top_n=8, season_year_suffix="-26",
        stage_rules=_LATAM_RULES, tier_note="extension slot (relegation runs off the annual aggregate table)",
    ),

    # —— 五大联赛国内杯 ——
    # kind="cup_two_leg" 在本模块里读作"淘汰赛制赛事"(run_model 据此走签表而不是赛季
    # 模拟);单场/两回合是 stage_rules 的事,不是 kind 的事。n_teams 取 API 公布的最大
    # 一轮("Round of N"),它同时也是 club_prior 那条 len(teams) >= n_teams//2 的门槛。
    # 决赛都在中立球门、加时后点球,故 final_neutral_et=True;et_in_ties 只对**两回合
    # 决胜回合**生效,所以只有半决赛踢两回合的国王杯/联赛杯为 True。
    "fa_cup": Competition(
        key="fa_cup", name="FA Cup", zh="足总杯", api_football_id=45, season=2026,
        kind="cup_two_leg", enabled=False, et_in_ties=False, final_neutral_et=True,
        n_teams=128, tiebreak="", home_adv=0.0,
        stage_rules=_CUP_SINGLE_RULES,
        # 2026-27 赛季 API 尚未开(最新一季 2025);启用时先确认 season 已发布。
        tier_note="extension slot (single match throughout; API season 2026 not published yet)",
    ),
    "efl_cup": Competition(
        key="efl_cup", name="EFL Cup", zh="英格兰联赛杯", api_football_id=48, season=2026,
        kind="cup_two_leg", enabled=False, et_in_ties=True, final_neutral_et=True,
        n_teams=128, tiebreak="", home_adv=0.0,
        stage_rules=_CUP_TWO_LEG_SEMI_RULES, tier_note="extension slot (two-legged semi-finals)",
    ),
    "copa_del_rey": Competition(
        key="copa_del_rey", name="Copa del Rey", zh="国王杯", api_football_id=143, season=2026,
        kind="cup_two_leg", enabled=False, et_in_ties=True, final_neutral_et=True,
        n_teams=128, tiebreak="", home_adv=0.0,
        stage_rules=_CUP_TWO_LEG_SEMI_RULES,
        tier_note="extension slot (two-legged semi-finals; API season 2026 not published yet)",
    ),
    "coppa_italia": Competition(
        key="coppa_italia", name="Coppa Italia", zh="意大利杯", api_football_id=137, season=2026,
        kind="cup_two_leg", enabled=False, et_in_ties=False, final_neutral_et=True,
        n_teams=128, tiebreak="", home_adv=0.0,
        stage_rules=_CUP_SINGLE_RULES,
        tier_note="extension slot (single match throughout since 2024-25)",
    ),
    "dfb_pokal": Competition(
        key="dfb_pokal", name="DFB-Pokal", zh="德国杯", api_football_id=81, season=2026,
        kind="cup_two_leg", enabled=False, et_in_ties=False, final_neutral_et=True,
        n_teams=64, tiebreak="", home_adv=0.0,
        stage_rules=_CUP_SINGLE_RULES, tier_note="extension slot (single match throughout)",
    ),
    "coupe_de_france": Competition(
        key="coupe_de_france", name="Coupe de France", zh="法国杯", api_football_id=66, season=2026,
        kind="cup_two_leg", enabled=False, et_in_ties=False, final_neutral_et=True,
        n_teams=128, tiebreak="", home_adv=0.0,
        stage_rules=_CUP_SINGLE_RULES,
        tier_note="extension slot (single match throughout; API season 2026 not published yet)",
    ),
}


def active(include_disabled: bool = False) -> list[Competition]:
    """All enabled competitions — the single standard, no tiers."""
    return [c for c in REGISTRY.values() if c.enabled or include_disabled]


_FITTED_CACHE: dict | None = None


def fitted_params(comp_key: str) -> dict:
    """Per-competition fitted parameters (base_mu / home_adv), from
    data/priors/league_params.json (ops/fit_league_params writes it from
    last-season results). Registry/ModelConfig defaults apply when absent —
    so the module runs before the fit, just with the global constants."""
    global _FITTED_CACHE
    if _FITTED_CACHE is None:
        import json
        from prediction_market_soccer.config import CONFIG
        p = CONFIG.paths.priors / "league_params.json"
        try:
            _FITTED_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            _FITTED_CACHE = {}
    out = dict(_FITTED_CACHE.get(comp_key) or {})
    comp = REGISTRY.get(comp_key)
    if comp and comp.home_adv and "home_adv" not in out:
        out["home_adv"] = comp.home_adv
    return out


def get(comp_key: str) -> Competition:
    return REGISTRY[comp_key]


def by_api_id(league_id: int) -> Competition | None:
    for c in REGISTRY.values():
        if c.api_football_id == league_id:
            return c
    return None


def stage_of(comp_key: str, round_name: str | None) -> Stage:
    """C1's single source of truth: round name -> coarse stage, via registry rules.

    Returns Stage.UNKNOWN when no rule matches — callers must surface that
    (G1 gate: zero UNKNOWN across all stored fixtures), never guess.
    """
    comp = REGISTRY.get(comp_key)
    if comp is None or not round_name:
        return Stage.UNKNOWN
    for rx, st in comp.stage_rules:
        if rx.search(round_name):
            return st
    return Stage.UNKNOWN


def caps_for(comp_key: str, round_name: str | None, *, leg: int | None = None,
             is_final: bool | None = None) -> StageCaps:
    """Market-capability record for one fixture (§3.0 matrix).

    ``leg`` comes from the tie layer (1/2) for two-legged stages; ``is_final``
    may be forced by callers that already know (defaults to round-name check).
    """
    comp = REGISTRY.get(comp_key)
    st = stage_of(comp_key, round_name)
    if is_final is None:
        is_final = bool(round_name) and bool(re.match(r"^final$", round_name.strip(), re.I))
    if st == Stage.LEAGUE:
        return StageCaps(advance=False, two_leg=False, leg=None, et_then_pens=False,
                         neutral=False, ko_draw_semantics=False)
    if st == Stage.CUP_TWO_LEG:
        deciding = (leg == 2)
        return StageCaps(advance=True, two_leg=True, leg=leg,
                         et_then_pens=(comp.et_in_ties if comp else False) and deciding,
                         neutral=False, ko_draw_semantics=True)
    if st == Stage.CUP_SINGLE:
        return StageCaps(advance=True, two_leg=False, leg=None,
                         et_then_pens=(comp.final_neutral_et if (comp and is_final) else False),
                         neutral=bool(is_final and comp and comp.final_neutral_et),
                         ko_draw_semantics=True)
    # UNKNOWN: safest is league-shaped (no advance path fires), but flag loudly.
    return StageCaps(advance=False, two_leg=False, leg=None, et_then_pens=False,
                     neutral=False, ko_draw_semantics=False)


def caps_dict(caps: StageCaps, stage: Stage, agg: str | None = None) -> dict:
    """JSON-ready caps payload attached to every exported fixture (frontend contract)."""
    return {
        "stage": stage.value,
        "advance": caps.advance,
        "two_leg": caps.two_leg,
        "leg": caps.leg,
        "agg": agg,
        "et_then_pens": caps.et_then_pens,
        "neutral": caps.neutral,
    }


if __name__ == "__main__":
    print(f"registry: {len(REGISTRY)} comps, {len(active())} enabled")
    for c in active():
        print(f"  {c.key:14s} api={c.api_football_id:<4d} kind={c.kind:16s} "
              f"game={c.kalshi.get('game','-'):22s} champ={c.kalshi.get('champion','-')}")
    # Stage dispatch smoke test. Every round name below is a LIVE one (API-Football
    # /fixtures/rounds, 2026-08-26) — the extension slots are in here too, because the
    # point of a pre-filled slot is that flipping `enabled` cannot surface an UNKNOWN.
    cases = [("epl", "Regular Season - 3"), ("ucl", "League Stage - 1"),
             ("ucl", "3rd Qualifying Round"), ("ucl", "Final"),
             ("libertadores", "Quarter-finals"), ("argentina", "Clausura - 12"),
             ("brasileirao", "Regular Season - 21"),
             ("turkey", "Regular Season - 34"), ("scotland", "Regular Season - 33"),
             ("belgium", "Regular Season - 12"), ("efl_championship", "Regular Season - 46"),
             ("uruguay", "Torneo Intermedio - 7"), ("uruguay", "Torneo Intermedio - Final"),
             ("colombia", "Apertura - Quarter-finals"), ("colombia", "Clausura - 19"),
             ("venezuela", "Apertura - Quadrangular - 3"), ("venezuela", "Apertura - Final"),
             ("peru", "Clausura - 17"), ("bolivia", "Apertura - 22"),
             ("chile", "Regular Season - 30"), ("ecuador", "Regular Season - 30"),
             ("fa_cup", "1st Round Qualifying Replays"), ("fa_cup", "Round of 64"),
             ("fa_cup", "Semi-finals"), ("copa_del_rey", "Semi-finals"),
             ("copa_del_rey", "1/128-finals"), ("efl_cup", "Preliminary Round"),
             ("coppa_italia", "Round of 32"), ("dfb_pokal", "Round of 64"),
             ("coupe_de_france", "Final")]
    for k, r in cases:
        st = stage_of(k, r)
        cp = caps_for(k, r, leg=2 if st == Stage.CUP_TWO_LEG else None)
        print(f"  {k}:{r!r} -> {st.value} advance={cp.advance} et={cp.et_then_pens}")


_ALTDATA_CACHE = None


def altdata_weights(comp_key: str | None) -> dict:
    """Per-competition alt-data λ weights from data/priors/league_altdata.json
    (ops/fit_altdata_weights fits them on each competition's own history).

    Falls back to the ModelConfig globals when a competition has no fitted entry,
    so the module still runs before the fit — just with the shared constants."""
    global _ALTDATA_CACHE
    if _ALTDATA_CACHE is None:
        import json
        from prediction_market_soccer.config import CONFIG
        p = CONFIG.paths.priors / "league_altdata.json"
        try:
            _ALTDATA_CACHE = (json.loads(p.read_text(encoding="utf-8")).get("weights") or {}
                              if p.exists() else {})
        except Exception:
            _ALTDATA_CACHE = {}
    return dict(_ALTDATA_CACHE.get(comp_key) or {}) if comp_key else {}


def neutral_venue_for(comp_key: str | None, round_name: str | None,
                      conn=None, fixture_api_id: int | None = None) -> bool:
    """Is this fixture played at a NEUTRAL venue? (the host_neutral pricing input)

    The one place every caller should ask, so a backtest cannot end up scoring a
    model production does not price. Only a neutral-venue final qualifies — both legs
    of a two-legged tie have a real host, which the inherited World Cup convention
    (``host_neutral=is_knockout(round)``) got wrong for every knockout it saw.
    """
    if not comp_key:
        return False
    leg = None
    if conn is not None and fixture_api_id is not None:
        try:
            from prediction_market_soccer.ingest.soccer_ingest import leg_of
            leg, _ = leg_of(conn, fixture_api_id)
        except Exception:
            leg = None
    try:
        return bool(caps_for(comp_key, round_name, leg=leg).neutral)
    except Exception:
        return False
