"""In-play opportunity finder (plan 04 §4c, 08, 15) — per-minute, intra-game stats.

For every LIVE match, each poll:
  1. compute the **live fair price** from intra-game stats — state-dependent
     Poisson on (minute, score, red cards) shaded by live **xG** (model/inplay.py);
  2. compare to live market prices on each venue (pluggable quote sources):
     * **relative value** — fair vs a venue's de-vigged price (net of fee);
     * **cross-venue lock arb** — buy cheap-venue YES + expensive-venue NO when
       two TRADABLE venues quote the same outcome (cross_venue.evaluate_lock);
  3. emit the in-play **tactics** (draw take-profit, convergence, momentum).

Quote sources are functions ``venue -> {outcome: ask}`` for a given match; they
populate when the single-match markets go live (Kalshi per-match, Polymarket US
fwc-*, Global match events). Until then the finder still outputs fair prices +
tactics so the desk sees the model's live view.

Read-only: opportunities flow to the gated executor + the hard $1 test cap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.model.inplay import LiveMatchProb, live_match_prob
from prediction_market_soccer.strategy.cross_venue import evaluate_lock
from prediction_market_soccer.strategy.edge import compute_edge
from prediction_market_soccer.strategy.inplay_tactics import (
    convergence_take_profit,
    corner_total_signal,
    dormant_explosion,
    draw_trade_signal,
    favourite_comeback,
    finishing_uplift_over,
    formation_fragility,
    goal_overreaction_fade,
    knockout_late_draw,
    late_goal_bias,
    live_momentum_from_store,
    live_odds_crossval,
    totals_time_decay,
    lone_threat_removed,
    model_overshoot_take_profit,
    possession_trap_fade,
    red_card_value,
    xg_dominance_chase,
)

_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


def _ask(q) -> float | None:
    """Ask from a quote: {'ask','bid'} dict, or a plain float treated as the ask."""
    if isinstance(q, dict):
        return q.get("ask")
    return q


def _bid(q) -> float | None:
    if isinstance(q, dict):
        return q.get("bid", q.get("ask"))
    return q


@dataclass
class Opportunity:
    fixture_id: int
    match: str
    minute: int
    score: str
    kind: str            # "relative_value" | "lock_arb" | "tactic"
    side: str
    venue: str
    fair: float | None
    market: float | None
    edge: float | None
    action: str
    reason: str          # English fallback (numbers baked in)
    # i18n: stable template key + raw args so the frontend renders the localized
    # backbone with live numbers spliced in (5 languages). reason stays the fallback.
    reason_key: str = ""
    reason_args: dict = field(default_factory=dict)
    # Which question the row answers, so the desk can group instead of seeing a held-position
    # exit next to a new-entry buy and reading them as a contradiction. Never drops a signal.
    intent: str = "entry"   # "manage" (sell/lock a HELD position) | "entry" | "event"


# reason_key → intent. Everything not listed defaults to "entry" (relative_value, lock_arb,
# momentum, xg_chase, draw_entry, dormant/finishing/formation/late OVER, ko_draw, crossval).
_MANAGE_KEYS = {"overshoot_lock", "convergence_lock", "convergence_take",
                "draw_lock", "draw_lock_held", "under_lock", "under_take", "late_goal_hold"}
_EVENT_KEYS = {"fade_now", "fade_ago", "comeback", "red_card", "possession_trap", "lone_threat"}


def _intent_for(reason_key: str) -> str:
    if reason_key in _MANAGE_KEYS:
        return "manage"
    if reason_key in _EVENT_KEYS:
        return "event"
    return "entry"


def _is_knockout(round_name) -> bool:
    # C1: registry-driven (union over enabled comps); league rounds never count.
    from prediction_market_soccer.model.match_pricing import is_knockout as _rk
    return _rk(round_name)


def _favourite_basis(conn, prior, sm, hi: str, ai: str) -> tuple[str, float, str]:
    """Pre-match favourite as an EXPLICIT, explainable combination of the two views the
    desk sees (Part 2): TEAM STRENGTH (the 球队强度 view — pure rating, no recent-form
    blend) z-scored, PLUS RECENT FORM (the 近期状态 view — the same form_z form.json
    ranks by). favourite = higher (z_strength + z_form). Recomputed every refresh, so a
    strong team in poor form can lose favourite status to an in-form one.

    Returns (fav_side, fav_win_prob, basis) where basis explains WHY:
      * ``aligned``           — strength and form agree (strong AND in form)
      * ``strong_poor_form``  — favourite is the stronger team but worse recent form
      * ``in_form_underdog``  — favourite is weaker by strength but in better form
    """
    from prediction_market_soccer.model.form_strength import form_index
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.strength import build_strength

    # Both of these are FIXTURE-INDEPENDENT — the same 399-club fit and the same form
    # index for every match in the poll — yet they were rebuilt per fixture per cycle.
    # Measured at ~4.0s and ~0.4s each; with several matches live that alone overran
    # the 60s poll interval, which is how a real-time loop silently stops being
    # real-time. Cached for the life of the process; the live loop is a fresh process
    # each minute, so the data is never more than one cycle stale.
    ratings = _cached_ratings(prior)
    vals = list(ratings.values()) or [0.0]
    mu = sum(vals) / len(vals)
    sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
    z_str = {t: (ratings.get(t, mu) - mu) / sd for t in (hi, ai)}
    fi = _cached_form(conn)
    z_form = {t: (fi[t].form_z if t in fi else 0.0) for t in (hi, ai)}
    comb = {t: z_str[t] + z_form[t] for t in (hi, ai)}      # explicit equal-weight z-sum

    fav = hi if comb[hi] >= comb[ai] else ai
    fav_side = "home" if fav == hi else "away"
    mp = price_match(sm, hi, ai)                            # win prob from the live model
    prob = mp.p_home if fav_side == "home" else mp.p_away

    strength_fav = hi if z_str[hi] >= z_str[ai] else ai
    form_fav = hi if z_form[hi] >= z_form[ai] else ai
    if strength_fav == form_fav:
        basis = "aligned"
    elif fav == strength_fav:
        basis = "strong_poor_form"
    else:
        basis = "in_form_underdog"
    return fav_side, prob, basis


def _last_event(conn, fixture_row, etype: str, hi: str, ai: str, *, detail_like: str | None = None):
    """Most recent event of a type (Goal / Card) for a live fixture → (side, minute).
    Own goals are excluded for 'Goal'. Returns (None, None) if none."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    q = ("SELECT team_api_id, minute, detail FROM fixture_event "
         "WHERE fixture_api_id=? AND type=? ")
    params: list = [fixture_row["api_id"], etype]
    if detail_like:
        q += "AND detail LIKE ? "
        params.append(detail_like)
    if etype == "Goal":
        q += "AND (detail IS NULL OR detail != 'Own Goal') "
    q += "ORDER BY minute DESC, seq DESC LIMIT 1"
    r = conn.execute(q, params).fetchone()
    if not r:
        return (None, None)
    side = "home" if cmap.get(r["team_api_id"]) == hi else ("away" if cmap.get(r["team_api_id"]) == ai else None)
    return (side, r["minute"])


