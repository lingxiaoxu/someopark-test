"""Review an isolated RNN candidate's forward shadow evidence; never promote it.

The six held metrics must be computed on exactly the same positive, finite
candidate/incumbent/MA5/actual intersection. ``n_held_expected`` is the incumbent
and MA5's eligible held universe before intersecting the candidate. A row without
that coverage evidence cannot qualify. Forward evidence comes from the saved
candidate forecast, not an assertion in the tracking CSV: its actual creation
timestamp must precede the target NYSE session's open. Legacy naive timestamps
are interpreted in America/New_York, matching the project's launchd host.

The default gate requires five completed, out-of-training forward sessions,
sample-weighted MAPE and log-MSE no worse than the incumbent and strictly better
than MA5, and both metrics no worse than the incumbent on >=60% of sessions.
These are operational acceptance rules, not a statistical significance claim.
``ready`` means evidence is ready for review; it never authorizes or executes a
production change. Bad forward rows block ready rather than being silently
discarded to improve a candidate's score.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT

ET = "America/New_York"
METRICS = tuple(f"{arm}_held_{metric}" for arm in ("rnn", "prod", "ma5")
                for metric in ("mape", "log_mse"))
NONBLOCKING_EXCLUSIONS = {
    "different_candidate_version", "inside_training_window", "not_forward",
    "actual_session_not_complete",
}


def _day(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("expected YYYY-MM-DD")
    return pd.Timestamp(text).strftime("%Y-%m-%d")


def _timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError("timestamp missing")
    return ts.tz_localize(ET) if ts.tzinfo is None else ts.tz_convert(ET)


def _text(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _positive_count(value: Any) -> int:
    n = float(value)
    if not math.isfinite(n) or n <= 0 or not n.is_integer():
        raise ValueError("count must be a positive integer")
    return int(n)


def _session(pred_date: str, actual_date: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Calendar-only lookup; does not access market data or the network."""
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("NYSE").schedule(pred_date, actual_date)
    days = [d.strftime("%Y-%m-%d") for d in sched.index]
    if days != [pred_date, actual_date]:
        raise ValueError("actual_date must be the next NYSE session after pred_date")
    actual = sched.iloc[-1]
    return _timestamp(actual.market_open), _timestamp(actual.market_close)


