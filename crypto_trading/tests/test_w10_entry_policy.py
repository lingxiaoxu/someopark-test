"""Causal gating and common-cohort accounting for the isolated W10 experiment."""
from copy import deepcopy
import json

import pytest

from crypto_trading.crypto_strategies.downside_paper.features import compute_features
from crypto_trading.crypto_strategies.w10_entry_paper import policy


PARAMETERS = {"policy": policy.POLICY_VERSION}


def candidate(identity="a", expiry=1500., side="yes"):
    return {"id": identity, "strategy": "w10", "ticker": "fixture:" + identity,
            "side": side, "decision_ts": 1000., "expires_ts": expiry,
            "parent_binding": {"source_strategy": "w8_complete_set", "version": "fixture"},
            "metadata": {"parent_leg": "tilted", "contracts": 1., "entry_price": .7}}


def features(momentum=1., flow=.3):
    return {"decision_ts": 1000., "valid": True, "flow_valid": True,
            "momentum_1m_bp": momentum, "observed_flow_imbalance_1m": flow}


def episode(identity="a", net=.3, expiry=1500., momentum=1., flow=.3, filled=True):
    c, f = candidate(identity, expiry), features(momentum, flow)
    fee = .01 if filled else 0.
    cost = max(.7, -net + .1) if filled else 0.
    outcome = {**{key: deepcopy(c[key]) for key in ("id", "strategy", "ticker", "expires_ts", "parent_binding")},
        "closed_ts": expiry + 1, "net_usd": net, "cost_usd": cost, "fees_usd": fee,
        "payout_usd": net + cost + fee,
        "metadata": {"source_fill_verified": True, "unverified_order_quantity": 0,
                     "zero_fill": not filled, "fills": int(filled), "turnover_quantity": float(filled),
                     "coverage_gap": False}}
    return {"candidate": c, "features": f, "decision": policy.decide(c, f, PARAMETERS),
            "legacy_decision": policy.legacy_decide(c, f, PARAMETERS), "outcome": outcome}


@pytest.mark.parametrize("side,momentum,flow,expected", [
    ("yes", 1, .1, "accept"), ("no", -1, -.1, "accept"),
    ("yes", -1, .1, "skip"), ("yes", 1, -.1, "skip"),
    ("no", 1, -.1, "skip"), ("no", -1, .1, "skip"),
    ("yes", 0, 1, "skip"), ("yes", 1, 0, "skip"),
    ("no", 0, -1, "skip"), ("no", -1, 0, "skip"),
    ("yes", 1e-12, 1e-12, "accept"), ("no", -1e-12, -1e-12, "accept"),
])
def test_gate_both_directions_and_strict_zero_boundaries(side, momentum, flow, expected):
    c, f = candidate(side=side), features(momentum, flow)
    before = deepcopy((c, f, PARAMETERS))
    result = policy.decide(c, f, PARAMETERS)
    assert result["decision"] == expected
    assert (c, f, PARAMETERS) == before


@pytest.mark.parametrize("patch", [
    {"valid": False}, {"flow_valid": False}, {"valid": 1}, {"flow_valid": 1},
    {"decision_ts": None}, {"decision_ts": 1000.00001}, {"decision_ts": 999.99999},
    {"momentum_1m_bp": float("nan")}, {"momentum_1m_bp": float("inf")},
    {"momentum_1m_bp": True}, {"observed_flow_imbalance_1m": None},
    {"observed_flow_imbalance_1m": -1.000001}, {"observed_flow_imbalance_1m": 1.000001},
])
def test_bad_or_noncausal_evidence_is_unclassified_for_both_arms(patch):
    f = {**features(), **patch}
    assert policy.decide(candidate(), f, PARAMETERS)["decision"] == "unclassified"
    assert policy.legacy_decide(candidate(), f, PARAMETERS)["decision"] == "unclassified"