def _red_rescinded(conn, fixture_row, card_minute: int | None) -> bool:
    """True if a VAR review overturned the red card around ``card_minute``.

    The feed keeps the original Card row and adds a Var row rather than deleting, so
    a red-card tactic that only reads the Card row keeps trading a team it thinks is
    down to ten men."""
    if card_minute is None:
        return False
    r = conn.execute(
        "SELECT 1 FROM fixture_event WHERE fixture_api_id=? AND type='Var' "
        "AND detail LIKE '%Red card cancelled%' AND minute BETWEEN ? AND ? LIMIT 1",
        (fixture_row["api_id"], card_minute - 2, card_minute + 6)).fetchone()
    return r is not None


def _goal_on_board(conn, fixture_row, hi: str, ai: str, side: str, gh: int, ga: int) -> bool:
    """True if `side`'s latest goal event is actually reflected on the scoreboard.

    A goal can be disallowed (VAR / offside) or corrected by the feed AFTER its
    event row is written: the fixture_event row stays, but home_goals/away_goals
    revert. We count that side's (non-own-goal) goal events and require it to be
    ≤ the goals showing on the board — otherwise the latest event is a phantom
    goal and the goal-driven tactics must ignore it.
    """
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    target = hi if side == "home" else ai
    n_events = 0
    for r in conn.execute(
        "SELECT team_api_id, detail FROM fixture_event WHERE fixture_api_id=? AND type='Goal'",
        (fixture_row["api_id"],),
    ):
        if (r["detail"] or "") == "Own Goal":
            continue
        if cmap.get(r["team_api_id"]) == target:
            n_events += 1
    on_board = gh if side == "home" else ga
    return n_events <= on_board


