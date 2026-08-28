"""Kalshi club-soccer market discovery — per-competition (TRANSFORM_PLAN §2.2).

Forked from the WC discovery; every parsing subtlety is kept verbatim (the
"Reg Time:" sub-title strip — live-verified on KXUCLGAME qualifiers — the
``-TIE`` suffix draw leg, complement NO-side books, floor/cap-strike lines).
What changed (C1/C3): series tickers come from the LEAGUE REGISTRY and entity
mapping goes through the FROZEN per-comp alias table (aliases_<comp>.json,
bootstrap + human-curated §3.6) — live paths resolve EXACT names only; anything
unmapped is dropped AND counted (monitor surfaces the count, never silent).

Usage: one KalshiDiscovery instance per competition:
    d = KalshiDiscovery("epl"); d.match_quotes(home_club_id, away_club_id)
"""
from __future__ import annotations

import json
from functools import lru_cache
import re
from dataclasses import dataclass

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import get
from prediction_market_soccer.ingest.soccer_ingest import club_id_of
from prediction_market_soccer.venues.base import OrderBook
from prediction_market_soccer.venues.kalshi.market_data import KalshiMarketData

PROD_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class KalshiMarketRef:
    ticker: str
    real_entity_type: str        # "team" | "player"
    real_entity_id: str | None   # canonical club_id (None if unmapped)
    label: str                   # yes_sub_title


@lru_cache(maxsize=64)
def _load_aliases_cached(comp_key: str, mtime: float) -> tuple:
    """(name, club_id) pairs for one competition, memoised on the file's mtime.

    Every KalshiDiscovery reads THIRTEEN alias files at construction (its own plus the
    twelve it merges into the global map), and the live poller builds one per competition
    with fixtures in the window — so a single cycle re-read and re-parsed the same files
    dozens of times. Keying on mtime keeps a bootstrap_aliases run picked up immediately
    without a restart.
    """
    p = CONFIG.paths.priors / f"aliases_{comp_key}.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return tuple((dict(doc.get("aliases") or {})).items())
    except Exception:
        return ()


def _load_aliases(comp_key: str) -> dict[str, str]:
    p = CONFIG.paths.priors / f"aliases_{comp_key}.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        mt = 0.0
    return dict(_load_aliases_cached(comp_key, mt))


