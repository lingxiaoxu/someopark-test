"""Independent integration checks for the new W9 paper observer.

Every file and tape is synthetic under tmp_path. No real recorder, strategy
state, model weight, network client, service or launch agent is used.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from crypto_trading.crypto_strategies.w9_rnn_paper import observer as mod


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


ENTRY = epoch("2026-09-16T01:36:58Z")
EXPIRY = epoch("2026-09-16T01:45:00Z")


def parent_row(asset, opened=ENTRY, side="yes", cost=.9):
    return {"ticker": f"KX{asset}15M-26SEP152145-45", "series": f"KX{asset}15M",
            "opened": iso(opened), "close": iso(EXPIRY), "leg": "band",
            "side": side, "cost": cost}


def prediction(asset="BTC", at=ENTRY, risk=6., threshold=3.):
    minute = int(at // 60) * 60
    return {"asset": asset, "decision_ts": minute, "prediction_ts": minute,
            "generated_at": minute+1., "persisted_at": minute+2.,
            "model_available_ts": minute-120., "model_id": "synthetic-model-v1",
            "feature_last_recorded_receipt_ts": minute-61.,
            "strict_recorded_pit_eligible": True, "risk_available": True,
            "pred_abs_return_augmented": risk,
            "prior_calibration_risk_q90_augmented": threshold,
            "mode": "live_paper_forecast", "origin": "live_forecast"}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "crypto_trading"
    source = root / "trading_signals/live_watch/w7_noisefade_state.json"
    source.parent.mkdir(parents=True)
    parent = {"version": "synthetic-w7-current", "main_registered_at": "2026-09-06T00:00:00Z",
              "trades": [], "positions": {}}
    source.write_text(json.dumps(parent))
    source_code = root / "parent.py"
    own_code = root / "new-observer.py"
    source_code.write_text("# protected parent\n")
    own_code.write_text("# isolated W9\n")
    parameters = tmp_path / "parameters.json"
    parameters.write_bytes(mod.PARAMETERS.read_bytes())
    monkeypatch.setattr(mod, "ROOT", root)
    monkeypatch.setattr(mod, "SOURCE", source)
    monkeypatch.setattr(mod, "PARAMETERS", parameters)
    monkeypatch.setattr(mod, "PARENT_FILES", [source_code])
    monkeypatch.setattr(mod, "own_files", lambda: [own_code, parameters])

    class FakeTape:
        def __init__(self, _):
            self.tail = SimpleNamespace(errors=0)

        def update(self, _):
            pass

        def for_asset(self, asset):
            return [asset], [], []

    monkeypatch.setattr(mod, "ExistingTape", FakeTape)
    monkeypatch.setattr(mod, "compute_features", lambda context, book, trades, at: {
        "decision_ts": at, "valid": True, "flow_valid": True,
        "momentum_1m_bp": 1., "observed_flow_imbalance_1m": .2})
    clock = [ENTRY-30]
    output = root / "trading_signals/w9_rnn_paper"
    instances = []

    def new():
        instance = mod.Observer(output, clock=lambda: clock[0])
        instances.append(instance)
        return instance

    def write(rows=(), trades=(), **overrides):
        state = deepcopy(parent)
        state.update(overrides)
        state["positions"] = {r["ticker"]: {k: v for k, v in r.items() if k != "ticker"} for r in rows}
        state["trades"] = list(trades)
        mod.atomic_json(source, state)

    def publish(rows):
        output.mkdir(parents=True, exist_ok=True)
        with (output / "live_predictions.jsonl").open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row)+"\n")

    yield SimpleNamespace(root=root, source=source, source_code=source_code,
                          own_code=own_code, parameters=parameters, clock=clock,
                          output=output, new=new, write=write, publish=publish)
    for instance in instances:
        instance.lock.close()


def test_registration_excludes_historical_entries_and_historical_pnl(sandbox):
    sandbox.write([parent_row("ETH", ENTRY-31)])
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    result = observer.cycle()
    assert result["new_decisions"] == 0
    assert observer.state["book"]["episodes"] == {}
    assert observer.state["historical_research_pnl_included"] is False
    assert observer.state["summary"]["all_registered"]["net_pnl_usd"] == 0


def test_entire_atomic_parent_batch_uses_one_common_quantity(sandbox):
    observer = sandbox.new()
    sandbox.publish([prediction("BTC"), prediction("ETH", risk=1.)])
    sandbox.write([parent_row("BTC"), parent_row("ETH")])
    sandbox.clock[0] = ENTRY+2
    assert observer.cycle()["new_decisions"] == 2
    episodes = list(observer.state["book"]["episodes"].values())
    assert {e["decision"]["quantity"] for e in episodes} == {12}
    assert all(e["decision"]["batch_admitted_members"] == 2 for e in episodes)
    assert sum(e["decision"]["rnn_risk_high"] for e in episodes) == 1
    assert all(e["decision"]["full_feature_known"] for e in episodes)
    journal = [json.loads(line) for line in (sandbox.output / "decisions.jsonl").read_text().splitlines()]
    assert len(journal) == 2
    assert len({r["event_id"] for r in journal}) == 2


def test_restart_idempotent_decision_and_settlement(sandbox):
    observer = sandbox.new()
    sandbox.publish([prediction()])
    row = parent_row("BTC")
    sandbox.write([row])
    sandbox.clock[0] = ENTRY+2
    observer.cycle()
    observer.lock.close()
    restarted = sandbox.new()
    assert restarted.cycle()["new_decisions"] == 0
    settled = {**row, "closed": iso(EXPIRY+180), "win": True,
               "pnl_c": round((1-row["cost"]-.07*row["cost"]*(1-row["cost"]))*100, 2)}
    sandbox.write(trades=[settled])
    sandbox.clock[0] = EXPIRY+181
    assert restarted.cycle()["new_settlements"] == 1
    expected = settled["pnl_c"] * 12 / 100
    assert restarted.state["summary"]["all_registered"]["net_pnl_usd"] == pytest.approx(expected)
    assert restarted.cycle()["new_settlements"] == 0
    assert len((sandbox.output / "decisions.jsonl").read_text().splitlines()) == 1
    assert len((sandbox.output / "settlements.jsonl").read_text().splitlines()) == 1


def test_changed_parent_binding_blocks_admission(sandbox):
    observer = sandbox.new()
    sandbox.write([parent_row("BTC")], version="changed-parent")
    sandbox.clock[0] = ENTRY+2
    result = observer.cycle()
    assert result["new_admissions_allowed"] is False
    assert observer.state["source_checks"]["parent_binding_matches"] is False
    assert observer.state["book"]["episodes"] == {}


def test_changed_entry_same_id_blocks_before_idempotent_filter(sandbox):
    observer = sandbox.new()
    sandbox.write([parent_row("BTC")])
    sandbox.clock[0] = ENTRY+2
    observer.cycle()
    before = deepcopy(observer.state["book"])
    sandbox.write([parent_row("BTC", cost=.91), parent_row("ETH", opened=ENTRY+3)])
    sandbox.clock[0] = ENTRY+4
    with pytest.raises(mod.policy.SourceRevisionError, match="entry changed"):
        observer.cycle()
    assert observer.state["book"] == before
    assert observer.state["new_admissions_allowed"] is False
    assert observer.state["source_checks"]["parent_entries_unchanged"] is False
    revision = next(iter(observer.state["source_entry_revisions"].values()))
    assert revision["original_candidate"]["metadata"]["entry_price"] == .9
    assert revision["observed_candidate"]["metadata"]["entry_price"] == .91


def test_registration_uses_same_policy_version_as_book(sandbox):
    observer = sandbox.new()
    assert observer.state["version"] == observer.state["book"]["policy_version"] == mod.policy.POLICY_VERSION


@pytest.mark.parametrize("filename", ["source_code", "own_code"])
def test_changed_source_bytes_block_admission(sandbox, filename):
    observer = sandbox.new()
    getattr(sandbox, filename).write_text("# modified\n")
    sandbox.write([parent_row("BTC")])
    sandbox.clock[0] = ENTRY+2
    assert observer.cycle()["new_admissions_allowed"] is False
    assert observer.state["book"]["episodes"] == {}


def test_late_persisted_forecast_not_backdated_into_signal(sandbox):
    observer = sandbox.new()
    late = prediction()
    late["persisted_at"] = ENTRY+1
    sandbox.publish([late])
    sandbox.write([parent_row("BTC")])
    sandbox.clock[0] = ENTRY+2
    observer.cycle()
    episode = next(iter(observer.state["book"]["episodes"].values()))
    assert episode["prediction"] is None
    assert episode["decision"]["model_known"] is False
    assert episode["decision"]["quantity"] == 25
    assert episode["decision"]["flow_known"] is True


def test_source_discovered_after_deadline_never_retroactively_allocated(sandbox):
    observer = sandbox.new()
    sandbox.write([parent_row("BTC")])
    sandbox.clock[0] = ENTRY+91
    assert observer.cycle()["new_decisions"] == 0
    assert len(observer.state["missed_candidates"]) == 1
    assert observer.state["book"]["episodes"] == {}
    assert observer.cycle()["new_decisions"] == 0


def test_state_committed_before_journal_failure_repairs_on_restart(sandbox, monkeypatch):
    observer = sandbox.new()
    sandbox.write([parent_row("BTC")])
    sandbox.clock[0] = ENTRY+2
    real_sync = mod.sync_journal
    monkeypatch.setattr(mod, "sync_journal", lambda *_: (_ for _ in ()).throw(OSError("synthetic journal failure")))
    with pytest.raises(OSError, match="synthetic journal failure"):
        observer.cycle()
    saved = json.loads((sandbox.output / "state.json").read_text())
    assert len(saved["book"]["episodes"]) == len(saved["decision_events"]) == 1
    observer.lock.close()
    monkeypatch.setattr(mod, "sync_journal", real_sync)
    restarted = sandbox.new()
    assert restarted.cycle()["new_decisions"] == 0
    journal = [json.loads(line) for line in (sandbox.output / "decisions.jsonl").read_text().splitlines()]
    assert len(journal) == 1
    assert journal[0]["event_id"] == saved["decision_events"][0]["event_id"]


def test_torn_final_journal_line_repaired_without_duplicate_event(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = {"event_id": "decision:first", "n": 1}
    second = {"event_id": "decision:second", "n": 2}
    path.write_bytes((json.dumps(first)+"\n"+'{"event_id":"decision:second"').encode())
    mod.sync_journal(path, [first, second])
    mod.sync_journal(path, [first, second])
    assert [json.loads(line) for line in path.read_text().splitlines()] == [first, second]


def test_output_cannot_overlap_parent_state_folder(sandbox):
    with pytest.raises(ValueError, match="separate"):
        mod.Observer(sandbox.source.parent / "new-child", clock=lambda: ENTRY)


def test_parameter_change_cannot_reuse_registered_state(sandbox):
    observer = sandbox.new()
    observer.lock.close()
    params = json.loads(sandbox.parameters.read_text())
    params["risk_contract_cap"] = 11
    sandbox.parameters.write_text(json.dumps(params))
    with pytest.raises(ValueError, match="New parameters"):
        sandbox.new()


@pytest.mark.parametrize("field,value", [
    ("generated_at", ENTRY+1), ("persisted_at", ENTRY+1),
    ("model_available_ts", ENTRY+1), ("feature_last_recorded_receipt_ts", ENTRY+1),
    ("prediction_ts", ENTRY+1), ("prediction_ts", ENTRY-61),
    ("strict_recorded_pit_eligible", False), ("risk_available", False),
    ("origin", "historical_replay"), ("pred_abs_return_augmented", float("nan")),
])
def test_prediction_selection_requires_actual_preentry_availability(field, value):
    row = prediction()
    row[field] = value
    assert mod.select_prediction([row], "BTC", ENTRY) is None


def test_rejected_late_latest_prediction_does_not_hide_prior_eligible():
    good = prediction()
    bad = {**good, "prediction_ts": good["prediction_ts"]+1, "persisted_at": ENTRY+1}
    assert mod.select_prediction([good, bad], "BTC", ENTRY) == good
