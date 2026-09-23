"""Invariant tests for the independent, read-only paper observation lifecycle."""
import json
from pathlib import Path

from crypto_trading.crypto_strategies.downside_paper.policy import decide, summarize
from crypto_trading.crypto_strategies.downside_paper.tape import JsonlTail, ExistingTape


def candidate(identifier="x", strategy="w9"):
    return {"id": identifier, "strategy": strategy, "side": "no", "asset": "BTC", "decision_ts": 1000.0}


def features(flow=0.8, momentum=1):
    return {"valid": True, "flow_valid": True, "observed_flow_imbalance_1m": flow,
            "momentum_1m_bp": momentum}


PARAMS = {"policy": "price_flow_reversal_v1"}


def test_no_buy_is_adverse_to_no_but_not_yes():
    assert decide(candidate(), features(), PARAMS)["decision"] == "skip"
    yes = dict(candidate(), side="yes")
    assert decide(yes, features(), PARAMS)["decision"] == "accept"
    assert decide(candidate(), features(momentum=-1), PARAMS)["decision"] == "accept"


def test_missing_flow_not_a_false_zero_volume():
    bad = dict(features(), flow_valid=False)
    assert decide(candidate(), bad, PARAMS)["decision"] == "unclassified"
    assert decide(candidate(), dict(features(), momentum_1m_bp=float("nan")), PARAMS)["decision"] == "unclassified"


def test_summary_preserves_sacrificed_profit_and_excludes_missing():
    rows = {}
    for i, (action, net) in enumerate((("skip", -20), ("skip", 2), ("accept", 3), ("unclassified", -50))):
        rows[str(i)] = {"candidate": candidate(str(i)), "decision": {"decision": action}, "outcome": {"net_usd": net}}
    result = summarize(rows)["w9"]
    assert result["all_parent_settled_net_usd"] == -65
    assert result["matched_control_net_usd"] == -15
    assert result["matched_candidate_net_usd"] == 3
    assert result["avoided_loss_usd"] == 20
    assert result["sacrificed_profit_usd"] == 2
    assert result["matched_difference_usd"] == 18


def test_tail_retries_partial_last_line_and_survives_rotation(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_bytes(b'{"x":1}\n{"x":')
    tail = JsonlTail()
    assert tail.read(path) == [{"x": 1}]
    with path.open("ab") as stream:
        stream.write(b'2}\n')
    assert tail.read(path) == [{"x": 2}]
    assert tail.read(path) == []
    path.unlink()
    path.write_text('{"x":3}\n')
    assert tail.read(path) == [{"x": 3}]


def test_tail_seed_end_does_not_replay_past_orders(tmp_path):
    path = tmp_path / "orders.jsonl"
    path.write_text('{"x":"old"}\n')
    tail = JsonlTail()
    tail.seed_end(path)
    assert tail.read(path) == []
    with path.open("a") as stream:
        stream.write('{"x":"new"}\n')
    assert tail.read(path) == [{"x": "new"}]


def test_tape_retains_row_written_after_cycle_cutoff(tmp_path):
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(1000, timezone.utc).strftime("%Y-%m-%d")
    p = tmp_path / "context/BTC" / (day + ".jsonl")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"recv_ts": 1001, "mid_px": "100"}) + "\n")
    tape = ExistingTape(tmp_path, assets=("BTC",))
    tape.update(1000)
    assert tape.for_asset("BTC")[0][0]["recv_ts"] == 1001
    tape.update(1002)
    assert len(tape.for_asset("BTC")[0]) == 1


def test_admission_frozen_even_after_counterfactual_known():
    from crypto_trading.crypto_strategies.downside_paper.observer import record_candidate
    state = {"episodes": {}}
    record_candidate(state, candidate(), features(), 1002, PARAMS)
    assert state["episodes"]["x"]["decision"]["decision"] == "skip"
    assert not record_candidate(state, candidate(), features(flow=-0.8), 1003, PARAMS)
    assert state["episodes"]["x"]["decision"]["decision"] == "skip"


def test_late_discovery_never_claimed_prospective():
    from crypto_trading.crypto_strategies.downside_paper.observer import record_candidate
    state = {"episodes": {}}
    record_candidate(state, candidate(), features(), 1100, PARAMS)
    assert state["episodes"]["x"]["decision"]["decision"] == "unclassified"


def test_package_has_no_execution_or_network_imports():
    import ast
    root = Path(__file__).resolve().parents[1] / "crypto_strategies/downside_paper"
    forbidden = ("requests", "httpx", "urllib", "socket", "subprocess", "live_watch", "execution", "kalshi")
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(part in name for name in names for part in forbidden), (path, names)
