"""Prospective, causal first-quote gate on complete W8 tilted paper episodes.

An accepted episode inherits every parent replacement, fill, management exit,
fee and settlement. This is a conditional paper cohort, not an independent
execution simulator. Unclassified evidence never earns credit for avoiding a
loss. All three reported arms use exactly the same qualified settled cohort.
"""
from collections import Counter, defaultdict
import math

from ..downside_paper.policy import decide as _legacy_decide


POLICY_VERSION = "aligned_price_flow_v1"
_CLASSIFIED = frozenset(("accept", "skip"))


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _feature_error(candidate, features):
    if features.get("valid") is not True or features.get("flow_valid") is not True:
        return "incomplete_causal_features"
    if candidate.get("side") not in ("yes", "no"):
        return "unknown_contract_direction"
    decision = _finite(candidate.get("decision_ts"))
    feature_at = _finite(features.get("decision_ts"))
    if decision is None or decision <= 0 or feature_at != decision:
        return "feature_timestamp_not_parent_first_quote"
    momentum = _finite(features.get("momentum_1m_bp"))
    flow = _finite(features.get("observed_flow_imbalance_1m"))
    if momentum is None or flow is None:
        return "missing_price_flow_feature"
    if not -1 <= flow <= 1:
        return "observed_flow_imbalance_out_of_bounds"
    return None


def decide(candidate, features, parameters):
    """Require strictly positive direction-aligned one-minute price AND flow.

    Features must have been computed using the original first order timestamp;
    receiving the source order later does not permit a later feature cutoff.
    The existing causal feature builder owns per-record receipt/venue checks.
    """
    if parameters.get("policy", POLICY_VERSION) != POLICY_VERSION:
        return {"decision": "unclassified", "reason": "unsupported_policy_version"}
    error = _feature_error(candidate, features)
    if error:
        return {"decision": "unclassified", "reason": error}
    direction = 1 if candidate["side"] == "yes" else -1
    momentum = direction * float(features["momentum_1m_bp"])
    flow = direction * float(features["observed_flow_imbalance_1m"])
    keep = momentum > 0 and flow > 0
    return {
        "decision": "accept" if keep else "skip",
        "reason": "price_and_observed_flow_support_direction" if keep else "price_and_observed_flow_not_both_supportive",
        "aligned_momentum_1m_bp": momentum,
        "aligned_observed_flow_1m": flow,
    }


def legacy_decide(candidate, features, parameters):
    """The original W10 predicate, reused with identical causal eligibility."""
    error = _feature_error(candidate, features)
    if error:
        return {"decision": "unclassified", "reason": error}
    return _legacy_decide(candidate, features, parameters)


def _outcome_error(row):
    if row.get("source_outcome_changed") or row.get("source_candidate_changed"):
        return "source_revised"
    if row.get("pit_evidence_issue"):
        return "pit_evidence_issue"
    outcome = row["outcome"]
    metadata = outcome.get("metadata", {})
    if metadata.get("source_fill_verified") is not True:
        return "source_fill_unverified"
    unknown = _finite(metadata.get("unverified_order_quantity"))
    if unknown != 0:
        return "source_fill_unverified"
    amounts = {key: _finite(outcome.get(key)) for key in ("net_usd", "cost_usd", "fees_usd", "payout_usd")}
    if any(value is None for value in amounts.values()):
        return "invalid_source_cashflows"
    if any(amounts[key] < -1e-9 for key in ("cost_usd", "fees_usd", "payout_usd")):
        return "invalid_source_cashflows"
    if abs(amounts["payout_usd"] - amounts["cost_usd"] - amounts["fees_usd"] - amounts["net_usd"]) > 1e-7:
        return "invalid_source_cashflows"
    fills = _finite(metadata.get("fills"))
    turnover = _finite(metadata.get("turnover_quantity"))
    if fills is None or fills < 0 or fills != int(fills) or turnover is None or turnover < 0:
        return "invalid_source_fill_evidence"
    if metadata.get("zero_fill") is not (fills == 0):
        return "invalid_source_fill_evidence"
    if fills == 0 and (turnover != 0 or any(abs(x) > 1e-8 for x in amounts.values())):
        return "invalid_source_fill_evidence"
    candidate = row["candidate"]
    if any(outcome.get(key) != candidate.get(key) for key in ("id", "ticker", "expires_ts", "parent_binding")):
        return "source_identity_mismatch"
    expiry, closed = _finite(candidate.get("expires_ts")), _finite(outcome.get("closed_ts"))
    if expiry is None or closed is None or closed < expiry:
        return "invalid_source_settlement_time"
    return None


