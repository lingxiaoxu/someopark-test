"""ops/trading_kalshi.py — §30 demo-account mirror executor (user directive 2026-08-18).

The paper ledger IS the inventory; this module is its ×100 shadow on the Kalshi DEMO
account. It NEVER makes a trading decision — no model, no gates of its own, no timing.
"The ledger did it" is the only signal: every paper fill (open leg, exit leg, arb leg,
snipe leg) becomes exactly one demo order, buys for opens, sells for `close_*` fills.

Modes:
  DARK (armed=False, the build state until PR-9 fires): the FULL pipeline runs on every
  paper fill — gates, sizing, buying-power guard, prod-ask re-fetch for the latency
  component — and writes a `status='dryrun'` row instead of POSTing. Flipping `arm()`
  is the only difference between rehearsal and live demo trading; nothing else changes.
  ARMED: real orders through exec/kalshi_exec.place_taker_order (taker with a hard
  price cap — naked market orders are impossible by construction, §30.3).

Latency (§30.3): on_fill() is called INLINE by every fills-writing site (five doors:
ledger.record / decide_all argmax / exits / arb / snipe — tests/test_trading_kalshi
pins that every `INSERT INTO fills` in production code is followed by the hook).
sync() is the catch-up backstop for crashes, not the primary path.

Accounting (§30.4): one identity, asserted from two independent sources on every
snapshot — equity = cash + Σposition MTM;
cash = start_cash + Σtransfers + Σrealized − Σfees − Σopen cost − reserved.
Σtransfers is the deposits/withdrawals ledger (`demo_transfers`), added after the
2026-08-20 top-up showed the identity had no way to say "capital moved" — a benign
deposit after arming would have halted the mirror as unexplained drift. Transfers
enter ONLY through `record_transfer` (manual, note mandatory): the identity's job is
to halt on unexplained movement, so nothing may explain drift away automatically.
LHS from the exchange API, RHS derived from demo_fills + settlements. Disagreement
beyond DRIFT_TOL_USD is a HALT (mirror stops, alert, human ack) — never "pick a side".
MTM marks come from the PRODUCTION book (`quotes`); the demo book is thin and disjoint
and never enters equity.

Gate structure (§30.2, user: Brier series_gate is PAUSED for this path):
  ① settings.trading_enabled ∧ KALSHI_TRADING_ENABLED=1   (armed sends only)
  ② the paper ledger itself + per-series ops kill-switch (`series_off:{series}`)
  ③ circuit_breaker clean 7d ∧ no un-acked mirror halt
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

MIRROR_MULT = 100                 # paper $0.42 -> demo $42 (user-given constant)
ORDER_TIMEOUT_MIN = 15            # non-terminal after this -> cancel + 'unfilled'
DRIFT_TOL_USD = 1.00              # balance identity tolerance before HALT
PRODASK_TIMEOUT_S = 4             # latency-component re-fetch budget (single try)


# ── state (demo_mirror_state k/v) ────────────────────────────────────────────

def _get_state(conn, k: str) -> str | None:
    r = conn.execute("SELECT v FROM demo_mirror_state WHERE k=?", (k,)).fetchone()
    return None if r is None else r["v"]


def _set_state(conn, k: str, v: str) -> None:
    conn.execute("INSERT OR REPLACE INTO demo_mirror_state VALUES(?,?)", (k, v))
    conn.commit()


def armed(conn) -> bool:
    return _get_state(conn, "armed") == "1"


def arm(conn, start_cash: float | None = None) -> dict:
    """Flip to live demo trading. Watermark = current MAX(fills.id): pre-arming paper
    fills are never retro-mirrored (their exits no-op via the held-count clamp).
    start_cash is read from the exchange unless given (tests)."""
    now = datetime.now(timezone.utc).isoformat()
    if start_cash is None:
        from prediction_market_macro.venues.kalshi.account import fetch_balance_usd
        start_cash = fetch_balance_usd()
    wm = conn.execute("SELECT COALESCE(MAX(id),0) FROM fills").fetchone()[0]
    _set_state(conn, "watermark", str(wm))
    _set_state(conn, "start_cash", f"{start_cash:.2f}")
    _set_state(conn, "armed_ts", now)
    _set_state(conn, "armed", "1")
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now, "warn", "trading_kalshi",
                  f"ARMED: mirror live, watermark fill_id={wm}, start_cash=${start_cash:.2f}"))
    conn.commit()
    return {"watermark": wm, "start_cash": start_cash, "armed_ts": now}


def halt(conn, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _set_state(conn, "halt", json.dumps({"reason": reason, "ts": now}))
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now, "error", "trading_kalshi", f"MIRROR HALT: {reason}"))
    conn.commit()


def halted(conn) -> str | None:
    v = _get_state(conn, "halt")
    return None if v is None else json.loads(v).get("reason")


def ack_halt(conn) -> None:
    conn.execute("DELETE FROM demo_mirror_state WHERE k='halt'")
    conn.commit()


def _watermark(conn, live_fill_id: int | None = None) -> int:
    """Rehearsal starts at first deploy, not at the beginning of history. First
    initialization depends on who asks: the sweep excludes everything already in the
    ledger (MAX(id)); the INLINE hook is by construction handling a brand-new fill,
    so it initializes to fill_id−1 — otherwise the very first post-deploy fill would
    set the mark on itself and be swallowed."""
    v = _get_state(conn, "watermark")
    if v is None:
        if live_fill_id is not None:
            wm = live_fill_id - 1
        else:
            wm = conn.execute("SELECT COALESCE(MAX(id),0) FROM fills").fetchone()[0]
        _set_state(conn, "watermark", str(wm))
        return wm
    return int(v)


def _start_cash(conn) -> float:
    v = _get_state(conn, "start_cash")
    if v is not None:
        return float(v)
    from prediction_market_macro.venues.kalshi.account import current_bankroll
    return current_bankroll(conn)


def _transfers_net(conn) -> float:
    """Σdeposits − Σwithdrawals since arming — the §30.4 identity's missing term.

    The 2026-08-20 top-up ($492.65 → $2,700) exposed this: the identity had no way to
    say "capital moved", so a benign deposit after arming would have read as
    unexplained drift and halted the mirror. The fix is a LEDGER, not a start_cash
    rebase: rebasing conflates "capital injected" with "PnL earned", and every
    downstream figure — the equity-vs-start curve, the buying-power guard, the drift
    check — needs those separated to mean anything.
    """
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='deposit' THEN amount_usd"
        " ELSE -amount_usd END), 0) FROM demo_transfers").fetchone()
    return round(float(r[0]), 2)


def record_transfer(conn, amount_usd: float, kind: str, note: str,
                    ts: str | None = None) -> dict:
    """The ONLY way a cash movement enters the identity. Deliberately manual.

    Auto-classifying drift as a deposit would delete the reason the identity exists —
    "unexplained cash movement halts the mirror" stops meaning anything if the mirror
    explains its own drift away. So the flow on a benign transfer is: snapshot halts →
    a human recognizes the deposit → this function records it with a mandatory note →
    the next snapshot's identity heals → `ack_halt` is pressed with the ledger row as
    its paper trail. Every step leaves a record; none is skippable (the plan's
    "两者都要留痕,不能靠 ack_halt 一按了事").
    """
    if kind not in ("deposit", "withdrawal"):
        raise ValueError(f"kind must be deposit|withdrawal, got {kind!r}")
    if not (amount_usd > 0):
        raise ValueError(f"amount_usd must be positive (kind carries the sign),"
                         f" got {amount_usd}")
    if not note or not note.strip():
        raise ValueError("note is mandatory — a transfer with no reason on record is "
                         "exactly the unexplained movement the identity exists to catch")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO demo_transfers(ts, amount_usd, kind, note, recorded_ts)"
        " VALUES(?,?,?,?,?)", (ts or now, round(amount_usd, 2), kind, note.strip(), now))
    conn.execute(
        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
        (now, "warn", "trading_kalshi",
         f"TRANSFER recorded: {kind} ${amount_usd:.2f} ({note.strip()}) — "
         f"net transfers now ${_transfers_net(conn):.2f}"))
    conn.commit()
    return {"kind": kind, "amount_usd": round(amount_usd, 2),
            "transfers_net": _transfers_net(conn)}


# ── gates (§30.2: ledger-driven; Brier series_gate PAUSED for this path) ─────

def _mirror_gate(conn, settings, series: str) -> str | None:
    """None = green. Only consulted for REAL sends; dark mode always rehearses."""
    if not getattr(settings, "trading_enabled", False):
        return "settings.trading_enabled=False"
    if os.environ.get("KALSHI_TRADING_ENABLED") != "1":
        return "env KALSHI_TRADING_ENABLED!=1"
    if _get_state(conn, f"series_off:{series}") == "1":
        return f"kill-switch series_off:{series}"
    h = halted(conn)
    if h:
        return f"mirror_halt:{h}"
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cb = conn.execute("SELECT COUNT(*) c FROM alerts WHERE source='circuit_breaker'"
                      " AND ts>=?", (week_ago,)).fetchone()
    if cb["c"] > 0:
        return "circuit_breaker within 7d"
    return None


# ── positions & balance sheet (§30.4) ────────────────────────────────────────

def demo_positions(conn) -> list[dict]:
    """Open demo positions = Σ demo_fills per (ticker, side), buys − sells, minus
    settled tickers. The exchange positions endpoint only RECONCILES this; it is
    never a second book of record."""
    rows = conn.execute(
        "SELECT f.ticker, f.side,"
        " SUM(CASE WHEN f.action='buy' THEN f.count ELSE -f.count END) AS net,"
        " SUM(CASE WHEN f.action='buy' THEN f.price*f.count ELSE 0 END) AS buy_cost,"
        " SUM(CASE WHEN f.action='buy' THEN f.count ELSE 0 END) AS buy_n"
        " FROM demo_fills f"
        " WHERE f.ticker NOT IN (SELECT ticker FROM settlements)"
        " GROUP BY f.ticker, f.side HAVING net > 0").fetchall()
    out = []
    for r in rows:
        mark = _prod_mark(conn, r["ticker"], r["side"])
        avg_cost = r["buy_cost"] / r["buy_n"] if r["buy_n"] else 0.0
        out.append({"ticker": r["ticker"], "side": r["side"], "count": r["net"],
                    "avg_cost": round(avg_cost, 4), "mark": mark,
                    "cost": round(avg_cost * r["net"], 2),
                    "mtm": round((mark if mark is not None else avg_cost) * r["net"], 2)})
    return out


def _prod_mark(conn, ticker: str, side: str) -> float | None:
    """PRODUCTION-book mid for MTM (same source as paper marks). Demo-book marks
    never enter equity (§30.4 口径 1)."""
    r = conn.execute("SELECT yes_bid, yes_ask FROM quotes WHERE ticker=?"
                     " ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
    if r is None or r["yes_bid"] is None or r["yes_ask"] is None:
        return None
    mid = (r["yes_bid"] + r["yes_ask"]) / 2.0
    return round(mid if side == "yes" else 1.0 - mid, 4)


def _realized_and_fees(conn) -> tuple[float, float]:
    """Cumulative realized (sell proceeds − their cost basis + settlement payouts −
    settled cost) and cumulative fees, derived purely from demo_fills + settlements."""
    fees = conn.execute("SELECT COALESCE(SUM(fee_usd),0) FROM demo_fills").fetchone()[0]
    realized = 0.0
    # settled tickers: payout count×$1 if side matches result, minus cost basis
    for r in conn.execute(
            "SELECT f.ticker, f.side,"
            " SUM(CASE WHEN f.action='buy' THEN f.count ELSE -f.count END) AS net,"
            " SUM(CASE WHEN f.action='buy' THEN f.price*f.count"
            "     ELSE -f.price*f.count END) AS net_cost, s.result"
            " FROM demo_fills f JOIN settlements s ON s.ticker=f.ticker"
            " GROUP BY f.ticker, f.side").fetchall():
        payout = float(r["net"]) if (r["result"] == r["side"]) else 0.0
        realized += payout - float(r["net_cost"])
    # sold-before-settle legs on unsettled tickers: proceeds − proportional cost
    for r in conn.execute(
            "SELECT ticker, side,"
            " SUM(CASE WHEN action='sell' THEN price*count ELSE 0 END) AS sell_amt,"
            " SUM(CASE WHEN action='sell' THEN count ELSE 0 END) AS sell_n,"
            " SUM(CASE WHEN action='buy' THEN price*count ELSE 0 END) AS buy_cost,"
            " SUM(CASE WHEN action='buy' THEN count ELSE 0 END) AS buy_n"
            " FROM demo_fills WHERE ticker NOT IN (SELECT ticker FROM settlements)"
            " GROUP BY ticker, side HAVING sell_n > 0").fetchall():
        avg_cost = r["buy_cost"] / r["buy_n"] if r["buy_n"] else 0.0
        realized += float(r["sell_amt"]) - avg_cost * float(r["sell_n"])
    return round(realized, 4), round(float(fees), 4)


def _reserved(conn) -> float:
    """Buying power held by in-flight (non-terminal) orders — part of the identity,
    so two mirrors in one tick cannot double-spend (§30.4 口径 2)."""
    r = conn.execute(
        "SELECT COALESCE(SUM((count_target-count_filled)*paper_ask),0) FROM demo_orders"
        " WHERE status IN ('intent','sent','partial') AND action='buy'").fetchone()
    return round(float(r[0]), 2)


def snapshot_balance_sheet(conn, cash_exchange: float | None = None) -> dict:
    """One identity, two sources; drift beyond tolerance HALTS the mirror."""
    now = datetime.now(timezone.utc).isoformat()
    pos = demo_positions(conn)
    realized, fees = _realized_and_fees(conn)
    reserved = _reserved(conn)
    transfers = _transfers_net(conn)
    positions_cost = round(sum(p["cost"] for p in pos), 2)
    positions_mtm = round(sum(p["mtm"] for p in pos), 2)
    cash_expected = round(_start_cash(conn) + transfers + realized - fees
                          - positions_cost - reserved, 2)
    if cash_exchange is None:
        if armed(conn):
            from prediction_market_macro.venues.kalshi.account import fetch_balance_usd
            cash_exchange = fetch_balance_usd()
        else:
            cash_exchange = cash_expected      # dark: no exchange writes -> identical
    drift = round(abs(cash_exchange - cash_expected), 2)
    row = {"ts": now, "cash_exchange": round(cash_exchange, 2),
           "cash_expected": cash_expected, "reserved_usd": reserved,
           "positions_cost": positions_cost, "positions_mtm": positions_mtm,
           "equity": round(cash_exchange + positions_mtm, 2),
           "realized_cum": realized, "fees_cum": fees,
           "n_open_positions": len(pos),
           "exposure_json": json.dumps(_exposure(pos)), "drift_usd": drift,
           "transfers_cum": transfers}
    conn.execute(
        "INSERT OR REPLACE INTO demo_balance_sheet(ts, cash_exchange, cash_expected,"
        " reserved_usd, positions_cost, positions_mtm, equity, realized_cum, fees_cum,"
        " n_open_positions, exposure_json, drift_usd, transfers_cum) VALUES"
        " (:ts,:cash_exchange,:cash_expected,:reserved_usd,:positions_cost,"
        "  :positions_mtm,:equity,:realized_cum,:fees_cum,:n_open_positions,"
        "  :exposure_json,:drift_usd,:transfers_cum)", row)
    conn.commit()
    if armed(conn) and drift > DRIFT_TOL_USD:
        halt(conn, f"balance drift ${drift:.2f} > ${DRIFT_TOL_USD:.2f}"
                   f" (exchange {cash_exchange:.2f} vs expected {cash_expected:.2f})")
    return row


def _exposure(pos: list[dict]) -> dict:
    out: dict[str, float] = {}
    for p in pos:
        series = p["ticker"].split("-", 1)[0]
        out[series] = round(out.get(series, 0.0) + p["cost"], 2)
    return out


# ── the mirror itself ────────────────────────────────────────────────────────

def _prod_ask(ticker: str) -> float | None:
    """Latency-component re-fetch: production ask at send time. SINGLE try, short
    timeout — kalshi_md's paced 9-retry client would blow the inline budget."""
    url = (f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
           f"/orderbook?depth=1")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "someopark-macro"})
        with urllib.request.urlopen(req, timeout=PRODASK_TIMEOUT_S) as r:
            fp = (json.load(r).get("orderbook_fp") or {})
        no = [(float(p), float(s)) for p, s in (fp.get("no_dollars") or [])]
        return round(1.0 - max(no, key=lambda x: x[0])[0], 4) if no else None
    except Exception:                                            # noqa: BLE001
        return None


