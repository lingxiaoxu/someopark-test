"""W9/W10 prospective conditional-cohort paper observer, exclusively local reads.

W9 inherits W7 MAIN paper fills and fees. W10 admits or skips a whole W8 tilted
market at its first posted order, including all subsequent management. This is
not an independent execution simulation; excluded episodes do not free parent
capital or generate counterfactual parent orders. No execution modules imported.
"""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from .features import compute_features
from .policy import POLICY_VERSION, decide, summarize
from .sources import (SourceNotReady, make_parent_binding,
                      w7_candidates_from_state, w7_outcomes_from_state,
                      w8_candidate_from_order, w8_existing_tickers,
                      w8_outcomes_from_state)
from .tape import ExistingTape, JsonlTail

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "trading_signals/live_watch"
OUTPUT = ROOT / "trading_signals/w9_w10_paper"
WATCHED = [ROOT / "crypto_strategies/live_watch" / n for n in
           ("w7_noisefade.py", "w8_complete_set.py", "config.yaml")]
WATCHED += [ROOT / "crypto_strategies/event_binary/complete_set.py"]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in WATCHED}


def own_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob("*.py")}


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_parents():
    return [json.loads((SOURCE / (n + "_state.json")).read_text())
            for n in ("w7_noisefade", "w8_complete_set")]


def log_paths(now):
    date = datetime.fromtimestamp(now, timezone.utc)
    return [SOURCE / ("log_" + (date - timedelta(days=delta)).strftime("%Y-%m-%d") + ".jsonl")
            for delta in (1, 0)]


def event_timestamp(event):
    value = event.get("ts")
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def record_candidate(state, candidate, feature, observed_at, parameters):
    """Idempotent admission; never revise a decision after observing a result."""
    identifier = candidate["id"]
    if identifier in state["episodes"]:
        return False
    lag = observed_at - float(candidate["decision_ts"])
    if lag < 0 or lag > 90:
        decision = {"decision": "unclassified", "reason": "late_source_discovery"}
    else:
        decision = decide(candidate, feature, parameters)
    state["episodes"][identifier] = {
        "candidate": candidate, "features": feature, "decision": decision,
        "observed_at": observed_at, "discovery_lag_seconds": lag, "outcome": None,
    }
    return True


