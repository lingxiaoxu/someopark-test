"""Cycle integration invariants with synthetic parents and temporary output only.

The live registration branch is never entered. All parent reads, source adapters,
market tape and log readers are mocked. Persistence exercises only tmp_path.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from crypto_trading.crypto_strategies.downside_paper import observer as mod


PARAMS = {"policy": "price_flow_reversal_v1"}


def event(ticker="synthetic-BTC", at=1001.0, **extra):
    return {"ts": at, "strategy": "w8_complete_set", "action": "paper_order",
            "book": "tilted", "intent": {"ticker": ticker}, **extra}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = SimpleNamespace(clock=1002.0, events=[], calls=[], feature_calls=[],
                        parents=[{"version": "one"}, {"version": "one"}],
                        outcomes=[], observers=[], output=tmp_path / "isolated-paper")
    parent_hashes, self_hashes = {"parent": "a" * 64}, {"observer.py": "b" * 64}
    logpath = tmp_path / "mocked-source-log.jsonl"

    class FakeLogs:
        def __init__(self):
            self.cursors = {}
            self.errors = 0

        def seed_end(self, path):
            raise AssertionError("Synthetic resume must not register a live observer")

        def read(self, path):
            key = str(path)
            offset = self.cursors.get(key, (1, 0))[1]
            incoming = deepcopy(h.events[offset:])
            self.cursors[key] = (1, len(h.events))
            return incoming

    class FakeTape:
        def __init__(self, root):
            self.tail = SimpleNamespace(errors=0)

        def update(self, now):
            pass

        def for_asset(self, asset):
            return [], [], []

    def binding(strategy, state):
        return {"strategy": strategy, "version": state["version"]}

    def candidate_adapter(ev, state, parent_binding, **kwargs):
        h.calls.append(ev["intent"]["ticker"])
        if ev.get("malformed"):
            raise ValueError("synthetic source mismatch")
        if ev.get("not_ready"):
            raise mod.SourceNotReady("synthetic delayed parent write")
        return {"id": "w10:" + ev["intent"]["ticker"], "strategy": "w10",
                "ticker": ev["intent"]["ticker"], "asset": "BTC", "side": "no",
                "decision_ts": ev["ts"], "expires_ts": 1020.0,
                "parent_binding": deepcopy(parent_binding), "metadata": {}}

    def feature_adapter(context, books, trades, at):
        h.feature_calls.append(at)
        return {"valid": True, "flow_valid": True,
                "observed_flow_imbalance_1m": .8, "momentum_1m_bp": 1.0}

    monkeypatch.setattr(mod, "SOURCE", tmp_path / "mocked-parent-source")
    monkeypatch.setattr(mod.time, "time", lambda: h.clock)
    monkeypatch.setattr(mod, "load_parents", lambda: deepcopy(h.parents))
    monkeypatch.setattr(mod, "source_hashes", lambda: deepcopy(parent_hashes))
    monkeypatch.setattr(mod, "own_hashes", lambda: deepcopy(self_hashes))
    monkeypatch.setattr(mod, "make_parent_binding", binding)
    monkeypatch.setattr(mod, "w8_existing_tickers", lambda state: set())
    monkeypatch.setattr(mod, "w7_candidates_from_state", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "w7_outcomes_from_state", lambda *args, **kwargs: [])
    monkeypatch.setattr(mod, "w8_candidate_from_order", candidate_adapter)
    monkeypatch.setattr(mod, "w8_outcomes_from_state", lambda *args, **kwargs: deepcopy(h.outcomes))
    monkeypatch.setattr(mod, "compute_features", feature_adapter)
    monkeypatch.setattr(mod, "log_paths", lambda now: [logpath])
    monkeypatch.setattr(mod, "ExistingTape", FakeTape)
    monkeypatch.setattr(mod, "JsonlTail", FakeLogs)
    h.output.mkdir()
    synthetic = {
        "version": mod.POLICY_VERSION, "registered_at": 1000.0,
        "mode": "conditional_parent_cohort_paper_only", "parameters": PARAMS,
        "parameters_sha256": mod.digest(PARAMS), "own_source_sha256": self_hashes,
        "parent_disk_sha256": parent_hashes,
        "parent_bindings": [binding("w7", h.parents[0]), binding("w8", h.parents[1])],
        "w8_startup_excluded_tickers": [], "episodes": {}, "pending_events": [],
        "source_errors": [], "cycles": 0, "last_tick": None, "log_cursors": {},
    }
    (h.output / "state.json").write_text(json.dumps(synthetic))

    def resume():
        obs = mod.Observer(deepcopy(PARAMS), output=h.output)
        h.observers.append(obs)
        return obs

    h.resume = resume
    h.observer = resume()
    yield h
    for obs in h.observers:
        obs.lock.close()


def test_future_first_quote_retained_and_processed_next_cycle(harness):
    h = harness
    h.clock = 1001.0
    h.events.append(event(at=1001.5))
    h.observer.cycle()
    assert h.calls == []
    assert len(h.observer.state["pending_events"]) == 1
    assert h.observer.state["episodes"] == {}
    h.clock = 1002.0
    h.observer.cycle()
    assert h.calls == ["synthetic-BTC"]
    assert h.feature_calls == [1001.5]
    assert len(h.observer.state["episodes"]) == 1
    assert h.observer.state["pending_events"] == []
    assert h.observer.state["episodes"]["w10:synthetic-BTC"]["decision"]["decision"] == "skip"


def test_malformed_event_does_not_consume_following_valid_first_quote(harness):
    h = harness
    h.events.extend([event("bad", malformed=True), event("good")])
    h.observer.cycle()
    assert h.calls == ["bad", "good"]
    assert list(h.observer.state["episodes"]) == ["w10:good"]
    assert h.observer.state["source_errors"][0]["reason"] == "invalid_source_order"
    assert h.observer.state["source_candidate_unresolved_count"] == 1
    persisted = json.loads((h.output / "state.json").read_text())
    assert len(persisted["episodes"]) == 1


@pytest.mark.parametrize("broken_intent", [None, []])
def test_invalid_intent_shape_does_not_lose_following_quote(harness, broken_intent):
    h = harness
    broken = event("bad")
    broken["intent"] = broken_intent
    h.events.extend([broken, event("good")])
    h.observer.cycle()
    assert list(h.observer.state["episodes"]) == ["w10:good"]
    assert h.observer.state["source_errors"][0]["reason"] == "invalid_source_order"


def test_source_not_ready_timeout_is_unresolved_not_avoided_pnl(harness):
    h = harness
    h.events.append(event(not_ready=True))
    h.observer.cycle()
    assert len(h.observer.state["pending_events"]) == 1
    h.clock = 1093.0  # 91 seconds since first observation, beyond the 90-second allowance.
    summary = h.observer.cycle()["w10"]
    assert h.observer.state["pending_events"] == []
    assert h.observer.state["episodes"] == {}
    assert h.observer.state["source_candidate_unresolved_count"] == 1
    assert h.observer.state["source_errors"][0]["reason"] == "order_state_not_ready_90s"
    assert summary["matched_settled_episodes"] == 0
    assert summary["matched_difference_usd"] == summary["avoided_loss_usd"] == 0
    assert "excludes unresolved" in h.observer.state["comparison_scope"]


def test_parent_binding_change_blocks_new_admissions(harness):
    h = harness
    h.parents[1]["version"] = "two"
    h.events.append(event())
    h.observer.cycle()
    assert not h.observer.state["new_admissions_allowed"]
    assert h.observer.state["source_checks"]["parent_binding_matches"] == [True, False]
    assert h.observer.state["episodes"] == {}
    assert h.calls == []


def test_state_resume_preserves_cursor_decision_and_one_settlement(harness):
    h = harness
    h.events.append(event())
    h.observer.cycle()
    decision = deepcopy(h.observer.state["episodes"]["w10:synthetic-BTC"]["decision"])
    h.observer.lock.close()
    resumed = h.resume()
    h.clock = 1003.0
    resumed.cycle()
    assert h.calls == ["synthetic-BTC"]  # The saved log cursor did not replay the old quote.
    h.events.append(event())  # A duplicate source quote is also economically idempotent.
    h.clock = 1021.0
    h.outcomes.append({"id": "w10:synthetic-BTC", "net_usd": -2.0,
                       "closed_ts": 1020.0, "metadata": {"source_fill_verified": True}})
    summary = resumed.cycle()["w10"]
    assert h.feature_calls == [1001.0]
    assert len(resumed.state["episodes"]) == 1
    assert resumed.state["episodes"]["w10:synthetic-BTC"]["decision"] == decision
    assert summary["settled_episodes"] == 1
    assert summary["matched_control_net_usd"] == -2.0
    assert summary["matched_difference_usd"] == 2.0
    resumed.cycle()
    assert resumed.state["summary"]["w10"] == summary


def test_pending_future_quote_survives_restart(harness):
    h = harness
    h.clock = 1001.0
    h.events.append(event(at=1001.5))
    h.observer.cycle()
    h.observer.lock.close()
    resumed = h.resume()
    h.clock = 1002.0
    resumed.cycle()
    assert h.calls == ["synthetic-BTC"]
    assert len(resumed.state["episodes"]) == 1
    assert resumed.state["pending_events"] == []
