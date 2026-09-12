"""exec/kalshi_mirror.py — execute immutable forward paper decisions on DEMO.

CURRENT LIVE PATH
    run_cycle calls exec.demo_forward after the independent paper lifecycle has
    durably recorded its decisions. Demo only executes that fixed side, stake and
    exit intent; it never generates paper entries/exits or depends on public
    market discovery. Missing liquidity is retried with fresh execution context,
    and every IOC attempt and outcome is preserved separately.

    The older calculation/scanner helpers below remain for compatibility and
    historical inspection. They are NOT called by the current live run_cycle.

LEGACY IMPLEMENTATION NOTES (describe those compatibility helpers only)

WHAT IS MIRRORED (and nothing else)
    The paper ledger behind the 准确度 & 盈亏 view (ops/settle_bets + ops/performance_report)
    records, per match, up to two positions and one close-out rule:
      * PRE   — the pre-match bet decided on the PRE milestone quotes (≤20' before kickoff):
                decision_model.decide() picks the most-underpriced side sized [$0.2, $2]; when
                no side clears the edge bar the ledger bets the model ARGMAX at the flat $1.
      * INPLAY — the causal in-play relative-value entry (settle_bets._inplay_entry): the FIRST
                milestone after a goal/red card where the live fair beats the ask by ≥3¢,
                edge-weighted ¼-Kelly stake in the same envelope.
      * EXIT  — strategy.smart_exit: SELL a held pick the moment the market bid over-reacts
                above the live model fair by the overshoot trigger (≤22¢, shrinking near
                100¢), regulation minutes only; otherwise hold to the whistle and let the
                90' 3-way settle.
    The ledger is frozen RETROSPECTIVELY at settlement from the milestone_snapshot rows the
    live loop wrote. This module runs the SAME decision functions on the SAME rows at the
    moment they are written, and places the equivalent order on the demo venue — so the
    demo account follows the ledger by construction rather than by re-implementation:
      * PRE   → the decision block of performance_report.match_pick, replicated verbatim for
                an unsettled fixture (match_pick itself refuses to price a match without a
                final score), fed the PRE row through quotes_from_milestone_row and the same
                PIT strength / PIT calibration / motivation tilt / PIT form.
      * INPLAY → settle_bets._inplay_entry called live on the fixture (it scans exactly the
                milestone rows that exist so far and returns the first tradable one).
      * EXIT  → the smart_exit rule evaluated every live cycle on the CURRENT minute/score
                and the CURRENT demo bid (the ledger evaluates it later on recorded price
                points — same rule, finer time grid here).

WHAT IS REUSED (call-only — crypto_trading is never modified)
    crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient: the V2 event-
    contract order client (POST /portfolio/events/orders, side bid|ask, fixed-point count and
    price, time_in_force). Its wire format was verified against Kalshi's Create Order V2 spec
    and live on the demo host (201 create, 200 cancel) before this module was written. Sells
    go through the same client as "buy the other side" — its own documented semantics: an
    ask @ p on the single book is a sale of the YES leg at p.

SAFETY (all four must hold, checked every cycle, never overridable from data)
    1. KALSHI_DEMO_MIRROR=true in the environment (default off);
    2. the soccer config's KALSHI_ENV is "demo";
    3. crypto_trading's own kalshi_env() resolves to "demo";
    4. the constructed client's base URL is the demo host.
    Plus: per-order notional ≤ the strategy's own stake ceiling ($2), a daily order budget,
    an open-position ceiling, IOC-only (no resting orders to leak), and one row per
    (fixture, track) — the database is the idempotency key, written BEFORE the HTTP call.

KNOWN, IRREDUCIBLE DIFFERENCES FROM THE LEDGER (recorded, not hidden)
    * Prices: the ledger's entry/exit ¢ are the best-venue quotes of the recorded row (Poly
      first, then Kalshi PROD); the mirror decides on those same numbers but can only FILL on
      the Kalshi demo book. ledger_* / exit_bid_c hold the ledger's numbers, ask_c / avg_fill_c
      / exit_avg_c the demo fills — reconcile the two, don't expect them equal.
    * Hindsight in smart_exit: the ledger only evaluates an exit when ≥3 milestone points
      exist for the match (a data-sufficiency test made after the match). Live, the mirror
      sells at the first triggering milestone; if the loop later misses milestones (<3 in
      total) the ledger will show "held" where the mirror sold. Rare (capture has an 8-minute
      grace per milestone) and visible in kalshi_mirror_eval.
    * Poly Global ticks: on the ~1% of club fixtures Polymarket Global does list, the
      ledger's in-play entry/exit read ~13 per-minute ticks backfilled AFTER the match; live
      only the 5 milestones exist. The mirror follows the milestone path.
    * ClubElo outage on the day: today's PIT prior is built without the Elo anchor while the
      ledger, freezing days later, reads that date's CSV from history.

    python -m prediction_market_soccer.exec.kalshi_mirror --status
    python -m prediction_market_soccer.exec.kalshi_mirror --once          # one live cycle
    python -m prediction_market_soccer.exec.kalshi_mirror --build-pit-cache
"""
from __future__ import annotations

from prediction_market_soccer.ops.maintenance_gate import writer

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG

ENV_FLAG = "KALSHI_DEMO_MIRROR"
_TRUTHY = ("1", "true", "yes", "on")

# Envelope guards — the strategy's own stake ceiling is the per-order notional cap.
MAX_ORDER_USD = float(CONFIG.decision.max_stake_usd)          # $2.00
MAX_ORDERS_PER_DAY = 80
MAX_OPEN_POSITIONS = 40
MAX_ATTEMPTS = 4                # venue-side failures (5xx / IOC missed the ask / no ask yet) retried up to this
PRE_WINDOW_BEFORE_MIN = 25      # a PRE row is stashed ≤20' pre-kickoff; scan a little wider
PRE_LATE_GRACE_MIN = 0          # PRE means observed and submitted strictly before kickoff
_REG_MAX_MIN = 95               # regulation incl. stoppage (same clamp as smart_exit)
_PIT_CACHE_MAX_AGE_H = 36.0
_LOG = CONFIG.paths.logs / "kalshi_mirror.jsonl"
_SIDES = ("home", "draw", "away")
_FINISHED = ("FT", "AET", "PEN", "AWD", "WO")
_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


# ── switches ──────────────────────────────────────────────────────────────────
def enabled() -> bool:
    return os.getenv(ENV_FLAG, "false").strip().lower() in _TRUTHY


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _log(_event: str, **fields) -> None:
    rec = {"ts": _iso(_now()), "event": _event, **fields}
    try:
        CONFIG.paths.ensure()
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    print(f"[kalshi_mirror] {_event}: " + ", ".join(f"{k}={v}" for k, v in fields.items()
                                                     if k not in ("raw",)))