class Observer:
    def __init__(self, parameters, output=OUTPUT):
        self.output = Path(output)
        # The program cannot be pointed at an existing strategy's source directory.
        if self.output.resolve() == SOURCE.resolve() or SOURCE.resolve() in self.output.resolve().parents:
            raise ValueError("output must be isolated from live_watch")
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = (self.output / "observer.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.parameters = parameters
        self.tape = ExistingTape(ROOT / "price_data/hyperliquid")
        self.logs = JsonlTail()
        self.state_path = self.output / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if self.state["parameters_sha256"] != digest(parameters):
                raise ValueError("parameters changed: create a separate research registration")
            self.logs.cursors = {k: tuple(v) for k, v in self.state.get("log_cursors", {}).items()}
        else:
            before = time.time()
            parents = load_parents()
            hashes = source_hashes()
            bindings = [make_parent_binding(strategy, parent)
                        for strategy, parent in zip(("w7", "w8"), parents)]
            # Capture current EOF before defining registration. No earlier order is admitted.
            for path in log_paths(before):
                self.logs.seed_end(path)
            registered = time.time()
            self.state = {
                "version": POLICY_VERSION, "registered_at": registered,
                "mode": "conditional_parent_cohort_paper_only",
                "parameters": parameters, "parameters_sha256": digest(parameters),
                "own_source_sha256": own_hashes(), "parent_disk_sha256": hashes,
                "parent_bindings": bindings,
                "w8_startup_excluded_tickers": sorted(w8_existing_tickers(parents[1])),
                "episodes": {}, "pending_events": [], "source_errors": [],
                "cycles": 0, "last_tick": None, "log_cursors": self.logs.cursors,
                "limitations": [
                    "No new market data or orders; recorded parent paper fills are inherited.",
                    "W8 paired control is not added to tilted PNL.",
                    "No independent inventory/capital rerun after skipping an episode.",
                    "W8 registered kernel differs from disk; parent loaded source remains unverified.",
                    "Observed print notional is incomplete venue volume.",
                    "Unclassified episodes are excluded from matched comparisons.",
                    "Review after 300 classified settled expiry windows per candidate; not proof by repeated peeking.",
                ],
            }
            atomic_json(self.output / "registration.json", self.state)
            atomic_json(self.state_path, self.state)

    def cycle(self):
        now = time.time()
        parents = load_parents()
        bindings = [make_parent_binding(strategy, parent)
                    for strategy, parent in zip(("w7", "w8"), parents)]
        matching = [binding == expected for binding, expected in zip(bindings, self.state["parent_bindings"])]
        sources_unchanged = source_hashes() == self.state["parent_disk_sha256"]
        own_unchanged = own_hashes() == self.state["own_source_sha256"]
        can_admit = all(matching) and sources_unchanged and own_unchanged
        self.state["new_admissions_allowed"] = can_admit
        self.state["source_checks"] = {"parent_binding_matches": matching,
                                        "parent_disk_unchanged": sources_unchanged,
                                        "observer_disk_unchanged": own_unchanged}
        self.tape.update(now)
        events = self.state["pending_events"][:]
        for path in log_paths(now):
            events.extend(self.logs.read(path))
        self.state["pending_events"] = []
        since = self.state["registered_at"]
        candidates = []
        if can_admit:
            candidates.extend(w7_candidates_from_state(parents[0], bindings[0], since_ts=since, until_ts=now))
            for event in events:
                if event.get("strategy") != "w8_complete_set" or event.get("action") != "paper_order" or event.get("book") != "tilted":
                    continue
                intent = event.get("intent")
                if not isinstance(intent, dict):
                    self.state["source_errors"].append({"at": now, "ticker": None, "reason": "invalid_source_order", "detail": "intent is not a mapping"})
                    continue
                ticker = intent.get("ticker")
                if ticker in self.state["w8_startup_excluded_tickers"]:
                    continue
                try:
                    # Log writes may race this cycle's frozen cutoff. Carry them
                    # forward instead of consuming a still-future first quote.
                    if event_timestamp(event) > now:
                        self.state["pending_events"].append(event)
                        continue
                    candidate = w8_candidate_from_order(event, parents[1], bindings[1], since_ts=since, until_ts=now)
                except SourceNotReady:
                    # An atomic parent-state write may trail the emitted order event.
                    event.setdefault("_paper_observer_first_seen", now)
                    if now - event["_paper_observer_first_seen"] <= 90:
                        self.state["pending_events"].append(event)
                    else:
                        self.state["source_errors"].append({"at": now, "ticker": ticker, "reason": "order_state_not_ready_90s"})
                    continue
                except (ValueError, KeyError, TypeError) as error:
                    self.state["source_errors"].append({"at": now, "ticker": ticker, "reason": "invalid_source_order", "detail": str(error)})
                    continue
                if candidate:
                    candidates.append(candidate)
        for candidate in sorted(candidates, key=lambda c: (c["decision_ts"], c["id"])):
            if candidate["id"] in self.state["episodes"]:
                continue
            feature = compute_features(*self.tape.for_asset(candidate["asset"]), candidate["decision_ts"])
            record_candidate(self.state, candidate, feature, now, self.parameters)
        for index, extractor in enumerate((w7_outcomes_from_state, w8_outcomes_from_state)):
            if not matching[index]:
                continue
            for outcome in extractor(parents[index], bindings[index], until_ts=now):
                episode = self.state["episodes"].get(outcome["id"])
                if episode is None:
                    continue
                if episode["outcome"] is None:
                    episode["outcome"] = outcome
                elif episode["outcome"] != outcome:
                    # Preserve original evaluation and explicitly surface source reconciliation.
                    episode["outcome_revision"] = outcome
                    episode["source_outcome_changed"] = True
        self.state["summary"] = summarize(self.state["episodes"])
        unresolved = self.state.setdefault("unresolved_source_tickers", {})
        for error in self.state["source_errors"]:
            if error.get("ticker"):
                unresolved.setdefault(error["ticker"], error)
        self.state["source_candidate_unresolved_count"] = len(unresolved)
        self.state["comparison_scope"] = "successfully attributed prospective parent episodes; excludes unresolved source candidates and unclassified features"
        self.state["cycles"] += 1
        self.state["last_tick"] = now
        self.state["log_cursors"] = self.logs.cursors
        self.state["tape_parse_errors"] = self.tape.tail.errors
        self.state["source_errors"] = self.state["source_errors"][-1000:]
        atomic_json(self.state_path, self.state)
        return self.state["summary"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True, type=Path)
    parser.add_argument("--loop", type=float, default=0)
    args = parser.parse_args()
    observer = Observer(json.loads(args.parameters.read_text()))
    while True:
        start = time.time()
        try:
            summary = observer.cycle()
            print(json.dumps({"at": start, "summary": summary}), flush=True)
        except Exception as error:
            print(json.dumps({"at": start, "error_type": type(error).__name__, "error": str(error)}), flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(max(1, args.loop - (time.time() - start)))


if __name__ == "__main__":
    main()
