"""Polymarket Global — READ-ONLY reference reader (plan 07 §2, 08; club edition).

Polymarket Global (`clob.polymarket.com` + Gamma + Data) is the deeper, often
price-leading pool. From the US it is **readable but NOT tradable** (order
geoblock). We use it purely as a third reference price for cross-venue value
(plan 08): when Polymarket US diverges from Global's de-vigged price, that is a
signal the thinner US pool may be mispriced.

NO credentials required — all endpoints are public. This class deliberately does
NOT implement ExecutionVenue; `venue_guard` additionally blocks any order routed
to `poly_global`.

Three public APIs (plan 07 §2):
  * Gamma  (`gamma-api.polymarket.com`)  — market/event discovery + metadata
  * CLOB   (`clob.polymarket.com`)       — order book / price / midpoint reads
  * Data   (`data-api.polymarket.com`)   — public trades / positions / OI

Identifiers: condition_id (market) / token_id (a YES or NO outcome). Prices are
0–1 and read directly as implied probabilities.
"""
from __future__ import annotations

import re
from decimal import Decimal

import requests

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active as _active_comps
from prediction_market_soccer.config.leagues import get as _get_comp
from prediction_market_soccer.venues.base import OrderBook

_VENUE = "poly_global"
_SOCCER_TAG = "100350"          # Gamma tag id for soccer

# Season / qualification events, per kind. These REPLACE the WC's per-nation
# "reach round X" slug table: a Gamma sweep of tags ucl / uel /
# europa-conference-league / lib / sud / premier-league / brazil-serie-a on
# 2026-08-26 returned ZERO per-round club events (no "reach round of 16",
# no "reach quarterfinals", …). The club edition only lists:
#   champion    — "EPL: 2027 Champion" / "Copa Libertadores: Winner"
#   league_play — "UEFA Champions League: Team to Qualify for League Play"
#                 (the real analogue of the WC 2-way advance: it prices the
#                  qualifying-tie winners, which is exactly the stage where our
#                  caps say advance=True for UEFA)
#   euro_spot   — "EPL: Team to qualify for the 2027-28 UEFA Champions League"
# Their slugs end in a per-event numeric suffix (…-20260701202025549), so they can
# only be found by tag + title — a hardcoded slug table would rot on every relist.
_SEASON_TITLE_RULES: dict[str, re.Pattern] = {
    "champion": re.compile(r"\b(champion|winner)\b", re.I),
    "league_play": re.compile(r"qualify for league play", re.I),
    "euro_spot": re.compile(r"qualify for .*uefa", re.I),
}


# Club legal-form / society abbreviations, as a CLOSED vocabulary. Polymarket writes
# clubs in their full registered form ("US Sassuolo Calcio", "SSC Napoli", "Angers SCO")
# while API-Football — the axis our club_ids are derived from — writes the plain name
# ("Sassuolo", "Napoli", "Angers"). Removing these tokens is normalization, not
# similarity matching, so it keeps the live path on the exact-only rule. Deliberately
# EXCLUDED: tokens that are themselves club names (AEK, CSKA, LASK, NEC, PSV, PAOK) —
# stripping those would erase the club instead of its legal form.
_CLUB_FORM_TOKENS = """
ac acf ad afc aj as ass bc bk bsc ca calcio cd cf cfc club cs csd ec es fa fc football
fsv futbol futebol gf gnk hnk if ks mfk mh nk ofk osc pfc pfk rc rcd rfc rsc ru sc scd
sco sd se sfp sk sl ss ssc ssd sv tsg tsv ud us usl vfb vfl
""".split()
_CLUB_FORM_RE = re.compile(r"(?<![a-z])(" + "|".join(_CLUB_FORM_TOKENS) + r")(?![a-z])", re.I)