# ── the broker: the crypto_trading client, called as-is ──────────────────────
class DemoBroker:
    """Thin adapter over crypto_trading's KalshiEventOrderClient (never modified).

    Asserts the demo host at construction and on every order. Reads (book, positions,
    balance, orders) use the client's authed GET; orders use its create_order with the
    documented yes/no semantics; count is whole contracts.
    """

    def __init__(self):
        from crypto_trading.crypto_common.config import kalshi_env as _crypto_env
        from crypto_trading.crypto_common.kalshi.ratelimit import KalshiRateLimiter
        from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient
        if CONFIG.venue.kalshi_env != "demo":
            raise RuntimeError(f"mirror refused: soccer KALSHI_ENV={CONFIG.venue.kalshi_env!r} is not demo")
        if _crypto_env() != "demo":
            raise RuntimeError(f"mirror refused: crypto kalshi_env()={_crypto_env()!r} is not demo")
        self.c = KalshiEventOrderClient(env="demo")
        if "demo.kalshi" not in self.c.base:
            raise RuntimeError(f"mirror refused: client base is not the demo host: {self.c.base}")
        self.limiter = KalshiRateLimiter()

    # reads
    def _book_capture(self, ticker: str):
        from prediction_market_soccer.venues.kalshi.market_data import best_prices
        if not self.limiter.acquire_read():
            raise TimeoutError('Demo read rate limit')
        started = _now().isoformat()
        response = self.c._authed("GET", f"/markets/{ticker}/orderbook")
        response.raise_for_status()
        raw = response.json()
        received = _now().isoformat()
        return best_prices(raw, market_key=ticker), raw, started, received

    def book(self, ticker: str):
        return self._book_capture(ticker)[0]

    def book_observation(self, ticker: str, binding: dict):
        """Same GET's BBO and raw bytes; never attach evidence from another read."""
        from prediction_market_soccer.util.market_identity import validate_binding
        from prediction_market_soccer.util.quote_evidence import make_receipt, quote_from_receipt
        if not validate_binding(binding, environment='demo') or binding['market_id'] != ticker or binding['provider'] != 'kalshi':
            raise ValueError('Demo orderbook requires its exact reviewed contract binding')
        book, raw, started, received = self._book_capture(ticker)
        # venues/ keeps Decimal for money (plan 01 §4.3); the receipt body is canonical
        # JSON, so sizes cross the boundary as strings — the same convention as
        # venues/kalshi/discovery.py. Raw Decimal here made receipt_hash raise TypeError
        # and blocked every demo entry on a book with real depth (2026-09-11).
        return quote_from_receipt(make_receipt(binding, ask=book.yes_ask, bid=book.yes_bid,
            raw=raw, request_started_at=started, received_at=received,
            ask_size=str(book.no_depth) if book.no_depth is not None else None,
            bid_size=str(book.yes_depth) if book.yes_depth is not None else None,
            derivation={'rule':'binary_complement','ask_from':'no_bid'}))

    def balance(self) -> dict:
        self.limiter.acquire_read()
        r = self.c._authed("GET", "/portfolio/balance")
        r.raise_for_status()
        return r.json()

    def positions(self) -> dict[str, float]:
        """{ticker: signed contracts} from the venue (positive = YES long)."""
        out: dict[str, float] = {}
        for p in self._pages('/portfolio/positions', 'market_positions', limit=200):
            try:
                import math
                value = float(p.get("position_fp") or 0.0)
                if not math.isfinite(value):
                    raise ValueError('Non-finite position')
                out[p["ticker"]] = value
            except (TypeError, ValueError):
                raise ValueError('Invalid demo position response')
        return out

    def _pages(self, path: str, key: str, **filters) -> list[dict]:
        """A complete demo listing or an exception; partial pages never mean absent."""
        from urllib.parse import urlencode
        out, cursor, seen = [], None, set()
        while True:
            params = dict(filters)
            if cursor:
                params['cursor'] = cursor
            if not self.limiter.acquire_read():
                raise TimeoutError('Demo read rate limit')
            response = self.c._authed('GET', path + '?' + urlencode(params))
            response.raise_for_status()
            doc = response.json()
            if not isinstance(doc.get(key), list):
                raise ValueError('Incomplete demo listing')
            out.extend(doc[key])
            cursor = doc.get('cursor')
            if not cursor:
                return out
            if cursor in seen:
                raise ValueError('Repeated demo listing cursor')
            seen.add(cursor)

    def markets(self, series_ticker: str) -> list[dict]:
        return self._pages('/markets', 'markets', series_ticker=series_ticker,
                           status='open', limit=1000)

    def events(self, series_ticker: str) -> list[dict]:
        return self._pages('/events', 'events', series_ticker=series_ticker,
                           status='open', with_nested_markets='true', limit=200)

    def market(self, ticker: str) -> dict:
        if not self.limiter.acquire_read():
            raise TimeoutError('Demo read rate limit')
        response = self.c._authed('GET', f'/markets/{ticker}')
        response.raise_for_status()
        return response.json()['market']

    def orders_for(self, ticker: str) -> list[dict]:
        return self._pages('/portfolio/orders', 'orders', ticker=ticker, limit=200)

    def fills_for_order(self, ticker: str, order_id: str) -> list[dict]:
        return [f for f in self._pages('/portfolio/fills', 'fills', ticker=ticker, order_id=order_id, limit=1000)
                if f.get('order_id') == order_id and f.get('ticker', f.get('market_ticker')) == ticker]

    def estimate_taker_fee(self, ticker: str, count: int, price: float) -> dict:
        """Current series schedule, conservative direct-member estimate; fills remain authoritative."""
        series = ticker.split('-')[0]
        cache = getattr(self,'_series_fees',{})
        if series not in cache:
            self.limiter.acquire_read()
            res = self.c._authed('GET',f'/series/{series}')
            res.raise_for_status()
            cache[series] = res.json()['series']
            self._series_fees = cache
        info = cache[series]
        if info.get('fee_type') != 'quadratic':
            raise ValueError('Unknown series taker fee type')
        from decimal import Decimal, ROUND_CEILING
        p,n,m = Decimal(str(price)),Decimal(count),Decimal(str(info.get('fee_multiplier')))
        if not m.is_finite() or m<0: raise ValueError('Unknown series fee multiplier')
        # Official July 2026 schedule: round cost + fee to $0.0001; no slippage beyond IOC limit.
        raw = Decimal('.07')*m*n*p*(1-p)
        fee = (n*p+raw).quantize(Decimal('.0001'),rounding=ROUND_CEILING)-n*p
        return {'fee_usd':float(fee),'fee_per_contract':float(fee/n), 'fee_multiplier':float(m),
                'source':'current_series_quadratic_schedule','precision':'estimated_before_fill',
                'observed_at':_iso(_now()),'series':series}

    # writes — IOC only: fills now or dies at the engine; nothing rests
    def _send(self, *, ticker: str, contract_side: str, count: int, price: float,
              client_order_id: str) -> dict:
        if "demo.kalshi" not in self.c.base:
            raise RuntimeError("mirror refused at send: not the demo host")
        if not (0.0 < price < 1.0):
            raise ValueError(f"price {price} outside (0,1)")
        if count < 1:
            raise ValueError("count must be ≥ 1")
        if contract_side == 'yes' and count * price > MAX_ORDER_USD + 1e-9:
            raise ValueError('Demo entry exceeds current order cap')
        if not self.limiter.acquire_write():
            raise TimeoutError('Demo write rate limit')
        # Reuse the client's verified V2 translation and authentication, but retain
        # the COMPLETE HTTP response: create_order truncates its body to 400 chars.
        from crypto_trading.crypto_common.kalshi.rest_event import EVENTS_ORDERS_V2
        sent = self.c.v2_body(ticker=ticker, contract_side=contract_side, count=int(count),
                              price_dollars=round(price, 4), client_order_id=client_order_id,
                              tif='immediate_or_cancel')
        response = self.c._authed('POST', EVENTS_ORDERS_V2, sent)
        body, parsed = {}, False
        try:
            import math
            body = response.json()
            filled, remaining = float(body['fill_count']), float(body['remaining_count'])
            avg = float(body['average_fill_price']) if body.get('average_fill_price') is not None else None
            parsed = (bool(body.get('order_id')) and math.isfinite(filled) and 0 <= filled <= count
                      and math.isfinite(remaining) and remaining == 0
                      and (filled == 0 or (avg is not None and math.isfinite(avg) and 0 <= avg <= 1)))
        except (ValueError, TypeError, KeyError):
            filled, remaining, avg = 0.0, 0.0, None
        ok = response.status_code in (200, 201)
        return {"ok": ok, "status_code": response.status_code, 'outcome_known': bool(ok and parsed),
                "order_id": body.get("order_id") if isinstance(body, dict) else None,
                "fill_count": filled, "remaining_count": remaining, "avg_fill": avg,
                "raw": response.text, "sent": sent}

    def buy_yes(self, ticker: str, count: int, ask: float, client_order_id: str) -> dict:
        """Take the YES ask: wire side=bid @ ask."""
        return self._send(ticker=ticker, contract_side="yes", count=count, price=ask,
                          client_order_id=client_order_id)

    def sell_yes(self, ticker: str, count: int, bid: float, client_order_id: str) -> dict:
        """Hit the YES bid. The client only speaks "buy": buying NO at (1 − bid) is, by its
        own documented translation, an ask @ bid on the single book — a sale of our YES leg."""
        return self._send(ticker=ticker, contract_side="no", count=count,
                          price=round(1.0 - bid, 4), client_order_id=client_order_id)


# ── sizing (the ledger's contract count, rounded to whole contracts) ─────────
def contracts(ask: float, stake_usd: float, *, cap_usd: float = MAX_ORDER_USD) -> int:
    """Whole contracts for a $stake at `ask` (0-1): the ledger's stake/(ask) rounded to the
    nearest contract, floored at 1, and never more than the strategy's own stake ceiling."""
    from prediction_market_soccer.util.pricing import contracts_for
    if not ask or ask <= 0:
        return 0
    n = max(1, int(round(contracts_for(ask * 100.0, stake_usd))))
    while n > 1 and n * ask > cap_usd + 1e-9:
        n -= 1
    return n


