"""synth/worlds.py — a synthetic macro path becomes a schema-identical sqlite database.

**Why a database and not an API.** A scored observation is one call to
`pnl_score.event_pnl(conn, series, tok, key, close_ts, params)`, and everything that
function needs it reads from `conn`. Handing it a synthetic `conn` therefore runs the
REAL strategy — `decide()`, the db-state gates, `exits.run`'s rules, the taker-fee model,
`grid_pmf`, the settlement laddering — against invented data, with not one line of it
re-implemented here. The alternative (re-deriving PnL from a generated path) would be a
second implementation of the strategy, and this repo has already paid for that mistake
twice: `pnl_score` itself diverged from `walkforward` on gates (#125/#133) and again on
exits (#144), each time silently ranking parameter sets on a strategy production does not
run. A synthetic world cannot diverge, because there is nothing in it to diverge.

**What has to be in the world.** Audited against every read `event_pnl` performs,
transitively through `FeatureStore`, the eight model modules, `pit_gates.GateHistory` and
the `walkforward` helpers it imports:

| table | who reads it | empty behaviour |
|---|---|---|
| `fred_obs` | every model, via `FeatureStore` | model raises, event skipped — **silent zero** |
| `fut_daily` | energy (CL/NG/RB), cpi (RB), fed (ZQ) | `RuntimeError` under 25 bars |
| `contracts` | `_legs_at` | no legs, event unscoreable |
| `settlements` | `_legs_at`, `quotable_events` | event invisible |
| `candles` | `_candle_quote` | leg dropped from the book |
| `event_flags` | the severity gate | no flag, proceeds |
| `releases` | `decide`'s freeze window | no freeze, proceeds |
| `preds` | `GateHistory` when `db_gates=True` | no gate state, proceeds ungated |
| `quotes` | fed's market prior | empty prior |
| `cleveland_nowcast` | cpi YoY anchor | falls back to the internal model |
| `fed_statements` | fed's meeting panel | no meetings |

The dangerous rows in that table are the ones whose empty behaviour is *graceful*. A world
missing `fred_obs` fails loudly; a world missing `preds` quietly scores every candidate
ungated, and a world missing `cleveland_nowcast` quietly scores CPI on a different model
than production runs. Both would produce plausible numbers that answer the wrong question.
So `materialize` copies every one of them and `verify_world` asserts the counts rather than
trusting that they were copied.

**The round trip is the gate.** `roundtrip(conn, series)` rebuilds a REAL event through
the same writers a synthetic world uses — same settlement laddering, same knowledge-time
stamping, same candle arithmetic — and requires `event_pnl` to return bit-identical
numbers against the rebuilt world and against production. Until that passes, nothing
downstream means anything: a writer that stamps a release an hour late, or ladders a
settlement with the wrong strictness, produces a world in which the strategy is scored on
a subtly different game and no amount of generator quality would show it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from prediction_market_macro.config.registry import REGISTRY

# Tables carried into a world, with the column whose value decides whether a row was
# knowable at the cutoff. `None` means the table is copied whole.
#
# `contracts` is copied whole ON PURPOSE, and it is the one table where that is the PIT-safe
# choice rather than a shortcut. Its `first_seen_ts` is not a listing time — it is the
# 2026-07-28 backfill stamp, identical for years of history — so truncating on it would
# empty the table in every world spliced before that date. What a contract row carries is
# the ladder: strikes and close time, both published well in advance and therefore knowable.
# The OUTCOME lives in `settlements` and the PRICES in `candles`, and those two are
# truncated, which is what actually stops a world from seeing its own future.
_PIT_TABLES: dict[str, str | None] = {
    "fred_obs": "knowledge_time",
    "fut_daily": "knowledge_time",
    "quotes": "ts",
    "cleveland_nowcast": "knowledge_time",
    "fed_statements": "knowledge_time",
    "event_flags": "ts",
    "releases": "scheduled_ts",
    "preds": "asof",
    "contracts": None,
    "settlements": "settled_ts",
    "candles": None,          # end_ts is an epoch INTEGER — special-cased in materialize
}

# Kalshi's 404 sentinel: `kalshi_md.candles` writes end_ts=0 when a ticker has no
# candlesticks at all, and `quotable_events` excludes those rows with `end_ts > 100000`.
# A synthetic world must clear the same bar or its events are invisible to the very
# function that is supposed to score them.
CANDLE_SENTINEL = 100_000

# When a futures settlement becomes knowable: the evening of its own session. Named rather
# than left as a bare default so a caller computing a splice from a futures anchor uses the
# same hour the bars are stamped with, instead of a second copy that can drift.
FUT_CLOSE_HOUR = 20


# ── schema ───────────────────────────────────────────────────────────────────
def clone_schema(src: sqlite3.Connection, dst: sqlite3.Connection) -> list[str]:
    """Copy every CREATE statement verbatim from `src`.

    Verbatim, from `sqlite_master`, rather than a schema literal kept in this file: the
    production schema changes and a copy here would rot silently, which for this module
    means synthetic worlds that are subtly not the shape production reads. The cost is
    that a world carries empty tables it never uses; that costs nothing.
    """
    made = []
    for (sql,) in src.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0"
            " WHEN 'index' THEN 1 ELSE 2 END").fetchall():
        dst.execute(sql)
        made.append(sql)
    dst.commit()
    return made


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


# ── point-in-time copy ───────────────────────────────────────────────────────
def materialize(src: sqlite3.Connection, dst_path: Path | str,
                cutoff: datetime | None = None) -> sqlite3.Connection:
    """Create a world at `dst_path` holding everything knowable at `cutoff`.

    `cutoff` is the splice point: real history up to it, generated path after it. Passing
    `None` copies everything, which is what the round trip uses.

    The truncation is by KNOWLEDGE time, not event time. A CPI print for month T becomes
    knowable two weeks into T+1, so a world spliced at T must still contain the T-1 print
    that was published inside T — truncating on event_time would silently delete the most
    recent data every model actually reads, and every model would then be run on a
    shorter history than production gives it.

    The market side is truncated too, and that is not decoration. `settlements` reaches back
    to 2021 with a real `settled_ts`, and KXAAAGASW's model regresses its settle-vs-proxy
    gap on PAST settled events (`energy._aaa_settled_mids`); left whole, a world spliced at
    T would hand that regression the outcomes of events that had not happened yet. The
    model does filter `settled_ts <= asof` itself, so today this is defence in depth rather
    than a live leak — but "no leak because every consumer remembered to filter" is the
    weaker of the two guarantees, and this repo has been bitten twice by the strong one
    being absent.
    """
    dst_path = Path(dst_path)
    if dst_path.exists():
        dst_path.unlink()
    dst = sqlite3.connect(str(dst_path))
    dst.row_factory = sqlite3.Row
    clone_schema(src, dst)
    have = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    cut = cutoff.isoformat() if cutoff is not None else None
    for table, tcol in _PIT_TABLES.items():
        if table not in have:
            continue
        cols = _columns(src, table)
        sel = f"SELECT {','.join(cols)} FROM {table}"
        args: tuple = ()
        if cut is not None and tcol is not None:
            sel += f" WHERE {tcol}<=?"
            args = (cut,)
        elif cut is not None and table == "candles":
            # `end_ts` is epoch seconds, so it cannot go through the ISO comparison above.
            # The 404 sentinel (end_ts=0) is kept deliberately: it records "this ticker has
            # no candlesticks at all", which was as true before the cutoff as after, and
            # dropping it would make an unquotable event look merely unrecorded.
            sel += " WHERE end_ts<=?"
            args = (int(cutoff.timestamp()),)
        rows = src.execute(sel, args).fetchall()
        if rows:
            dst.executemany(
                f"INSERT OR REPLACE INTO {table}({','.join(cols)})"
                f" VALUES({','.join('?' * len(cols))})",
                [tuple(r) for r in rows])
    dst.commit()
    return dst


# ── publication lag ──────────────────────────────────────────────────────────
def publication_lag(conn: sqlite3.Connection, sid: str,
                    default_hour: int = 13) -> tuple[int, int]:
    """(median days from event_time to first vintage, release hour UTC) for `sid`.

    Measured, not tabulated. A synthetic release has to be stamped with a knowledge time
    or the models will read it the instant it is "created", which is the exact PIT leak
    this repo has already been bitten by — `project_macro_replay_pit_fixes` records two
    separate occasions. Measuring it from the real `fred_obs` gets NFP's three-week lag,
    claims' five days and WTI's same-day right without a table anyone has to maintain.

    The median is used rather than the mean because holiday shifts and the occasional
    re-benchmarking produce a long right tail that a mean would follow.
    """
    rows = conn.execute(
        "SELECT event_time, MIN(vintage_date) v, MIN(knowledge_time) k FROM fred_obs"
        " WHERE sid=? GROUP BY event_time ORDER BY event_time DESC LIMIT 60",
        (sid,)).fetchall()
    if not rows:
        raise ValueError(f"publication_lag: no fred_obs rows for {sid!r}")
    days, hours = [], []
    for r in rows:
        try:
            ev = datetime.fromisoformat(str(r["event_time"]))
            vi = datetime.fromisoformat(str(r["v"]))
        except ValueError:
            continue
        days.append((vi.date() - ev.date()).days)
        kt = str(r["k"]).replace("Z", "+00:00")
        try:
            hours.append(datetime.fromisoformat(kt).hour)
        except ValueError:
            pass
    if not days:
        raise ValueError(f"publication_lag: unparseable event/vintage dates for {sid!r}")
    return int(np.median(days)), int(np.median(hours)) if hours else default_hour


# ── writing a generated path ─────────────────────────────────────────────────
def write_fred(dst: sqlite3.Connection, sid: str, values: pd.Series,
               lag_days: int, hour: int, first_seen: str = "synthetic") -> int:
    """Write generated observations of `sid` as first prints stamped point-in-time.

    One vintage per observation, `vintage_date = event_time + lag_days`. That makes a
    synthetic world revision-free — first print equals latest vintage — which is the same
    assumption `panel.py` trains under and is stated in both places rather than in
    neither. It is safe for every model in the registry today because none of them trades
    the revision process; a model that did would be scored generously here, and this is
    the line to revisit if one is ever written.

    **The two DELETEs are load-bearing and neither is housekeeping.** `fred_obs` is keyed
    `(sid, event_time, vintage_date)`, so `INSERT OR REPLACE` alone does NOT replace a real
    observation of the same week — it lands BESIDE it whenever the real vintage date
    differs by even a day, which it routinely does (holiday-shifted releases, any revision).
    `FeatureStore` would then serve whichever vintage its own rule picks, and a synthetic
    world would be reading a mixture of the generated path and the history it was supposed
    to replace, week by week, with nothing raising. The first DELETE clears every vintage
    of every week being generated. The second clears every real week AFTER the generated
    path begins, because a real print the path happens not to cover is a leaked future
    observation of a series the model reads, and it would be read as fact.
    """
    rows = []
    for ts, v in values.dropna().items():
        ev = pd.Timestamp(ts).to_pydatetime()
        vint = (ev + timedelta(days=lag_days)).date()
        kt = datetime(vint.year, vint.month, vint.day, hour, tzinfo=timezone.utc)
        rows.append((sid, ev.date().isoformat(), float(v), vint.isoformat(),
                     kt.isoformat(), first_seen))
    if not rows:
        return 0
    start = min(r[1] for r in rows)
    dst.execute("DELETE FROM fred_obs WHERE sid=? AND event_time>=?", (sid, start))
    dst.executemany(
        "INSERT OR REPLACE INTO fred_obs(sid, event_time, value, vintage_date,"
        " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)", rows)
    dst.commit()
    return len(rows)


def write_fut(dst: sqlite3.Connection, root: str, closes: pd.Series,
              hour: int = FUT_CLOSE_HOUR, first_seen: str = "synthetic") -> int:
    """Write generated futures bars. Only `close` is generated; OHLC are set to it.

    Nothing in the consuming models reads open/high/low — `FeatureStore.fut_closes` selects
    `close` alone — so inventing an intrabar range would be fabricating detail no scorer
    can see, in a table a human might later mistake for real bars.

    `fut_daily` is keyed `(root, event_time)` so the REPLACE genuinely replaces, but the
    trailing DELETE is still needed for the same reason as in `write_fred`: a real bar on a
    date the generated path skips (the path is weekly, the real series daily) would survive
    inside the synthetic future as a true observation of where the market went.
    """
    rows = []
    for ts, v in closes.dropna().items():
        d = pd.Timestamp(ts).to_pydatetime()
        kt = datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)
        rows.append((root, d.date().isoformat(), float(v), float(v), float(v), float(v),
                     None, kt.isoformat(), first_seen))
    if not rows:
        return 0
    dst.execute("DELETE FROM fut_daily WHERE root=? AND event_time>=?",
                (root, min(r[1] for r in rows)))
    dst.executemany(
        "INSERT OR REPLACE INTO fut_daily(root, event_time, open, high, low, close,"
        " volume, knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    dst.commit()
    return len(rows)


# ── settlement ───────────────────────────────────────────────────────────────
def settle_leg(leg: dict, y: float, strict_gt: bool) -> str:
    """'yes' | 'no' for one contract leg given the released value.

    The four strike types Kalshi actually uses in this book, counted from `contracts`:
    `greater` (5483), `between` (1953), `greater_or_equal` (502), `less` (145). `custom`
    (199) and NULL (604) are refused rather than guessed — a custom leg's rule lives in
    prose in the rulebook, and settling it by a plausible-looking default would put a
    wrong outcome into the sample with no signal that it happened.

    `strict_gt` comes from the series registry, where it is a rulebook-verified fact:
    KXCPI's "Above X" settles NO when the print equals X, KXJOBLESSCLAIMS' does not. The
    registry is the single source and this function must not develop an opinion of its own,
    because a strictness error is invisible in aggregate and lands entirely on the
    at-the-money leg, which is the one that decides most of the PnL.
    """
    st = leg.get("strike_type")
    lo, hi = leg.get("floor_strike"), leg.get("cap_strike")
    if st in ("greater", "greater_or_equal"):
        if lo is None:
            raise ValueError(f"{leg.get('ticker')}: {st} leg has no floor_strike")
        strict = strict_gt if st == "greater" else False
        return "yes" if (y > lo if strict else y >= lo) else "no"
    if st == "less":
        if hi is None:
            raise ValueError(f"{leg.get('ticker')}: less leg has no cap_strike")
        return "yes" if y < hi else "no"
    if st == "between":
        if lo is None or hi is None:
            raise ValueError(f"{leg.get('ticker')}: between leg missing a bound")
        return "yes" if lo <= y <= hi else "no"
    raise ValueError(f"{leg.get('ticker')}: cannot settle strike_type {st!r} "
                     "— custom and NULL legs have prose rules and are not guessable")


def leg_category(ticker: str) -> str:
    """The category label of a mutually-exclusive leg: the ticker suffix after the last '-'.

    `decide_all._structs_categorical` keys the model's probabilities by exactly this, and
    ignores `strike_type` entirely while doing it. Deriving the label the same way here is
    not a convention invented for this module — it is the one the strategy already trades on,
    and any other choice would settle a leg the pricer never matched.
    """
    return ticker.rsplit("-", 1)[-1]


def settle_category(legs: list[dict], cat: str) -> dict[str, str]:
    """YES on the one leg whose category is `cat`, NO on the rest.

    KXFEDDECISION is five mutually-exclusive `custom` legs (Hike >25bps / Hike 25bps /
    maintain / Cut 25bps / Cut >25bps) with no numeric strikes at all, so `settle_leg`
    cannot and must not touch it: there is no threshold to compare against. Exactly one leg
    settles YES, by construction of the market, and an outcome naming no leg is refused
    rather than settled as an all-NO event that would look like a valid observation.
    """
    cats = {l["ticker"]: leg_category(l["ticker"]) for l in legs}
    if cat not in set(cats.values()):
        raise ValueError(f"settle_category: outcome {cat!r} names no leg of "
                         f"{sorted(set(cats.values()))}")
    return {tk: ("yes" if c == cat else "no") for tk, c in cats.items()}


def settle_event(legs: list[dict], y: float | str, series: str) -> dict[str, str]:
    """Settle a whole event: ladder a released value, or pick the realised category.

    Which one is decided by `REGISTRY[series].structure`, not by the type of `y` and not by
    inspecting the legs. The registry is where every other module asks this question and a
    second opinion here could disagree with it silently.
    """
    spec = REGISTRY[series]
    if spec.structure == "categorical":
        if not isinstance(y, str):
            raise ValueError(f"settle_event: {series} is categorical and needs a category "
                             f"label, got {y!r}")
        return settle_category(legs, y)
    if isinstance(y, str):
        raise ValueError(f"settle_event: {series} is a {spec.structure} and needs a "
                         f"numeric released value, got {y!r}")
    return {l["ticker"]: settle_leg(l, y, spec.strict_gt) for l in legs}


# ── writing an event ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EventPlan:
    """One synthetic event: its ladder, its close, its outcome, and its book.

    `book` is (ticker -> [(end_ts, yes_bid, yes_ask)]). It is NOT generated here and this
    module will not invent one: §5 of the plan is explicit that a fabricated counterparty
    makes the parameter search optimise against our own pricing error, which is worse than
    not running. `book.py` (S4) transplants it from the 63 real events; this module only
    writes down what it is handed.

    `settle_time` is separate from `close_time` because it is READ, and PIT: `energy.py`'s
    `_aaa_settled_mids` builds its drift prior from `settlements.settled_ts <= asof`, so a
    world that stamped settlement at the close would hand that model outcomes hours early.
    Defaulting it to the close is the conservative direction (the print is knowable no
    earlier than the market shuts), and every real event carries its own value through
    `read_event` anyway.

    Per-leg `close_time`/`settled_ts` inside a `legs` dict WIN over these event-level
    values. Real events genuinely disagree leg-to-leg — 10 of 808 do, KXFED/22DEC across 5
    distinct closes — and `quotable_events` filters candles on each leg's own close, so
    flattening them to the event maximum would make legs quotable that production never
    quoted.
    """
    series: str
    period: str
    legs: list[dict]
    close_time: datetime
    outcome: float | str
    book: dict[str, list[tuple[int, float, float]]]
    settle_time: datetime | None = None
    event_ticker: str | None = None
    status: str = "settled"


def write_event(dst: sqlite3.Connection, plan: EventPlan,
                first_seen: str = "synthetic") -> dict:
    """Write one event's contracts, candles and settlements into a world.

    `first_seen_ts` is stamped "synthetic" rather than carried, deliberately: nothing in
    the scoring path reads it (it is an ingest-idempotency column), so it costs no fidelity
    and it leaves every fabricated row self-identifying in a database that otherwise looks
    exactly like production.

    Returns a small summary so callers can assert on it instead of re-querying.
    """
    results = settle_event(plan.legs, plan.outcome, plan.series)
    ct = plan.close_time.isoformat()
    st_ts = (plan.settle_time or plan.close_time).isoformat()
    etk = plan.event_ticker or f"{plan.series}-{plan.period}"
    con_rows, set_rows, cdl_rows = [], [], []
    for leg in plan.legs:
        tk = leg["ticker"]
        con_rows.append((tk, plan.series, etk, plan.period,
                         leg.get("sub_title"), leg.get("strike_type"),
                         leg.get("floor_strike"), leg.get("cap_strike"),
                         leg.get("close_time") or ct,
                         leg.get("status") or plan.status, first_seen))
        set_rows.append((tk, plan.series, plan.period, results[tk],
                         leg.get("settled_ts") or st_ts, first_seen))
        for end_ts, bid, ask in plan.book.get(tk, []):
            if end_ts <= CANDLE_SENTINEL:
                raise ValueError(f"{tk}: candle end_ts={end_ts} is at or below the 404 "
                                 "sentinel and quotable_events would not see this event")
            mid = None if bid is None or ask is None else 0.5 * (bid + ask)
            cdl_rows.append((tk, int(end_ts), bid, ask, mid, None))
    dst.executemany(
        "INSERT OR REPLACE INTO contracts(ticker, series, event_ticker, period, sub_title,"
        " strike_type, floor_strike, cap_strike, close_time, status, first_seen_ts)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)", con_rows)
    dst.executemany(
        "INSERT OR REPLACE INTO settlements(ticker, series, period, result, settled_ts,"
        " first_seen_ts) VALUES(?,?,?,?,?,?)", set_rows)
    dst.executemany(
        "INSERT OR REPLACE INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
        " price_close, volume) VALUES(?,?,?,?,?,?)", cdl_rows)
    dst.commit()
    return {"series": plan.series, "period": plan.period, "legs": len(con_rows),
            "candles": len(cdl_rows), "outcome": plan.outcome,
            "yes_legs": sum(v == "yes" for v in results.values())}


# ── reading a real event back out, for transplanting and for the round trip ──
def read_event(conn: sqlite3.Connection, series: str, period: str) -> EventPlan:
    """The real event as an `EventPlan`, so it can be re-written through the writers.

    This is what makes the round trip meaningful. Copying rows with `INSERT..SELECT` would
    prove only that sqlite copies rows. Passing a real event back through `write_event`
    exercises the settlement laddering, the close-time stamping and the candle arithmetic
    — where the bugs that matter would actually live — and then demands that `event_pnl`
    not notice.
    """
    legs = [dict(r) for r in conn.execute(
        "SELECT c.ticker, c.sub_title, c.strike_type, c.floor_strike, c.cap_strike,"
        " c.close_time, c.status, c.event_ticker, s.result, s.settled_ts FROM contracts c"
        " JOIN settlements s ON s.ticker=c.ticker"
        " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')"
        " ORDER BY c.ticker", (series, period)).fetchall()]
    if not legs:
        raise ValueError(f"read_event: {series}/{period} has no settled legs")
    book: dict[str, list[tuple[int, float, float]]] = {}
    for leg in legs:
        book[leg["ticker"]] = [
            (int(r["end_ts"]), r["yes_bid_close"], r["yes_ask_close"])
            for r in conn.execute(
                "SELECT end_ts, yes_bid_close, yes_ask_close FROM candles"
                " WHERE ticker=? AND end_ts>? ORDER BY end_ts",
                (leg["ticker"], CANDLE_SENTINEL)).fetchall()]
    cts = [l["close_time"] for l in legs if l["close_time"]]
    if not cts:
        raise ValueError(f"read_event: {series}/{period} has no close_time")
    close = datetime.fromisoformat(max(cts).replace("Z", "+00:00"))
    sts = [l["settled_ts"] for l in legs if l["settled_ts"]]
    settle = (datetime.fromisoformat(max(sts).replace("Z", "+00:00")) if sts else None)
    y = _implied_outcome(legs, series)
    return EventPlan(series=series, period=period, legs=legs, close_time=close,
                     outcome=y, book=book, settle_time=settle,
                     event_ticker=legs[0].get("event_ticker"))


def _implied_outcome(legs: list[dict], series: str) -> float | str:
    """Recover an outcome consistent with the real legs' yes/no pattern.

    The database stores each leg's settlement but not the value that produced it, and the
    round trip needs that value to drive `settle_event`. On a ladder, any number inside the
    interval the pattern implies reproduces the pattern exactly, so the midpoint of the
    tightest consistent interval is chosen, with an open side resolved one `round_rule`
    step out. On a categorical market there is nothing to interpolate: the outcome IS the
    single YES leg's category.

    If no value can reproduce the stored pattern, that is raised rather than approximated:
    it means the stored settlements are mutually inconsistent under the registry's
    strictness, which is a real finding about the data and not something to paper over.
    """
    spec = REGISTRY[series]
    if spec.structure == "categorical":
        yes = [l["ticker"] for l in legs if l.get("result") == "yes"]
        if len(yes) != 1:
            raise ValueError(f"_implied_outcome: {series} is categorical and mutually "
                             f"exclusive but {len(yes)} legs settled YES")
        return leg_category(yes[0])
    lo, hi = implied_interval(legs, series)
    step = REGISTRY[series].round_rule
    # An open side has to be closed SOMEHOW to name a single number, and one grid step past
    # the last strike is the least committal choice. It is a choice, not a fact, which is
    # why `implied_interval` does not make it: the top bucket of KXPAYROLLS is "300,000 or
    # more" and January 2024 printed 353,000, so treating `lo + step` as an upper BOUND
    # would call a correct settlement value inconsistent.
    if np.isinf(lo):
        lo = hi - step
    if np.isinf(hi):
        hi = lo + step
    return float(0.5 * (lo + hi))


def implied_interval(legs: list[dict], series: str) -> tuple[float, float]:
    """The tightest interval the legs' yes/no pattern pins the outcome to, infinities kept.

    Split out from `_implied_outcome` because the interval is the stronger statement and the
    two callers want different things from it. The round trip needs a single number, so it
    closes an open side arbitrarily and takes the midpoint — harmless, because any value
    inside reproduces the pattern it is about to re-derive. `build.verify_settle` is asking
    whether a recomputed settlement value is CONSISTENT with what actually paid out, and for
    that an open side must stay open: the extreme buckets of every ladder here are
    unbounded, and closing them one step out would reject exactly the large prints the
    market cared most about.
    """
    spec = REGISTRY[series]
    lo, hi = -np.inf, np.inf
    for leg in legs:
        st, f, c, res = (leg.get("strike_type"), leg.get("floor_strike"),
                         leg.get("cap_strike"), leg.get("result"))
        if st in ("greater", "greater_or_equal") and f is not None:
            if res == "yes":
                lo = max(lo, f)
            else:
                hi = min(hi, f)
        elif st == "less" and c is not None:
            if res == "yes":
                hi = min(hi, c)
            else:
                lo = max(lo, c)
        elif st == "between" and f is not None and c is not None and res == "yes":
            lo, hi = max(lo, f), min(hi, c)
    if np.isinf(lo) and np.isinf(hi):
        raise ValueError(f"implied_interval: {series} legs pin no interval at all")
    if lo > hi:
        raise ValueError(f"implied_interval: {series} settlements are inconsistent — "
                         f"legs imply the value is both >{lo} and <{hi}")
    return float(lo), float(hi)


# ── verification ─────────────────────────────────────────────────────────────
def verify_world(dst: sqlite3.Connection, src: sqlite3.Connection,
                 cutoff: datetime | None = None) -> dict:
    """Row counts per carried table, world vs source, plus the tables that came back empty.

    The reason this exists rather than trusting `materialize`: most of the tables a world
    carries fail GRACEFULLY when empty (see the module docstring). A world with no `preds`
    scores every candidate ungated and reports confident numbers; a world with no
    `cleveland_nowcast` scores CPI on a fallback model production does not run. Neither
    raises. So emptiness is checked explicitly, and `empty` is returned for the caller to
    assert on rather than merely logged.
    """
    out: dict[str, dict] = {}
    have = {r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table in _PIT_TABLES:
        if table not in have:
            continue
        n_dst = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_src = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        out[table] = {"world": int(n_dst), "source": int(n_src)}
    return {"tables": out, "cutoff": cutoff.isoformat() if cutoff else None,
            "empty": sorted(t for t, v in out.items()
                            if v["world"] == 0 and v["source"] > 0)}


# ── the round trip ───────────────────────────────────────────────────────────
_SCORE_KEYS = ("edge", "argmax", "hybrid", "staked", "traded", "stream")


def rebuild_event(dst: sqlite3.Connection, plan: EventPlan,
                  first_seen: str = "synthetic") -> dict:
    """Replace one event in `dst` by re-writing it through the synthetic writers.

    The DELETE is not housekeeping. A world inherits the real ladder from the byte copy,
    so writing the rebuilt legs on top with INSERT OR REPLACE would leave any leg the
    rebuild DROPPED — an unsettled leg, a leg the plan chose not to carry — sitting in the
    world as a real row, and `_legs_at` would happily quote it. Clearing the event's
    tickers first makes the rebuilt event the ONLY event, which is the claim being tested.
    """
    tks = [l["ticker"] for l in plan.legs]
    marks = ",".join("?" * len(tks))
    for table in ("candles", "settlements", "contracts"):
        dst.execute(f"DELETE FROM {table} WHERE ticker IN ({marks})", tks)
    stale = dst.execute(
        "SELECT COUNT(*) FROM contracts WHERE series=? AND period=?",
        (plan.series, plan.period)).fetchone()[0]
    if stale:
        raise ValueError(f"rebuild_event: {plan.series}/{plan.period} still has {stale} "
                         "contract rows the plan does not name — the rebuilt event would "
                         "not be the whole event")
    dst.commit()
    return write_event(dst, plan, first_seen=first_seen)


def clear_series(dst: sqlite3.Connection, series: str,
                 after: datetime | None = None) -> dict[str, int]:
    """Drop the real contracts, settlements and candles of `series` that the generated
    path replaces — those closing strictly after `after`, or all of them if `after` is None.

    A world inherits the whole market side by byte copy, which is right for `rebuild_event`
    — the round trip's claim is precisely that a real event survives being rewritten — and
    wrong for GENERATION. There, the real events are the ones whose outcomes the generated
    path is meant to replace, and leaving them in place would let `quotable_events` return
    a mixture: some events priced against a synthetic macro path, some against the real one
    that actually happened. That mixture scores, and it scores as though the synthetic
    sample were larger and more real than it is, which is the one error this whole exercise
    cannot afford.

    `after` exists because "clear the series" and "clear the replaced events" are not the
    same set, and the difference is a model. KXAAAGASW's drift regression is fitted on its
    OWN past settled events (`energy._aaa_settled_mids`, min_fit=10, ~28 available); wiping
    the series wholesale leaves that fit with nothing, the model silently falls back, and
    the synthetic world would then be scoring a materially weaker model than production
    runs. Pre-splice events are history the model is entitled to read. Pass the splice.

    Ordering matters: candles and settlements are cleared through the ticker list read from
    `contracts`, so `contracts` goes last.
    """
    if after is None:
        where, args = "series=?", (series,)
    else:
        # A NULL close_time cannot be placed relative to the splice. It is treated as
        # replaced, because an event whose ladder we cannot date is not history we can
        # justify letting a model read.
        where = "series=? AND (close_time IS NULL OR close_time>?)"
        args = (series, after.isoformat())
    # The ticker list goes in as a SUBQUERY, not as an expanded parameter list: KXWTIW
    # alone has 2,408 contracts, and an IN(?,?,...) of that width is one SQLite build away
    # from SQLITE_MAX_VARIABLE_NUMBER.
    pick = f"SELECT ticker FROM contracts WHERE {where}"
    out = {}
    for table in ("candles", "settlements"):
        out[table] = dst.execute(
            f"DELETE FROM {table} WHERE ticker IN ({pick})", args).rowcount
    out["contracts"] = dst.execute(f"DELETE FROM contracts WHERE {where}", args).rowcount
    # `settlements` also carries a series column, and a settlement whose contract row was
    # already missing from the copy would otherwise survive as an outcome with no ladder.
    orphan, oargs = ("series=?", (series,)) if after is None else (
        "series=? AND (settled_ts IS NULL OR settled_ts>?)", (series, after.isoformat()))
    out["settlements"] += dst.execute(
        f"DELETE FROM settlements WHERE {orphan}", oargs).rowcount
    dst.commit()
    return out


def roundtrip(conn: sqlite3.Connection, series: str, limit: int | None = None,
              work: Path | str | None = None, params: dict | None = None,
              log=None) -> dict:
    """Rebuild every scoreable event of `series` through the writers; demand identical PnL.

    This is the S3 gate, and it is a gate rather than a smoke test because everything
    downstream is an argument about numbers this module produces. The failure it exists to
    catch is not "the world is empty" — `verify_world` catches that, loudly. It is the
    world that scores *plausibly* and wrongly: a settlement laddered with the wrong
    strictness flips only the at-the-money leg, a close stamped an hour late moves only the
    last entry day, a candle written under the 404 sentinel makes an event invisible. Each
    shifts PnL by a realistic-looking amount, and each would be indistinguishable from the
    generator simply having produced a different macro path.

    So the comparison is deliberately the harshest one available: the SAME events, the SAME
    parameters, real data on both sides, and equality demanded on the exact dict keys the
    parameter search consumes — `hybrid` is what `param_argmin` sums, `staked` is the ROI
    denominator, `edge`/`argmax` are the two streams it can trade one against the other.
    Anything the writers do not preserve has nowhere to hide.

    Returns {"series","n","ok","mismatches",...}; `ok` is the whole verdict.
    """
    from prediction_market_macro.research import pnl_score as _ps
    from prediction_market_macro.util.periods import kalshi_period_to_key

    uni = [e for e in _ps.quotable_events(conn, series)
           if kalshi_period_to_key(e["tok"])]
    if not uni:
        raise ValueError(f"roundtrip: {series} has no quotable events to rebuild")
    if limit:
        uni = uni[-limit:]

    work = Path(work) if work is not None else Path("/tmp") / f"synth_rt_{series}.db"
    snapshot(conn, work)
    world = sqlite3.connect(str(work))
    world.row_factory = sqlite3.Row

    rebuilt, mismatches, skipped = [], [], []
    try:
        for ev in uni:
            try:
                plan = read_event(conn, series, ev["tok"])
            except ValueError as exc:
                # An event whose stored settlements pin no consistent value cannot be
                # rebuilt from an outcome, and inventing one would be exactly the quiet
                # wrongness this gate exists to refuse. Recorded, not swallowed: `ok`
                # stays False unless the caller has looked at these.
                skipped.append({"tok": ev["tok"], "why": str(exc)})
                continue
            rebuild_event(world, plan)
            rebuilt.append(ev)

        for ev in rebuilt:
            key = kalshi_period_to_key(ev["tok"])
            a = _ps.event_pnl(conn, series, ev["tok"], key, ev["close_ts"], params=params)
            b = _ps.event_pnl(world, series, ev["tok"], key, ev["close_ts"], params=params)
            if a is None or b is None:
                if a is not b:
                    mismatches.append({"tok": ev["tok"], "prod": a, "world": b,
                                       "field": "scoreable"})
                continue
            for k in _SCORE_KEYS:
                if a[k] != b[k]:
                    mismatches.append({"tok": ev["tok"], "field": k,
                                       "prod": a[k], "world": b[k]})
            if log:
                log(f"  {ev['tok']}: hybrid {a['hybrid']:+.2f} vs {b['hybrid']:+.2f}")
    finally:
        world.close()

    return {"series": series, "n": len(rebuilt), "skipped": skipped,
            "mismatches": mismatches, "world": str(work),
            "ok": not mismatches and not skipped}


def snapshot(src: sqlite3.Connection | Path | str, dst_path: Path | str) -> Path:
    """A full copy of a database as a world base, via sqlite's online backup API.

    NOT `shutil.copyfile`. `macro.db` runs in WAL mode with a live writer (`tick.py` every
    few minutes), so the `.db` file alone is a torn copy: everything committed since the
    last checkpoint lives in the `-wal` sidecar. A file copy would silently produce a world
    missing the most recent quotes and observations — which is to say, exactly the rows the
    round trip is comparing against — and would do it intermittently, depending on when the
    last checkpoint happened to land. `backup()` reads through WAL and holds a read
    transaction while it copies, so the result is a consistent point in time.
    """
    dst_path = Path(dst_path)
    if dst_path.exists():
        dst_path.unlink()
    own = not isinstance(src, sqlite3.Connection)
    conn = sqlite3.connect(str(src)) if own else src
    dst = sqlite3.connect(str(dst_path))
    try:
        conn.backup(dst)
    finally:
        dst.close()
        if own:
            conn.close()
    return dst_path