def poly_club_id(label: str) -> str:
    """Polymarket club label -> our canonical club_id, EXACT (never fuzzy).

    Same normalization as ``ingest.soccer_ingest.club_id_of`` plus a Unicode NFKD
    fold, because Polymarket writes accented club names in full ("São Paulo",
    "Grêmio FBPA", "Alavés") while club_id_of only strips characters it recognises
    — without the fold those become ``so_paulo`` / ``alavs`` and join to nothing.
    """
    import unicodedata
    s = unicodedata.normalize("NFKD", (label or "").strip())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    from prediction_market_soccer.ingest.soccer_ingest import club_id_of
    return club_id_of(s)


def poly_club_candidates(label: str) -> tuple[str, ...]:
    """Ordered EXACT club_id candidates for a Polymarket label: as-written first,
    then with the club legal-form tokens removed.

    Order matters — "Athletic Club" and "Club Brugge" must resolve as themselves
    before the ``club`` token is ever considered noise. Measured on the live PMUS
    listing (852 club labels): alias+fold alone resolved 189, adding this second
    pass resolved 476. The rest are genuine per-comp alias-table entries
    ("Tottenham Hotspur" → tottenham, "Olympique de Marseille" → marseille) and
    must be curated there, never guessed here.
    """
    out: list[str] = []
    for s in (label or "").strip(), _CLUB_FORM_RE.sub(" ", (label or "").strip()):
        cid = poly_club_id(s)
        if cid and cid not in out:
            out.append(cid)
    return tuple(out)


def parse_clob_book(payload: dict, token_id: str) -> OrderBook:
    """Normalise a CLOB `/book` response into our two-sided OrderBook.

    Unlike Kalshi (bids only), Polymarket returns both `bids` and `asks` for a
    token directly. For the YES token: yes_bid = best (max) bid, yes_ask = best
    (min) ask; the NO side is the binary complement (1 − yes price).
    """
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    # Polymarket returns levels as {"price": "0.42", "size": "100"}; best bid is
    # the highest price, best ask the lowest.
    best_bid = max((Decimal(str(b["price"])) for b in bids), default=None)
    best_ask = min((Decimal(str(a["price"])) for a in asks), default=None)
    bid_depth = next((Decimal(str(b["size"])) for b in bids
                      if Decimal(str(b["price"])) == best_bid), None) if best_bid is not None else None
    ask_depth = next((Decimal(str(a["size"])) for a in asks
                      if Decimal(str(a["price"])) == best_ask), None) if best_ask is not None else None

    one = Decimal("1")
    no_bid = (one - best_ask) if best_ask is not None else None
    no_ask = (one - best_bid) if best_bid is not None else None
    return OrderBook(
        venue=_VENUE, market_key=token_id,
        yes_bid=best_bid, yes_ask=best_ask, no_bid=no_bid, no_ask=no_ask,
        yes_depth=bid_depth, no_depth=ask_depth,
    )


