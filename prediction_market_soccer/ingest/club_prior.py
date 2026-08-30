"""Club prior — the soccer replacement for the WC static prior (TRANSFORM_PLAN §3.2).

Replaces ``prior_ingest.py``'s 48-team/12-group snapshot with per-competition club
priors built from three anchors:

  1. last-season final table (API-Football standings, season-1; promoted clubs
     get a conservative default),
  2. ClubElo (one CSV for all of Europe, api.clubelo.com — cross-league
     comparable; South America is NOT covered → neutral fill, disclosed),
  3. market-implied (Kalshi champion series devig — wired in Phase 3; the slot
     exists so the strength reverse-fit can consume it later).

IRON RULE inherited verbatim from the WC module: priors are a *stale starting
line*, never a tradable signal. Played results override them (strength update
path); with 34–38 rounds a season the update path dominates quickly.

Interface mirrors the WC ``PriorSnapshot`` (class + core methods kept;
``group``→``league``, ``draw()``→``league_table()``). ``clubs_all.json`` also
carries a WC-compatible ``teams: [{team, fifa_rank}]`` projection so copied
consumers that read ``CONFIG.paths.prior_ext_sim_v0`` (inplay_confidence) work
with a config repoint only.
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Competition, active, get

_PRIORS = CONFIG.paths.priors

# ClubElo country code → our comp keys whose clubs live in that country.
_ELO_COUNTRY_TO_COMPS = {
    "ENG": ["epl"], "ESP": ["laliga"], "ITA": ["seriea"], "GER": ["bundesliga"],
    "FRA": ["ligue1"], "POR": ["portugal"], "NED": ["eredivisie"],
}

# Aliases: ClubElo / venue spellings → canonical club_id (extended by bootstrap).
CLUB_ALIASES: dict[str, str] = {
    # ClubElo short names → API-Football canonical ids (seeded; bootstrap extends)
    "man city": "manchester_city", "man united": "manchester_united",
    "forest": "nottingham_forest", "wolves": "wolves",
    "newcastle": "newcastle", "tottenham": "tottenham",
    "atletico": "atletico_madrid", "betis": "real_betis", "sociedad": "real_sociedad",
    "bilbao": "athletic_club", "celta": "celta_vigo",
    "inter": "inter", "milan": "ac_milan", "napoli": "napoli", "roma": "as_roma",
    "lazio": "lazio", "juventus": "juventus", "atalanta": "atalanta",
    "bayern": "bayern_munich", "bayern munchen": "bayern_munich",
    "dortmund": "borussia_dortmund", "leverkusen": "bayer_leverkusen",
    "gladbach": "borussia_monchengladbach", "leipzig": "rb_leipzig",
    "paris sg": "paris_saint_germain", "marseille": "marseille", "monaco": "monaco",
    "lyon": "lyon", "lille": "lille",
}


def canonical_club_name(name: str) -> str:
    key = (name or "").strip().lower()
    return CLUB_ALIASES.get(key, name.strip())


def team_id(name: str) -> str:
    """Stable club_id (same normalization as ingest.soccer_ingest.club_id_of)."""
    key = (name or "").strip().lower()
    if key in CLUB_ALIASES:
        return CLUB_ALIASES[key]
    s = key.replace("&", "and")
    s = re.sub(r"[''´`\.\-/]", " ", s)
    s = re.sub(r"\s+", "_", s.strip())
    return re.sub(r"[^a-z0-9_]", "", s)


@dataclass(frozen=True)
class ClubPrior:
    club_id: str
    name: str
    zh: str
    league: str                 # comp key (was `group` in the WC snapshot)
    api_team_id: int
    elo: float | None           # ClubElo (None outside coverage)
    elo_rank: int | None        # global rank across all enabled comps' clubs with Elo
    last_pts: float | None      # last-season points (per-round for cross-format comparability)
    last_rank: int | None
    promoted: bool
    anchor_points: float | None # expected-season-points target for the strength reverse-fit
    market_p_champion: float | None = None  # Phase 3 fills from Kalshi devig

    # ── WC PriorSnapshot drop-in compatibility (copied consumers use these names) ──
    @property
    def team_id(self) -> str:      # noqa: A003 — WC field name
        return self.club_id

    @property
    def fifa_rank(self) -> int:    # WC rank field → global Elo/anchor rank
        return self.elo_rank if self.elo_rank is not None else 999

    @property
    def group(self) -> str:        # WC group letter → league key
        return self.league


@dataclass(frozen=True)
class ClubPriorSnapshot:
    prior_id: str
    source: str
    as_of: str
    is_stale: bool
    league: str
    teams: tuple[ClubPrior, ...]

    @property
    def by_id(self) -> dict[str, ClubPrior]:
        return {t.club_id: t for t in self.teams}

    def ranks(self) -> dict[str, int]:
        """club_id -> cross-league Elo rank (the WC snapshot's fifa_rank analogue)."""
        return {t.club_id: t.elo_rank for t in self.teams if t.elo_rank is not None}

    def league_table(self) -> list[str]:
        """club_ids ordered by anchor strength (was `draw()` in the WC snapshot)."""
        return [t.club_id for t in sorted(
            self.teams, key=lambda t: -(t.anchor_points if t.anchor_points is not None else -1))]


class PriorValidationError(ValueError):
    pass


# WC drop-in aliases (copied consumers import these exact names).
canonical_team_name = canonical_club_name
PriorSnapshot = ClubPriorSnapshot
TEAM_ALIASES = CLUB_ALIASES


def _path(comp_key: str) -> Path:
    return _PRIORS / f"clubs_{comp_key}.json"


def load_prior(league: str | None = None, *, suffix: str = "") -> ClubPriorSnapshot:
    """Load one competition's club prior; ``league=None`` loads the merged all-comps
    snapshot (clubs_all.json — cross-league Elo ranks, used by confidence tiers).

    ``suffix`` selects a parallel set written by ``build_all(suffix=...)`` — the
    walk-forward builds point-in-time priors under "_pit" so a backtest never reads,
    and never overwrites, the nightly file the live exports price from.
    """
    path = (_PRIORS / f"clubs_all{suffix}.json") if league is None else _path(league + suffix)
    if not path.exists():
        raise PriorValidationError(f"club prior not found: {path} (run: python -m "
                                   "prediction_market_soccer.ingest.club_prior --build)")
    raw = json.loads(path.read_text(encoding="utf-8"))
    teams = tuple(ClubPrior(**{k: rec.get(k) for k in ClubPrior.__dataclass_fields__})
                  for rec in raw["clubs"])
    snap = ClubPriorSnapshot(
        prior_id=raw["prior_id"], source=raw["source"], as_of=raw["as_of"],
        is_stale=bool(raw.get("is_stale", True)), league=raw.get("league", "all"), teams=teams)
    _validate(snap)
    return snap


def _validate(snap: ClubPriorSnapshot) -> None:
    errors = []
    ids = [t.club_id for t in snap.teams]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        errors.append(f"duplicate club_ids: {dupes}")
    if snap.league != "all":
        comp = get(snap.league)
        if comp.kind == "league" and len(snap.teams) != comp.n_teams:
            errors.append(f"{snap.league}: expected {comp.n_teams} clubs, got {len(snap.teams)}")
        elif comp.kind in ("swiss_ucl", "cup_two_leg", "league_playoffs") and len(snap.teams) < comp.n_teams // 2:
            # qualifying supersets (UCL 52 now → 36 after the draw) are normal;
            # only a drastically short roster is an error
            errors.append(f"{snap.league}: implausibly few clubs ({len(snap.teams)} < {comp.n_teams}//2)")
    if errors:
        raise PriorValidationError("; ".join(errors))


# ── builder ──────────────────────────────────────────────────────────────────

def _is_today(as_of: str | None) -> bool:
    """True when ``as_of`` is today or later — i.e. when a live venue read is legitimate."""
    if not as_of:
        return True
    return as_of[:10] >= datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _market_champion_probs(comp_key: str) -> dict[str, float]:
    """{club_id: de-vigged P(champion)} from the Kalshi champion book, or {}.

    The third anchor of TRANSFORM_PLAN §3.2, and the only one that is forward
    looking: the table and ClubElo both describe what a club HAS done, while the
    title book is money on what it is about to do (it prices the summer's
    transfers, the manager change and the injury list before any of them show up
    in a result). Shin de-vig because a title book is a longshot-heavy N-way
    market, the same treatment strategy/xv_monitor gives it.
    """
    try:
        from prediction_market_soccer.venues.champion_prices import season_cents
        from prediction_market_soccer.strategy.devig import devig
    except Exception:
        return {}
    try:
        cents = season_cents(comp_key, "champion") or {}
    except Exception:
        return {}
    keys = [k for k, v in cents.items() if v is not None and v > 0]
    if len(keys) < 4:          # a two-name board carries no field information
        return {}
    asks = [cents[k] / 100.0 for k in keys]
    try:
        p = devig(asks, method="shin")
    except Exception:
        try:
            p = devig(asks, method="multiplicative")
        except Exception:
            return {}
    return {k: float(x) for k, x in zip(keys, p)}


def _fetch_clubelo(as_of: str) -> list[dict]:
    """One CSV = every European club's Elo. Cached on disk per date."""
    import requests
    cache = _PRIORS / f"clubelo_{as_of}.csv"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        last_err = None
        text = None
        for url in (f"http://api.clubelo.com/{as_of}", f"https://api.clubelo.com/{as_of}"):
            for attempt in range(3):
                try:
                    r = requests.get(url, timeout=90)
                    r.raise_for_status()
                    text = r.text
                    break
                except Exception as e:  # noqa: BLE001 — retry then surface
                    last_err = e
            if text:
                break
        if not text:
            raise RuntimeError(f"clubelo fetch failed after retries: {last_err}")
        cache.write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ClubElo covers UEFA only. Any registry club outside these federations must never
# receive an Elo — the fuzzy pass below cannot tell "Barcelona SC" (Ecuador) from
# "Barcelona" (Spain), and it handed the Ecuadorian club a 1952 Elo, an anchor of
# 1.571 and 8th place of 47 in the Libertadores prior. Worse, a fabricated Elo also
# blocked the domestic-league anchor loan, which is gated on having no Elo.
_ELO_ELIGIBLE_COMPS = frozenset({
    "epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "uel", "uecl",
    "portugal", "eredivisie",
})


def _match_elo(elo_rows: list[dict], registry_rows: list[dict],
               comp_key: str | None = None) -> dict[int, float]:
    """api_team_id -> Elo, via alias table first, then a fuzzy match that is actually
    constrained (the previous version documented a constraint it never applied)."""
    if comp_key is not None and comp_key not in _ELO_ELIGIBLE_COMPS:
        # A CONMEBOL club has no ClubElo row; every "match" would be a coincidence of
        # spelling. Return nothing and let the table / domestic-loan anchors speak.
        return {}
    by_alias: dict[int, float] = {}
    unmatched_registry = []
    elo_by_id: dict[str, float] = {}
    for er in elo_rows:
        cid = team_id(er["Club"])
        elo_by_id.setdefault(cid, float(er["Elo"]))
    for rr in registry_rows:
        cid = rr["club_id"]
        if cid in elo_by_id:
            by_alias[rr["api_team_id"]] = elo_by_id[cid]
        else:
            unmatched_registry.append(rr)
    # fuzzy pass for the rest (bootstrap-time only; result frozen into the JSON)
    elo_names = {er["Club"]: float(er["Elo"]) for er in elo_rows}
    keys = list(elo_names)
    for rr in unmatched_registry:
        # 0.78 is right ONCE the federation gate above is in place: the 40 extra
        # matches it buys over 0.90 are all genuine German/Nordic prefix differences
        # ("SC Freiburg"→"Freiburg", "VfB Stuttgart"→"Stuttgart"), and raising it to
        # 0.90 cost 40 real European clubs their Elo. What made "Juventud"→"Juventus"
        # possible was searching a European table for a South American club at all,
        # which the gate now prevents.
        best = difflib.get_close_matches(rr["name"], keys, n=1, cutoff=0.78)
        if best:
            by_alias[rr["api_team_id"]] = elo_names[best[0]]
    return by_alias



def _current_table(conn, comp, season, as_of: str) -> dict:
    """{team_api_id: {points, played}} for the CURRENT season as it stood at ``as_of``.

    The `standing` table is a snapshot of NOW and carries no history, so a prior built
    for a past date read a table that already knew the rest of the season. That is a
    real leak for the mid-season competitions this anchor exists for — Brasileirão and
    the Argentine league are half-played, so `_cur_ppr` is the dominant term, and a
    walk-forward model scoring a July match was anchored on the August table. Measured:
    corr(anchor_points, the CURRENT season's realised points-per-round) = 0.986.

    For a PAST date the table is therefore reconstructed from the fixtures that had
    actually finished by then — the only source that can answer the question. For today
    the official `standing` feed is used unchanged, because it is authoritative (it
    carries points deductions and administrative rulings that fixtures cannot show).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not as_of or as_of >= today:
        return {r["team_api_id"]: dict(r) for r in conn.execute(
            "SELECT team_api_id, points, rank, played FROM standing WHERE league_id=? AND season=?",
            (comp.api_football_id, (season or comp.season)))}
    from prediction_market_soccer.config.leagues import Stage, stage_of
    out: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT round, home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE league_id=? AND season=? AND status_short IN ('FT','AET','PEN') "
        "AND home_goals IS NOT NULL AND kickoff_ts < ?",
            (comp.api_football_id, (season or comp.season), as_of)):
        if stage_of(comp.key, r["round"]) != Stage.LEAGUE:
            continue          # a cup round is not a league table
        hg, ag = int(r["home_goals"]), int(r["away_goals"])
        for tid, gf, ga in ((r["home_api_id"], hg, ag), (r["away_api_id"], ag, hg)):
            if tid is None:
                continue
            d = out.setdefault(tid, {"team_api_id": tid, "points": 0, "rank": None, "played": 0})
            d["points"] += 3 if gf > ga else (1 if gf == ga else 0)
            d["played"] += 1
    return out

def build_all(conn, *, as_of: str | None = None, season: int | None = None,
              suffix: str = "") -> dict:
    """Build clubs_<comp>.json for every enabled comp + merged clubs_all.json.

    Requires: sync_teams + sync_standings(season) + sync_standings(season-1)
    already ingested. Zero API-Football calls here; one ClubElo CSV fetch."""
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        elo_rows = _fetch_clubelo(as_of)
    except Exception as e:
        print(f"[club_prior] ClubElo unavailable ({e}) — building without Elo anchor")
        elo_rows = []

    all_clubs: list[dict] = []
    summary = {}
    for comp in active():
        regs = [dict(r) for r in conn.execute(
            "SELECT club_id, comp, api_team_id, name, zh, logo FROM club_registry WHERE comp=?",
            (comp.key,))]
        if not regs:
            print(f"[club_prior] {comp.key}: no clubs in registry — skipped (ingest static first)")
            continue
        elo_of = _match_elo(elo_rows, regs, comp.key) if elo_rows else {}
        # last-season table (Europe: season-1; SA calendar comps: season-1 too)
        last = {r["team_api_id"]: dict(r) for r in conn.execute(
            "SELECT team_api_id, points, rank, played FROM standing WHERE league_id=? AND season=?",
            (comp.api_football_id, (season or comp.season) - 1))}
        # current standings (mid-season comps: BRA/ARG already half-played)
        cur = _current_table(conn, comp, season, as_of)

        clubs = []
        for rr in regs:
            tid = rr["api_team_id"]
            elo = elo_of.get(tid)
            lrow = last.get(tid)
            crow = cur.get(tid)
            last_ppr = (lrow["points"] / max(1, lrow["played"])) if lrow and lrow.get("points") is not None else None
            # CUP COMPETITIONS: "last season's table" is that CUP's own group/qualifying
            # standings — a handful of games against a self-selected field, not a measure
            # of club strength. Rapid Vienna read 0.17 ppr (1 point from its European
            # mini-group) and the model then wanted its opponents at any price, producing
            # the +0.27/+0.30 "edges" that lost money on the qualifying rounds. Only a
            # LEAGUE table is a strength anchor; for cups the ClubElo anchor carries the
            # club, and a comp-mean default fills in when Elo is missing.
            if comp.kind != "league" and (not lrow or (lrow.get("played") or 0) < 10):
                last_ppr = None
            # current-season table only once it carries signal (mid-season joins like
            # BRA/ARG, plan R8); with 1-2 rounds played it is pure noise (a 1-0 start
            # reads as 3.0 ppr) — fall back to last season below the threshold.
            cur_ppr = None
            if crow and crow.get("points") is not None and (crow.get("played") or 0) >= 8:
                cur_ppr = crow["points"] / crow["played"]
            promoted = lrow is None
            clubs.append({
                "club_id": rr["club_id"], "name": rr["name"], "zh": rr.get("zh") or "",
                "league": comp.key, "api_team_id": tid,
                "elo": elo, "elo_rank": None,
                "last_pts": round(last_ppr, 4) if last_ppr is not None else None,
                "last_rank": lrow.get("rank") if lrow else None,
                "promoted": promoted,
                "anchor_points": None, "market_p_champion": None,
                "_cur_ppr": cur_ppr,
            })

        # anchor_points: expected points-per-round, from Elo z (within comp) blended with
        # last-season ppr; current-season ppr (mid-season SA comps) replaces last-season.
        elos = [c["elo"] for c in clubs if c["elo"] is not None]
        if elos:
            mu = sum(elos) / len(elos)
            sd = (sum((e - mu) ** 2 for e in elos) / len(elos)) ** 0.5 or 1.0
        pprs = [c["_cur_ppr"] if c["_cur_ppr"] is not None else c["last_pts"] for c in clubs]
        pprs = [p for p in pprs if p is not None]
        # A handful of surviving cup rows is not a league mean — require a real
        # sample before trusting it, else use the neutral 1.35 ppr baseline.
        league_mean_ppr = (sum(pprs) / len(pprs)) if len(pprs) >= 8 else 1.35
        # Promoted-club anchor: they perform about like the clubs they REPLACED, i.e.
        # the relegation zone — not mid-table. The old `league_mean * 0.75` default put
        # Paderborn/Elversberg above Köln and Werder, and the model then priced Mainz v
        # Paderborn as an away win (26/28/46) against a 58/23/19 market. Anchor instead
        # on the mean ppr of the bottom `releg_direct+releg_playoff` clubs (min 2) of
        # the reference table — "a promoted club plays like the bottom of the table",
        # which is what the market prices them at.
        # Third anchor: what the title market thinks. Converted onto the ppr scale by
        # its own z within the competition, exactly like the Elo anchor, so all three
        # anchors speak one language before they are blended.
        # The market anchor is a LIVE read of the Kalshi title book — season_cents calls
        # list_events(status="open"), which has no date parameter and no history. For a
        # prior stamped in the past that is the market's opinion TODAY, formed after the
        # very matches the prior is used to score: measured, a 2026-07-14 PIT prior and
        # today's carried identical market_p_champion for 20 of 20 Brasileirão clubs.
        # An anchor we cannot know as of that date is therefore OMITTED, not approximated —
        # the prior falls back on the two anchors that ARE historical (last season's table,
        # and ClubElo, which the API does serve per date). This also makes a backtest
        # reproducible: rebuilding the same as_of tomorrow now yields the same prior.
        mkt = _market_champion_probs(comp.key) if _is_today(as_of) else {}
        mkt_z: dict[str, float] = {}
        if len(mkt) >= 4:
            import math as _m
            # log-odds: a title market is extremely skewed (a favourite at 60% and a
            # tail at 0.5%), and averaging raw probabilities would let one favourite
            # dominate the z entirely.
            lo = {k: _m.log(max(v, 1e-4) / max(1 - v, 1e-4)) for k, v in mkt.items()}
            _m_mu = sum(lo.values()) / len(lo)
            _m_sd = (sum((v - _m_mu) ** 2 for v in lo.values()) / len(lo)) ** 0.5 or 1.0
            mkt_z = {k: (v - _m_mu) / _m_sd for k, v in lo.items()}

        _k = max(2, (comp.releg_direct or 0) + (comp.releg_playoff or 0))
        _bottom = sorted(pprs)[:_k] if pprs else []
        promoted_ppr = round(
            (sum(_bottom) / len(_bottom)) if _bottom else league_mean_ppr * 0.62, 4)
        for c in clubs:
            table_ppr = c["_cur_ppr"] if c["_cur_ppr"] is not None else c["last_pts"]
            # Is the table half of the blend REAL evidence, or just a filler? In a cup
            # most clubs have no usable table (above), and averaging a real Elo read
            # 50/50 with a constant fill would flatten genuine strength differences —
            # so the fill only gets a token weight.
            real_table = table_ppr is not None
            if not real_table:
                table_ppr = promoted_ppr
            if c["elo"] is not None and elos:
                z = (c["elo"] - mu) / sd
                elo_ppr = league_mean_ppr + 0.35 * z   # ~0.35 ppr per Elo σ (v1, disclosed)
                w_elo = 0.5 if real_table else 0.85
                anchor = w_elo * elo_ppr + (1.0 - w_elo) * table_ppr
            else:
                anchor = table_ppr
            mz = mkt_z.get(c["club_id"])
            if mz is not None:
                # A quarter weight, deliberately small: the title book is sharp about
                # the top of the table and nearly silent about mid-table (everyone
                # outside the race trades at the same 1¢), so it earns a nudge, not a
                # vote. Same 0.35-ppr-per-sigma scale as the Elo anchor.
                mkt_ppr = league_mean_ppr + 0.35 * mz
                anchor = 0.75 * anchor + 0.25 * mkt_ppr
                c["market_p_champion"] = round(mkt.get(c["club_id"], 0.0), 5)
            c["anchor_points"] = round(anchor, 4)
            c.pop("_cur_ppr", None)

        doc = {
            "prior_id": f"clubs_{comp.key}{suffix}_{as_of}",
            "source": "api-football standings (s & s-1) + ClubElo + Kalshi champion book (Shin de-vig)",
            "as_of": as_of, "is_stale": True, "league": comp.key,
            "notes": [
                "anchor_points = expected points-per-round; 0.5*Elo-implied + 0.5*table ppr;",
                "promoted clubs default 0.75x league mean; SA comps use CURRENT-season table",
                "(mid-season join, plan R8); ClubElo covers Europe only — SA clubs fall back",
                "to table anchor (plan §3.2/§3.8-e: neutral fill, never fabricated).",
            ],
            "clubs": clubs,
        }
        _path(comp.key + suffix).write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        n_elo = sum(1 for c in clubs if c["elo"] is not None)
        summary[comp.key] = f"{len(clubs)} clubs, {n_elo} with Elo"
        all_clubs.extend(clubs)

    # global rank across every enabled comp's clubs (dedup by club_id).
    # Primary key = Elo; when ClubElo is unreachable/uncovered (SA), fall back to
    # anchor_ppr × league-strength weight — coarse but sufficient for the
    # confidence-tier percentiles (v1, disclosed; Elo backfills when reachable).
    _LG_W = {"epl": 1.00, "laliga": 0.97, "seriea": 0.95, "bundesliga": 0.95,
             "ligue1": 0.90, "ucl": 1.00, "uel": 0.85, "uecl": 0.75,
             "brasileirao": 0.82, "argentina": 0.78, "libertadores": 0.85,
             "sudamericana": 0.78, "portugal": 0.85, "eredivisie": 0.83}
    best: dict[str, dict] = {}
    for c in all_clubs:
        cur_ = best.get(c["club_id"])
        if cur_ is None or ((c["elo"] or 0) > (cur_["elo"] or 0)):
            best[c["club_id"]] = dict(c)
    def _rank_key(c: dict) -> float:
        if c["elo"] is not None:
            return c["elo"]
        # anchor_ppr ~[0.5, 2.6] → map onto a pseudo-Elo band [1400, 1900] so
        # Elo-known and fallback clubs sort together sanely
        a = (c["anchor_points"] or 1.0) * _LG_W.get(c["league"], 0.7)
        return 1400.0 + 200.0 * a
    ranked = sorted(best.values(), key=lambda c: -_rank_key(c))
    for i, c in enumerate(ranked, 1):
        c["elo_rank"] = i
    for c in all_clubs:
        b = best.get(c["club_id"])
        c["elo_rank"] = b.get("elo_rank") if b else None

    # Second pass. Two things can only be done once EVERY competition has been built:
    # the global elo_rank, and the domestic-anchor loan below.
    #
    # A CONMEBOL cup club has no usable anchor of its own: its "last season table" is
    # that cup's own group phase (correctly rejected upstream) and ClubElo covers 2 of
    # 47 Libertadores clubs. Everything therefore collapsed onto the same promoted
    # default — 45 of 47 clubs shared two anchor values, the rating spread came out at
    # 0.15 against 0.47-0.53 in every other competition, and the model priced a cup tie
    # as close to a coin flip whoever was playing. But many of those clubs ARE in a
    # domestic league we track with a real table, so their strength is known — just
    # filed under another competition. Lend it across, on the domestic ppr scale.
    domestic: dict[str, float] = {}
    for comp in active():
        if comp.kind not in ("league", "league_playoffs"):
            continue
        dp = _path(comp.key + suffix)
        if not dp.exists():
            continue
        for c in json.loads(dp.read_text(encoding="utf-8"))["clubs"]:
            if c.get("anchor_points") is not None and not c.get("promoted"):
                domestic[c["club_id"]] = c["anchor_points"]

    for comp in active():
        p = _path(comp.key + suffix)
        if not p.exists():
            continue
        doc = json.loads(p.read_text(encoding="utf-8"))
        n_loaned = 0
        for c in doc["clubs"]:
            b = best.get(c["club_id"])
            c["elo_rank"] = b.get("elo_rank") if b else None
            # Only lend where the club had nothing better: a cup entry whose own anchor
            # is the promoted/default fill and which has no Elo of its own.
            if (comp.kind in ("cup_two_leg", "swiss_ucl")
                    and c.get("elo") is None
                    and c["club_id"] in domestic):
                c["anchor_points"] = round(domestic[c["club_id"]], 4)
                c["anchor_source"] = "domestic_league"
                n_loaned += 1
        if n_loaned:
            summary[comp.key] = summary.get(comp.key, "") + f", {n_loaned} domestic anchors"
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    # The MERGED snapshot, written last and under the same suffix as everything else.
    #
    # Two bugs lived in the single line this replaces. It hard-coded "clubs_all.json"
    # while every per-comp file honoured `suffix`, so a walk-forward build
    # (build_all(suffix="_pit")) silently replaced the LIVE cross-league prior with a
    # point-in-time one — observed on disk as clubs_all.json carrying as_of 2026-08-03
    # while the per-comp files said 2026-08-27, and load_prior() with no argument is
    # what ~40 live call sites read. And it ran BEFORE the domestic-anchor loan below,
    # so even a correct nightly build left the merged file disagreeing with the per-comp
    # files on every loaned club. Rebuilt from the files as they finally stand.
    _best_final: dict[str, dict] = {}
    for comp in active():
        fp = _path(comp.key + suffix)
        if not fp.exists():
            continue
        for c in json.loads(fp.read_text(encoding="utf-8"))["clubs"]:
            cur = _best_final.get(c["club_id"])
            if cur is None or (c.get("elo") or 0) > (cur.get("elo") or 0):
                _best_final[c["club_id"]] = c
    # Re-rank from scratch. The per-comp files carry an elo_rank from the first pass, so
    # a club read back out of them arrives with a stale one — and a club with no Elo at
    # all kept a rank it had no basis for, which is how the merged snapshot ended up with
    # 399 ranked clubs, 88 duplicate positions and a maximum of 315. Only a club with an
    # Elo gets a cross-league rank; everyone else is explicitly None.
    for c in _best_final.values():
        c["elo_rank"] = None
    _ranked = sorted([c for c in _best_final.values() if c.get("elo") is not None],
                     key=lambda c: -c["elo"])
    for i, c in enumerate(_ranked, 1):
        c["elo_rank"] = i
    merged = {
        "prior_id": f"clubs_all{suffix}_{as_of}", "source": "merged per-comp priors",
        "as_of": as_of, "is_stale": True, "league": "all",
        "clubs": list(_best_final.values()),
        # WC-compat projection: copied consumers read {"teams":[{"team","fifa_rank"}]}
        "teams": [{"team": c["name"], "fifa_rank": c.get("elo_rank") or 999}
                  for c in _best_final.values()],
    }
    (_PRIORS / f"clubs_all{suffix}.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[club_prior] built:", json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build club priors (three-anchor, per comp)")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--show", default=None, help="comp key to print")
    args = ap.parse_args()
    if args.build:
        from prediction_market_soccer.ingest import store
        build_all(store.init_db())
    if args.show:
        s = load_prior(args.show)
        for t in sorted(s.teams, key=lambda t: -(t.anchor_points or 0))[:8]:
            print(f"  {t.club_id:28s} elo={t.elo or '-':>7} rank={t.elo_rank or '-':>4} "
                  f"anchor_ppr={t.anchor_points}")
