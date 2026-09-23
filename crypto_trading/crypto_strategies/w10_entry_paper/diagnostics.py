"""Append-only quote revalidation diagnostics; never an execution/PnL model.

The parent tilted book creates only passive entry orders. Risk reductions are
separate taker fills without an order ID. Read the supplied snapshot and reuse
the caller's causal recorder features; no strategy imports, I/O or API calls.
Finding a quote after its creation does not prove a usable pre-trade decision.
Reported fills may themselves arrive late, so absence is never proof of no fill.
"""
from copy import deepcopy
from hashlib import sha256
import json
import math

from crypto_trading.crypto_strategies.downside_paper.sources import _ticker, _timestamp


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean numeric field")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("nonfinite numeric field")
    return value


def _gate(features, side, decision_ts):
    result = {"classification": "unclassified", "aligned_momentum_1m_bp": None,
              "aligned_observed_flow_imbalance_1m": None}
    if features.get("valid") is not True or features.get("flow_valid") is not True:
        return {**result, "reason": "missing_valid_price_or_sampled_flow"}
    try:
        if abs(_number(features["decision_ts"]) - decision_ts) > 1e-6:
            raise ValueError("feature time differs from quote creation")
        momentum = _number(features["momentum_1m_bp"])
        flow = _number(features["observed_flow_imbalance_1m"])
        if not -1 <= flow <= 1:
            raise ValueError("flow imbalance out of range")
    except (KeyError, TypeError, ValueError, OverflowError):
        return {**result, "reason": "invalid_or_mistimed_features"}
    direction = 1 if side == "yes" else -1
    return {"classification": "accept" if direction*momentum > 0 and direction*flow > 0 else "skip",
            "aligned_momentum_1m_bp": direction*momentum,
            "aligned_observed_flow_imbalance_1m": direction*flow,
            "reason": "strict_positive_aligned_price_and_sampled_flow"}


