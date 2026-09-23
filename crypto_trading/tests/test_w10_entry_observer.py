"""Independent W10 integration checks; every source and output is synthetic.

These tests exercise the real first-order/settlement adapters but never read
the live parent, write any existing strategy, or connect to an exchange.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from crypto_trading.crypto_strategies.w10_entry_paper import observer as mod


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


ENTRY = epoch("2026-09-16T03:06:00Z")
EXPIRY = epoch("2026-09-16T03:15:00Z")
TICKER = "KXBTC15M-26SEP152315-15"


def market(*, side="yes", price=.71, fills=()):
    return {"ticker": TICKER, "series": "KXBTC15M", "close_ts": EXPIRY,
            "orders": [{"id": TICKER+":1", "side": side, "price": price,
                        "quantity": 1., "created_ts": ENTRY,
                        "activate_ts": ENTRY+.5, "expires_ts": ENTRY+20,
                        "remaining": 1., "queue_ahead": 5.}],
            "fills": list(fills)}


def order_event(*, logged=ENTRY+.1, side="yes", price=.71, number=1):
    return {"ts": iso(logged), "strategy": "w8_complete_set", "action": "paper_order",
            "book": "tilted", "intent": {"ticker": TICKER,
                "client_order_id": "w8-paper-"+TICKER+f":{number}",
                "side": "bid" if side == "yes" else "ask",
                "price": f"{price if side == 'yes' else 1-price:.4f}",
                "count": "1.00", "post_only": True, "expiration_time": int(ENTRY+20),
                "submitted": False, "mode": "observation"}}


def settlement(*, net=.28, zero=False, unverified=0):
    return {"ticker": TICKER, "series": "KXBTC15M", "close_ts": EXPIRY,
            "result": "yes", "settled_at": iso(EXPIRY+180),
            "fills": 0 if zero else 1, "quantity": 0 if zero else 1,
            "cost_usd": 0 if zero else .71, "fees_usd": 0 if zero else .01,
            "payout_usd": 0 if zero else .72+net, "net_usd": 0 if zero else net,
            "paired_net_usd": 0, "residual_net_usd": 0 if zero else net,
            "coverage_gap": False, "unverified_order_quantity": unverified}


def fill(at):
    return {"ts": at, "order_id": TICKER+":1", "side": "yes", "price": .71,
            "quantity": 1., "fee_usd": .01, "liquidity": "maker_model"}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path/"crypto_trading"
    source = root/"trading_signals/live_watch/w8_complete_set_state.json"
    source.parent.mkdir(parents=True)
    source_code = root/"parent.py"
    own_code = root/"new-observer.py"
    source_code.write_text("# protected W8\n")
    own_code.write_text("# isolated W10\n")
    parameters = tmp_path/"parameters.json"
    parameters.write_bytes(mod.PARAMETERS.read_bytes())
    output = root/"trading_signals/w10_entry_paper"
    logfile = source.parent/"log_synthetic.jsonl"
    logfile.write_text("")
    parent = {"version": "synthetic-w8-v8a", "registered_at": "2026-09-14T05:17:35Z",
              "source_sha256": {"kernel.py": "a"*64}, "last_tick_ts": ENTRY-30,
              "books": {"paired": {"complete_sets": True, "positions": {}, "trades": []},
                        "tilted": {"complete_sets": False, "positions": {}, "trades": []}}}
    source.write_text(json.dumps(parent))
    for key, value in {"ROOT": root, "SOURCE": source, "PARAMETERS": parameters,
                       "PARENT_FILES": [source_code], "RUNTIME": output}.items():
        monkeypatch.setattr(mod, key, value)
    monkeypatch.setattr(mod, "own_files", lambda: [own_code, parameters])
    monkeypatch.setattr(mod, "log_paths", lambda _: [logfile])

    class FakeTape:
        def __init__(self, *args, **kwargs):
            self.tail = SimpleNamespace(errors=0)

        def update(self, _):
            pass

        def for_asset(self, asset):
            return [asset], [], []

    monkeypatch.setattr(mod, "ExistingTape", FakeTape)
    feature_calls = []
    feature = {"valid": True, "flow_valid": True, "momentum_1m_bp": 1.,
               "observed_flow_imbalance_1m": .2}

    def compute(_context, _books, _trades, at):
        feature_calls.append(at)
        return {"decision_ts": at, **feature}

    monkeypatch.setattr(mod, "compute_features", compute)
    clock = [ENTRY-30]
    instances = []

    def new():
        instance = mod.Observer(output, clock=lambda: clock[0])
        instances.append(instance)
        return instance

    def write(*, positions=None, trades=(), paired=None, **extra):
        state = deepcopy(parent)
        state["last_tick_ts"] = clock[0]
        state.update(extra)
        state["books"]["tilted"]["positions"] = positions or {}
        state["books"]["tilted"]["trades"] = list(trades)
        if paired is not None:
            state["books"]["paired"].update(paired)
        mod.atomic_json(source, state)

    def publish(*events):
        with logfile.open("a") as stream:
            for event in events:
                stream.write(json.dumps(event)+"\n")

    yield SimpleNamespace(root=root, source=source, source_code=source_code,
                          own_code=own_code, parameters=parameters, output=output,
                          logfile=logfile, clock=clock, feature=feature,
                          feature_calls=feature_calls, new=new, write=write,
                          publish=publish)
    for instance in instances:
        instance.lock.close()


def admit(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    sandbox.publish(order_event())
    result = observer.cycle()
    assert result["new_decisions"] == 1
    return observer


def test_registration_seeds_eof_and_never_imports_historical_profit(sandbox):
    sandbox.publish(order_event(logged=ENTRY-31))
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    assert observer.cycle()["new_decisions"] == 0
    assert observer.state["episodes"] == {}


def test_preexisting_ticker_in_paired_book_cannot_be_new_tilted_episode(sandbox):
    sandbox.write(paired={"positions": {TICKER: {"orders": [], "fills": []}}})
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    sandbox.publish(order_event())
    assert observer.cycle()["new_decisions"] == 0
    assert observer.state["episodes"] == {}


def test_first_quote_uses_created_clock_not_log_or_observer_clock(sandbox):
    observer = admit(sandbox)
    episode = next(iter(observer.state["episodes"].values()))
    assert episode["candidate"]["decision_ts"] == ENTRY
    assert episode["observed_at"] == ENTRY+2
    assert episode["decision_published_at"] >= ENTRY+2
    assert ENTRY in sandbox.feature_calls


def test_log_before_atomic_parent_order_waits_then_admits_once(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write()
    sandbox.publish(order_event())
    assert observer.cycle()["new_decisions"] == 0
    assert len(observer.state["pending_events"]) == 1
    sandbox.clock[0] = ENTRY+4
    sandbox.write(positions={TICKER: market()})
    assert observer.cycle()["new_decisions"] == 1
    assert observer.cycle()["new_decisions"] == 0


def test_future_appended_event_survives_cutoff_and_restart(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY
    sandbox.write(positions={TICKER: market()})
    sandbox.publish(order_event())
    assert observer.cycle()["new_decisions"] == 0
    assert observer.state["pending_events"]
    observer.lock.close()
    restarted = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    assert restarted.cycle()["new_decisions"] == 1
    assert not restarted.state["pending_events"]


def test_replacement_cannot_reenter_previously_rejected_market(sandbox):
    sandbox.feature["momentum_1m_bp"] = -1.
    observer = admit(sandbox)
    initial = deepcopy(next(iter(observer.state["episodes"].values()))["decision"])
    sandbox.feature["momentum_1m_bp"] = 1.
    sandbox.clock[0] = ENTRY+5
    sandbox.publish(order_event(logged=ENTRY+4, number=2))
    observer.cycle()
    assert len(observer.state["episodes"]) == 1
    assert next(iter(observer.state["episodes"].values()))["decision"] == initial


@pytest.mark.parametrize("filename", ["source_code", "own_code"])
def test_changed_source_bytes_block_admission(sandbox, filename):
    observer = sandbox.new()
    getattr(sandbox, filename).write_text("# altered\n")
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    sandbox.publish(order_event())
    assert observer.cycle()["new_admissions_allowed"] is False
    assert observer.state["episodes"] == {}


def test_changed_parent_registration_blocks_admission(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()}, version="changed-parent")
    sandbox.publish(order_event())
    assert observer.cycle()["new_admissions_allowed"] is False
    assert observer.state["episodes"] == {}


def test_stale_parent_blocks_admission(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()}, last_tick_ts=ENTRY-100)
    sandbox.publish(order_event())
    assert observer.cycle()["new_admissions_allowed"] is False
    assert observer.state["episodes"] == {}


def test_changed_active_entry_without_new_log_is_quarantined(sandbox):
    observer = admit(sandbox)
    before = deepcopy(next(iter(observer.state["episodes"].values()))["candidate"])
    sandbox.clock[0] = ENTRY+5
    sandbox.write(positions={TICKER: market(price=.72)})
    observer.cycle()
    episode = next(iter(observer.state["episodes"].values()))
    assert episode["candidate"] == before
    assert episode["source_candidate_changed"] is True
    assert observer.state["new_admissions_allowed"] is False


def test_restarts_preserve_one_decision_and_one_whole_market_settlement(sandbox):
    observer = admit(sandbox)
    observer.lock.close()
    restarted = sandbox.new()
    assert restarted.cycle()["new_decisions"] == 0
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement()], paired={"trades": [settlement(net=999.)]})
    assert restarted.cycle()["new_settlements"] == 1
    assert restarted.cycle()["new_settlements"] == 0
    episode = next(iter(restarted.state["episodes"].values()))
    assert episode["outcome"]["net_usd"] == pytest.approx(.28)
    assert len((sandbox.output/"decisions.jsonl").read_text().splitlines()) == 1
    assert len((sandbox.output/"settlements.jsonl").read_text().splitlines()) == 1


def test_settlement_revision_does_not_overwrite_original_cashflow(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement()])
    observer.cycle()
    sandbox.clock[0] += 2
    sandbox.write(trades=[settlement(net=.27)])
    observer.cycle()
    episode = next(iter(observer.state["episodes"].values()))
    assert episode["outcome"]["net_usd"] == pytest.approx(.28)
    assert episode["source_outcome_changed"] is True


@pytest.mark.parametrize("relative", ["trading_signals/live_watch/child", "trading_signals/w9_rnn_paper",
                                      "trading_signals/w9_w10_paper", "price_data/new"])
def test_output_cannot_write_other_project_runtime_paths(sandbox, relative):
    with pytest.raises(ValueError):
        mod.Observer(sandbox.root/relative, clock=lambda: ENTRY)


def test_parameter_change_requires_new_registration(sandbox):
    observer = sandbox.new()
    observer.lock.close()
    parameters = json.loads(sandbox.parameters.read_text())
    parameters["policy"] += "-changed"
    sandbox.parameters.write_text(json.dumps(parameters))
    with pytest.raises(ValueError):
        sandbox.new()


def test_torn_final_journal_line_repaired_without_duplicates(tmp_path):
    path = tmp_path/"journal.jsonl"
    first = {"event_id": "decision:first", "n": 1}
    second = {"event_id": "decision:second", "n": 2}
    path.write_bytes((json.dumps(first)+"\n"+'{"event_id":"decision:second"').encode())
    mod.sync_journal(path, [first, second])
    mod.sync_journal(path, [first, second])
    assert [json.loads(line) for line in path.read_text().splitlines()] == [first, second]


def test_observed_fill_after_durable_decision_permits_matched_whole_cashflow(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = ENTRY+5
    sandbox.write(positions={TICKER: market(fills=[fill(ENTRY+4)])})
    observer.cycle()
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement()], paired={"trades": [settlement(net=999.)]})
    summary = observer.cycle()["summary"]
    assert summary["matched_settled_episodes"] == 1
    assert summary["matched_control_net_usd"] == pytest.approx(.28)
    assert summary["matched_candidate_net_usd"] == pytest.approx(.28)
    assert summary["arms"]["parent"]["filled_expiry_windows"] == 1


def test_fill_reported_later_but_predating_decision_invalidates_matched_pnl(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = ENTRY+5
    sandbox.write(positions={TICKER: market(fills=[fill(ENTRY+1)])})
    observer.cycle()
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement()])
    summary = observer.cycle()["summary"]
    assert summary["matched_settled_episodes"] == 0
    assert summary["exclusion_reasons"]["pit_evidence_issue"] == 1
    assert summary["matched_candidate_net_usd"] == 0
    assert next(iter(observer.state["episodes"].values()))["outcome"]["net_usd"] == pytest.approx(.28)


def test_filled_settlement_without_first_fill_timing_is_not_claimed_pit(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement()])
    summary = observer.cycle()["summary"]
    assert summary["matched_settled_episodes"] == 0
    assert next(iter(observer.state["episodes"].values()))["pit_evidence_issue"] == "parent_fill_timing_not_observed_before_settlement"


def test_zero_fill_is_zero_economics_and_not_profitable_evidence(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement(zero=True)])
    summary = observer.cycle()["summary"]
    assert summary["matched_settled_episodes"] == 1
    assert summary["matched_settled_expiry_windows"] == 1
    assert summary["matched_candidate_net_usd"] == 0
    assert summary["arms"]["parent"]["filled_expiry_windows"] == 0
    assert summary["arms"]["candidate"]["filled_win_rate"] is None


def test_invalid_flow_is_never_credited_as_avoided_loss(sandbox):
    sandbox.feature["flow_valid"] = False
    observer = admit(sandbox)
    sandbox.clock[0] = ENTRY+5
    sandbox.write(positions={TICKER: market(fills=[fill(ENTRY+4)])})
    observer.cycle()
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement(net=-.72)])
    summary = observer.cycle()["summary"]
    assert summary["matched_settled_episodes"] == 0
    assert summary["unclassified_episodes"] == 1
    assert summary["avoided_loss_usd"] == 0


@pytest.mark.parametrize("wrapped", [None, [], "invalid"])
def test_bad_event_wrapper_does_not_lose_next_valid_first_quote(sandbox, wrapped):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+2
    sandbox.write(positions={TICKER: market()})
    sandbox.publish({"event": wrapped}, order_event())
    assert observer.cycle()["new_decisions"] == 1
    assert len(observer.state["episodes"]) == 1
    assert observer.state["source_errors"]


def test_decision_unconfirmed_at_restart_cannot_later_become_pit_eligible(sandbox):
    observer = admit(sandbox)
    path = sandbox.output/"state.json"
    state = json.loads(path.read_text())
    next(iter(state["episodes"].values()))["decision_published_at"] = None
    mod.atomic_json(path, state)
    observer.lock.close()
    restarted = sandbox.new()
    sandbox.clock[0] = ENTRY+5
    sandbox.write(positions={TICKER: market(fills=[fill(ENTRY+4)])})
    restarted.cycle()
    row = next(iter(restarted.state["episodes"].values()))
    assert row["decision_published_at"] is None
    assert row["pit_evidence_issue"] == "decision_publication_not_confirmed_before_restart"


def test_late_discovery_stays_unclassified_not_retroactive_gate(sandbox):
    observer = sandbox.new()
    sandbox.clock[0] = ENTRY+91
    sandbox.write(positions={TICKER: market()})
    sandbox.publish(order_event())
    observer.cycle()
    row = next(iter(observer.state["episodes"].values()))
    assert row["decision"] == {"decision": "unclassified", "reason": "late_source_discovery"}
    assert row["legacy_decision"]["decision"] == "unclassified"


def test_journal_cache_does_not_rescan_unchanged_archive(tmp_path, monkeypatch):
    path = tmp_path/"journal.jsonl"
    records = [{"event_id": "one", "value": 1}]
    cache = {}
    mod.sync_journal(path, records, cache)
    original_open = Path.open
    reads = []

    def tracked_open(self, mode="r", *args, **kwargs):
        if self == path and "r" in mode:
            reads.append(mode)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    mod.sync_journal(path, records, cache)
    mod.sync_journal(path, records, cache)
    assert reads == []


def test_journal_cache_rescans_external_signature_change(tmp_path, monkeypatch):
    path = tmp_path/"journal.jsonl"
    first = {"event_id": "one", "value": 1}
    second = {"event_id": "two", "value": 2}
    cache = {}
    mod.sync_journal(path, [first], cache)
    with path.open("a") as stream:
        stream.write(json.dumps(second)+"\n")
    original_open = Path.open
    reads = []

    def tracked_open(self, mode="r", *args, **kwargs):
        if self == path and "r" in mode:
            reads.append(mode)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    mod.sync_journal(path, [first, second], cache)
    assert reads == ["rb+"]
    assert cache[str(path)]["ids"] == {"one", "two"}
    with original_open(path) as stream:
        assert [json.loads(line) for line in stream] == [first, second]


def test_diagnostics_are_not_pruned_until_archive_is_durable(sandbox, monkeypatch):
    observer = admit(sandbox)
    original = deepcopy(observer.state["quote_diagnostics"])
    assert original
    original_sync = mod.sync_journal

    def fail_diagnostic_archive(path, records, cache=None):
        if path.name == "quote_diagnostics.jsonl":
            raise OSError("synthetic diagnostic publication failure")
        return original_sync(path, records, cache)

    monkeypatch.setattr(mod, "sync_journal", fail_diagnostic_archive)
    with pytest.raises(OSError, match="publication failure"):
        observer.persist(set(), {TICKER})
    assert observer.state["quote_diagnostics"] == original
    assert json.loads((sandbox.output/"state.json").read_text())["quote_diagnostics"] == original
    monkeypatch.setattr(mod, "sync_journal", original_sync)
    observer.persist(set(), {TICKER})
    assert observer.state["quote_diagnostics"] == {}
    assert observer.state["archived_quote_diagnostic_records"] == len(original)
    archived = [json.loads(line) for line in (sandbox.output/"quote_diagnostics.jsonl").read_text().splitlines()]
    assert {row["id"] for row in archived} == set(original)


def test_crash_before_prune_commit_replays_archive_without_duplicates(sandbox):
    observer = admit(sandbox)
    old_state = deepcopy(observer.state)
    count = len(old_state["quote_diagnostics"])
    assert count
    observer.persist(set(), {TICKER})
    mod.atomic_json(sandbox.output/"state.json", old_state)
    observer.lock.close()
    restarted = sandbox.new()
    restarted.persist(set(), {TICKER})
    assert restarted.state["quote_diagnostics"] == {}
    assert restarted.state["archived_quote_diagnostic_records"] == count
    archived = [json.loads(line) for line in (sandbox.output/"quote_diagnostics.jsonl").read_text().splitlines()]
    assert len(archived) == count
    assert len({row["event_id"] for row in archived}) == count


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), "2026-09-16T03:06:00"])
def test_timestamp_rejects_ambiguous_or_invalid_values(bad):
    with pytest.raises(ValueError):
        mod.timestamp(bad)


def test_snapshot_fill_after_frozen_cutoff_is_carried_until_next_cycle(sandbox):
    observer = admit(sandbox)
    sandbox.clock[0] = ENTRY+3
    sandbox.write(positions={TICKER: market(fills=[fill(ENTRY+4)])})
    observer.cycle()
    row = next(iter(observer.state["episodes"].values()))
    assert row["first_parent_fill_ts"] is None
    sandbox.clock[0] = ENTRY+5
    observer.cycle()
    assert row["first_parent_fill_ts"] == ENTRY+4
    assert not row.get("pit_evidence_issue")


def test_missing_position_without_settlement_retains_original_diagnostics(sandbox):
    observer = admit(sandbox)
    original = deepcopy(observer.state["quote_diagnostics"])
    sandbox.clock[0] = ENTRY+5
    sandbox.write()
    observer.cycle()
    assert observer.state["quote_diagnostics"] == original
    sandbox.clock[0] = ENTRY+7
    sandbox.write(positions={TICKER: market()})
    observer.cycle()
    assert observer.state["quote_diagnostics"] == original


def test_archived_ticker_tombstone_prevents_reclassified_diagnostic_ids(sandbox):
    observer = admit(sandbox)
    original = (sandbox.output/"quote_diagnostics.jsonl").read_text()
    sandbox.clock[0] = EXPIRY+181
    sandbox.write(trades=[settlement(zero=True)])
    observer.cycle()
    assert observer.state["quote_diagnostics"] == {}
    assert TICKER in observer.state["archived_quote_tickers"]
    sandbox.clock[0] += 2
    sandbox.write(positions={TICKER: market()}, trades=[settlement(zero=True)])
    observer.cycle()
    assert observer.state["quote_diagnostics"] == {}
    assert (sandbox.output/"quote_diagnostics.jsonl").read_text() == original
