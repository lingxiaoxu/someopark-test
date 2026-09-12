"""ops/exits.py — position exit policy (PLAN §11 exit.py; mother smart_exit's macro twin).

Default: hold to settlement. Early exit only when:
  1. edge reversal: the STRUCTURE's holding edge (sum over legs of fair - current mid,
     i.e. fair(struct) - cost(struct) at mid) < -0.06 with exit-side depth
  2. red-light: series circuit breaker tripped (health red / ledger mismatch /
     rolling-20 drawdown) → FORCED exit at market regardless of edge (铁律 10)
Freeze window (10 min pre-release) blocks exits too. Every exit = new ledger rows
(kind='exit') + paper fills at mid - $0.01 slippage; append-only always.

`shadow_run()` (PR-7 step 1 / #143) is the same measurement with the trigger tightened to
`hold_edge <= 0` and the execution removed — it writes to `shadow_exits` and never to
`decisions`. It shares `hold_state()` and `exit_realized()` with the live path on purpose:
a re-implementation would drift, and #141 is the standing proof of how expensive that is.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY, effective_strike_type
from prediction_market_macro.ops.ledger import open_positions
from prediction_market_macro.strategy.edge import taker_fee, two_sided
from prediction_market_macro.util.quotes import quote_is_fresh
from prediction_market_macro.util.execution import serialized_execution

EXIT_EDGE = -0.06
SLIP = 0.01


def _quote(conn, ticker, now: datetime | None = None):
    row = conn.execute(
        "SELECT ts, yes_bid, yes_ask, bid_depth, ask_depth FROM quotes WHERE ticker=?"
        " ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
    return row if now is None or quote_is_fresh(row, now) else None


def exit_realized(legs_exit) -> float:
    """Realized PnL of closing NOW: exit proceeds − entry cost − both fees.

    Shared with the PR-7 shadow path so the two arms of that comparison are priced by one
    formula. `legs_exit` is `[(fill_row, exit_price, depth), ...]` as built below.
    """
    return sum((px - f["price"]) * f["count"]
               - taker_fee(px, f["count"]) - (f["fee_usd"] or 0.0)
               for f, px, _ in legs_exit)


def struct_mid_cost(conn, st) -> float | None:
    """The structure's cost priced at the MIDPOINT — rule 1's reference price, at entry.

    `hold_state` below answers "what is rule 1 looking at?" for a position that already
    exists: it needs `fills` and a `preds` row, neither of which a candidate structure has.
    This answers the same question for a structure we are about to buy, and it is the same
    number by the identity `hold_state` already documents:

        sum_legs[fair(side) - mid(side)] = fair(struct) - [sum_legs mid(side) - 1_bucket]

    so `st.fair - struct_mid_cost(...)` IS the `hold_edge` rule 1 will compute one instant
    later. Returns None on the same conditions `hold_state` returns None — a missing quote
    or a book nobody is making a market in — because those are precisely the states in
    which rule 1 abstains, and a caller must read None as "rule 1 cannot fire", never as
    "rule 1 would fire".
    """
    total = 0.0
    for leg in st.legs:
        q = _quote(conn, leg.ticker)
        if q is None or not two_sided(q["yes_bid"], q["yes_ask"]):
            return None
        mid = (q["yes_bid"] + q["yes_ask"]) / 2.0
        total += mid if leg.side == "yes" else 1.0 - mid
    return total - (1.0 if st.kind == "bucket" else 0.0)


def opens_into_exit(st, mid_cost: float | None) -> bool:
    """#148. Would rule 1 close this structure the moment it is opened?

    The argmax stream enters only when `fair <= cost` (`decide_all.defers_to_market`) and
    puts NO floor under how far below cost the fair may sit, while rule 1 above closes
    anything whose holding edge is under `EXIT_EDGE`. Two rules, one position, opposite
    verdicts on the same cycle — and the exit wins, because it runs after.

    Measured on the live book, all four argmax legs ever placed:

        3100  fair 0.6357  ask 0.77  mid-cost 0.7600  ->  -0.1243   opened 09:12:03.036
                                                                    exited 09:12:03.255
        3161  fair 0.6357  ask 0.76  mid-cost 0.7500  ->  -0.1143   round trip, same tick
        3222  fair 0.6357  ask 0.76  mid-cost 0.7500  ->  -0.1143   round trip, same tick
        3284  fair 0.8999  ask 0.90  mid-cost 0.8800  ->  +0.0199   still open

    Three of four were liquidated 219ms after entry for -$0.07 each on $2.29 staked —
    -9.2%, which is just the round-trip taker cost booked for nothing. The figures above
    are this function's own arithmetic replayed against the quote each row was written on,
    and they reproduce the `hold_edge` the live exits actually recorded to 4dp.

    **Why the fix goes on the ENTRY side.** The other coherent repair is to stop applying
    an edge-reversal exit to a stream whose entry premise is "we have no edge, defer to the
    market" — arguably the deeper incoherence, since argmax never claimed the edge rule 1
    watches for. It is deliberately NOT taken here: it LOOSENS an exit, i.e. it would hold
    positions the book closes today, and nothing yet says that pays. PR-2 (#126) is the
    pre-registered test of whether the defer-to-market thesis makes money at all, and until
    it reads out the conservative direction is the only defensible one. This guard can only
    ever subtract a trade.

    **What it does not do.** It makes the two rules CONSISTENT, not identical. A position
    that drifts past `EXIT_EDGE` tomorrow is still closed tomorrow — that is rule 1 doing
    its job. All this refuses is opening a position already on the wrong side of it.

    No new constant: the threshold is `EXIT_EDGE` itself, read from the module that owns
    it, so the two can never drift back into contradiction.
    """
    return mid_cost is not None and (st.fair - mid_cost) < EXIT_EDGE


def _quote_version(q) -> tuple | None:
    # Include values as well as ts: a same-timestamp correction is a new book too.
    return (tuple(q[k] for k in ("ts", "yes_bid", "yes_ask", "bid_depth", "ask_depth"))
            if q is not None else None)


def _quotes_unchanged(conn, pos, versions: dict, now: datetime) -> bool:
    return all(f["ticker"] in versions
               and _quote_version(_quote(conn, f["ticker"], now)) == versions[f["ticker"]]
               for f in pos["fills"])


@contextmanager
def _write_snapshot(conn):
    """Pin the validated book through the paper writes, without owning mirror I/O.

    execution_lock excludes other ledger passes, but market ingestion intentionally
    does not take it. A SQLite write reservation closes that remaining race. Preserve
    a caller's existing transaction; a stale deferred snapshot must fail its upgrade
    instead of writing from old data. The savepoint rolls back only this exit on error.
    """
    own_transaction = not conn.in_transaction
    try:
        if own_transaction:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("UPDATE quotes SET ts=ts WHERE 0")  # reserve writer, change no rows
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY_SNAPSHOT:
            raise
        # The caller's deferred read snapshot predates an ingestion commit. It
        # cannot be upgraded, and we must not roll back unrelated caller work.
        yield False
        return
    conn.execute("SAVEPOINT macro_exit_write")
    try:
        yield True
    except BaseException:
        conn.execute("ROLLBACK TO macro_exit_write")
        conn.execute("RELEASE macro_exit_write")
        if own_transaction:
            conn.rollback()
        raise
    else:
        conn.execute("RELEASE macro_exit_write")
        if own_transaction:
            conn.commit()


def _write_exit(conn, pos, ts: str, hold_edge, legs_exit, note: str,
                *, quote_versions: dict) -> bool:
    # Earlier positions can spend seconds in their inline mirror HTTP calls. Check
    # the actual write instant, not the clock that began the maintenance cycle.
    with _write_snapshot(conn) as usable:
        if not usable:
            return False
        now = datetime.now(timezone.utc)
        if (not _exit_window_open(conn, pos, now)
                or not _quotes_unchanged(conn, pos, quote_versions, now)):
            return False
        if any(_forced_sell_quote(_quote(conn, f["ticker"], now), f["side"]) is None
               for f in pos["fills"]):
            return False
        ts = now.isoformat()
        fill_ids = _record_exit(conn, pos, ts, hold_edge, legs_exit, note)
    # All legs are recorded against one verified book before any mirror request.
    # run already owns/commits its caller's ledger pass; finish it here as well when
    # a prior caller write started the transaction, so HTTP never holds its writer.
    conn.commit()
    from prediction_market_macro.ops import trading_kalshi
    for fill_id in fill_ids:
        trading_kalshi.on_fill(conn, fill_id)
    return True


def _record_exit(conn, pos, ts: str, hold_edge, legs_exit, note: str) -> list[int]:
    # Stored in inputs_json so the rolling-20 drawdown breaker and reports see
    # exit losses, not just settlement losses.
    #
    # `closes_decision_id` (#149) is the whole point of `pos` being a row rather than a
    # (series, period) pair: this exit retires ONE position, and until it said so the
    # ledger could only be read as "some close happened on this period". Three of the four
    # already-open checks then subtracted it from a count it was never part of.
    realized = exit_realized(legs_exit)
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note,"
        " closes_decision_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, pos["series"], pos["period"], pos["structure_json"], "exit",
         None, None, hold_edge, pos["size_usd"],
         json.dumps({"exit_note": note, "realized_usd": round(realized, 4)}),
         pos["model_version"], "{}",
         f"{note} realized={realized:+.4f}", pos["id"]))
    did = cur.lastrowid
    fill_ids = []
    for f, px, _ in legs_exit:
        curf = conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
            " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (did, ts, f["ticker"], f"close_{f['side']}", px, f["count"],
             taker_fee(px, f["count"])))
        fill_ids.append(curf.lastrowid)
    return fill_ids


def frozen(conn, pos, now: datetime) -> bool:
    """True inside the 10-minute pre-release freeze — no exit of any kind may fire.

    Shared with `shadow_run()`: a shadow arm that "exits" during a window the live book is
    barred from trading would be an unfillable counterfactual.
    """
    spec = REGISTRY.get(pos["series"])
    if spec is None:
        return True                       # unknown series: nothing may act on it
    rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                       (spec.calendar, pos["period"])).fetchone()
    if not rel:
        return False
    dt_min = (datetime.fromisoformat(rel["scheduled_ts"]) - now).total_seconds() / 60
    return 0 <= dt_min <= 10


def hold_state(conn, pos, now: datetime | None = None) -> dict | None:
    """This position's CURRENT structure holding edge and its transactable exit legs.

    Returns `{"hold_edge": float, "legs_exit": [(fill, exit_px, depth), ...]}`, or None
    when the position is **unmeasurable** — no pred, an unpriceable leg, or a leg without
    a two-sided book. None means "hold": it is never a reversal, and no caller may read it
    as one.

    Ladder series price via the latest ladder pmf; CATEGORICAL series (Fed) via the latest
    probs — they were previously never re-evaluated at all (blind spot: the deep-OTM Fed
    lottery legs from the pre-gate era just sat there).
    """
    spec = REGISTRY.get(pos["series"])
    if spec is None:
        return None
    # Shadow predictions must never drive exits, just as they cannot drive entries.
    pr = conn.execute(
        "SELECT asof, ladder_json, dist_json FROM preds WHERE series=? AND period=?"
        " AND model_version LIKE ? ORDER BY asof DESC LIMIT 1",
        (pos["series"], pos["period"], spec.model + "/%")).fetchone()
    if pr is None:
        return None
    if now is not None:
        from prediction_market_macro.ops.decide_all import PRED_STALE_H
        age = (now - datetime.fromisoformat(pr["asof"])).total_seconds() / 3600
        if not 0 <= age <= PRED_STALE_H:
            return None
    pmf = ({float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
           if pr["ladder_json"] else None)
    probs = None
    if pmf is None:
        d0 = json.loads(pr["dist_json"] or "{}")
        probs = d0.get("probs") if isinstance(d0.get("probs"), dict) else None
        if probs is None:
            return None
    from prediction_market_macro.model.common import leg_fair
    hold_edges, legs_exit, quote_versions = [], [], {}
    for f in pos["fills"]:
        c = conn.execute(
            "SELECT floor_strike, cap_strike, strike_type FROM contracts"
            " WHERE ticker=?", (f["ticker"],)).fetchone()
        q = _quote(conn, f["ticker"], now)
        if q is None:
            return None
        quote_versions[f["ticker"]] = _quote_version(q)
        base_side = f["side"].replace("close_", "")
        if probs is not None:                        # categorical leg
            cat = f["ticker"].rsplit("-", 1)[-1]
            fair_yes = float(probs.get(cat, 0.0))
        else:
            if c is None or (c["floor_strike"] is None
                             and c["cap_strike"] is None):
                return None
            # the leg's OWN strike metadata — between buckets and less-type
            # legs priced correctly, not as bare survival(floor)
            try:
                fair_yes = leg_fair(pmf, effective_strike_type(pos["series"], c["strike_type"]),
                                    c["floor_strike"], c["cap_strike"])
            except Exception:                         # noqa: BLE001
                return None
        fair = fair_yes if base_side == "yes" else 1 - fair_yes
        if not two_sided(q["yes_bid"], q["yes_ask"]):
            # Without a competitive book there is no measurable holding edge, and
            # the midpoint that used to be computed here was actively dangerous:
            # KXCPIYOY-26SEP-T3.4 quotes 0.18/0.98, so a mid of 0.58 could show a
            # reversal and liquidate a held position into the 0.18 bid. Default is
            # hold-to-settlement; a leg nobody is making a market in stays held.
            # (The red-light forced exit is deliberately NOT gated on this.)
            return None
        mid = (q["yes_bid"] + q["yes_ask"]) / 2
        mid_side = mid if base_side == "yes" else 1 - mid
        hold_edges.append(fair - mid_side)
        exit_px = (q["yes_bid"] if base_side == "yes" else 1 - q["yes_ask"])
        depth = q["bid_depth"] if base_side == "yes" else q["ask_depth"]
        legs_exit.append((f, max(exit_px - SLIP, 0.01), depth))
    if not hold_edges:
        return None
    # SUM, not min (#141). A position is one STRUCTURE, opened and closed as a
    # package, so its holding edge is the sum of its legs':
    #     e_lo + e_hi = [S(lo) - mid_lo] + [(1-S(hi)) - (1-mid_hi)]
    #                 = [S(lo) - S(hi)] - [mid_lo - mid_hi]
    #                 = fair(bucket) - cost(bucket) at the current mid
    # which is exactly the quantity `decide()` gated on at entry. For a single leg
    # sum == min, so nothing changes there.
    #
    # min() was wrong by construction on spreads: the lo leg of a bucket is bought
    # deep-ITM near $1, so its STANDALONE model edge is negative essentially always,
    # and min() read that as a reversal the same second the position opened. Measured
    # on the ledger: 39 same-cycle open->exit round trips, 36 of them buckets, and all
    # 36 were holding a POSITIVE structure edge when they were liquidated (#3197 today
    # was +0.1399 and paid -$0.27 in fees to close). The other 3 are argmax singles,
    # where sum == min and the exit is the separate #126/#137 conflict, not this bug.
    #
    # Verified NOT a fair disagreement: reconstructing the structure fair from these
    # per-leg `leg_fair` calls reproduces the entry `fair` to 4dp, and strike_type vs
    # spec.strict_gt agrees on all 6,000+ contracts across all 14 series.
    return {"hold_edge": sum(hold_edges), "legs_exit": legs_exit,
            "quote_versions": quote_versions}


def _closed_book(conn, pos, now: datetime) -> bool:
    """Only explicitly active contracts with a verified future close are sellable."""
    if not pos["fills"]:
        return True
    for f in pos["fills"]:
        c = conn.execute("SELECT status, close_time FROM contracts WHERE ticker=?",
                         (f["ticker"],)).fetchone()
        if c is None or c["status"] != "active" or not c["close_time"]:
            return True
        try:
            if datetime.fromisoformat(c["close_time"].replace("Z", "+00:00")) <= now:
                return True
        except (AttributeError, TypeError, ValueError):
            return True
    return False


def _exit_window_open(conn, pos, now: datetime) -> bool:
    """Recheck timing and each quote immediately before recording an exit."""
    return (not frozen(conn, pos, now) and not _closed_book(conn, pos, now)
            and all(_quote(conn, f["ticker"], now) is not None for f in pos["fills"]))


def _forced_sell_quote(q, side: str) -> tuple[float, float] | None:
    """A red light may cross a wide spread, but cannot sell to absent liquidity."""
    if q is None or side not in {"yes", "no"}:
        return None
    try:
        bid = round(float(q["yes_bid"]) if side == "yes" else 1 - float(q["yes_ask"]), 4)
        depth = float(q["bid_depth"] if side == "yes" else q["ask_depth"])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bid) or not math.isfinite(depth) or not SLIP < bid <= 1 or depth <= 0:
        return None
    return max(bid - SLIP, 0.01), depth


@serialized_execution
def run(conn, settings, *, eligible_tickers: set[str] | None = None) -> int:
    from prediction_market_macro.ops import risk
    n = 0
    for pos in open_positions(conn):
        if eligible_tickers is not None and any(
                f["ticker"] not in eligible_tickers for f in pos["fills"]):
            continue
        now = datetime.now(timezone.utc)
        if frozen(conn, pos, now):
            continue
        if _closed_book(conn, pos, now):
            continue

        # rule 2 — red light: breaker tripped ⇒ forced market exit, no edge check
        trip = risk.breaker_tripped(conn, pos["series"])
        if trip:
            legs_exit, quote_versions, ok = [], {}, True
            for f in pos["fills"]:
                q = _quote(conn, f["ticker"], now)
                sell = _forced_sell_quote(q, f["side"])
                if sell is None:
                    ok = False
                    break
                exit_px, depth = sell
                legs_exit.append((f, exit_px, depth))
                quote_versions[f["ticker"]] = _quote_version(q)
            if ok and legs_exit:
                n += int(_write_exit(conn, pos, now.isoformat(), None, legs_exit,
                                     f"health_red_forced_exit [{trip[:120]}]",
                                     quote_versions=quote_versions))
            continue

        # rule 1 — edge reversal against the CURRENT model.
        state = hold_state(conn, pos, now)
        if state is None:
            continue
        hold_edge, legs_exit = state["hold_edge"], state["legs_exit"]
        # rule 3 — regime review: a position that today's structural gates would
        # REFUSE to open (penny-lottery entry) and whose CURRENT model sees no
        # positive holding edge is dead capital — release it while any meaningful
        # value remains (recoverable ≥ 2c/contract; below that fees eat the exit)
        from prediction_market_macro.strategy.decision import GATES as _G
        regime_bad = (any(f["price"] < _G.get("min_leg_price", 0.10)
                          for f in pos["fills"])
                      and hold_edge < 0.0
                      and all(px >= 0.02 for _, px, _ in legs_exit))
        if regime_bad:
            n += int(_write_exit(conn, pos, now.isoformat(), hold_edge, legs_exit,
                                 f"regime_review_exit hold_edge={hold_edge:.4f} (penny-entry,"
                                 f" no current edge)", quote_versions=state["quote_versions"]))
            continue
        if hold_edge >= EXIT_EDGE or any(d < 20 for _, _, d in legs_exit):
            continue
        n += int(_write_exit(conn, pos, now.isoformat(), hold_edge, legs_exit,
                             f"edge_reversal hold_edge={hold_edge:.4f}",
                             quote_versions=state["quote_versions"]))
    conn.commit()
    return n


# ── PR-7 step 1 (#143): S2 shadow ─────────────────────────────────────────────────────

S2_EDGE = 0.0
"""S2's trigger. NOT a fitted constant — it is `EXIT_EDGE` with the -0.06 removed.

PR-7 step 0 found the rally cell rejects in reverse: positions the market has marked UP
give it back (E[y-m] = -0.376, event-clustered CI [-0.637, -0.028]). The registered
response is to close a position the moment its holding edge is gone rather than waiting
for it to go 6c against the model, because at `hold_edge = 0` the position is holding
nothing but fee risk. Registration is `docs/PREREGISTER.md` PR-7 step 1, K=3.

Changing this number re-opens the registration. It may not be swept.
"""


def shadow_run(conn, settings, *, eligible_tickers: set[str] | None = None) -> int:
    """Record what S2 would have done. **Never executes, never touches `decisions`.**

    One row per open position per cycle in `shadow_exits`, carrying the holding edge, the
    transactable exit price, and what closing right there would have realized — priced by
    `exit_realized()`, the live path's own formula.

    Why record instead of reconstructing later: `quotes` does retain history, so a
    reconstruction is not literally impossible. The point is *when the code is written*.
    Every reconstruction choice — which cycle's quote, which pred vintage, what slippage —
    is a researcher degree of freedom, and choosing them after the settlements are known is
    how a pre-registered test quietly becomes a fitted one. Writing the arm down daily,
    before any of it has settled, is what makes PR-7 step 1 a forward test at all.

    Guards mirror rule 1 exactly, because the shadow arm has to be an order that could
    actually have been sent: no trigger inside the freeze window, none on an unmeasurable
    book, none without exit-side depth. A blocked day is still logged with
    `triggered = 0` and the reason in `note`, so "S2 never fired" and "the logger was
    dead" can be told apart.
    """
    n = 0
    for pos in open_positions(conn):
        if eligible_tickers is not None and any(
                f["ticker"] not in eligible_tickers for f in pos["fills"]):
            continue
        now = datetime.now(timezone.utc)
        if frozen(conn, pos, now):
            continue                                   # not sellable ⇒ not shadowable
        if _closed_book(conn, pos, now):
            continue
        state = hold_state(conn, pos, now)
        if state is None:
            continue
        hold_edge, legs_exit = state["hold_edge"], state["legs_exit"]
        thin = any(d < 20 for _, _, d in legs_exit)
        triggered = (hold_edge <= S2_EDGE) and not thin
        note = ("s2_trigger" if triggered
                else "no_depth" if thin
                else "edge_intact")
        with _write_snapshot(conn) as usable:
            if not usable:
                continue
            now = datetime.now(timezone.utc)
            if (not _exit_window_open(conn, pos, now)
                    or not _quotes_unchanged(conn, pos, state["quote_versions"], now)):
                continue
            ts = now.isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO shadow_exits(ts_utc, rule, decision_id, series,"
                " period, hold_edge, triggered, realized_usd, legs_json, note)"
                " VALUES(?,'S2',?,?,?,?,?,?,?,?)",
                (ts, pos["id"], pos["series"], pos["period"], hold_edge, int(triggered),
                 round(exit_realized(legs_exit), 6),
                 json.dumps([{"ticker": f["ticker"], "side": f["side"],
                              "entry_px": f["price"], "count": f["count"],
                              "exit_px": px, "depth": d} for f, px, d in legs_exit]),
                 f"{note} hold_edge={hold_edge:.4f}"))
        n += int(triggered)
    conn.commit()
    return n