# ── PIT records cache (for the ledger's as-of calibration) ───────────────────
def build_pit_cache(conn) -> dict:
    """Write data/output/pit_records.json — settle_bets._pit_py over the 60-day window.

    _pit_cal fits the calibration on these; the ledger computes them at settlement. They
    cost ~140 strength fits, so the live loop never builds them inline: settle_reports (the
    background post-settle job) and the daily refresh keep this cache warm."""
    from prediction_market_soccer.ops.settle_bets import _pit_py
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    recs = _pit_py(conn, cmap)
    doc = {"built_at": _iso(_now()), "n": len(recs), "records": recs}
    CONFIG.paths.ensure()
    from prediction_market_soccer.ops.run_status import atomic_json
    path = CONFIG.paths.output / "pit_records.json"
    atomic_json(path, doc)
    return {"n": len(recs), "path": str(path)}


_PIT_REBUILD_SPAWNED = False


def load_pit_records() -> list | None:
    """The cached PIT records, or None only when there is NO cache at all.

    A stale cache (older than _PIT_CACHE_MAX_AGE_H) is still USED — the ledger's as-of
    calibration moves slowly and a slightly old fit is far closer to it than no pre-match
    mirror at all — but a background rebuild is spawned once per process so the next
    cycles read a fresh one. A missing cache also spawns the rebuild and defers."""
    global _PIT_REBUILD_SPAWNED
    doc = None
    try:
        doc = json.loads((CONFIG.paths.output / "pit_records.json").read_text(encoding="utf-8"))
        built = datetime.fromisoformat(doc["built_at"])
        stale = (_now() - built) > timedelta(hours=_PIT_CACHE_MAX_AGE_H)
    except (OSError, ValueError, KeyError):
        doc, stale = None, True
    if stale and not _PIT_REBUILD_SPAWNED:
        _PIT_REBUILD_SPAWNED = True
        try:
            import subprocess
            import sys
            subprocess.Popen([sys.executable, "-m", "prediction_market_soccer.exec.kalshi_mirror", "--build-pit-cache"],
                             stdout=open(CONFIG.paths.logs / "kalshi_mirror_pitcache.log", "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
            _log("pit_cache_rebuild_spawned", had_cache=doc is not None)
        except Exception as e:  # noqa: BLE001
            _log("pit_cache_rebuild_spawn_failed", error=str(e)[:160])
    if doc is None:
        return None
    if stale:
        _log("pit_cache_stale_used", built_at=doc.get("built_at"))
    return doc.get("records") or []


# ── helpers shared by the three legs ─────────────────────────────────────────
def _cmap(conn) -> dict:
    return {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}


def _fixture(conn, fid: int):
    return conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts, round, "
        "raw_json, league_id, status_short, elapsed FROM fixture WHERE api_id=?", (fid,)).fetchone()


class _Tickers:
    """(comp, home, away) → {side: ticker} via the club discovery (prod public listing; the
    demo exchange carries the identical event/market tickers — verified).

    ``failed`` records the competitions whose discovery call itself failed (rate limit,
    outage). A caller must be able to tell "the venue does not list this pairing" from
    "we could not ask" — recording the second as the first is how a transient 429 becomes
    a permanent 'this competition has no market' in a measurement table."""

    def __init__(self):
        self._disc: dict = {}
        self.failed: set[str] = set()

    def for_match(self, comp: str, hi: str, ai: str) -> dict | None:
        from prediction_market_soccer.venues.kalshi.discovery import KalshiDiscovery
        if comp not in self._disc:
            try:
                self._disc[comp] = KalshiDiscovery(comp).match_index()
                self.failed.discard(comp)
            except Exception as e:  # noqa: BLE001 — venue outage on one comp ≠ all comps
                _log("discovery_failed", comp=comp, error=str(e)[:160])
                self._disc[comp] = {}
                self.failed.add(comp)
        e = self._disc[comp].get(frozenset({hi, ai}))
        if not e:
            return None
        t = {"home": e["teams"].get(hi), "away": e["teams"].get(ai), "draw": e.get("tie")}
        return t if all(t.values()) else None

    def index_ok(self, comp: str) -> bool:
        """True when this competition's listing was actually retrieved (and is non-empty)."""
        return comp not in self.failed and bool(self._disc.get(comp))


class _Strength:
    """PIT strength per (kickoff day, comp) — the model the ledger prices this match with."""

    def __init__(self, conn):
        self.conn = conn
        self._m: dict = {}

    def get(self, kickoff_ts: str, comp: str):
        from prediction_market_soccer.ops.performance_report import _pit_strength
        k = (kickoff_ts[:10], comp)
        if k not in self._m:
            self._m[k] = _pit_strength(self.conn, kickoff_ts, comp)
        return self._m[k]


def _today_orders(conn) -> int:
    day = _now().strftime("%Y-%m-%d")
    return conn.execute("SELECT COUNT(*) FROM kalshi_mirror WHERE submitted_at >= ?", (day,)).fetchone()[0]


def _open_positions(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM kalshi_mirror WHERE status='open'").fetchone()[0]


def _mark_eval(conn, fid: int, milestone: str, verdict: str) -> None:
    conn.execute("INSERT OR REPLACE INTO kalshi_mirror_eval (fixture_api_id, milestone, evaluated_at, verdict) "
                 "VALUES (?,?,?,?)", (fid, milestone, _iso(_now()), verdict[:200]))


# ── leg 1: the PRE bet ────────────────────────────────────────────────────────
def pre_decision(conn, fx, hi: str, ai: str, pre_row, records: list, strength: _Strength, *, decision_at=None) -> dict | None:
    """The ledger's pre-match decision for an UNSETTLED fixture — the decision block of
    performance_report.match_pick, line for line, minus everything that needs a final score.

    Returns {side, stake_usd, bet_kind, ledger_entry_c, ledger_venue, net_edge, model} or
    None when the fixture cannot be priced."""
    from prediction_market_soccer.model.form_strength import form_index
    from prediction_market_soccer.model.match_pricing import is_knockout, price_match_calibrated
    from prediction_market_soccer.model.motivation import motivation_multipliers
    from prediction_market_soccer.ops.performance_report import _fifa_ranks, _row_comp
    from prediction_market_soccer.ops.settle_bets import _conf, _pit_cal
    from prediction_market_soccer.strategy.decision_model import decide, quotes_from_milestone_row
    from prediction_market_soccer.util.pricing import to_cents

    comp = _row_comp(fx)
    decision_at = decision_at or _iso(_now())
    if not comp or not fx["kickoff_ts"]:
        return None
    knockout = is_knockout(fx["round"], comp)
    sm = strength.get(decision_at, comp)
    if not (hi in sm.ratings and ai in sm.ratings):
        return None
    cal = _pit_cal(records, decision_at)
    conf = _conf(cal)
    lam_mult = None
    mh, ma, motiv = motivation_multipliers(conn, _fifa_ranks(), hi, ai, fx["round"], CONFIG.model)
    if (mh, ma) != (1.0, 1.0):
        lam_mult = (mh, ma)
    mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=cal, lam_mult=lam_mult,
                                host_neutral=knockout)
    raw_mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=None, lam_mult=lam_mult,
                                    host_neutral=knockout)
    model = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
    # bookmaker consensus (pre-match book only) → the argmax bet's reference price
    bd = conn.execute(
        "SELECT AVG(p_home) bh, AVG(p_draw) bdr, AVG(p_away) ba FROM match_odds "
        "WHERE fixture_api_id=? AND bookmaker <> 'live_consensus' AND fetched_at<=?", (fx["api_id"],decision_at)).fetchone()
    if bd and bd["bh"] is not None:
        s = (bd["bh"] or 0) + (bd["bdr"] or 0) + (bd["ba"] or 0)
        price = {"home": bd["bh"] / s, "draw": bd["bdr"] / s, "away": bd["ba"] / s} if s else model
    else:
        price = model
    model_pick = max(_SIDES, key=lambda k: model[k])
    try:
        fi = form_index(conn, as_of=decision_at)
        form = {"home_z": fi[hi].form_z if hi in fi else None,
                "away_z": fi[ai].form_z if ai in fi else None}
    except Exception:  # noqa: BLE001 — the ledger tolerates a missing form index the same way
        form = None
    quotes = quotes_from_milestone_row(pre_row)
    d = decide(model, quotes, calib_confidence=conf, form=form, gate_open=True,
               conviction_side=(motiv or {}).get("conviction_side"))
    if d.side is not None:
        side, stake, kind, edge = d.side, d.stake_usd, "value", d.net_edge
    else:
        side, stake, kind, edge = model_pick, CONFIG.decision.base_stake_usd, "argmax", (model[model_pick] - price[model_pick])

    # the ledger's 入场¢ for the row: real PRE venue ask, Poly then Kalshi, else book de-vig
    if pre_row[f"poly_{side}_ask"] is not None:
        ledger_c, ledger_venue = to_cents(pre_row[f"poly_{side}_ask"]), "poly"
    elif pre_row[f"kalshi_{side}_ask"] is not None:
        ledger_c, ledger_venue = to_cents(pre_row[f"kalshi_{side}_ask"]), "kalshi"
    else:
        return None  # No observed offer for the chosen side: no forward paper entry or demo order.
    return {"side": side, "stake_usd": round(float(stake), 2), "bet_kind": kind,
            "ledger_entry_c": ledger_c, "ledger_venue": ledger_venue,
            "net_edge": (round(float(edge), 4) if edge is not None else None),
            "model": {k: round(v, 4) for k, v in model.items()},
            "raw_model": dict(zip(_SIDES, (raw_mp.p_home, raw_mp.p_draw, raw_mp.p_away))),
            "pit_status": "forward_recorded_model_version_not_fully_audited",
            "decision_at": decision_at,
            "cal": {"method": (cal or {}).get("method"), "param": (cal or {}).get("param"), "n": (cal or {}).get("n")}}