def _held(conn, ticker: str, side: str) -> int:
    r = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN action='buy' THEN count ELSE -count END),0)"
        " FROM demo_fills WHERE ticker=? AND side=?", (ticker, side)).fetchone()
    return int(r[0])


def on_fill(conn, fill_id: int) -> None:
    """INLINE hook — the very next call after a paper fill lands (§30.3). Best-effort:
    NEVER raises into the trading task; failures alert and the sync() backstop retries."""
    try:
        _mirror_one(conn, fill_id)
    except Exception as e:                                       # noqa: BLE001
        try:
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), "warn",
                          "trading_kalshi", f"on_fill({fill_id}) failed: {e}"))
            conn.commit()
        except Exception:                                        # noqa: BLE001
            pass


def sync(conn) -> dict:
    """Catch-up backstop + order poller + snapshot. Safe to call every tick."""
    n = 0
    for r in conn.execute(
            "SELECT f.id FROM fills f LEFT JOIN demo_orders d ON d.fill_id=f.id"
            " WHERE d.fill_id IS NULL AND f.id>? AND f.mode='paper' ORDER BY f.id",
            (_watermark(conn),)).fetchall():
        try:
            _mirror_one(conn, r["id"])
            n += 1
        except Exception as e:                                   # noqa: BLE001
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (datetime.now(timezone.utc).isoformat(), "warn",
                          "trading_kalshi", f"sync mirror fill={r['id']}: {e}"))
            conn.commit()
    polled = poll_open_orders(conn)
    snap = snapshot_balance_sheet(conn)
    return {"mirrored": n, "polled": polled, "drift": snap["drift_usd"]}


