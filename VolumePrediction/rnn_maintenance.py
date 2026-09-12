"""Monthly RNN candidate training and daily observation; promotion is explicit.

The training launchd job calls --train-monthly. daily_update calls
run_candidate_shadows after publishing the incumbent's forecast. Only --promote
can change the serving version, and it requires a successful forward review.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import OUT, get_logger

log = get_logger("rnn_maintenance")


def _service(service=None):
    if service is None:
        from VolumePrediction.service import VolumeService
        return VolumeService()
    return service


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    os.replace(tmp, path)


def candidate_versions(service=None) -> list[str]:
    svc = _service(service)
    data = svc.registry.load()
    incumbent = (data.get("blend") or {}).get("rnn_version")
    return sorted(version for version, rec in data.get("models", {}).items()
                  if rec.get("kind") == "learned.rnn"
                  and rec.get("status") == "candidate" and version != incumbent)


def _incumbent_at_training(svc, version: str) -> str:
    rec = svc.registry.load().get("models", {}).get(version, {})
    meta = rec.get("meta") or {}
    incumbent = meta.get("incumbent_version") or meta.get("previous_rnn_version")
    if not incumbent:
        path = svc.art / "registry" / "artifacts" / version / "meta.json"
        artifact = json.loads(path.read_text()) if path.exists() else {}
        provenance = artifact.get("refreeze") or {}
        incumbent = provenance.get("incumbent_version") or provenance.get("previous_rnn_version")
    if not incumbent:
        raise ValueError(f"candidate {version} lacks its incumbent at training")
    return incumbent


def run_candidate_shadows(service=None) -> dict:
    """Update each candidate's own state and report; never publish/promote it."""
    from VolumePrediction import shadow_rnn
    from VolumePrediction.rnn_candidate_review import review_candidate
    svc = _service(service)
    results = {}
    for version in candidate_versions(svc):
        try:
            rc = shadow_rnn.run_daily(version=version, service=svc)
            if rc:
                results[version] = {"status": "error", "rc": rc}
                log.error(f"candidate {version} shadow failed: rc={rc}")
                continue
            report = review_candidate(version, artifacts_dir=svc.art,
                                      expected_incumbent_version=_incumbent_at_training(svc, version))
            _atomic_json(svc.art / "shadow_rnn" / "candidates" / version / "review.json",
                         report)
            results[version] = report
            log.info(f"candidate {version} review: {report.get('status')}")
        except Exception as exc:  # A candidate must not interrupt production updates.
            log.exception(f"candidate {version} observation failed")
            results[version] = {"status": "error", "error": str(exc)}
    return {"status": "error" if any(r.get("status") == "error"
                                     for r in results.values()) else "ok",
            "candidates": results}


def train_monthly(*, dry_run: bool = False, service=None,
                  now: Optional[pd.Timestamp] = None) -> dict:
    """Wait for a fresh monthly panel; do not compete with Saturday jobs."""
    from VolumePrediction import refreeze_rnn
    svc = _service(service)
    now = now if now is not None else pd.Timestamp.now(tz="America/New_York")
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("America/New_York")
    else:
        now = now.tz_convert("America/New_York")
    ym = now.strftime("%Y-%m")
    if now.dayofweek == 5:
        return {"status": "skipped", "reason": "Saturday reserved for heavy VP jobs",
                "exit_code": 0}
    data = svc.registry.load()
    for version, rec in data.get("models", {}).items():
        if (rec.get("kind") == "learned.rnn"
                and str(rec.get("recorded_at", "")).startswith(ym)
                and rec.get("status") in {"candidate", "production", "serving"}
                and (svc.art / "registry" / "artifacts" / version / "meta.json").exists()):
            return {"status": "skipped", "reason": "RNN candidate already built this month",
                    "version": version, "exit_code": 0}
    planned = refreeze_rnn.plan(out_dir=svc.art, raw_dir=svc.raw, now=now)
    # A stale panel must not create a fresh-looking monthly maintenance stamp.
    from VolumePrediction.data import polygon_loader as pl
    raw_days = svc._raw_dates()
    asof = planned.get("asof")
    if not asof or not raw_days:
        return {"status": "waiting", "reason": "no complete panel/raw date",
                "plan": planned, "exit_code": 3}
    age = len([d for d in pl.trading_days(asof, raw_days[-1]) if d > asof])
    if age > 10:
        return {"status": "waiting", "reason": "panel more than 10 trading days behind raw",
                "panel_age_sessions": age, "plan": planned, "exit_code": 3}
    result = refreeze_rnn.run(dry_run=dry_run, out_dir=svc.art, raw_dir=svc.raw, now=now)
    if not dry_run and result.get("exit_code", 0) == 0:
        result["observation"] = run_candidate_shadows(svc)
        if result["observation"].get("status") == "error":
            result["exit_code"] = 1
    return result


def promote_candidate(version: str, *, by: str, service=None) -> dict:
    """Explicit reviewed switch, carrying the candidate's matching same-day cache."""
    from VolumePrediction.rnn_candidate_review import review_candidate
    from VolumePrediction.service import _rnn_cache
    from VolumePrediction.prod_model_rnn import next_trading_day
    svc = _service(service)
    if version not in candidate_versions(svc):
        raise ValueError("version is not a registered, non-serving RNN candidate")
    report = review_candidate(version, artifacts_dir=svc.art,
                              expected_incumbent_version=_incumbent_at_training(svc, version))
    if report.get("status") != "ready":
        raise ValueError(f"candidate is not ready: {report.get('status')}")
    data = svc.registry.load()
    current = (data.get("blend") or {}).get("rnn_version")
    incumbent = report.get("incumbent_version")
    if not incumbent or incumbent != current:
        raise ValueError("review incumbent does not match the currently serving RNN")
    asof = svc._raw_dates()[-1]
    art = svc.art / "registry" / "artifacts" / version
    meta = json.loads((art / "meta.json").read_text())
    if meta.get("seq_tail_date") != next_trading_day(asof):
        raise ValueError("candidate state is not current; run candidate observation first")
    frame = _rnn_cache(svc.art, version, asof, meta["trained_through"])
    cache = art / f"blend_serve_{asof}.parquet"
    tmp = cache.with_suffix(".tmp")
    frame.reset_index().to_parquet(tmp, index=False)
    os.replace(tmp, cache)
    # No weights or inference run after the pointer changes. Its ready cache is in place.
    switched = svc.ops.set_blend(True, rnn_version=version, by=by)
    data = svc.registry.load()
    data["models"][version]["status"] = "serving"
    data["models"][version]["promotion_review"] = report
    svc.registry.save(data)
    _atomic_json(art / "promotion.json", {"by": by, "previous_version": current,
                 "asof": asof, "review": report, "blend": switched})
    return {"status": "promoted", "version": version, "previous_version": current,
            "asof": asof, "blend": switched}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--train-monthly", action="store_true")
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--promote", metavar="VERSION")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--by", default="user_approved_20260909")
    args = ap.parse_args()
    if args.promote:
        if args.dry_run:
            from VolumePrediction.rnn_candidate_review import review_candidate
            result = review_candidate(args.promote)
        else:
            result = promote_candidate(args.promote, by=args.by)
    elif args.train_monthly:
        result = train_monthly(dry_run=args.dry_run)
    elif args.dry_run:
        result = {"status": "dry_run", "candidates": candidate_versions()}
    else:
        result = run_candidate_shadows()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return result.get("exit_code", 1 if result.get("status") == "error" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
