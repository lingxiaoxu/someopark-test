"""Pure, separately recorded W9 flow/budget/daily-tuned-RNN paper policy.

No filesystem, model loader, live strategy, or exchange client imports. The
caller supplies COMPLETE W7 atomic-state entry batches, PIT features, previously
published model forecasts, and source outcomes. Returned dictionaries are new
copies and can be atomically persisted by the caller.

This is conditional paper exposure accounting, not exchange execution. Unknown
flow/model evidence retains the known controls; it never silently means a fully
classified signal. The main book contains all registered signals. Its
full-feature attribution preserves the quantities allocated in that main book.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import math
from typing import Mapping

POLICY_VERSION = "flow_budget50_daily_tuned_rnn_risk_half_v1"
RESEARCH_VARIANT = "flow_budget50_tuned_rnn_risk_half"
SCHEMA_VERSION = 1
BASE_CONTRACTS = 25
RISK_CONTRACTS = 12
COMMON_DIRECTION_BUDGET_USD = 50.0
MAX_PREDICTION_AGE_SECONDS = 60.0


class LateBatchError(ValueError):
    """An unseen earlier/equal-time entry would change a committed batch."""


class SourceRevisionError(ValueError):
    """A source identity or settled amount changed after it was recorded."""


def _number(value, label="number"):
    if isinstance(value, bool):
        raise ValueError(label + " must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(label + " must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(label + " must be finite")
    return result


def _timestamp(value, label="timestamp"):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
            value = parsed.timestamp()
        except ValueError as exc:
            raise ValueError(label + " must be timezone-qualified") from exc
    result = _number(value, label)
    if result < 0:
        raise ValueError(label + " must be nonnegative")
    return result


def source_fee_per_contract(cost):
    """W7's rounded continuous paper fee, inferable from entry price alone.

    W7 rounds the per-contract net cents to two decimals. Both win and loss
    outcomes imply this fee at supported four-decimal prices. We never read a
    future settlement to decide the entry's budget. It is not an exchange fee.
    """
    price = _number(cost, "entry_price")
    if not .78 <= price <= .98 or abs(price - round(price, 4)) > 1e-10:
        raise ValueError("entry price must be W7 MAIN four-decimal paper price")
    return max(0.0, -price - round((-price - .07 * price * (1-price)) * 100, 2) / 100)


def cent_rounded_fee(contracts, cost):
    """Separate fee sensitivity; never the primary research accounting."""
    if contracts == 0:
        return 0.0
    price = Decimal(str(cost))
    return float((Decimal(".07") * int(contracts) * price * (1-price)).quantize(
        Decimal(".01"), rounding=ROUND_CEILING))


def _check_candidate(candidate, parent_binding=None):
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate must be a mapping")
    if not isinstance(candidate.get("id"), str) or not candidate["id"]:
        raise ValueError("candidate id required")
    if candidate.get("strategy") != "w9" or candidate.get("side") not in ("yes", "no"):
        raise ValueError("only W9 source YES/NO candidates are allowed")
    if candidate.get("asset") not in ("BTC", "ETH", "SOL", "DOGE", "XRP"):
        raise ValueError("asset outside W7 MAIN universe")
    if not isinstance(candidate.get("ticker"), str) or not candidate["ticker"]:
        raise ValueError("exact source ticker required")
    if not isinstance(candidate.get("parent_binding"), Mapping):
        raise ValueError("parent binding required")
    if parent_binding is not None and dict(candidate["parent_binding"]) != dict(parent_binding):
        raise SourceRevisionError("candidate parent binding changed")
    decision = _timestamp(candidate.get("decision_ts"), "decision_ts")
    expiry = _timestamp(candidate.get("expires_ts"), "expires_ts")
    if decision >= expiry:
        raise ValueError("entry must precede exact contract expiry")
    meta = candidate.get("metadata", {})
    if meta.get("parent_leg") != "MAIN" or _number(meta.get("contracts")) != BASE_CONTRACTS:
        raise ValueError("candidate must inherit 25-contract MAIN paper entry")
    source_fee_per_contract(meta.get("entry_price"))
    return decision, expiry


def decision_only(candidate, features=None, prediction=None):
    """Determine controls/cap before chronological common-direction allocation."""
    at, _ = _check_candidate(candidate)
    features, prediction = features or {}, prediction or {}
    flow_known = False
    flow_errors = []
    aligned_flow = aligned_momentum = None
    try:
        if features.get("valid") is not True or features.get("flow_valid") is not True:
            raise ValueError("incomplete_causal_price_flow")
        if abs(_timestamp(features.get("decision_ts")) - at) > 1e-6:
            raise ValueError("flow_decision_timestamp_mismatch")
        momentum = _number(features.get("momentum_1m_bp"), "momentum")
        flow = _number(features.get("observed_flow_imbalance_1m"), "flow")
        if not -1 <= flow <= 1:
            raise ValueError("flow_imbalance_out_of_range")
        for key in ("latest_feature_timestamp", "feature_last_recorded_receipt_ts"):
            if key in features and _timestamp(features[key]) > at:
                raise ValueError("future_flow_receipt")
        sign = 1 if candidate["side"] == "yes" else -1
        aligned_flow, aligned_momentum = sign*flow, sign*momentum
        flow_known = True
    except (TypeError, ValueError) as exc:
        flow_errors.append(str(exc))
    flow_skip = flow_known and aligned_momentum < 0 and aligned_flow <= -.5

    model_known = False
    model_errors = []
    risk = threshold = prediction_ts = None
    try:
        if prediction.get("strict_recorded_pit_eligible") is not True:
            raise ValueError("model_not_recorded_pit_eligible")
        if prediction.get("risk_available") is not True:
            raise ValueError("model_risk_not_available")
        if prediction.get("asset") != candidate["asset"]:
            raise ValueError("model_asset_mismatch")
        if not isinstance(prediction.get("model_id"), str) or not prediction["model_id"]:
            raise ValueError("model_id_missing")
        prediction_ts = _timestamp(prediction.get("decision_ts"), "prediction_ts")
        if not 0 <= at-prediction_ts <= MAX_PREDICTION_AGE_SECONDS:
            raise ValueError("prediction_future_or_older_than60s")
        times = {key: _timestamp(prediction.get(key), key) for key in (
            "generated_at", "persisted_at", "model_available_ts", "feature_last_recorded_receipt_ts")}
        if any(value > at for value in times.values()):
            raise ValueError("prediction_not_published_or_inputs_unavailable_at_entry")
        if times["feature_last_recorded_receipt_ts"] > prediction_ts:
            raise ValueError("prediction_contains_future_input")
        if not times["model_available_ts"] <= times["generated_at"] <= times["persisted_at"]:
            raise ValueError("model_publication_order_invalid")
        if times["generated_at"] < prediction_ts:
            raise ValueError("prediction_generated_before_nominal_decision")
        risk = _number(prediction.get("pred_abs_return_augmented"), "risk")
        threshold = _number(prediction.get("prior_calibration_risk_q90_augmented"), "risk_q90")
        if risk < 0 or threshold < 0:
            raise ValueError("risk_or_threshold_negative")
        model_known = True
    except (TypeError, ValueError) as exc:
        model_errors.append(str(exc))
    risk_high = model_known and risk > threshold
    cap = 0 if flow_skip else (RISK_CONTRACTS if risk_high else BASE_CONTRACTS)
    return {
        "policy_version": POLICY_VERSION, "research_variant": RESEARCH_VARIANT,
        "flow_known": flow_known, "model_known": model_known,
        "full_feature_known": flow_known and model_known,
        "flow_skip": bool(flow_skip), "rnn_risk_high": bool(risk_high),
        "flow_unknown_reasons": flow_errors, "model_unknown_reasons": model_errors,
        "aligned_momentum_1m_bp": aligned_momentum,
        "aligned_observed_flow_imbalance_1m": aligned_flow,
        "pred_abs_return_augmented": risk, "prior_calibration_risk_q90_augmented": threshold,
        "prediction_ts": prediction_ts, "model_id": prediction.get("model_id"),
        "pre_budget_cap": cap,
        "unknown_semantics": "retain_known_controls_and_record_incomplete_features",
    }


def new_state(registration_ts, parent_binding):
    if not isinstance(parent_binding, Mapping) or parent_binding.get("source_strategy") != "w7_noisefade":
        raise ValueError("W9 requires independently bound W7 source")
    return {
        "schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION,
        "research_variant": RESEARCH_VARIANT,
        "registered_at": _timestamp(registration_ts),
        "parent_binding": deepcopy(dict(parent_binding)),
        "mode": "paper_only", "last_decision_ts": None,
        "spent_by_bucket": {}, "episodes": {},
        "accounting": "parent_rounded_paper_net_proportional_to_quantity",
    }


def _check_state(state):
    if state.get("schema_version") != SCHEMA_VERSION or state.get("policy_version") != POLICY_VERSION:
        raise ValueError("different policy/schema state cannot be reused")
    if state.get("mode") != "paper_only":
        raise ValueError("W9 only supports isolated paper state")


def _bucket(expiry, side):
    return f"{expiry:.6f}:{side}"


def apply_batch(state, candidates, flow_features_by_id=None, model_predictions_by_id=None, *, observed_at):
    """Commit complete chronological source batches; return (state, new rows).

    ``candidates`` may contain previously persisted IDs (idempotent replay), but
    all new members of the same timestamp must be supplied together. A later
    signal cannot alter earlier allocations. No budget is returned at settlement.
    The caller must source this from the parent's atomic state snapshot after a
    complete cycle, not process individual log entries as separate batches.
    """
    _check_state(state)
    result = deepcopy(state)
    observed = _timestamp(observed_at)
    flow_features_by_id = flow_features_by_id or {}
    model_predictions_by_id = model_predictions_by_id or {}
    unseen = {}
    for candidate in candidates:
        at, expiry = _check_candidate(candidate, result["parent_binding"])
        if at < _timestamp(result["registered_at"]):
            raise ValueError("candidate predates W9 registration")
        if at > observed:
            raise ValueError("cannot observe a future candidate")
        identity = candidate["id"]
        old = result["episodes"].get(identity)
        if old is not None:
            if old["candidate"] != dict(candidate):
                raise SourceRevisionError("source candidate changed after recording")
            continue
        if identity in unseen:
            if unseen[identity] != dict(candidate):
                raise SourceRevisionError("conflicting candidate duplicates")
            continue
        if observed >= expiry:
            raise LateBatchError("unseen entry first observed at/after expiry")
        watermark = result.get("last_decision_ts")
        if watermark is not None and at <= _timestamp(watermark):
            raise LateBatchError("unseen entry at/before committed complete-batch watermark")
        unseen[identity] = deepcopy(dict(candidate))
    groups = defaultdict(list)
    for candidate in unseen.values():
        groups[(candidate["decision_ts"], candidate["expires_ts"], candidate["side"])].append(candidate)
    added = []
    for (at, expiry, side), rows in sorted(groups.items()):
        rows.sort(key=lambda candidate: candidate["ticker"])
        decisions = {row["id"]: decision_only(row, flow_features_by_id.get(row["id"]),
                                              model_predictions_by_id.get(row["id"])) for row in rows}
        admitted = [row for row in rows if decisions[row["id"]]["pre_budget_cap"] > 0]
        bucket = _bucket(expiry, side)
        spent = _number(result["spent_by_bucket"].get(bucket, 0.0), "spent_budget")
        if not 0 <= spent <= COMMON_DIRECTION_BUDGET_USD + 1e-8:
            raise ValueError("persisted budget is invalid")
        available = max(0.0, COMMON_DIRECTION_BUDGET_USD-spent)
        n = min((decisions[row["id"]]["pre_budget_cap"] for row in admitted), default=0)
        unit_total = sum(row["metadata"]["entry_price"] + source_fee_per_contract(
            row["metadata"]["entry_price"]) for row in admitted)
        while n > 0 and n*unit_total > available+1e-9:
            n -= 1
        result["spent_by_bucket"][bucket] = spent+n*unit_total
        for row in rows:
            identity = row["id"]
            decision = decisions[identity]
            qty = n if decision["pre_budget_cap"] > 0 else 0
            price = row["metadata"]["entry_price"]
            fee = source_fee_per_contract(price)
            decision.update({
                "quantity": qty, "decision": "accept" if qty > 0 else "skip",
                "reason": "adverse_price_and_observed_flow" if decision["flow_skip"] else
                          "common_direction_budget_exhausted" if qty == 0 else
                          "rnn_risk_and_common_direction_budget" if decision["rnn_risk_high"] else
                          "common_direction_budget",
                "batch_decision_ts": at, "batch_admitted_members": len(admitted),
                "batch_common_quantity": n, "budget_bucket": bucket,
                "bucket_spent_before_usd": spent,
                "bucket_spent_after_usd": spent+n*unit_total,
                "entry_cost_usd": qty*price, "entry_paper_fee_usd": qty*fee,
                "entry_total_cost_usd": qty*(price+fee),
                "parent_contracts": BASE_CONTRACTS,
                "source_paper_fee_per_contract_usd": fee,
                "fees_basis": "W7_rounded_continuous_paper_fee_not_exchange_fee",
            })
            episode = {"id": identity, "candidate": row, "features": deepcopy(flow_features_by_id.get(identity, {})),
                       "prediction": deepcopy(model_predictions_by_id.get(identity, {})),
                       "observed_at": observed, "decision": decision, "outcome": None}
            result["episodes"][identity] = episode
            added.append(deepcopy(episode))
        result["last_decision_ts"] = max(float(at), float(result.get("last_decision_ts") or 0))
    return result, added


def settle(state, outcomes):
    """Scale actual parent paper outcomes; idempotent and never recycle budget."""
    _check_state(state)
    result = deepcopy(state)
    added = []
    for outcome in outcomes:
        if not isinstance(outcome, Mapping) or outcome.get("id") not in result["episodes"]:
            continue
        episode = result["episodes"][outcome["id"]]
        candidate = episode["candidate"]
        for key in ("id", "strategy", "ticker", "asset", "expires_ts", "parent_binding"):
            if outcome.get(key) != candidate.get(key):
                raise SourceRevisionError("outcome differs from bound source " + key)
        if episode.get("outcome") is not None:
            if episode["outcome"]["parent_outcome"] != dict(outcome):
                raise SourceRevisionError("source settlement revised after recording")
            continue
        if _timestamp(outcome.get("closed_ts")) < candidate["expires_ts"]:
            raise ValueError("settlement precedes expiry")
        meta = outcome.get("metadata", {})
        if _number(meta.get("contracts")) != BASE_CONTRACTS or not isinstance(meta.get("parent_recorded_win"), bool):
            raise ValueError("source 25-contract paper win/quantity required")
        amounts = {key: _number(outcome.get(key), key) for key in (
            "cost_usd", "fees_usd", "payout_usd", "net_usd")}
        if any(amounts[key] < -1e-9 for key in ("cost_usd", "fees_usd", "payout_usd")):
            raise ValueError("source paper cost/fee/payout cannot be negative")
        if abs(amounts["payout_usd"]-amounts["cost_usd"]-amounts["fees_usd"]-amounts["net_usd"]) > 1e-7:
            raise ValueError("source paper outcome does not reconcile")
        price = candidate["metadata"]["entry_price"]
        expected_fee = source_fee_per_contract(price)*BASE_CONTRACTS
        if (abs(amounts["cost_usd"]-price*BASE_CONTRACTS) > 1e-7 or
                abs(amounts["fees_usd"]-expected_fee) > 1e-7 or
                abs(amounts["payout_usd"]-BASE_CONTRACTS*int(meta["parent_recorded_win"])) > 1e-7):
            raise SourceRevisionError("source paper settlement disagrees with frozen entry cost/fee/win")
        qty = episode["decision"]["quantity"]
        scaled = {key: value*qty/BASE_CONTRACTS for key, value in amounts.items()}
        paper = {
            "id": candidate["id"], "ticker": candidate["ticker"],
            "closed_ts": outcome["closed_ts"], "expires_ts": candidate["expires_ts"],
            "quantity": qty, "win": meta["parent_recorded_win"], **scaled,
            "parent_net_usd": amounts["net_usd"],
            "delta_vs_parent_usd": scaled["net_usd"]-amounts["net_usd"],
            "cent_fee_sensitivity_net_usd": qty*(int(meta["parent_recorded_win"])-price)-cent_rounded_fee(qty, price),
            "parent_outcome": deepcopy(dict(outcome)),
            "accounting": "parent_rounded_paper_net_proportional_to_quantity",
            "independent_exchange_execution": False,
        }
        episode["outcome"] = paper
        added.append(deepcopy(paper))
    return result, added


def _metrics(episodes):
    settled = [row for row in episodes if row.get("outcome") is not None]
    active = [row for row in settled if row["decision"]["quantity"] > 0]
    windows = defaultdict(lambda: [0.0, 0.0])
    for row in settled:
        outcome = row["outcome"]
        window = windows[row["candidate"]["expires_ts"]]
        window[0] += outcome["net_usd"]
        window[1] += outcome["parent_net_usd"]
    def dd(index):
        current = peak = worst = 0.0
        for _, pair in sorted(windows.items()):
            current += pair[index]
            peak = max(peak, current)
            worst = min(worst, current-peak)
        return worst
    contracts = sum(row["decision"]["quantity"] for row in active)
    return {
        "signals": len(episodes), "settled_signals": len(settled),
        "pending_signals": len(episodes)-len(settled),
        "traded_settled_signals": len(active), "contracts_settled": contracts,
        "winning_trades": sum(row["outcome"]["win"] for row in active),
        "trade_win_rate": sum(row["outcome"]["win"] for row in active)/len(active) if active else None,
        "contract_weighted_win_rate": sum(row["decision"]["quantity"] for row in active if row["outcome"]["win"])/contracts if contracts else None,
        "net_pnl_usd": sum(row["outcome"]["net_usd"] for row in settled),
        "parent_net_pnl_usd": sum(row["outcome"]["parent_net_usd"] for row in settled),
        "max_drawdown_usd": dd(0), "parent_max_drawdown_usd": dd(1),
        "expiry_windows": len(windows),
        "flow_unknown_signals": sum(not row["decision"]["flow_known"] for row in episodes),
        "model_unknown_signals": sum(not row["decision"]["model_known"] for row in episodes),
        "rnn_risk_high_signals": sum(row["decision"]["rnn_risk_high"] for row in episodes),
        "flow_skipped_signals": sum(row["decision"]["flow_skip"] for row in episodes),
    }


def summarize(state):
    _check_state(state)
    rows = list(state["episodes"].values())
    return {
        "policy_version": POLICY_VERSION, "registered_at": state["registered_at"],
        "all_registered": _metrics(rows),
        "full_feature_attribution": _metrics([row for row in rows if row["decision"]["full_feature_known"]]),
        "full_feature_semantics": "subset_attribution_of_actual_allocations_not_reallocated_subcohort",
        "mode": "paper_only", "independent_exchange_execution": False,
    }