def _scan_pre(conn, broker: DemoBroker, tickers: _Tickers, strength: _Strength, records: list | None) -> list[dict]:
    now = _now()
    lo, hi_ = _iso(now - timedelta(minutes=PRE_LATE_GRACE_MIN)), _iso(now + timedelta(minutes=PRE_WINDOW_BEFORE_MIN))
    from prediction_market_soccer.config.leagues import active
    lids = tuple(c.api_football_id for c in active())
    rows = conn.execute(
        "SELECT ms.*, f.kickoff_ts kick, f.status_short st, f.elapsed el "
        "FROM milestone_snapshot ms JOIN fixture f ON f.api_id=ms.fixture_api_id "
        "WHERE ms.milestone='PRE' AND f.kickoff_ts BETWEEN ? AND ? AND f.league_id IN ({}) "
        "AND NOT EXISTS (SELECT 1 FROM kalshi_mirror_eval e WHERE e.fixture_api_id=ms.fixture_api_id "
        "               AND e.milestone='PRE')".format(",".join("?" * len(lids))),
        (lo, hi_, *lids)).fetchall()
    if not rows:
        return []
    if records is None:
        _log("pre_deferred", reason="pit_records cache missing/stale — build it (settle_reports / --build-pit-cache)",
             fixtures=[r["fixture_api_id"] for r in rows])
        return []          # NOT evaluated: the rows stay eligible for the next cycle
    cmap = _cmap(conn)
    actions = []
    for pr in rows:
        fid = pr["fixture_api_id"]
        fx = _fixture(conn, fid)
        if fx is None:
            _mark_eval(conn, fid, "PRE", "no_fixture"); continue
        from prediction_market_soccer.util.timing_provenance import live_snapshots, record_decision, _dt
        if _now() >= _dt(fx['kickoff_ts']):
            _mark_eval(conn, fid, "PRE", "late:at_or_after_kickoff"); continue
        observed = next((m for m in live_snapshots(conn, fid, _iso(_now())) if m['milestone'] == 'PRE'), None)
        if observed is None:
            _mark_eval(conn, fid, "PRE", "unverified_snapshot_provenance"); continue
        pr = observed
        if fx["status_short"] in _FINISHED or (fx["status_short"] in _LIVE and (fx["elapsed"] or 0) > PRE_LATE_GRACE_MIN):
            _mark_eval(conn, fid, "PRE", f"late:{fx['status_short']}/{fx['elapsed']}")
            _log("pre_skipped_late", fixture=fid, status=fx["status_short"], elapsed=fx["elapsed"]); continue
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            _mark_eval(conn, fid, "PRE", "unmapped_team"); continue
        try:
            dec = pre_decision(conn, fx, hi, ai, pr, records, strength, decision_at=_iso(_now()))
        except Exception as e:  # noqa: BLE001 — transient: NOT marked, retried next cycle
            _log("pre_decision_error", fixture=fid, error=str(e)[:200]); continue
        if dec is None:
            _mark_eval(conn, fid, "PRE", "unpriceable"); continue
        dec['snapshot_observed_at'] = observed['observed_at']
        record_decision(conn, fid, 'pre', dec)
        from prediction_market_soccer.ops.performance_report import _row_comp
        act = _place_entry(conn, broker, tickers, fx, hi, ai, track="pre", side=dec["side"],
                           stake=dec["stake_usd"], bet_kind=dec["bet_kind"], entry_min=0,
                           ledger_c=dec["ledger_entry_c"], ledger_venue=dec["ledger_venue"],
                           ledger_edge=dec["net_edge"], comp=_row_comp(fx), extra=dec)
        if act.get("terminal", True):
            _mark_eval(conn, fid, "PRE", f"{dec['bet_kind']}:{dec['side']}:${dec['stake_usd']}:{act.get('status', 'done')}")
        if act.get("action"):
            actions.append(act)
    conn.commit()
    return actions


# ── leg 2: the in-play entry ──────────────────────────────────────────────────
def _scan_inplay(conn, broker: DemoBroker, tickers: _Tickers, live_fids: list[int]) -> list[dict]:
    if not live_fids:
        return []
    ph = ",".join("?" * len(live_fids))
    rows = conn.execute(
        "SELECT fixture_api_id, milestone FROM milestone_snapshot "
        "WHERE fixture_api_id IN ({}) AND milestone IN ('T15','T30','HT','T60','T75') "
        "AND NOT EXISTS (SELECT 1 FROM kalshi_mirror_eval e WHERE e.fixture_api_id=milestone_snapshot.fixture_api_id "
        "               AND e.milestone=milestone_snapshot.milestone)".format(ph), live_fids).fetchall()
    if not rows:
        return []
    from prediction_market_soccer.ops.performance_report import _row_comp
    from prediction_market_soccer.ops.settle_bets import _inplay_entry, _MAX_ENTRY_MIN
    cmap = _cmap(conn)
    by_fid: dict[int, list[str]] = {}
    for r in rows:
        by_fid.setdefault(r["fixture_api_id"], []).append(r["milestone"])
    actions = []
    for fid, miles in by_fid.items():
        fx = _fixture(conn, fid)
        if fx is None:
            for m in miles:
                _mark_eval(conn, fid, m, "no_fixture")
            continue
        have = conn.execute("SELECT status,fill_count,attempts FROM kalshi_mirror WHERE fixture_api_id=? AND track='inplay'", (fid,)).fetchone()
        retryable = have and have['status'] in ('unfilled','error') and float(have['fill_count'] or 0)==0 and int(have['attempts'] or 1)<MAX_ATTEMPTS
        if have and not retryable:
            for m in miles:
                _mark_eval(conn, fid, m, "already_entered")
            continue
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            for m in miles:
                _mark_eval(conn, fid, m, "unmapped_team")
            continue
        if fx["home_goals"] is None or fx["away_goals"] is None:
            continue                                     # score not synced yet — retry next cycle
        try:
            entry = _inplay_entry(conn, fx, hi, ai, decision_at=_iso(_now()))
        except Exception as e:  # noqa: BLE001 — transient: NOT marked, retried next cycle
            _log("inplay_entry_error", fixture=fid, error=str(e)[:200]); continue
        verdict = "no_edge" if not entry else f"relative_value:{entry['side']}@{entry['entry_cents']}¢ {entry['milestone']}"
        if not entry:
            for m in miles:
                _mark_eval(conn, fid, m, verdict)
            continue
        if (fx["elapsed"] or 0) > _MAX_ENTRY_MIN:
            for m in miles:
                _mark_eval(conn, fid, m, verdict + ":late")
            _log("inplay_skipped_late", fixture=fid, elapsed=fx["elapsed"]); continue
        from prediction_market_soccer.util.timing_provenance import record_decision
        record_decision(conn, fid, 'inplay', entry)
        act = _place_entry(conn, broker, tickers, fx, hi, ai, track="inplay", side=entry["side"],
                           stake=entry["stake_usd"], bet_kind="relative_value", entry_min=int(entry["entry_min"]),
                           ledger_c=entry["entry_cents"], ledger_venue=entry.get("source"),
                           ledger_edge=entry.get("edge"), comp=_row_comp(fx),
                           extra=entry)
        if act.get("terminal", True):
            for m in miles:
                _mark_eval(conn, fid, m, verdict + f":{act.get('status', 'done')}")
        if act.get("action"):
            actions.append(act)
    conn.commit()
    return actions


