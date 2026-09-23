"""PIT publication, quote expiry and complete-management accounting regression."""
from copy import deepcopy
import json

import pytest

from crypto_trading.crypto_strategies.w10_entry_paper import fv60_policy as policy


def order(number=1, created=1000., *, side="yes", price=.7):
    return {"id": f"fixture:{number}", "side": side, "price": price, "quantity": 1.,
            "created_ts": created, "activate_ts": created+.5, "expires_ts": created+20}


def entry(source, *, ts=None, quantity=1.):
    return {"order_id": source["id"], "side": source["side"], "price": source["price"],
            "quantity": quantity, "fee_usd": 0., "ts": ts or source["created_ts"]+3,
            "liquidity": "maker_model", "source": "print:"+source["id"]}


def quote_record(source):
    return {"source": policy.quote_source(source, "fixture"),
            "source_fingerprint": policy.quote_fingerprint(source, "fixture"),
            "decision": {"decision": "accept" if source["created_ts"]-1000 <= 60 else "skip"},
            "observed_at": source["created_ts"]+.8,
            "decision_published_at": source["created_ts"]+1,
            "decision_effective_at": source["created_ts"]+1.5}


def episode(*, probability=.8, result="yes", orders=None, fills=None, expiry=1600.):
    orders = orders if orders is not None else [order()]
    fills = fills if fills is not None else [entry(orders[0])]
    candidate = {"id": "w10:fixture", "strategy": "w10", "ticker": "fixture", "side": "yes",
                 "decision_ts": 1000., "expires_ts": expiry,
                 "parent_binding": {"version": "fixture", "registered_at": 500.},
                 "metadata": {"source_order_id": "fixture:1", "entry_price": .7,
                              "contracts": 1., "parent_leg": "tilted"}}
    features = {"valid": True, "decision_ts": 1000., "probability_proxy": probability,
                "edge_proxy": probability-.7}
    cost = sum(f["price"]*f["quantity"] for f in fills)
    fees = sum(f["fee_usd"] for f in fills)
    payout = sum(f["quantity"] for f in fills if f["side"] == result)
    outcome = {**{key: deepcopy(candidate[key]) for key in
                  ("id", "strategy", "ticker", "expires_ts", "parent_binding")},
               "closed_ts": expiry+1, "cost_usd": cost, "fees_usd": fees,
               "payout_usd": payout, "net_usd": payout-cost-fees,
               "metadata": {"source_fill_verified": True, "unverified_order_quantity": 0,
                   "fills": len(fills), "zero_fill": not fills,
                   "turnover_quantity": sum(f["quantity"] for f in fills),
                   "official_result": result, "coverage_gap": False}}
    return {"candidate": candidate, "features": features,
            "decision": policy.decide(candidate, features, {}),
            "observed_at": 1000.8, "decision_published_at": 1001.,
            "minimum_publication_latency_seconds": .5, "issues": [],
            "quotes": {o["id"]: quote_record(o) for o in orders},
            "source_ledger": {"ticker": "fixture", "close_ts": expiry, "orders": orders, "fills": fills},
            "source_ledger_final": True, "outcome": outcome}


@pytest.mark.parametrize("probability,expected", [(0., "skip"), (.7, "skip"), (.7000001, "accept"), (1., "accept")])
def test_strict_value_gate_and_no_mutation(probability, expected):
    row = episode(probability=probability)
    before = deepcopy(row)
    assert policy.decide(row["candidate"], row["features"], {})["decision"] == expected
    assert before == row


@pytest.mark.parametrize("patch,reason", [
    ({"valid": False}, "incomplete_causal_valuation"),
    ({"valid": 1}, "incomplete_causal_valuation"),
    ({"decision_ts": 1000.01}, "feature_timestamp_not_parent_first_quote"),
    ({"probability_proxy": float("nan")}, "invalid_probability_or_edge"),
    ({"probability_proxy": True}, "invalid_probability_or_edge"),
    ({"probability_proxy": 1.01}, "invalid_probability_or_edge"),
    ({"edge_proxy": .2}, "valuation_edge_identity_mismatch"),
])
def test_missing_nonfinite_or_noncausal_value_fails_closed(patch, reason):
    row = episode()
    row["features"].update(patch)
    assert policy.decide(row["candidate"], row["features"], {}) == {"decision": "unclassified", "reason": reason}


def test_unknown_version_and_alternative_thresholds_not_silently_allowed():
    row = episode()
    for params in ({"policy": "future_v2"}, {"entry_creation_max_age_seconds": 120}):
        assert policy.decide(row["candidate"], row["features"], params)["decision"] == "unclassified"


def test_deadline_uses_initial_timestamp_never_side_change_or_fill_time():
    row = episode()
    for age, side, expected in ((0, "yes", "accept"), (60, "no", "accept"), (60.00001, "yes", "skip")):
        result = policy.decide_quote(row["candidate"], row["decision"], order(2, 1000+age, side=side))
        assert result["decision"] == expected
    assert policy.decide_quote(row["candidate"], row["decision"], order(2, 999))["decision"] == "unclassified"


