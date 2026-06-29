"""ops/backfill_milestones.py — reconstruct milestone price tracks for already-played
matches (plan 18 §2.4c).

The live `milestone_snapshot` capture only sees matches going forward. To give the
PriceTrack view history from match 1, we backfill the 6 milestones (PRE / T15 / T30
/ HT / T60 / T75 / FT) for every finished fixture using Polymarket Global's
PERSISTENT single-match events (`fifwc-{h}-{a}-{date}`, closed/archived still
queryable) + the public CLOB prices-history series.

For each finished fixture:
  * locate its Poly Global event by ET date + team-name match (no fragile code map);
  * pull each 3-way outcome token's per-minute price series once;
  * sample the price at each milestone's wall-clock (kickoff + minute);
  * compute the score at that minute from goal events; FT uses the real result
    (winner outcome settles 100¢ / 0¢);
  * de-vig the three outcome prices into a market probability;
  * INSERT OR IGNORE into milestone_snapshot (price_source='candlestick').

Idempotent + re-runnable; missing data is left as NULL (honest), never fabricated.

    python -m prediction_market.ops.backfill_milestones [--limit N] [--verbose]
"""
from __future__ import annotations

import argparse
import difflib
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prediction_market.config import CONFIG

ET = ZoneInfo("America/New_York")
_FINISHED = ("FT", "AET", "PEN")

# Milestone → minute offset from kickoff used to SAMPLE the market series.
_MILESTONES = [("PRE", -5), ("T15", 15), ("T30", 30), ("HT", 47), ("T60", 60), ("T75", 75)]

# Team-name aliases: our API-Football name → tokens that may appear in Poly titles.
_ALIASES = {
    "czechia": "czech", "usa": "united states", "ivory coast": "cote divoire",
    "cape verde islands": "cabo verde", "south korea": "korea", "turkiye": "turkey",
    "ir iran": "iran", "iran": "ir iran", "republic of ireland": "ireland",
}


def _norm(s: str) -> str:
    # Transliterate accents (ô→o, ü→u, ç→c) before stripping, so "Côte d'Ivoire"
    # and "Türkiye" normalise to ascii rather than losing the accented letter.
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    return re.sub(r"[^a-z]", "", s)


def _name_score(our_name: str, candidate: str) -> float:
    """0..1 similarity between our team name and one outcome title (alias-aware)."""
    base = _norm(our_name)
    cn = _norm(candidate)
    if not base or not cn:
        return 0.0
    if base in cn or cn in base:
        return 1.0
    alias = _norm(_ALIASES.get(our_name.lower(), ""))
    if alias and (alias in cn or cn in alias):
        return 1.0
    return difflib.SequenceMatcher(None, base, cn).ratio()


def _name_match(our_name: str, candidate_names: list[str]) -> bool:
    return any(_name_score(our_name, c) >= 0.72 for c in candidate_names)


