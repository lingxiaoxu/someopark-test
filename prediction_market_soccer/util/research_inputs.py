"""Explicit, immutable research inputs. No default database, config, or output path.

A completed run has a fixed ordered scope, an input manifest, immutable items and
one terminal conclusion for every fixture/track. Completion does not certify PIT:
retrospective reference prices and ambiguous clocks retain those labels.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


class CandidateUnavailable(ValueError):
    code = "candidate_unavailable"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def epoch(value):
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        import math
        if not math.isfinite(value):
            raise ValueError("invalid timestamp")
        return float(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return dt.timestamp()


def _path(path, root, *, exists=False):
    path, root = Path(path), Path(root).resolve(strict=True)
    if not path.is_absolute():
        raise ValueError("explicit absolute path required")
    resolved = path.resolve(strict=exists)
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("path outside isolated root")
    # Refuse aliases rather than accepting an apparently safe symlink spelling.
    if path != resolved:
        raise ValueError("symlink/aliased path refused")
    return resolved


class _SnapshotConnection(sqlite3.Connection):
    def source_raw_path(self, original_ref):
        if self.source_raw_archive is not None:
            return self.source_raw_archive.resolve(original_ref)
        return _path(Path(original_ref), self.source_snapshot_root, exists=True)


def _ro(path, *, factory=sqlite3.Connection):
    # Only standalone snapshots are accepted. Opening a WAL-header backup with
    # mode=ro alone can create empty -wal/-shm files, despite a read-only intent.
    # Check before opening, then prevent SQLite from creating sidecars at all.
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError("snapshot must be checkpointed and immutable")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, factory=factory)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


class SourceSnapshot:
    def __init__(self, path, *, root, fixed_as_of=None, fixture_scope=(), raw_archive=None):
        self.path = _path(path, root, exists=True)
        self.conn = _ro(self.path, factory=_SnapshotConnection)
        self.conn.source_snapshot_root = Path(root).resolve(strict=True)
        self.conn.source_raw_archive = None
        self.raw_archive = None
        sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.manifest = {"snapshot_id": sha, "sha256": sha,
                         "schema_version": self.conn.execute("PRAGMA user_version").fetchone()[0],
                         "fixed_as_of": fixed_as_of, "fixture_scope": list(fixture_scope)}
        try:
            if raw_archive is not None:
                from prediction_market_soccer.util.source_archive import RawArchive
                self.conn.source_raw_archive = RawArchive(raw_archive, root=root, source_sha256=sha, conn=self.conn)
                self.raw_archive = dict(raw_archive)
        except BaseException:
            self.conn.close()
            raise

    def resolve_raw_path(self, original_ref):
        return self.conn.source_raw_path(original_ref)

    def assert_unchanged(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.manifest["sha256"]:
            raise ValueError("source snapshot changed")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


_TERMINAL = {"reconstructed_usable", "retrospective_reference_only", "time_ambiguous",
             "identity_unresolved", "source_unavailable", "unsupported_market"}
_KINDS = {"quote", "state", "features", "result", "series", "diff", "binding"}


class CandidateWriter:
    def __init__(self, path, *, root, run_id, input_manifest, source=None):
        self.root = Path(root).resolve(strict=True)
        self.path = _path(path, root)
        self.source = source
        if source is not None:
            if self.path == source.path or (self.path.exists() and self.path.samefile(source.path)):
                raise ValueError("candidate cannot alias source snapshot")
        # A hard-linked target could mutate a file elsewhere even inside root.
        if self.path.exists() and self.path.stat().st_nlink > 1:
            raise ValueError("hard-linked candidate refused")
        self.run_id = str(run_id)
        self.manifest = json.loads(canonical(input_manifest))
        self.scope_id = self.manifest.get("scope_id")
        self.fixture_ids = self.manifest.get("fixture_ids")
        self.tracks = self.manifest.get("tracks", ["pre", "inplay"])
        if not self.run_id or not self.scope_id or not isinstance(self.fixture_ids, list):
            raise ValueError("run and fixed ordered fixture scope required")
        if len(set(self.fixture_ids)) != len(self.fixture_ids) or not self.tracks or len(set(self.tracks)) != len(self.tracks):
            raise ValueError("duplicate scope identity")
        if any(not isinstance(x, int) or isinstance(x, bool) for x in self.fixture_ids):
            raise ValueError("invalid fixture identity")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS research_run(
              run_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_item(
              run_id TEXT NOT NULL REFERENCES research_run(run_id), kind TEXT NOT NULL,
              fixture_id INTEGER NOT NULL, target_at REAL NOT NULL, side TEXT NOT NULL, market_kind TEXT NOT NULL,
              payload TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
              PRIMARY KEY(run_id,kind,fixture_id,target_at,side,market_kind));
            CREATE TABLE IF NOT EXISTS research_completion(
              run_id TEXT PRIMARY KEY REFERENCES research_run(run_id), payload TEXT NOT NULL, payload_hash TEXT NOT NULL);
        ''')
        for table, keys in [("research_run", "NEW.run_id=run_id"),
                            ("research_item", "NEW.run_id=run_id AND NEW.kind=kind AND NEW.fixture_id=fixture_id AND NEW.target_at=target_at AND NEW.side=side AND NEW.market_kind=market_kind"),
                            ("research_completion", "NEW.run_id=run_id")]:
            self.conn.executescript(f'''
                CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT,'immutable research record'); END;
                CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table}
                WHEN EXISTS(SELECT 1 FROM {table} WHERE {keys})
                BEGIN SELECT RAISE(ABORT,'immutable research conflict'); END;
            ''')
        self.conn.executescript('''
            CREATE TRIGGER IF NOT EXISTS research_item_sealed BEFORE INSERT ON research_item
            WHEN EXISTS(SELECT 1 FROM research_completion WHERE run_id=NEW.run_id)
            BEGIN SELECT RAISE(ABORT,'completed research run'); END;
        ''')
        old = self.conn.execute("SELECT * FROM research_run WHERE run_id=?", (self.run_id,)).fetchone()
        if old:
            prior_manifest = json.loads(old["manifest"])
            run_meta = prior_manifest.get("candidate_run")
            if run_meta is None:
                self.conn.close()
                raise ValueError("legacy candidate run is read-only; use a new run for versioned metadata")
        else:
            run_meta = {"schema_version": 1, "run_id": self.run_id,
                        "created_at": datetime.now(timezone.utc).isoformat()}
        if self.manifest.get("candidate_run") not in (None, run_meta):
            self.conn.close()
            raise ValueError("candidate run metadata conflict")
        self.manifest["candidate_run"] = run_meta
        encoded = canonical(self.manifest)
        if old and (old["manifest"] != encoded or old["scope_id"] != self.scope_id):
            self.conn.close()
            raise ValueError("run_id input conflict")
        if old is None:
            self.conn.execute("INSERT INTO research_run VALUES(?,?,?,?)",
                              (self.run_id, self.scope_id, encoded, digest(self.manifest)))
        self.conn.commit()

    def add(self, kind, fixture_id, target_at, payload, *, status="ok", side="", market_kind="match"):
        if kind not in _KINDS or fixture_id not in self.fixture_ids or status not in {"ok", "unavailable", "invalid"}:
            raise ValueError("invalid candidate item")
        if market_kind not in self.manifest.get("market_kinds", ["match"]):
            raise ValueError("market outside scope")
        target = epoch(target_at)
        key = (self.run_id, kind, fixture_id, target, side, market_kind)
        encoded, sha = canonical(payload), digest(payload)
        old = self.conn.execute("SELECT * FROM research_item WHERE run_id=? AND kind=? AND fixture_id=? AND target_at=? AND side=? AND market_kind=?", key).fetchone()
        if old:
            if old["payload"] != encoded or old["status"] != status:
                raise ValueError("immutable candidate item conflict")
            return sha
        self.conn.execute("INSERT INTO research_item VALUES(?,?,?,?,?,?,?,?,?)", (*key, encoded, sha, status))
        self.conn.commit()
        return sha

    def finish(self, statuses):
        """statuses is [{fixture_id,track,status,reason?}], covering every scope pair."""
        statuses = list(statuses)
        expected = {(fid, track) for fid in self.fixture_ids for track in self.tracks}
        actual = [(s["fixture_id"], s["track"]) for s in statuses]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("completion must cover exact fixture/track scope")
        if any(s["status"] not in _TERMINAL for s in statuses):
            raise ValueError("pending/retryable candidate cannot be completed")
        if self.source is not None:
            self.source.assert_unchanged()
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM research_item WHERE run_id=? ORDER BY kind,fixture_id,target_at,side,market_kind", (self.run_id,))]
        payload = {"run_id": self.run_id, "scope_id": self.scope_id, "input_manifest_hash": digest(self.manifest),
                   "items_hash": digest(rows), "scope_hash": digest(self.fixture_ids), "statuses": statuses,
                   "status": "completed"}
        old = self.conn.execute("SELECT payload FROM research_completion WHERE run_id=?", (self.run_id,)).fetchone()
        if old is not None:
            if json.loads(old[0]) != payload:
                raise ValueError("completion conflict")
            return payload
        self.conn.execute("INSERT INTO research_completion VALUES(?,?,?)", (self.run_id, canonical(payload), digest(payload)))
        self.conn.commit()
        return payload

    def close(self):
        self.conn.close()


def candidate_method_hashes():
    """Direct candidate arithmetic/input consumers; not a claim about historical availability."""
    files=('strategy/decision_model.py','strategy/edge.py','model/inplay.py','model/inplay_constants.py',
           'strategy/smart_exit.py','ops/settle_bets.py','ops/strategy_candidate.py','util/pricing.py',
           'util/match_timeline.py','util/price_history.py','util/research_inputs.py')
    base=Path(__file__).resolve().parents[1]
    return {rel:hashlib.sha256((base/rel).read_bytes()).hexdigest() for rel in files}


class CandidateMarketData:
    def __init__(self, path, *, root, run_id, scope_id, source=None):
        self.source = source
        self.root = Path(root).resolve(strict=True)
        self.path = _path(path, root, exists=True)
        self.snapshot_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.conn = _ro(self.path)
        self.run_id, self.scope_id = run_id, scope_id
        try:
            run = self.conn.execute("SELECT * FROM research_run WHERE run_id=?", (run_id,)).fetchone()
            completed = self.conn.execute("SELECT * FROM research_completion WHERE run_id=?", (run_id,)).fetchone()
            if not run or run["scope_id"] != scope_id or not completed:
                raise CandidateUnavailable("run is missing, incomplete, or from another scope")
            self.manifest = json.loads(run["manifest"])
            self.run_metadata = self.manifest.get("candidate_run")
            if self.run_metadata is not None:
                if self.run_metadata.get("schema_version") != 1 or self.run_metadata.get("run_id") != run_id:
                    raise CandidateUnavailable("candidate schema/run metadata mismatch")
                epoch(self.run_metadata.get("created_at"))
            self.completion = json.loads(completed["payload"])
            self.items = [dict(r) for r in self.conn.execute("SELECT * FROM research_item WHERE run_id=? ORDER BY kind,fixture_id,target_at,side,market_kind", (run_id,))]
            if digest(self.manifest) != run["manifest_hash"] or digest(self.completion) != completed["payload_hash"] or digest(self.items) != self.completion["items_hash"]:
                raise CandidateUnavailable("candidate hash mismatch")
            expected = {(fid, t) for fid in self.manifest["fixture_ids"] for t in self.manifest.get("tracks", ["pre", "inplay"])}
            conclusions = self.completion["statuses"]
            if len(conclusions) != len(expected) or {(s["fixture_id"], s["track"]) for s in conclusions} != expected or any(s["status"] not in _TERMINAL for s in conclusions):
                raise CandidateUnavailable("candidate scope incomplete")
            if self.completion["input_manifest_hash"] != digest(self.manifest) or self.completion["scope_hash"] != digest(self.manifest["fixture_ids"]):
                raise CandidateUnavailable("candidate input manifest mismatch")
            for row in self.items:
                row["data"] = json.loads(row["payload"])
                if digest(row["data"]) != row["payload_hash"]:
                    raise CandidateUnavailable("candidate item hash mismatch")
        except Exception:
            self.conn.close()
            raise

    def _result(self, status, data=None, reason=None):
        return {"status": status, "data": data, "reason": reason,
                "provenance": {"run_id": self.run_id, "scope_id": self.scope_id,
                               "input_manifest_hash": self.completion["input_manifest_hash"]}}

    def _rows(self, kind, fid, cutoff, market_kind="match"):
        if fid not in self.manifest["fixture_ids"]:
            raise CandidateUnavailable("fixture outside candidate scope")
        at = epoch(cutoff)
        return [r for r in self.items if r["kind"] == kind and r["fixture_id"] == fid and r["market_kind"] == market_kind and r["target_at"] <= at]

    def _asof(self, kind, fid, cutoff, market_kind="match"):
        rows = self._rows(kind, fid, cutoff, market_kind)
        if not rows:
            return self._result("unavailable", reason="missing_" + kind)
        row = max(rows, key=lambda r: r["target_at"])
        if row["status"] != "ok":
            return self._result(row["status"], reason=row["data"].get("reason", "invalid_" + kind))
        data = row["data"]
        available = data.get("available_at", data.get("known_at"))
        if available is None or epoch(available) > epoch(cutoff):
            return self._result("unavailable", reason=kind + "_not_available_at_cutoff")
        if kind == "state" and row["target_at"] != epoch(cutoff) and (data.get("valid_until") is None or epoch(data["valid_until"]) < epoch(cutoff)):
            return self._result("unavailable", reason="state_not_valid_at_target")
        if kind == "state" and data.get("certainty") != "verified":
            return self._result("unavailable", reason="time_ambiguous")
        return self._result("ok", data)

    def state_at(self, fid, cutoff):
        return self._asof("state", fid, cutoff)

    def features_at(self, fid, cutoff):
        return self._asof("features", fid, cutoff)

    def result_at(self, fid, cutoff, market_kind="match"):
        return self._asof("result", fid, cutoff, market_kind)

    def quotes_at(self, fid, cutoff, market_kind="match", required_sides=None, *, action="buy"):
        required_sides = required_sides or (["home", "draw", "away"] if market_kind == "match" else ["home", "away"])
        rows = self._rows("quote", fid, cutoff, market_kind)
        selected = {}
        for side in required_sides:
            candidates = [r for r in rows if r["side"] == side]
            if not candidates:
                return self._result("unavailable", reason="missing_quote:" + side)
            row = max(candidates, key=lambda r: r["target_at"])
            q = row["data"]
            if row["status"] != "ok":
                return self._result(row["status"], reason=q.get("reason", "quote_unavailable"))
            from prediction_market_soccer.util.price_history import _finite
            price = q.get("ask" if action == "buy" else "bid") if self.manifest.get("require_observed_quotes") else q.get("price", q.get("reference_price"))
            if not _finite(price) or not 0 <= price <= 1:
                return self._result("invalid", reason="invalid_candidate_quote")
            sample = q.get("sample_ts", q.get("provider_sample_at"))
            if sample is None or not 0 <= epoch(cutoff) - epoch(sample) <= 180:
                return self._result("unavailable", reason="quote_not_causal_or_stale")
            if self.manifest.get("require_observed_quotes", False):
                if self.source is None or self.manifest.get('source_snapshot',{}).get('sha256') != self.source.manifest['sha256']:
                    return self._result("unavailable", reason="verified_source_snapshot_required")
                from prediction_market_soccer.util.source_history import resolve_receipt
                from prediction_market_soccer.util.quote_evidence import qualify_quote, validate_receipt
                r=q.get('receipt')
                if not validate_receipt(r):
                    return self._result('invalid',reason='invalid_observed_receipt')
                raw=self.source.conn.execute('SELECT raw_ref FROM quote_receipt_v1 WHERE receipt_id=?',(r['receipt_id'],)).fetchone()
                if not raw:
                    return self._result('unavailable',reason='receipt_not_in_source_snapshot')
                try:
                    self.source.resolve_raw_path(raw[0])
                except ValueError:
                    return self._result('unavailable',reason='raw_object_outside_candidate_root')
                at=datetime.fromtimestamp(epoch(cutoff),timezone.utc).isoformat()
                durable=resolve_receipt(self.source.conn,r['receipt_id'],at)
                if durable is None or durable['payload_hash'] != r['payload_hash']:
                    return self._result('unavailable',reason='receipt_not_available_at_cutoff')
                qualified=qualify_quote(q,action,fixture_id=fid,side=side,market_kind=market_kind,
                                        settlement_scope='advance' if market_kind=='advance' else 'regulation',
                                        now=at,max_age_seconds=180,require_available=True,
                                        persisted_available_at=durable['available_at'])
                if not qualified['eligible']:
                    return self._result('unavailable',reason=qualified['reason'])
            selected[side] = q
        return self._result("ok", selected)

    def exit_path(self, fid, side, after, until, market_kind="match"):
        rows = [r for r in self._rows("quote", fid, until, market_kind) if r["side"] == side and r["target_at"] > epoch(after)]
        if not rows:
            return self._result("unavailable", reason="missing_exit_path")
        path = []
        for row in rows:
            q = self.quotes_at(fid, row["target_at"], market_kind, [side], action="sell")
            state = self.state_at(fid, row["target_at"])
            if q["status"] != "ok" or state["status"] != "ok":
                return self._result("unavailable", reason=q["reason"] or state["reason"])
            path.append({"target_at": row["target_at"], "quote": q["data"][side], "state": state["data"]})
        times = [r["target_at"] for r in rows]
        if times[0] - epoch(after) > 180 or epoch(until) - times[-1] > 180 or any(b-a > 180 for a,b in zip(times,times[1:])):
            return self._result("unavailable", reason="exit_path_has_unobserved_gap")
        # A manifest must declare the path end. Merely receiving a few ticks is not
        # evidence that no later exit occurred.
        ends = self.manifest.get("exit_path_until", {})
        declared = ends.get(str(fid), {}).get(side)
        if declared is None or epoch(declared) < epoch(until):
            return self._result("unavailable", reason="exit_path_coverage_unknown")
        return self._result("ok", path)

    def assert_unchanged(self):
        if hashlib.sha256(self.path.read_bytes()).hexdigest()!=self.snapshot_sha256:
            raise CandidateUnavailable('candidate database changed after validation')

    def parameters(self):
        """Construct exactly declared parameters; never fill missing fields from today."""
        from dataclasses import fields
        from prediction_market_soccer.config.config import DecisionConfig, RiskConfig
        values=[]
        for name,cls in [('decision_parameters',DecisionConfig),('risk_parameters',RiskConfig)]:
            body=self.manifest.get(name)
            if not isinstance(body,dict) or set(body)!={f.name for f in fields(cls)}:
                raise CandidateUnavailable('complete fixed '+name+' required')
            values.append(cls(**body))
        return tuple(values)

    def verify_method_code(self):
        hashes=self.manifest.get('code_hashes',{})
        for rel,sha in candidate_method_hashes().items():
            if hashes.get(rel)!=sha:
                raise CandidateUnavailable('candidate method code mismatch: '+rel)

    def close(self):
        self.conn.close()
