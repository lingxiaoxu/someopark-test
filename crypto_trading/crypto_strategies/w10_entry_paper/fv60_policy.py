"""Frozen FV60 gates and conservative prospective source-cohort accounting.

This module does not create orders or model a new queue. Source paper fills
are eligible only after W10 durably publishes the applicable decision. A
precommitted 60-second deadline may reject later creations without pretending
that their later observation was a new pre-trade decision. All management and
fees are inherited only if the retained entry inventory is exactly identical.
An unidentified partial inventory never earns credit for avoiding a loss.
"""
from collections import Counter, defaultdict
from hashlib import sha256
import json
import math

from .policy import _finite, _outcome_error, _risk


POLICY_VERSION = "fresh_spot_value_entry60_v1"
ENTRY_CREATION_MAX_AGE_SECONDS = 60.0
MODE = "prospective_conditional_source_quote_cohort"
_CLASSIFIED = frozenset(("accept", "skip"))
_CASH = ("net_usd", "cost_usd", "fees_usd", "payout_usd")


def _unclassified(reason):
    return {"decision": "unclassified", "reason": reason}


def _classification(value):
    return value.get("decision") if isinstance(value, dict) else value


def _feature_error(candidate, features):
    if features.get("valid") is not True:
        return "incomplete_causal_valuation"
    if candidate.get("side") not in ("yes", "no"):
        return "unknown_contract_direction"
    created, at = _finite(candidate.get("decision_ts")), _finite(features.get("decision_ts"))
    expiry = _finite(candidate.get("expires_ts"))
    if created is None or created <= 0 or at != created:
        return "feature_timestamp_not_parent_first_quote"
    if expiry is None or expiry-created <= 60:
        return "outside_frozen_before_final_minute_formula"
    metadata = candidate.get("metadata", {})
    price, quantity = _finite(metadata.get("entry_price")), _finite(metadata.get("contracts"))
    probability, edge = _finite(features.get("probability_proxy")), _finite(features.get("edge_proxy"))
    if price is None or not 0 < price < 1 or quantity is None or quantity <= 0:
        return "invalid_first_quote_allocation"
    if probability is None or not 0 <= probability <= 1 or edge is None:
        return "invalid_probability_or_edge"
    if abs(edge-(probability-price)) > 1e-10:
        return "valuation_edge_identity_mismatch"
    return None


def decide(candidate, features, parameters):
    """The frozen initial valuation gate; its inputs are receipt-clock causal."""
    if parameters.get("policy", POLICY_VERSION) != POLICY_VERSION:
        return _unclassified("unsupported_policy_version")
    if parameters.get("entry_creation_max_age_seconds", 60) != 60:
        return _unclassified("unregistered_entry_deadline")
    error = _feature_error(candidate, features)
    if error:
        return _unclassified(error)
    edge = float(features["edge_proxy"])
    return {"decision": "accept" if edge > 0 else "skip",
            "reason": "fresh_spot_normal_value_vs_original_quote",
            "probability_proxy": float(features["probability_proxy"]),
            "edge_proxy": edge,
            "entry_creation_deadline_ts": float(candidate["decision_ts"])+60}


def quote_source(order, ticker=None):
    """Canonical immutable creation fields; queue and cancellation may evolve."""
    if not isinstance(order, dict):
        raise ValueError("source quote must be a dictionary")
    identifier = order.get("id", order.get("order_id"))
    ticker = ticker or order.get("ticker")
    if not isinstance(identifier, str) or not identifier or not isinstance(ticker, str) or not ticker:
        raise ValueError("missing source quote identity")
    if order.get("id") is not None and order.get("order_id") not in (None, identifier):
        raise ValueError("source quote ID aliases disagree")
    try:
        sequence = int(identifier.removeprefix(ticker+":"))
    except (ValueError, TypeError):
        raise ValueError("invalid source quote sequence") from None
    if sequence < 1 or identifier != ticker+":"+str(sequence):
        raise ValueError("invalid source quote sequence")
    fields = {key: _finite(order.get(key)) for key in
              ("price", "quantity", "created_ts", "activate_ts", "expires_ts")}
    if any(value is None for value in fields.values()):
        raise ValueError("invalid source quote number")
    if (order.get("side") not in ("yes", "no") or not 0 < fields["price"] < 1
            or fields["quantity"] <= 0
            or not 0 < fields["created_ts"] <= fields["activate_ts"] < fields["expires_ts"]):
        raise ValueError("invalid source quote terms")
    return {"id": identifier, "ticker": ticker, "side": order["side"], **fields}