def _team_live_stats(conn, fixture_row) -> dict:
    """Per-side live xG / possession / shots from fixture_stats → {'home':{...},'away':{...}}.
    Keyed by side so the data-mined tactics don't need team-id plumbing."""
    rows = {r["team_api_id"]: r for r in conn.execute(
        "SELECT team_api_id, xg, possession, shots_total, corners FROM fixture_stats "
        "WHERE fixture_api_id=?", (fixture_row["api_id"],))}
    out = {}
    for side, tid in (("home", fixture_row["home_api_id"]), ("away", fixture_row["away_api_id"])):
        r = rows.get(tid)
        out[side] = {"xg": r["xg"] if r else None,
                     "possession": r["possession"] if r else None,
                     "shots": r["shots_total"] if r else None,
                     "corners": r["corners"] if r else None}
    return out


def _formations(conn, fixture_row) -> tuple[str | None, str | None]:
    """(home_formation, away_formation) from the lineup table (pulled pre/at kickoff)."""
    f = {r["team_api_id"]: r["formation"] for r in conn.execute(
        "SELECT team_api_id, formation FROM lineup WHERE fixture_api_id=?", (fixture_row["api_id"],))}
    return f.get(fixture_row["home_api_id"]), f.get(fixture_row["away_api_id"])


def _lone_threat(conn, fixture_row, *, min_team_shots: int = 5):
    """Detect a side whose shots funnel through ONE player (≥50% share) who has since
    LEFT the pitch (subbed off / sent off). Returns (side, player_name, share, removed)
    for the first such side, else (None, None, None, False). Live player stats come from
    fixture_player_stats; departures from fixture_event (Subst / Red Card)."""
    # shots per (team, player) and the team totals.
    per = {}
    for r in conn.execute(
        "SELECT team_api_id, player_api_id, player_name, shots_total FROM fixture_player_stats "
        "WHERE fixture_api_id=? AND shots_total IS NOT NULL AND shots_total>0", (fixture_row["api_id"],)):
        per.setdefault(r["team_api_id"], []).append(r)
    # players who left the pitch: substituted off (assist holds the incomer; player is the one off)
    # or red-carded.
    gone = set()
    for r in conn.execute(
        "SELECT player_api_id, assist_api_id, type, detail FROM fixture_event "
        "WHERE fixture_api_id=? AND (type='subst' OR (type='Card' AND detail LIKE '%Red%'))",
        (fixture_row["api_id"],)):
        if r["type"] == "subst":
            gone.add(r["assist_api_id"])           # API-Football subst: player=IN, assist=OFF
        else:
            gone.add(r["player_api_id"])           # red card: the carded player leaves
    for side, tid in (("home", fixture_row["home_api_id"]), ("away", fixture_row["away_api_id"])):
        rows = per.get(tid, [])
        total = sum(x["shots_total"] for x in rows)
        if total < min_team_shots:
            continue
        top = max(rows, key=lambda x: x["shots_total"])
        share = top["shots_total"] / total
        if share >= 0.50 and top["player_api_id"] in gone:
            return side, top["player_name"], share, True
    return None, None, None, False


_RATINGS_CACHE: dict = {}
_FORM_CACHE: dict = {}


def _cached_ratings(prior) -> dict:
    # Keyed on the prior's IDENTITY IN THE DOMAIN (competition + as-of), not on
    # id(prior): CPython reuses the address of a freed object, so an id key can hand
    # one competition's ratings to another that happens to land on the same address.
    key = (getattr(prior, "league", None), getattr(prior, "as_of", None))
    if key not in _RATINGS_CACHE:
        from prediction_market_soccer.model.strength import build_strength
        _RATINGS_CACHE.clear()          # one prior per process; never grow unbounded
        _RATINGS_CACHE[key] = build_strength(prior).ratings
    return _RATINGS_CACHE[key]