def _mirror_one(conn, fill_id: int) -> None:
    f = conn.execute("SELECT * FROM fills WHERE id=?", (fill_id,)).fetchone()
    if f is None or f["mode"] != "paper" or f["id"] <= _watermark(conn, fill_id):
        return
    if conn.execute("SELECT 1 FROM demo_orders WHERE fill_id=?", (fill_id,)).fetchone():
        return                                                   # idempotent
    raw_side = f["side"]
    action = "sell" if raw_side.startswith("close_") else "buy"
    side = raw_side.removeprefix("close_")
    ticker, series = f["ticker"], f["ticker"].split("-", 1)[0]
    now = datetime.now(timezone.utc).isoformat()
    coid = f"spm-m{fill_id}"
    target = MIRROR_MULT * int(f["count"])
    base = {"fill_id": fill_id, "decision_id": f["decision_id"],
            "client_order_id": coid, "ticker": ticker, "side": side,
            "action": action, "count_target": target, "count_filled": 0,
            "paper_ask": float(f["price"]), "prod_ask_at_send": None,
            "avg_price": None, "fee_usd": None, "status": "dryrun",
            "order_id": None, "ts_sent": now, "ts_terminal": None, "note": None}

    def write(row):
        conn.execute(
            "INSERT INTO demo_orders VALUES(:fill_id,:decision_id,:client_order_id,"
            " :ticker,:side,:action,:count_target,:count_filled,:paper_ask,"
            " :prod_ask_at_send,:avg_price,:fee_usd,:status,:order_id,:ts_sent,"
            " :ts_terminal,:note)", row)
        conn.commit()

    if action == "sell":
        held = _held(conn, ticker, side)
        count = min(target, held)
        if count <= 0:
            # pre-arming opens are never mirrored, so their exits legitimately no-op
            base.update(status="skipped_noheld",
                        note=f"paper close x{target} but demo holds 0")
            write(base)
            return
        base["count_target"] = count
    is_armed = armed(conn)
    base["prod_ask_at_send"] = _prod_ask(ticker)                 # latency component
    if not is_armed:
        est_px = base["prod_ask_at_send"] or base["paper_ask"]
        from prediction_market_macro.strategy.edge import taker_fee
        base.update(status="dryrun", avg_price=None,
                    fee_usd=round(taker_fee(est_px, base["count_target"]), 2),
                    note=f"dark rehearsal; would {action} x{base['count_target']}")
        write(base)
        return

    from prediction_market_macro.config.settings import load_settings
    s = load_settings()
    why = _mirror_gate(conn, s, series)
    if why:
        base.update(status="skipped_halt" if "halt" in why else "skipped_gate", note=why)
        write(base)
        return
    if action == "buy":
        # buying-power guard reads cash_expected − reserved (never the raw balance);
        # transfers included, or a post-arm deposit would exist at the exchange but be
        # invisible to sizing while a withdrawal would let the mirror spend money that left
        realized, fees = _realized_and_fees(conn)
        pos_cost = sum(p["cost"] for p in demo_positions(conn))
        power = (_start_cash(conn) + _transfers_net(conn) + realized - fees
                 - pos_cost - _reserved(conn))
        affordable = int(power / max(base["paper_ask"], 0.01))
        if affordable <= 0:
            base.update(status="skipped_power", note=f"power ${power:.2f} affords 0")
            write(base)
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now, "warn", "trading_kalshi",
                          f"buying power exhausted: {ticker} target x{base['count_target']}"))
            conn.commit()
            return
        if affordable < base["count_target"]:
            base["note"] = f"power-scaled {base['count_target']}->{affordable}"
            base["count_target"] = affordable
    base["status"] = "intent"
    write(base)                                                  # intent BEFORE send
    from prediction_market_macro.exec.kalshi_exec import place_taker_order
    mode = _get_state(conn, "taker_mode") or "market_capped"     # arming canary sets
    res = place_taker_order(
        conn, ticker=ticker, side=side, action=action, count=base["count_target"],
        ref_price_cents=max(1, min(99, round(base["paper_ask"] * 100))),
        mode=mode, client_order_id=coid)
    if res.status == "accepted":
        conn.execute("UPDATE demo_orders SET status='sent', order_id=? WHERE fill_id=?",
                     (res.order_id, fill_id))
    else:
        conn.execute("UPDATE demo_orders SET status='unfilled', ts_terminal=?,"
                     " note=? WHERE fill_id=?", (now, f"send error: {res.reason}", fill_id))
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now, "error", "trading_kalshi",
                      f"order send failed {ticker}: {res.reason}"))
    conn.commit()


