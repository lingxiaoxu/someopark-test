"""ops/ledger.py — the append-only PIT decision ledger (PLAN §12; frozen-ledger discipline).

decisions rows are NEVER updated; every state change is a NEW row (kind: open/exit/pass/
cancel/settle_note). Paper fills are written alongside opens at the depth-aware fill price
(strategy/edge.py::fill_price) with the exact taker fee — the same prices strategy/
decision.py sized the order against.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.strategy.decision import Decision
from prediction_market_macro.strategy.edge import taker_fee



def record(conn, *, series: str, period: str, decision: Decision, pred_inputs: dict,
           model_version: str, note: str = "") -> int:
    now = datetime.now(timezone.utc).isoformat()
    st = decision.struct
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair, ask,"
        " net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, series, period,
         json.dumps({"kind": st.kind, "desc": st.desc,
                     "legs": [{"ticker": l.ticker, "side": l.side, "price": l.price}
                              for l in st.legs]} if st else {}),
         decision.action, st.fair if st else None, st.cost if st else None,
         st.net_edge() if st else None, decision.size_usd,
         json.dumps(pred_inputs, ensure_ascii=False), model_version,
         json.dumps(decision.gate_snapshot), note or ";".join(decision.reasons)))
    did = cur.lastrowid
    if decision.action == "open" and st is not None:
        # one shared fill model with strategy/decision.py, which sized `count` against
        # these same prices — a flat pad here (the old PAPER_SLIP) was invisible to sizing
        # and blew the $1 cap on cheap legs. See strategy/edge.py::fill_price.
        from prediction_market_macro.ops import trading_kalshi
        for leg, px in zip(st.legs, st.fill_prices(decision.count)):
            curf = conn.execute(
                "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count, fee_usd,"
                " mode) VALUES(?,?,?,?,?,?,?, 'paper')",
                (did, now, leg.ticker, leg.side, px, decision.count,
                 taker_fee(px, decision.count)))
            trading_kalshi.on_fill(conn, curf.lastrowid)   # §30.3 inline mirror
    conn.commit()
    return did


OPEN_KINDS = ("open", "argmax", "arb", "snipe")
CLOSE_KINDS = ("exit", "cancel", "settle_note")


def open_decisions(conn, series: str, period: str) -> list[dict]:
    """The rows in this (series, period) that are STILL OPEN, oldest first.

    The single source of truth for "do I already hold this?" — `has_open` below,
    `strategy/arb._has_open_arb`, `strategy/snipe._has_open_snipe` and
    `ops/decide_all._has_any_open` all read it, and #141 is the standing proof of what a
    fifth private re-implementation costs.

    #149. Every one of those checks used to be a pair of SQL SUMs, and three of the four
    counted ONE kind of open against EVERY kind of close. A close row only named a
    (series, period), so an argmax exit decremented the edge stream's counter as well —
    and on KXWTIW 2026-08-07 that flipped `has_open` to False while two edge positions
    were live, which is how decisions 3638 and 3697 became a genuine duplicate position
    15 seconds apart. Closes now carry `closes_decision_id`, so a close retires the ONE
    position it actually closed.

    Rows written before that column existed have it NULL, and those are retired FIFO —
    the reading the old `open_positions` query gave them. That fallback is not a guess:
    replaying the whole live ledger both ways agreed on all 8 open positions and all 66
    periods, so the bridge is provably non-regressive on the history it has to serve.

    Worth stating plainly, because "there is a fallback for legacy rows" understates it:
    as of 2026-08-06 the live book has 119 close rows and **all 119 are NULL** (exit 53,
    cancel 43, settle_note 23; newest is id 3704, the 18:31Z KXNATGASW exit). Every
    `closes_decision_id` writer — `exits._write_exit`, `pnl.settle_note`,
    `retire_stale_book` — sets the column and is verified to do so, but none has written a
    row since the fix landed, so the `cid is not None` branch of `_replay` below is
    exercised only by tests. In production the FIFO branch is currently doing 100% of the
    pairing. That is fine (it is the reading being replaced, and it agrees), but it means
    the linkage is UNPROVEN ON LIVE DATA: the first real close after this note is the one
    to check, and if it too comes out NULL then the fix is not reaching the live path.
    """
    return [dict(r) for r in _replay(conn, series, period)[0]]


def _replay(conn, series: str, period: str) -> tuple[list, dict]:
    """One pass over a period's rows -> (still_open_rows, {open_id: the row that closed it}).

    #150. Both halves of the answer come from ONE replay because they are the same
    question asked twice, and every place that re-asked the second half privately got it
    wrong: `frontend_export` paired "is it closed?" to `settle_note` only (so an exited
    position stayed on the displayed book forever) and pinned every position on a period
    to that period's FIRST settle_note; `pnl.report` and `risk.check_rolling20` deduped
    closures by `(series, period)`, which keeps one closure per PERIOD when the unit that
    closes is a POSITION. #149 made the ledger able to answer this; these three were
    still asking the old way.

    Attribution and the legacy FIFO fallback are identical to `open_decisions` — this is
    that function's body, returning the pairing it was already computing and discarding.
    """
    kinds = OPEN_KINDS + CLOSE_KINDS
    rows = conn.execute(
        f"SELECT * FROM decisions WHERE series=? AND period=? AND kind IN"
        f" ({','.join('?' * len(kinds))}) ORDER BY id",
        (series, period, *kinds)).fetchall()
    stack: list = []
    closed: dict = {}
    for r in rows:
        if r["kind"] in OPEN_KINDS:
            stack.append(r)
        else:
            cid = r["closes_decision_id"]
            if cid is not None:
                # `closed` records the FIRST close to retire an id. A second close naming
                # the same id retires nothing (it is not on the stack), and must not
                # overwrite the pairing either — the pre-#149 ledger re-exited one
                # position up to 6×, and the last of those is not what closed it.
                if any(x["id"] == cid for x in stack):
                    closed[cid] = r
                stack = [x for x in stack if x["id"] != cid]
            elif stack:
                closed[stack.pop(0)["id"]] = r
    return stack, closed


def closures(conn, kinds: tuple[str, ...] = CLOSE_KINDS) -> list[dict]:
    """Every CLOSED position, as {**open_row, "close": close_row}, oldest open first.

    The counterpart to `open_positions`. One entry per position — not per close row and
    not per period (several positions on one period settle separately, which is live right
    now: KXWTIW 2026-08-07 holds 3). `kinds` narrows to a close kind, e.g. ('settle_note',)
    for settled-only.

    Close rows outnumber closed positions: 119 rows retire 103 positions. Every `exit`
    (53) and `cancel` (43) pairs to a distinct open, but only 7 of the 23 `settle_note`
    rows do — the other 16 are the pre-#149 settle_pass re-settling positions it had
    already finished. Summing settle_note ROWS gives -6.07; summing settled POSITIONS
    gives -4.37, and the second is the one that happened.
    """
    keys = conn.execute(
        "SELECT DISTINCT series, period FROM decisions WHERE kind IN"
        f" ({','.join('?' * len(OPEN_KINDS))})", OPEN_KINDS).fetchall()
    out = []
    for k in keys:
        for oid, close in _replay(conn, k["series"], k["period"])[1].items():
            if close["kind"] not in kinds:
                continue
            o = conn.execute("SELECT * FROM decisions WHERE id=?", (oid,)).fetchone()
            out.append({**dict(o), "close": dict(close)})
    return sorted(out, key=lambda d: d["id"])


def realized_usd(close: dict) -> float | None:
    """The money a close row booked, or None if that row never recorded it.

    None is NOT zero and callers must not coerce it: 42 of the 53 live exits predate the
    field (40 carry only `{"hold_edges": [...]}`, 2 only `{"exit_note": ...}`; just 11
    carry a figure), so a `.get(..., 0.0)` here
    reports a clean $0.00 for a position that in fact lost money — the exact shape of the
    "KXPCECORE 13 round trips, realized +0.00" mistake recorded in §25.20(b).

    `settle_note` writes the figure into `size_usd` as well, and all 23 live rows agree
    with their own `inputs_json` to the cent; `inputs_json` is read first regardless, so
    the two can never silently diverge.
    """
    try:
        v = json.loads(close["inputs_json"] or "{}").get("realized_usd")
    except json.JSONDecodeError:
        v = None
    if v is None and close["kind"] == "settle_note":
        v = close["size_usd"]
    return None if v is None else float(v)


def has_open(conn, series: str, period: str) -> bool:
    """Is an EDGE-stream position (kind='open') live on this (series, period)?

    A settled position is CLOSED, exactly as `open_positions` already treats it. An
    earlier fix here was about the opposite direction — this omitted 'settle_note' while
    its three siblings counted it, so a period whose position had settled still read as
    held and every later day passed with `already_open_no_averaging_down`, locking the
    slot for the life of that period. Both readings now come from `open_decisions`, so
    neither can drift back.
    """
    return any(d["kind"] == "open" for d in open_decisions(conn, series, period))


def open_positions(conn) -> list[dict]:
    """Every still-open decision on the book, with its paper fills.

    settle_note counts as closed — otherwise settle_pass re-settles (and marks keep
    marking) finished positions forever.
    """
    keys = conn.execute(
        "SELECT DISTINCT series, period FROM decisions WHERE kind IN"
        f" ({','.join('?' * len(OPEN_KINDS))})", OPEN_KINDS).fetchall()
    out = []
    for k in keys:
        for d in open_decisions(conn, k["series"], k["period"]):
            fills = conn.execute("SELECT * FROM fills WHERE decision_id=?",
                                 (d["id"],)).fetchall()
            out.append({**d, "fills": [dict(f) for f in fills]})
    # id order, which is what the single-query version returned. Callers iterate this to
    # WRITE exits and settle_notes, so the order decides the ids those rows get; grouping
    # by period would have quietly reshuffled the ledger for no reason.
    return sorted(out, key=lambda d: d["id"])
