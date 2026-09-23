"""Isolated policy invariants and exact frozen research replay.

The research fixture uses explicitly SIMULATED historical publication times to
check policy arithmetic only. It does not claim historical live deployment.
"""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from crypto_trading.crypto_strategies.w9_rnn_paper import policy

BINDING = {"source_strategy": "w7_noisefade", "target_strategy": "w9",
           "version": "fixture", "registered_at": "2026-09-06T00:00:00+00:00"}


def candidate(identity="a", at=1000., expiry=1500., side="yes", price=.9, asset="BTC"):
    return {"id": identity, "strategy": "w9", "ticker": "fixture:"+identity,
            "asset": asset, "side": side, "decision_ts": at, "expires_ts": expiry,
            "parent_binding": deepcopy(BINDING),
            "metadata": {"parent_leg": "MAIN", "contracts": 25, "entry_price": price}}


def flow(at=1000., momentum=1., imbalance=.3):
    return {"decision_ts": at, "valid": True, "flow_valid": True,
            "momentum_1m_bp": momentum, "observed_flow_imbalance_1m": imbalance}


def forecast(at=1000., high=False, asset="BTC"):
    return {"decision_ts": at-10, "asset": asset, "model_id": "fixture",
            "strict_recorded_pit_eligible": True, "risk_available": True,
            "generated_at": at-9, "persisted_at": at-8, "model_available_ts": at-100,
            "feature_last_recorded_receipt_ts": at-60,
            "pred_abs_return_augmented": 3. if high else 1.,
            "prior_calibration_risk_q90_augmented": 2.}


def outcome(c, win=True):
    price = c["metadata"]["entry_price"]
    cost = 25*price
    fee = 25*policy.source_fee_per_contract(price)
    payout = 25.*win
    return {**{key: deepcopy(c[key]) for key in ("id", "strategy", "ticker", "asset", "expires_ts", "parent_binding")},
            "closed_ts": c["expires_ts"]+5, "cost_usd": cost, "fees_usd": fee,
            "payout_usd": payout, "net_usd": payout-cost-fee,
            "metadata": {"contracts": 25, "parent_recorded_win": bool(win)}}


def test_exact_flow_boundaries_yes_and_no_and_unknown_retains_controls():
    yes = candidate()
    assert policy.decision_only(yes, flow(momentum=-.1, imbalance=-.5))["pre_budget_cap"] == 0
    assert policy.decision_only(yes, flow(momentum=0, imbalance=-.5))["pre_budget_cap"] == 25
    assert policy.decision_only(yes, flow(momentum=-.1, imbalance=-.49999))["pre_budget_cap"] == 25
    no = candidate(side="no")
    assert policy.decision_only(no, flow(momentum=.1, imbalance=.5))["flow_skip"]
    unknown = policy.decision_only(yes, {**flow(momentum=-1, imbalance=-1), "flow_valid": False}, forecast(high=True))
    assert not unknown["flow_known"] and unknown["model_known"] and unknown["pre_budget_cap"] == 12
    neither = policy.decision_only(yes)
    assert neither["pre_budget_cap"] == 25 and not neither["full_feature_known"]


@pytest.mark.parametrize("patch", [
    {"decision_ts": 1000.1}, {"decision_ts": 939.9}, {"generated_at": 1001},
    {"persisted_at": 1001}, {"model_available_ts": 1001},
    {"feature_last_recorded_receipt_ts": 991}, {"generated_at": 992, "persisted_at": 991},
    {"pred_abs_return_augmented": float("nan")}, {"pred_abs_return_augmented": -1},
    {"prior_calibration_risk_q90_augmented": float("inf")},
    {"prior_calibration_risk_q90_augmented": -1}, {"risk_available": False},
    {"strict_recorded_pit_eligible": False}, {"asset": "ETH"}, {"model_id": None},
])
def test_unavailable_late_future_invalid_model_cannot_reduce_risk(patch):
    result = policy.decision_only(candidate(), flow(), {**forecast(high=True), **patch})
    assert not result["model_known"] and result["pre_budget_cap"] == 25
    assert result["model_unknown_reasons"]


def test_risk_strict_threshold_and_explicit_publication_evidence_required():
    p = forecast()
    p["pred_abs_return_augmented"] = 2.
    assert not policy.decision_only(candidate(), flow(), p)["rnn_risk_high"]
    p["pred_abs_return_augmented"] = 2.00001
    assert policy.decision_only(candidate(), flow(), p)["pre_budget_cap"] == 12
    del p["persisted_at"]
    assert not policy.decision_only(candidate(), flow(), p)["model_known"]