# ── shared: place an entry ────────────────────────────────────────────────────
@writer
def _place_entry(conn, broker: DemoBroker, tickers: _Tickers, fx, hi: str, ai: str, *, track: str,
                 side: str, stake: float, bet_kind: str, entry_min: int, ledger_c, ledger_venue,
                 ledger_edge, comp: str | None, extra: dict | None = None) -> dict | None:
    fid = fx["api_id"]
    attempts = 1
    prev = conn.execute("SELECT id, status, fill_count, attempts FROM kalshi_mirror WHERE fixture_api_id=? AND track=?",
                        (fid, track)).fetchone()
    if prev is not None:
        # A row that never got a contract (venue 5xx, or an IOC that missed a moving ask) is
        # RETRIED — the paper ledger holds this bet regardless, so a transient venue fault
        # must not silently drop the mirror. Bounded by MAX_ATTEMPTS.
        #
        # 'pending' is NOT retryable here: it means the POST was sent and we never learned
        # the outcome. The order may be live at the exchange. Re-sending under a new
        # client_order_id would double the position while our books recorded one contract,
        # so the position could never be fully exited. _reconcile_pending owns those rows —
        # it looks the client_order_id up on the venue and adopts whatever really happened.
        # Both retryable states mean NOTHING IS LIVE at the venue: 'unfilled' came from a
        # clean response with fill_count 0 (an IOC that missed), and 'error' is only set by
        # _reconcile_pending after it confirmed the venue holds no order under our
        # client_order_id. Only then is replacing the row with a fresh attempt safe.
        retryable = prev["status"] in ("error", "unfilled") and float(prev["fill_count"] or 0) == 0
        if not retryable or int(prev["attempts"] or 1) >= MAX_ATTEMPTS:
            return {"terminal": True}
        attempts = int(prev["attempts"] or 1) + 1
        conn.execute("DELETE FROM kalshi_mirror WHERE id=?", (prev["id"],))
    if _today_orders(conn) >= MAX_ORDERS_PER_DAY:
        _log("budget_blocked", fixture=fid, track=track, reason=f"daily order budget {MAX_ORDERS_PER_DAY}")
        return {"terminal": True}     # the budget does not come back today
    if _open_positions(conn) >= MAX_OPEN_POSITIONS:
        _log("budget_blocked", fixture=fid, track=track, reason=f"open positions ≥ {MAX_OPEN_POSITIONS}")
        return {"terminal": False}    # positions close; re-evaluate next cycle
    tk = tickers.for_match(comp, hi, ai) if comp else None
    base = {"fixture_api_id": fid, "track": track, "comp": comp, "side": side, "bet_kind": bet_kind,
            "entry_min": entry_min, "ledger_entry_c": ledger_c, "ledger_venue": ledger_venue,
            "ledger_stake_usd": stake, "ledger_edge": ledger_edge, "attempts": attempts}
    if not tk:
        if comp and not tickers.index_ok(comp):
            # the LISTING call failed (rate limit, or a "database is locked" while another
            # process held the write lock). Writing "no market" here is a TERMINAL verdict
            # on the bet — on 2026-09-03 that lost a real La Liga pre-match entry to a
            # transient lock. Record nothing so the next cycle asks again.
            _log("entry_deferred_listing_unreachable", fixture=fid, track=track, comp=comp,
                 side=side, attempt=attempts)
            return {"terminal": False}
        _insert(conn, {**base, "ticker": "", "count": 0, "status": "skipped",
                       "note": "no Kalshi market for this pairing", "raw_json": json.dumps(extra or {})})
        _log("entry_skipped_no_market", fixture=fid, track=track, side=side)
        return {"terminal": True}
    ticker = tk[side]
    try:
        ob = broker.book(ticker)
    except Exception as e:  # noqa: BLE001
        _insert(conn, {**base, "ticker": ticker, "count": 0, "status": "error",
                       "note": f"book unavailable: {str(e)[:120]}"})
        _log("entry_book_error", fixture=fid, track=track, ticker=ticker, error=str(e)[:160])
        return {"terminal": False}
    ask = float(ob.yes_ask) if ob.yes_ask is not None else None
    if ask is None or not (0.0 < ask < 1.0):
        # the demo book can show no YES offer on a heavy favourite for a while (measured on
        # Flamengo 83¢: bid 78, no ask) — a liquidity gap, not a decision: keep retrying on
        # the next cycles (bounded by MAX_ATTEMPTS) instead of writing the bet off
        _insert(conn, {**base, "ticker": ticker, "count": 0, "status": "unfilled",
                       "note": f"no executable ask (yes_ask={ask}) — retry"})
        _log("entry_no_ask", fixture=fid, track=track, ticker=ticker, attempt=attempts)
        return {"terminal": attempts >= MAX_ATTEMPTS}
    n = contracts(ask, stake)
    # An edge at another venue/earlier quote is not permission to pay any demo price.
    fair = (extra or {}).get('fair')
    if fair is None:
        fair = ((extra or {}).get('model') or {}).get(side)
    threshold = CONFIG.risk.min_net_edge if track == 'inplay' else CONFIG.decision.min_net_edge
    if track == 'pre' and ask * 100 < CONFIG.decision.longshot_cents:
        threshold += CONFIG.decision.longshot_extra_theta
    if track == 'pre' and side == 'draw':
        threshold += getattr(CONFIG.decision, 'draw_extra_theta', 0.0)
    try:
        import math
        from dataclasses import asdict
        from prediction_market_soccer.strategy.edge import compute_edge
        if n<1 or fair is None or not math.isfinite(float(fair)) or not 0 <= float(fair) <= 1:
            raise ValueError('Missing finite model probability')
        fee = broker.estimate_taker_fee(ticker,n,ask)
        fee_p = float(fee['fee_per_contract'])
        if not math.isfinite(fee_p) or fee_p<0: raise ValueError('Invalid fee estimate')
        edge = compute_edge(float(fair),ask,sigma_p=float(((extra or {}).get('sigma') or {}).get(side,0)),
                            k=CONFIG.risk.shrink_k,fee=fee_p,theta=threshold)
        extra = {**(extra or {}),'execution_quote':{'ask':ask,'observed_at':_iso(_now()),'fee':fee,
                  'edge':asdict(edge),'strategy_kind':bet_kind}}
        if bet_kind in ('value','relative_value') and not edge.tradable:
            _log('entry_price_rejected',fixture=fid,track=track,ask=ask,fair=fair,net_edge=edge.net_edge,threshold=threshold)
            return {'terminal':False,'status':'execution_edge_unavailable'}
    except Exception as exc:
        _log('entry_edge_unavailable',fixture=fid,track=track,error=str(exc)[:120])
        return {'terminal':False,'status':'execution_edge_unavailable'}
    if track == 'pre':
        from prediction_market_soccer.util.timing_provenance import _dt
        if _now() >= _dt(fx['kickoff_ts']):
            return {'terminal': True, 'status': 'late_pre'}
    coid = f"mirror-{track}-{fid}-{uuid.uuid4().hex[:8]}"
    # write the intent BEFORE the HTTP call: the row is the idempotency key, so a crash
    # between send and record can never place this (fixture, track) twice
    row_id = _insert(conn, {**base, "ticker": ticker, "ask_c": round(ask * 100, 1), "count": n,
                            "client_order_id": coid, "status": "pending", "submitted_at": _iso(_now()),
                            "raw_json": json.dumps(extra or {}, default=str)})
    conn.commit()
    try:
        res = broker.buy_yes(ticker, n, ask, coid)
    except Exception as e:  # noqa: BLE001
        # UNKNOWN, not "no order". An IOC either fills at the matching engine or dies there;
        # a lost response (read timeout, reset, proxy 5xx) tells us nothing about which. The
        # row STAYS 'pending' so _reconcile_pending resolves it against the venue by
        # client_order_id on a later cycle; marking it 'error' would make it look retryable
        # and a second order could be placed on top of a live one.
        conn.execute("UPDATE kalshi_mirror SET note=? WHERE id=?",
                     (f"send outcome unknown: {str(e)[:130]}", row_id))
        conn.commit()
        _log("entry_send_unknown", fixture=fid, track=track, ticker=ticker, coid=coid, error=str(e)[:160])
        return {"terminal": False}
    filled = res["fill_count"]
    status = "open" if filled > 0 else ("unfilled" if res["ok"] else "error")
    if (not res['ok'] and int(res.get('status_code') or 500)>=500) or (filled and res.get('avg_fill') is None):
        status = 'pending'
    conn.execute(
        "UPDATE kalshi_mirror SET status=?, order_id=?, fill_count=?, avg_fill_c=?, filled_at=?, note=?, "
        "raw_json=json_patch(coalesce(raw_json,'{}'), ?) WHERE id=?",
        (status, res["order_id"], filled, (round(res["avg_fill"] * 100, 1) if res["avg_fill"] is not None else None),
         (_iso(_now()) if filled else None), (None if res["ok"] else f"http {res['status_code']}: {str(res['raw'])[:150]}"),
         json.dumps({"entry_response": res.get("raw"), "entry_sent": res.get("sent")}, default=str), row_id))
    conn.commit()
    if filled and res.get('order_id') and hasattr(broker,'fills_for_order'):
        try:
            from prediction_market_soccer.util.timing_provenance import record_fills
            record_fills(conn,broker.fills_for_order(ticker,res['order_id']));conn.commit()
        except Exception as e:
            _log('fill_receipt_unavailable',fixture=fid,order_id=res['order_id'],error=str(e)[:120])
    act = {"action": "entry", "track": track, "fixture": fid, "side": side, "ticker": ticker, "bet_kind": bet_kind,
           "ledger_c": ledger_c, "ask_c": round(ask * 100, 1), "count": n, "filled": filled,
           "avg_fill_c": (round(res["avg_fill"] * 100, 1) if res["avg_fill"] else None), "status": status,
           "attempt": attempts, "http": res["status_code"]}
    _log("entry", **act)
    # terminal once a contract is held, or once the retry budget is spent; a venue 5xx or an
    # IOC that missed the ask is re-tried on the next cycle (the row above is the marker)
    act["terminal"] = bool(filled > 0) or attempts >= MAX_ATTEMPTS
    return act