def _event_score(hn: str, an: str, outs: list[str]) -> float:
    """How well a fixture's two teams match an event's two outcome titles (best
    bipartite-ish pairing; 0 if either side has no decent match)."""
    if len(outs) < 2:
        return 0.0
    h = max(_name_score(hn, c) for c in outs)
    a = max(_name_score(an, c) for c in outs)
    if h < 0.72 or a < 0.72:
        return 0.0
    return h + a


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
    from prediction_market.ingest import store
    from prediction_market.util.price_history import price_at
    from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader

    conn = conn or store.init_db()
    tname = {r["api_id"]: r["name"] for r in conn.execute("SELECT api_id, name FROM team")}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    fixtures_all = conn.execute(
        "SELECT MIN(kickoff_ts) lo, MAX(kickoff_ts) hi FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(",".join("?" * len(_FINISHED))),
        _FINISHED).fetchone()
    # Window the Gamma query to the finished-match date range (±2d slack) so all
    # single-match events are returned reliably instead of paging through noise.
    from datetime import timedelta
    lo = (datetime.fromisoformat(fixtures_all["lo"]) - timedelta(days=2)).date().isoformat() if fixtures_all["lo"] else None
    hi = (datetime.fromisoformat(fixtures_all["hi"]) + timedelta(days=2)).date().isoformat() if fixtures_all["hi"] else None

    rd = PolymarketGlobalReader()
    events = rd.list_wc_match_events(end_date_min=lo, end_date_max=hi)
    # Index events by date → list of (event, [outcome names excluding draw]).
    by_date: dict[str, list] = {}
    for e in events:
        outs = [n for n in e["teams"] if "draw" not in n.lower()]
        by_date.setdefault(e["date"], []).append((e, outs))

    fixtures = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts "
        "FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))), _FINISHED).fetchall()
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
        ko = datetime.fromisoformat(fx["kickoff_ts"])
        etd = ko.astimezone(ET).date().isoformat()
        ko_ts = int(ko.timestamp())

        # Locate the Poly event: among same-ET-date events, take the BEST-scoring
        # pairing of our two teams to its two outcome titles (avoids first-match
        # false positives when several matches share a date).
        match = None
        best_score = 0.0
        for e, outs in by_date.get(etd, []):
            sc = _event_score(hn, an, outs)
            if sc > best_score:
                best_score, match = sc, (e, outs)
        if not match:
            misses.append(f"{hn} v {an} ({etd})")
            continue
        e, outs = match
        n_matched += 1

        # Map our home/away/draw to this event's tokens + fetch price series once each.
        def _token_for(team_name):
            for title, tok in e["teams"].items():
                if "draw" in title.lower():
                    continue
                if _name_match(team_name, [title]):
                    return tok
            return None

        tok_home, tok_away = _token_for(hn), _token_for(an)
        tok_draw = next((t for n, t in e["teams"].items() if "draw" in n.lower()), None)
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
                # REPLACE: candlestick history is the authoritative per-minute
                # reconstruction, so it heals any stale/garbled live row it overwrites.
                "INSERT OR REPLACE INTO milestone_snapshot "
                "(fixture_api_id, milestone, ts, elapsed, status_short, home_goals, away_goals, "
                " poly_home_ask, poly_home_bid, poly_draw_ask, poly_draw_bid, poly_away_ask, poly_away_bid, "
                " devig_home, devig_draw, devig_away, poly_token_home, poly_token_draw, poly_token_away, "
                " price_source) VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?, ?,?,?, ?)",
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
    return {"fixtures": len(fixtures), "matched": n_matched, "rows": n_rows, "misses": misses,
            "advance_pre": adv}


# Knockout match → the round a team reaches by WINNING it (the "advance" market key).
_NEXT_ROUND = {"round of 32": "r16", "round of 16": "qf", "quarter-finals": "sf",
               "quarterfinals": "sf", "quarter finals": "sf",
               "semi-finals": "final", "semifinals": "final", "semi finals": "final"}


def backfill_advance_pre(conn=None, *, verbose: bool = False) -> dict:
    """Backfill the 2-way ADVANCE entry price into the PRE milestone row of SETTLED
    knockout matches that don't have it yet (the price-track marks the knockout argmax
    entry ¢ from these). Source: Polymarket Global's per-team "reach <next round>" YES
    price history (keyless), sampled ~5 min before kickoff. UPDATE-only (never REPLACE),
    so it adds the advance columns without touching the existing 3-way PRE prices."""
    from prediction_market.ingest import store
    from prediction_market.util.price_history import price_at
    from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    # Settled knockout fixtures whose PRE row lacks the advance price.
    rows = conn.execute(
        "SELECT f.api_id, f.home_api_id, f.away_api_id, f.kickoff_ts, f.round "
        "FROM fixture f JOIN milestone_snapshot m "
        "  ON m.fixture_api_id=f.api_id AND m.milestone='PRE' "
        "WHERE f.status_short IN ('FT','AET','PEN') AND f.home_goals IS NOT NULL "
        "  AND lower(COALESCE(f.round,'')) NOT LIKE '%group%' "
        "  AND m.poly_adv_home_ask IS NULL AND m.kalshi_adv_home_ask IS NULL").fetchall()
    if not rows:
        return {"matched": 0, "updated": 0}
    rd = PolymarketGlobalReader()
    idx_cache: dict[str, dict] = {}
    n_match = n_upd = 0
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        rk = _NEXT_ROUND.get((r["round"] or "").strip().lower())
        if not (hi and ai and rk):
            continue
        n_match += 1
        if rk not in idx_cache:
            try:
                idx_cache[rk] = rd.reach_round_index(rk)
            except Exception as e:
                if verbose:
                    print(f"  reach_round_index({rk}) failed: {e}")
                idx_cache[rk] = {}
        idx = idx_cache[rk]
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
            print(f"  ✓ {hi} v {ai} ({rk}) advance PRE: home={ph} away={pa}")
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
          f"{len(st['misses'])} misses")


if __name__ == "__main__":
    main()