def _cached_form(conn) -> dict:
    if "idx" not in _FORM_CACHE:
        from prediction_market_soccer.model.form_strength import form_index
        _FORM_CACHE["idx"] = form_index(conn)
    return _FORM_CACHE["idx"]


def _held_entry(conn, fixture_api_id: int, side: str) -> float | None:
    """Our entry price on ``side`` of this fixture, or None when we hold nothing.

    The take-profit tactics (`draw_lock_held`, `under_take`) read differently with a
    position behind them: with an entry they quote a realised basis, without one they
    can only say "sell if held". Under the paper gate no order is ever sent, so this
    returns None today and those tactics degrade to their basis-free branch — the
    honest reading. When execution is enabled the same lookup starts returning fills.

    The fixture is matched on a WORD BOUNDARY, not with a bare LIKE '%id%': the venue
    market key embeds the fixture id among other digits, so fixture 123 matched a key
    for fixture 1234 and could have attributed someone else's entry to this match.
    """
    try:
        rows = conn.execute(
            "SELECT market_key, ask FROM signal WHERE action='BUY' AND side=? "
            "ORDER BY run_ts DESC", (side,)).fetchall()
    except Exception:
        return None
    import re
    pat = re.compile(rf"(?<!\d){fixture_api_id}(?!\d)")
    for r in rows:
        if r["ask"] is None or not r["market_key"]:
            continue
        if pat.search(str(r["market_key"])):
            return float(r["ask"])
    return None


def _live_book_prob(conn, fixture_row) -> dict:
    """Most-recent de-vigged bookmaker 1X2 implied probs from match_odds (API-Football
    Odds, pre-match seed + In-Play updates) → {'home','draw','away'} or {} if none."""
    r = conn.execute(
        "SELECT p_home, p_draw, p_away FROM match_odds WHERE fixture_api_id=? "
        "ORDER BY fetched_at DESC LIMIT 1", (fixture_row["api_id"],)).fetchone()
    if not r or r["p_home"] is None:
        return {}
    s = (r["p_home"] or 0) + (r["p_draw"] or 0) + (r["p_away"] or 0)
    if s <= 0:
        return {}
    return {"home": r["p_home"] / s, "draw": r["p_draw"] / s, "away": r["p_away"] / s}


def live_fair(conn, sm, fixture_row) -> tuple[LiveMatchProb, dict]:
    """xG-shaded live fair prices for one fixture (intra-game stats → price)."""
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    hi, ai = cmap.get(fixture_row["home_api_id"]), cmap.get(fixture_row["away_api_id"])
    lam_h, lam_a = sm.pair_lambdas(hi, ai)
    xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
        "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fixture_row["api_id"],))}
    reds = {r["team_api_id"]: r["n"] for r in conn.execute(
        "SELECT team_api_id, COUNT(*) n FROM fixture_event WHERE fixture_api_id=? "
        "AND type='Card' AND detail LIKE '%Red%' GROUP BY team_api_id", (fixture_row["api_id"],))}
    minute = fixture_row["elapsed"] or 0
    gh, ga = fixture_row["home_goals"] or 0, fixture_row["away_goals"] or 0
    lp = live_match_prob(
        lam_h, lam_a, minute, gh, ga,
        red_home=reds.get(fixture_row["home_api_id"], 0), red_away=reds.get(fixture_row["away_api_id"], 0),
        xg_home=xg.get(fixture_row["home_api_id"]), xg_away=xg.get(fixture_row["away_api_id"]),
    )
    fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}
    return lp, fair