def _ratio(numerator, denominator):
    return numerator / denominator if denominator > 0 else None


def _risk(values):
    running = peak = drawdown = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)
    tail = sorted(values)[:max(1, math.ceil(len(values) * .05))]
    return {
        "realized_max_drawdown_usd": drawdown if values else None,
        "worst_expiry_window_usd": min(values) if values else None,
        "worst_5pct_mean_window_usd": sum(tail) / len(tail) if tail else None,
    }


def summarize(episodes):
    """Compare parent, legacy gate and new gate on one common settled cohort.

    Win rate means positive whole-episode NET cashflow divided by filled
    episodes. Quote retention and cashflow retention describe reduced activity;
    they are not a capital-normalized return or an independent portfolio ROI.
    Management gaps remain in the sample and are counted separately, because
    a historical gap is not itself evidence of an unverified modeled fill.
    """
    rows = [r for r in episodes.values() if r.get("candidate", {}).get("strategy") == "w10"]
    settled = [r for r in rows if r.get("outcome") is not None]
    exclusions = Counter()
    matched = []
    for row in settled:
        if (row.get("decision", {}).get("decision") not in _CLASSIFIED
                or row.get("legacy_decision", {}).get("decision") not in _CLASSIFIED):
            exclusions["unclassified_in_either_arm"] += 1
            continue
        error = _feature_error(row["candidate"], row.get("features", {})) or _outcome_error(row)
        metadata = row["candidate"].get("metadata", {})
        quantity, price = _finite(metadata.get("contracts")), _finite(metadata.get("entry_price"))
        if not error and (quantity is None or quantity <= 0 or price is None or not 0 < price < 1):
            error = "invalid_first_quote_allocation"
        if error:
            exclusions[error] += 1
        else:
            matched.append(row)

    windows = defaultdict(lambda: {"parent": 0.0, "legacy": 0.0, "candidate": 0.0})
    for row in matched:
        value = float(row["outcome"]["net_usd"])
        target = windows[float(row["candidate"]["expires_ts"])]
        target["parent"] += value
        if row["legacy_decision"]["decision"] == "accept":
            target["legacy"] += value
        if row["decision"]["decision"] == "accept":
            target["candidate"] += value

    parent_contracts = sum(float(r["candidate"]["metadata"]["contracts"]) for r in matched)
    parent_quote_notional = sum(float(r["candidate"]["metadata"]["contracts"]) * float(r["candidate"]["metadata"]["entry_price"]) for r in matched)
    parent_cost = sum(float(r["outcome"]["cost_usd"]) for r in matched)
    parent_filled = sum(not r["outcome"]["metadata"]["zero_fill"] for r in matched)
    arms = {}
    for name, decision_key in (("parent", None), ("legacy", "legacy_decision"), ("candidate", "decision")):
        kept = [r for r in matched if decision_key is None or r[decision_key]["decision"] == "accept"]
        skipped = [r for r in matched if decision_key is not None and r[decision_key]["decision"] == "skip"]
        filled = [r for r in kept if not r["outcome"]["metadata"]["zero_fill"]]
        winning = sum(float(r["outcome"]["net_usd"]) > 0 for r in filled)
        quote_contracts = sum(float(r["candidate"]["metadata"]["contracts"]) for r in kept)
        quote_notional = sum(float(r["candidate"]["metadata"]["contracts"]) * float(r["candidate"]["metadata"]["entry_price"]) for r in kept)
        cost = sum(float(r["outcome"]["cost_usd"]) for r in kept)
        arms[name] = {
            "kept_episodes": len(kept), "skipped_episodes": len(skipped),
            "filled_episodes": len(filled), "unfilled_episodes": len(kept) - len(filled),
            "winning_filled_episodes": winning,
            "losing_filled_episodes": sum(float(r["outcome"]["net_usd"]) < 0 for r in filled),
            "flat_filled_episodes": sum(float(r["outcome"]["net_usd"]) == 0 for r in filled),
            "filled_win_rate": _ratio(winning, len(filled)),
            "filled_expiry_windows": len({r["candidate"]["expires_ts"] for r in filled}),
            "net_usd": sum(float(r["outcome"]["net_usd"]) for r in kept),
            "inherited_cost_usd": cost,
            "inherited_fees_usd": sum(float(r["outcome"]["fees_usd"]) for r in kept),
            "inherited_turnover_quantity": sum(float(r["outcome"]["metadata"]["turnover_quantity"]) for r in kept),
            "avoided_loss_usd": -sum(min(float(r["outcome"]["net_usd"]), 0) for r in skipped),
            "sacrificed_profit_usd": sum(max(float(r["outcome"]["net_usd"]), 0) for r in skipped),
            "first_quote_contracts": quote_contracts,
            "first_quote_notional_usd": quote_notional,
            "episode_retention_fraction": _ratio(len(kept), len(matched)),
            "filled_episode_retention_fraction": _ratio(len(filled), parent_filled),
            "first_quote_contract_retention_fraction": _ratio(quote_contracts, parent_contracts),
            "first_quote_notional_retention_fraction": _ratio(quote_notional, parent_quote_notional),
            "inherited_cost_retention_fraction": _ratio(cost, parent_cost),
            "management_gap_episodes": sum(bool(r["outcome"]["metadata"].get("coverage_gap")) for r in kept),
            "window_risk": _risk([values[name] for _, values in sorted(windows.items())]),
        }
    parent, legacy, candidate = (arms[k]["net_usd"] for k in ("parent", "legacy", "candidate"))
    return {
        "policy": POLICY_VERSION,
        "episodes": len(rows), "settled_episodes": len(settled),
        "pending_episodes": len(rows) - len(settled),
        "source_candidate_changed_episodes": sum(bool(r.get("source_candidate_changed")) for r in rows),
        "pit_evidence_issue_episodes": sum(bool(r.get("pit_evidence_issue")) for r in rows),
        "unclassified_episodes": sum(r.get("decision", {}).get("decision") not in _CLASSIFIED for r in rows),
        "matched_settled_episodes": len(matched),
        "matched_settled_expiry_windows": len(windows),
        "excluded_settled_episodes": len(settled) - len(matched),
        "exclusion_reasons": dict(sorted(exclusions.items())),
        "matched_control_net_usd": parent, "matched_legacy_net_usd": legacy,
        "matched_candidate_net_usd": candidate,
        "matched_difference_usd": candidate - parent,
        "matched_difference_vs_legacy_usd": candidate - legacy,
        "avoided_loss_usd": arms["candidate"]["avoided_loss_usd"],
        "sacrificed_profit_usd": arms["candidate"]["sacrificed_profit_usd"],
        "arms": arms,
        "window_risk": {name: arm["window_risk"] for name, arm in arms.items()},
        "independent_live_profit": False,
        "execution_semantics": "conditional_whole_W8_tilted_paper_episodes_including_all_parent_management",
        "allocation_semantics": "retain_or_skip_whole_episode_no_freed_risk_redistribution_no_capital_ROI",
    }