def collect_quote_diagnostics(parent_state, excluded_tickers, since_ts, until_ts,
                              observed_ts, existing_records, feature_fn):
    """Return {'records': new append-only events, 'errors': structured errors}.

    ``existing_records`` maps each prior event's ``id`` to its entire record.
    Persist new records by ID. Never replace an earlier quote record. Source
    creation-field changes produce a separate ``quote_source_revision`` event;
    mutable queue/remaining/cancellation fields are deliberately not revisions.
    ``feature_fn(asset, created_ts)`` must return ``compute_features`` output.
    Newly reported fill evidence is appended even if its exchange timestamp
    predates our first observation. There is intentionally no calculated PnL.

    Coverage is the active source snapshot, not a replay of already removed
    settled orders. The caller separately monitors read gaps and parent binding.
    """
    since, until, observed = map(_timestamp, (since_ts, until_ts, observed_ts))
    if since > until or observed < until:
        raise ValueError("invalid diagnostic observation interval")
    book = parent_state.get("books", {}).get("tilted", {})
    if book.get("complete_sets") is not False:
        raise ValueError("diagnostics require the tilted non-completion source book")
    registration = _timestamp(parent_state["registered_at"])
    identity = {"registered_at": registration, "version": parent_state["version"], "book": "tilted"}
    original = {r["quote_key"]: r for r in existing_records.values()
                if r.get("event_type") == "quote_observed"}
    known = set(existing_records)
    out, errors = [], []

    def append(record):
        if record["id"] not in known:
            known.add(record["id"])
            out.append(record)

    for ticker, market in sorted(book.get("positions", {}).items()):
        if ticker in excluded_tickers:
            continue
        try:
            asset, close = _ticker(ticker, "w8")
            if market.get("series") != "KX"+asset+"15M" or abs(_timestamp(market["close_ts"])-close) > 1e-6:
                raise ValueError("market identity mismatch")
            orders = market.get("orders", [])
            ids = [o.get("id") for o in orders]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate source order ID")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            errors.append({"ticker": ticker, "error": str(exc)})
            continue
        for order in orders:
            try:
                oid = order["id"]
                sequence = int(oid.removeprefix(ticker+":"))
                if oid != ticker+":"+str(sequence) or sequence < 1:
                    raise ValueError("invalid source order identity")
                key = "w10quote:"+_hash({**identity, "ticker": ticker, "order_id": oid})[:32]
                # Current source exits never enter orders[]. Reject explicit
                # exit/taker annotations if a later source schema introduces them.
                if (order.get("source") in ("risk_flatten", "profitable_pair_hedge")
                        or order.get("role") in ("exit", "hedge")
                        or order.get("liquidity") == "taker_depth_model"):
                    continue
                created = _timestamp(order["created_ts"])
                # An existing quote's timestamp moving outside the admission
                # interval is a source revision, not permission to hide it.
                if not max(since, registration) <= created <= until and key not in original:
                    continue
                price, quantity = _number(order["price"]), _number(order["quantity"])
                activated, expires = _timestamp(order["activate_ts"]), _timestamp(order["expires_ts"])
                side = order["side"]
                if not (side in ("yes", "no") and 0 < price < 1 and quantity > 0
                        and created <= activated < expires <= close):
                    raise ValueError("invalid source quote terms")
                fields = dict(ticker=ticker, order_id=oid, asset=asset, side=side,
                              price=price, quantity=quantity, created_ts=created,
                              activate_ts=activated, expires_ts=expires)
                fingerprint = _hash(fields)
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
                errors.append({"ticker": ticker, "order_id": order.get("id"), "error": str(exc)})
                continue
            base = {"quote_key": key, "ticker": ticker, "asset": asset, "order_id": oid,
                    "diagnostic_only": True, "pnl_eligible": False, "observed_ts": observed}
            old = original.get(key)
            if old is None:
                try:
                    features = deepcopy(feature_fn(asset, created))
                    if not isinstance(features, dict):
                        raise TypeError("features must be a dictionary")
                    # Fail closed on non-serializable/nonfinite feature payloads.
                    _hash(features)
                except Exception as exc:
                    features = {"valid": False, "flow_valid": False, "decision_ts": created,
                                "errors": ["feature_evaluation_failed:"+type(exc).__name__]}
                old = {**base, "id": key+":observed", "event_type": "quote_observed",
                       "source": fields, "source_fingerprint": fingerprint,
                       "quote_stage": "first" if sequence == 1 else "replacement",
                       "source_order_sequence": sequence, "first_observed_ts": observed,
                       "observation_lag_seconds": observed-created, "features": features,
                       "rule": "aligned_price_1m_gt0_and_sampled_flow_1m_gt0",
                       "execution_claim": "diagnostic_only_not_pretrade_or_independent_execution",
                       "fill_timing_claim": "absence_of_reported_fill_never_proves_observed_before_fill",
                       **_gate(features, side, created)}
                append(old)
                original[key] = old
            elif old["source_fingerprint"] != fingerprint:
                append({**base, "id": key+":revision:"+fingerprint[:24],
                        "event_type": "quote_source_revision", "first_observed_ts": old["first_observed_ts"],
                        "original_source_fingerprint": old["source_fingerprint"],
                        "revised_source_fingerprint": fingerprint, "revised_source": fields,
                        "reason": "immutable_quote_creation_fields_changed_no_reclassification"})
            # Report only actual source entry fills associated with this quote.
            # Flatten/hedge fills have no matching order ID and stay out.
            for fill in market.get("fills", []):
                if fill.get("order_id") != oid or fill.get("liquidity") != "maker_model":
                    continue
                try:
                    fill_ts = _timestamp(fill["ts"])
                    if fill_ts > until:
                        continue
                    quantity, price = _number(fill["quantity"]), _number(fill["price"])
                    if quantity <= 0 or not 0 < price < 1 or fill_ts < created:
                        raise ValueError("invalid diagnostic fill evidence")
                    evidence = {k: deepcopy(fill.get(k)) for k in
                                ("order_id", "ts", "side", "price", "quantity", "fee_usd", "liquidity", "source")}
                    digest = _hash(evidence)
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    errors.append({"ticker": ticker, "order_id": oid, "error": str(exc)})
                    continue
                append({**base, "id": key+":fill:"+digest[:24], "event_type": "quote_fill_reported",
                        "fill": evidence, "first_quote_observed_ts": old["first_observed_ts"],
                        "fill_precedes_or_equals_first_quote_observation": fill_ts <= old["first_observed_ts"],
                        "first_quote_observation_minus_fill_seconds": old["first_observed_ts"]-fill_ts,
                        "quote_source_revised": old["source_fingerprint"] != fingerprint,
                        "execution_claim": "parent_paper_fill_evidence_not_new_candidate_execution"})
    return {"records": out, "errors": errors}