def _insert(conn, row: dict) -> int:
    cols = list(row.keys())
    cur = conn.execute(f"INSERT INTO kalshi_mirror ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [row[c] for c in cols])
    return int(cur.lastrowid)


# ── leg 3: exits (smart-exit sell at a MILESTONE, else settle at the whistle) ──
_MILESTONE_MIN = {"T15": 15, "T30": 30, "HT": 45, "T60": 60, "T75": 75}


@writer
def _scan_exits(conn, broker: DemoBroker, strength: _Strength, live_by_fid: dict[int, dict]) -> list[dict]:
    """Evaluate immutable observed milestones, then recheck the actual demo bid.

    Stored score/reds/elapsed must still match the currently observed live state.
    A retry loses its old signal when that state or freshness changes. The intent is
    durable before HTTP; unknown outcomes are reconciled before another sell.
    """
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN, overshoot_trigger
    from prediction_market_soccer.model.match_pricing import is_knockout
    from prediction_market_soccer.util.pricing import reg_score
    rows = conn.execute("SELECT * FROM kalshi_mirror WHERE status='open'").fetchall()
    if not rows:
        return []
    cmap = _cmap(conn)
    actions = []
    venue_pos: dict[str, float] | None = None
    blocked_tickers = {r['ticker'] for r in rows if json.loads(r['raw_json'] or '{}').get('exit_outcome_unknown')}
    for r in rows:
        fid, side, ticker, track = r["fixture_api_id"], r["side"], r["ticker"], r["track"]
        held = float(r["fill_count"]) - float(r["exit_fill_count"] or 0.0)
        if held <= 1e-8:
            conn.execute("UPDATE kalshi_mirror SET status='exited' WHERE id=?", (r["id"],)); continue
        fx = _fixture(conn, fid)
        if fx is None:
            continue
        if ticker in blocked_tickers:
            _log('exit_reconciliation_required', fixture=fid, ticker=ticker)
            continue
        # A final fixture is insufficient: record only the venue's terminal payout.
        if fx["status_short"] in _FINISHED and fx["home_goals"] is not None:
            from prediction_market_soccer.util.demo_settlement import terminal_binary_settlement
            try:
                market = broker.market(ticker)
                terminal = terminal_binary_settlement(market, ticker=ticker)
            except Exception:
                terminal = None
            if terminal is None:
                continue
            gh, ga = reg_score(fx["raw_json"], fx["home_goals"], fx["away_goals"])
            won = int(terminal == 'yes')
            entry_c = float(r["avg_fill_c"] or r["ask_c"] or 0.0)
            sold = float(r['exit_fill_count'] or 0)
            proceeds_c = sold * float(r['exit_avg_c'] or 0) + held * (100.0 if won else 0)
            pnl = proceeds_c / float(r['fill_count']) - entry_c
            raw = json.loads(r['raw_json'] or '{}'); raw['demo_settlement'] = market
            conn.execute("UPDATE kalshi_mirror SET status='settled', exit_reason='settled', won=?, pnl_c=?, "
                         "exited_at=?,raw_json=? WHERE id=?", (won, round(pnl, 1), _iso(_now()), json.dumps(raw), r["id"]))
            act = {"action": "settled", "track": track, "fixture": fid, "side": side, "won": won,
                   "score": f"{gh}-{ga}", "pnl_c_per_contract": round(pnl, 1), "held": held}
            _log("settled", **act); actions.append(act); continue
        if fx["status_short"] not in _LIVE:
            continue                       # not started / feed gap — nothing to decide
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai and r["comp"]):
            continue
        # A sell already decided at an earlier milestone but not (fully) filled by the venue
        # is retried first — the ledger's decision is made, only the execution is pending.
        pending_min = r["exit_min"] if (r["exit_reason"] is None and r["exit_min"] is not None) else None
        entry_floor = max(1, int(r["entry_min"] or 0))
        from prediction_market_soccer.util.timing_provenance import live_snapshots, record_decision, current_state_matches
        decision_at = _iso(_now())
        ms_rows = [m for m in live_snapshots(conn, fid, decision_at) if m['milestone'] in _MILESTONE_MIN
                   and current_state_matches(conn,fx,m,decision_at)]
        if pending_min is not None and not any(m['elapsed']==pending_min for m in ms_rows):
            conn.execute('UPDATE kalshi_mirror SET exit_min=NULL,exit_bid_c=NULL,exit_fair_c=NULL WHERE id=?',(r['id'],))
            pending_min = None
        decided_min, decided_price, decided_fair, decided_trig = None, None, None, None
        if pending_min is not None:
            decided_min, decided_price, decided_fair = pending_min, r["exit_bid_c"], r["exit_fair_c"]
        else:
            lam = None
            for m in ms_rows:
                mn = m.get('elapsed')
                if mn is None:
                    continue
                if mn < entry_floor or mn > _REG_MAX_MIN:
                    continue
                ekey = f"exit:{track}:{m['milestone']}"
                if conn.execute("SELECT 1 FROM kalshi_mirror_eval WHERE fixture_api_id=? AND milestone=?",
                                (fid, ekey)).fetchone():
                    continue
                # the row's own price for the pick — the ledger's _milestone_ticks order
                price = next((v for v in (m[f"kalshi_{side}_bid"], m[f"poly_{side}_bid"]) if v is not None), None)
                if price is None:
                    _mark_eval(conn, fid, ekey, "no_price"); continue
                try:
                    if lam is None:
                        sm = strength.get(fx["kickoff_ts"], r["comp"])
                        lam = sm.pair_lambdas(hi, ai, knockout=is_knockout(fx["round"]))
                    sh, sa, rh, ra = (m.get(k) for k in ('home_goals','away_goals','reds_home','reds_away'))
                    if any(v is None for v in (sh,sa,rh,ra)):
                        _mark_eval(conn, fid, ekey, 'unverified_observed_state'); continue
                    lp = live_match_prob(lam[0], lam[1], mn, sh, sa, red_home=rh, red_away=ra)
                except Exception as e:  # noqa: BLE001 — transient: not marked, retried next cycle
                    _log("exit_fair_error", fixture=fid, milestone=m["milestone"], error=str(e)[:160]); break
                fair = {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[side]
                trig = min(OVERSHOOT_MARGIN, overshoot_trigger(fair))
                fired = float(price) * 100.0 >= fair * 100.0 + trig * 100.0
                _mark_eval(conn, fid, ekey, f"{'SELL' if fired else 'hold'} price={round(float(price)*100,1)} "
                                            f"fair={round(fair*100,1)} trig={round(trig*100,1)} score={sh}-{sa}")
                if fired:
                    decided_min, decided_price, decided_fair, decided_trig = mn, round(float(price) * 100, 1), round(fair * 100, 1), round(trig * 100, 1)
                    conn.execute("UPDATE kalshi_mirror SET exit_min=?, exit_bid_c=?, exit_fair_c=? WHERE id=?",
                                 (mn, decided_price, decided_fair, r["id"]))
                    record_decision(conn, fid, f'exit:{track}', {'side':side,'sold_min':mn,'sold_c':decided_price,
                                    'fair_c':decided_fair,'snapshot_observed_at':m['observed_at'],'evidence_level':'forward_observed_paper'})
                    break
            conn.commit()
        if decided_min is None:
            continue                       # no milestone over-reaction (yet) — hold
        # execute on the demo venue at ITS current bid; never sell more than the venue holds
        try:
            ob = broker.book(ticker)
        except Exception as e:  # noqa: BLE001
            blocked_tickers.add(ticker)
            _log("exit_book_error", fixture=fid, ticker=ticker, error=str(e)[:160]); continue
        bid = float(ob.yes_bid) if ob.yes_bid is not None else None
        if bid is None or not (0.0 < bid < 1.0):
            _log("exit_no_bid", fixture=fid, ticker=ticker); continue
        if decided_fair is not None and bid < (float(decided_fair)/100 + overshoot_trigger(float(decided_fair)/100)):
            _log('exit_price_rejected', fixture=fid, ticker=ticker, bid=bid, fair_c=decided_fair); continue
        if venue_pos is None:
            try:
                venue_pos = broker.positions()
            except Exception:  # noqa: BLE001
                continue
        vp = venue_pos.get(ticker)
        n = max(0, min(int(held), int(vp or 0)))
        if n < 1:
            _log("exit_nothing_held", fixture=fid, ticker=ticker, ours=held, venue=vp); continue
        coid = f"mirror-exit-{track}-{fid}-{uuid.uuid4().hex[:8]}"
        state = json.loads(r['raw_json'] or '{}')
        if state.get('exit_outcome_unknown'):
            _log('exit_reconciliation_required', fixture=fid, ticker=ticker); continue
        conn.execute("UPDATE kalshi_mirror SET exit_client_order_id=?,raw_json=json_patch(coalesce(raw_json,'{}'),?) WHERE id=?",
                     (coid,json.dumps({'exit_outcome_unknown':coid,'exit_intent_at':_iso(_now())}),r['id']))
        conn.commit()
        try:
            res = broker.sell_yes(ticker, n, bid, coid)
        except Exception as e:  # noqa: BLE001
            blocked_tickers.add(ticker)
            conn.execute("UPDATE kalshi_mirror SET raw_json=json_patch(coalesce(raw_json,'{}'),?) WHERE id=?",
                         (json.dumps({'exit_outcome_unknown':coid}),r['id']))
            conn.commit()
            _log("exit_send_error", fixture=fid, ticker=ticker, error=str(e)[:160]); continue
        if not res["ok"]:
            if int(res.get('status_code') or 500)>=500:
                blocked_tickers.add(ticker)
                conn.execute("UPDATE kalshi_mirror SET raw_json=json_patch(coalesce(raw_json,'{}'),?) WHERE id=?",
                             (json.dumps({'exit_outcome_unknown':coid}),r['id']))
            else:
                conn.execute("UPDATE kalshi_mirror SET raw_json=json_patch(coalesce(raw_json,'{}'),?) WHERE id=?",
                             (json.dumps({'exit_outcome_unknown':None}),r['id']))
            conn.execute("UPDATE kalshi_mirror SET note=? WHERE id=?",
                         (f"exit http {res['status_code']}: {str(res['raw'])[:120]}", r["id"]))
            conn.commit()
            _log("exit_venue_error", fixture=fid, ticker=ticker, http=res["status_code"], raw=res["raw"])
            continue                       # decision stands → retried next cycle
        sold = res["fill_count"]
        if sold and res.get('avg_fill') is None:
            blocked_tickers.add(ticker)
            _log('exit_fill_price_unknown',fixture=fid,ticker=ticker)
            continue
        new_exit_fill = float(r["exit_fill_count"] or 0.0) + sold
        remaining = float(r["fill_count"]) - new_exit_fill
        entry_c = float(r["avg_fill_c"] or r["ask_c"] or 0.0)
        sold_c = (res["avg_fill"] * 100.0) if res["avg_fill"] is not None else 0.0
        previous_sold = float(r['exit_fill_count'] or 0)
        weighted_sold_c = ((float(r['exit_avg_c'] or 0)*previous_sold + sold_c*sold)/new_exit_fill) if new_exit_fill else None
        exit_fills = list(state.get('exit_fills') or [])
        if not exit_fills and previous_sold and state.get('exit_response'):
            exit_fills.append(state['exit_response'])
        if sold:
            exit_fills.append(res.get('raw'))
        done = sold > 0 and remaining <= 1e-8
        exit_order_ids = list(state.get('exit_order_ids') or ([r['exit_order_id']] if previous_sold and r['exit_order_id'] else []))
        if sold and res.get('order_id'): exit_order_ids.append(res['order_id'])
        conn.execute(
            "UPDATE kalshi_mirror SET exit_order_id=?, exit_fill_count=?, exit_avg_c=?, exited_at=?, "
            "status=?, exit_reason=?, pnl_c=?, raw_json=json_patch(coalesce(raw_json,'{}'), ?) WHERE id=?",
            (res["order_id"], new_exit_fill, weighted_sold_c, (_iso(_now()) if done else None),
             ("exited" if done else "open"), ("smart_exit" if done else None),
             (round(weighted_sold_c - entry_c, 1) if done else None),
             json.dumps({"exit_response": res.get("raw"), "exit_sent": res.get("sent"), 'exit_fills':exit_fills,
                         'exit_order_ids':exit_order_ids,'exit_outcome_unknown':None}, default=str), r["id"]))
        conn.commit()
        venue_pos[ticker] = max(0,float(venue_pos.get(ticker) or 0)-sold)
        if sold and res.get('order_id') and hasattr(broker,'fills_for_order'):
            try:
                from prediction_market_soccer.util.timing_provenance import record_fills
                record_fills(conn,broker.fills_for_order(ticker,res['order_id']));conn.commit()
            except Exception as e:
                _log('fill_receipt_unavailable',fixture=fid,order_id=res['order_id'],error=str(e)[:120])
        act = {"action": "smart_exit", "track": track, "fixture": fid, "side": side, "ticker": ticker,
               "milestone_min": decided_min, "ledger_price_c": decided_price, "fair_c": decided_fair,
               "trigger_c": decided_trig, "demo_bid_c": round(bid * 100, 1), "requested": n, "sold": sold,
               "sold_c": (round(sold_c, 1) if sold else None),
               "pnl_c_per_contract": (round(sold_c - entry_c, 1) if done else None),
               "status": "exited" if done else ("partial" if sold else "unfilled_retry")}
        _log("smart_exit", **act); actions.append(act)
    conn.commit()
    return actions


# ── reconcile rows whose send outcome is unknown ─────────────────────────────
@writer
def _reconcile_pending(conn, broker: DemoBroker) -> int:
    """Resolve persisted intents by client ID and actual fills; limit prices are never fills."""
    from prediction_market_soccer.util.timing_provenance import record_fills, fills_cash, _dt
    n = 0
    rows = conn.execute("SELECT * FROM kalshi_mirror WHERE json_extract(raw_json,'$.paper_entry_id') IS NULL AND (status='pending' OR json_extract(raw_json,'$.exit_outcome_unknown') IS NOT NULL)").fetchall()
    for r in rows:
        raw = json.loads(r['raw_json'] or '{}')
        is_exit = bool(raw.get('exit_outcome_unknown'))
        coid = raw['exit_outcome_unknown'] if is_exit else r['client_order_id']
        try:
            orders = broker.orders_for(r["ticker"])
        except Exception:  # noqa: BLE001
            continue
        matches = [x for x in orders if x.get('client_order_id')==coid]
        if len(matches)>1: continue
        o = matches[0] if matches else None
        if o is None:
            # the venue has no record of this client_order_id, so the POST never landed and
            # nothing is live. Release the row to the retry path (bounded by MAX_ATTEMPTS).
            age = 0.0
            try:
                age = (_now() - _dt(raw.get('exit_intent_at') if is_exit else r['submitted_at'])).total_seconds()
            except (TypeError, ValueError):
                pass
            if age > 120:      # give the exchange time to make a just-sent order listable
                if is_exit:
                    conn.execute("UPDATE kalshi_mirror SET raw_json=json_patch(coalesce(raw_json,'{}'),?),note=? WHERE id=?",
                                 (json.dumps({'exit_outcome_unknown':None}),'exit intent absent in complete venue order listing',r['id']))
                elif float(r['fill_count'] or 0)==0:
                    conn.execute("UPDATE kalshi_mirror SET status='error', note=? WHERE id=?",
                                 ("send outcome unknown; venue has no such client_order_id — safe to retry", r["id"]))
                n += 1
            continue
        # IOC must have reached a terminal state before an empty response can permit retry.
        if o.get('status') not in ('executed','canceled','cancelled'):
            continue
        try:
            import math
            filled = float(o.get('fill_count_fp') if o.get('fill_count_fp') is not None else o.get('fill_count',0))
            if not math.isfinite(filled) or filled<0: continue
            receipts = broker.fills_for_order(r['ticker'],o['order_id']) if filled else []
            expected_action = 'sell' if is_exit else 'buy'
            if any(f.get('order_id')!=o['order_id'] or f.get('ticker',f.get('market_ticker'))!=r['ticker'] or f.get('action')!=expected_action for f in receipts):
                continue
            cash = fills_cash(receipts)
            if cash is None or abs(cash['count']-filled)>1e-8: continue
            if filled: record_fills(conn,receipts)
        except Exception as exc:
            _log('pending_fill_receipt_unavailable',order_id=o.get('order_id'),error=str(exc)[:120]);continue
        avg = cash['cash_usd']/filled if filled else None
        recovered = {'order_id':o['order_id'],'fill_count':filled,'average_fill_price':avg,
                     'average_fee_paid':cash['fee_usd']/filled if filled else None,'source':'verified_fills_recovery'}
        if is_exit:
            prior = float(r['exit_fill_count'] or 0)
            total = prior+filled
            if total>float(r['fill_count'])+1e-8: continue
            price = (prior*float(r['exit_avg_c'] or 0)+cash['cash_usd']*100)/total if total else None
            done = total>=float(r['fill_count'])-1e-8
            parts = list(raw.get('exit_fills') or ([raw['exit_response']] if prior and raw.get('exit_response') else []))
            ids = list(raw.get('exit_order_ids') or ([r['exit_order_id']] if prior and r['exit_order_id'] else []))
            if filled: parts.append(recovered);ids.append(o['order_id'])
            conn.execute("UPDATE kalshi_mirror SET status=?,exit_fill_count=?,exit_avg_c=?,exit_order_id=?,exited_at=?,exit_reason=?,pnl_c=?,raw_json=json_patch(coalesce(raw_json,'{}'),?) WHERE id=?",
                         ('exited' if done else 'open',total,price,o['order_id'],_iso(_now()) if done else None,
                          'smart_exit' if done else None,price-float(r['avg_fill_c']) if done and r['avg_fill_c'] is not None else None,
                          json.dumps({'exit_outcome_unknown':None,'exit_fills':parts,'exit_order_ids':ids,'exit_response':recovered}),r['id']))
        else:
            conn.execute("UPDATE kalshi_mirror SET status=?,order_id=?,fill_count=?,avg_fill_c=?,filled_at=?,raw_json=json_patch(coalesce(raw_json,'{}'),?) WHERE id=?",
                         ('open' if filled else 'unfilled',o['order_id'],filled,avg*100 if avg is not None else None,
                          min((f['created_time'] for f in receipts),default=None),json.dumps({'entry_response':recovered}),r['id']))
        n += 1
    if n:
        conn.commit()
    return n


# ── the cycle ─────────────────────────────────────────────────────────────────
@writer
def run_cycle(conn, inplay_doc: dict | None = None) -> dict:
    """One mirror pass: PRE entries → in-play entries → exits/settlements. Never raises
    into the caller; every leg is isolated. Returns a summary dict."""
    if not enabled():
        return {"enabled": False}
    t0 = time.time()
    try:
        broker = DemoBroker()
    except Exception as e:  # noqa: BLE001
        _log("broker_refused", error=str(e)[:200])
        return {"enabled": True, "error": str(e)[:200]}
    out = {"enabled": True, "actions": [], "errors": []}
    from prediction_market_soccer.exec import demo_forward
    # Paper lifecycle has already recorded immutable entry/exit decisions. Demo
    # consumes them and never writes a paper decision or reselects side/timing.
    for name, fn in (("forward", lambda: demo_forward.run(conn, broker)),):
        try:
            res = fn()
            out["actions"].extend(res.get('actions', []))
            out["errors"].extend(res.get('errors', []))
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{name}: {str(e)[:160]}")
            _log("leg_error", leg=name, error=str(e)[:200])
    out["open"] = _open_positions(conn)
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["summary"] = (f"{len(out['actions'])} action(s), {out['open']} open, {out['elapsed_s']}s"
                      + (f", errors={out['errors']}" if out["errors"] else ""))
    try:
        _export(conn, broker, out)
    except Exception as e:  # noqa: BLE001
        _log("export_error", error=str(e)[:160])
    return out


def _export(conn, broker: DemoBroker | None, last: dict) -> None:
    from prediction_market_soccer.util.timing_provenance import demo_execution_summary
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM kalshi_mirror ORDER BY submitted_at DESC LIMIT 200").fetchall()]
    for r in rows:
        r.pop("raw_json", None)
    bal = None
    if broker is not None:
        try:
            b = broker.balance()
            bal = {"cash_usd": round(int(b.get("balance", 0)) / 100.0, 2),
                   "portfolio_value_usd": round(int(b.get("portfolio_value", 0)) / 100.0, 2)}
        except Exception:  # noqa: BLE001
            bal = None
    closed = [r for r in rows if r["status"] in ("exited", "settled") and r["pnl_c"] is not None]
    doc = {"ts": _iso(_now()), "enabled": enabled(), "env": CONFIG.venue.kalshi_env, "balance": bal,
           "counts": {k: sum(1 for r in rows if r["status"] == k)
                      for k in ("open", "exited", "settled", "unfilled", "skipped", "error", "pending")},
           "realized_c": round(sum(float(r["pnl_c"]) * (float(r["fill_count"]) if r["status"] == "settled"
                                                          else float(r["exit_fill_count"] or 0)) for r in closed), 1),
           "realized_c_basis":"legacy_gross_before_fees", "execution_summary":demo_execution_summary(conn),
           "last_cycle": {k: last.get(k) for k in ("actions", "errors", "elapsed_s")},
           "rows": rows}
    CONFIG.paths.ensure()
    from prediction_market_soccer.ops.run_status import atomic_json
    atomic_json(CONFIG.paths.output / "kalshi_mirror.json", doc)


# ── CLI ───────────────────────────────────────────────────────────────────────
@writer
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Kalshi DEMO mirror of the 择时(实现) strategy")
    ap.add_argument("--status", action="store_true", help="print switches, balance, open rows")
    ap.add_argument("--once", action="store_true", help="run one mirror cycle now")
    ap.add_argument("--build-pit-cache", action="store_true", help="rebuild data/output/pit_records.json")
    a = ap.parse_args()
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    if a.build_pit_cache:
        print("[kalshi_mirror] pit cache:", build_pit_cache(conn))
    if a.once:
        print("[kalshi_mirror] cycle:", json.dumps(run_cycle(conn), ensure_ascii=False, default=str)[:1500])
    if a.status or not (a.once or a.build_pit_cache):
        print(f"  {ENV_FLAG}={enabled()}  soccer KALSHI_ENV={CONFIG.venue.kalshi_env}")
        try:
            b = DemoBroker()
            bal = b.balance()
            print(f"  demo balance: cash=${int(bal.get('balance', 0)) / 100:.2f}  portfolio=${int(bal.get('portfolio_value', 0)) / 100:.2f}")
        except Exception as e:  # noqa: BLE001
            print(f"  broker: REFUSED — {e}")
        recs = load_pit_records()
        print(f"  pit_records cache: {'%d records' % len(recs) if recs is not None else 'MISSING/STALE'}")
        for r in conn.execute("SELECT fixture_api_id, track, side, ticker, status, count, fill_count, avg_fill_c, "
                              "exit_avg_c, pnl_c, submitted_at FROM kalshi_mirror ORDER BY submitted_at DESC LIMIT 15"):
            print("  ", dict(r))


if __name__ == "__main__":
    main()
