"""Kalshi World Cup market discovery (plan 01 §3, 12 §9). Public, no auth.

Market data is public, so discovery uses the PROD public host regardless of the
trading environment (we want the real WC markets as reference, even while
execution is demo/gated). Discovers the champion, golden-boot and other WC
series, and maps each market to a canonical real-world entity (team_id / player).

Entity mapping: for the champion event `KXMENWORLDCUP-26`, each market's
`yes_sub_title` is the team name (France, Spain, …) — mapped via our canonical
alias table. The rigorous fallback (plan 12 §9) is structured_targets /
milestones (`home_team_id`/`away_team_id`); used where titles are ambiguous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from prediction_market.ingest.prior_ingest import canonical_team_name, load_prior, team_id
from prediction_market.venues.base import OrderBook
from prediction_market.venues.kalshi.market_data import KalshiMarketData

PROD_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"

# Known WC series anchors (plan 01 §3).
CHAMPION_SERIES = "KXMENWORLDCUP"
GOLDEN_BOOT_SERIES = "KXWCGOALLEADER"
MATCH_SERIES = "KXWCGAME"        # single-match 3-way (home / draw=Tie / away)
TOTALS_SERIES = "KXWCTOTAL"      # single-match total goals (YES = Over N.5; markets per line)


@dataclass(frozen=True)
class KalshiMarketRef:
    ticker: str
    real_entity_type: str        # "team" | "player"
    real_entity_id: str | None   # canonical team_id (or None if unmapped)
    label: str                   # yes_sub_title


class KalshiDiscovery:
    def __init__(self, base_url: str = PROD_PUBLIC):
        self.md = KalshiMarketData(base_url)
        self._team_ids = {t.team_id for t in load_prior().teams}

    def _team_ref(self, ticker: str, label: str) -> KalshiMarketRef:
        tid = team_id(canonical_team_name(label or ""))
        return KalshiMarketRef(
            ticker=ticker, real_entity_type="team",
            real_entity_id=tid if tid in self._team_ids else None, label=label,
        )

    def champion_markets(self) -> list[KalshiMarketRef]:
        """48 team markets of the Men's World Cup winner event (mapped to team_id)."""
        events = self.md.list_events(CHAMPION_SERIES, status="open")
        refs: list[KalshiMarketRef] = []
        for ev in events:
            for m in ev.get("markets", []):
                refs.append(self._team_ref(m["ticker"], m.get("yes_sub_title", "")))
        return refs

    def golden_boot_markets(self) -> list[dict]:
        """Golden-boot player markets (entity mapping is player; left raw for now)."""
        events = self.md.list_events(GOLDEN_BOOT_SERIES, status="open")
        return [m for ev in events for m in ev.get("markets", [])]

    def orderbook(self, ticker: str) -> OrderBook:
        return self.md.get_orderbook(ticker)

    def match_index(self) -> dict[frozenset, dict]:
        """{frozenset(team_id, team_id): {teams:{tid:ticker}, tie:ticker, event}} for KXWCGAME.

        Maps each single-match event by the pair of canonical team_ids (parsed
        from each market's yes_sub_title team name). Cached per instance.
        """
        if getattr(self, "_mi", None) is not None:
            return self._mi
        idx: dict[frozenset, dict] = {}
        for ev in self.md.list_events(MATCH_SERIES, status="open"):
            teams: dict[str, str] = {}
            tie = None
            for m in ev.get("markets", []):
                sub = (m.get("yes_sub_title") or "").strip()
                if sub.lower() in ("tie", "draw"):
                    tie = m["ticker"]
                    continue
                tid = team_id(canonical_team_name(sub))
                if tid in self._team_ids:
                    teams[tid] = m["ticker"]
            if len(teams) == 2 and tie:
                idx[frozenset(teams)] = {"teams": teams, "tie": tie, "event": ev.get("event_ticker")}
        self._mi = idx
        return idx

    def match_quotes(self, home_id: str, away_id: str) -> dict[str, dict] | None:
        """{home/draw/away: {'ask': yes_ask, 'bid': yes_bid}} for a match (None if no market)."""
        entry = self.match_index().get(frozenset({home_id, away_id}))
        if not entry:
            return None

        def q(ticker):
            ob = self.orderbook(ticker)
            return {"ask": float(ob.yes_ask) if ob.yes_ask is not None else None,
                    "bid": float(ob.yes_bid) if ob.yes_bid is not None else None}

        out: dict[str, dict] = {("home" if tid == home_id else "away"): q(t)
                                for tid, t in entry["teams"].items()}
        out["draw"] = q(entry["tie"])
        return out

    def totals_index(self) -> dict[frozenset, dict]:
        """{frozenset(team_id, team_id): {lines:{line: ticker}, event}} for KXWCTOTAL.

        Each event ("Jordan vs Argentina: Total Goals") has one market per Over line
        (floor_strike = 0.5/1.5/2.5/…; YES = Over). Team pair parsed from the event
        title. Cached per instance."""
        if getattr(self, "_ti", None) is not None:
            return self._ti
        idx: dict[frozenset, dict] = {}
        for ev in self.md.list_events(TOTALS_SERIES, status="open"):
            title = ev.get("title") or ""             # "Jordan vs Argentina: Total Goals"
            head = title.split(":")[0]
            parts = re.split(r"\s+vs\.?\s+", head, flags=re.IGNORECASE)
            if len(parts) != 2:
                continue
            t1 = team_id(canonical_team_name(parts[0].strip()))
            t2 = team_id(canonical_team_name(parts[1].strip()))
            if not (t1 in self._team_ids and t2 in self._team_ids):
                continue
            lines: dict[float, str] = {}
            for m in ev.get("markets", []):
                fs = m.get("floor_strike")
                if fs is not None:
                    lines[float(fs)] = m["ticker"]
            if lines:
                idx[frozenset({t1, t2})] = {"lines": lines, "event": ev.get("event_ticker")}
        self._ti = idx
        return idx

    def totals_quotes(self, home_id: str, away_id: str, line: float = 2.5) -> dict[str, dict] | None:
        """{over/under: {'ask','bid'}} for a match's total-goals line (None if no market).

        YES = Over `line`; Under is the NO side (complement of the YES book)."""
        entry = self.totals_index().get(frozenset({home_id, away_id}))
        if not entry or line not in entry["lines"]:
            return None
        ob = self.orderbook(entry["lines"][line])
        over_ask = float(ob.yes_ask) if ob.yes_ask is not None else None
        over_bid = float(ob.yes_bid) if ob.yes_bid is not None else None
        # NO (Under) prices are the complement of the YES (Over) book.
        under_ask = (1.0 - over_bid) if over_bid is not None else None
        under_bid = (1.0 - over_ask) if over_ask is not None else None
        return {"over": {"ask": over_ask, "bid": over_bid},
                "under": {"ask": under_ask, "bid": under_bid}}


if __name__ == "__main__":
    d = KalshiDiscovery()
    champ = d.champion_markets()
    mapped = [r for r in champ if r.real_entity_id]
    print(f"Kalshi champion event: {len(champ)} markets, {len(mapped)} mapped to canonical team_id")
    for r in mapped[:5]:
        ob = d.orderbook(r.ticker)
        print(f"  {r.label:<16} {r.ticker:<22} yes_ask={ob.yes_ask} yes_bid={ob.yes_bid}")