def poll_open_orders(conn) -> int:
    """Advance non-terminal orders to terminal state; write actual executions to
    demo_fills; cancel at timeout. Unparseable exchange payloads HALT (loud, not
    silent) — the arming-day canary is what proves the parser against reality."""
    if not armed(conn):
        return 0
    from prediction_market_macro.exec.kalshi_exec import cancel_order
    from prediction_market_macro.venues.kalshi.account import fetch_fills, fetch_order
    n = 0
    now = datetime.now(timezone.utc)
    for o in conn.execute("SELECT * FROM demo_orders WHERE status IN ('sent','partial')"
                          " AND order_id IS NOT NULL").fetchall():
        try:
            od = fetch_order(o["order_id"])
            filled = int(od.get("filled_count") or od.get("taker_fill_count") or 0)
            status = str(od.get("status") or "")
        except Exception as e:                                   # noqa: BLE001
            halt(conn, f"order poll unparseable for {o['order_id']}: {e}")
            return n
        if filled > o["count_filled"]:
            _adopt_fills(conn, o)
        terminal = status in ("executed", "canceled", "cancelled")
        aged = (now - datetime.fromisoformat(o["ts_sent"])).total_seconds() \
            > ORDER_TIMEOUT_MIN * 60
        if terminal or aged:
            if aged and not terminal:
                cancel_order(conn, o["order_id"])
            final = "filled" if filled >= o["count_target"] else \
                ("partial" if filled > 0 else "unfilled")
            conn.execute("UPDATE demo_orders SET status=?, ts_terminal=? WHERE fill_id=?",
                         (final, now.isoformat(), o["fill_id"]))
            conn.commit()
            n += 1
    return n