def quote_fingerprint(order, ticker=None):
    source = quote_source(order, ticker)
    return sha256(json.dumps(source, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def decide_quote(candidate, initial_decision, source_order, parameters=None):
    """Classify immutable quote intent, independently of eventual fills.

    Publication/observation timing is verified separately when accounting.
    A side change never resets the initial clock; old orders are not cancelled
    at 60 seconds and existing inventory is never flattened by this predicate.
    """
    if parameters and parameters.get("policy", POLICY_VERSION) != POLICY_VERSION:
        return _unclassified("unsupported_policy_version")
    initial = _classification(initial_decision)
    if initial not in _CLASSIFIED:
        return _unclassified("initial_valuation_unclassified")
    first = _finite(candidate.get("decision_ts"))
    try:
        source = quote_source(source_order, candidate.get("ticker"))
    except ValueError:
        return _unclassified("invalid_source_quote")
    expiry = _finite(candidate.get("expires_ts"))
    if first is None or expiry is None or source["created_ts"] < first or source["expires_ts"] > expiry:
        return _unclassified("source_quote_outside_episode")
    age = source["created_ts"]-first
    if initial == "skip":
        return {"decision": "skip", "reason": "initial_fair_value_rejected", "quote_age_seconds": age}
    keep = age <= ENTRY_CREATION_MAX_AGE_SECONDS
    return {"decision": "accept" if keep else "skip", "quote_age_seconds": age,
            "reason": "within_frozen_entry_creation_window" if keep else "precommitted_entry_deadline_elapsed"}


def _reject(reason, **extra):
    return {"status": "unclassified", "reason": reason, **extra}


def _publication_error(row):
    created = _finite(row["candidate"].get("decision_ts"))
    observed, published = _finite(row.get("observed_at")), _finite(row.get("decision_published_at"))
    if (created is None or observed is None or published is None
            or not created <= observed <= published < row["candidate"]["expires_ts"]):
        return "initial_decision_publication_unproven"
    if _classification(row.get("decision")) == "accept" and published > created+60:
        return "initial_accept_published_after_entry_deadline"
    return None


def _ledger(row):
    """Validate full source cashflows without trusting a filtered fill list."""
    ledger = row.get("source_ledger")
    if not isinstance(ledger, dict):
        return None, "missing_complete_source_ledger"
    candidate, outcome = row["candidate"], row["outcome"]
    if (ledger.get("ticker") != candidate["ticker"]
            or _finite(ledger.get("close_ts")) != _finite(candidate["expires_ts"])):
        return None, "source_ledger_identity_mismatch"
    if row.get("source_ledger_final") is not True:
        return None, "source_ledger_not_final"
    if not isinstance(ledger.get("orders"), list) or not isinstance(ledger.get("fills"), list):
        return None, "invalid_source_ledger_lists"
    orders = {}
    raw_orders = {}
    try:
        for order in ledger["orders"]:
            source = quote_source(order, candidate["ticker"])
            if source["id"] in orders:
                return None, "duplicate_source_quote"
            if source["created_ts"] < candidate["decision_ts"] or source["expires_ts"] > candidate["expires_ts"]:
                return None, "source_quote_outside_episode"
            orders[source["id"]] = source
            raw_orders[source["id"]] = order
    except (ValueError, TypeError):
        return None, "invalid_source_quote"
    first_id = candidate.get("metadata", {}).get("source_order_id", candidate["ticker"]+":1")
    first = orders.get(first_id)
    if not first or (first["created_ts"] != candidate["decision_ts"] or first["side"] != candidate["side"]
            or first["price"] != candidate["metadata"]["entry_price"]
            or first["quantity"] != candidate["metadata"]["contracts"]):
        return None, "first_source_quote_identity_mismatch"
    expected = {candidate["ticker"]+":"+str(n) for n in range(1, len(orders)+1)}
    if set(orders) != expected:
        return None, "incomplete_source_quote_sequence"
    official = outcome["metadata"].get("official_result")
    if official not in ("yes", "no"):
        return None, "missing_official_result"
    cash = dict.fromkeys(_CASH, 0.0)
    turnover = 0.0
    entries, fills = [], []
    quantities = defaultdict(float)
    seen = set()
    for fill in ledger["fills"]:
        if not isinstance(fill, dict):
            return None, "invalid_source_fill"
        numbers = {k: _finite(fill.get(k)) for k in ("quantity", "price", "fee_usd", "ts")}
        if (any(v is None for v in numbers.values()) or numbers["quantity"] <= 0
                or not 0 < numbers["price"] < 1 or numbers["fee_usd"] < 0
                or fill.get("side") not in ("yes", "no")
                or not candidate["decision_ts"] <= numbers["ts"] <= candidate["expires_ts"]):
            return None, "invalid_source_fill"
        # Duplicate IDs/timestamps may represent genuinely separate prints, so
        # include recorded trade identity and amounts in the fingerprint.
        try:
            fingerprint = json.dumps(fill, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            return None, "invalid_source_fill"
        if fingerprint in seen:
            return None, "duplicate_source_fill"
        seen.add(fingerprint)
        if fill.get("liquidity") == "maker_model":
            source = orders.get(fill.get("order_id"))
            if source is None:
                return None, "source_entry_fill_without_order"
            if (fill["side"] != source["side"] or abs(numbers["price"]-source["price"]) > 1e-10
                    or not source["activate_ts"] < numbers["ts"] < source["expires_ts"]):
                return None, "source_entry_fill_terms_mismatch"
            cancellation = raw_orders[source["id"]].get("cancel_ts", raw_orders[source["id"]].get("cancel_effective_ts"))
            if cancellation is not None and (_finite(cancellation) is None or numbers["ts"] >= float(cancellation)):
                return None, "source_fill_after_effective_cancel"
            quantities[source["id"]] += numbers["quantity"]
            if quantities[source["id"]] > source["quantity"]+1e-8:
                return None, "source_entry_overfill"
            entries.append(fill)
        elif (fill.get("liquidity") != "taker_depth_model" or fill.get("order_id") is not None
                or fill.get("source") not in ("risk_flatten", "profitable_pair_hedge")):
            return None, "unknown_source_management_fill"
        fills.append(fill)
        turnover += numbers["quantity"]
        cash["cost_usd"] += numbers["quantity"]*numbers["price"]
        cash["fees_usd"] += numbers["fee_usd"]
        cash["payout_usd"] += numbers["quantity"]*(fill["side"] == official)
    cash["net_usd"] = cash["payout_usd"]-cash["cost_usd"]-cash["fees_usd"]
    if (len(fills) != outcome["metadata"]["fills"]
            or abs(turnover-outcome["metadata"]["turnover_quantity"]) > 1e-7
            or any(abs(cash[k]-float(outcome[k])) > 1e-7 for k in _CASH)):
        return None, "source_ledger_does_not_reconcile_to_settlement"
    if fills and not entries:
        return None, "management_without_entry_inventory"
    return {"orders": orders, "raw_orders": raw_orders, "fills": fills, "entries": entries,
            "cashflows": cash, "turnover_quantity": turnover}, None


def evaluate_episode(row):
    """Return conservative eligibility and both arms for one registered market."""
    if row.get("outcome") is None:
        return {"status": "pending", "reason": "awaiting_official_source_settlement"}
    if row.get("source_ledger_final") is not True:
        # The compact settlement may reach source state before the compressed
        # full-life archive. An outstanding archive is an unresolved outcome,
        # not a permanently failed classification and never an avoided loss.
        return {"status": "pending", "reason": "awaiting_complete_source_ledger"}
    candidate = row.get("candidate", {})
    if row.get("issues") or row.get("source_quote_changed") or row.get("source_ledger_changed"):
        return _reject("source_or_timing_issue")
    error = _feature_error(candidate, row.get("features", {}))
    if error:
        return _reject(error)
    initial = _classification(row.get("decision"))
    if initial not in _CLASSIFIED:
        return _reject("initial_valuation_unclassified")
    expected = decide(candidate, row["features"], {})["decision"]
    if initial != expected:
        return _reject("initial_decision_changed_or_inconsistent")
    error = _publication_error(row) or _outcome_error(row)
    if error:
        return _reject(error)
    source, error = _ledger(row)
    if error:
        return _reject(error)
    initial_pub = float(row["decision_published_at"])
    latency = _finite(row.get("minimum_publication_latency_seconds", .5))
    if latency != .5:
        return _reject("unregistered_publication_latency")
    if any(float(f["ts"]) <= initial_pub for f in source["fills"]):
        return _reject("source_fill_precedes_initial_publication")
    accepted, skipped = set(), set()
    if initial == "skip":
        skipped = set(source["orders"])
    else:
        for oid, terms in source["orders"].items():
            expected = decide_quote(candidate, row["decision"], terms)
            record = row.get("quotes", {}).get(oid)
            # The immutable absolute deadline was already published before
            # this new order existed; observation delay cannot reset it.
            if expected["decision"] == "skip" and initial_pub < terms["created_ts"]:
                if record is not None:
                    try:
                        if (quote_source(record["source"], candidate["ticker"]) != terms
                                or record.get("source_fingerprint") != quote_fingerprint(terms)
                                or _classification(record.get("decision")) != "skip"):
                            return _reject("source_quote_revised_or_decision_inconsistent")
                    except (KeyError, TypeError, ValueError):
                        return _reject("invalid_recorded_quote")
                skipped.add(oid)
                continue
            if expected["decision"] != "accept" or not isinstance(record, dict):
                return _reject("missing_or_unclassified_entry_quote")
            try:
                if (quote_source(record["source"], candidate["ticker"]) != terms
                        or record.get("source_fingerprint") != quote_fingerprint(terms)
                        or _classification(record.get("decision")) != "accept"):
                    return _reject("source_quote_revised_or_decision_inconsistent")
            except (KeyError, TypeError, ValueError):
                return _reject("invalid_recorded_quote")
            observed, published = _finite(record.get("observed_at")), _finite(record.get("decision_published_at"))
            cancellation = source["raw_orders"][oid].get("cancel_ts", source["raw_orders"][oid].get("cancel_effective_ts"))
            if cancellation is not None and _finite(cancellation) is None:
                return _reject("invalid_source_cancellation")
            end = min(terms["expires_ts"], float(cancellation)) if cancellation is not None else terms["expires_ts"]
            if (observed is None or published is None
                    or not terms["created_ts"] <= observed <= published < end
                    or published < initial_pub or published > candidate["decision_ts"]+60):
                return _reject("entry_quote_publication_unproven_or_late")
            effective = published+latency
            if effective >= end:
                return _reject("entry_quote_effective_after_source_expiry_or_cancel")
            stored_effective = record.get("decision_effective_at")
            if stored_effective is not None and _finite(stored_effective) != effective:
                return _reject("entry_quote_effective_timestamp_inconsistent")
            if any(float(f["ts"]) <= effective for f in source["entries"] if f["order_id"] == oid):
                return _reject("source_fill_precedes_quote_publication")
            accepted.add(oid)
    retained = [f for f in source["entries"] if f["order_id"] in accepted]
    if retained and len(retained) != len(source["entries"]):
        return _reject("partial_inventory_unidentified", retained_entry_fills=len(retained),
                       source_entry_fills=len(source["entries"]))
    same = bool(retained) or not source["entries"] and initial == "accept"
    zero = dict.fromkeys(_CASH, 0.0)
    return {"status": "qualified", "reason": "all_entry_inventory_identical" if same else "no_retained_entry_inventory",
            "inventory_identity": "same_entry_inventory" if same else "no_entry_inventory",
            "source": {**source["cashflows"], "fills": len(source["fills"]),
                       "entry_fills": len(source["entries"]), "turnover_quantity": source["turnover_quantity"]},
            "candidate": {**(source["cashflows"] if same else zero),
                          "fills": len(source["fills"]) if same else 0,
                          "entry_fills": len(retained),
                          "turnover_quantity": source["turnover_quantity"] if same else 0.0},
            "accepted_quotes": len(accepted), "skipped_quotes": len(skipped),
            "initial_decision_published_at": initial_pub,
            "deadline_precommitted_at": initial_pub,
            "independent_live_profit": False}


def summarize(episodes):
    """Main register plus matched common-cohort economics; unknown is not zero."""
    rows = [row for row in episodes.values() if row.get("candidate", {}).get("strategy") == "w10"]
    evaluated = [(row, evaluate_episode(row)) for row in rows]
    qualified = [(row, value) for row, value in evaluated if value["status"] == "qualified"]
    exclusions = Counter(value["reason"] for _, value in evaluated if value["status"] == "unclassified")
    windows = defaultdict(lambda: {"parent": 0.0, "candidate": 0.0})
    for row, value in qualified:
        window = windows[float(row["candidate"]["expires_ts"])]
        window["parent"] += value["source"]["net_usd"]
        window["candidate"] += value["candidate"]["net_usd"]
    arms = {}
    for arm, key in (("parent", "source"), ("candidate", "candidate")):
        values = [value[key] for _, value in qualified]
        filled = [value for value in values if value["fills"] > 0]
        wins = sum(value["net_usd"] > 0 for value in filled)
        arms[arm] = {**{k: sum(value[k] for value in values) for k in _CASH},
            "filled_episodes": len(filled), "unfilled_episodes": len(values)-len(filled),
            "winning_filled_episodes": wins,
            "losing_filled_episodes": sum(value["net_usd"] < 0 for value in filled),
            "flat_filled_episodes": sum(value["net_usd"] == 0 for value in filled),
            "filled_win_rate": wins/len(filled) if filled else None,
            "fills": sum(value["fills"] for value in values),
            "entry_fills": sum(value["entry_fills"] for value in values),
            "turnover_quantity": sum(value["turnover_quantity"] for value in values),
            "filled_expiry_windows": len({row["candidate"]["expires_ts"] for row, value in qualified if value[key]["fills"]}),
            "window_risk": _risk([value[arm] for _, value in sorted(windows.items())])}
    avoided = sum(-min(value["source"]["net_usd"], 0) for _, value in qualified if value["inventory_identity"] == "no_entry_inventory")
    sacrificed = sum(max(value["source"]["net_usd"], 0) for _, value in qualified if value["inventory_identity"] == "no_entry_inventory")
    return {"policy": POLICY_VERSION, "mode": MODE, "episodes": len(rows),
        "settled_episodes": sum(row.get("outcome") is not None for row in rows),
        "pending_episodes": sum(value["status"] == "pending" for _, value in evaluated),
        "awaiting_source_settlement_episodes": sum(value["reason"] == "awaiting_official_source_settlement" for _, value in evaluated),
        "awaiting_complete_source_ledger_episodes": sum(value["reason"] == "awaiting_complete_source_ledger" for _, value in evaluated),
        "initial_accept_episodes": sum(_classification(row.get("decision")) == "accept" for row in rows),
        "initial_reject_episodes": sum(_classification(row.get("decision")) == "skip" for row in rows),
        "initial_unclassified_episodes": sum(_classification(row.get("decision")) not in _CLASSIFIED for row in rows),
        "matched_settled_episodes": len(qualified), "matched_settled_expiry_windows": len(windows),
        "excluded_settled_episodes": sum(exclusions.values()), "exclusion_reasons": dict(sorted(exclusions.items())),
        "matched_control_net_usd": arms["parent"]["net_usd"],
        "matched_candidate_net_usd": arms["candidate"]["net_usd"],
        "matched_difference_usd": arms["candidate"]["net_usd"]-arms["parent"]["net_usd"],
        "avoided_loss_usd": avoided, "sacrificed_profit_usd": sacrificed,
        "arms": arms, "window_risk": {key: value["window_risk"] for key, value in arms.items()},
        "expiry_windows": [{"expires_ts": expiry, **value} for expiry, value in sorted(windows.items())],
        "qualified_accepted_quotes": sum(value["accepted_quotes"] for _, value in qualified),
        "qualified_skipped_quotes": sum(value["skipped_quotes"] for _, value in qualified),
        "management_gap_episodes": sum(bool(row.get("outcome", {}).get("metadata", {}).get("coverage_gap")) for row, _ in qualified),
        "independent_live_profit": False,
        "execution_semantics": MODE,
        "allocation_semantics": "identical_source_entry_inventory_inherits_all_management_else_zero_or_unidentified;no_freed_risk_redistribution",
        "timing_semantics": "actual_durable_publication_required_for_accepts;absolute_deadline_precommitted_for_later_creation_rejections",
        "historical_research_pnl_included": False}
