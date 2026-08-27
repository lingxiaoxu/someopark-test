"""Polymarket US club-soccer single-match discovery (plan 07, 08; D1-3 / R2). Read-only.

Polymarket US lists our club competitions natively — LIVE-MEASURED 2026-08-26 across
all 12 registry comps (epl/lal/sea/bun/lg1/ucl/uel/uecl/lib/sud/bra/lpa). The WC-era
`fwc-` vocabulary is gone; what is on the venue now:

  event slug   ``<pfx>-{home}-{away}-{ET-date}``      e.g. ``epl-ast-ars-2026-08-31``
  3-way result ``atc-{event}-{teamcode}`` / ``atc-{event}-draw``
  totals       ``tsc-{event}-{N}pt5``                 (YES = Over)
  (also asc-… spreads and astatc-… props, which we do not price)

``<pfx>`` is the competition's ``pmus_slug_prefix`` in the league registry and is NOT
the same vocabulary as Polymarket Global's (`lg1` here vs `fl1` there, `uecl` vs `col`,
`lpa` vs `arg`) — that is why the registry carries both fields.

NO ADVANCE MARKET: 3,711 markets across 66 live club events contained zero
``aadc-…-to-advance`` (and zero market slug containing "advance") — the WC per-match
advance instrument simply is not listed for clubs. ``advance_quotes`` therefore returns
None by construction instead of building slugs that 404, and the two-way advance stays
a Kalshi-only reference (R2: single-venue operation, recorded rather than faked).

Discovery is series-scoped, not keyword-scoped: ``series.list`` gives each competition's
series ids and one ``events.list`` per competition returns every base match event of the
window WITH its ``teams[]`` (safeName / abbreviation / league) — so the club→code map,
the pairing index and the competition tag all come from the same response. That replaces
the WC keyword sweep ("World Cup", "FIFA", …), which returned a different subset on every
call. Prices still come from ``markets.bbo`` per leg (US REST is 60/min).

NOTE on the SDK envelope: ``events.retrieve_by_slug`` returns ``{"event": {...}}``
— the markets are under ``["event"]["markets"]`` (this bit me once).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active
# Shared with the Global reader on purpose: both venues are Polymarket and spell clubs
# the same way, so the label→club_id rule must not drift between them.
from prediction_market_soccer.venues.polymarket_global.reader import poly_club_candidates

# Base (non-derivative) match slug. Team codes are 2-6 chars and may carry digits
# (s04, aek1, icde) — measured over the live slug set.
_CODE = r"[a-z0-9]{2,6}"

# How far the pairing index reaches around "now". Poly US lists a match roughly two
# weeks ahead and keeps it after settlement, so this covers both upcoming_export
# (next fixtures) and live_poller (in-play) from one listing per competition.
_WINDOW_BACK_DAYS = 5
_WINDOW_FWD_DAYS = 21
_CACHE_TTL_SEC = 300

# upcoming_export builds one PolymarketUSDiscovery PER COMPETITION inside its loop, so
# the listing cache has to outlive the instance or we would pay 12x for the same data.
_CACHE: dict = {"at": None, "events": {}, "codes": {}, "series": None}


def _load_aliases() -> dict[str, str]:
    """Frozen per-comp alias tables (§3.6), merged — the same exact-only entity
    resolution path KalshiDiscovery uses, so both venues agree on club identity."""
    out: dict[str, str] = {}
    for c in active(include_disabled=True):
        p = CONFIG.paths.priors / f"aliases_{c.key}.json"
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for k, v in (doc.get("aliases") or {}).items():
            out.setdefault(k, v)
    return out


class PolymarketUSDiscovery:
    def __init__(self, client=None, *, window_back_days: int = _WINDOW_BACK_DAYS,
                 window_fwd_days: int = _WINDOW_FWD_DAYS):
        if client is None:
            from polymarket_us import PolymarketUS
            client = PolymarketUS(key_id=os.environ["PMUS_KEY_ID"], secret_key=os.environ["PMUS_SECRET"])
        self.c = client
        self.back, self.fwd = window_back_days, window_fwd_days
        self._aliases = _load_aliases()
        from prediction_market_soccer.ingest import store
        conn = store.init_db()
        self._club_ids = {r["club_id"] for r in conn.execute(
            "SELECT DISTINCT club_id FROM club_registry")}
        # Club labels seen on the venue that no alias/exact rule could resolve. Kept
        # (not swallowed) so the R6 unmapped-market alert has something to count: with
        # ~500 clubs and winter renames, a silent drop is how a whole league goes dark.
        self.unmapped: list[str] = []

    # ── entity resolution: exact alias → exact (accent-folded) normalization ──
    def _resolve(self, *labels: str) -> str | None:
        """First exact hit across the given spellings of one club, else None + count.

        The alias table wins over normalization for every spelling before any
        normalization is tried, so a curated entry can always override the rule.
        """
        labels = tuple(s.strip() for s in labels if (s or "").strip())
        for s in labels:
            if s in self._aliases:
                return self._aliases[s]
        for s in labels:
            for cid in poly_club_candidates(s):
                if cid in self._club_ids:
                    return cid
        if labels and labels[0] not in self.unmapped:
            self.unmapped.append(labels[0])   # deduped: this list is read as an alert count
        return None

    # ── series-scoped discovery ──────────────────────────────────────────────
    def _series_ids(self) -> dict[str, list[int]]:
        """{comp_key: [series id, ...]} — a competition can have both a rolling series
        ('lg1') and a season-stamped one ('epl-2025'), and which one carries the live
        fixtures changes at the season roll, so we query every id that matches."""
        if _CACHE["series"] is not None:
            return _CACHE["series"]
        catalogue: dict[str, str] = {}
        for off in range(0, 2000, 100):
            try:
                out = self.c.series.list({"limit": 100, "offset": off})
            except Exception:
                break
            items = (out.get("series") if isinstance(out, dict) else out) or []
            if not items:
                break
            for s in items:
                catalogue.setdefault(s.get("slug") or "", s.get("id"))
        by_comp: dict[str, list[int]] = {}
        for c in active():
            pfx = c.pmus_slug_prefix
            if not pfx:
                continue
            ids = []
            for slug, sid in catalogue.items():
                if slug == pfx or slug.startswith(pfx + "-"):
                    try:
                        ids.append(int(sid))
                    except (TypeError, ValueError):
                        continue
            if ids:
                by_comp[c.key] = ids
        _CACHE["series"] = by_comp
        return by_comp

    def _load(self, *, force: bool = False) -> None:
        """One events.list per competition; fills the pairing index and the code map."""
        now = datetime.now(timezone.utc)
        if not force and _CACHE["at"] and (now - _CACHE["at"]).total_seconds() < _CACHE_TTL_SEC:
            return
        lo = (now - timedelta(days=self.back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (now + timedelta(days=self.fwd)).strftime("%Y-%m-%dT%H:%M:%SZ")
        series = self._series_ids()
        events: dict[frozenset, list[dict]] = {}
        codes: dict[str, str] = {}
        for c in active():
            ids = series.get(c.key)
            if not ids:
                continue
            # Paged: a busy competition (Argentina runs 30 clubs) can exceed one page
            # inside the window, and a truncated page silently drops half its fixtures.
            for offset in range(0, 400, 100):
                try:
                    out = self.c.events.list({"seriesId": ids, "limit": 100, "offset": offset,
                                              "startTimeMin": lo, "startTimeMax": hi})
                except Exception:
                    break
                page = (out.get("events") if isinstance(out, dict) else out) or []
                if not page:
                    break
                for e in page:
                    rec = self._parse_event(c.pmus_slug_prefix, e)
                    if rec:
                        events.setdefault(rec["pair"], []).append(rec)
                        for cid, code in rec["codes"].items():
                            codes[cid] = code
                if len(page) < 100:
                    break
        _CACHE.update({"at": now, "events": events, "codes": codes})

    def _parse_event(self, pfx: str, e: dict) -> dict | None:
        slug = (e.get("slug") or "").lower()
        m = re.match(rf"^{re.escape(pfx)}-({_CODE})-({_CODE})-(\d{{4}}-\d{{2}}-\d{{2}})$", slug)
        if not m:
            return None                      # derivative / season event, not a base match
        # teams[] carries the venue's own club identity (safeName is the short form that
        # actually joins to our registry; name keeps the legal suffix). Order in the array
        # is not contractual, so sides come from the slug codes, which are.
        by_code: dict[str, str] = {}
        codes: dict[str, str] = {}
        for t in (e.get("teams") or []):
            cid = self._resolve(t.get("safeName"), t.get("name"))
            code = (t.get("abbreviation") or "").lower()
            if cid and code:
                by_code[code] = cid
                codes[cid] = code
        hc, ac = m.group(1), m.group(2)
        hid, aid = by_code.get(hc), by_code.get(ac)
        if not (hid and aid):
            return None
        return {"slug": slug, "date": m.group(3), "home_code": hc, "away_code": ac,
                "home_id": hid, "away_id": aid, "codes": codes,
                "pair": frozenset({hid, aid})}

    def code_map(self) -> dict[str, str]:
        """{canonical club_id: Poly US team abbreviation} learned from the listing.

        This is exactly what ``club_registry.poly_code`` is meant to hold (that column
        is still empty); an ingest step can persist it once someone owns that write.
        """
        self._load()
        return dict(_CACHE["codes"])

    def _find_event(self, home_id: str, away_id: str, et_date: str) -> dict | None:
        """The event for this pairing, from the index, else a single batched slug probe."""
        self._load()
        cands = _CACHE["events"].get(frozenset({home_id, away_id})) or []
        exact = next((r for r in cands if r["date"] == et_date), None)
        if exact:
            return exact
        # Two legs of a tie sit a week apart, so only accept a near-date fallback inside
        # a 2-day slack (kickoff sliding across the ET/UTC boundary), never "closest".
        if et_date and cands:
            try:
                want = date.fromisoformat(et_date)
                near = [r for r in cands if abs((date.fromisoformat(r["date"]) - want).days) <= 2]
                if len(near) == 1:
                    return near[0]
            except ValueError:
                pass
        return self._probe_slugs(home_id, away_id, et_date)

    def _probe_slugs(self, home_id: str, away_id: str, et_date: str) -> dict | None:
        """Outside the listing window: build the candidate slugs from known codes and
        resolve them in ONE batched events.list (the API takes a slug list)."""
        codes = _CACHE["codes"]
        hc, ac = codes.get(home_id), codes.get(away_id)
        if not (hc and ac and et_date):
            return None
        prefixes = [c.pmus_slug_prefix for c in active() if c.pmus_slug_prefix]
        cands = [f"{p}-{a}-{b}-{et_date}" for p in prefixes for a, b in ((hc, ac), (ac, hc))]
        try:
            out = self.c.events.list({"slug": cands, "limit": len(cands)})
        except Exception:
            return None
        for e in ((out.get("events") if isinstance(out, dict) else out) or []):
            slug = (e.get("slug") or "").lower()
            pfx = slug.split("-", 1)[0]
            rec = self._parse_event(pfx, e)
            if rec and rec["pair"] == frozenset({home_id, away_id}):
                return rec
        return None

    # ── pricing ──────────────────────────────────────────────────────────────
    def _price(self, slug: str) -> dict | None:
        """{'ask': bestAsk, 'bid': bestBid} for a market (the real fill prices)."""
        try:
            bbo = self.c.markets.bbo(slug)
            md = bbo.get("marketData", bbo) if isinstance(bbo, dict) else {}
            ask = (md.get("bestAsk") or {}).get("value")
            bid = (md.get("bestBid") or {}).get("value")
            cur = (md.get("currentPx") or {}).get("value")
            return {"ask": float(ask) if ask is not None else (float(cur) if cur else None),
                    "bid": float(bid) if bid is not None else (float(cur) if cur else None)}
        except Exception:
            return None

    def match_quotes(self, home_id: str, away_id: str, et_date: str) -> dict | None:
        """Raw 3-way {home, draw, away} prices for a match (None if not found).

        ``et_date`` is the US ET date string 'YYYY-MM-DD' — the same date the slug uses.
        The three leg slugs are derived from the event slug's own team codes, so no
        question-text parsing is needed to tell the sides apart.
        """
        ev = self._find_event(home_id, away_id, et_date)
        if not ev:
            return None
        legs = {"home": ev["home_code"], "draw": "draw", "away": ev["away_code"]}
        # The slug is always <home>-<away> in Poly's own ordering ("ordering": "home" for
        # every soccer league in sports.list), which may be the reverse of OUR fixture's
        # home/away for a neutral venue — the parsed ids tell us which way round it is.
        if ev["home_id"] != home_id:
            legs = {"home": ev["away_code"], "draw": "draw", "away": ev["home_code"]}
        out: dict[str, dict] = {}
        for side, tok in legs.items():
            px = self._price(f"atc-{ev['slug']}-{tok}")
            if px:
                out[side] = px
        return out if {"home", "draw", "away"} <= set(out) else None

    def advance_quotes(self, home_id: str, away_id: str, et_date: str) -> dict[str, dict] | None:
        """Always None: Polymarket US lists no per-match advance market for clubs.

        The WC edition read ``aadc-{event}-to-advance``; a full sweep of every market in
        every live club event (3,711 markets / 66 events, 2026-08-26) found no market
        whose slug contains "advance" in any competition — including the UEL/UECL playoff
        and CONMEBOL knockout events, which are exactly where one would live. Returning
        None keeps the caller on the Kalshi advance reference instead of spending a
        request per match on a slug that cannot exist.
        """
        return None

    def totals_quotes(self, home_id: str, away_id: str, et_date: str,
                      line: float = 2.5) -> dict[str, dict] | None:
        """{over/under: {'ask','bid'}} for a match's total-goals line (None if not found).

        Totals slug: ``tsc-{event}-{N}pt5`` ("Will the total in X be more than N.5?") —
        YES = Over, Under is the complement of the YES book. ``line`` 2.5 → token ``2pt5``.
        The first-half/second-half variants carry an extra ``-fh``/``-sh`` token and are
        deliberately not matched.
        """
        ev = self._find_event(home_id, away_id, et_date)
        if not ev:
            return None
        tok = f"{int(line)}pt5" if line == int(line) + 0.5 else str(line).replace(".", "pt")
        p = self._price(f"tsc-{ev['slug']}-{tok}")
        if not p:
            return None
        over_ask, over_bid = p.get("ask"), p.get("bid")
        return {"over": {"ask": over_ask, "bid": over_bid},
                "under": {"ask": (1.0 - over_bid) if over_bid is not None else None,
                          "bid": (1.0 - over_ask) if over_ask is not None else None}}


if __name__ == "__main__":
    d = PolymarketUSDiscovery()
    print("series per comp:", d._series_ids())
    d._load(force=True)
    idx = _CACHE["events"]
    print(f"pairing index: {len(idx)} pairings, code map: {len(_CACHE['codes'])} clubs, "
          f"unmapped labels: {len(set(d.unmapped))}")
    for pair, recs in list(idx.items())[:5]:
        r = recs[0]
        print(f"  {r['slug']:30s} {r['home_id']} vs {r['away_id']} ({r['date']})")
        print("    3-way:", d.match_quotes(r["home_id"], r["away_id"], r["date"]))
