"""Source adapter invariants: no fixture touches runtime state or live logs."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from crypto_trading.crypto_strategies.downside_paper import sources


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


TICKER = "KXBTC15M-26SEP151430-30"
OPEN = ts("2026-09-15T18:21:54Z")
CLOSE = ts("2026-09-15T18:30:00Z")
DONE = ts("2026-09-15T18:33:18Z")


def w7_state():
    row = {"ticker": TICKER, "series": "KXBTC15M", "side": "no", "leg": "band", "cost": .95,
           "opened": "2026-09-15T18:21:54Z", "closed": "2026-09-15T18:33:18Z", "win": False, "pnl_c": -95.33}
    return {"version": "v3_2026-08-31", "main_registered_at": "2026-09-06T05:21:26.869790Z",
            "positions": {}, "trades": [row]}


def w8_state():
    order = {"id": TICKER + ":1", "side": "no", "price": .71, "quantity": 1,
             "created_ts": OPEN, "activate_ts": OPEN+.5, "expires_ts": OPEN+20,
             "remaining": 1, "queue_ahead": 5}
    market = {"ticker": TICKER, "series": "KXBTC15M", "close_ts": CLOSE,
              "orders": [order], "fills": []}
    return {"version": "w8_v8a_20260914", "registered_at": "2026-09-14T05:17:35.331355Z",
            "source_sha256": {"kernel.py": "a" * 64},
            "books": {"paired": {"complete_sets": True, "positions": {}, "trades": []},
                      "tilted": {"complete_sets": False, "positions": {TICKER: market}, "trades": []}}}


def order_event():
    return {"ts": datetime.fromtimestamp(OPEN+.1, timezone.utc).isoformat(),
            "strategy": "w8_complete_set", "action": "paper_order", "book": "tilted",
            "intent": {"ticker": TICKER, "client_order_id": "w8-paper-"+TICKER+":1",
                       "side": "ask", "price": "0.2900", "count": "1.00", "post_only": True,
                       "expiration_time": int(OPEN+20), "submitted": False, "mode": "observation"}}


def w8_settlement(*, zero=False, unverified=0):
    return {"ticker": TICKER, "series": "KXBTC15M", "close_ts": CLOSE, "result": "yes",
            "settled_at": "2026-09-15T18:33:18Z", "fills": 0 if zero else 2,
            "quantity": 0 if zero else 2, "cost_usd": 0 if zero else .82,
            "fees_usd": 0 if zero else .01, "payout_usd": 0 if zero else 1,
            "net_usd": 0 if zero else .17, "paired_net_usd": 0 if zero else .17,
            "residual_net_usd": 0, "coverage_gap": True, "unverified_order_quantity": unverified}


def test_w7_main_membership_registration_and_exact_outcome_id():
    state = w7_state()
    state["trades"] += [{**state["trades"][0], "leg": "obs"}, {**state["trades"][0], "cost": .77},
                        {**state["trades"][0], "opened": "2026-09-01T18:21:54Z"}]
    binding = sources.make_parent_binding("w7", state, {"strategy.py": "b" * 64})
    candidates = sources.w7_candidates_from_state(state, binding, since_ts=0, until_ts=DONE)
    outcomes = sources.w7_outcomes_from_state(state, binding, until_ts=DONE)
    assert len(candidates) == len(outcomes) == 1
    assert candidates[0]["id"] == outcomes[0]["id"]
    assert candidates[0]["strategy"] == "w9"
    assert candidates[0]["expires_ts"] == CLOSE
    assert outcomes[0]["net_usd"] == pytest.approx(-23.8325)
    assert outcomes[0]["cost_usd"] == pytest.approx(23.75)
    assert outcomes[0]["fees_usd"] == pytest.approx(.0825)


def test_w7_entry_to_settlement_preserves_id_and_does_not_mutate_input():
    state = w7_state()
    trade = state["trades"].pop()
    state["positions"][TICKER] = {**trade, "close": "2026-09-15T18:30:00Z"}
    binding = sources.make_parent_binding("w7", state)
    before = deepcopy(state)
    candidate = sources.w7_candidates_from_state(state, binding, since_ts=OPEN, until_ts=OPEN)[0]
    assert state == before
    state["positions"].clear()
    state["trades"] = [trade]
    assert sources.w7_outcomes_from_state(state, binding, until_ts=DONE-.01) == []
    outcome = sources.w7_outcomes_from_state(state, binding, until_ts=DONE)[0]
    assert outcome["id"] == candidate["id"]


def test_parent_version_registration_and_recorded_hash_changes_fail_closed():
    state = w8_state()
    binding = sources.make_parent_binding("w8", state, {"kernel.py": "b" * 64})
    assert binding["registered_source_sha256"] != binding["observed_source_sha256"]
    for change in ({"version": "different"}, {"registered_at": "2026-09-15T00:00:00Z"},
                   {"source_sha256": {"kernel.py": "c"*64}}):
        bad = deepcopy(state)
        bad.update(change)
        with pytest.raises(sources.ParentBindingMismatch):
            sources.w8_candidate_from_order(order_event(), bad, binding, since_ts=OPEN, until_ts=DONE)


def test_w8_first_order_uses_outcome_side_and_exact_parent_created_time():
    state, event = w8_state(), order_event()
    binding = sources.make_parent_binding("w8", state)
    before = deepcopy(state)
    c = sources.w8_candidate_from_order(event, state, binding, since_ts=OPEN, until_ts=DONE)
    assert c["side"] == "no"
    assert c["metadata"]["entry_price"] == .71  # V2 ask=.29 is NOT NO cost.
    assert c["decision_ts"] == OPEN  # Not the later log append time.
    assert c["metadata"]["parent_leg"] == "tilted"
    assert state == before
    c["parent_binding"]["version"] = "mutated_output"
    assert binding["version"] == state["version"]


def test_w8_quote_before_state_write_is_retryable_without_fallback():
    state = w8_state()
    binding = sources.make_parent_binding("w8", state)
    state["books"]["tilted"]["positions"][TICKER]["orders"] = []
    with pytest.raises(sources.SourceNotReady):
        sources.w8_candidate_from_order(order_event(), state, binding, since_ts=OPEN, until_ts=DONE)


def test_w8_event_appended_after_frozen_cycle_cutoff_is_retryable_not_discarded():
    state, event = w8_state(), order_event()
    binding = sources.make_parent_binding("w8", state)
    with pytest.raises(sources.SourceNotReady, match="newer than"):
        sources.w8_candidate_from_order(event, state, binding, since_ts=OPEN, until_ts=OPEN)
    candidate = sources.w8_candidate_from_order(event, state, binding, since_ts=OPEN, until_ts=OPEN+1)
    assert candidate["decision_ts"] == OPEN
    assert candidate["ticker"] == TICKER


@pytest.mark.parametrize("patch", [
    {"book": "paired"}, {"action": "paper_fill"}, {"action": "verdict_latched", "net_usd": 451},
    {"version": "other_version"}, {"ts": "2026-09-13T18:21:54Z"},
])
def test_w8_nonentry_wrong_arm_test_verdict_and_wrong_version_are_not_candidates(patch):
    state, event = w8_state(), order_event()
    event.update(patch)
    assert sources.w8_candidate_from_order(event, state, sources.make_parent_binding("w8", state),
                                            since_ts=OPEN, until_ts=DONE) is None


def test_later_quote_does_not_reopen_a_rejected_episode():
    state, event = w8_state(), order_event()
    event["intent"]["client_order_id"] = "w8-paper-" + TICKER + ":2"
    assert sources.w8_candidate_from_order(event, state, sources.make_parent_binding("w8", state),
                                            since_ts=OPEN, until_ts=DONE) is None


def test_mismatched_price_or_ambiguous_ticker_is_rejected():
    state, event = w8_state(), order_event()
    event["intent"]["price"] = ".71"
    with pytest.raises(ValueError, match="does not match"):
        sources.w8_candidate_from_order(event, state, sources.make_parent_binding("w8", state),
                                        since_ts=OPEN, until_ts=DONE)
    state = w7_state()
    state["trades"][0]["ticker"] = "KXBTC15M-26SEP151430-45"
    with pytest.raises(ValueError, match="suffix"):
        sources.w7_candidates_from_state(state, sources.make_parent_binding("w7", state), since_ts=0, until_ts=DONE)


def test_whole_tilted_lifecycle_not_paired_reference_or_only_profitable_component():
    state = w8_state()
    binding = sources.make_parent_binding("w8", state)
    c = sources.w8_candidate_from_order(order_event(), state, binding, since_ts=OPEN, until_ts=DONE)
    row = w8_settlement()
    row.update(cost_usd=1.02, fees_usd=.01, net_usd=-.03, paired_net_usd=.17, residual_net_usd=-.20)
    state["books"]["tilted"]["trades"] = [row]
    state["books"]["paired"]["trades"] = [{**row, "net_usd": 999}]
    result = sources.w8_outcomes_from_state(state, binding, until_ts=DONE)
    assert len(result) == 1
    assert result[0]["id"] == c["id"]
    assert result[0]["net_usd"] == -.03
    assert result[0]["cost_usd"] == 1.02
    assert result[0]["metadata"]["paired_component_usd"] == .17
    assert result[0]["metadata"]["residual_component_usd"] == -.20


def test_zero_fill_outcome_is_zero_and_unverified_outcome_is_flagged():
    state = w8_state()
    binding = sources.make_parent_binding("w8", state)
    state["books"]["tilted"]["trades"] = [w8_settlement(zero=True)]
    result = sources.w8_outcomes_from_state(state, binding, until_ts=DONE)[0]
    assert result["net_usd"] == 0 and result["metadata"]["zero_fill"]
    state["books"]["tilted"]["trades"] = [w8_settlement(unverified=.5)]
    result = sources.w8_outcomes_from_state(state, binding, until_ts=DONE)[0]
    assert result["metadata"]["source_fill_verified"] is False
    assert result["metadata"]["unverified_order_quantity"] == .5


def test_all_preexisting_positions_even_unfilled_are_excluded_at_startup():
    state = w8_state()
    no_order = "KXETH15M-26SEP151430-30"
    past = "KXXRP15M-26SEP151415-15"
    state["books"]["paired"]["positions"][no_order] = {"orders": [], "fills": []}
    state["books"]["paired"]["trades"] = [{"ticker": past, "fills": 0}]
    assert sources.w8_existing_tickers(state) == {TICKER, no_order, past}


def test_adapter_does_not_import_live_managers_clients_or_shared_state_helpers():
    # Deliberately structural: importing this adapter must never trigger an
    # unrelated live daemon's locks, environment/auth loading, or I/O setup.
    import ast
    from pathlib import Path
    tree = ast.parse(Path(sources.__file__).read_text())
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(name and (name.startswith("crypto_trading") or name in {"requests", "httpx", "os", "pathlib"}) for name in imports)
