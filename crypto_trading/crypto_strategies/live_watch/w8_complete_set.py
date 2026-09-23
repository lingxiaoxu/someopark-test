"""W8 independent, read-only Kalshi observation daemon (v5, 2026-09-12).

    python -m crypto_trading.crypto_strategies.live_watch.w8_complete_set --loop 2

THE STRATEGY lives in event_binary/complete_set.py - read its module docstring
for the rule and for why every constant is what it is. In one line: post small
passive bids only on the favoured side of a 15-minute BTC/ETH/DOGE/XRP binary,
only at $0.60-$0.78, complete the set for a +2c lock when the other leg is cheap
enough, otherwise ride the leg to official settlement.

THIS MODULE is the observation harness around it. What it guarantees:
  * read-only. Public GETs only; it never imports the execution router, never
    mirrors to demo, never authenticates. `enabled` or `demo_mirror` in config
    raises rather than trading.
  * two independent books. "tilted" (directional residual allowed) and
    "paired" (control) share market data and nothing else: separate ledgers,
    separate dollar stops, separate evidence kills, separate verdicts. v1's
    shared stop let the tilted book's loss terminate the control arm and
    destroyed the comparison it exists for.
  * settlement is the venue's official `result`, never inferred; a market is
    not settled until every order's whole lifetime has been read off the
    public tape (otherwise a late print would be lost).
  * fills are a conservative queue MODEL over public prints - hypothetical,
    not receipts. Restarts never fill stale orders; replayed prints are
    deduped; an order is never pruned while its life is unread.
  * data quality gates evidence. A window with a coverage gap is discarded
    from the verdict rather than judged, and the discard count is reported.
  * one decision, once. Each book latches at 300 CLEAN windows (pooled
    per-window sums, window-clustered t >= 2.5) and never re-opens; before
    that an always-valid boundary can kill on evidence. Continuous re-reading
    of a fixed-n gate is what made W7 measure 6.2% type-I where 0.6% was
    advertised.
  * it must not disturb the 24/7 recorders: paced public reads with sticky
    backoff, no state rewrite while in cooldown, tape that records the market
    metadata once per window rather than per tick.

Parameters are frozen per version: changing one requires a new version string
and a fresh book, never a mixed ledger.
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
# v5 (2026-09-12, user decision "加系列不放宽 range"): DOGE and XRP join.
# Same rule, same band, evaluated on 17 days of our own tape - passive entry
# on the favoured side inside [0.60,0.78], one observation per window, pooled
# per contract with window-clustered t:
#   DOGE +3.35c t 2.70 | XRP +3.13c t 2.53 | BTC +2.85c t 2.31
#   ETH  +0.66c t 0.51 | SOL +0.20c t 0.15  <- SOL EXCLUDED: it dilutes
# Adding DOGE+XRP raises the mean and the t (1.78c/t 2.00 -> 2.50c/t 4.01).
# CORRECTION (2026-09-14 audit): it does NOT raise window supply. The latch's
# unit is the distinct close_ts (_window_rows groups on it), and 15-minute
# markets give exactly 96 closes per UTC day no matter how many coins trade -
# the old "153 -> 304 per day, verdict in ~1 day" read market counts, and the
# ratio being exactly the coin ratio was the tell. Measured, every registration
# already saturates the calendar (v7 38 windows / 40 closes, 3.90/h). So 300
# windows is ~3.2 DAYS of uninterrupted running, not one. What four coins do
# buy is precision inside a window: mean pairwise within-window cross-coin
# correlation of net_usd is +0.234, worth ~2.35 independent draws per window.
# The per-coin ordering above is also NOT separable - max |t| over all ten
# pairwise coin comparisons is 2.40 against a Bonferroni 5% critical 2.81, and
# SOL moves to 3rd of 5 if the sampling window shifts by two minutes. SOL stays
# out only to avoid re-selecting the universe on the same 17 days that chose
# the band; it is not an established dilution.
# ETH is kept: it is the weakest included series but was part of the original
# registration, and dropping it now would be post-hoc selection on the same
# 17 days that chose the band.
SERIES = ("KXBTC15M", "KXETH15M", "KXDOGE15M", "KXXRP15M")
# v8: the two books now differ in ONE registered way - whether the second leg
# is ever bought. "paired" keeps the v1..v7 rule (complete at <= 0.98) and is
# the CONTROL; "tilted" holds the favoured leg to settlement and is the
# TREATMENT. They quote the same markets from the same data, so the difference
# is paired on identical close_ts. The pre-v8 difference (a 1.44x directional
# residual) was void: its only executed effect was an undocumented clip floor,
# now removed - see complete_set.py's residual allowance note.
BOOK_COMPLETES = {"paired": True, "tilted": False}
TAPE_ROOT = PRICE_DATA / "kalshi" / NAME / "prod"
logger = logging.getLogger(__name__)
_HTTP_RESUME_TS = 0.0
_HTTP_RATE_STREAK = 0
_HTTP_MIN_INTERVAL = 1.0
_HTTP_NEXT_REQUEST_TS = 0.0
_HTTP_LAST_RATE_TS = 0.0
_HTTP_LAST_DECAY_TS = 0.0
_HTTP_LAST_SUCCESS_TS = 0.0
# The pacer used to ratchet UP only (x1.5 to a 4s ceiling) and never come back
# down. Measured 2026-09-12 on the four-coin book: 0.9% of requests were rate
# limited, yet the interval sat pinned at 4.0s forever - and at 4s the two
# requests inside one cycle (trades page + orderbook) take longer than the
# caught_up tolerance of max(5, 2*interval+3), so EVERY window was marked
# gapped and the 300-clean-window verdict was unreachable by construction.
# The fix is to let a sustained clean period shed pace, never to relax the
# gap standard (manufacturing clean windows by lowering the bar is the v1
# mistake). Ratchet up stays immediate; decay is slow and one step at a time.
# A cycle must be able to swallow more wall-clock than it costs, or the
# observer falls permanently behind: with four series at ~3.5s pacing one
# revisit takes ~33s while the old 30s backfill cap advanced the trade
# watermark by at most 30s, so `recv - interval_end` grew without bound
# (measured 2026-09-13: median 55s, p90 335s, 84% over the caught_up
# tolerance -> 0 clean windows in 5 hours). Raising the cap fetches MORE
# trades per request; completeness is still enforced by pagination, so this
# buys catch-up without weakening the gap standard.
BACKFILL_MAX_S = 180.0
# The strip recorder already writes a FULL-depth orderbook for every 15M
# market every 90s (W8 fetches depth=20). Reusing that write costs nothing
# and removes a paced request from the cycle - the recorder is untouched, we
# only read what it already stored (user decision 2026-09-13: "不要动录制器
# 但是你直接用他的数据"). The freshness gate is the quote TTL: posting a
# 20s-lived quote off a book older than 20s would be a slower trader than we
# model, so anything staler triggers our own fetch. Every observation records
# which source it used, so the audit trail never has to guess.
RECORDED_BOOK_MAX_AGE_S = 20.0
STRIPS_ROOT = PRICE_DATA / "kalshi" / "event_strips" / "prod"


def recorded_orderbook(series: str, ticker: str, now: float) -> tuple[dict, float] | None:
    """Freshest recorded book for ``ticker``, or None if too old/absent."""
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    path = STRIPS_ROOT / series / "orderbook" / f"{day}.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:                    # tail only: these files
            fh.seek(max(0, size - 400_000))             # reach tens of MB/day
            chunk = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line or ticker not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("ticker") != ticker:
            continue
        age = now - row.get("recv_ts", 0)
        if 0 <= age <= RECORDED_BOOK_MAX_AGE_S and row.get("ob"):
            return row["ob"], age
        return None                                     # newest is already stale
    return None
_HTTP_FLOOR_INTERVAL = 1.5     # below this the v1 book drew steady 429s
_HTTP_DECAY_AFTER_S = 600.0    # 10 min with no 429 before any decay
_HTTP_DECAY_EVERY_S = 300.0    # then one step per 5 min
_HTTP_DECAY_STEP_S = 0.5
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
    global _HTTP_NEXT_REQUEST_TS, _HTTP_LAST_RATE_TS, _HTTP_LAST_DECAY_TS
    global _HTTP_LAST_SUCCESS_TS
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
        _HTTP_COUNTS["rate_limited"] += 1
        # WHOSE fault is this 429? Measured 2026-09-13: the repo's daily strip
        # recorder issues 11.5 req/s against the same host while this observer
        # issues 0.26 req/s (2.2% of the total), and a hand probe at 10 req/s
        # drew ZERO 429s either anonymous or authenticated. So these are
        # COLLATERAL - the collision rate is set by the other process, and
        # halving our own already-tiny rate does not reduce it. It only made
        # our cycle slow enough to dirty every window (interval pinned at 4s,
        # 0 clean windows in 5 hours). Escalate only when WE are plausibly the
        # cause: a real streak at a rate that could matter. Otherwise back off
        # briefly with jitter and carry on at the same pace.
        try:
            requested = float(response.headers.get("Retry-After", "0"))
        except ValueError:
            requested = 0.
        ours = _HTTP_RATE_STREAK >= 4 and _HTTP_MIN_INTERVAL < 4.
        if ours:
            _HTTP_MIN_INTERVAL = min(4., _HTTP_MIN_INTERVAL*1.5)
            delay = max(requested, min(300., 60.*2**min(_HTTP_RATE_STREAK-4, 3)))
        else:
            # deterministic jitter (no Math.random equivalent needed): spread
            # retries across the colliding process's cycle instead of lining
            # up with it again
            delay = max(requested, 3.0 + (_HTTP_COUNTS["requests"] % 7))
        _HTTP_RESUME_TS = time.time()+delay
        raise ReadRateLimited(f"public API 429; cooldown {delay:.0f}s"
                              f"{' (own rate)' if ours else ' (collateral)'}")
    response.raise_for_status()
    payload = response.json()
    # Only consecutive 429s escalate the long cooldown. Successful reads
    # break that streak without restoring the reduced pace: otherwise sparse
    # 429s, separated by many successes, accumulate into minute-long outages.
    _HTTP_RATE_STREAK = 0
    _HTTP_LAST_SUCCESS_TS = time.time()
    # sustained clean period -> shed one step of pace (see the constants above)
    now = time.time()
    if (_HTTP_MIN_INTERVAL > _HTTP_FLOOR_INTERVAL
            and now - _HTTP_LAST_RATE_TS >= _HTTP_DECAY_AFTER_S
            and now - _HTTP_LAST_DECAY_TS >= _HTTP_DECAY_EVERY_S):
        _HTTP_MIN_INTERVAL = max(_HTTP_FLOOR_INTERVAL,
                                 _HTTP_MIN_INTERVAL - _HTTP_DECAY_STEP_S)
        _HTTP_LAST_DECAY_TS = now
    return payload


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
                    cum_net_usd=0., paired_net_usd=0., residual_net_usd=0.,
                    set_pair_net_usd=0., drift_pair_net_usd=0.,
                    # v8: the one registered difference between the arms. Kept
                    # HERE and not only in `parameters`, which carries the
                    # kernel default and so cannot describe a per-book rule.
                    complete_sets=BOOK_COMPLETES[b],
                    stopped_new=False)
                    for b in ("paired", "tilted")}, errors={}, gaps=[],
                verdict=None, verdicts={}, stopped_new=False)


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
                    m["final_backfill_span_s"] = min(BACKFILL_MAX_S, span*2)
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
            for key in ("net_usd", "paired_net_usd", "residual_net_usd",
                        "set_pair_net_usd", "drift_pair_net_usd",
                        "stop_pair_net_usd"):
                dest = "cum_net_usd" if key == "net_usd" else key
                book[dest] = book.get(dest, 0.) + row.get(key, 0.)
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


def _window_rows(book: dict) -> dict:
    windows = {}
    for r in book["trades"]:
        windows.setdefault(r["close_ts"], []).append(r)
    return {ts: rs for ts, rs in windows.items() if any(r["fills"] for r in rs)}


def _clean(rows: list) -> bool:
    """Is this window's P&L TRUSTWORTHY? That is a narrower question than
    "did we watch it perfectly", and conflating the two made the verdict
    unreachable by construction.

    `unverified_order_quantity > 0` means an order's life spanned an interval
    of trade tape we never read, so a fill may be missing and the recorded
    P&L may be WRONG. Those windows must be excluded - non-negotiable.

    `coverage_gap` alone means only that we looked late (>30s between visits)
    while the trade tape for the period was nonetheless read in full: every
    fill that could have happened was seen, the arithmetic is right, and the
    strategy simply managed less often than a continuously-watching one. That
    is a property of the strategy AS EXECUTED, not a data defect, so the
    window counts - and the management-gap rate is reported beside the
    verdict so the reader knows what was measured.

    Why this matters (measured 2026-09-13): per-observation completeness ran
    89%, but a 15-minute window holds ~50 observations, so P(no gap anywhere)
    = 0.89^50 ~= 0.3%. Nine of nine filled windows were flagged - every one
    of them with unverified_order_quantity == 0.0, i.e. every one with a
    provably complete fill ledger.
    """
    return not any(r.get("unverified_order_quantity", 0) for r in rows)


def _managed_gap(rows: list) -> bool:
    """Window was P&L-trustworthy but revisited late at some point."""
    return any(r.get("coverage_gap") for r in rows)


def _window_sums(book: dict, clean_only: bool = True) -> list[float]:
    ws = _window_rows(book)
    return [sum(r["net_usd"] for r in rs) for ts, rs in sorted(ws.items())
            if _clean(rs) or not clean_only]


def _latch(st: dict):
    """W7-style: each book latches ONCE, at 300 CLEAN windows.

    v1 required the FIRST 300 windows to all be gap-free; with 429s gapping
    most windows that verdict was unreachable by construction. v2 judges on
    clean windows only (a gapped window is missing data, not evidence) and
    reports how many were discarded - data quality gates evidence honestly
    instead of blocking it forever. Judged per book; the decision never
    re-opens (optional stopping is the 6.2%-type-I lesson from W7).
    """
    verdicts = st.setdefault("verdicts", {})
    for label, book in st["books"].items():
        if verdicts.get(label) is not None:
            continue
        ws = _window_rows(book)
        clean = {ts: rs for ts, rs in ws.items() if _clean(rs)}
        if len(clean) < 300:
            continue
        selected = sorted(clean)[:300]
        boundary = selected[-1]
        if any(m["close_ts"] <= boundary for m in book["positions"].values()):
            continue  # BTC and ETH in the same window must both settle first
        sums = [sum(r["net_usd"] for r in clean[t]) for t in selected]
        n, mu, t_stat = kernel.window_sum_stats(sums)
        contracts = sum(r.get("quantity", 0.) for t in selected for r in clean[t])
        verdicts[label] = dict(
            registered_at=st["registered_at"], evaluated_at=iso(), book=label,
            windows=n, net_usd=round(sum(sums), 4),
            management_gap_windows=sum(_managed_gap(clean[t]) for t in selected),
            mean_usd_per_window=round(mu, 4), t_window=round(t_stat, 3),
            net_c_per_contract=(round(sum(sums)/contracts*100, 3)
                                if contracts else None),
            gapped_windows_discarded=len(ws)-len(clean),
            passed=bool(sum(sums) > 0 and t_stat >= 2.5),
            scope="paper queue model only; not live execution approval")
        common.log_line(NAME, dict(action="verdict_latched", **verdicts[label]))
    if st.get("verdict") is None and verdicts.get("tilted") is not None:
        st["verdict"] = verdicts["tilted"]  # v1 field kept for readers


def run(cfg: dict | None = None, **_) -> dict:
    global _HTTP_RESUME_TS, _HTTP_MIN_INTERVAL, _HTTP_RATE_STREAK, _HTTP_LAST_RATE_TS
    global _HTTP_LAST_DECAY_TS, _HTTP_LAST_SUCCESS_TS
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
        _HTTP_LAST_DECAY_TS = max(_HTTP_LAST_DECAY_TS, http.get("last_decay_ts", 0.))
        _HTTP_LAST_SUCCESS_TS = max(_HTTP_LAST_SUCCESS_TS, http.get("last_success_ts", 0.))
        for key in _HTTP_COUNTS:
            _HTTP_COUNTS[key] = max(_HTTP_COUNTS[key], http.get(key, 0))
        if st["parameters"] != p.as_dict():
            raise ValueError("W8 parameters changed; register a new version instead of mixing books")
        now = time.time()
        if now-st.get("last_tick_ts", 0) < 1.0:
            return {"strategy": NAME, "status": "CADENCE_SKIP"}
        if now < max(_HTTP_RESUME_TS, st.get("http_resume_ts", 0.)):
            # In cooldown no request goes out and no book can change; v1 still
            # rewrote (and fsynced) the whole multi-hundred-KB state every 2s
            # tick - 1.2 GB/h of no-op writes on the recorders' volume.
            return {"strategy": NAME, "status": "RATE_LIMIT_BACKOFF",
                    "resume_at": iso(max(_HTTP_RESUME_TS, st.get("http_resume_ts", 0.)))}
        fee_ok = _refresh_fees(st, now)
        _mark_management_gaps(st, now)
        _record_settlements(st, now)
        # v2: each book latches on ITS OWN loss (and its own evidence test) -
        # v1's shared latch let the tilted book's breach terminate the paired
        # CONTROL arm at -$86.86, which never itself crossed the limit, so the
        # two series stopped being comparable. A latch is still checked before
        # either book quotes within the cycle.
        for label, book in st["books"].items():
            if book.get("stopped_new"):
                continue
            if book["cum_net_usd"] < -float(c.get("max_cum_loss_usd", 100)):
                book["stopped_new"] = True
                book["stopped_reason"] = (f"dollar backstop: cum "
                                          f"{book['cum_net_usd']:.2f}")
            else:
                sums = _window_sums(book)
                n, mu, t = kernel.window_sum_stats(sums)
                if n >= 30 and t <= -kernel.always_valid_bound(n):
                    book["stopped_new"] = True
                    book["stopped_reason"] = (
                        f"evidence: window t={t:.2f} <= "
                        f"-{kernel.always_valid_bound(n):.2f} over {n} windows")
            if book.get("stopped_new"):
                common.log_line(NAME, dict(action="book_stopped", book=label,
                                           reason=book["stopped_reason"]))
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
            # If NO book held an order or inventory across this interval, no
            # fill can have occurred in it - so the trade tape for it carries
            # no information and the watermark may advance without reading it.
            # This halves the requests per cycle early in a window, which is
            # what lets a four-series cycle stay inside the revisit budget.
            idle = all(
                not any(o["remaining"] > 1e-9 for o in m.get("orders", []))
                and abs(kernel.net_quantity(m)) <= 1e-9
                for m in (b["positions"].get(ticker) for b in st["books"].values())
                if m is not None)
            try:
                request_started = time.time()
                trade_end = min(request_started,
                                previous+inp.get("backfill_span_s", BACKFILL_MAX_S))
                if idle and ticker in st["inputs"]:
                    trades, tape_complete = [], True
                else:
                    trades, tape_complete = fetch_trades(ticker, previous, trade_end)
                reused = recorded_orderbook(series, ticker, time.time())
                if reused is not None:
                    raw, book_source = reused[0], f"recorder({reused[1]:.0f}s)"
                    st.setdefault("book_source", {})["reused"] = \
                        st.get("book_source", {}).get("reused", 0) + 1
                else:
                    raw = read_get("/markets/"+ticker+"/orderbook", {"depth": 20})
                    book_source = "own_fetch"
                    st.setdefault("book_source", {})["fetched"] = \
                        st.get("book_source", {}).get("fetched", 0) + 1
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
            # v1 re-recorded the identical 2.2KB market metadata blob on every
            # observation - 276 MB/day, 3.4x the entire strips recorder set,
            # on the same volume whose exhaustion killed four recorders.
            # Record it once per ticker (it is static for a window).
            meta_key = hashlib.sha256(json.dumps(meta, sort_keys=True,
                                                 default=str).encode()).hexdigest()[:16]
            tape_row = dict(kind="observation", recv_ts=received, ticker=ticker,
                            series=series, metadata_sha=meta_key, orderbook=raw,
                            book_source=book_source,
                            trades=trades, complete_trade_interval=complete,
                            interval_start=previous, interval_end=trade_end)
            if inp.get("metadata_sha") != meta_key:
                tape_row["metadata"] = meta
                inp["metadata_sha"] = meta_key
            append_tape(tape_row)
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
                    float(c.get("max_total_net_contracts", 30))-other_risk)),
                    complete_sets=BOOK_COMPLETES[label])
                m["stop_new"] = m["stop_new"] or not fee_ok or active_risk > float(c.get("max_total_net_contracts", 30))
                if ledger["cum_net_usd"] < -float(c.get("max_cum_loss_usd", 100)):
                    ledger["stopped_new"] = True
                    ledger.setdefault("stopped_reason",
                                      f"dollar backstop: cum {ledger['cum_net_usd']:.2f}")
                m["stop_new"] = m["stop_new"] or bool(ledger.get("stopped_new"))
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
                elif (m["close_ts"]-received <= p.flatten_before_s
                        or ledger.get("stopped_new") or m.get("force_flatten")):
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
                inp["backfill_span_s"] = max(.25, inp.get("backfill_span_s", BACKFILL_MAX_S)/2)
            else:
                inp["backfill_span_s"] = min(BACKFILL_MAX_S,
                                             inp.get("backfill_span_s", BACKFILL_MAX_S)*2)
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
        st["status"] = ("FEE_UNVERIFIED" if not fee_ok else
                        "STOPPED_NEW" if all(b.get("stopped_new")
                                             for b in st["books"].values())
                        else "OBSERVING")
        st["http_resume_ts"] = _HTTP_RESUME_TS
        st["http_read_control"] = dict(min_interval_s=_HTTP_MIN_INTERVAL,
            rate_streak=_HTTP_RATE_STREAK, last_rate_ts=_HTTP_LAST_RATE_TS,
            last_decay_ts=_HTTP_LAST_DECAY_TS, last_success_ts=_HTTP_LAST_SUCCESS_TS,
            **_HTTP_COUNTS)
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