def _adopt_fills(conn, o) -> None:
    """Pull actual executions for one order from the exchange fills feed."""
    from prediction_market_macro.venues.kalshi.account import fetch_fills
    got = 0.0
    cnt = 0
    for fl in fetch_fills(ticker=o["ticker"]):
        if fl.get("order_id") != o["order_id"]:
            continue
        xid = fl.get("fill_id") or fl.get("trade_id")
        price_c = fl.get("yes_price") if o["side"] == "yes" else fl.get("no_price")
        if xid is None or price_c is None:
            halt(conn, f"fill payload unparseable for order {o['order_id']}: {fl}")
            return
        px = float(price_c) / 100.0
        c = int(fl.get("count") or 0)
        from prediction_market_macro.strategy.edge import taker_fee
        fee = float(fl.get("fee") or fl.get("taker_fee") or taker_fee(px, c))
        conn.execute(
            "INSERT OR IGNORE INTO demo_fills(fill_id, ticker, side, action, price,"
            " count, fee_usd, exchange_fill_id, ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (o["fill_id"], o["ticker"], o["side"], o["action"], px, c, fee, str(xid),
             fl.get("created_time") or datetime.now(timezone.utc).isoformat()))
        got += px * c
        cnt += c
    if cnt:
        conn.execute(
            "UPDATE demo_orders SET count_filled=(SELECT COALESCE(SUM(count),0)"
            " FROM demo_fills WHERE fill_id=?), avg_price=?, fee_usd=(SELECT"
            " COALESCE(SUM(fee_usd),0) FROM demo_fills WHERE fill_id=?)"
            " WHERE fill_id=?",
            (o["fill_id"], round(got / cnt, 4), o["fill_id"], o["fill_id"]))
        conn.commit()


def reconcile(conn) -> dict:
    """Daily: exchange positions vs Σdemo_fills derivation. Mismatch = HALT (§30.2-2)."""
    if not armed(conn):
        return {"skipped": "dark"}
    from prediction_market_macro.venues.kalshi.account import fetch_positions
    ours = {(p["ticker"], p["side"]): p["count"] for p in demo_positions(conn)}
    theirs: dict[tuple, int] = {}
    for mp in fetch_positions():
        t = mp.get("ticker")
        pos = int(mp.get("position") or 0)
        if pos > 0:
            theirs[(t, "yes")] = pos
        elif pos < 0:
            theirs[(t, "no")] = -pos
    diff = {k: (ours.get(k, 0), theirs.get(k, 0))
            for k in set(ours) | set(theirs) if ours.get(k, 0) != theirs.get(k, 0)}
    if diff:
        halt(conn, f"position reconciliation mismatch: {diff}")
    return {"ok": not diff, "n_ours": len(ours), "n_theirs": len(theirs),
            "diff": {f"{k[0]}/{k[1]}": v for k, v in diff.items()}}
