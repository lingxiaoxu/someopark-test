"""Build a full batch privately; promote only after every required step succeeds."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops.run_status import (
    atomic_bytes, publication_lock, read_json_doc, strategy_ledger_version,
)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExportStage:
    def __enter__(self):
        self.original = CONFIG.paths
        self.original.ensure()
        self.root = Path(tempfile.mkdtemp(prefix=".refresh-stage-", dir=self.original.data))
        self.baseline = {}
        for key in ("output", "frontend_data"):
            source = getattr(self.original, key)
            target = self.root / key
            shutil.copytree(source, target)
            self.baseline[key] = {p.relative_to(target): _digest(p) for p in target.rglob("*") if p.is_file()}
        object.__setattr__(CONFIG, "paths", replace(self.original, output=self.root / "output",
                                                  frontend_data=self.root / "frontend_data"))
        return self

    def promote(self):
        # The minute loop can publish while a long batch is calculating. Keep a
        # newer live artifact instead of rolling it back to the start-of-batch view.
        live_files = {"inplay_live.json", "inplay_live_advance.json", "upcoming.json",
                      "xv_matches.json", "milestone_marks.json", "risk_report.json"}
        def stamp(path):
            try:
                doc = json.loads(path.read_text())
                value = doc.get("as_of") or doc.get("ts") or doc.get("generated_at")
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except (OSError, ValueError, TypeError, AttributeError):
                return None
        with publication_lock():
            target_versions = {}
            for key in ("output", "frontend_data"):
                staged = self.root / key
                version = strategy_ledger_version(read_json_doc(staged / "performance_report.json"))
                target_versions[key] = version
                if version:
                    marks_version = strategy_ledger_version(read_json_doc(staged / "milestone_marks.json"))
                    if marks_version != version:
                        raise ValueError("Performance and price track must publish the same strategy ledger version")
                    pdf = staged / "performance_report.pdf"
                    if not pdf.is_file():
                        raise ValueError("Canonical strategy publication requires its PnL PDF")
                    previous = strategy_ledger_version(read_json_doc(getattr(self.original, key) / "performance_report.json"))
                    if previous != version and self.baseline[key].get(Path(pdf.name)) == _digest(pdf):
                        raise ValueError("A new strategy ledger requires a regenerated PnL PDF")
            if target_versions["output"] != target_versions["frontend_data"]:
                raise ValueError("Output and frontend must publish the same strategy ledger version")
            preserve = set()
            for key in ("output", "frontend_data"):
                relatives = set(self.baseline[key]) | {Path(name) for name in live_files}
                for relative in relatives:
                    if relative.name not in live_files:
                        continue
                    baseline = self.baseline[key].get(relative)
                    dst, src = getattr(self.original, key) / relative, self.root / key / relative
                    if dst.is_file() and _digest(dst) != baseline:
                        if relative.name == "milestone_marks.json" and target_versions[key]:
                            if strategy_ledger_version(read_json_doc(dst)) != target_versions[key]:
                                continue
                        current_ts, staged_ts = stamp(dst), stamp(src)
                        if current_ts is None or staged_ts is None or current_ts > staged_ts:
                            preserve.add(relative)
            # Preserve a live price snapshot only if BOTH published copies carry
            # this batch's ledger; never let one matching copy mask a stale one.
            for relative in list(preserve):
                if relative.name == "milestone_marks.json" and any(target_versions.values()):
                    if any(strategy_ledger_version(read_json_doc(getattr(self.original, key) / relative))
                           != target_versions[key] for key in ("output", "frontend_data")):
                        preserve.discard(relative)
            changes = []
            for key in ("output", "frontend_data"):
                source, destination = self.root / key, getattr(self.original, key)
                for path in sorted(source.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(source)
                    if relative in preserve or self.baseline[key].get(relative) == _digest(path):
                        continue
                    dst = destination / relative
                    changes.append((dst, path.read_bytes(), dst.read_bytes() if dst.exists() else None))
            completed = []
            try:
                for dst, payload, old in changes:
                    completed.append((dst, old))
                    atomic_bytes(dst, payload)
            except BaseException:
                # Every per-file replacement is atomic; roll back the batch if a
                # later replacement fails. The publication lock excludes the live writer.
                for dst, old in reversed(completed):
                    if old is None:
                        dst.unlink(missing_ok=True)
                    else:
                        atomic_bytes(dst, old)
                raise

    def __exit__(self, *exc):
        object.__setattr__(CONFIG, "paths", self.original)
        shutil.rmtree(self.root)
