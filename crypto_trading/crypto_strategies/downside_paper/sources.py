"""Pure source adapters for isolated W9/W10 paper cohort observation.

No live strategy, common state helper, exchange client, filesystem or network is
used here. All inputs are caller-provided snapshots/events. All timestamp
arguments and returned *_ts values are finite Unix seconds in UTC.

Public API:
  make_parent_binding('w7'|'w8', state, source_hashes=None) -> immutable-by-copy dict
  w7_candidates_from_state(state, binding, *, since_ts, until_ts, contracts=25)
  w7_outcomes_from_state(state, binding, *, until_ts, contracts=25)
  w8_candidate_from_order(event, state, binding, *, since_ts, until_ts)
  w8_outcomes_from_state(state, binding, *, until_ts)
  w8_existing_tickers(state) -> set[str]

Candidate dictionaries have id, strategy ('w9'/'w10'), ticker, asset, side,
decision_ts, expires_ts, parent_binding and metadata. Outcome dictionaries have
the same id/binding, closed_ts, expires_ts, net_usd, cost_usd, fees_usd and
payout_usd. W10 inherits an admitted tilted MARKET'S complete lifecycle. It
does not filter individual fills, re-run queue fills or re-allocate the W8
manager's global risk limits. It is a conditional source cohort, not an
independent execution simulation. The paired arm is never added to W10 PnL.

paper_order can precede the parent's atomic state write. SourceNotReady means
retain the event and retry against a later supplied snapshot; never switch to
an unverified log-price/timestamp fallback. Time out as unclassified, not a
profitable avoided trade. ParentBindingMismatch blocks admission to a changed
parent; an outcome from another registration must not settle an old episode.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from typing import Mapping
from zoneinfo import ZoneInfo


class SourceNotReady(ValueError):
    """A first quote is newer than the cutoff or absent from parent state."""


class ParentBindingMismatch(ValueError):
    """Source registration/version/hash provenance differs from the binding."""


_TICKER = re.compile(r"^KX(BTC|ETH|SOL|DOGE|XRP)15M-(\d{2}[A-Z]{3}\d{6})-(00|15|30|45)$")
_NATIVE = {"w7": "w7_noisefade", "w8": "w8_complete_set"}
_TARGET = {"w7": "w9", "w8": "w10"}


def _timestamp(value) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a timestamp")
    if isinstance(value, (float, int)):
        result = float(value)
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("source timestamps must include a timezone")
        result = dt.timestamp()
    else:
        raise ValueError("missing or unsupported timestamp")
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid timestamp")
    return result


def _number(value) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric source amount")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite source amount")
    return result


def _ticker(ticker: str, parent: str) -> tuple[str, float]:
    match = _TICKER.fullmatch(ticker) if isinstance(ticker, str) else None
    if not match:
        raise ValueError("not an exact supported Kalshi 15M ticker")
    asset, local_event, minute = match.groups()
    if parent == "w8" and asset == "SOL":
        raise ValueError("SOL is outside the registered W8 universe")
    local = datetime.strptime(local_event, "%y%b%d%H%M").replace(tzinfo=ZoneInfo("America/New_York"))
    if local.minute != int(minute) or local.minute % 15:
        raise ValueError("ticker suffix does not match its expiry minute")
    # Ambiguous fall-back local times cannot identify an exact expiration
    # without venue metadata; reject instead of guessing fold 0 or fold 1.
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("ambiguous New York ticker expiry requires exact metadata")
    return asset, local.astimezone(timezone.utc).timestamp()


def _hashes(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("source hashes must be a mapping")
    result = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("source hash provenance requires SHA-256 values")
        result[key] = digest.lower()
    return dict(sorted(result.items()))


def make_parent_binding(strategy: str, state: Mapping, source_hashes: Mapping | None = None) -> dict:
    """Bind source registration and both recorded/disk hash provenance.

    source_hashes are observed bytes supplied by the caller, not proof of the
    code loaded in another process. Registered W8 hashes may differ; preserve
    both rather than pretending to repair or overwrite that history.
    """
    if strategy not in _NATIVE:
        raise ValueError("parent must be w7 or w8")
    version = state.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("parent version is required")
    registered = _timestamp(state.get("main_registered_at" if strategy == "w7" else "registered_at"))
    if registered <= 0:
        raise ValueError("parent registration must be a real positive timestamp")
    return {
        "source_strategy": _NATIVE[strategy], "target_strategy": _TARGET[strategy],
        "version": version, "registered_at": datetime.fromtimestamp(registered, timezone.utc).isoformat(),
        "registered_source_sha256": _hashes(state.get("source_sha256")),
        "observed_source_sha256": _hashes(source_hashes),
        "source_hash_semantics": "recorded_registration_and_caller_observed_bytes_not_process_memory",
    }


def _check_binding(parent: str, state: Mapping, binding: Mapping) -> None:
    actual = make_parent_binding(parent, state, binding.get("observed_source_sha256"))
    if actual != dict(binding):
        raise ParentBindingMismatch("source parent binding changed")


def _episode_id(binding: Mapping, ticker: str, leg: str) -> str:
    identity = {key: binding[key] for key in ("source_strategy", "version", "registered_at")}
    identity.update(ticker=ticker, leg=leg)
    digest = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return f"{binding['target_strategy']}:{digest}"


def _candidate(binding, ticker, asset, side, decision, expiry, leg, metadata):
    return {"id": _episode_id(binding, ticker, leg), "strategy": binding["target_strategy"],
            "ticker": ticker, "asset": asset, "side": side, "decision_ts": decision,
            "expires_ts": expiry, "parent_binding": deepcopy(dict(binding)), "metadata": metadata}


def _w7_entry(row: Mapping, binding: Mapping, contracts: float) -> dict | None:
    if row.get("leg") != "band":
        return None
    cost = _number(row["cost"])
    if not .78 <= cost <= .98:
        return None
    decision = _timestamp(row["opened"])
    if decision < _timestamp(binding["registered_at"]):
        return None
    ticker = row["ticker"]
    asset, expiry = _ticker(ticker, "w7")
    if row.get("series") != "KX" + asset + "15M":
        raise ValueError("W7 series and exact ticker differ")
    if row.get("close") is not None and abs(_timestamp(row["close"])-expiry) > 1e-6:
        raise ValueError("W7 source close differs from exact ticker")
    side = row.get("side")
    if side not in ("yes", "no") or decision >= expiry:
        raise ValueError("invalid W7 direction or entry time")
    return _candidate(binding, ticker, asset, side, decision, expiry, "MAIN",
                      {"parent_leg": "MAIN", "entry_price": cost, "contracts": contracts,
                       "parent_recorded_leg": "band", "entry_price_bounds": [.78, .98],
                       "source_kind": "W7_paper_entry_state", "execution_claim": "inherited_paper_not_exchange_fill"})


def w7_candidates_from_state(state: Mapping, binding: Mapping, *, since_ts: float, until_ts: float,
                             contracts: float = 25) -> list[dict]:
    _check_binding("w7", state, binding)
    since, until, quantity = _timestamp(since_ts), _timestamp(until_ts), _number(contracts)
    if since > until or quantity <= 0:
        raise ValueError("invalid interval or quantity")
    rows = list(state.get("trades", [])) + [{**p, "ticker": ticker} for ticker, p in state.get("positions", {}).items()]
    out = {}
    for row in rows:
        candidate = _w7_entry(row, binding, quantity)
        if candidate and since <= candidate["decision_ts"] <= until:
            if candidate["id"] in out:
                raise ValueError("duplicate source W7 episode")
            out[candidate["id"]] = candidate
    return sorted(out.values(), key=lambda r: (r["decision_ts"], r["ticker"]))


def w7_outcomes_from_state(state: Mapping, binding: Mapping, *, until_ts: float,
                           contracts: float = 25) -> list[dict]:
    _check_binding("w7", state, binding)
    until, quantity = _timestamp(until_ts), _number(contracts)
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    out = []
    seen = set()
    for row in state.get("trades", []):
        candidate = _w7_entry(row, binding, quantity)
        if candidate is None:
            continue
        closed = _timestamp(row["closed"])
        if max(closed, candidate["expires_ts"]) > until:
            continue
        if closed < candidate["expires_ts"] or not isinstance(row.get("win"), bool):
            raise ValueError("W7 settlement time/win is invalid")
        if candidate["id"] in seen:
            raise ValueError("duplicate W7 outcome")
        seen.add(candidate["id"])
        cost = candidate["metadata"]["entry_price"] * quantity
        payout = quantity if row["win"] else 0.0
        net = _number(row["pnl_c"]) * quantity / 100
        fee = payout - cost - net
        if fee < -1e-7:
            raise ValueError("W7 paper amounts do not reconcile")
        out.append({**{k: candidate[k] for k in ("id", "strategy", "ticker", "asset", "expires_ts", "parent_binding")},
                    "closed_ts": closed, "net_usd": net, "cost_usd": cost, "fees_usd": max(0.0, fee),
                    "payout_usd": payout, "metadata": {"parent_leg": "MAIN", "contracts": quantity,
                    "parent_recorded_win": row["win"], "fees_basis": "implied_by_parent_rounded_paper_pnl_not_exchange_fee",
                    "source_kind": "W7_settled_paper_state"}})
    return sorted(out, key=lambda r: (r["closed_ts"], r["ticker"]))


def w8_existing_tickers(state: Mapping) -> set[str]:
    """Conservatively seed ALL source episodes before a new W10 registration.

    Includes zero-fill positions, quoted/ordered positions and settled trades
    from both source books; starting mid-episode must not count as a new quote.
    """
    result = set()
    for book in state.get("books", {}).values():
        result.update(book.get("positions", {}))
        result.update(r["ticker"] for r in book.get("trades", []) if isinstance(r.get("ticker"), str))
    return result


def w8_candidate_from_order(event: Mapping, state: Mapping, binding: Mapping, *,
                            since_ts: float, until_ts: float) -> dict | None:
    _check_binding("w8", state, binding)
    since, until = _timestamp(since_ts), _timestamp(until_ts)
    if since > until:
        raise ValueError("invalid interval")
    event = event.get("event", event)
    if event.get("strategy") != "w8_complete_set" or event.get("action") != "paper_order" or event.get("book") != "tilted":
        return None
    if event.get("version", binding["version"]) != binding["version"]:
        return None
    logged = _timestamp(event["ts"])
    if logged > until:
        # The caller may read an appended line after freezing cycle.now.
        # Returning None here would let the tail cursor discard a new episode.
        raise SourceNotReady("W8 order event is newer than this observation cutoff")
    if logged < max(since, _timestamp(binding["registered_at"])):
        return None
    intent = event.get("intent", {})
    ticker = intent.get("ticker")
    asset, expiry = _ticker(ticker, "w8")
    order_id = ticker + ":1"
    if intent.get("client_order_id") != "w8-paper-" + order_id:
        return None  # Later revisions cannot make a rejected episode re-enter.
    if (intent.get("submitted") is not False or intent.get("mode") != "observation"
            or intent.get("post_only") is not True or intent.get("side") not in ("bid", "ask")):
        return None
    parent_book = state.get("books", {}).get("tilted", {})
    if parent_book.get("complete_sets") is not False:
        raise ParentBindingMismatch("W10 requires the parent tilted non-completion book")
    market = parent_book.get("positions", {}).get(ticker)
    if market is None:
        raise SourceNotReady("first W8 quote has no active source market in this snapshot")
    if market.get("series") != "KX" + asset + "15M" or abs(_timestamp(market["close_ts"])-expiry) > 1e-6:
        raise ValueError("W8 market identity/expiry differs from exact ticker")
    matches = [o for o in market.get("orders", []) if o.get("id") == order_id]
    if not matches:
        raise SourceNotReady("first W8 order has not reached parent state yet")
    if len(matches) != 1:
        raise ValueError("duplicate first source W8 order")
    order = matches[0]
    decision, price, quantity = _timestamp(order["created_ts"]), _number(order["price"]), _number(order["quantity"])
    side = "yes" if intent["side"] == "bid" else "no"
    v2_price = price if side == "yes" else 1-price
    if (order.get("side") != side or abs(_number(intent["price"])-v2_price) > 1e-8
            or abs(_number(intent["count"])-quantity) > 1e-8 or quantity <= 0
            or int(_number(intent["expiration_time"])) != int(_number(order["expires_ts"]))):
        raise ValueError("W8 log order does not match the source ledger")
    if not max(since, _timestamp(binding["registered_at"])) <= decision <= min(until, logged):
        return None
    if decision >= expiry:
        return None
    return _candidate(binding, ticker, asset, side, decision, expiry, "tilted",
                      {"parent_leg": "tilted", "source_order_id": order_id, "entry_price": price,
                       "contracts": quantity, "source_log_ts": logged, "decision_ts_basis": "parent_order_created_ts",
                       "source_kind": "W8_tilted_first_quote_verified_in_state",
                       "execution_claim": "conditional_whole_episode_cohort_not_independent_manager"})


def w8_outcomes_from_state(state: Mapping, binding: Mapping, *, until_ts: float) -> list[dict]:
    _check_binding("w8", state, binding)
    until, registered = _timestamp(until_ts), _timestamp(binding["registered_at"])
    book = state.get("books", {}).get("tilted", {})
    if book.get("complete_sets") is not False:
        raise ParentBindingMismatch("W10 requires the tilted book")
    out, seen = [], set()
    for row in book.get("trades", []):
        ticker = row["ticker"]
        asset, expiry = _ticker(ticker, "w8")
        closed = _timestamp(row["settled_at"])
        if expiry <= registered or max(expiry, closed) > until:
            continue
        if row.get("series") != "KX" + asset + "15M" or abs(_timestamp(row["close_ts"])-expiry) > 1e-6 or closed < expiry:
            raise ValueError("invalid W8 settlement identity/time")
        if row.get("result") not in ("yes", "no"):
            raise ValueError("W8 outcome requires a directly recorded official result")
        eid = _episode_id(binding, ticker, "tilted")
        if eid in seen:
            raise ValueError("duplicate W8 outcome")
        seen.add(eid)
        amounts = {key: _number(row[key]) for key in ("net_usd", "cost_usd", "fees_usd", "payout_usd")}
        if any(amounts[k] < -1e-9 for k in ("cost_usd", "fees_usd", "payout_usd")):
            raise ValueError("negative W8 cash amount")
        if abs(amounts["payout_usd"]-amounts["cost_usd"]-amounts["fees_usd"]-amounts["net_usd"]) > 1e-7:
            raise ValueError("W8 settlement cashflows do not reconcile")
        if abs(_number(row["paired_net_usd"])+_number(row["residual_net_usd"])-amounts["net_usd"]) > 1e-7:
            raise ValueError("W8 lifecycle components do not reconcile")
        raw_fills, unknown = _number(row["fills"]), _number(row.get("unverified_order_quantity", 0))
        fills = int(raw_fills)
        if fills < 0 or fills != raw_fills or unknown < 0:
            raise ValueError("invalid W8 fill evidence")
        quantity = _number(row["quantity"])
        if quantity < 0 or (fills == 0 and (quantity != 0 or any(abs(x) > 1e-8 for x in amounts.values()))):
            raise ValueError("W8 no-fill settlement must have zero lifecycle cashflows")
        out.append({"id": eid, "strategy": "w10", "ticker": ticker, "asset": asset,
                    "expires_ts": expiry, "closed_ts": closed, "parent_binding": deepcopy(dict(binding)), **amounts,
                    "metadata": {"parent_leg": "tilted", "source_kind": "W8_whole_market_settlement",
                    "official_result": row["result"], "fills": fills, "zero_fill": fills == 0,
                    "turnover_quantity": quantity, "coverage_gap": bool(row.get("coverage_gap")),
                    "unverified_order_quantity": unknown, "source_fill_verified": unknown == 0,
                    "paired_component_usd": _number(row["paired_net_usd"]),
                    "residual_component_usd": _number(row["residual_net_usd"]),
                    "component_semantics": "components_of_tilted_only_not_the_paired_control_arm",
                    "execution_claim": "inherits_all_parent_management_fills_and_settlement"}})
    return sorted(out, key=lambda r: (r["closed_ts"], r["ticker"]))
