"""exec/kalshi_mirror.py — mirror the 择时(实现) strategy onto the Kalshi DEMO account.

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
PRE_LATE_GRACE_MIN = 5          # still mirror a PRE bet discovered ≤5' after kickoff
_REG_MAX_MIN = 95               # regulation incl. stoppage (same clamp as smart_exit)
_PIT_CACHE = CONFIG.paths.output / "pit_records.json"
_PIT_CACHE_MAX_AGE_H = 36.0
_LOG = CONFIG.paths.logs / "kalshi_mirror.jsonl"
_EXPORT = CONFIG.paths.output / "kalshi_mirror.json"
_SIDES = ("home", "draw", "away")
_FINISHED = ("FT", "AET", "PEN", "AWD", "WO")
_LIVE = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


# ── switches ──────────────────────────────────────────────────────────────────
def enabled() -> bool:
    return os.getenv(ENV_FLAG, "false").strip().lower() in _TRUTHY


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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
    def book(self, ticker: str):
        from prediction_market_soccer.venues.kalshi.market_data import best_prices
        self.limiter.acquire_read()
        r = self.c._authed("GET", f"/markets/{ticker}/orderbook")
        r.raise_for_status()
        return best_prices(r.json(), market_key=ticker)

    def balance(self) -> dict:
        self.limiter.acquire_read()
        r = self.c._authed("GET", "/portfolio/balance")
        r.raise_for_status()
        return r.json()

    def positions(self) -> dict[str, float]:
        """{ticker: signed contracts} from the venue (positive = YES long)."""
        self.limiter.acquire_read()
        r = self.c._authed("GET", "/portfolio/positions?limit=200")
        r.raise_for_status()
        out: dict[str, float] = {}
        for p in r.json().get("market_positions") or []:
            try:
                out[p["ticker"]] = float(p.get("position_fp") or 0.0)
            except (TypeError, ValueError):
                continue
        return out

    def orders_for(self, ticker: str) -> list[dict]:
        self.limiter.acquire_read()
        r = self.c._authed("GET", f"/portfolio/orders?ticker={ticker}&limit=50")
        return (r.json().get("orders") or []) if r.status_code == 200 else []

    # writes — IOC only: fills now or dies at the engine; nothing rests
    def _send(self, *, ticker: str, contract_side: str, count: int, price: float,
              client_order_id: str) -> dict:
        if "demo.kalshi" not in self.c.base:
            raise RuntimeError("mirror refused at send: not the demo host")
        if not (0.0 < price < 1.0):
            raise ValueError(f"price {price} outside (0,1)")
        if count < 1:
            raise ValueError("count must be ≥ 1")
        self.limiter.acquire_write()
        rec = self.c.create_order(ticker=ticker, side=contract_side, count=int(count),
                                  price_dollars=round(price, 4), client_order_id=client_order_id,
                                  tif="immediate_or_cancel")
        body = {}
        try:
            body = json.loads(rec.get("response") or "{}")
        except json.JSONDecodeError:
            body = {}
        ok = rec.get("status_code") in (200, 201)
        return {"ok": ok, "status_code": rec.get("status_code"),
                "order_id": body.get("order_id"), "fill_count": float(body.get("fill_count") or 0.0),
                "remaining_count": float(body.get("remaining_count") or 0.0),
                "avg_fill": (float(body["average_fill_price"]) if body.get("average_fill_price") else None),
                "raw": rec.get("response"), "sent": rec.get("body_sent")}

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
    _PIT_CACHE.write_text(json.dumps(doc), encoding="utf-8")
    return {"n": len(recs), "path": str(_PIT_CACHE)}


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
        doc = json.loads(_PIT_CACHE.read_text(encoding="utf-8"))
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
def pre_decision(conn, fx, hi: str, ai: str, pre_row, records: list, strength: _Strength) -> dict | None:
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
    if not comp or not fx["kickoff_ts"]:
        return None
    knockout = is_knockout(fx["round"], comp)
    sm = strength.get(fx["kickoff_ts"], comp)
    if not (hi in sm.ratings and ai in sm.ratings):
        return None
    cal = _pit_cal(records, fx["kickoff_ts"])
    conf = _conf(cal)
    lam_mult = None
    mh, ma, motiv = motivation_multipliers(conn, _fifa_ranks(), hi, ai, fx["round"], CONFIG.model)
    if (mh, ma) != (1.0, 1.0):
        lam_mult = (mh, ma)
    mp = price_match_calibrated(sm, hi, ai, knockout=False, cal=cal, lam_mult=lam_mult,
                                host_neutral=knockout)
    model = {"home": mp.p_home, "draw": mp.p_draw, "away": mp.p_away}
    # bookmaker consensus (pre-match book only) → the argmax bet's reference price
    bd = conn.execute(
        "SELECT AVG(p_home) bh, AVG(p_draw) bdr, AVG(p_away) ba FROM match_odds "
        "WHERE fixture_api_id=? AND bookmaker <> 'live_consensus'", (fx["api_id"],)).fetchone()
    if bd and bd["bh"] is not None:
        s = (bd["bh"] or 0) + (bd["bdr"] or 0) + (bd["ba"] or 0)
        price = {"home": bd["bh"] / s, "draw": bd["bdr"] / s, "away": bd["ba"] / s} if s else model
    else:
        price = model
    model_pick = max(_SIDES, key=lambda k: model[k])
    try:
        fi = form_index(conn, as_of=fx["kickoff_ts"])
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
        ledger_c, ledger_venue = to_cents(price[side]), "book_devig"
    return {"side": side, "stake_usd": round(float(stake), 2), "bet_kind": kind,
            "ledger_entry_c": ledger_c, "ledger_venue": ledger_venue,
            "net_edge": (round(float(edge), 4) if edge is not None else None),
            "model": {k: round(v, 4) for k, v in model.items()},
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
        if fx["status_short"] in _FINISHED or (fx["status_short"] in _LIVE and (fx["elapsed"] or 0) > PRE_LATE_GRACE_MIN):
            _mark_eval(conn, fid, "PRE", f"late:{fx['status_short']}/{fx['elapsed']}")
            _log("pre_skipped_late", fixture=fid, status=fx["status_short"], elapsed=fx["elapsed"]); continue
        hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
        if not (hi and ai):
            _mark_eval(conn, fid, "PRE", "unmapped_team"); continue
        try:
            dec = pre_decision(conn, fx, hi, ai, pr, records, strength)
        except Exception as e:  # noqa: BLE001 — transient: NOT marked, retried next cycle
            _log("pre_decision_error", fixture=fid, error=str(e)[:200]); continue
        if dec is None:
            _mark_eval(conn, fid, "PRE", "unpriceable"); continue
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
        have = conn.execute("SELECT 1 FROM kalshi_mirror WHERE fixture_api_id=? AND track='inplay'", (fid,)).fetchone()
        if have:
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
            entry = _inplay_entry(conn, fx, hi, ai)     # the ledger's own causal rule, live
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
        act = _place_entry(conn, broker, tickers, fx, hi, ai, track="inplay", side=entry["side"],
                           stake=entry["stake_usd"], bet_kind="relative_value", entry_min=int(entry["entry_min"]),
                           ledger_c=entry["entry_cents"], ledger_venue=entry.get("source"),
                           ledger_edge=entry.get("edge"), comp=_row_comp(fx),
                           extra={k: entry[k] for k in ("milestone", "edge", "stake_usd") if k in entry})
        if act.get("terminal", True):
            for m in miles:
                _mark_eval(conn, fid, m, verdict + f":{act.get('status', 'done')}")
        if act.get("action"):
            actions.append(act)
    conn.commit()
    return actions


# ── shared: place an entry ────────────────────────────────────────────────────
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
        retryable = prev["status"] in ("error", "unfilled") and float(prev["fill_count"] or 0) < 1
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
        conn.execute("UPDATE kalshi_mirror SET status='error', note=? WHERE id=?", (f"send failed: {str(e)[:150]}", row_id))
        conn.commit()
        _log("entry_send_error", fixture=fid, track=track, ticker=ticker, error=str(e)[:160])
        return {"terminal": False}
    filled = res["fill_count"]
    status = "open" if filled > 0 else ("unfilled" if res["ok"] else "error")
    conn.execute(
        "UPDATE kalshi_mirror SET status=?, order_id=?, fill_count=?, avg_fill_c=?, filled_at=?, note=?, "
        "raw_json=json_patch(coalesce(raw_json,'{}'), ?) WHERE id=?",
        (status, res["order_id"], filled, (round(res["avg_fill"] * 100, 1) if res["avg_fill"] else (round(ask * 100, 1) if filled else None)),
         (_iso(_now()) if filled else None), (None if res["ok"] else f"http {res['status_code']}: {str(res['raw'])[:150]}"),
         json.dumps({"entry_response": res.get("raw"), "entry_sent": res.get("sent")}, default=str), row_id))
    conn.commit()
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


def _scan_exits(conn, broker: DemoBroker, strength: _Strength, live_by_fid: dict[int, dict]) -> list[dict]:
    """The ledger's exit, evaluated where the ledger evaluates it.

    strategy/smart_exit prices a held pick only at the recorded MILESTONE rows (club
    fixtures have no per-minute Poly Global ticks): at each milestone minute mn ≥ the entry
    minute it reconstructs the score from fixture_event, prices the live fair with the PIT
    lambdas (knockout-scaled by the round), takes the row's own price for the pick — Kalshi
    bid, else ask, else Poly — and sells the first time price ≥ fair + trigger. That is the
    "15' 卖 43¢" the 择时 tab shows. The first version of this leg re-evaluated every cycle on
    the live demo bid, which would have sold more often and earlier than the ledger; this one
    decides on the SAME row, the SAME minute and the SAME price the ledger will read back,
    and only executes on the demo venue. A milestone is evaluated exactly once per position;
    a triggered sell that the venue did not fill is retried on later cycles (the decision
    stands — the ledger sold at that minute).
    """
    from prediction_market_soccer.model.inplay import live_match_prob
    from prediction_market_soccer.model.inplay_constants import OVERSHOOT_MARGIN, overshoot_trigger
    from prediction_market_soccer.model.match_pricing import is_knockout
    from prediction_market_soccer.ops.settle_bets import _event_timelines, _state_at
    from prediction_market_soccer.util.pricing import reg_score
    rows = conn.execute("SELECT * FROM kalshi_mirror WHERE status='open'").fetchall()
    if not rows:
        return []
    cmap = _cmap(conn)
    actions = []
    venue_pos: dict[str, float] | None = None
    for r in rows:
        fid, side, ticker, track = r["fixture_api_id"], r["side"], r["ticker"], r["track"]
        held = float(r["fill_count"]) - float(r["exit_fill_count"] or 0.0)
        if held < 1:
            conn.execute("UPDATE kalshi_mirror SET status='exited' WHERE id=?", (r["id"],)); continue
        fx = _fixture(conn, fid)
        if fx is None:
            continue
        # settled at the whistle → bookkeeping only (the venue settles the contract itself)
        if fx["status_short"] in _FINISHED and fx["home_goals"] is not None:
            gh, ga = reg_score(fx["raw_json"], fx["home_goals"], fx["away_goals"])
            result = "home" if gh > ga else ("draw" if gh == ga else "away")
            won = int(side == result)
            entry_c = float(r["avg_fill_c"] or r["ask_c"] or 0.0)
            pnl = (100.0 - entry_c) if won else -entry_c
            conn.execute("UPDATE kalshi_mirror SET status='settled', exit_reason='settled', won=?, pnl_c=?, "
                         "exited_at=? WHERE id=?", (won, round(pnl, 1), _iso(_now()), r["id"]))
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
        ms_rows = conn.execute(
            "SELECT * FROM milestone_snapshot WHERE fixture_api_id=? AND milestone IN ('T15','T30','HT','T60','T75')",
            (fid,)).fetchall()
        ms_rows.sort(key=lambda m: _MILESTONE_MIN[m["milestone"]])
        decided_min, decided_price, decided_fair, decided_trig = None, None, None, None
        if pending_min is not None:
            decided_min, decided_price, decided_fair = pending_min, r["exit_bid_c"], r["exit_fair_c"]
        else:
            lam = None
            for m in ms_rows:
                mn = _MILESTONE_MIN[m["milestone"]]
                if mn < entry_floor or mn > _REG_MAX_MIN:
                    continue
                ekey = f"exit:{track}:{m['milestone']}"
                if conn.execute("SELECT 1 FROM kalshi_mirror_eval WHERE fixture_api_id=? AND milestone=?",
                                (fid, ekey)).fetchone():
                    continue
                # the row's own price for the pick — the ledger's _milestone_ticks order
                price = next((v for v in (m[f"kalshi_{side}_bid"], m[f"kalshi_{side}_ask"],
                                          m[f"poly_{side}_bid"], m[f"poly_{side}_ask"]) if v is not None), None)
                if price is None:
                    _mark_eval(conn, fid, ekey, "no_price"); continue
                try:
                    if lam is None:
                        sm = strength.get(fx["kickoff_ts"], r["comp"])
                        lam = sm.pair_lambdas(hi, ai, knockout=is_knockout(fx["round"]))
                    goals, _reds = _event_timelines(conn, fid, fx["home_api_id"])
                    sh, sa = _state_at(goals, mn)
                    lp = live_match_prob(lam[0], lam[1], mn, sh, sa)     # no red-card term — as smart_exit
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
                    break
            conn.commit()
        if decided_min is None:
            continue                       # no milestone over-reaction (yet) — hold
        # execute on the demo venue at ITS current bid; never sell more than the venue holds
        try:
            ob = broker.book(ticker)
        except Exception as e:  # noqa: BLE001
            _log("exit_book_error", fixture=fid, ticker=ticker, error=str(e)[:160]); continue
        bid = float(ob.yes_bid) if ob.yes_bid is not None else None
        if bid is None or not (0.0 < bid < 1.0):
            _log("exit_no_bid", fixture=fid, ticker=ticker); continue
        if venue_pos is None:
            try:
                venue_pos = broker.positions()
            except Exception:  # noqa: BLE001
                venue_pos = {}
        vp = venue_pos.get(ticker)
        n = int(held) if vp is None else max(0, min(int(held), int(vp)))
        if n < 1:
            _log("exit_nothing_held", fixture=fid, ticker=ticker, ours=held, venue=vp); continue
        coid = f"mirror-exit-{track}-{fid}-{uuid.uuid4().hex[:8]}"
        conn.execute("UPDATE kalshi_mirror SET exit_client_order_id=? WHERE id=?", (coid, r["id"]))
        conn.commit()
        try:
            res = broker.sell_yes(ticker, n, bid, coid)
        except Exception as e:  # noqa: BLE001
            _log("exit_send_error", fixture=fid, ticker=ticker, error=str(e)[:160]); continue
        if not res["ok"]:
            conn.execute("UPDATE kalshi_mirror SET note=? WHERE id=?",
                         (f"exit http {res['status_code']}: {str(res['raw'])[:120]}", r["id"]))
            conn.commit()
            _log("exit_venue_error", fixture=fid, ticker=ticker, http=res["status_code"], raw=res["raw"])
            continue                       # decision stands → retried next cycle
        sold = res["fill_count"]
        new_exit_fill = float(r["exit_fill_count"] or 0.0) + sold
        remaining = float(r["fill_count"]) - new_exit_fill
        entry_c = float(r["avg_fill_c"] or r["ask_c"] or 0.0)
        sold_c = (res["avg_fill"] * 100.0) if res["avg_fill"] else (bid * 100.0)
        done = sold > 0 and remaining < 1
        conn.execute(
            "UPDATE kalshi_mirror SET exit_order_id=?, exit_fill_count=?, exit_avg_c=?, exited_at=?, "
            "status=?, exit_reason=?, pnl_c=?, raw_json=json_patch(coalesce(raw_json,'{}'), ?) WHERE id=?",
            (res["order_id"], new_exit_fill, (round(sold_c, 1) if sold else None), (_iso(_now()) if done else None),
             ("exited" if done else "open"), ("smart_exit" if done else None),
             (round(sold_c - entry_c, 1) if done else None),
             json.dumps({"exit_response": res.get("raw"), "exit_sent": res.get("sent")}, default=str), r["id"]))
        conn.commit()
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
def _reconcile_pending(conn, broker: DemoBroker) -> int:
    n = 0
    for r in conn.execute("SELECT id, ticker, client_order_id, ask_c FROM kalshi_mirror WHERE status='pending'").fetchall():
        try:
            orders = broker.orders_for(r["ticker"])
        except Exception:  # noqa: BLE001
            continue
        o = next((x for x in orders if x.get("client_order_id") == r["client_order_id"]), None)
        if o is None:
            continue
        filled = float(o.get("fill_count_fp") or 0.0)
        px = o.get("yes_price_dollars")
        conn.execute("UPDATE kalshi_mirror SET status=?, order_id=?, fill_count=?, avg_fill_c=?, filled_at=? WHERE id=?",
                     ("open" if filled > 0 else "unfilled", o.get("order_id"), filled,
                      (round(float(px) * 100, 1) if (px and filled) else None), (_iso(_now()) if filled else None), r["id"]))
        n += 1
    if n:
        conn.commit()
    return n


# ── the cycle ─────────────────────────────────────────────────────────────────
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
    if inplay_doc is None:
        from prediction_market_soccer.ops import inplay_export
        inplay_doc = inplay_export.build(conn, with_venues=True)
    live = [m for m in (inplay_doc.get("matches") or []) if m.get("fixture_id")]
    live_by_fid = {int(m["fixture_id"]): m for m in live}
    tickers, strength = _Tickers(), _Strength(conn)
    out = {"enabled": True, "actions": [], "errors": []}
    for name, fn in (("reconcile", lambda: _reconcile_pending(conn, broker)),
                     ("pre", lambda: _scan_pre(conn, broker, tickers, strength, load_pit_records())),
                     ("inplay", lambda: _scan_inplay(conn, broker, tickers, list(live_by_fid))),
                     ("exits", lambda: _scan_exits(conn, broker, strength, live_by_fid))):
        try:
            res = fn()
            if isinstance(res, list):
                out["actions"].extend(res)
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
           "last_cycle": {k: last.get(k) for k in ("actions", "errors", "elapsed_s")},
           "rows": rows}
    CONFIG.paths.ensure()
    _EXPORT.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ── CLI ───────────────────────────────────────────────────────────────────────
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
