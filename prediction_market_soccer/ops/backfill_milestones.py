"""ops/backfill_milestones.py — reconstruct milestone price tracks for already-played
matches (plan 18 §2.4c).

The live `milestone_snapshot` capture only sees matches going forward. To give the
PriceTrack view history from the first stored match, we backfill the 6 milestones
(PRE / T15 / T30 / HT / T60 / T75 / FT) for every finished fixture using Polymarket
Global's PERSISTENT single-match events (`<comp-prefix>-{h}-{a}-{date}`, e.g.
`epl-cry-mac-2026-08-28`; closed/archived events stay queryable) + the public CLOB
prices-history series.

For each finished fixture:
  * locate its Poly Global event by competition + CLUB IDENTITY (both sides resolved
    to the same canonical club_id the rest of the module uses), scanning ET date ±1d;
  * pull each 3-way outcome token's per-minute price series once;
  * sample the price at each milestone's wall-clock (kickoff + minute);
  * compute the score at that minute from goal events; FT uses the real result
    (winner outcome settles 100¢ / 0¢);
  * de-vig the three outcome prices into a market probability;
  * INSERT OR REPLACE into milestone_snapshot (price_source='candlestick').

Idempotent + re-runnable; missing data is left as NULL (honest), never fabricated.

ENTITY RESOLUTION (club edition, TRANSFORM_PLAN §3.6). The WC version compared team
NAMES with difflib at a 0.72 cutoff plus a substring rule. Club names break both:
"Manchester City" vs "Manchester United" scores 0.80, and "Inter" / "Barcelona" /
"Nacional" / "Independiente" are substrings of Inter Miami / Barcelona SC / Nacional
Potosí / Independiente Medellín — a false match would staple one match's whole price
track onto another. So identity now goes through the frozen per-competition alias
tables (data/priors/aliases_<comp>.json) and the exact club_id normalisation, and an
unresolved venue label is COUNTED and reported rather than guessed at.

    python -m prediction_market_soccer.ops.backfill_milestones [--limit N] [--verbose]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prediction_market_soccer.config import CONFIG

ET = ZoneInfo("America/New_York")
_FINISHED = ("FT", "AET", "PEN")

# Milestone → minute offset from kickoff used to SAMPLE the market series.
_MILESTONES = [("PRE", -5), ("T15", 15), ("T30", 30), ("HT", 47), ("T60", 60), ("T75", 75)]


def _load_aliases(comp_key: str) -> dict[str, str]:
    """{venue spelling -> club_id} for one competition (bootstrap + curated, §3.6)."""
    try:
        doc = json.loads(
            (CONFIG.paths.priors / f"aliases_{comp_key}.json").read_text(encoding="utf-8"))
        return dict(doc.get("aliases") or {})
    except Exception:
        return {}


class _ClubResolver:
    """Polymarket outcome title → canonical club_id, EXACT only.

    Same resolution order the two venue adapters use (alias table → exact
    normalisation), so backfill, discovery and the live path all agree on who a
    label refers to. Comp-scoped first, then the merged table: Gamma occasionally
    tags a UEFA tie under the domestic prefix, and a club's alias was curated under
    whichever competition Kalshi listed it in.
    """

    def __init__(self, conn):
        self._by_comp: dict[str, dict[str, str]] = {}
        self._merged: dict[str, str] = {}
        from prediction_market_soccer.config.leagues import active
        for c in active(include_disabled=True):
            al = _load_aliases(c.key)
            self._by_comp[c.key] = al
            for k, v in al.items():
                self._merged.setdefault(k, v)
        self._club_ids = {r["club_id"] for r in conn.execute(
            "SELECT DISTINCT club_id FROM club_registry")}
        self.unmapped: list[str] = []   # deduped; surfaced in the run summary

    def resolve(self, label: str, comp_key: str | None) -> str | None:
        from prediction_market_soccer.venues.polymarket_global.reader import poly_club_candidates
        s = (label or "").strip()
        if not s:
            return None
        for table in (self._by_comp.get(comp_key or "", {}), self._merged):
            cid = table.get(s)
            if cid and cid in self._club_ids:
                return cid
        for cid in poly_club_candidates(s):
            if cid in self._club_ids:
                return cid
        if s not in self.unmapped:
            self.unmapped.append(s)
        return None


def _shift_day(iso_date: str, days: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


def _score_at(conn, fixture_id: int, hi_api: int, ai_api: int, minute: int) -> tuple[int, int]:
    """Home/away goals scored by `minute` (from goal events; own goals credited to
    the OTHER team's tally, matching the scoreboard)."""
    gh = ga = 0
    for r in conn.execute(
        "SELECT team_api_id, minute, detail FROM fixture_event "
        "WHERE fixture_api_id=? AND type='Goal'", (fixture_id,)):
        if (r["minute"] or 0) > minute:
            continue
        own = (r["detail"] or "") == "Own Goal"
        scoring_home = (r["team_api_id"] == hi_api) != own  # own goal flips side
        if scoring_home:
            gh += 1
        else:
            ga += 1
    return gh, ga


def backfill(conn=None, *, limit: int | None = None, verbose: bool = False, force: bool = False) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.util.price_history import price_at
    from prediction_market_soccer.venues.polymarket_global.reader import PolymarketGlobalReader

    conn = conn or store.init_db()
    tname = {r["api_id"]: r["name"] for r in conn.execute("SELECT api_id, name FROM team")}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    resolver = _ClubResolver(conn)

    # CLUB SCOPE GUARD (same rationale as backfill_price_ticks): recent + our comps only.
    from prediction_market_soccer.config.leagues import active as _active, by_api_id as _comp_of
    _lids = tuple(c.api_football_id for c in _active())
    _lph = ",".join("?" * len(_lids))
    fixtures_all = conn.execute(
        "SELECT MIN(kickoff_ts) lo, MAX(kickoff_ts) hi FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) AND kickoff_ts >= datetime('now', '-14 days')".format(
            ",".join("?" * len(_FINISHED)), _lph),
        (*_FINISHED, *_lids)).fetchone()
    # Window the Gamma query to the finished-match date range (±2d slack) so all
    # single-match events are returned reliably instead of paging through noise.
    from datetime import timedelta
    lo = (datetime.fromisoformat(fixtures_all["lo"]) - timedelta(days=2)).date().isoformat() if fixtures_all["lo"] else None
    hi = (datetime.fromisoformat(fixtures_all["hi"]) + timedelta(days=2)).date().isoformat() if fixtures_all["hi"] else None

    rd = PolymarketGlobalReader()
    events = rd.list_match_events(end_date_min=lo, end_date_max=hi)
    # Index events by (competition, date). The reader already tags each event with our
    # comp key (it parses the registry's Poly slug prefix), so keying on it means a
    # fixture is only ever compared against events from its OWN competition — the
    # first line of defence against a same-day, same-name club collision.
    by_comp_date: dict[tuple[str | None, str], list] = {}
    for e in events:
        sides = {}                      # club_id -> YES token, draw leg excluded
        for title, tok in (e["teams"] or {}).items():
            if "draw" in title.lower():
                continue
            cid = resolver.resolve(title, e.get("comp"))
            if cid:
                sides[cid] = tok
        e["_sides"] = sides
        e["_draw_token"] = next((t for n, t in (e["teams"] or {}).items()
                                 if "draw" in n.lower()), None)
        by_comp_date.setdefault((e.get("comp"), e["date"]), []).append(e)

    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, league_id "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "AND league_id IN ({}) AND kickoff_ts >= datetime('now', '-14 days') "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED)), _lph),
        (*_FINISHED, *_lids)).fetchall()
    # Incremental by default: only (re)process matches that don't yet have a complete
    # FT milestone row, so a steady-state run is cheap (the cycle guard calls us until
    # a just-ended match's venue history is finally available). force=True redoes all.
    if not force:
        done = {r["fixture_api_id"] for r in conn.execute(
            "SELECT fixture_api_id FROM milestone_snapshot WHERE milestone='FT'")}
        fixtures = [f for f in fixtures if f["api_id"] not in done]
    if limit:
        fixtures = fixtures[:limit]

    n_matched = n_rows = 0
    misses = []
    for fx in fixtures:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        hn, an = tname.get(fx["home_api_id"], ""), tname.get(fx["away_api_id"], "")
        comp = _comp_of(fx["league_id"])
        ko = datetime.fromisoformat(fx["kickoff_ts"])
        etd = ko.astimezone(ET).date().isoformat()
        ko_ts = int(ko.timestamp())
        if not (hi and ai and comp):
            misses.append(f"{hn} v {an} ({etd}) — fixture side unmapped")
            continue

        # Locate the Poly event by CLUB IDENTITY within this competition: both of our
        # club_ids must appear among the event's resolved outcomes. An id match is
        # exact, so there is no "best score" to pick between — either the event is this
        # match or it is not, and an ambiguous name can no longer promote a wrong event.
        # Because the match is now decided by identity, the date can be scanned ±1 day
        # instead of pinned to the ET date: Poly dates its slug by the LOCAL kickoff, so
        # a CONMEBOL match at 00:30 UTC sits on the previous day in ET and used to be
        # recorded as a miss. Two clubs cannot meet twice in a competition inside 3 days,
        # so the wider window cannot introduce an ambiguity.
        cand = [ev for d in (etd, _shift_day(etd, -1), _shift_day(etd, 1))
                for ev in by_comp_date.get((comp.key, d), [])]
        e = next((ev for ev in cand if hi in ev["_sides"] and ai in ev["_sides"]), None)
        if e is None:
            misses.append(f"{hn} v {an} ({comp.key} {etd})")
            continue
        n_matched += 1

        tok_home, tok_away = e["_sides"][hi], e["_sides"][ai]
        tok_draw = e["_draw_token"]
        ser = {}
        for side, tok in (("home", tok_home), ("draw", tok_draw), ("away", tok_away)):
            ser[side] = rd.prices_history(tok, fidelity=1) if tok else []

        result = "home" if fx["home_goals"] > fx["away_goals"] else (
            "draw" if fx["home_goals"] == fx["away_goals"] else "away")

        rows = list(_MILESTONES) + [("FT", 95)]
        for code, mn in rows:
            when = ko_ts + mn * 60
            ft = code == "FT"
            px = {}
            for side in ("home", "draw", "away"):
                if ft:
                    px[side] = 100.0 / 100 if side == result else 0.0  # settlement (0–1)
                else:
                    v, _ = price_at(ser[side], when, key="price")
                    px[side] = v
            # de-vig the 3 outcome prices (if all present) into a market prob.
            devig = None
            present = [px[s] for s in ("home", "draw", "away") if px[s] is not None]
            if len(present) == 3 and sum(present) > 0:
                tot = sum(px[s] for s in ("home", "draw", "away"))
                devig = {s: round(px[s] / tot, 4) for s in ("home", "draw", "away")}
            gh, ga = (fx["home_goals"], fx["away_goals"]) if ft else _score_at(
                conn, fx["api_id"], fx["home_api_id"], fx["away_api_id"], max(mn, 0))

            conn.execute(
                # UPSERT that FILLS, never replaces. The statement only carries the poly_*
                # columns, so INSERT OR REPLACE nulled everything it does not name — the
                # live-captured Kalshi book and the model probabilities recorded at that
                # minute (measured: 1,414 candlestick rows, 0 with a Kalshi price, against
                # 206 of 353 live rows that have one). The exit rule prefers the Kalshi bid,
                # so that loss silently moved backfilled matches onto Poly prices.
                # A live row was written AT that minute from the real book; candlestick is
                # a later reconstruction. Keep whichever value already exists and fill only
                # the gaps.
                "INSERT INTO milestone_snapshot "
                "(fixture_api_id, milestone, ts, elapsed, status_short, home_goals, away_goals, "
                " poly_home_ask, poly_home_bid, poly_draw_ask, poly_draw_bid, poly_away_ask, poly_away_bid, "
                " devig_home, devig_draw, devig_away, poly_token_home, poly_token_draw, poly_token_away, "
                " price_source) VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?, ?,?,?, ?) "
                "ON CONFLICT(fixture_api_id, milestone) DO UPDATE SET "
                " status_short=COALESCE(milestone_snapshot.status_short, excluded.status_short), "
                " home_goals=COALESCE(milestone_snapshot.home_goals, excluded.home_goals), "
                " away_goals=COALESCE(milestone_snapshot.away_goals, excluded.away_goals), "
                " poly_home_ask=COALESCE(milestone_snapshot.poly_home_ask, excluded.poly_home_ask), "
                " poly_home_bid=COALESCE(milestone_snapshot.poly_home_bid, excluded.poly_home_bid), "
                " poly_draw_ask=COALESCE(milestone_snapshot.poly_draw_ask, excluded.poly_draw_ask), "
                " poly_draw_bid=COALESCE(milestone_snapshot.poly_draw_bid, excluded.poly_draw_bid), "
                " poly_away_ask=COALESCE(milestone_snapshot.poly_away_ask, excluded.poly_away_ask), "
                " poly_away_bid=COALESCE(milestone_snapshot.poly_away_bid, excluded.poly_away_bid), "
                " devig_home=COALESCE(milestone_snapshot.devig_home, excluded.devig_home), "
                " devig_draw=COALESCE(milestone_snapshot.devig_draw, excluded.devig_draw), "
                " devig_away=COALESCE(milestone_snapshot.devig_away, excluded.devig_away), "
                " poly_token_home=COALESCE(milestone_snapshot.poly_token_home, excluded.poly_token_home), "
                " poly_token_draw=COALESCE(milestone_snapshot.poly_token_draw, excluded.poly_token_draw), "
                " poly_token_away=COALESCE(milestone_snapshot.poly_token_away, excluded.poly_token_away), "
                " price_source=CASE WHEN milestone_snapshot.kalshi_home_ask IS NOT NULL "
                "                    OR milestone_snapshot.p_model_home IS NOT NULL "
                "                   THEN milestone_snapshot.price_source ELSE excluded.price_source END",
                (fx["api_id"], code, datetime.fromtimestamp(when, timezone.utc).isoformat(),
                 max(mn, 0), "FT" if ft else None, gh, ga,
                 px["home"], px["home"], px["draw"], px["draw"], px["away"], px["away"],
                 devig["home"] if devig else None, devig["draw"] if devig else None,
                 devig["away"] if devig else None, tok_home, tok_draw, tok_away,
                 "candlestick"))
            n_rows += 1
        if verbose:
            print(f"  ✓ {hn} v {an} ({etd}) → {e['slug']}")

    conn.commit()
    # Knockout 2-way advance PRE price (for the price-track's advance entry ¢) — additive,
    # UPDATE-only, failure-tolerant (never blocks the 3-way backfill above).
    try:
        adv = backfill_advance_pre(conn, verbose=verbose)
    except Exception as e:
        adv = {"matched": 0, "updated": 0}
        if verbose:
            print(f"  advance PRE backfill skipped: {e}")
    if verbose and misses:
        print("  misses (left blank):")
        for m in misses:
            print(f"    – {m}")
    if verbose and resolver.unmapped:
        print(f"  unmapped venue labels ({len(resolver.unmapped)}) — curate into "
              f"data/priors/aliases_<comp>.json:")
        for u in resolver.unmapped:
            print(f"    ? {u}")
    return {"fixtures": len(fixtures), "matched": n_matched, "rows": n_rows, "misses": misses,
            "unmapped": list(resolver.unmapped), "advance_pre": adv}


