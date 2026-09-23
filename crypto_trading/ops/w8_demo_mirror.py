"""Continuous W8 v8a treatment mirror on Kalshi DEMO, separate from observation.

Reuse W7's KalshiEventOrderClient for signing, V2 encoding, submission and
cancellation. Entries retain W8's price, post-only flag and original 20s expiry;
risk_flatten signals use IOC against this mirror's VERIFIED inventory only.
Nothing here dispatches PROD orders or writes the observer/W7 state. Run with
--arm for demo execution; the default is a read-only dry run.

A journaled client ID precedes every POST. Unknown outcomes block another order
in that market; zero-fill terminal orders allow a subsequent fresh paper quote.
Accounting uses our order IDs and official results, never the shared account's
aggregate position or PnL (W7 may trade the same ticker).

Service: bash crypto_trading/ops/w8_demo.sh install|status|stop.
Execution journal/state: trading_signals/w8_demo_mirror/ (under crypto_trading).
The observer's config stays demo_mirror:false: that flag rejects in-process
execution; this separately armed service reads the observer's fresh log only.
Partial/zero demo fills are recorded as such, never copied from paper fills.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient

LOG_DIR = SIGNALS_DIR / "live_watch"       # read only
OUT_DIR = SIGNALS_DIR / "w8_demo_mirror"
DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"
SERIES = ("KXBTC15M", "KXETH15M", "KXDOGE15M", "KXXRP15M")
BOOK = "tilted"
VERSION = "w8_v8a_20260914"
MIN_REM_S, MAX_REM_S = 180., 960.
MAX_SIGNAL_AGE = 15.
MAX_OPEN_RISK = 6.
MAX_LOSS = 100.
EXIT_RETRY_BASE_S, EXIT_RETRY_MAX_S = 5., 30.
MAX_EXIT_QUOTE_AGE_S = 2.
TERMINAL = {"canceled", "executed"}


class ObserverUnavailable(RuntimeError):
    """A transient state-write/liveness race; a fresh signal may be deferred."""


def stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _emit(payload):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    line = json.dumps({"ts": now.isoformat(), **payload}, default=str)
    with (OUT_DIR / f"log_{now:%Y-%m-%d}.jsonl").open("a") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def decode_intent(intent):
    """Undo W8's wire inversion before W7's client encodes it once again."""
    price = float(intent["price"])
    if intent["side"] not in ("bid", "ask") or not 0 < price < 1:
        raise ValueError("invalid wire side/price")
    return ("yes", price) if intent["side"] == "bid" else ("no", round(1-price, 4))


def public_market(ticker):
    r = requests.get(f"{DEMO_BASE}/markets/{ticker}", timeout=5)
    r.raise_for_status()
    return r.json()["market"]


def live_demo_market(ticker):
    market = public_market(ticker)
    remaining = stamp(market["close_time"]) - time.time()
    return market if market.get("status") == "active" and MIN_REM_S <= remaining <= MAX_REM_S else None


def demo_exit_quote(ticker, side):
    # Same precision-preserving, DEMO-only book reader used by W7. Importing
    # this helper does not construct a router or inspect any PROD account.
    from crypto_trading.crypto_common.execution_events import demo_executable_ask
    return demo_executable_ask(ticker, side, timeout=5.)


def account_json(client, path, **params):
    if params:
        path += "?" + urlencode(params)
    r = client._authed("GET", path)
    r.raise_for_status()
    return r.json()


def account_rows(client, path, key, **params):
    rows, cursor, seen = [], None, set()
    for _ in range(50):
        page = account_json(client, path, limit=100, **params,
                            **({"cursor": cursor} if cursor else {}))
        if not isinstance(page.get(key), list) or "cursor" not in page:
            raise ValueError(f"incomplete {key} account response")
        rows.extend(page[key])
        cursor = page.get("cursor")
        if not cursor:
            return rows
        if cursor in seen:
            raise ValueError("repeated account cursor; audit is incomplete")
        seen.add(cursor)
    raise ValueError("account pagination limit; audit is incomplete")


def read_order(client, record):
    if record.get("order_id"):
        return account_json(client, "/portfolio/orders/" + record["order_id"])["order"]
    rows = account_rows(client, "/portfolio/orders", "orders", ticker=record["ticker"])
    return next((o for o in rows if o.get("client_order_id") == record["client_order_id"]), None)


class Tail:
    """Skip pre-start backlog; on UTC rollover start the new file at byte zero."""
    def __init__(self):
        self.path = self.today()
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    @staticmethod
    def today():
        return LOG_DIR / f"log_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def read(self):
        current = self.today()
        if current != self.path:
            self.path, self.offset = current, 0
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
        with self.path.open("rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(size - self.offset)
        cut = chunk.rfind(b"\n") + 1
        self.offset += cut
        out = []
        for line in chunk[:cut].splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


class Mirror:
    def __init__(self, *, dry_run=False, client=None):
        self.dry_run = dry_run
        self.client = None if dry_run else (client or KalshiEventOrderClient(env="demo", timeout=5))
        if self.client is not None and (self.client.env != "demo" or self.client.base != DEMO_BASE):
            raise ValueError("W8 mirror requires the DEMO client and host")
        self.path = OUT_DIR / ("dry_run_state.json" if dry_run else "state.json")
        # A corrupt journal is a hard stop, not permission to start empty.
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": VERSION, "started_at": datetime.now(timezone.utc).isoformat(),
            "markets": {}, "counters": {}, "net_pnl_usd": 0.}
        if self.state["version"] != VERSION:
            raise ValueError("mirror registration changed; preserve the existing ledger")
        self.last_heartbeat = 0.
        self.last_source_cancel_check = 0.
        self.state.setdefault("queued", {})
        self.state.setdefault("deferred", {})
        self.state["execution_revision"] = "20260915_book_and_exit_recovery"

    def save(self):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w") as fh:
            json.dump(self.state, fh, separators=(",", ":"), allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        temp.replace(self.path)

    def event(self, event, **values):
        counts = self.state["counters"]
        counts[event] = counts.get(event, 0) + 1
        _emit(dict(event=event, **values))

    def observer(self, *, entry, signal_ts=None):
        try:
            st = json.loads((LOG_DIR / "w8_complete_set_state.json").read_text())
        except (OSError, ValueError) as exc:
            raise ObserverUnavailable("observer state temporarily unreadable") from exc
        if st.get("version") != VERSION:
            raise ValueError("observer version differs from armed mirror")
        # The producer verifies its registered parameters before emitting
        # signals, but saves last_tick_ts only at the END of its HTTP cycle.
        # A fresh event in its local tape proves liveness independently of
        # that lagging heartbeat. Version and original 15s TTL remain strict.
        fresh_event = signal_ts is not None and -1 <= time.time()-signal_ts <= MAX_SIGNAL_AGE
        if time.time() - stamp(st["last_tick_ts"]) > 60 and not fresh_event:
            raise ObserverUnavailable("observer heartbeat stale")
        if entry and (st.get("stopped_new") or st["books"][BOOK].get("stopped_new")):
            raise ValueError("observer has stopped new entries")
        return st

    @staticmethod
    def net(m):
        return sum(o.get("filled", 0.) * (1 if o["side"] == "yes" else -1) for o in m["orders"])

    @staticmethod
    def pending(m):
        return [o for o in m["orders"] if not o.get("terminal")]

    def open_risk(self):
        return sum(abs(self.net(m)) + sum(o["quantity"] for o in self.pending(m))
                   for m in self.state["markets"].values() if not m.get("settled"))

    def handle(self, row):
        if row.get("strategy") != "w8_complete_set" or row.get("book") != BOOK:
            return False
        entry = row.get("action") == "paper_order"
        exiting = row.get("action") == "paper_fill" and row.get("source") == "risk_flatten"
        if not entry and not exiting:
            return False
        now = time.time()
        if not -1 <= now - stamp(row["ts"]) <= MAX_SIGNAL_AGE:
            return False
        intent = row.get("intent") or {}
        ticker = intent.get("ticker") if entry else row.get("ticker")
        if not ticker or not any(ticker.startswith(s + "-") for s in SERIES):
            return False
        if row.get("version", VERSION) != VERSION:
            raise ValueError("signal version differs from armed mirror")
        try:
            self.observer(entry=entry, signal_ts=stamp(row["ts"]))
        except ObserverUnavailable as exc:
            key = ticker + (":entry" if entry else ":exit")
            if key not in self.state["deferred"]:
                self.event("signal_deferred", ticker=ticker, reason=str(exc))
            self.state["deferred"][key] = row
            return False
        self.state["last_source_signal_ts"] = max(stamp(row["ts"]), self.state.get("last_source_signal_ts", 0))
        if exiting:
            return self.request_exit(ticker, row)
        side, price = decode_intent(intent)
        expiry = int(intent.get("expiration_time", 0))
        if (intent.get("count") != "1.00" or intent.get("post_only") is not True
                or not .62 <= price <= .86 or not 2 <= expiry-now <= 21):
            return False
        source_id = intent["client_order_id"]
        coid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"w8-demo:{VERSION}:{source_id}"))
        m = self.state["markets"].get(ticker)
        if m and m.get("exit_requested"):
            return False
        if m and self.pending(m):
            # A quote can arrive just before the old order's terminal
            # readback. Keep the newest request until that readback, bounded
            # by the ORIGINAL signal freshness and expiry.
            self.state["queued"][ticker+":entry"] = row
            return False
        if m and (m.get("settled") or any(o["client_order_id"] == coid for o in m["orders"])):
            return False
        # Even a partial fill consumes this one-contract entry budget. Do not
        # round its residual up to another whole contract on demo.
        if m and any(o.get("filled", 0) > 0 for o in m["orders"]):
            return False
        if self.state["net_pnl_usd"] <= -MAX_LOSS or self.open_risk() + 1 > MAX_OPEN_RISK:
            self.event("skip", ticker=ticker, reason="demo_risk_budget")
            return False
        market = live_demo_market(ticker)
        if market is None:
            self.event("skip", ticker=ticker, reason="not_live_on_demo")
            return False
        quantity = 1.
        if m is None:
            m = self.state["markets"][ticker] = {"orders": [], "close_ts": stamp(market["close_time"])}
        if self.dry_run:
            self.event("would_send", ticker=ticker, side=side, price=price, quantity=quantity, entry=entry)
            return False
        # HTTP/market lookup may use up the source quote's TTL. Never renew it.
        if (time.time()-stamp(row["ts"]) > MAX_SIGNAL_AGE
                or time.time() >= stamp(market["close_time"])
                or (entry and expiry-time.time() < 2)):
            self.event("skip", ticker=ticker, reason="quote_expired_during_lookup")
            return False
        return self.send_order(m, ticker=ticker, coid=coid, source_id=source_id,
                               side=side, price=price, quantity=quantity,
                               entry=entry, expiry=expiry)

    def send_order(self, m, *, ticker, coid, source_id, side, price, quantity,
                   entry, expiry, evidence=None):
        record = dict(ticker=ticker, client_order_id=coid, source_id=source_id,
                      side=side, price=price, quantity=quantity, entry=entry,
                      expiration_time=expiry, submitted_ts=time.time(), status="pending_send",
                      filled=0., cost=0., fees=0., terminal=False)
        if evidence is not None:
            record["execution_quote"] = evidence
        m["orders"].append(record)
        self.save()  # crash after POST must leave the order reserved
        try:
            response = self.client.create_order(
                ticker=ticker, side=side, price_dollars=price, count=quantity,
                client_order_id=coid, tif="good_till_canceled" if entry else "immediate_or_cancel",
                post_only=entry, **({"expiration_time": expiry} if entry else {}))
            record["status_code"] = response["status_code"]
            try:
                body = json.loads(response.get("response") or "{}")
            except ValueError:
                body = {}
            order_id = body.get("order_id") or body.get("order", {}).get("order_id")
            record["order_id"] = order_id
            record["status"] = "accepted" if response["status_code"] == 201 else "unknown"
            # Explicit validation refusal has no executable order. Timeouts,
            # rate limits, server errors and duplicate IDs remain unresolved.
            if response["status_code"] in (400, 401, 403, 404, 422) and not order_id:
                record.update(status="rejected", terminal=True)
            self.event("sent", **record, response=response.get("response"), body_sent=response.get("body_sent"))
        except Exception as exc:
            record.update(status="unknown", error=str(exc)[:200])
            self.event("send_error", ticker=ticker, client_order_id=coid, error=str(exc)[:200])
        self.save()
        return True

    def request_exit(self, ticker, row):
        """Latch a verified fresh risk signal; the desire to flatten persists."""
        m = self.state["markets"].get(ticker)
        if m is None or m.get("settled"):
            return False
        source_id = "risk-"+hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        m["exit_requested"] = True
        m.setdefault("exit_request_id", source_id)
        m.setdefault("exit_requested_ts", stamp(row["ts"]))
        m["exit_source_limit"] = float(row["price"])
        self.state["queued"].pop(ticker+":entry", None)
        self.state["deferred"].pop(ticker+":entry", None)
        for o in self.pending(m):
            if o["entry"]:
                o["cancel_requested"] = True
        self.save()  # preserve the exit request even if the next GET crashes
        return self.manage_exit_market(ticker, m)

    def manage_exit_market(self, ticker, m):
        """Continue reducing only this journal's inventory using new demo quotes.

        The source signal expires as a PRICE, not as a risk decision. Never
        reuse its old price or paper quantity, and never resubmit an unknown
        request. Every new IOC has a journaled sequence and its own client ID.
        """
        if not m.get("exit_requested") or m.get("settled"):
            return False
        now = time.time()
        if now >= m["close_ts"]:
            m["exit_status"] = "awaiting_settlement"
            return False
        if self.pending(m):
            m["exit_status"] = "awaiting_order_confirmation"
            return False
        net = round(self.net(m), 2)
        if abs(net) < .01:
            m["exit_status"] = "flat"
            return False
        if now < m.get("next_exit_attempt_ts", 0):
            return False
        side = "no" if net > 0 else "yes"
        streak = min(3, int(m.get("exit_retry_streak", 0)))
        delay = min(EXIT_RETRY_MAX_S, EXIT_RETRY_BASE_S * 2**streak)
        m["next_exit_attempt_ts"] = now+delay
        m["exit_retry_streak"] = streak+1
        m.setdefault("exit_request_id", next((o["source_id"] for o in m["orders"]
                     if not o["entry"]), f"legacy:{VERSION}:{ticker}"))
        self.save()  # restarting cannot bypass the retry backoff
        try:
            started = time.time()
            market = public_market(ticker)
            if market.get("status") != "active":
                m["exit_status"] = "market_not_active"
                return False
            quote = demo_exit_quote(ticker, side)
            if quote["status"] != "ready":
                m["exit_status"] = quote["status"]
                self.event("exit_wait", ticker=ticker, **quote)
                return False
            if (time.time()-started > MAX_EXIT_QUOTE_AGE_S
                    or time.time() >= min(m["close_ts"], stamp(market["close_time"]))):
                m["exit_status"] = "quote_expired"
                self.event("exit_wait", ticker=ticker, reason="quote_expired_during_lookup")
                return False
            from decimal import Decimal, ROUND_DOWN
            available = min(Decimal(str(abs(net))), Decimal(str(quote["book_top_quantity"])))
            quantity = float(available.quantize(Decimal(".01"), rounding=ROUND_DOWN))
            price = float(quote["book_ask_dollars"])
            if quantity < .01 or not math.isfinite(price) or not 0 < price < 1:
                m["exit_status"] = "no_executable_quantity"
                return False
            if self.dry_run:
                self.event("would_exit", ticker=ticker, side=side, price=price, quantity=quantity)
                return False
            sequence = int(m.get("exit_sequence", sum(not o["entry"] for o in m["orders"])))+1
            m["exit_sequence"] = sequence
            source_id = f"{m['exit_request_id']}:attempt:{sequence}"
            coid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"w8-demo:{VERSION}:{ticker}:{source_id}"))
            m["exit_status"] = "submitting"
            return self.send_order(m, ticker=ticker, coid=coid, source_id=source_id,
                                   side=side, price=price, quantity=quantity, entry=False,
                                   expiry=int(m["close_ts"]), evidence=quote)
        except Exception as exc:
            m["exit_status"] = "quote_or_execution_error"
            self.event("exit_wait", ticker=ticker, reason=str(exc)[:200])
            return False

    def manage_exits(self):
        return sum(int(self.manage_exit_market(t, m)) for t, m in self.state["markets"].items())

    def drain_deferred(self):
        count = 0
        for key, _ in sorted(list(self.state["deferred"].items()), key=lambda item: not item[0].endswith(":exit")):
            row = self.state["deferred"].pop(key, None)
            if row is None:  # an earlier exit removed its pending entry
                continue
            if not -1 <= time.time()-stamp(row["ts"]) <= MAX_SIGNAL_AGE:
                self.event("signal_expired", ticker=key.split(":")[0], reason="source_state_not_ready")
                continue
            try:
                count += int(self.handle(row))
            except Exception as exc:
                self.event("signal_error", error=str(exc)[:200])
        return count

    def drain_queued(self):
        count = 0
        for key, row in sorted(list(self.state["queued"].items()), key=lambda item: not item[0].endswith(":exit")):
            ticker = (row.get("intent") or {}).get("ticker") or row.get("ticker")
            if time.time()-stamp(row["ts"]) > MAX_SIGNAL_AGE:
                self.state["queued"].pop(key)
            elif not self.pending(self.state["markets"][ticker]):
                self.state["queued"].pop(key)
                try:
                    count += int(self.handle(row))
                except Exception as exc:
                    self.event("signal_error", error=str(exc)[:200])
        return count

    def resolve_absent_after_close(self, o, m, now):
        """Recover a lost refusal only after two complete, attributable audits.

        Never used for a known exchange order ID. A fill with an unknown owner
        prevents recovery; the account may contain W7 fills in this market.
        """
        if o.get("order_id") or now < m["close_ts"]+120:
            return False
        market = public_market(o["ticker"])
        if market.get("status") not in ("settled", "finalized"):
            return False
        orders = account_rows(self.client, "/portfolio/orders", "orders", ticker=o["ticker"])
        if any(r.get("client_order_id") == o["client_order_id"] for r in orders):
            return False
        known = {r["order_id"]: r for r in orders}
        fills = account_rows(self.client, "/portfolio/fills", "fills", ticker=o["ticker"])
        for fill in fills:
            oid = fill["order_id"]
            owner = known.get(oid) or account_json(self.client, "/portfolio/orders/"+oid)["order"]
            if (owner.get("ticker") != o["ticker"] or not owner.get("client_order_id")
                    or owner["client_order_id"] == o["client_order_id"]):
                return False
        evidence = dict(ts=now, market_status=market["status"], orders=len(orders),
                        fills=len(fills), all_fills_attributed_elsewhere=True)
        checks = o.setdefault("absence_checks", [])
        checks.append(evidence)
        if len(checks) >= 2 and now-checks[0]["ts"] >= 30:
            o.update(terminal=True, status="absent_after_close", filled=0., cost=0., fees=0.)
            self.event("absent_after_close", ticker=o["ticker"], client_order_id=o["client_order_id"],
                       evidence=[checks[0], checks[-1]])
            return True
        return False

    def sync_source_cancels(self, now):
        """Propagate early cancellation of still-unfilled paper quotes.

        A paper fill is not a demo fill: completion of a simulated order must
        not cancel a still-valid demo quote merely because the tapes differ.
        Only an explicit cancel_ts on an unfilled source remainder qualifies.
        No observer state is written, and only our persisted order IDs can be
        canceled. Exchange refusal leaves the original expiry as the backstop.
        """
        if now-self.last_source_cancel_check < 2:
            return
        candidates = [o for m in self.state["markets"].values() for o in self.pending(m)
                      if o["entry"] and now < o["expiration_time"]
                      and not o.get("cancel_requested")]
        if not candidates:
            return
        self.last_source_cancel_check = now
        try:
            source = self.observer(entry=False)["books"][BOOK].get("positions", {})
        except (OSError, ValueError, KeyError, ObserverUnavailable):
            return  # unreadable/stale source is not evidence of a cancellation
        for o in candidates:
            paper_id = o["source_id"].removeprefix("w8-paper-")
            paper = next((p for p in source.get(o["ticker"], {}).get("orders", [])
                          if p["id"] == paper_id), None)
            if (paper and float(paper.get("remaining", 0)) > 0
                    and paper.get("cancel_ts", float("inf")) <= now):
                o.update(cancel_requested=True, cancel_reason="source_quote_canceled",
                         source_cancel_ts=paper["cancel_ts"])
                self.event("source_cancel", ticker=o["ticker"], order_id=o.get("order_id"),
                           source_id=o["source_id"], source_cancel_ts=paper["cancel_ts"])

    def reconcile(self):
        if self.dry_run:
            return
        now = time.time()
        self.sync_source_cancels(now)
        for ticker, m in self.state["markets"].items():
            if m.get("settled"):
                continue
            for o in m["orders"]:
                interval = 60 if now >= m["close_ts"]+120 else 5
                if o.get("terminal") or now-o.get("checked_ts", 0) < interval:
                    continue
                o["checked_ts"] = now
                try:
                    if (o.get("cancel_requested") and o.get("order_id")
                            and not o.get("cancel_attempted")
                            and now < o["expiration_time"]):
                        o["cancel_attempted"] = True
                        reply = self.client.cancel_order(o["order_id"], market_ticker=ticker)
                        self.event("cancel_requested", order_id=o["order_id"], **reply)
                        # A response alone never makes an order terminal.
                    raw = read_order(self.client, o)
                    if raw is None:
                        if self.resolve_absent_after_close(o, m, now):
                            continue
                        # An absent lookup is not a proved zero fill. Reserve
                        # this market (and its risk budget) until it is found.
                        raise ValueError("order not visible yet; preserving unknown exposure")
                    if raw["client_order_id"] != o["client_order_id"] or raw["ticker"] != ticker:
                        raise ValueError("order identity mismatch")
                    filled = float(raw["fill_count_fp"])
                    remaining = float(raw["remaining_count_fp"])
                    cost = float(raw["maker_fill_cost_dollars"])+float(raw["taker_fill_cost_dollars"])
                    fees = float(raw["maker_fees_dollars"])+float(raw["taker_fees_dollars"])
                    if (raw.get("outcome_side", raw.get("side")) != o["side"]
                            or not all(math.isfinite(v) and v >= 0 for v in (filled, remaining, cost, fees))
                            or filled+remaining > o["quantity"]+1e-8):
                        raise ValueError("unexpected filled quantity")
                    before = (o["status"], o.get("filled"))
                    o.update(order_id=raw["order_id"], status=raw["status"], filled=filled,
                             remaining=remaining,
                             cost=cost, fees=fees)
                    terminal = raw["status"] in TERMINAL and remaining == 0
                    if terminal and filled:
                        fills = account_rows(self.client, "/portfolio/fills", "fills", order_id=o["order_id"])
                        own = [f for f in fills if f.get("order_id") == o["order_id"]]
                        qty = sum(float(f["count_fp"]) for f in own)
                        if abs(qty-filled) > 1e-6:
                            raise ValueError("fills not yet reconciled to terminal order")
                        o["fills"] = own
                    if not o["entry"] and filled > before[1]:
                        m["exit_retry_streak"] = 0
                    o["terminal"] = terminal
                    o.pop("read_error", None)
                    if before != (o["status"], filled) or terminal:
                        self.event("readback", ticker=ticker, order_id=o["order_id"], found=True,
                                   status=o["status"], filled=filled, remaining=remaining,
                                   cost=o["cost"], fees=o["fees"], terminal=terminal)
                except Exception as exc:
                    o["read_error"] = str(exc)[:200]
                    if now-o.get("error_logged_ts", 0) >= 60:
                        o["error_logged_ts"] = now
                        self.event("readback_error", ticker=ticker, order_id=o.get("order_id"), error=o["read_error"])
            if now >= m["close_ts"]+10 and not self.pending(m) and now-m.get("settlement_checked_ts", 0) >= 60:
                m["settlement_checked_ts"] = now
                try:
                    market = public_market(ticker)
                    result = market.get("result")
                    if result not in ("yes", "no") or market.get("status") not in ("settled", "finalized"):
                        continue
                    cost = sum(o["cost"] for o in m["orders"])
                    fees = sum(o["fees"] for o in m["orders"])
                    payout = sum(o["filled"] for o in m["orders"] if o["side"] == result)
                    m.update(settled=True, result=result, cost=cost, fees=fees, payout=payout,
                             net_pnl_usd=round(payout-cost-fees, 8), settled_ts=now)
                    self.state["net_pnl_usd"] = round(sum(x.get("net_pnl_usd", 0.) for x in self.state["markets"].values()), 8)
                    self.event("settlement", ticker=ticker, result=result, cost=cost, fees=fees,
                               payout=payout, net_pnl_usd=m["net_pnl_usd"])
                except Exception as exc:
                    self.event("settlement_error", ticker=ticker, error=str(exc)[:200])

    def heartbeat(self):
        now = time.time()
        if now-self.last_heartbeat < 30:
            return
        self.last_heartbeat = now
        try:
            self.observer(entry=True, signal_ts=self.state.get("last_source_signal_ts"))
            observer_status = "ok"
        except Exception as exc:
            observer_status = str(exc)[:200]
        active = {t: m for t, m in self.state["markets"].items() if not m.get("settled")}
        self.state.update(last_heartbeat=datetime.now(timezone.utc).isoformat(), pid=os.getpid(),
                          mode="dry_run" if self.dry_run else "demo", observer_status=observer_status,
                          pending_orders=sum(len(self.pending(m)) for m in active.values()),
                          open_net_contracts=sum(abs(self.net(m)) for m in active.values()))
        self.event("heartbeat", **{k: self.state[k] for k in ("pid", "mode", "observer_status", "pending_orders", "open_net_contracts", "net_pnl_usd")})
        self.save()


def run(*, dry_run, limit=0):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / ("dry_run.lock" if dry_run else "daemon.lock")).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        reader = Tail()
        mirror = Mirror(dry_run=dry_run)
        mirror.event("start", dry_run=dry_run, book=BOOK, version=VERSION, pid=os.getpid())
        count = 0
        while True:
            # New quotes first, before account reads consume their short TTL.
            for row in reader.read():
                try:
                    count += int(mirror.handle(row))
                except Exception as exc:
                    mirror.event("signal_error", error=str(exc)[:200])
            mirror.reconcile()
            count += mirror.drain_deferred()
            count += mirror.drain_queued()
            count += mirror.manage_exits()
            mirror.heartbeat()
            mirror.save()
            if limit and count >= limit:
                mirror.event("stop", reason="limit", count=count)
                return count
            time.sleep(1.)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", action="store_true", help="place DEMO orders; default is dry-run")
    ap.add_argument("--limit", type=int, default=0, help="stop after N attempts (0: continuous)")
    args = ap.parse_args(argv)
    run(dry_run=not args.arm, limit=args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