class KalshiDiscovery:
    def __init__(self, comp_key: str, base_url: str = PROD_PUBLIC):
        self.comp = get(comp_key)
        self.md = KalshiMarketData(base_url)
        self.aliases = _load_aliases(comp_key)
        from prediction_market_soccer.ingest import store
        conn = store.init_db()
        self._club_ids = {r["club_id"] for r in conn.execute(
            "SELECT club_id FROM club_registry WHERE comp=?", (comp_key,))}
        # season-level markets (champion/top-N) can list clubs not yet in THIS comp's
        # registry (UCL league-phase clubs before the draw enters the fixture list) —
        # they resolve against the global registry; match markets stay comp-exact.
        self._global_ids = {r["club_id"] for r in conn.execute(
            "SELECT DISTINCT club_id FROM club_registry")}
        self._global_aliases: dict[str, str] = {}
        for c in ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "uel",
                  "uecl", "brasileirao", "argentina", "libertadores", "sudamericana"):
            for k, v in _load_aliases(c).items():
                self._global_aliases.setdefault(k, v)
        self.unmapped: list[str] = []   # names seen but unresolvable (monitor reads this)

    # ── entity resolution: exact alias → exact normalization → drop+count ─────
    def _resolve(self, label: str, *, scope: str = "comp") -> str | None:
        s = (label or "").strip()
        if s.lower().startswith("reg time:"):
            s = s.split(":", 1)[1].strip()
        if scope == "comp" and s in self.aliases:
            return self.aliases[s]
        if scope == "global" and s in self._global_aliases:
            return self._global_aliases[s]
        cid = club_id_of(s)
        pool = self._club_ids if scope == "comp" else self._global_ids
        if cid in pool:
            return cid
        if scope == "comp" and s in self._global_aliases and self._global_aliases[s] in pool:
            return self._global_aliases[s]
        if s and s.lower() not in ("tie", "draw"):
            self.unmapped.append(s)
        return None

    def _team_ref(self, ticker: str, label: str, *, scope: str = "global") -> KalshiMarketRef:
        return KalshiMarketRef(ticker=ticker, real_entity_type="team",
                               real_entity_id=self._resolve(label, scope=scope), label=label)

    # ── season-level: champion series (KXPREMIERLEAGUE-27 …) ─────────────────
    def champion_markets(self) -> list[KalshiMarketRef]:
        series = self.comp.kalshi.get("champion")
        if not series:
            return []
        refs: list[KalshiMarketRef] = []
        for ev in self.md.list_events(series, status="open"):
            for m in ev.get("markets", []):
                refs.append(self._team_ref(m["ticker"], m.get("yes_sub_title", "")))
        return refs

    def season_markets(self, family: str) -> list[KalshiMarketRef]:
        """Any season family from the registry map: 'top4' / 'relegation' / 'last' /
        'top8' / 'ro16' / 'ro8' / 'ro4' / 'finalist' — one market per club."""
        series = self.comp.kalshi.get(family)
        if not series:
            return []
        refs: list[KalshiMarketRef] = []
        for ev in self.md.list_events(series, status="open"):
            for m in ev.get("markets", []):
                refs.append(self._team_ref(m["ticker"], m.get("yes_sub_title", "")))
        return refs

    def topscorer_markets(self) -> list[dict]:
        series = self.comp.kalshi.get("topscorer")
        if not series:
            return []
        return [m for ev in self.md.list_events(series, status="open")
                for m in ev.get("markets", [])]

    def orderbook(self, ticker: str) -> OrderBook:
        return self.md.get_orderbook(ticker)

    # ── per-match 3-way (GAME series) ────────────────────────────────────────
    def match_index(self) -> dict[frozenset, dict]:
        """{frozenset(club_id, club_id): {teams:{cid:ticker}, tie:ticker, event}}.

        Sub-title parsing kept verbatim from WC: strip the KO "Reg Time:" prefix
        (UCL/UEL qualifiers title their 3-way "X vs Y: Regulation Time Moneyline");
        the draw leg is sub "Tie"/"Draw" or a ``-TIE`` ticker suffix."""
        if getattr(self, "_mi", None) is not None:
            return self._mi
        idx: dict[frozenset, dict] = {}
        for ev in self.md.list_events(self.comp.kalshi["game"], status="open"):
            teams: dict[str, str] = {}
            tie = None
            for m in ev.get("markets", []):
                sub = (m.get("yes_sub_title") or "").strip()
                raw = sub.split(":", 1)[1].strip() if sub.lower().startswith("reg time:") else sub
                if raw.lower() in ("tie", "draw") or (m.get("ticker", "").endswith("-TIE")):
                    tie = m["ticker"]
                    continue
                cid = self._resolve(sub)
                if cid:
                    teams[cid] = m["ticker"]
            if len(teams) == 2 and tie:
                idx[frozenset(teams)] = {"teams": teams, "tie": tie, "event": ev.get("event_ticker")}
        self._mi = idx
        return idx

    def match_quotes(self, home_id: str, away_id: str) -> dict[str, dict] | None:
        entry = self.match_index().get(frozenset({home_id, away_id}))
        if not entry:
            return None

        def q(ticker):
            ob = self.orderbook(ticker)
            return {"ask": float(ob.yes_ask) if ob.yes_ask is not None else None,
                    "bid": float(ob.yes_bid) if ob.yes_bid is not None else None}

        out: dict[str, dict] = {("home" if cid == home_id else "away"): q(t)
                                for cid, t in entry["teams"].items()}
        out["draw"] = q(entry["tie"])
        return out

    # ── per-tie 2-way advance (ADVANCE series; caps.advance fixtures only) ────
    def advance_index(self) -> dict[frozenset, dict]:
        series = self.comp.kalshi.get("advance")
        if getattr(self, "_ai", None) is not None:
            return self._ai
        idx: dict[frozenset, dict] = {}
        if series:
            for ev in self.md.list_events(series, status="open"):
                teams: dict[str, str] = {}
                for m in ev.get("markets", []):
                    sub = (m.get("yes_sub_title") or "").strip()
                    if sub.lower().endswith(" advances"):
                        sub = sub[: -len(" advances")].strip()
                    if sub.lower().startswith("to advance:"):
                        sub = sub.split(":", 1)[1].strip()
                    cid = self._resolve(sub)
                    if cid:
                        teams[cid] = m["ticker"]
                if len(teams) == 2:
                    idx[frozenset(teams)] = {"teams": teams, "event": ev.get("event_ticker")}
        self._ai = idx
        return idx

    def advance_quotes(self, home_id: str, away_id: str) -> dict[str, dict] | None:
        entry = self.advance_index().get(frozenset({home_id, away_id}))
        if not entry:
            return None

        def q(ticker):
            ob = self.orderbook(ticker)
            return {"ask": float(ob.yes_ask) if ob.yes_ask is not None else None,
                    "bid": float(ob.yes_bid) if ob.yes_bid is not None else None}

        return {("home" if cid == home_id else "away"): q(t)
                for cid, t in entry["teams"].items()}

    # ── totals / corners ladders (title-parsed pair, line-keyed markets) ─────
    def _pair_from_title(self, title: str) -> frozenset | None:
        head = (title or "").split(":")[0]
        parts = re.split(r"\s+vs\.?\s+", head, flags=re.IGNORECASE)
        if len(parts) != 2:
            return None
        c1 = self._resolve(parts[0].strip())
        c2 = self._resolve(parts[1].strip())
        if c1 and c2:
            return frozenset({c1, c2})
        return None

    def totals_index(self) -> dict[frozenset, dict]:
        if getattr(self, "_ti", None) is not None:
            return self._ti
        idx: dict[frozenset, dict] = {}
        series = self.comp.kalshi.get("total")
        if series:
            for ev in self.md.list_events(series, status="open"):
                pair = self._pair_from_title(ev.get("title") or "")
                if not pair:
                    continue
                lines: dict[float, str] = {}
                for m in ev.get("markets", []):
                    fs = m.get("floor_strike")
                    if fs is not None:
                        lines[float(fs)] = m["ticker"]
                if lines:
                    idx[pair] = {"lines": lines, "event": ev.get("event_ticker")}
        self._ti = idx
        return idx

    def totals_quotes(self, home_id: str, away_id: str, line: float = 2.5) -> dict[str, dict] | None:
        entry = self.totals_index().get(frozenset({home_id, away_id}))
        if not entry or line not in entry["lines"]:
            return None
        ob = self.orderbook(entry["lines"][line])
        over_ask = float(ob.yes_ask) if ob.yes_ask is not None else None
        over_bid = float(ob.yes_bid) if ob.yes_bid is not None else None
        under_ask = (1.0 - over_bid) if over_bid is not None else None
        under_bid = (1.0 - over_ask) if over_ask is not None else None
        return {"over": {"ask": over_ask, "bid": over_bid},
                "under": {"ask": under_ask, "bid": under_bid}}

    def corners_index(self) -> dict[frozenset, dict]:
        if getattr(self, "_ci", None) is not None:
            return self._ci
        idx: dict[frozenset, dict] = {}
        series = self.comp.kalshi.get("corners")
        if series:
            for ev in self.md.list_events(series, status="open"):
                pair = self._pair_from_title(ev.get("title") or "")
                if not pair:
                    continue
                lines: dict[float, str] = {}
                for m in ev.get("markets", []):
                    n = m.get("floor_strike")
                    if n is None:
                        n = m.get("cap_strike")
                    if n is not None:
                        lines[float(n) - 0.5] = m["ticker"]   # "N+" ⇒ Over (N-0.5)
                if lines:
                    idx[pair] = {"lines": lines, "event": ev.get("event_ticker")}
        self._ci = idx
        return idx

    def corners_quotes(self, home_id: str, away_id: str) -> dict[float, dict] | None:
        entry = self.corners_index().get(frozenset({home_id, away_id}))
        if not entry:
            return None
        out: dict[float, dict] = {}
        for line, ticker in entry["lines"].items():
            ob = self.orderbook(ticker)
            over_ask = float(ob.yes_ask) if ob.yes_ask is not None else None
            over_bid = float(ob.yes_bid) if ob.yes_bid is not None else None
            out[line] = {
                "ask": over_ask, "bid": over_bid,
                "under_ask": (1.0 - over_bid) if over_bid is not None else None,
                "under_bid": (1.0 - over_ask) if over_ask is not None else None,
            }
        return out or None


if __name__ == "__main__":
    for key in ("epl", "ucl"):
        d = KalshiDiscovery(key)
        mi = d.match_index()
        print(f"— {key}: {len(mi)} match events mapped; unmapped names: {sorted(set(d.unmapped))}")
        for pair, entry in list(mi.items())[:2]:
            a, b = sorted(pair)
            q = d.match_quotes(a, b)
            print(f"  {a} v {b}: {q}")
        champ = d.champion_markets()
        ok = [r for r in champ if r.real_entity_id]
        print(f"  champion series: {len(champ)} markets, {len(ok)} mapped")