def test_immutable_fingerprint_excludes_remaining_queue_and_cancellation():
    source = order()
    changed = {**source, "remaining": 0., "queue_ahead": 0., "cancel_ts": 1008.}
    assert policy.quote_fingerprint(source, "fixture") == policy.quote_fingerprint(changed, "fixture")
    assert policy.quote_fingerprint(source, "fixture") != policy.quote_fingerprint({**source, "price": .71}, "fixture")


def test_full_inventory_retains_every_management_leg_and_fee():
    source = order()
    management = {"side": "no", "price": .01, "quantity": 1., "fee_usd": .003,
                  "ts": 1590., "liquidity": "taker_depth_model", "source": "risk_flatten"}
    row = episode(fills=[entry(source), management])
    result = policy.evaluate_episode(row)
    assert result["status"] == "qualified"
    assert result["source"] == result["candidate"]
    assert result["candidate"]["net_usd"] == pytest.approx(.287)
    assert result["candidate"]["fees_usd"] == .003
    assert result["candidate"]["fills"] == 2
    assert not result["independent_live_profit"]


def test_deadline_stops_new_creations_not_old_live_order_or_inventory():
    first, old_live, future = order(), order(2, 1050), order(3, 1061)
    # A permitted order fills at 1065, after the absolute deadline. Its original
    # expiry is 1070; it is retained because FV60 did not cancel old orders.
    row = episode(orders=[first, old_live, future], fills=[entry(old_live, ts=1065)])
    result = policy.evaluate_episode(row)
    assert result["status"] == "qualified"
    assert result["candidate"]["net_usd"] == pytest.approx(.3)
    assert result["accepted_quotes"] == 2 and result["skipped_quotes"] == 1


def test_precommitted_late_quote_skip_does_not_need_fake_early_publication():
    first, future = order(), order(2, 1061)
    row = episode(orders=[first, future], fills=[entry(future)])
    del row["quotes"][future["id"]]
    result = policy.evaluate_episode(row)
    assert result["status"] == "qualified"
    assert result["candidate"]["net_usd"] == 0
    assert result["deadline_precommitted_at"] == 1001
    assert result["source"]["net_usd"] == pytest.approx(.3)


def test_initial_reject_covers_future_quotes_but_never_before_publication():
    row = episode(probability=.6)
    row["quotes"] = {}
    assert policy.evaluate_episode(row)["candidate"]["net_usd"] == 0
    row["decision_published_at"] = 1003
    assert policy.evaluate_episode(row)["reason"] == "source_fill_precedes_initial_publication"


def test_partial_entry_inventory_is_unidentified_not_scaled_management():
    first, future = order(), order(2, 1061)
    row = episode(orders=[first, future], fills=[entry(first, quantity=.4), entry(future, quantity=.6)])
    result = policy.evaluate_episode(row)
    assert result["status"] == "unclassified"
    assert result["reason"] == "partial_inventory_unidentified"
    summary = policy.summarize({"a": row})
    assert summary["matched_control_net_usd"] == summary["matched_candidate_net_usd"] == 0
    assert summary["avoided_loss_usd"] == 0


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(source_candidate_changed=True), "source_revised"),
    (lambda r: r.update(source_outcome_changed=True), "source_revised"),
    (lambda r: r.update(issues=["read_gap"]), "source_or_timing_issue"),
    (lambda r: r.update(source_ledger_changed=True), "source_or_timing_issue"),
    (lambda r: r["quotes"].clear(), "missing_or_unclassified_entry_quote"),
    (lambda r: r["quotes"]["fixture:1"].update(source_fingerprint="changed"), "source_quote_revised_or_decision_inconsistent"),
    (lambda r: r["quotes"]["fixture:1"].update(decision_published_at=1002.5, decision_effective_at=1003.), "source_fill_precedes_quote_publication"),
    (lambda r: r["quotes"]["fixture:1"].update(decision_effective_at=1000.), "entry_quote_effective_timestamp_inconsistent"),
    (lambda r: r["source_ledger"]["orders"][0].update(cancel_ts=1001.4), "source_fill_after_effective_cancel"),
    (lambda r: r["outcome"].update(net_usd=3), "invalid_source_cashflows"),
    (lambda r: r["source_ledger"]["fills"].clear(), "source_ledger_does_not_reconcile_to_settlement"),
    (lambda r: r["source_ledger"]["fills"][0].update(order_id="missing"), "source_entry_fill_without_order"),
    (lambda r: r["source_ledger"]["fills"][0].update(ts=1000.5), "source_entry_fill_terms_mismatch"),
    (lambda r: r["source_ledger"]["fills"][0].update(price=.71), "source_entry_fill_terms_mismatch"),
    (lambda r: r["source_ledger"]["fills"][0].update(quantity=1.01), "source_entry_overfill"),
    (lambda r: r.update(minimum_publication_latency_seconds=0), "unregistered_publication_latency"),
])
def test_malformed_missing_revised_and_late_evidence_never_earns_avoidance(mutation, reason):
    row = episode(result="no")
    mutation(row)
    assert policy.evaluate_episode(row)["reason"] == reason
    summary = policy.summarize({"a": row})
    assert summary["excluded_settled_episodes"] == 1
    assert summary["avoided_loss_usd"] == summary["sacrificed_profit_usd"] == 0