def test_legacy_reuses_original_threshold_and_no_future_or_unknown_version():
    for side, sign in (("yes", 1), ("no", -1)):
        c = candidate(side=side)
        assert policy.legacy_decide(c, features(-sign, -.5 * sign), PARAMETERS)["decision"] == "skip"
        assert policy.legacy_decide(c, features(-sign, -.4999 * sign), PARAMETERS)["decision"] == "accept"
        assert policy.decide(c, features(-sign, -.4999 * sign), PARAMETERS)["decision"] == "skip"
    assert policy.decide(candidate(), features(), {"policy": "not_registered"})["decision"] == "unclassified"
    assert policy.decide(candidate(side="up"), features(), PARAMETERS)["decision"] == "unclassified"


def test_future_received_rows_cannot_change_feature_builder_or_gate():
    context = [{"recv_ts": t, "mid_px": 100 + (t - 700) / 1000} for t in range(690, 1001, 5)]
    book = [{"recv_ts": 998., "venue_ts": 998000., "bids": [[100, 1]], "asks": [[101, 1]]}]
    trades = [{"recv_ts": t, "venue_ts": t * 1000, "px": 100, "sz": 1,
               "side": "B", "tid": t, "coin": "BTC"} for t in (950, 970, 990)]
    original = compute_features(context, book, trades, 1000.)
    future = compute_features(context + [{"recv_ts": 1000.01, "mid_px": 1}],
        book + [{"recv_ts": 1000.01, "venue_ts": 1000010, "bids": [[1, 100]], "asks": [[2, 100]]}],
        trades + [{"recv_ts": 1000.01, "venue_ts": 999000, "px": 100, "sz": 1000,
                   "side": "A", "tid": "future", "coin": "BTC"}], 1000.)
    assert original == future
    assert original["valid"] and original["flow_valid"]
    assert policy.decide(candidate(), original, PARAMETERS)["decision"] == "accept"


def test_common_cohort_includes_unfilled_but_win_rate_only_filled_and_zero_not_win():
    rows = {"win": episode("win", 2), "loss": episode("loss", -3, momentum=-1, flow=-.7),
            "sacrifice": episode("sacrifice", 1, momentum=1, flow=-.2),
            "flat": episode("flat", 0), "no_fill": episode("no_fill", 0, filled=False)}
    result = policy.summarize(rows)
    assert result["matched_settled_episodes"] == 5
    assert result["matched_control_net_usd"] == 0
    assert result["matched_legacy_net_usd"] == 3
    assert result["matched_candidate_net_usd"] == 2
    assert result["avoided_loss_usd"] == 3
    assert result["sacrificed_profit_usd"] == 1
    assert result["matched_difference_vs_legacy_usd"] == -1
    new = result["arms"]["candidate"]
    assert new["kept_episodes"] == 3 and new["filled_episodes"] == 2 and new["unfilled_episodes"] == 1
    assert new["filled_win_rate"] == .5 and new["flat_filled_episodes"] == 1
    assert new["episode_retention_fraction"] == .6
    assert new["first_quote_contract_retention_fraction"] == .6
    assert new["first_quote_notional_retention_fraction"] == pytest.approx(.6)
    assert not result["independent_live_profit"]
    assert "roi" not in json.dumps(result).lower().replace("no_capital_roi", "")