def find_opportunities(conn=None, sm=None, *, quote_sources: dict | None = None,
                       fee: float = 0.01, theta: float | None = None) -> list[dict]:
    """All in-play opportunities across live matches (relative value + arb + tactics)."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    conn = conn or store.init_db()
    prior = load_prior()
    sm = sm or build_strength(prior)
    theta = CONFIG.risk.min_net_edge if theta is None else theta
    quote_sources = quote_sources or {}
    name = {t.team_id: t.name for t in prior.teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    live = conn.execute(
        "SELECT api_id, league_id, home_api_id, away_api_id, home_goals, away_goals, elapsed, round "
        "FROM fixture WHERE status_short IN ({})".format(",".join("?" * len(_LIVE))), _LIVE).fetchall()
    opps: list[Opportunity] = []
    for fx in live:
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            continue
        m = f"{name.get(hi, hi)} vs {name.get(ai, ai)}"
        minute = fx["elapsed"] or 0
        gh, ga = fx["home_goals"] or 0, fx["away_goals"] or 0
        score = f"{gh}-{ga}"
        lp, fair = live_fair(conn, sm, fx)
        # Totals (OVER/UNDER 2.5) fair from the live model → enables totals relative
        # value + the finishing-uplift signal once a totals market is quoted.
        p_over25 = lp.p_over_total.get(2.5)
        if p_over25 is not None:
            fair["over"], fair["under"] = p_over25, 1.0 - p_over25
        # Live market quotes per venue (3-way + KXWCTOTAL / tsc totals), fetched once.
        quotes = {v: fn(fx["api_id"]) for v, fn in quote_sources.items()}

        def _best_ask(side: str):
            xs = [_ask(q.get(side)) for q in quotes.values() if q and q.get(side) is not None]
            xs = [a for a in xs if a is not None]
            return min(xs) if xs else None

        def _best_bid(side: str):
            xs = [_bid(q.get(side)) for q in quotes.values() if q and q.get(side) is not None]
            xs = [a for a in xs if a is not None]
            return max(xs) if xs else None

        over_mkt, under_mkt = _best_ask("over"), _best_ask("under")
        mkt_by_side = {"over": over_mkt, "under": under_mkt}

        # Event-driven context: pre-match favourite (explicit strength+form basis, Part 2),
        # the latest goal + red card, KO flag.
        fav_side, fav_prob, fav_basis = _favourite_basis(conn, prior, sm, hi, ai)
        last_goal_side, last_goal_min = _last_event(conn, fx, "Goal", hi, ai)
        # Score-consistency guard: a goal can be chalked off (VAR / offside / feed
        # correction) AFTER its event row was written — the event lingers but the
        # scoreboard reverts. Only treat the latest goal as "recent" if the scoring
        # side's goal-event count matches the goals actually on the board; otherwise
        # the goal-driven tactics (overreaction fade) would fire on a disallowed goal.
        if last_goal_side and not _goal_on_board(conn, fx, hi, ai, last_goal_side, gh, ga):
            last_goal_side, last_goal_min = None, None
        carded_side, card_min = _last_event(conn, fx, "Card", hi, ai, detail_like="%Red%")
        # A red can be rescinded on review; the event row stays but the player is back
        # on. The goal path already guards against its phantom equivalent — this is the
        # same failure on the card path, and the feed does write 'Red card cancelled'.
        if carded_side and _red_rescinded(conn, fx, card_min):
            carded_side, card_min = None, None
        knockout = _is_knockout(fx["round"])
        # Format identity for the signals that are fitted per competition format
        # (late-draw persistence differs by an order of significance between a
        # domestic league and a two-legged decider). Derived from the fixture's own
        # competition + round, so a signal can never be applied to the wrong shape.
        _comp_key, _caps = None, None
        try:
            from prediction_market_soccer.config.leagues import by_api_id, caps_for
            _c = by_api_id(fx["league_id"]) if fx["league_id"] is not None else None
            if _c is not None:
                from prediction_market_soccer.ingest.soccer_ingest import leg_of
                _comp_key = _c.key
                _leg, _ = leg_of(conn, fx["api_id"])
                _caps = caps_for(_c.key, fx["round"], leg=_leg)
        except Exception as e:   # noqa: BLE001 — never let format lookup kill the poll
            # It stays a guard (a registry miss must not stop the whole poll) but it no
            # longer hides silently: a missing column here disabled a whole tactic for a
            # day with nothing in the logs.
            print(f"[inplay_arb] format lookup failed for fixture "
                  f"{fx['api_id']}: {type(e).__name__}: {e}")

        # (1) tactics — mostly model-only; convergence now takes the live market BID so it
        # only "locks" when the bid is near fair (won't tell you to sell a 0.92 fair into 0.79).
        _cv_side = "home" if gh > ga else "away"
        # The draw lifecycle needs the live TIE quote to produce its entry branch, and
        # the held entry price to produce its take-profit-with-basis branch. Both were
        # called with neither (the World Cup module has the same omission), so the only
        # entry-side draw signal in the module could never fire and every level-late
        # draw degraded to the generic "sell if held" text. Kalshi quotes the TIE leg of
        # every club 3-way, and the frozen ledger holds the entry when we are actually in.
        _draw_ask = _best_ask("draw")
        _held_draw = _held_entry(conn, fx["api_id"], "draw")
        _held_under = _held_entry(conn, fx["api_id"], "under")
        for sig in (draw_trade_signal(lp, draw_entry=_held_draw,
                                      draw_market_price=_draw_ask),
                    totals_time_decay(lp, entry_under=_held_under),
                    convergence_take_profit(_cv_side, _held_entry(conn, fx["api_id"], _cv_side) or 0.5,
                                            lp, market_bid=_best_bid(_cv_side)) if gh != ga else None,
                    live_momentum_from_store(conn, fx["api_id"], fx["home_api_id"], fx["away_api_id"], minute, gh, ga),
                    goal_overreaction_fade(lp, prematch_fav_side=fav_side, last_goal_side=last_goal_side,
                                           last_goal_minute=last_goal_min, fav_basis=fav_basis),
                    favourite_comeback(lp, prematch_fav_side=fav_side, prematch_fav_prob=fav_prob, fav_basis=fav_basis),
                    red_card_value(lp, carded_side=carded_side, card_minute=card_min),
                    knockout_late_draw(lp, knockout=knockout, caps=_caps, comp_key=_comp_key)):
            if sig and sig.act != "HOLD":
                opps.append(Opportunity(fx["api_id"], m, minute, score, "tactic", sig.side,
                                        "model", fair.get(sig.side), None, None, sig.act, sig.reason,
                                        reason_key=sig.reason_key, reason_args=sig.reason_args,
                                        intent=_intent_for(sig.reason_key)))

        # (1b) data-mined tactics (26-match intra-game study) — read the extra live
        # stats once, then fan out. Each gracefully HOLDs when its data is absent.
        st = _team_live_stats(conn, fx)
        comb_xg = None
        if st["home"]["xg"] is not None and st["away"]["xg"] is not None:
            comb_xg = st["home"]["xg"] + st["away"]["xg"]
        hf, af = _formations(conn, fx)
        lt_side, lt_player, lt_share, lt_removed = _lone_threat(conn, fx)
        book = _live_book_prob(conn, fx)
        mined = [
            dormant_explosion(lp, combined_xg=comb_xg),
            finishing_uplift_over(lp, market_over_price=over_mkt),
            late_goal_bias(lp),
            formation_fragility(lp, home_formation=hf, away_formation=af),
            xg_dominance_chase(xg_for=st["home"]["xg"], xg_against=st["away"]["xg"],
                               goals_for=gh, goals_against=ga, minute=minute, side="home"),
            xg_dominance_chase(xg_for=st["away"]["xg"], xg_against=st["home"]["xg"],
                               goals_for=ga, goals_against=gh, minute=minute, side="away"),
            possession_trap_fade(possession=st["home"]["possession"], xg_for=st["home"]["xg"],
                                 goals_for=gh, goals_against=ga, minute=minute, side="home"),
            possession_trap_fade(possession=st["away"]["possession"], xg_for=st["away"]["xg"],
                                 goals_for=ga, goals_against=gh, minute=minute, side="away"),
            lone_threat_removed(side=lt_side or "home", lone_player=lt_player, shot_share=lt_share,
                                removed=lt_removed, minute=minute, exp_remaining_goals=lp.exp_remaining_goals),
        ]
        for side in ("home", "draw", "away"):
            # Pass the live tradable-venue ask so crossval uses its SOUND branch (book AND
            # model both beat the venue → venue lagging), not the noisy book>model-only branch
            # that contradicted the model and fired on quote-cadence noise.
            mined.append(live_odds_crossval(model_fair=fair[side], book_prob=book.get(side),
                                            side=side, our_venue_price=_best_ask(side)))
        for sig in mined:
            if sig and sig.act != "HOLD":
                # Attach the live totals market price to OVER/UNDER tactics so the desk
                # sees what it would pay. NO model-vs-market edge is shown for these:
                # the pattern tactics (dormant / formation / late) deliberately disagree
                # with the naive Poisson p_over, so an edge vs the model would mislead —
                # the genuine model-vs-market totals edge surfaces as a relative_value row.
                mkt = mkt_by_side.get(sig.side)
                opps.append(Opportunity(fx["api_id"], m, minute, score, "tactic", sig.side,
                                        "model" if mkt is None else "totals", fair.get(sig.side),
                                        round(mkt, 3) if mkt is not None else None, None,
                                        sig.act, sig.reason,
                                        reason_key=sig.reason_key, reason_args=sig.reason_args,
                                        intent=_intent_for(sig.reason_key)))
                # kind stays "tactic" so ranking / frontend colour / i18n are unchanged.

        # (1c) corner-total signal — its own market (Kalshi KXWCCORNERS ladder), separate
        # from the goal-totals book. Fires only against a REAL corner quote with edge; the
        # signal HOLDs on missing/early/stale corners, so a no-quote match adds nothing.
        ch, ca = st["home"]["corners"], st["away"]["corners"]
        corners_now = (ch + ca) if (ch is not None and ca is not None) else None
        corner_q = next((quotes.get(v) for v in ("kalshi_corners", "poly_us_corners")
                         if quotes.get(v)), None)
        # Per-competition corner prior (fitted in data/priors/league_corners.json):
        # a Serie A match and a CONMEBOL cup tie do not produce corners at the same
        # rate, and the signal's fair value is a direct function of that rate.
        try:
            from prediction_market_soccer.model.inplay_corners import corner_prior
            _nu_full, _ = corner_prior(_comp_key)
        except Exception:
            _nu_full = None
        csig = corner_total_signal(corners_now, minute, gh, ga, quotes=corner_q,
                                   nu_full=_nu_full)
        if csig and csig.act != "HOLD":
            opps.append(Opportunity(fx["api_id"], m, minute, score, "tactic", csig.side,
                                    "corners", csig.reason_args.get("fair"),
                                    csig.reason_args.get("ask"), None, csig.act, csig.reason,
                                    reason_key=csig.reason_key, reason_args=csig.reason_args,
                                    intent=_intent_for(csig.reason_key)))

        # (2) market-dependent: relative value + cross-venue lock arb (3-way + totals).
        # Quote per outcome is {'ask','bid'} (or a plain float = ask==bid).
        sides = ["home", "draw", "away"] + (["over", "under"] if "over" in fair else [])
        for side in sides:
            present = {v: q.get(side) for v, q in quotes.items() if q and q.get(side) is not None}
            # One row per side: back the CHEAPEST ask (best edge); name the other
            # venues that also qualify, instead of a near-duplicate row per venue.
            asks_by_v = {v: _ask(qv) for v, qv in present.items() if _ask(qv) is not None}
            # Model-aware take-profit: if the BEST bid we could sell into over-reacts above
            # the live model fair (+margin), emit a SELL to lock the overshoot (validated
            # ~2.8× hold-to-FT). Fires per side; the desk sells whatever it actually holds.
            bids_by_v = {v: _bid(qv) for v, qv in present.items() if _bid(qv) is not None}
            if bids_by_v and side in ("home", "draw", "away"):
                best_bid_v = max(bids_by_v, key=bids_by_v.get)
                best_bid = bids_by_v[best_bid_v]
                tp = model_overshoot_take_profit(side, best_bid, lp)
                if tp.act == "SELL":
                    opps.append(Opportunity(fx["api_id"], m, minute, score, "tactic", side,
                                            best_bid_v, round(fair[side], 3), round(best_bid, 3),
                                            round(best_bid - fair[side], 3), "SELL", tp.reason,
                                            reason_key=tp.reason_key, reason_args=tp.reason_args,
                                            intent=_intent_for(tp.reason_key)))
            if asks_by_v:
                best_v = min(asks_by_v, key=asks_by_v.get)
                best_ask = asks_by_v[best_v]
                e = compute_edge(fair[side], best_ask, fee=fee, theta=theta)
                if e.tradable:
                    also = [v for v, a in asks_by_v.items()
                            if v != best_v and compute_edge(fair[side], a, fee=fee, theta=theta).tradable]
                    venue_lbl = best_v + (f" (+{', '.join(also)})" if also else "")
                    also_str = ", ".join(f"{v} {asks_by_v[v]:.2f}" for v in also)
                    reason = f"model {fair[side]:.2f} > {best_v} ask {best_ask:.2f}"
                    if also:
                        reason += f"; also {also_str}"
                    opps.append(Opportunity(fx["api_id"], m, minute, score, "relative_value", side, venue_lbl,
                                            round(fair[side], 3), round(best_ask, 3), round(e.net_edge, 3),
                                            "BUY", reason, reason_key="relative_value",
                                            reason_args={"fair": round(fair[side], 2), "venue": best_v,
                                                         "ask": round(best_ask, 2), "also": also_str},
                                            intent="entry"))
            # Cross-venue lock arb: BUY YES at the cheapest ASK, SELL YES (=buy NO)
            # at the highest BID, on two EXECUTABLE venues. Lock = sell_bid − buy_ask
            # − fees. Using bid for the sell leg avoids false positives.
            ex = {v: qv for v, qv in present.items() if v in CONFIG.venue.executable_venues}
            asks = {v: _ask(qv) for v, qv in ex.items() if _ask(qv) is not None}
            bids = {v: _bid(qv) for v, qv in ex.items() if _bid(qv) is not None}
            if len(asks) >= 1 and len(bids) >= 1:
                buy_v = min(asks, key=asks.get)
                sell_v = max(bids, key=bids.get)
                if buy_v != sell_v:
                    lock = evaluate_lock(asks[buy_v], bids[sell_v], equiv_verified=True,
                                         fee_cheap=fee, fee_expensive=fee)
                    if lock.tradable:
                        no_price, cost = 1 - bids[sell_v], asks[buy_v] + 1 - bids[sell_v]
                        opps.append(Opportunity(
                            fx["api_id"], m, minute, score, "lock_arb", side, f"{buy_v}+{sell_v}",
                            None, None, round(lock.net_lock, 3), "ARB",
                            f"buy {buy_v} YES {asks[buy_v]:.2f} + {sell_v} NO {no_price:.2f} "
                            f"(cost {cost:.2f}) → lock {lock.net_lock:+.2f}",
                            reason_key="lock_arb",
                            reason_args={"buy_v": buy_v, "ask": round(asks[buy_v], 2), "sell_v": sell_v,
                                         "no": round(no_price, 2), "cost": round(cost, 2),
                                         "lock": round(lock.net_lock, 2)},
                            intent="entry"))
    # Rank: lock arb > relative value > tactic; by |edge|.
    rank = {"lock_arb": 0, "relative_value": 1, "tactic": 2}
    opps.sort(key=lambda o: (rank[o.kind], -(abs(o.edge) if o.edge is not None else 0)))
    return [asdict(o) for o in opps]


if __name__ == "__main__":
    import sqlite3

    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    # Demo with a synthetic live match + a synthetic two-venue mispricing.
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "2H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": 0, "away_goals": 0, "elapsed": 78,
        "updated_at": store.utcnow()}, pk=["api_id"])
    for tid, xg in ((10, 1.9), (20, 0.3)):   # France dominating xG but 0:0
        store.upsert(c, "fixture_stats", {"fixture_api_id": 1, "team_api_id": tid, "xg": xg, "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    sm = build_strength(load_prior())
    # Synthetic quote sources: Kalshi cheap on France, Global expensive (cross-venue gap).
    qs = {"kalshi": lambda fid: {"home": 0.30, "draw": 0.45, "away": 0.10},
          "poly_global": lambda fid: {"home": 0.42, "draw": 0.40, "away": 0.12}}
    opps = find_opportunities(conn=c, sm=sm, quote_sources=qs)
    print(f"{len(opps)} in-play opportunities (France 0:0 Senegal @78', France xG 1.9):")
    for o in opps[:8]:
        print(f"  [{o['kind']:<14}] {o['action']:<4} {o['side']:<5} {o['venue']:<14} edge={o['edge']} — {o['reason'][:46]}")