def test_zero_fill_has_no_win_but_requires_complete_timely_evidence():
    row = episode(fills=[])
    result = policy.evaluate_episode(row)
    assert result["status"] == "qualified"
    assert result["candidate"]["fills"] == 0
    assert policy.summarize({"a": row})["arms"]["candidate"]["filled_win_rate"] is None
    row["quotes"]["fixture:1"].update(decision_published_at=1020, decision_effective_at=1020.5)
    assert policy.evaluate_episode(row)["reason"] == "entry_quote_publication_unproven_or_late"


def test_settlement_arriving_before_full_archive_remains_pending_then_resolves():
    row = episode()
    row["source_ledger_final"] = False
    assert policy.evaluate_episode(row) == {"status": "pending", "reason": "awaiting_complete_source_ledger"}
    summary = policy.summarize({"a": row})
    assert summary["settled_episodes"] == 1 and summary["pending_episodes"] == 1
    assert summary["awaiting_complete_source_ledger_episodes"] == 1
    assert summary["awaiting_source_settlement_episodes"] == 0
    assert summary["excluded_settled_episodes"] == 0
    assert summary["avoided_loss_usd"] == summary["matched_control_net_usd"] == 0
    row["source_ledger_final"] = True
    assert policy.evaluate_episode(row)["status"] == "qualified"


def test_zero_fill_source_cancelled_before_effective_publication_is_unknown():
    row = episode(fills=[])
    row["source_ledger"]["orders"][0]["cancel_ts"] = 1001.4
    assert policy.evaluate_episode(row)["reason"] == "entry_quote_effective_after_source_expiry_or_cancel"


def test_quote_created_just_before_deadline_but_published_after_is_unknown():
    first, last = order(), order(2, 1059.5)
    row = episode(orders=[first, last], fills=[])
    assert policy.evaluate_episode(row)["reason"] == "entry_quote_publication_unproven_or_late"


def test_pending_not_treated_as_zero_profit_and_final_common_risk_groups_expiry():
    a = episode()
    b = episode(probability=.6, result="no")
    c = episode(result="no", expiry=2500)
    pending = episode()
    pending["outcome"] = None
    unknown = episode()
    unknown["features"]["valid"] = False
    rows = dict(a=a, b=b, c=c, pending=pending, unknown=unknown)
    before = deepcopy(rows)
    result = policy.summarize(rows)
    assert rows == before
    assert result["episodes"] == 5 and result["pending_episodes"] == 1
    assert result["matched_settled_episodes"] == 3 and result["matched_settled_expiry_windows"] == 2
    assert result["excluded_settled_episodes"] == 1
    assert result["matched_control_net_usd"] == pytest.approx(-1.1)
    assert result["matched_candidate_net_usd"] == pytest.approx(-.4)
    assert result["avoided_loss_usd"] == .7 and result["sacrificed_profit_usd"] == 0
    assert result["window_risk"]["parent"]["realized_max_drawdown_usd"] == pytest.approx(-1.1)
    assert result["window_risk"]["candidate"]["realized_max_drawdown_usd"] == pytest.approx(-.7)
    json.dumps(result, allow_nan=False)


def test_sacrificed_profit_counted_separately_from_avoided_loss():
    a = episode(probability=.6)
    b = episode(probability=.6, result="no")
    result = policy.summarize(dict(a=a, b=b))
    assert result["sacrificed_profit_usd"] == pytest.approx(.3)
    assert result["avoided_loss_usd"] == pytest.approx(.7)
    assert result["matched_difference_usd"] == pytest.approx(.4)


def test_extra_management_fill_is_never_silently_ignored():
    row = episode()
    row["source_ledger"]["fills"].append({"side": "no", "price": .1, "quantity": 1,
        "fee_usd": .01, "ts": 1500, "source": "risk_flatten", "liquidity": "taker_depth_model"})
    assert policy.evaluate_episode(row)["reason"] == "source_ledger_does_not_reconcile_to_settlement"


def test_duplicate_source_fills_and_missing_quote_sequence_are_rejected():
    row = episode()
    row["source_ledger"]["fills"].append(deepcopy(row["source_ledger"]["fills"][0]))
    assert policy.evaluate_episode(row)["reason"] == "duplicate_source_fill"
    row = episode(orders=[order(), order(3, 1061)])
    assert policy.evaluate_episode(row)["reason"] == "incomplete_source_quote_sequence"