def test_source_fee_is_causal_and_matches_both_outcomes_all_main_prices():
    for i in range(7800, 9801):
        price = i/10000
        fee = policy.source_fee_per_contract(price)
        for win in (0, 1):
            actual = win-price-round((win-price-.07*price*(1-price))*100, 2)/100
            assert fee == pytest.approx(actual, abs=1e-12)


def test_complete_simultaneous_common_integer_batch_and_risk_member_caps_others():
    cs = [candidate(str(i)) for i in range(5)]
    state = policy.new_state(900., BINDING)
    next_state, rows = policy.apply_batch(state, cs, {c["id"]: flow() for c in cs},
                                          {"0": forecast(high=True)}, observed_at=1001.)
    # 12*5*.9063 would exceed $50; every admitted same-side member gets 11.
    assert [r["decision"]["quantity"] for r in rows] == [11]*5
    assert next_state["spent_by_bucket"]["1500.000000:yes"] == pytest.approx(11*5*.9063)
    assert state["episodes"] == {}  # Pure input preservation.
    two = cs[:2]
    _, rows = policy.apply_batch(state, two, {}, {"0": forecast(high=True)}, observed_at=1001.)
    assert [r["decision"]["quantity"] for r in rows] == [12, 12]


def test_flow_skip_is_not_counted_in_common_budget_or_minimum_cap():
    cs = [candidate(str(i)) for i in range(3)]
    features = {"0": flow(momentum=-1, imbalance=-1)}
    _, rows = policy.apply_batch(policy.new_state(900., BINDING), cs, features, {}, observed_at=1001.)
    assert [r["decision"]["quantity"] for r in rows] == [0, 25, 25]


def test_expiry_and_direction_budgets_independent_and_later_signals_do_not_reallocate():
    first = [candidate("a"), candidate("b", side="no"), candidate("c", expiry=1600)]
    state, rows = policy.apply_batch(policy.new_state(900., BINDING), first, {}, {}, observed_at=1001.)
    assert [r["decision"]["quantity"] for r in rows] == [25]*3
    later = candidate("d", at=1020)
    state, rows = policy.apply_batch(state, [later], {}, {}, observed_at=1021)
    assert rows[0]["decision"]["quantity"] == 25
    last = candidate("e", at=1040)
    state, rows = policy.apply_batch(state, [last], {}, {}, observed_at=1041)
    assert rows[0]["decision"]["quantity"] == 5
    assert state["episodes"]["a"]["decision"]["quantity"] == 25
    assert max(state["spent_by_bucket"].values()) <= 50+1e-9


def test_restart_replay_idempotence_late_batch_and_source_revision_detected():
    c = candidate()
    state, rows = policy.apply_batch(policy.new_state(900., BINDING), [c], {}, {}, observed_at=1001)
    persisted = json.loads(json.dumps(state))
    after, rows = policy.apply_batch(persisted, [c], {"a": flow(momentum=-1, imbalance=-1)}, {}, observed_at=1002)
    assert rows == [] and after == persisted
    for at in (999., 1000.):
        with pytest.raises(policy.LateBatchError):
            policy.apply_batch(state, [candidate("late", at=at)], {}, {}, observed_at=1002)
    changed = deepcopy(c)
    changed["metadata"]["entry_price"] = .85
    with pytest.raises(policy.SourceRevisionError):
        policy.apply_batch(state, [changed], {}, {}, observed_at=1002)
    with pytest.raises(policy.LateBatchError):
        policy.apply_batch(policy.new_state(900., BINDING), [c], {}, {}, observed_at=1500)


def test_settlement_is_scaled_separate_idempotent_never_recycles_budget():
    c = candidate()
    state, _ = policy.apply_batch(policy.new_state(900., BINDING), [c], {"a": flow()}, {"a": forecast(high=True)}, observed_at=1001)
    spent_before = deepcopy(state["spent_by_bucket"])
    raw = outcome(c, False)
    updated, settled = policy.settle(state, [raw])
    assert updated["spent_by_bucket"] == spent_before
    assert state["episodes"]["a"]["outcome"] is None
    assert settled[0]["quantity"] == 12
    assert settled[0]["net_usd"] == pytest.approx(raw["net_usd"]*12/25)
    assert settled[0]["fees_usd"] == pytest.approx(raw["fees_usd"]*12/25)
    assert settled[0]["net_usd"] == pytest.approx(settled[0]["payout_usd"]-settled[0]["cost_usd"]-settled[0]["fees_usd"])
    same, rows = policy.settle(json.loads(json.dumps(updated)), [raw])
    assert same == updated and rows == []
    bad = deepcopy(raw)
    bad["net_usd"] += .01
    with pytest.raises(policy.SourceRevisionError):
        policy.settle(updated, [bad])
    bad = deepcopy(raw)
    bad["parent_binding"]["version"] = "changed"
    with pytest.raises(policy.SourceRevisionError):
        policy.settle(state, [bad])