def test_unknown_and_source_unverified_revised_excluded_from_all_three_arms():
    rows = {name: episode(name, -10) for name in ("unknown_new", "unknown_old", "revision", "entry_revision", "unverified", "missing_evidence")}
    rows["unknown_new"]["decision"]["decision"] = "unclassified"
    rows["unknown_old"]["legacy_decision"]["decision"] = "unclassified"
    rows["revision"]["source_outcome_changed"] = True
    rows["entry_revision"]["source_candidate_changed"] = True
    rows["unverified"]["outcome"]["metadata"]["source_fill_verified"] = False
    del rows["missing_evidence"]["outcome"]["metadata"]["source_fill_verified"]
    rows["good"] = episode("good", .1)
    rows["pending"] = {**episode("pending", -100), "outcome": None}
    before = deepcopy(rows)
    result = policy.summarize(rows)
    assert rows == before
    assert result["episodes"] == 8 and result["pending_episodes"] == 1
    assert result["matched_settled_episodes"] == 1
    assert result["excluded_settled_episodes"] == 6
    assert result["exclusion_reasons"] == {"unclassified_in_either_arm": 2, "source_revised": 2, "source_fill_unverified": 2}
    for arm in result["arms"].values():
        assert arm["net_usd"] == .1 and arm["avoided_loss_usd"] == 0


@pytest.mark.parametrize("mutation", [
    lambda r: r["outcome"].update(net_usd=float("nan")),
    lambda r: r["outcome"].update(net_usd=100),
    lambda r: r["outcome"].update(ticker="another-market"),
    lambda r: r["outcome"].update(closed_ts=100),
    lambda r: r["outcome"]["metadata"].update(zero_fill=True),
    lambda r: r["outcome"]["metadata"].update(fills=.5),
    lambda r: r["outcome"]["metadata"].update(unverified_order_quantity=.1),
    lambda r: r["candidate"]["metadata"].update(contracts=0),
    lambda r: r["features"].update(decision_ts=1001.),
])
def test_corrupt_settlement_or_features_cannot_enter_summary(mutation):
    row = episode()
    mutation(row)
    result = policy.summarize({"a": row})
    assert result["matched_settled_episodes"] == 0
    assert result["excluded_settled_episodes"] == 1
    assert result["matched_control_net_usd"] == result["matched_candidate_net_usd"] == 0
    json.dumps(result, allow_nan=False)


def test_management_gap_retained_and_simultaneous_assets_aggregated_before_drawdown():
    rows = {"a": episode("a", -10, 1500), "b": episode("b", 12, 1500),
            "c": episode("c", -3, 2400, momentum=-1, flow=-.7),
            "d": episode("d", 1, 3300)}
    rows["a"]["outcome"]["metadata"]["coverage_gap"] = True
    result = policy.summarize(rows)
    assert result["matched_settled_expiry_windows"] == 3
    assert result["matched_control_net_usd"] == 0
    assert result["matched_candidate_net_usd"] == 3
    assert result["arms"]["parent"]["management_gap_episodes"] == 1
    assert result["window_risk"]["parent"]["realized_max_drawdown_usd"] == -3
    assert result["window_risk"]["candidate"]["realized_max_drawdown_usd"] == 0
    assert result["window_risk"]["parent"]["worst_expiry_window_usd"] == -3


def test_late_discovered_predating_fill_is_preserved_but_excluded_for_all_arms():
    row = episode(net=-.7, momentum=-1, flow=-.6)
    row["decision_published_at"] = 1002.
    row["pit_evidence_issue"] = {"earliest_parent_fill_ts": 1001., "reason": "fill_before_publication"}
    result = policy.summarize({"a": row})
    assert result["pit_evidence_issue_episodes"] == 1
    assert result["exclusion_reasons"] == {"pit_evidence_issue": 1}
    assert result["matched_settled_episodes"] == 0
    assert result["avoided_loss_usd"] == 0
    assert all(arm["net_usd"] == 0 for arm in result["arms"].values())


def test_empty_registration_has_no_win_rate_or_invented_roi_and_other_strategies_ignored():
    row = episode()
    row["candidate"]["strategy"] = "w9"
    result = policy.summarize({"other": row})
    assert result["episodes"] == 0
    for arm in result["arms"].values():
        assert arm["net_usd"] == 0 and arm["filled_win_rate"] is None
        assert arm["episode_retention_fraction"] is None
        assert arm["window_risk"]["realized_max_drawdown_usd"] is None
    json.dumps(result, allow_nan=False)