# Round name → the ladder rung a club REACHES by winning this tie, per competition
# family. The WC version was a flat dict of one tournament's round names; club
# competitions each run their own ladder, so the rung is looked up here and then kept
# only if the competition's REGISTRY entry actually lists that market family
# (`comp.kalshi`). UCL carries the whole ladder (KXUCLRO16/RO8/RO4/FINALIST); UEL and
# UECL list only the qualification `advance`, so their KO rounds correctly resolve to
# nothing rather than to a UCL ticker.
_UEFA_LADDER = {
    "knockout round play-offs": "ro16", "knockout round play-off": "ro16",
    "round of 16": "ro8",
    "quarter-finals": "ro4", "quarterfinals": "ro4", "quarter finals": "ro4",
    "semi-finals": "finalist", "semifinals": "finalist", "semi finals": "finalist",
}


def _advance_round_key(comp, round_name: str | None) -> str | None:
    """Which per-club 'reaches X' market this fixture's winner enters, or None.

    Registry-driven (TRANSFORM_PLAN §3.0): the fixture must sit on an advance-capable
    stage, and the competition must actually list the rung. Only the Swiss-format UEFA
    competitions have a league phase to qualify INTO, so `advance` is theirs alone —
    the CONMEBOL/Argentine advance markets are per-MATCH tickers on Kalshi, not the
    per-club reach series this Poly Global backfill reads, so they return None here
    and their advance columns stay NULL rather than being filled from the wrong book.
    """
    from prediction_market_soccer.config.leagues import Stage, caps_for, stage_of
    if comp is None or not round_name:
        return None
    if not caps_for(comp.key, round_name).advance:
        return None
    rn = round_name.strip().lower()
    if stage_of(comp.key, round_name) == Stage.CUP_TWO_LEG and comp.kind == "swiss_ucl":
        if "qualif" in rn or "prelimin" in rn:
            return "advance" if comp.kalshi.get("advance") else None
    rung = _UEFA_LADDER.get(rn)
    return rung if (rung and comp.kalshi.get(rung)) else None


