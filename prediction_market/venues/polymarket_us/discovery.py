"""Polymarket US World Cup single-match discovery (plan 07, 08). Read-only.

Polymarket US lists full single-match markets (3-way result + exact scores +
halftime + totals + props), tradable like Kalshi (the only blocker to trading is
USDC.e funding + the trading gate). Event slug: ``fwc-{home}-{away}-{ET-date}``;
the 3-way result markets are ``atc-{event}-{teamcode}`` and ``atc-{event}-draw``.

NOTE on the SDK envelope: ``events.retrieve_by_slug`` returns ``{"event": {...}}``
— the markets are under ``["event"]["markets"]`` (this bit me once).

Frugal: builds the team→code map from ONE events search, then per match does
1 retrieve + reads the 3 result markets' prices (US REST is 60/min).
"""
from __future__ import annotations

import os
import re

from prediction_market.ingest.prior_ingest import canonical_team_name, load_prior, team_id

_WIN_RE = re.compile(r"Will\s+(.+?)\s+win", re.IGNORECASE)


class PolymarketUSDiscovery:
    def __init__(self, client=None):
        if client is None:
            from polymarket_us import PolymarketUS
            client = PolymarketUS(key_id=os.environ["PMUS_KEY_ID"], secret_key=os.environ["PMUS_SECRET"])
        self.c = client
        self._team_ids = {t.team_id for t in load_prior().teams}
        self._codes: dict[str, str] | None = None   # canonical_team_id -> us code

    def code_map(self) -> dict[str, str]:
        """{canonical_team_id: us 3-letter code} parsed from fwc-* event slugs+titles."""
        if self._codes is not None:
            return self._codes
        from polymarket_us.types.search import SearchParams

        codes: dict[str, str] = {}
        for q in ("World Cup", "FIFA", "World Cup match", "vs", "Group", "World Cup group"):
            for e in self.c.search.query(SearchParams(query=q, limit=100, status="active")).get("events", []):
                slug, title = e.get("slug", ""), e.get("title", "")
                m = re.match(r"fwc-([a-z]{3})-([a-z]{3})-", slug)
                if not m or " vs" not in title.lower():
                    continue
                c1, c2 = m.group(1), m.group(2)
                parts = re.split(r"\s+vs\.?\s+", title, flags=re.IGNORECASE)
                if len(parts) != 2:
                    continue
                for code, name in ((c1, parts[0]), (c2, parts[1])):
                    tid = team_id(canonical_team_name(name.strip()))
                    if tid in self._team_ids:
                        codes[tid] = code
        self._codes = codes
        return codes

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

    def match_quotes(self, home_id: str, away_id: str, et_date: str) -> dict[str, float] | None:
        """Raw 3-way {home, draw, away} prices for a match (None if not found).

        ``et_date`` is the US ET date string 'YYYY-MM-DD' (the slug date).
        """
        codes = self.code_map()
        hc, ac = codes.get(home_id), codes.get(away_id)
        if not (hc and ac):
            return None
        for ev_slug in (f"fwc-{hc}-{ac}-{et_date}", f"fwc-{ac}-{hc}-{et_date}"):
            try:
                ev = self.c.events.retrieve_by_slug(ev_slug).get("event") or {}
            except Exception:
                continue
            markets = ev.get("markets") or []
            if not markets:
                continue
            prefix = f"atc-{ev_slug}-"
            out: dict[str, float] = {}
            for m in markets:
                s = m.get("slug", "")
                if not s.startswith(prefix):
                    continue
                tok = s[len(prefix):]
                if "-" in tok:            # only single-token = the 3-way result legs
                    continue
                if tok == "draw":
                    out["draw"] = self._price(s)
                else:
                    win = _WIN_RE.search(m.get("question", ""))
                    tid = team_id(canonical_team_name(win.group(1))) if win else None
                    if tid == home_id:
                        out["home"] = self._price(s)
                    elif tid == away_id:
                        out["away"] = self._price(s)
            if {"home", "draw", "away"} <= set(out):
                return out
        return None

    def advance_quotes(self, home_id: str, away_id: str, et_date: str) -> dict[str, dict] | None:
        """Raw 2-way {home, away} advance prices for a knockout match (None if not found).

        Poly US lists a dedicated per-match advance market inside the same ``fwc-`` event:
        ``aadc-fwc-{home}-{away}-{date}-to-advance-{teamcode}`` ("Will <Team> advance against
        <Opp> on <date>?") — YES = that team advances the tie (ET + penalties included). No
        draw leg. Mirrors match_quotes; ``et_date`` is the ET 'YYYY-MM-DD' slug date."""
        codes = self.code_map()
        hc, ac = codes.get(home_id), codes.get(away_id)
        if not (hc and ac):
            return None
        for ev_slug in (f"fwc-{hc}-{ac}-{et_date}", f"fwc-{ac}-{hc}-{et_date}"):
            try:
                ev = self.c.events.retrieve_by_slug(ev_slug).get("event") or {}
            except Exception:
                continue
            markets = ev.get("markets") or []
            if not markets:
                continue
            prefix = f"aadc-{ev_slug}-to-advance-"
            out: dict[str, dict] = {}
            for m in markets:
                s = m.get("slug", "")
                if not s.startswith(prefix):
                    continue
                code = s[len(prefix):]
                if code == hc:
                    out["home"] = self._price(s)
                elif code == ac:
                    out["away"] = self._price(s)
            if {"home", "away"} <= set(out):
                return out
        return None

    def totals_quotes(self, home_id: str, away_id: str, et_date: str,
                      line: float = 2.5) -> dict[str, dict] | None:
        """{over/under: {'ask','bid'}} for a match's total-goals line (None if not found).

        Poly US totals market slug: ``tsc-fwc-{home}-{away}-{date}-{N}pt5`` ("Will the
        total in X be more than N.5?") — YES = Over. Under is the complement of the YES
        book. ``line`` like 2.5 → token ``2pt5``."""
        codes = self.code_map()
        hc, ac = codes.get(home_id), codes.get(away_id)
        if not (hc and ac):
            return None
        tok = f"{int(line)}pt5" if line == int(line) + 0.5 else str(line).replace(".", "pt")
        for ev_slug in (f"fwc-{hc}-{ac}-{et_date}", f"fwc-{ac}-{hc}-{et_date}"):
            try:
                ev = self.c.events.retrieve_by_slug(ev_slug).get("event") or {}
            except Exception:
                continue
            for m in ev.get("markets") or []:
                if m.get("slug", "") == f"tsc-{ev_slug}-{tok}":
                    p = self._price(m["slug"])
                    if not p:
                        return None
                    over_ask, over_bid = p.get("ask"), p.get("bid")
                    return {"over": {"ask": over_ask, "bid": over_bid},
                            "under": {"ask": (1.0 - over_bid) if over_bid is not None else None,
                                      "bid": (1.0 - over_ask) if over_ask is not None else None}}
        return None


if __name__ == "__main__":
    d = PolymarketUSDiscovery()
    print("US code map (sample):", dict(list(d.code_map().items())[:6]))
    for h, a, dt in [("iraq", "norway", "2026-06-16"), ("argentina", "algeria", "2026-06-16")]:
        print(f"  {h} vs {a} ({dt}):", d.match_quotes(h, a, dt))