def _next_session(pred_date: str) -> str:
    import pandas_market_calendars as mcal
    end = (pd.Timestamp(pred_date) + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    schedule = mcal.get_calendar("NYSE").schedule(pred_date, end)
    days = [d.strftime("%Y-%m-%d") for d in schedule.index]
    if len(days) < 2 or days[0] != pred_date:
        raise ValueError("forecast input date is not a NYSE session")
    return days[1]


def _forecast_evidence(path: Path, version: str, trained_through: str,
                       pred_date: str, opened: pd.Timestamp) -> dict:
    """Read saved provenance; later historical replays fail the open-time gate."""
    if not path.is_file():
        return {"ok": False, "reason": "forecast_evidence_missing", "path": str(path)}
    try:
        df = pd.read_parquet(path)
        needed = {"model_version", "trained_through", "date", "generated_at"}
        if df.empty or not needed.issubset(df.columns):
            raise ValueError("required provenance columns missing")
        for col, expected in (("model_version", version),
                              ("trained_through", trained_through),
                              ("date", pred_date)):
            values = df[col].map(str)
            # date may be serialized by parquet as midnight timestamps.
            if col == "date":
                values = df[col].map(lambda x: pd.Timestamp(x).strftime("%Y-%m-%d"))
            if not values.eq(expected).all():
                return {"ok": False, "reason": f"forecast_{col}_mismatch",
                        "path": str(path)}
        times = [_timestamp(t) for t in df.generated_at.unique()]
        latest, earliest = max(times), min(times)
        result = {"ok": latest < opened, "path": str(path),
                  "generated_at_min": earliest.isoformat(),
                  "generated_at_max": latest.isoformat(),
                  "target_market_open": opened.isoformat(),
                  "naive_timestamp_timezone": ET}
        if not result["ok"]:
            result["reason"] = "not_forward"
        elif earliest.strftime("%Y-%m-%d") < pred_date:
            result.update(ok=False, reason="forecast_created_before_input_date")
        return result
    except (ValueError, TypeError, OSError, KeyError, ImportError) as exc:
        return {"ok": False, "reason": "forecast_evidence_invalid",
                "detail": type(exc).__name__, "path": str(path)}


def review_candidate(version: str, *, trained_through: str | None = None,
                     artifacts_dir: str | Path | None = None,
                     candidate_dir: str | Path | None = None,
                     artifact_dir: str | Path | None = None,
                     expected_incumbent_version: str | None = None,
                     min_days: int = 5, min_held: int = 30,
                     min_coverage_ratio: float = 0.8,
                     max_coverage_drop: float = 0.2,
                     daily_win_fraction: float = 0.6,
                     now: str | datetime | pd.Timestamp | None = None) -> dict:
    """Return an explanatory JSON-safe report, with no writes or promotions.

    All accepted days are used, with duplicate actual dates rejected rather than
    choosing a favorable rerun. MAPE/log-MSE are weighted by common held count to
    recover the equally weighted stock-day mean from each day's aggregate. This
    does not assume stock-days are statistically independent. Coverage is also
    compared with the previous valid day's ratio to reveal sudden loss.

    Explicit directories support isolated synthetic tests and standalone review;
    metadata, if present, must agree with the requested training date/version.
    """
    if not version or Path(version).name != version or version in {".", ".."}:
        raise ValueError("version must be a single artifact directory name")
    if min_days < 5 or min_held < 1:
        raise ValueError("require at least five days and at least one held ticker")
    if not 0 < min_coverage_ratio <= 1 or not 0 <= max_coverage_drop < 1:
        raise ValueError("invalid coverage thresholds")
    if not 0.6 <= daily_win_fraction <= 1:
        raise ValueError("daily_win_fraction must be between 0.6 and 1")
    root = Path(artifacts_dir) if artifacts_dir else OUT
    cdir = Path(candidate_dir) if candidate_dir else root / "shadow_rnn" / "candidates" / version
    adir = Path(artifact_dir) if artifact_dir else root / "registry" / "artifacts" / version
    meta_path = adir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    if meta and meta.get("version") != version:
        raise ValueError("artifact metadata version mismatch")
    if trained_through and meta and meta.get("trained_through") != trained_through:
        raise ValueError("requested trained_through disagrees with artifact")
    trained_through = _day(trained_through or meta.get("trained_through", ""))
    current = _timestamp(now if now is not None else pd.Timestamp.now(tz="UTC"))
    track = cdir / "rnn_ab_tracking.csv"
    report = {
        "schema_version": 1, "model_version": version,
        "trained_through": trained_through, "generated_at": current.isoformat(),
        "status": "observing", "promotion_performed": False,
        "automatic_promotion_authorized": False,
        "decision_scope": "Read-only evidence review; production switching is a separate action.",
        "tracking_csv": str(track), "expected_incumbent_version": expected_incumbent_version,
        "incumbent_version": expected_incumbent_version,
        "thresholds": {"min_days": min_days, "min_held": min_held,
                       "min_coverage_ratio": min_coverage_ratio,
                       "max_coverage_drop": max_coverage_drop,
                       "daily_win_fraction": daily_win_fraction},
        "aggregation": "common-held-count weighted stock-day mean; no significance claim",
        "accepted_days": [], "excluded_rows": [], "reasons": [],
        "gates": {}, "summary": {},
    }
    if not track.is_file():
        report["reasons"] = ["tracking_file_missing", "insufficient_forward_days"]
        report["summary"] = {"n_forward_days": 0, "days_remaining": min_days}
        return report
    try:
        rows = pd.read_csv(track).to_dict("records")
    except (ValueError, OSError, pd.errors.ParserError, pd.errors.EmptyDataError):
        report["status"] = "failed"
        report["reasons"] = ["tracking_file_invalid"]
        return report

    # Only same-candidate duplicate targets are ambiguous; unrelated legacy rows
    # are listed as excluded without stealing valid candidate observation days.
    dates = [_text(r.get("actual_date")) for r in rows
             if _text(r.get("candidate_version")) == version]
    duplicates = {d for d in dates if dates.count(d) > 1}
    previous_coverage = None
    for i, row in sorted(enumerate(rows), key=lambda item: _text(item[1].get("actual_date"))):
        rec = {"csv_row": i + 2, "pred_date": _text(row.get("pred_date")),
               "actual_date": _text(row.get("actual_date")), "reasons": []}
        reasons = rec["reasons"]
        if _text(row.get("candidate_version")) != version:
            reasons.append("different_candidate_version")
        try:
            pred_date, actual_date = _day(rec["pred_date"]), _day(rec["actual_date"])
        except (ValueError, TypeError):
            reasons.append("invalid_dates")
            pred_date = actual_date = None
        if actual_date and actual_date <= trained_through:
            reasons.append("inside_training_window")
        if reasons:
            report["excluded_rows"].append(rec)
            continue
        if actual_date in duplicates:
            reasons.append("duplicate_actual_date")
        try:
            opened, closed = _session(pred_date, actual_date)
        except (ValueError, TypeError, KeyError):
            reasons.append("not_next_trading_session")
            opened = closed = None
        if closed is not None and current < closed:
            reasons.append("actual_session_not_complete")
        if opened is not None:
            evidence = _forecast_evidence(cdir / f"rnn_pred_{pred_date}.parquet",
                                          version, trained_through, pred_date, opened)
            rec["forecast_evidence"] = evidence
            if not evidence["ok"]:
                reasons.append(evidence["reason"])
        if reasons:
            report["excluded_rows"].append(rec)
            continue
        if _text(row.get("candidate_trained_through")) != trained_through:
            reasons.append("candidate_trained_through_mismatch")
        incumbent = _text(row.get("incumbent_version"))
        incumbent_versions = set(incumbent.split(";")) - {""}
        rec["incumbent_version"] = incumbent
        if not incumbent_versions or incumbent_versions & {"unknown", "nan", "None"}:
            reasons.append("incumbent_version_missing")
        elif version in incumbent_versions:
            reasons.append("incumbent_is_candidate")
        elif expected_incumbent_version and expected_incumbent_version not in incumbent_versions:
            reasons.append("incumbent_version_mismatch")
        if _text(row.get("held_sample_scope")) != "common":
            reasons.append("common_held_sample_unverified")
        for col in METRICS:
            try:
                value = float(row.get(col))
                if not math.isfinite(value) or value < 0:
                    raise ValueError("metric must be finite and nonnegative")
                rec[col] = value
            except (ValueError, TypeError):
                reasons.append(f"invalid_metric:{col}")
        coverage = None
        try:
            n_held = _positive_count(row.get("n_held"))
            n_common = _positive_count(row.get("n_held_common"))
            n_expected = _positive_count(row.get("n_held_expected"))
            if n_held != n_common or n_common > n_expected:
                raise ValueError("held counts must be a valid common intersection")
            coverage = n_common / n_expected
            rec.update(n_held=n_common, n_held_expected=n_expected,
                       coverage_ratio=coverage)
            if n_common < min_held:
                reasons.append("insufficient_held_sample")
            if coverage < min_coverage_ratio:
                reasons.append("held_coverage_below_minimum")
            if previous_coverage and coverage < previous_coverage * (1 - max_coverage_drop) - 1e-12:
                reasons.append("held_coverage_drop")
        except (ValueError, TypeError):
            reasons.append("invalid_held_counts")
        if reasons:
            report["excluded_rows"].append(rec)
            continue
        previous_coverage = coverage
        rec.pop("reasons")
        rec["both_metrics_no_worse_than_incumbent"] = bool(
            rec["rnn_held_mape"] <= rec["prod_held_mape"] and
            rec["rnn_held_log_mse"] <= rec["prod_held_log_mse"])
        report["accepted_days"].append(rec)

    # An evaluation that failed before it wrote its CSV row must not disappear
    # from acceptance. Inspect already completed saved forward forecasts only;
    # historical replay and still-pending target sessions are not missing scores.
    tracked_pred_dates = {_text(r.get("pred_date")) for r in rows
                          if _text(r.get("candidate_version")) == version}
    for forecast in sorted(cdir.glob("rnn_pred_*.parquet")):
        pred_date = forecast.stem.removeprefix("rnn_pred_")
        if pred_date in tracked_pred_dates:
            continue
        try:
            actual_date = _next_session(_day(pred_date))
            if actual_date <= trained_through:
                continue
            opened, closed = _session(pred_date, actual_date)
            if current < closed:
                continue
            evidence = _forecast_evidence(forecast, version, trained_through, pred_date, opened)
            if evidence.get("reason") == "not_forward":
                continue
            report["excluded_rows"].append({
                "csv_row": None, "pred_date": pred_date, "actual_date": actual_date,
                "reasons": ["missing_tracking_row"] if evidence["ok"] else [evidence["reason"]],
                "forecast_evidence": evidence,
            })
        except (ValueError, TypeError, KeyError):
            report["excluded_rows"].append({
                "csv_row": None, "pred_date": pred_date, "actual_date": None,
                "reasons": ["invalid_untracked_forecast_date"], "forecast_path": str(forecast),
            })

    accepted = report["accepted_days"]
    n_days = len(accepted)
    if not expected_incumbent_version and accepted:
        common_versions = set.intersection(*[set(r["incumbent_version"].split(";"))
                                              for r in accepted])
        # Do not mistake a MA5 fallback label in a blended production artifact
        # for the incumbent RNN pointer. Explicit registration is preferred;
        # legacy rows may infer only a single common rnn_* version.
        rnn_versions = {v for v in common_versions if v.startswith("rnn_")}
        if len(rnn_versions) == 1:
            report["incumbent_version"] = rnn_versions.pop()
    blockers = [r["csv_row"] for r in report["excluded_rows"]
                if not set(r["reasons"]).issubset(NONBLOCKING_EXCLUSIONS)]
    required_wins = math.ceil(daily_win_fraction * n_days)
    wins = sum(r["both_metrics_no_worse_than_incumbent"] for r in accepted)
    summary = {"n_forward_days": n_days, "days_remaining": max(0, min_days - n_days),
               "n_held_stock_days": sum(r["n_held"] for r in accepted),
               "daily_both_metrics_wins": wins, "required_daily_wins": required_wins,
               "blocking_excluded_csv_rows": blockers,
               "incumbent_versions": sorted({r["incumbent_version"] for r in accepted})}
    gates = {"enough_forward_days": n_days >= min_days,
             "no_invalid_forward_evidence": not blockers,
             "incumbent_rnn_identified": bool(report["incumbent_version"])}
    if accepted:
        weights = [r["n_held"] for r in accepted]
        means = {col: float(np.average([r[col] for r in accepted], weights=weights))
                 for col in METRICS}
        summary.update(weighted_metrics=means,
                       held_min=min(weights), held_max=max(weights),
                       coverage_min=min(r["coverage_ratio"] for r in accepted),
                       coverage_mean=float(np.mean([r["coverage_ratio"] for r in accepted])),
                       first_actual_date=accepted[0]["actual_date"],
                       last_actual_date=accepted[-1]["actual_date"])
        for metric in ("mape", "log_mse"):
            gates[f"{metric}_no_worse_than_incumbent"] = bool(
                means[f"rnn_held_{metric}"] <= means[f"prod_held_{metric}"])
            gates[f"{metric}_beats_ma5"] = bool(
                means[f"rnn_held_{metric}"] < means[f"ma5_held_{metric}"])
        gates["daily_consistency"] = wins >= required_wins
    report["summary"], report["gates"] = summary, gates
    if n_days < min_days:
        report["reasons"].append("insufficient_forward_days")
    else:
        report["status"] = "ready" if all(gates.values()) else "failed"
    report["reasons"].extend(name for name, passed in gates.items()
                             if not passed and name != "enough_forward_days")
    if report["status"] == "ready":
        report["reasons"] = ["acceptance_gates_passed_separate_production_review_required"]
    return report


def write_report(report: dict, path: str | Path) -> Path:
    """Atomically publish only the review artifact, preserving strict JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--trained-through")
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--expected-incumbent-version")
    parser.add_argument("--min-days", type=int, default=5)
    parser.add_argument("--min-held", type=int, default=30)
    parser.add_argument("--min-coverage-ratio", type=float, default=0.8)
    parser.add_argument("--max-coverage-drop", type=float, default=0.2)
    parser.add_argument("--daily-win-fraction", type=float, default=0.6)
    args = parser.parse_args(argv)
    try:
        report = review_candidate(**vars(args))
        path = Path(report["tracking_csv"]).parent / "review.json"
        write_report(report, path)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"Candidate review could not complete: {exc}\n")
    print(json.dumps({"status": report["status"], "report": str(path),
                      "reasons": report["reasons"], "promotion_performed": False},
                     ensure_ascii=False))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
