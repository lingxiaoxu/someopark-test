"""Predeclared experimental flow gate, not a validated trading signal.

The input is sampled, causally received public prints, not full market volume.
Missing evidence is unclassified, never counted as an avoided loss.
"""
import math
from collections import defaultdict

POLICY_VERSION = "price_flow_reversal_v1"


def decide(candidate, features, parameters):
    if not features.get("valid") or not features.get("flow_valid"):
        return {"decision": "unclassified", "reason": "incomplete_causal_features"}
    side = candidate.get("side")
    if side not in ("yes", "no"):
        return {"decision": "unclassified", "reason": "unknown_contract_direction"}
    try:
        flow = float(features["observed_flow_imbalance_1m"])
        momentum = float(features["momentum_1m_bp"])
        if not all(math.isfinite(x) for x in (flow, momentum)):
            raise ValueError("invalid feature")
    except (KeyError, TypeError, ValueError):
        return {"decision": "unclassified", "reason": "missing_price_flow_feature"}
    aligned_flow = flow * (1 if side == "yes" else -1)
    aligned_momentum = momentum * (1 if side == "yes" else -1)
    blocked = aligned_flow <= -0.5 and aligned_momentum < 0
    return {
        "decision": "skip" if blocked else "accept",
        "reason": "adverse_price_and_observed_flow" if blocked else "gate_not_triggered",
        "aligned_observed_flow_1m": aligned_flow,
        "aligned_momentum_1m_bp": aligned_momentum,
    }


def summarize(episodes):
    """Matched admitted/skipped episodes, with all-parent control reported separately."""
    result = {}
    for strategy in ("w9", "w10"):
        rows = [r for r in episodes.values() if r["candidate"]["strategy"] == strategy]
        settled = [r for r in rows if r.get("outcome") is not None]
        matched = [r for r in settled if r["decision"]["decision"] in ("accept", "skip")
                   and not r.get("source_outcome_changed")
                   and r["outcome"].get("metadata", {}).get("source_fill_verified", True)]
        skipped = [r for r in matched if r["decision"]["decision"] == "skip"]
        control = sum(float(r["outcome"]["net_usd"]) for r in matched)
        variant = sum(float(r["outcome"]["net_usd"]) for r in matched if r["decision"]["decision"] == "accept")
        window_values = defaultdict(lambda: [0., 0.])
        for row in matched:
            key = row["candidate"].get("expires_ts", row["candidate"]["id"])
            net = float(row["outcome"]["net_usd"])
            window_values[key][0] += net
            window_values[key][1] += net if row["decision"]["decision"] == "accept" else 0
        risk = {}
        for index, label in enumerate(("control", "candidate")):
            path = [v[index] for _, v in sorted(window_values.items())]
            running, peak, drawdown = 0., 0., 0.
            for net in path:
                running += net
                peak = max(peak, running)
                drawdown = min(drawdown, running - peak)
            tail = sorted(path)[:max(1, math.ceil(len(path) * .05))]
            risk[label] = {"realized_max_drawdown_usd": drawdown,
                           "worst_expiry_window_usd": min(path) if path else None,
                           "worst_5pct_mean_window_usd": sum(tail) / len(tail) if tail else None}
        result[strategy] = {
            "episodes": len(rows), "settled_episodes": len(settled),
            "pending_episodes": len(rows) - len(settled),
            "unclassified_episodes": sum(r["decision"]["decision"] == "unclassified" for r in rows),
            "matched_settled_episodes": len(matched),
            "skipped_settled_episodes": len(skipped),
            "classified_settled_expiry_windows": len({r["candidate"].get("expires_ts") for r in matched}),
            "classified_parent_filled_expiry_windows": len({r["candidate"].get("expires_ts") for r in matched
                                                            if not r["outcome"].get("metadata", {}).get("zero_fill", False)}),
            "source_unverified_or_revised_episodes": len(settled) - sum(r["decision"]["decision"] == "unclassified" for r in settled) - len(matched),
            "all_parent_settled_net_usd": sum(float(r["outcome"]["net_usd"]) for r in settled),
            "matched_control_net_usd": control,
            "matched_candidate_net_usd": variant,
            "matched_difference_usd": variant - control,
            "avoided_loss_usd": -sum(min(float(r["outcome"]["net_usd"]), 0) for r in skipped),
            "sacrificed_profit_usd": sum(max(float(r["outcome"]["net_usd"]), 0) for r in skipped),
            "independent_live_profit": False,
            "window_risk": risk,
        }
    return result
