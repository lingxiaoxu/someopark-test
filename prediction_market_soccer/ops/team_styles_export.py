"""Team STYLE taxonomy export (P1) → data/output/team_styles.json for the frontend.

A team can play TWO styles (e.g. Spain = possession + high-press), so this is a MATRIX:
every registered club × 10 fixed styles. Each team is assigned 1–2 styles; for each style
it has, the cell carries the team's possession (`poss`) — within a style column teams are
ranked by poss (cells do NOT sum to 100%). Refreshed WEEKLY (descriptive, slow-moving).

Robust data source = a PRIOR auto-generated from each squad's EA-FC26 playStyles tags
(§3.8-d) BLENDED with live API-Football intra-game metrics (possession / passing /
directness / shot profile). The prior covers clubs that haven't played yet and stabilises
small live samples; the live metrics refine it as matches are played. A handful of hand
overrides sit on top (_PRIOR_OVERRIDES) for identities the tag aggregate demonstrably
misreads; FC26 covers 191 of our 399 registered clubs, and a club outside that coverage
is styled from live data alone rather than from a placeholder prior.

DESCRIPTIVE — a scouting aid, not a model input. The output stays a SUPERSET of the old
schema: a `_with_legacy()` adapter keeps the single `style` / `cluster` / `metrics` fields
so every existing downstream reader (agent tool, etc.) works UNCHANGED.

TWO LAYERS (club edition): a style column spans 10-12 competitions, so each cell
carries BOTH `rank`/`global_rank` (position in the column across every club) and
`league_rank`/`league_n` (position among that club's OWN competition in the same
column) — "#41 in balanced" is unreadable until you know whether that is out of
270 clubs or out of the 20 in the Premier League.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops.squad_export import league_membership

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

# ── FC26 playStyles auto-prior (TRANSFORM_PLAN §3.8-d) ───────────────────────
# The WC module hand-curated 48 national teams; clubs get their style PRIOR
# auto-generated from each squad's EA-FC26 playStyles tags (verified columns:
# team/leagueName/playStyles), mapped onto the same 10 style codes. The 0.55/0.45
# prior/live blend, dual-style rule and weekly throttle are unchanged.
_TAG_TO_STYLE = {
    "possession": ("Technical", "First Touch", "Incisive Pass", "Press Proven", "Inventive",
                   "Tiki Taka", "Pinged Pass"),
    "direct": ("Long Ball Pass", "Whipped Pass", "Rapid", "Quick Step", "Trivela"),
    "high_press": ("Relentless", "Intercept", "Anticipate", "Jockey", "Enforcer"),
    "low_block": ("Block", "Slide Tackle", "Bruiser"),
    "clinical": ("Finesse Shot", "Low Driven Shot", "Chip Shot", "Gamechanger"),
    "high_volume": ("Power Shot", "Power Header", "Acrobatic"),
    "set_piece": ("Dead Ball", "Precision Header", "Aerial Fortress", "Aerial"),
}

# EA ships UNLICENSED clubs under invented names and licensed ones under broadcast
# abbreviations, so name normalisation alone loses them. Each entry below was verified
# by reading the squad out of the FC26 frame, not guessed from the string: "Lombardia
# FC" = Lautaro/Sommer/Barella/Bastoni (Inter), "Milano FC" = Rafael Leão (AC Milan),
# "Latium" = Zaccagni/Provedel (Lazio), "Bergamo Calcio" = Éderson (Atalanta), "OM" =
# Pavard/Greenwood/Aubameyang (Marseille), "OL" = Tolisso/Tagliafico (Lyon).
# Measured effect: 129 → 192 of the 399 registered clubs carry an FC26 prior.
_FC_TEAM_ALIASES: dict[str, str] = {
    # England
    "Spurs": "tottenham", "Man Utd": "manchester_united",
    "Newcastle Utd": "newcastle", "Nott'm Forest": "nottingham_forest",
    # Spain
    "Atlético de Madrid": "atletico_madrid", "Celta": "celta_vigo", "D. Alavés": "alaves",
    # Italy (all four unlicensed in FC26)
    "Lombardia FC": "inter", "Milano FC": "ac_milan", "Latium": "lazio",
    "Bergamo Calcio": "atalanta",
    # France
    "Paris SG": "paris_saint_germain", "OM": "marseille", "OL": "lyon",
    "OGC Nice": "nice", "LOSC Lille": "lille", "Havre AC": "le_havre",
    "Stade Rennais FC": "rennes",
    # Germany (registry ids keep the umlaut-stripped spelling)
    "FC Bayern München": "bayern_mnchen", "1. FC Köln": "1_fc_kln",
    "TSG Hoffenheim": "1899_hoffenheim", "1899 Hoffenheim": "1899_hoffenheim",
    # Netherlands (reachable through the UEFA competitions)
    "N.E.C. Nijmegen": "nec_nijmegen",
    # Argentina — EA truncates to the short broadcast name
    "Central Córdoba": "central_cordoba_de_santiago", "Belgrano": "belgrano_cordoba",
    "Ind. Rivadavia": "independ_rivadavia", "Newell's": "newells_old_boys",
    "Sarmiento": "sarmiento_junin", "Defensa": "defensa_y_justicia",
    "Estudiantes": "estudiantes_l_p", "Instituto": "instituto_cordoba",
    "Dep. Riestra": "deportivo_riestra", "Unión": "union_santa_fe",
    "Talleres": "talleres_cordoba",
    # CONMEBOL
    "Atl. Nacional": "atletico_nacional", "IDV": "independiente_del_valle",
    "San Antonio": "san_antonio_bulo_bulo", "U. de Chile": "universidad_de_chile",
    "Dep. Táchira": "deportivo_tachira_fc", "Guaraní": "club_guarani",
    # DELIBERATELY ABSENT: EA's LPF "Gimnasia" is ambiguous (our registry carries both
    # Gimnasia La Plata and Gimnasia Mendoza), and Brazil has no FC26 league at all, so
    # every Brasileirão club is prior-less by construction — live metrics carry them.
}

# FC26 league → the competitions a club of that league may legitimately resolve to.
# Second tiers map to their own top flight because FC26 ships last season's tier and
# our registry is this season's (Málaga and Le Mans are promoted, not mismatches).
# Anything unlisted can only enter our universe through a continental cup.
_CONTINENTAL = frozenset({"ucl", "uel", "uecl", "libertadores", "sudamericana"})
_FC_LEAGUE_COMPS: dict[str, frozenset[str]] = {
    "Premier League": frozenset({"epl"}), "EFL Championship": frozenset({"epl"}),
    "LALIGA EA SPORTS": frozenset({"laliga"}), "LALIGA HYPERMOTION": frozenset({"laliga"}),
    "Serie A Enilive": frozenset({"seriea"}), "Serie BKT": frozenset({"seriea"}),
    "Bundesliga": frozenset({"bundesliga"}), "Bundesliga 2": frozenset({"bundesliga"}),
    "Ligue 1 McDonald's": frozenset({"ligue1"}), "Ligue 2 BKT": frozenset({"ligue1"}),
    "LPF": frozenset({"argentina"}),
    # CONMEBOL clubs move between the two cups season to season, so neither is exclusive
    "Libertadores": frozenset({"libertadores", "sudamericana"}),
    "Sudamericana": frozenset({"libertadores", "sudamericana"}),
}

# Reserve / academy sides carry the parent club's name and would otherwise pour their
# tags into the first team's aggregate ("Real Sociedad B" → real_sociedad).
_RESERVE_RE = re.compile(r"(\s(B|II|2)$)|U1[6-9]|U2[0-3]|Reserv", re.I)

# Hand overrides, applied AFTER the auto-prior. Kept deliberately short: each entry
# needs a tactical identity that is both uncontroversial and demonstrably missed by the
# tag aggregate, because a manual style is an opinion and the auto one is at least a
# measurement. The systematic reason overrides are still needed: EA's playStyles are
# individual-attribute tags, and the possession bucket owns 7 of them against low_block's
# 3, so the aggregate over-reports possession (43% of clubs primary) and barely ever
# reports a defensive shape. The right-hand comment on each row is the auto verdict it
# corrects — re-check them whenever _TAG_TO_STYLE or the FC26 vintage changes.
_PRIOR_OVERRIDES: dict[str, list[tuple[str, float]]] = {
    # auto: possession 1.0 + direct 0.86 — Simeone's side is the archetypal low block
    "atletico_madrid": [("low_block", 1.0), ("clinical", 0.6)],
    # auto: possession 1.0 + direct 0.5 — the "direct" leg is a Yamal/Raphinha pace
    # artifact; Flick's high line is the actual second trait
    "barcelona": [("possession", 1.0), ("high_press", 0.7)],
    # auto: possession only (no second style cleared the bar) — the press is half of it
    "manchester_city": [("possession", 1.0), ("high_press", 0.7)],
    # auto: high_press 1.0 + direct 0.8 — Getafe is the league's reference low block
    "getafe": [("low_block", 1.0), ("set_piece", 0.6)],
    # auto: possession 1.0 + low_block 0.92 — backwards; Dortmund defend high and break
    "borussia_dortmund": [("high_press", 1.0), ("direct", 0.8)],
}


def _fc26_style_prior() -> dict[str, list[tuple[str, float]]]:
    """{club_id: [(style, weight), ...]} from FC26 squad playStyles tag aggregates."""
    from collections import Counter, defaultdict

    from prediction_market_soccer.ingest import store as _store
    from prediction_market_soccer.ingest.fc_ingest import load_fc_frame
    from prediction_market_soccer.venues.polymarket_global.reader import poly_club_candidates

    conn = _store.init_db()
    comps_of: dict[str, set[str]] = {}
    reg_names: dict[str, str] = {}
    for r in conn.execute("SELECT club_id, comp, name FROM club_registry"):
        comps_of.setdefault(r["club_id"], set()).add(r["comp"])
        if r["name"]:
            reg_names.setdefault(r["name"], r["club_id"])
    reg_ids = set(comps_of)
    aliases = _venue_aliases()

    import difflib as _difflib

    def club_of(team_name: str, league_name: str) -> str:
        """FC26 team label → registry club_id, exact spellings first (§3.6 order).

        The old path was ``club_id_of`` then a 0.85 difflib guess, which resolved only
        129 clubs AND mis-resolved across countries ("Vitória SC" of Guimarães onto
        Brazil's Vitória, "R. Racing Club" of Santander onto Racing Avellaneda). Both
        failures are fixed by the same two rules: try the exact spellings the venue
        alias tables already carry, and let a club only match a competition its FC26
        league can actually feed.
        """
        s = (team_name or "").strip()
        if not s or _RESERVE_RE.search(s):
            return ""
        strict = _FC_LEAGUE_COMPS.get(league_name)
        allowed = (strict | _CONTINENTAL) if strict else _CONTINENTAL
        cid = _FC_TEAM_ALIASES.get(s)
        if cid and cid in reg_ids:
            return cid
        cid = aliases.get(s)
        if cid and cid in reg_ids:
            return cid
        for i, cid in enumerate(poly_club_candidates(s)):
            # The as-written spelling (i == 0) is unambiguous enough to stand alone;
            # the legal-form-stripped one is not, so it must clear the league gate.
            if cid in reg_ids and (i == 0 or comps_of[cid] & allowed):
                return cid
        # Last resort: a fuzzy name match, held to the club's OWN competition (no
        # continental escape hatch) — that is what kept Racing Santander out.
        best = _difflib.get_close_matches(s, list(reg_names), n=1, cutoff=0.85)
        if best:
            cid = reg_names[best[0]]
            if comps_of[cid] & (strict or _CONTINENTAL):
                return cid
        return ""

    df = load_fc_frame()
    tag_style = {t: code for code, tags in _TAG_TO_STYLE.items() for t in tags}
    scores: dict[str, Counter] = defaultdict(Counter)
    counts: Counter = Counter()
    for _, r in df.iterrows():
        club = club_of(str(r.get("team") or ""), str(r.get("leagueName") or ""))
        if not club:
            continue
        counts[club] += 1
        for raw in (str(r.get("playStyles") or "") + "," + str(r.get("playStylesPlus") or "")).split(","):
            tag = raw.strip().rstrip("+")
            code = tag_style.get(tag)
            if code:
                scores[club][code] += 1
    out: dict[str, list[tuple[str, float]]] = {}
    for club, sc in scores.items():
        if counts[club] < 8:
            continue
        norm = {c: v / counts[club] for c, v in sc.items()}
        ranked = sorted(norm.items(), key=lambda kv: -kv[1])
        if not ranked:
            continue
        top, top_v = ranked[0]
        styles = [(top, 1.0)]
        if len(ranked) > 1 and ranked[1][1] >= 0.5 * top_v:
            styles.append((ranked[1][0], round(ranked[1][1] / top_v, 2)))
        out[club] = styles
    out.update(_PRIOR_OVERRIDES)
    return out


def _venue_aliases() -> dict[str, str]:
    """Merged {venue spelling -> club_id} from data/priors/aliases_<comp>.json (§3.6).

    Reused rather than re-curated: those tables already hold the long spellings the
    market writes ("Tottenham Hotspur", "Olympique de Marseille"), and FC26 writes the
    same kind of long form, so one curation pass serves both.
    """
    from prediction_market_soccer.config.leagues import active as _active
    out: dict[str, str] = {}
    for c in _active(include_disabled=True):
        try:
            doc = json.loads(
                (CONFIG.paths.priors / f"aliases_{c.key}.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        for k, v in (doc.get("aliases") or {}).items():
            out.setdefault(k, v)
    return out


_PRIOR_CACHE: dict | None = None


def _get_prior() -> dict[str, list[tuple[str, float]]]:
    global _PRIOR_CACHE
    if _PRIOR_CACHE is None:
        try:
            _PRIOR_CACHE = _fc26_style_prior()
        except Exception as e:  # noqa: BLE001 — CSV missing → live-only styles
            print(f"[team_styles] FC26 auto-prior unavailable ({e}) — live-only")
            _PRIOR_CACHE = {}
    return _PRIOR_CACHE


PRIOR: dict[str, list[tuple[str, float]]] = {}   # populated lazily via _get_prior()

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
    """z-score a {team: value} map (robust to <2 teams).

    KNOWN LIMITATION: the pool is every played club across all 12 competitions, so a
    UECL qualifier's possession is compared against La Liga's. Re-pooling per
    competition would move teams between style COLUMNS (`style`/`cluster` feed the
    agent tool and strategy/inplay_confidence), so it is a model decision, not an
    export-field one — the display layer instead adds a per-competition rank to each
    cell so the number a reader sees has the right denominator.
    """
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
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior

    conn = conn or store.init_db()
    name, zh = {}, {}
    for r in conn.execute("SELECT club_id, name, zh FROM club_registry"):
        name[r["club_id"]] = r["name"]
        zh[r["club_id"]] = r["zh"] or ""
    # 归属赛事以"主联赛优先"判定,而不是 club_registry 的第一行 —— 后者取到哪一行
    # 取决于表扫描顺序,阿森纳可能被判进欧冠。
    league_of, leagues_of = league_membership(conn)
    all_teams = sorted(name)
    global PRIOR
    PRIOR = _get_prior()
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

    # 4) prior score per team (weight on the named styles, 0 elsewhere); None when the
    #    club has no FC26 prior at all — see the blend below for why that is not the
    #    same as a "balanced" prior.
    def prior_scores(t):
        named = PRIOR.get(t)
        if not named:
            return None
        sc = {c: 0.0 for c in STYLE_CODES}
        for code, w in named:
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
            # A missing FC26 prior used to be filled with ("balanced", 1.0), which then
            # won every blend by construction: the prior leg contributes up to 0.55 and
            # the normalised live leg at most 0.45, so a club with no prior was labelled
            # "balanced" no matter how extreme its actual possession/xG profile was.
            # FC26 covers 191 of our 399 clubs (Brazil has no FC26 league at all), so
            # that was mislabelling half the matrix. With no prior, trust the live data.
            blend = ({c: _PRIOR_WEIGHT * ps[c] + _LIVE_WEIGHT * lsn[c] for c in STYLE_CODES}
                     if ps else lsn)
        else:
            # No prior AND no live data: "balanced" is the honest placeholder, not a read.
            blend = ps or {c: (1.0 if c == "balanced" else 0.0) for c in STYLE_CODES}
        ranked = sorted(STYLE_CODES, key=lambda c: -blend[c])
        chosen = [ranked[0]]
        if blend[ranked[1]] >= _SECOND_STYLE_FRAC * blend[ranked[0]] and blend[ranked[1]] > 0:
            chosen.append(ranked[1])
        poss = round(raw[t]["poss"] / 100.0, 3) if t in raw else _DEFAULT_POSS[chosen[0]]
        teams_out.append({
            "team_id": t, "league": league_of.get(t), "leagues": leagues_of.get(t, []),
            "name": name.get(t, t), "zh": zh.get(t, ""),
            "poss": poss, "played": t in raw,
            "styles": [{"code": c} for c in chosen],
            "_raw": raw.get(t),
        })

    # 6) within each style column, rank teams by poss (desc); attach poss+rank to each cell
    #    列内名次原本只有跨赛事的一份(balanced 列 1..270),前端把它直接塞进 tooltip,
    #    筛到英超后仍显示 #41,读起来像"英超第 41 控球队"。所以每格再带一份本赛事内的
    #    名次 + 该赛事在这一列的球队数,筛选后才有可读的分母。
    for c in STYLE_CODES:
        members = [tm for tm in teams_out if any(s["code"] == c for s in tm["styles"])]
        members.sort(key=lambda tm: -tm["poss"])
        n_in_col: dict[str, int] = {}
        for tm in members:
            lg = tm.get("league")
            if lg is not None:
                n_in_col[lg] = n_in_col.get(lg, 0) + 1
        seen: dict[str, int] = {}
        for i, tm in enumerate(members):
            lg = tm.get("league")
            if lg is not None:
                seen[lg] = seen.get(lg, 0) + 1
            for s in tm["styles"]:
                if s["code"] == c:
                    s["rank"] = i + 1
                    s["league_rank"] = seen.get(lg) if lg is not None else None
                    s["league_n"] = n_in_col.get(lg) if lg is not None else None

    # 7) legacy projection so existing downstream readers work unchanged
    teams_final = [_with_legacy(tm) for tm in teams_out]
    teams_final.sort(key=lambda x: (x["style"], -x["poss"]))
    n_by_league: dict[str, int] = {}
    for tm in teams_final:
        lg = tm.get("league")
        if lg is not None:
            n_by_league[lg] = n_by_league.get(lg, 0) + 1
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n": len(teams_final),
        # 子标题的 `n clubs` 是全量;按赛事筛选后要用这里的分母,否则拿 399 冒充英超 20
        "n_by_league": n_by_league,
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
        "team_id": tm["team_id"], "league": tm.get("league"), "leagues": tm.get("leagues", []),
        "name": tm["name"], "zh": tm["zh"],
        "poss": tm["poss"], "played": tm["played"],
        # NEW: 1–2 styles, each with poss + within-style rank (global AND within its own comp)
        "styles": [{"code": s["code"], "label": STYLE_LABEL[s["code"]], "poss": tm["poss"],
                    "rank": s["rank"], "global_rank": s["rank"],
                    "league_rank": s.get("league_rank"), "league_n": s.get("league_n")}
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
            # The weekly throttle must not LOCK IN an empty build: the first club-edition
            # run happened before any fixture_stats existed, so every team read
            # played=false and the 7-day skip kept publishing that dead file. Rebuild
            # whenever live coverage has grown since the stored doc.
            prev_played = sum(1 for t in prev.get("teams", []) if t.get("played"))
            from prediction_market_soccer.ingest import store as _store
            _c = _store.init_db()
            live_teams = _c.execute(
                "SELECT COUNT(DISTINCT team_api_id) n FROM fixture_stats").fetchone()["n"]
            if age_d < 7 and prev_played >= min(live_teams, 1):
                print(f"team_styles.json: fresh ({age_d:.1f}d < 7d, {prev_played} played) — skipped")
                return
            if prev_played < live_teams:
                print(f"team_styles.json: live coverage grew ({prev_played} → up to {live_teams}) — rebuilding")
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
          f"across {len(doc['n_by_league'])} competitions "
          f"({sum(len(t['styles']) for t in doc['teams'])} style assignments; "
          f"{sum(t['played'] for t in doc['teams'])} played)")


if __name__ == "__main__":
    main()