class PolymarketGlobalReader:
    """Read-only client for Polymarket Global. Never places orders."""

    name = _VENUE
    executable = False

    def __init__(self, *, timeout: float = 15.0):
        self.cfg = CONFIG.venue
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": "someopark-prediction-market/1.0"})

    def _get(self, base: str, path: str, params: dict | None = None):
        r = self._s.get(f"{base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── Gamma: discovery ─────────────────────────────────────────────────────
    def list_events(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_gamma, "/events", params)

    def list_markets(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_gamma, "/markets", params)

    def search_events(self, query: str, *, limit: int = 200) -> list[dict]:
        """Client-side keyword filter over Gamma events (slug/title contains query)."""
        q = query.lower()
        events = self.list_events(limit=limit, closed="false")
        return [e for e in events
                if q in (e.get("slug", "") + " " + e.get("title", "")).lower()]

    # ── CLOB: order book / price ─────────────────────────────────────────────
    def get_book(self, token_id: str) -> OrderBook:
        payload = self._get(self.cfg.pmglobal_clob, "/book", {"token_id": token_id})
        return parse_clob_book(payload, token_id)

    def get_price(self, token_id: str, side: str = "buy") -> Decimal | None:
        payload = self._get(self.cfg.pmglobal_clob, "/price", {"token_id": token_id, "side": side})
        p = payload.get("price")
        return Decimal(str(p)) if p is not None else None

    def get_midpoint(self, token_id: str) -> Decimal | None:
        payload = self._get(self.cfg.pmglobal_clob, "/midpoint", {"token_id": token_id})
        m = payload.get("mid")
        return Decimal(str(m)) if m is not None else None

    # ── Data: public trades ──────────────────────────────────────────────────
    def recent_trades(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_data, "/trades", params)

    # ── CLOB: historical price series (plan 18 §2.4b) ────────────────────────
    def prices_history(self, token_id: str, *, start_ts: int | None = None,
                       end_ts: int | None = None, fidelity: int = 1,
                       interval: str | None = None) -> list[dict]:
        """Per-minute (or coarser) historical price series for a token.

        Public CLOB endpoint, no auth. Returns [{"ts": <unix>, "price": <0..1 float>}]
        sorted ascending. `fidelity` is the bar size in minutes (1 = per-minute).
        Pass an explicit start_ts/end_ts window, or `interval` (e.g. 'max','1w','1d').
        """
        params: dict = {"market": token_id, "fidelity": fidelity}
        if start_ts is not None:
            params["startTs"] = int(start_ts)
        if end_ts is not None:
            params["endTs"] = int(end_ts)
        if interval is not None:
            params["interval"] = interval
        elif start_ts is None and end_ts is None:
            params["interval"] = "max"
        payload = self._get(self.cfg.pmglobal_clob, "/prices-history", params)
        out = []
        for p in payload.get("history") or []:
            t, pr = p.get("t"), p.get("p")
            if t is not None and pr is not None:
                out.append({"ts": int(t), "price": float(pr)})
        return out

    # ── Gamma: single-match discovery (closed/archived too) ──────────────────
    @staticmethod
    def _prefix_map(include_disabled: bool = False) -> dict[str, str]:
        """{Poly Global slug prefix -> our comp key}, straight from the registry.

        Keeping the vocabulary in the registry is what makes "adding a league =
        one entry, zero code" true here: the match regex below is generated from
        this map rather than hand-maintained.
        """
        return {c.poly_slug_prefix: c.key for c in _active_comps(include_disabled)
                if c.poly_slug_prefix}

    @classmethod
    def _base_re(cls, include_disabled: bool = False) -> re.Pattern:
        # Prefixes are matched as a whole token (the trailing '-' is part of the
        # pattern), which is what keeps the near-miss neighbours out: col1/col2
        # (Colombia) vs col (Conference League), bra2/bra3/brco vs bra, argpn vs arg.
        # Team codes are 2-4 chars and MAY carry digits (s04, aek1, sto1, dae1) —
        # measured over 1,068 codes in live slugs; {2,5} leaves a little headroom.
        alts = "|".join(sorted((re.escape(p) for p in cls._prefix_map(include_disabled)),
                               key=len, reverse=True))
        return re.compile(rf"^({alts})-([a-z0-9]{{2,5}})-([a-z0-9]{{2,5}})-(\d{{4}}-\d{{2}}-\d{{2}})$")

    def list_match_events(self, *, soccer_tag: str = _SOCCER_TAG,
                          max_pages: int = 16, page: int = 100,
                          end_date_min: str | None = None,
                          end_date_max: str | None = None,
                          include_disabled: bool = False) -> list[dict]:
        """All single-match BASE events of our competitions (`<pfx>-{h}-{a}-{date}`),
        INCLUDING closed/archived ones (so already-played matches are reachable —
        the live `search_events` only sees open events).

        Pass an `end_date_min`/`end_date_max` ISO window (recommended) to filter the
        Gamma query to the match dates. Each returned dict: {slug, comp, league_prefix,
        date, teams: {name: token_id}, raw}; `teams` maps each 3-way outcome's
        groupItemTitle (real club name / 'Draw …') to its YES clob token.

        The sweep runs ONE PASS PER COMPETITION TAG rather than one pass over the whole
        soccer tag. That is not a micro-optimisation: the soccer tag holds ~2,250 events
        in a two-week window (90+ competitions, from Icelandic second tier to table
        tennis-adjacent friendlies), Gamma rejects offsets past ~2,100, and events are
        ordered by resolution date — so our twelve competitions' matches fall off the
        end of the pagination. Sweeping per tag returned every UEL/UECL/CONMEBOL match
        the whole-soccer sweep had been missing entirely.
        """
        import json as _json
        pmap = self._prefix_map(include_disabled)
        base_re = self._base_re(include_disabled)
        win: dict = {}
        if end_date_min:
            win["end_date_min"] = end_date_min
        if end_date_max:
            win["end_date_max"] = end_date_max
        out: list[dict] = []
        seen: set[str] = set()
        queries: list[dict] = [{"tag_slug": t} for c in _active_comps(include_disabled)
                               if c.poly_slug_prefix for t in c.poly_tag_slugs]
        if not queries:
            queries = [{"tag_id": soccer_tag}]

        def sweep(scope: dict) -> None:
            # Query BOTH closed and still-open events: a match that JUST finished can
            # linger as closed=false for a while before Polymarket archives it, but its
            # price history is already complete — so we must see open events too, else a
            # just-ended match can't be backfilled until Poly gets around to closing it.
            for closed in ("true", "false"):
                for offset in range(0, max_pages * page, page):
                    try:
                        evs = self.list_events(limit=page, closed=closed, archived="true",
                                               offset=offset, order="endDate",
                                               ascending="false", **scope, **win) or []
                    except Exception:
                        break
                    if not evs:
                        break
                    for e in evs:
                        slug = e.get("slug", "") or ""
                        if slug in seen:
                            continue
                        seen.add(slug)
                        m = base_re.match(slug)
                        if not m:
                            continue
                        teams: dict[str, str] = {}
                        for mk in e.get("markets") or []:
                            git = (mk.get("groupItemTitle") or mk.get("question") or "").strip()
                            toks = mk.get("clobTokenIds")
                            if isinstance(toks, str):
                                try:
                                    toks = _json.loads(toks)
                                except Exception:
                                    toks = None
                            if git and toks:
                                teams[git] = toks[0]
                        out.append({"slug": slug, "league_prefix": m.group(1),
                                    "comp": pmap.get(m.group(1)),
                                    "date": m.group(4), "teams": teams, "raw": e})

        for q in queries:
            sweep(q)
        return out

    # Historical name kept live: ops/backfill_price_ticks and ops/backfill_milestones
    # still call it, and the payload shape is unchanged (only `comp` was added).
    list_wc_match_events = list_match_events

    def find_match_event(self, slug: str) -> dict | None:
        """Fetch a single event by exact slug (e.g. 'epl-ars-che-2026-09-06')."""
        evs = self.list_events(slug=slug)
        return evs[0] if evs else None

    # ── Gamma: per-club season / qualification events ─────────────────────────
    def season_event_index(self, comp_key: str, kind: str = "champion",
                           *, max_pages: int = 4, page: int = 100) -> dict[str, str]:
        """{canonical_club_id: YES clob token_id} for a competition's season event.

        ``kind`` ∈ _SEASON_TITLE_RULES (champion / league_play / euro_spot). Resolved
        by (registry tag slug + title rule) rather than by slug, because the slugs
        carry a per-event numeric suffix. Returns {} when the competition has no such
        event listed — a normal state, not an error (Copa Sudamericana had no season
        event at all on 2026-08-26). Cached per (instance, comp, kind).
        """
        import json as _json
        cache = self.__dict__.setdefault("_sei", {})
        ck = (comp_key, kind)
        if ck in cache:
            return cache[ck]
        idx: dict[str, str] = {}
        try:
            tags = _get_comp(comp_key).poly_tag_slugs
        except KeyError:
            tags = ()
        rule = _SEASON_TITLE_RULES.get(kind)
        ev = None
        for tag in (tags if rule else ()):
            for offset in range(0, max_pages * page, page):
                try:
                    evs = self.list_events(limit=page, tag_slug=tag, archived="true",
                                           closed="false", offset=offset,
                                           order="endDate", ascending="false") or []
                except Exception:
                    break
                if not evs:
                    break
                ev = next((e for e in evs if rule.search(e.get("title") or "")), None)
                if ev:
                    break
            if ev:
                break
        for mk in (ev.get("markets") or []) if ev else []:
            toks = mk.get("clobTokenIds")
            if isinstance(toks, str):
                try:
                    toks = _json.loads(toks)
                except Exception:
                    toks = None
            if not toks:
                continue
            # This reader stays DB-free, so it cannot check a candidate against the club
            # registry the way the US adapter does; instead BOTH exact spellings are
            # indexed (as-written and legal-form-stripped) and the caller's own club_id
            # picks the right one. setdefault so a literal match is never overwritten
            # by another club's stripped form.
            for tid in poly_club_candidates(mk.get("groupItemTitle", "") or ""):
                idx.setdefault(tid, toks[0])
        cache[ck] = idx
        return idx

    def reach_round_index(self, round_key: str, *, comp_key: str | None = None) -> dict[str, str]:
        """{canonical_club_id: YES token} for "this club reaches <round_key>".

        The WC module fed this from per-nation reach-round events. Those do NOT exist
        in the club edition — the only per-club stage market Gamma lists is the
        qualifying→league-phase one, so `round_key='advance'` with a UEFA `comp_key`
        maps onto it and every other round key legitimately yields {}. Callers already
        treat {} as "no advance reference on this venue" and fall back to Kalshi.
        """
        if round_key == "advance" and comp_key:
            return self.season_event_index(comp_key, "league_play")
        return {}

    def advance_quotes(self, home_id: str, away_id: str, round_key: str,
                       *, comp_key: str | None = None) -> dict[str, dict] | None:
        """{home/away: {'ask','bid'}} 2-way advance for a knockout match (None if absent).

        Derived from the two clubs' reach-YES books (Global lists no dedicated per-match
        advance market). Mirrors the {home/away} shape of the Kalshi/Poly-US advance."""
        idx = self.reach_round_index(round_key, comp_key=comp_key)
        th, ta = idx.get(home_id), idx.get(away_id)
        if not (th and ta):
            return None

        def q(token):
            ob = self.get_book(token)
            return {"ask": float(ob.yes_ask) if ob.yes_ask is not None else None,
                    "bid": float(ob.yes_bid) if ob.yes_bid is not None else None}

        return {"home": q(th), "away": q(ta)}


if __name__ == "__main__":
    r = PolymarketGlobalReader()
    print(f"registry slug prefixes: {r._prefix_map()}")
    print(f"base regex: {r._base_re().pattern}")
    evs = r.list_match_events(max_pages=4)
    per_comp: dict[str, int] = {}
    for e in evs:
        per_comp[e["comp"] or e["league_prefix"]] = per_comp.get(e["comp"] or e["league_prefix"], 0) + 1
    print(f"Gamma: {len(evs)} club match event(s) in the current listing window")
    for k, n in sorted(per_comp.items(), key=lambda kv: -kv[1]):
        print(f"  {k:14s} {n}")
    for comp in ("epl", "ucl", "libertadores"):
        print(f"  season champion index {comp}: {len(r.season_event_index(comp, 'champion'))} clubs")