def backfill_advance_pre(conn=None, *, verbose: bool = False) -> dict:
    """Backfill the 2-way ADVANCE entry price into the PRE milestone row of SETTLED
    knockout matches that don't have it yet (the price-track marks the knockout argmax
    entry ¢ from these). Source: Polymarket Global's per-club "reach <rung>" YES price
    history (keyless), sampled ~5 min before kickoff. UPDATE-only (never REPLACE), so
    it adds the advance columns without touching the existing 3-way PRE prices.

    The knockout filter is the registry's ``caps_for``, not a round-name substring: a
    league round is called "Regular Season - 3" and a UCL league-phase round "League
    Stage - 3", so the WC module's ``round NOT LIKE '%group%'`` test called every
    single league match a knockout (bug class C1)."""
    from prediction_market_soccer.config.leagues import by_api_id as _comp_of
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.util.price_history import price_at
    from prediction_market_soccer.venues.polymarket_global.reader import PolymarketGlobalReader

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # Settled fixtures whose PRE row lacks the advance price; the stage test is applied
    # per row below (it needs the competition, which SQL has no view of).
    rows = conn.execute(
        "SELECT f.api_id, f.home_api_id, f.away_api_id, f.kickoff_ts, f.round, f.league_id "
        "FROM fixture f JOIN milestone_snapshot m "
        "  ON m.fixture_api_id=f.api_id AND m.milestone='PRE' "
        "WHERE f.status_short IN ('FT','AET','PEN') AND f.home_goals IS NOT NULL "
        "  AND m.poly_adv_home_ask IS NULL AND m.kalshi_adv_home_ask IS NULL").fetchall()
    if not rows:
        return {"matched": 0, "updated": 0}
    rd = PolymarketGlobalReader()
    idx_cache: dict[tuple[str, str], dict] = {}
    n_match = n_upd = 0
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        comp = _comp_of(r["league_id"])
        rk = _advance_round_key(comp, r["round"])
        if not (hi and ai and rk):
            continue
        n_match += 1
        ck = (comp.key, rk)
        if ck not in idx_cache:
            try:
                idx_cache[ck] = rd.reach_round_index(rk, comp_key=comp.key)
            except Exception as e:
                if verbose:
                    print(f"  reach_round_index({comp.key}, {rk}) failed: {e}")
                idx_cache[ck] = {}
        idx = idx_cache[ck]
        th, ta = idx.get(hi), idx.get(ai)
        if not (th and ta):
            continue
        try:
            ko_ts = int(datetime.fromisoformat(r["kickoff_ts"]).timestamp())
        except Exception:
            continue
        when = ko_ts - 5 * 60   # PRE ≈ kickoff − 5 min (matches _MILESTONES PRE)

        def _px(token):
            try:
                v, _ = price_at(rd.prices_history(token, fidelity=1), when, key="price")
                return float(v) if v is not None else None
            except Exception:
                return None

        ph, pa = _px(th), _px(ta)
        if ph is None and pa is None:
            continue
        conn.execute(
            "UPDATE milestone_snapshot SET poly_adv_home_ask=?, poly_adv_home_bid=?, "
            "poly_adv_away_ask=?, poly_adv_away_bid=? WHERE fixture_api_id=? AND milestone='PRE'",
            (ph, ph, pa, pa, r["api_id"]))
        n_upd += 1
        if verbose:
            print(f"  ✓ {hi} v {ai} ({comp.key} {rk}) advance PRE: home={ph} away={pa}")
    conn.commit()
    return {"matched": n_match, "updated": n_upd}


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill milestone price tracks from Poly Global history")
    ap.add_argument("--limit", type=int, default=None, help="cap number of fixtures")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-process all settled matches (not just those missing FT)")
    args = ap.parse_args()
    st = backfill(limit=args.limit, verbose=args.verbose, force=args.force)
    print(f"backfill: {st['matched']}/{st['fixtures']} matched, {st['rows']} milestone rows written, "
          f"{len(st['misses'])} misses, {len(st['unmapped'])} unmapped venue labels")


if __name__ == "__main__":
    main()