def test_fully_known_attribution_does_not_reallocate_or_hide_unknown_main_book():
    cs = [candidate("a"), candidate("b")]
    state, _ = policy.apply_batch(policy.new_state(900., BINDING), cs, {"a": flow()}, {"a": forecast(high=True)}, observed_at=1001)
    state, _ = policy.settle(state, [outcome(c) for c in cs])
    report = policy.summarize(state)
    assert report["all_registered"]["signals"] == 2
    assert report["all_registered"]["contracts_settled"] == 24
    assert report["full_feature_attribution"]["signals"] == 1
    assert report["full_feature_attribution"]["contracts_settled"] == 12
    assert report["all_registered"]["model_unknown_signals"] == 1


def test_exact_354_historical_quantity_and_pnl_reproduction():
    import pandas as pd
    root = Path(__file__).resolve().parents[2]
    source = root/"crypto_trading/trading_signals/reports/w9_rnn_pit_expanded_20260916T001013Z/analysis/strict_recent_refit"
    frame = pd.read_parquet(source/"original354_fixed_membership_decisions.parquet")
    assert len(frame) == 354
    state = policy.new_state(float(frame.decision_ts.min())-1, BINDING)
    outcomes = []
    for at, group in frame.sort_values(["decision_ts", "ticker"]).groupby("decision_ts", sort=True):
        candidates, features, models = [], {}, {}
        for row in group.to_dict("records"):
            c = candidate(row["id"], at=float(at), expiry=float(row["expiry_ts"]),
                          side=row["side"], price=float(row["cost"]), asset=row["asset"])
            c["ticker"] = row["ticker"]
            candidates.append(c)
            features[c["id"]] = flow(float(at), float(row["momentum_1m_bp"]), float(row["observed_flow_imbalance_1m"]))
            features[c["id"]].update(valid=bool(row["valid"]), flow_valid=bool(row["flow_valid"]))
            if row["model_known"]:
                # Explicit historical assumed publication for arithmetic parity;
                # live adapter must supply actual measured receipt timestamps.
                t = float(row["prediction_ts"])
                models[c["id"]] = {"decision_ts": t, "asset": row["asset"],
                    "model_id": "historical_policy_fixture_not_live_publication",
                    "strict_recorded_pit_eligible": True, "risk_available": True,
                    "generated_at": t, "persisted_at": t, "model_available_ts": t,
                    "feature_last_recorded_receipt_ts": float(row["feature_last_recorded_receipt_ts"]),
                    "pred_abs_return_augmented": float(row["pred_abs_tuned_rnn"]),
                    "prior_calibration_risk_q90_augmented": float(row["q90_tuned_rnn"])}
            o = outcome(c, bool(row["win"]))
            o.update(closed_ts=float(row["closed_ts"]), net_usd=float(row["net_usd"]),
                     fees_usd=float(row["source_fees_usd"]), cost_usd=float(row["cost_usd"]), payout_usd=float(row["payout_usd"]))
            outcomes.append(o)
        state, _ = policy.apply_batch(state, candidates, features, models, observed_at=float(at)+.001)
    for row in frame.to_dict("records"):
        d = state["episodes"][row["id"]]["decision"]
        assert d["quantity"] == int(row[policy.RESEARCH_VARIANT+"_qty"])
        assert d["model_known"] == bool(row["model_known"])
        assert d["flow_known"] == bool(row["flow_known"])
    state, _ = policy.settle(state, outcomes)
    actual = policy.summarize(state)["all_registered"]
    expected = json.loads((source/"results.json").read_text())["scopes"]["original354_fixed_membership"]["variants"][policy.RESEARCH_VARIANT]
    for metric in ("net_pnl_usd", "max_drawdown_usd", "trade_win_rate"):
        assert actual[metric] == pytest.approx(expected[metric], abs=1e-9)
    assert actual["model_unknown_signals"] == 3
    assert actual["net_pnl_usd"] == pytest.approx(49.3096)
