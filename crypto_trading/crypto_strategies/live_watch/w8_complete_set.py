"""W8 independent, read-only Kalshi observation daemon.

    python -m crypto_trading.crypto_strategies.live_watch.w8_complete_set --loop 2

Parallel to W7: event_binary/complete_set.py is the shared research kernel;
this module owns only w8_complete_set_state.json and its own compressed tape.
It never calls the execution router, demo mirror, or an authenticated write.
Observed public prints drive conservative queue fills. They are hypothetical,
not receipts of trades made by this account. Restarts do not fill stale orders.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import time

import requests

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.kalshi.enums import rest_base
from crypto_trading.crypto_strategies.event_binary import complete_set as kernel
from . import common, w7_noisefade

NAME = "w8_complete_set"
SERIES = ("KXBTC15M", "KXETH15M")
TAPE_ROOT = PRICE_DATA / "kalshi" / NAME / "prod"
logger = logging.getLogger(__name__)
_HTTP_RESUME_TS = 0.0
_HTTP_RATE_STREAK = 0
_HTTP_MIN_INTERVAL = 1.0
_HTTP_NEXT_REQUEST_TS = 0.0
_HTTP_LAST_RATE_TS = 0.0
_HTTP_COUNTS = {"requests": 0, "rate_limited": 0}
_LOADED_SOURCES = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (Path(__file__), Path(kernel.__file__))}


class ReadRateLimited(RuntimeError):
    pass


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()


def timestamp(value) -> float:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def read_get(path: str, params: dict | None = None) -> dict:
    """Public GET-only boundary. No credentials or execution router imported."""
    global _HTTP_RESUME_TS, _HTTP_RATE_STREAK, _HTTP_MIN_INTERVAL
    global _HTTP_NEXT_REQUEST_TS, _HTTP_LAST_RATE_TS
    if time.time() < _HTTP_RESUME_TS:
        raise ReadRateLimited(f"public API cooldown until {iso(_HTTP_RESUME_TS)}")
    # Anonymous reads do not inherit an authenticated account's request budget.
    # Pace every page as well as book reads, and retain the reduced rate after
    # a 429; a single successful response must not restore the overload.
    wait = _HTTP_NEXT_REQUEST_TS-time.time()
    if wait > 0:
        time.sleep(wait)
    _HTTP_NEXT_REQUEST_TS = time.time()+_HTTP_MIN_INTERVAL
    _HTTP_COUNTS["requests"] += 1
    response = requests.get(rest_base("prod")+path, params=params, timeout=8,
                            headers={"User-Agent": "someopark-w8-observer/1"})
    if response.status_code == 429:
        if time.time()-_HTTP_LAST_RATE_TS > 600:
            _HTTP_RATE_STREAK = 0
        _HTTP_RATE_STREAK += 1
        _HTTP_LAST_RATE_TS = time.time()
        _HTTP_MIN_INTERVAL = min(4., _HTTP_MIN_INTERVAL*1.5)
        _HTTP_COUNTS["rate_limited"] += 1
        try:
            requested = float(response.headers.get("Retry-After", "0"))
        except ValueError:
            requested = 0.
        delay = max(requested, min(300., 60.*2**min(_HTTP_RATE_STREAK-1, 3)))
        _HTTP_RESUME_TS = time.time()+delay
        raise ReadRateLimited(f"public API 429; cooldown {delay:.0f}s")
    response.raise_for_status()
    return response.json()


def normalize_trade(t: dict) -> dict | None:
    try:
        result = dict(trade_id=str(t["trade_id"]), ticker=t["ticker"],
                      ts=timestamp(t["created_time"]),
                      yes_price=float(t["yes_price_dollars"]),
                      quantity=float(t["count_fp"]),
                      taker_side=t.get("taker_outcome_side", t.get("taker_side")),
                      is_block_trade=bool(t.get("is_block_trade", False)))
        if not (0 < result["yes_price"] < 1 and result["quantity"] > 0):
            return None
        return result
    except (ValueError, TypeError, KeyError):
        return None


def fetch_trades(ticker: str, since: float, until: float) -> tuple[list, bool]:
    """Paginate backwards over this interval; fail closed if coverage is capped."""
    params = dict(ticker=ticker, limit=1000, min_ts=int(since)-1, max_ts=int(until)+1)
    cursor, rows, ids = None, [], set()
    for _ in range(8):
        data = read_get("/markets/trades", {**params, **({"cursor": cursor} if cursor else {})})
        batch = data.get("trades", [])
        normalized = [r for t in batch if (r := normalize_trade(t)) is not None]
        for row in normalized:
            if row["trade_id"] not in ids and since-1 <= row["ts"] <= until:
                ids.add(row["trade_id"])
                rows.append(row)
        cursor = data.get("cursor")
        if not cursor or not batch or (normalized and min(r["ts"] for r in normalized) < since-1):
            return sorted(rows, key=lambda r: (r["ts"], r["trade_id"])), True
    return sorted(rows, key=lambda r: (r["ts"], r["trade_id"])), False


def append_tape(payload: dict) -> None:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    TAPE_ROOT.mkdir(parents=True, exist_ok=True)
    with gzip.open(TAPE_ROOT / f"{day}.jsonl.gz", "at", compresslevel=3) as f:
        f.write(json.dumps(payload, separators=(",", ":"), default=str)+"\n")


def order_intent(ticker: str, order: dict) -> dict:
    """Reviewable V2 mapping; returning a dict is never submitting an order."""
    yes_side = order["side"] == "yes"
    return dict(ticker=ticker, client_order_id="w8-paper-"+order["id"],
                side="bid" if yes_side else "ask",
                price=f'{order["price"] if yes_side else 1-order["price"]:.4f}',
                count=f'{order["quantity"]:.2f}', time_in_force="good_till_canceled",
                post_only=True, expiration_time=int(order["expires_ts"]),
                self_trade_prevention_type="taker_at_cross", cancel_order_on_pause=True,
                submitted=False, mode="observation")


def _initial_state(params: kernel.Parameters) -> dict:
    return dict(version=params.version, registered_at=iso(), parameters=params.as_dict(),
                source_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (Path(__file__), Path(kernel.__file__))},
                execution_mode="observation_only", status="STARTING", ticks=0,
                inputs={}, series_fees={}, books={b: dict(positions={}, trades=[],
                    cum_net_usd=0., paired_net_usd=0., residual_net_usd=0.)
                    for b in ("paired", "tilted")}, errors={}, gaps=[],
                verdict=None, stopped_new=False)


def _params(cfg: dict) -> kernel.Parameters:
    # Signal parameters are frozen in the kernel, sizing in the parallel config.
    return replace(kernel.Parameters(), clip=float(cfg.get("contracts", 5)),
                   max_net=float(cfg.get("max_net_contracts", 15)))


def _refresh_fees(st: dict, now: float) -> bool:
    valid = True
    for series in SERIES:
        rec = st["series_fees"].get(series, {})
        if now-rec.get("checked_ts", 0) > 3600:
            try:
                data = read_get("/series/"+series).get("series", {})
                rec = dict(checked_ts=time.time(), fee_type=data.get("fee_type"),
                           fee_multiplier=data.get("fee_multiplier"), raw=data)
                st["series_fees"][series] = rec
            except Exception as e:
                st["errors"][series+":fee"] = str(e)[:180]
                valid = False
        # A fee change stops NEW quotes; already held exposure remains tracked.
        valid = valid and rec.get("fee_type") == "quadratic" and rec.get("fee_multiplier") == 1
    return valid


def _record_settlements(st: dict, now: float):
    due = {}
    for book in st["books"].values():
        for ticker, m in book["positions"].items():
            if now >= m["close_ts"]+60 and now-m.get("settlement_checked_ts", 0) >= 60:
                due[ticker] = True
    for ticker in due:
        positions = [b["positions"][ticker] for b in st["books"].values()
                     if ticker in b["positions"]]
        start = min(m.get("trade_watermark_ts", m["opened_ts"]) for m in positions)
        end = max([min(o["expires_ts"], o.get("cancel_ts", float("inf")))
                   for m in positions for o in m["orders"] if o["remaining"] > 1e-9]+[start])
        if end > start:
            # Drain the entire lifetime of old orders even when this market is
            # no longer in discovery (e.g. a restart across expiration).
            # Bound each catch-up request; a long interruption must not create
            # an unbounded pagination burst or discard unknown fills.
            span = min(m.get("final_backfill_span_s", 30.) for m in positions)
            target = min(end, now, start+span)
            try:
                final_trades, covered = fetch_trades(ticker, start, target)
            except Exception as e:
                final_trades, covered = [], False
                st["errors"][ticker+":final_tape"] = str(e)[:180]
            append_tape(dict(kind="final_trade_drain", ticker=ticker, ts=now,
                             interval_start=start, interval_end=target, complete=covered,
                             trades=final_trades))
            for m in positions:
                if covered:
                    kernel.process_trades(m, final_trades, kernel.Parameters(**st["parameters"]))
                    m["trade_watermark_ts"] = target
                    m["unverified_order_quantity"] = 0.
                    m["final_backfill_span_s"] = min(30., span*2)
                else:
                    m["coverage_gap"] = True
                    m["final_backfill_span_s"] = max(.25, span/2)
                    m["unverified_order_quantity"] = sum(o["remaining"] for o in m["orders"]
                        if min(o["expires_ts"], o.get("cancel_ts", float("inf"))) > start)
            if not covered or target < end:
                for m in positions:
                    m["settlement_checked_ts"] = now
                    m["settlement_pending_reason"] = "unread_order_lifetime"
                continue  # Official outcome cannot substitute for missing fills.
        try:
            result = read_get("/markets/"+ticker).get("market", {}).get("result")
        except Exception as e:
            result = None
            st["errors"][ticker+":settle"] = str(e)[:180]
        for label, book in st["books"].items():
            m = book["positions"].get(ticker)
            if not m:
                continue
            m["settlement_checked_ts"] = now
            row = kernel.settle(m, result)
            if row is None:
                continue
            row["settled_at"] = iso(now)
            book["trades"].append(row)
            for key in ("net_usd", "paired_net_usd", "residual_net_usd"):
                dest = "cum_net_usd" if key == "net_usd" else key
                book[dest] += row[key]
            # Full lot/order/fill ledger archived before removing active market.
            append_tape(dict(kind="settlement", ts=now, book=label, summary=row, ledger=m))
            book["positions"].pop(ticker)
            common.log_line(NAME, dict(action="settlement", book=label, **row))


def _mark_management_gaps(st: dict, now: float):
    # A terminal outage may never enter the successful-recovery path. Retain
    # that missed management interval even if all old fills are later read.
    for book in st["books"].values():
        for ticker, m in book["positions"].items():
            last = st["inputs"].get(ticker, {}).get("last_book_ts", m["opened_ts"])
            end = min(now, m["close_ts"])
            if end-last > 30 and not m.get("coverage_gap"):
                m["coverage_gap"] = True
                st["gaps"].append(dict(ticker=ticker, start=last, end=end,
                                       reason="position_management_gap"))


def _latch(st: dict):
    """One fixed 300-window decision on tilted net PnL; control is diagnostic."""
    if st.get("verdict") is not None:
        return
    rows = st["books"]["tilted"]["trades"]
    windows = {}
    for r in rows:
        windows.setdefault(r["close_ts"], []).append(r)
    windows = {ts: rs for ts, rs in windows.items() if any(r["fills"] for r in rs)}
    if len(windows) < 300:
        return
    selected = sorted(windows)[:300]
    boundary = selected[-1]
    if any(m["close_ts"] <= boundary for m in st["books"]["tilted"]["positions"].values()):
        return  # BTC and ETH in the same window must both finish settlement
    profits = [sum(r["net_usd"] for r in windows[t]) for t in selected]
    gap_windows = sum(any(r.get("coverage_gap") or r.get("unverified_order_quantity", 0)
                          for r in windows[t])
                      or (any("series" in r for r in windows[t]) and
                          set(r.get("series") for r in windows[t]) != set(SERIES))
                      for t in selected)
    import numpy as np
    from crypto_trading.crypto_common.trade_stats import newey_west_tstat
    a = np.asarray(profits, dtype=float)
    se = float(a.std(ddof=1)/len(a)**.5)
    hac = newey_west_tstat(a)
    conservative_se = max(se, hac["se_nw"]) if np.isfinite(hac["se_nw"]) else se
    t_stat = float(a.mean()/conservative_se) if conservative_se > 0 else None
    st["verdict"] = dict(registered_at=st["registered_at"], evaluated_at=iso(),
                         windows=300, net_usd=float(a.sum()), t_window=t_stat,
                         t_nw=hac["t_nw"], method="max(window_se,Newey_West_se)",
                         coverage_gap_windows=gap_windows,
                         passed=bool(gap_windows == 0 and a.sum() > 0 and t_stat is not None and t_stat >= 2.5),
                         scope="paper queue model only; not live execution approval")


def run(cfg: dict | None = None, **_) -> dict:
    global _HTTP_RESUME_TS, _HTTP_MIN_INTERVAL, _HTTP_RATE_STREAK, _HTTP_LAST_RATE_TS
    c = (cfg or common.load_cfg()).get(NAME, {})
    if c.get("enabled") or c.get("demo_mirror"):
        raise ValueError("W8 is observation-only; account execution is not implemented")
    state_path = common.state_path(NAME)
    with open(state_path.with_suffix(".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"strategy": NAME, "status": "BUSY"}
        p = _params(c)
        st = json.loads(state_path.read_text()) if state_path.exists() else _initial_state(p)
        _HTTP_RESUME_TS = max(_HTTP_RESUME_TS, st.get("http_resume_ts", 0.))
        http = st.get("http_read_control", {})
        _HTTP_MIN_INTERVAL = max(_HTTP_MIN_INTERVAL, http.get("min_interval_s", 1.))
        _HTTP_RATE_STREAK = max(_HTTP_RATE_STREAK, http.get("rate_streak", 0))
        _HTTP_LAST_RATE_TS = max(_HTTP_LAST_RATE_TS, http.get("last_rate_ts", 0.))
        for key in _HTTP_COUNTS:
            _HTTP_COUNTS[key] = max(_HTTP_COUNTS[key], http.get(key, 0))
        if st["parameters"] != p.as_dict():
            raise ValueError("W8 parameters changed; register a new version instead of mixing books")
        now = time.time()
        if now-st.get("last_tick_ts", 0) < 1.0:
            return {"strategy": NAME, "status": "CADENCE_SKIP"}
        fee_ok = _refresh_fees(st, now)
        _mark_management_gaps(st, now)
        _record_settlements(st, now)
        # Latch across BOTH books before either can quote. Otherwise paired,
        # processed first, can open one more order before tilted trips the stop.
        if any(book["cum_net_usd"] < -float(c.get("max_cum_loss_usd", 100))
               for book in st["books"].values()):
            st["stopped_new"] = True
        cycle = dict(strategy=NAME, ts=iso(now), status="OBSERVING", markets={})
        for series in SERIES:
            snap = w7_noisefade.latest_snapshot(series)  # discovery ONLY, never price
            if snap is None or not 0 <= now-snap.get("recv_ts", 0) <= 240:
                cycle["markets"][series] = {"status": "DISCOVERY_STALE"}
                continue
            candidates = [m for m in snap.get("markets", []) if m.get("status") == "active"
                          and timestamp(m["open_time"]) <= now < timestamp(m["close_time"])]
            if not candidates:
                cycle["markets"][series] = {"status": "NO_ACTIVE_MARKET"}
                continue
            meta = min(candidates, key=lambda m: timestamp(m["close_time"]))
            ticker = meta["ticker"]
            inp = st["inputs"].setdefault(ticker, {"last_trade_ts": now, "last_cycle_ts": now})
            previous = inp["last_trade_ts"]
            try:
                request_started = time.time()
                trade_end = min(request_started, previous+inp.get("backfill_span_s", 30.))
                trades, tape_complete = fetch_trades(ticker, previous, trade_end)
                raw = read_get("/markets/"+ticker+"/orderbook", {"depth": 50})
                received = time.time()
                book = kernel.normalize_book(raw, allow_one_sided=True)
                if book is None:
                    raise ValueError("empty/locked/crossed orderbook")
            except Exception as e:
                st["errors"][ticker] = str(e)[:180]
                cycle["markets"][series] = {"status": "DATA_ERROR", "error": str(e)[:180]}
                continue
            lag_gap = received-inp["last_cycle_ts"] > 30
            caught_up = (trade_end >= request_started-1e-6 and
                         received-trade_end <= max(5., 2*_HTTP_MIN_INTERVAL+3.))
            complete = tape_complete and not lag_gap and caught_up
            if not complete:
                st["gaps"].append(dict(ticker=ticker, start=previous, end=trade_end,
                                       reason="trade_pagination_or_observer_gap"))
            # Persist exact inputs before deriving hypothetical orders/fills.
            append_tape(dict(kind="observation", recv_ts=received, ticker=ticker,
                             series=series, metadata=meta, orderbook=raw, trades=trades,
                             complete_trade_interval=complete, interval_start=previous,
                             interval_end=trade_end))
            actions = {}
            for label, ledger in st["books"].items():
                m = ledger["positions"].setdefault(ticker, kernel.new_market(
                    ticker, series, timestamp(meta["close_time"]), received))
                if tape_complete:
                    fills = kernel.process_trades(m, trades, p)
                    m["trade_watermark_ts"] = trade_end
                    if lag_gap:
                        m["coverage_gap"] = True  # keep fills; flag missed management
                else:
                    # Do not advance the watermark or invent a past cancel.
                    # Retry a shorter interval, preserving possibly filled orders.
                    unknown = sum(o["remaining"] for o in m["orders"]
                                  if min(o["expires_ts"], o.get("cancel_ts", float("inf"))) > previous)
                    m["unverified_order_quantity"] = unknown
                    for order in m["orders"]:
                        order.setdefault("cancel_ts", received+p.latency_s)
                    m["coverage_gap"] = True
                    fills = []
                if tape_complete:
                    m["unverified_order_quantity"] = 0.
                # Completed fill interval ends BEFORE the new quote is posted.
                # Do not prune an order until all trades through its life are read.
                active_risk = sum(abs(kernel.net_quantity(x)) for x in ledger["positions"].values())
                other_risk = sum(abs(kernel.net_quantity(x))+max(
                    kernel._unread_order_risk(x, "yes", received), kernel._unread_order_risk(x, "no", received))
                    for t, x in ledger["positions"].items() if t != ticker)
                local_p = replace(p, max_net=max(0., min(p.max_net,
                    float(c.get("max_total_net_contracts", 30))-other_risk)))
                m["stop_new"] = m["stop_new"] or not fee_ok or active_risk > float(c.get("max_total_net_contracts", 30))
                if ledger["cum_net_usd"] < -float(c.get("max_cum_loss_usd", 100)):
                    st["stopped_new"] = True
                m["stop_new"] = m["stop_new"] or st["stopped_new"]
                if received >= m["close_ts"]:
                    created = []  # Never infer a new taker fill after market close.
                    m["stop_new"] = True
                elif not tape_complete or not caught_up:
                    m["coverage_gap"] = True
                    for order in m["orders"]:
                        order.setdefault("cancel_ts", received+p.latency_s)
                    created = []  # Risk decisions wait for the actual fill inventory.
                elif not fee_ok:
                    m["coverage_gap"] = True
                    m["fee_schedule_unverified"] = True
                    for order in m["orders"]:
                        order.setdefault("cancel_ts", received+p.latency_s)
                    created = []
                elif not book["two_sided"]:
                    # A missing bid side prevents new quotes, but existing
                    # inventory can still be reduced against the opposite depth.
                    for order in m["orders"]:
                        order.setdefault("cancel_ts", received+p.latency_s)
                    created = []
                    fills += kernel.flatten(m, book, received, p)
                elif (m["close_ts"]-received <= p.flatten_before_s or st["stopped_new"]
                        or m.get("force_flatten")):
                    created = kernel.update_quotes(m, book, received, local_p, residual=label == "tilted")
                    fills += kernel.flatten(m, book, received, p)
                else:
                    fills += kernel.take_pair(m, book, received, local_p, residual=label == "tilted")
                    created = kernel.update_quotes(m, book, received, local_p, residual=label == "tilted")
                intents = [order_intent(ticker, o) for o in created]
                for intent in intents:
                    common.log_line(NAME, dict(action="paper_order", book=label, intent=intent))
                for fill in fills:
                    common.log_line(NAME, dict(action="paper_fill", book=label, ticker=ticker, **fill))
                m["last_mark"] = kernel.liquidation_value(m, book, p)
                actions[label] = dict(fills=len(fills), quotes=len(created),
                                      net_contracts=kernel.net_quantity(m),
                                      paired_net_usd=m["paired_net_usd"], mark=m["last_mark"])
            inp.update(last_trade_ts=trade_end if tape_complete else previous, last_cycle_ts=received,
                       last_book_ts=received, interval_complete=complete)
            if not tape_complete:
                inp["backfill_span_s"] = max(.25, inp.get("backfill_span_s", 30.)/2)
            else:
                inp["backfill_span_s"] = min(30., inp.get("backfill_span_s", 30.)*2)
            market_status = ("AWAITING_SETTLEMENT" if received >= timestamp(meta["close_time"]) else
                             "CATCHING_UP" if not tape_complete or not caught_up else
                             "EXIT_ONLY_ONE_SIDED" if not book["two_sided"] else
                             "RECOVERED_AFTER_GAP" if lag_gap else "OK")
            cycle["markets"][series] = dict(status=market_status,
                                             ticker=ticker, trade_prints=len(trades), books=actions)
        st["gaps"] = st["gaps"][-1000:]
        _mark_management_gaps(st, time.time())
        st["ticks"] += 1
        st["last_tick_ts"] = time.time()
        st["last_tick"] = iso(st["last_tick_ts"])
        st["status"] = "FEE_UNVERIFIED" if not fee_ok else "STOPPED_NEW" if st["stopped_new"] else "OBSERVING"
        st["http_resume_ts"] = _HTTP_RESUME_TS
        st["http_read_control"] = dict(min_interval_s=_HTTP_MIN_INTERVAL,
            rate_streak=_HTTP_RATE_STREAK, last_rate_ts=_HTTP_LAST_RATE_TS, **_HTTP_COUNTS)
        if _HTTP_RESUME_TS > time.time():
            st["status"] = "RATE_LIMIT_BACKOFF"
        elif any(m["status"] in ("DATA_ERROR", "DISCOVERY_STALE", "CATCHING_UP")
                 for m in cycle["markets"].values()):
            st["status"] = "DATA_DEGRADED"
        elif all(m["status"] == "NO_ACTIVE_MARKET" for m in cycle["markets"].values()):
            st["status"] = "WAITING_FOR_MARKET"
        cycle["status"] = st["status"]
        current_sources = _LOADED_SOURCES
        if current_sources != st.get("source_sha256"):
            history = st.setdefault("operational_source_history", [])
            if not history or history[-1]["sha256"] != current_sources:
                history.append(dict(ts=iso(), sha256=current_sources,
                    note="Source revision recorded; parameters remain immutable. Review before interpreting results."))
        st["last_cycle"] = cycle
        _latch(st)
        common.save_state(NAME, st)
        return cycle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", type=float, default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Lifetime lock prevents accidental duplicate daemons; per-tick lock also
    # protects one-shot calls through the shared runner.
    with open(common.state_path(NAME).with_suffix(".daemon.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("W8 observer already running")
        while True:
            began = time.monotonic()
            try:
                report = run()
                print(json.dumps(report, separators=(",", ":")), flush=True)
            except Exception:
                logger.exception("W8 observation cycle failed")
                if not args.loop:
                    raise
            if not args.loop:
                break
            time.sleep(max(1., args.loop-(time.monotonic()-began)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
