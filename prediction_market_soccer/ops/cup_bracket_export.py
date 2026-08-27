"""ops/cup_bracket_export.py — the data-driven KNOCKOUT BRACKET product (C-18).

The club replacement for the WC module's ``ops/knockout_export.py``. That file is
FIFA-shaped to the bone (match numbers 73-104, Annex-C third-place slotting, one
fixed 32-leaf tree) and stays [闲置] per §2.2; nothing here imports it.

Why a fresh module instead of a rewrite: a club bracket is not a predetermined
tree that gets *filled in* — it is a list of ties the venue has actually drawn,
one round at a time, with rounds appearing weeks apart. So the export reads the
bracket out of the data (``tie`` rows for two-legged rounds, ``fixture`` rows for
single-match knockouts) rather than out of a hardcoded structure. The direct
consequence is the behaviour C-18 asks for: Libertadores/Sudamericana render a
real bracket today, and UCL/UEL/UECL start rendering their league-phase knockout
the moment the 2026-08-27/28 draw lands — no code change, no date switch.

Pricing is BORROWED, never re-derived (§C5 single-source rule):
  * two-legged tie      → ``model.ucl_phase._tie_win_prob`` (the same function the
    production KO-champion sim calls, so a tie's advance % on this card and the
    champion odds on the season card can never disagree);
  * single-match KO     → ``dixon_coles.knockout_advance_prob`` on the registry's
    neutral/ET flags for that round;
  * champion            → ``model.ucl_phase.ko_champion``.

Which competitions get a bracket comes from the registry ``kind``, never a name
test: a pure ``league`` has no knockout at all and is omitted from the payload —
that absence is what makes the frontend card hide itself for the big five + Brasileirão
(§3.0 caps matrix, "前端零赛制判断逻辑").

    python -m prediction_market_soccer.ops.cup_bracket_export → data/output/bracket.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Stage, active, caps_for, stage_of

_FINISHED = ("FT", "AET", "PEN")
_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")

# Registry kinds that can own a knockout bracket. "league" is deliberately absent.
_BRACKET_KINDS = ("swiss_ucl", "cup_two_leg", "league_playoffs")

# Champion board length — a KO tree of 4 alive ties has at most 8 live contenders,
# so this only truncates the long tail of an early-round bracket.
_CHAMPION_TOP = 12


# ── club identity ────────────────────────────────────────────────────────────
def _club_index(conn, comp) -> dict[int, dict]:
    """api_team_id → the frontend club ref (id/name/zh/logo) for one competition.

    club_registry is the curated source (carries zh + logo); the raw ``team`` row
    is the fallback so a club that entered via a mid-season qualifying tie still
    renders with a name instead of a numeric id.
    """
    out: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT club_id, api_team_id, name, zh, logo FROM club_registry WHERE comp=?",
        (comp.key,)):
        if r["api_team_id"] is None:
            continue
        out[int(r["api_team_id"])] = {"club_id": r["club_id"], "name": r["name"] or r["club_id"],
                                      "zh": r["zh"] or "", "logo": r["logo"]}
    return out


def _club_ref(idx: dict[int, dict], conn, api_id: int | None) -> dict | None:
    if api_id is None:
        return None
    hit = idx.get(int(api_id))
    if hit:
        return dict(hit)
    r = conn.execute("SELECT name, logo FROM team WHERE api_id=?", (api_id,)).fetchone()
    m = conn.execute("SELECT canonical_team_id FROM team_meta WHERE api_id=?", (api_id,)).fetchone()
    return {"club_id": (m["canonical_team_id"] if m else None),
            "name": (r["name"] if r else str(api_id)), "zh": "",
            "logo": (r["logo"] if r else None)}


# ── result reading ───────────────────────────────────────────────────────────
def _penalty_score(fx) -> tuple[int, int] | None:
    """(home, away) shootout score of a fixture, from the stored API payload."""
    if fx is None or not fx["raw_json"]:
        return None
    try:
        pen = (json.loads(fx["raw_json"]).get("score") or {}).get("penalty") or {}
    except Exception:
        return None
    h, a = pen.get("home"), pen.get("away")
    return (int(h), int(a)) if h is not None and a is not None else None


def _leg_row(fx, side_of_api: dict[int, str]) -> dict:
    """One leg, expressed in TIE sides (a/b) rather than home/away, so the frontend
    renders 'agg 2-1 to A' without re-deriving who hosted which leg."""
    home_side = side_of_api.get(fx["home_api_id"], "a")
    hg, ag = fx["home_goals"], fx["away_goals"]
    ga, gb = (hg, ag) if home_side == "a" else (ag, hg)
    return {
        "fixture_id": fx["api_id"],
        "kickoff": fx["kickoff_ts"],
        "status": fx["status_short"],
        "host": home_side,                      # which tie side hosted this leg
        "goals_a": None if ga is None else int(ga),
        "goals_b": None if gb is None else int(gb),
    }


def _tie_status(legs: list[dict], decided: bool) -> str:
    if decided:
        return "decided"
    if any((l["status"] or "") in _LIVE for l in legs):
        return "live"
    return "scheduled"


def _winner(agg_a, agg_b, *, decided: bool, decider_fx, side_of_api: dict[int, str],
            advanced_next: set[int], a_api: int, b_api: int) -> str | None:
    """Which side went through — data only, never an inference from the model.

    Three sources in decreasing directness: the aggregate, the deciding leg's
    shootout score, and (the belt for a level tie whose shootout the feed never
    filled in) membership of the NEXT round's ties, which is the venue's own
    answer to the question.
    """
    if not decided:
        return None
    if agg_a is not None and agg_b is not None and agg_a != agg_b:
        return "a" if agg_a > agg_b else "b"
    pen = _penalty_score(decider_fx)
    if pen and pen[0] != pen[1] and decider_fx is not None:
        home_side = side_of_api.get(decider_fx["home_api_id"], "a")
        away_side = "b" if home_side == "a" else "a"
        return home_side if pen[0] > pen[1] else away_side
    in_a, in_b = a_api in advanced_next, b_api in advanced_next
    if in_a != in_b:
        return "a" if in_a else "b"
    return None


# ── bracket assembly ─────────────────────────────────────────────────────────
def _two_leg_entries(conn, comp, idx, sm, cmap, tie_prob) -> list[dict]:
    entries = []
    fx_cache: dict[int, object] = {}

    def fx(fid):
        if fid not in fx_cache:
            fx_cache[fid] = conn.execute("SELECT * FROM fixture WHERE api_id=?", (fid,)).fetchone()
        return fx_cache[fid]

    ties = [dict(r) for r in conn.execute(
        # leg1_fixture_id order = the bracket order ucl_phase's KO sim pairs survivors
        # in, so the tree drawn here is the tree the champion odds were computed on.
        "SELECT * FROM tie WHERE comp=? ORDER BY round, leg1_fixture_id", (comp.key,))]
    # who is already drawn into a LATER round — the winner belt above
    by_round: dict[str, list[dict]] = {}
    for t in ties:
        by_round.setdefault(t["round"], []).append(t)

    for t in ties:
        a_api, b_api = t["team_a_api_id"], t["team_b_api_id"]
        side_of_api = {a_api: "a", b_api: "b"}
        legs = [_leg_row(f, side_of_api) for f in (fx(t["leg1_fixture_id"]), fx(t["leg2_fixture_id"])) if f]
        decided = bool(t["decided"])
        entries.append({
            "id": t["tie_key"], "round": t["round"], "kind": "two_leg",
            "a": _club_ref(idx, conn, a_api), "b": _club_ref(idx, conn, b_api),
            "legs": legs,
            "agg_a": t["agg_a"], "agg_b": t["agg_b"],
            "decided": decided,
            "status": _tie_status(legs, decided),
            "_a_api": a_api, "_b_api": b_api,
            "_decider": fx(t["leg2_fixture_id"]),
            "_side_of_api": side_of_api,
            "p_a": None if decided else tie_prob(t),
            "neutral": False,
        })
    return entries


def _single_entries(conn, comp, idx, sm) -> list[dict]:
    """Single-match knockouts: cup finals and the Argentine playoff bracket."""
    from prediction_market_soccer.model.dixon_coles import knockout_advance_prob

    cfg = sm.cfg if sm is not None else None
    entries = []
    for fx in conn.execute(
        "SELECT * FROM fixture WHERE league_id=? AND season=? ORDER BY kickoff_ts, api_id",
        (comp.api_football_id, comp.season)):
        if stage_of(comp.key, fx["round"]) != Stage.CUP_SINGLE:
            continue
        a_api, b_api = fx["home_api_id"], fx["away_api_id"]
        caps = caps_for(comp.key, fx["round"])
        side_of_api = {a_api: "a", b_api: "b"}
        leg = _leg_row(fx, side_of_api)
        decided = (fx["status_short"] or "") in _FINISHED
        p_a = None
        if not decided and sm is not None:
            ra = _club_ref(idx, conn, a_api) or {}
            rb = _club_ref(idx, conn, b_api) or {}
            ca, cb = ra.get("club_id"), rb.get("club_id")
            if ca in sm.ratings and cb in sm.ratings:
                lam_h, lam_a = sm.pair_lambdas(ca, cb, knockout=True, neutral=caps.neutral)
                p_a = round(float(knockout_advance_prob(
                    lam_h, lam_a, rho=cfg.dc_rho, kmax=cfg.score_matrix_kmax,
                    et_fraction=cfg.extra_time_fraction, penalty_home_edge=0.5)), 5)
        entries.append({
            "id": f"{comp.key}:{fx['round']}:{fx['api_id']}", "round": fx["round"], "kind": "single",
            "a": _club_ref(idx, conn, a_api), "b": _club_ref(idx, conn, b_api),
            "legs": [leg],
            "agg_a": leg["goals_a"], "agg_b": leg["goals_b"],
            "decided": decided,
            "status": _tie_status([leg], decided),
            "_a_api": a_api, "_b_api": b_api,
            "_decider": fx,
            "_side_of_api": side_of_api,
            "p_a": p_a,
            "neutral": bool(caps.neutral),
        })
    return entries


def _round_sort_key(entries: list[dict]) -> str:
    """Rounds are ordered by when they are actually PLAYED, not by name — the only
    ordering that survives a competition inventing a round name we have no table for
    ("Qualification Round 3", "Apertura - Round of 16")."""
    ks = [l["kickoff"] for e in entries for l in e["legs"] if l["kickoff"]]
    return min(ks) if ks else "9999"


def _league_block(conn, comp, *, n_sims: int | None) -> dict:
    idx = _club_index(conn, comp)
    sm = None
    try:
        from prediction_market_soccer.model.strength_cache import cached_strength
        sm = cached_strength(conn, comp.key)
    except Exception as e:  # noqa: BLE001 — a bracket without prices still beats no bracket
        print(f"[cup_bracket:{comp.key}] strength unavailable ({e}) — structure only")

    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    def tie_prob(t: dict):
        """Advance % for one undecided tie — the production tie pricer verbatim.
        ``_tie_win_prob`` is ucl_phase-private on purpose (it is the KO sim's own
        step); importing it is exactly the point, since a second implementation
        here is how this card would start disagreeing with the champion odds."""
        if sm is None:
            return None
        try:
            from prediction_market_soccer.model.ucl_phase import _tie_win_prob
            got = _tie_win_prob(conn, comp, sm, t, cmap)
        except Exception as e:  # noqa: BLE001
            print(f"[cup_bracket:{comp.key}] tie {t['tie_key']}: {e}")
            return None
        return None if got is None else round(float(got[2]), 5)

    entries = _two_leg_entries(conn, comp, idx, sm, cmap, tie_prob) + _single_entries(conn, comp, idx, sm)

    grouped: dict[str, list[dict]] = {}
    for e in entries:
        grouped.setdefault(e["round"], []).append(e)
    ordered = sorted(grouped.items(), key=lambda kv: _round_sort_key(kv[1]))

    # A club drawn into a later round has, by definition, won its earlier one.
    later_teams: list[set[int]] = []
    acc: set[int] = set()
    for _rnd, es in reversed(ordered):
        later_teams.append(set(acc))
        for e in es:
            acc.update({e["_a_api"], e["_b_api"]} - {None})
    later_teams.reverse()

    rounds = []
    for (rnd, es), advanced_next in zip(ordered, later_teams):
        for e in es:
            e["winner"] = _winner(e["agg_a"], e["agg_b"], decided=e["decided"],
                                  decider_fx=e["_decider"], side_of_api=e["_side_of_api"],
                                  advanced_next=advanced_next,
                                  a_api=e["_a_api"], b_api=e["_b_api"])
            for k in ("_a_api", "_b_api", "_decider", "_side_of_api"):
                e.pop(k, None)
        rounds.append({
            "round": rnd,                       # raw venue round name; frontend stageLabel() translates
            "stage": stage_of(comp.key, rnd).value,
            "n_ties": len(es),
            "open": any(not e["decided"] for e in es),
            "ties": es,
        })

    champion = None
    if sm is not None and comp.kind != "swiss_ucl":
        try:
            from prediction_market_soccer.model.ucl_phase import ko_champion
            kw = {"n_sims": n_sims} if n_sims else {}
            pc = ko_champion(conn, comp.key, sm, **kw)
        except Exception as e:  # noqa: BLE001
            print(f"[cup_bracket:{comp.key}] champion sim unavailable ({e})")
            pc = None
        if pc:
            name_by_cid = {v["club_id"]: v for v in idx.values() if v.get("club_id")}
            champion = [{"club_id": cid,
                         "name": (name_by_cid.get(cid) or {}).get("name", cid),
                         "zh": (name_by_cid.get(cid) or {}).get("zh", ""),
                         "logo": (name_by_cid.get(cid) or {}).get("logo"),
                         "p": p}
                        for cid, p in sorted(pc.items(), key=lambda kv: -kv[1])[:_CHAMPION_TOP]]

    # Empty states carry WHY, and the two reasons are genuinely different: a swiss
    # competition before its draw HAS no bracket to draw yet (UCL/UEL/UECL until
    # 2026-08-27/28), while a cup between rounds has one that simply is not published.
    if rounds:
        state = "ok"
    elif comp.kind == "swiss_ucl":
        state = "pending_draw"
    else:
        state = "pending_bracket"

    # A swiss competition mid-August is a genuine hybrid: its QUALIFYING bracket is
    # complete and worth showing, while the 36-club league phase — and the knockout
    # that hangs off it — waits on the draw. Reporting only `state: ok` would read as
    # "this is the whole bracket". The test is the data (are league-phase fixtures on
    # the calendar?), never the date or the round name, so it flips by itself.
    n_phase = sum(1 for r in conn.execute(
        "SELECT round FROM fixture WHERE league_id=? AND season=?",
        (comp.api_football_id, comp.season))
        if stage_of(comp.key, r["round"]) == Stage.LEAGUE)

    return {
        "league": comp.key, "name": comp.name, "zh": comp.zh, "kind": comp.kind,
        "state": state,
        "league_phase": {"drawn": n_phase > 0, "n_fixtures": n_phase} if comp.kind == "swiss_ucl" else None,
        "et_in_ties": comp.et_in_ties,
        "n_rounds": len(rounds),
        "rounds": rounds,
        "champion": champion,
    }


def build(conn=None, *, n_sims: int | None = None) -> dict:
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    leagues = []
    for comp in active():
        if comp.kind not in _BRACKET_KINDS:
            continue          # pure league: no knockout exists, so no card (§3.0)
        try:
            leagues.append(_league_block(conn, comp, n_sims=n_sims))
        except Exception as e:  # noqa: BLE001 — one broken comp must not blank the card
            print(f"[cup_bracket:{comp.key}] skipped: {e}")
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "leagues": leagues,
        "note": ("Bracket read from stored draws (tie pairings + knockout fixtures), not from a "
                 "fixed tree: a round appears here as soon as the venue draws it. Advance % of an "
                 "undecided tie = the production two-leg pricer (ucl_phase/dixon_coles) on the live "
                 "aggregate, per-competition ET-vs-pens rule; for a tie whose leg is in play it is a "
                 "PRE-MATCH number (the in-play card is the live source). Champion % = the KO-tree "
                 "Monte-Carlo, which prices the current round from the real tie state and "
                 "approximates later rounds as neutral one-off matches (v1 disclosure)."),
    }


def main() -> None:
    payload = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "bracket.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for lg in payload["leagues"]:
        if lg["state"] != "ok":
            print(f"  {lg['league']:14s} {lg['state']}")
            continue
        openr = [r["round"] for r in lg["rounds"] if r["open"]]
        top = (lg["champion"] or [{}])[0]
        print(f"  {lg['league']:14s} {lg['n_rounds']} rounds, open={openr or '-'}"
              + (f", fav={top.get('name')} {top.get('p', 0):.1%}" if top.get("name") else ""))
    print(f"bracket.json → {len(payload['leagues'])} competitions")


if __name__ == "__main__":
    main()
