"""Compare a calendar-retrained LGBM with saved diagnostics on common stocks.

This is a retrospective diagnostic, never a forward acceptance or promotion job.
The current LGBM/MA5/actual arms come from an existing after-fix diagnostic;
only the new, stateless LGBM is served. RNN forecasts are read without rolling.
No registry, production forecast, adapter, or strategy file is written.

Example (the coordinator schedules this; do not run alongside training):
  python -m VolumePrediction.lgbm_candidate_diagnostic \
    --candidate-art outputs/registry/artifacts/NEW_VERSION \
    --out-dir /tmp/vp_tests/lgbm_candidate_diagnostic/run

Relative artifact paths resolve from the current directory. Default data paths
resolve from this repository. Output must be a new directory under VP diagnostics
or /tmp/vp_tests. All metrics retain float precision; MAPE is in percent.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
OUT = PKG / "outputs"
REFERENCE = OUT / "diagnostics/lgbm_metrics_20260917"
CURRENT_VERSION = "lgbm_prod_20260904"
RNN_VERSION = "rnn_prod_20260904"
ARMS = ("candidate", "current", "ma5", "rnn")
ET = ZoneInfo("America/New_York")


def _now() -> str:
    return datetime.now(ET).isoformat()


def _json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False,
                              allow_nan=False, default=str))
    tmp.replace(path)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Sources:
    """Hash each input once; fail if any source changes during this run."""

    def __init__(self):
        self.files: dict[str, dict] = {}

    def add(self, path) -> dict:
        path = Path(path).resolve()
        key = str(path)
        st = path.stat()
        if key in self.files:
            old = self.files[key]
            if (st.st_size, st.st_mtime_ns) != (old["size"], old["mtime_ns"]):
                raise ValueError(f"Source changed during evaluation: {path}")
            return old
        before = (st.st_size, st.st_mtime_ns)
        digest = _sha(path)
        st = path.stat()
        if before != (st.st_size, st.st_mtime_ns):
            raise ValueError(f"Source changed while hashing: {path}")
        value = {"path": key, "sha256": digest, "size": st.st_size,
                 "mtime_ns": st.st_mtime_ns,
                 "file_modified_at": datetime.fromtimestamp(st.st_mtime, ET).isoformat()}
        self.files[key] = value
        return value

    def check(self) -> None:
        for path in list(self.files):
            self.add(path)


def _ticker_series(series: pd.Series, name: str) -> pd.Series:
    if isinstance(series.index, pd.MultiIndex) or series.index.has_duplicates \
            or series.index.isna().any():
        raise ValueError(f"{name}: ticker keys must be unique and present")
    if any(not isinstance(t, str) or not t for t in series.index):
        raise ValueError(f"{name}: ticker keys must be nonempty strings")
    return pd.to_numeric(series, errors="raise").rename(name)


def validate_forecast(frame, *, version, trained_through, asof, target,
                      market_open):
    """Validate immutable identity/time evidence; jointly filter values later."""
    required = {"ticker", "date", "pred_V", "model_version",
                "trained_through", "generated_at"}
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError("Forecast is empty or lacks version/date/time fields")
    for col, expected in (("model_version", version), ("trained_through", trained_through),
                          ("date", asof)):
        values = frame[col].astype(str)
        if col != "model_version":
            values = values.str[:10]
        if not values.eq(expected).all():
            raise ValueError(f"Forecast {col} does not match {expected}")
    if pd.Timestamp(target) <= pd.Timestamp(asof):
        raise ValueError("Target must follow asof")
    stamps = []
    for value in frame.generated_at.unique():
        t = pd.Timestamp(value)
        if pd.isna(t):
            raise ValueError("Forecast generated_at is missing or invalid")
        stamps.append(t.tz_localize(ET) if t.tzinfo is None else t.tz_convert(ET))
    opening = pd.Timestamp(market_open)
    if opening.tzinfo is None:
        opening = opening.tz_localize(ET)
    else:
        opening = opening.tz_convert(ET)
    first, last = min(stamps), max(stamps)
    forward = last < opening and pd.Timestamp(trained_through) < pd.Timestamp(target)
    evidence = {"model_version": version, "trained_through": trained_through,
                "asof": asof, "target": target, "generated_at_min": first.isoformat(),
                "generated_at_max": last.isoformat(), "market_open": opening.isoformat(),
                "naive_timestamp_timezone": "America/New_York",
                "forecast_kind": "forward" if forward else "replay",
                "rows": len(frame)}
    return _ticker_series(frame.set_index("ticker").pred_V, version), evidence


def cohorts(reference, candidate, rnn, held):
    """Same-day, same-ticker, one finite/positive mask shared by all four arms."""
    ref = pd.concat([_ticker_series(reference[c], name) for c, name in
                     (("actual", "actual"), ("lgbm", "current"), ("ma5", "ma5"))], axis=1)
    valid_reference = np.isfinite(ref).all(axis=1) & ref.gt(0).all(axis=1)
    expected = ref.index[valid_reference & ref.index.isin(held)]
    joined = pd.concat([ref, _ticker_series(candidate, "candidate"),
                        _ticker_series(rnn, "rnn")], axis=1, join="inner").sort_index()
    valid = np.isfinite(joined).all(axis=1) & joined.gt(0).all(axis=1)
    common = joined.loc[valid].copy()
    common["is_held"] = common.index.isin(held)
    nheld = int(common.is_held.sum())
    return common, {"n_reference_valid": int(valid_reference.sum()),
                    "n_held_expected": len(expected), "n_common": len(common),
                    "n_held_common": nheld,
                    "held_coverage": nheld / len(expected) if len(expected) else 0.0,
                    "n_intersection_before_filter": len(joined),
                    "n_invalid_common": int((~valid).sum()),
                    "n_candidate_source": len(candidate), "n_rnn_source": len(rnn)}


def evaluate_cohort(frame):
    rows = []
    for scope, sub in (("all_common", frame),
                       ("held_common", frame.loc[frame.is_held])):
        row = {"scope": scope, "n": len(sub)}
        actual = sub.actual.to_numpy(float)
        for arm in ARMS:
            pred = sub[arm].to_numpy(float)
            row[arm + "_mape"] = float(np.abs(pred / actual - 1).mean() * 100) if len(sub) else None
            row[arm + "_log_mse"] = float(((np.log(pred) - np.log(actual)) ** 2).mean()) if len(sub) else None
        rows.append(row)
    return rows


def summarize(rows, *, min_held=30, min_coverage=.8):
    """Predeclared held-stock-day gates; RNN is a reference, never a gate."""
    if min_held < 1 or not 0 < min_coverage <= 1:
        raise ValueError("Invalid cohort gate thresholds")
    pooled = {}
    for scope in ("all_common", "held_common"):
        rr = [r for r in rows if r["scope"] == scope]
        n = sum(r["n"] for r in rr)
        result = {"n": n, "days": len(rr)}
        for arm in ARMS:
            for metric in ("mape", "log_mse"):
                key = arm + "_" + metric
                result[key] = sum(r[key] * r["n"] for r in rr if r["n"]) / n if n else None
        pooled[scope] = result
    held = [r for r in rows if r["scope"] == "held_common"]
    targets = [r["target"] for r in held]
    all_targets = [r["target"] for r in rows if r["scope"] == "all_common"]
    gates = {"dates_complete_and_unique": bool(held) and len(set(targets)) == len(targets)
             and sorted(all_targets) == sorted(targets),
             "every_day_min_held": bool(held) and all(r["n"] >= min_held for r in held),
             "every_day_min_coverage": bool(held) and all(r.get("held_coverage", 0) >= min_coverage for r in held)}
    p = pooled["held_common"]
    for metric in ("mape", "log_mse"):
        gates[metric + "_no_worse_than_current"] = bool(p["n"] and p["candidate_" + metric] <= p["current_" + metric])
        gates[metric + "_beats_ma5"] = bool(p["n"] and p["candidate_" + metric] < p["ma5_" + metric])
    return {"status": "diagnostic_pass" if all(gates.values()) else "diagnostic_fail",
            "pooled": pooled, "gates": gates,
            "failed_gates": [name for name, ok in gates.items() if not ok]}


def _sessions(start, end):
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=pd.Timestamp(start) - pd.Timedelta(days=15), end_date=end)
    dates = schedule.index.strftime("%Y-%m-%d").tolist()
    return [(dates[i - 1], day, schedule.iloc[i].market_open)
            for i, day in enumerate(dates) if i and start <= day <= end]


def _artifact(path, sources):
    path = Path(path).resolve()
    sources.add(path / "meta.json")
    meta = json.loads((path / "meta.json").read_text())
    if meta.get("kind") != "learned.lgbm" or meta.get("version") != path.name:
        raise ValueError(f"Invalid LGBM artifact identity: {path}")
    for name in ("model.pkl", "per_ticker.parquet"):
        sources.add(path / name)
    return meta


def _held(asof, advice_dir, sources):
    held, evidence = set(), []
    for stem in ("pairs_mrpt_advice", "pairs_mtfs_advice", "aiss_advice",
                 "aeus_advice", "ssrs_advice"):
        paths = sorted(p for p in Path(advice_dir).glob(f"{stem}_*.json")
                       if p.stem.split("_")[-1] <= asof)
        if not paths:
            raise ValueError(f"No advice at/before {asof} for {stem}")
        p = paths[-1]
        evidence.append(sources.add(p))
        d = json.loads(p.read_text())
        for h in d.get("holdings") or []:
            held.add(h.get("ticker"))
        for row in d.get("positions") or []:
            held.update((row.get("s1"), row.get("s2")))
        for e in d.get("etfs") or []:
            if (e.get("shares") or 0) > 0:
                held.add(e.get("etf") or e.get("ticker"))
    held.discard(None)
    held.discard("")
    return held, evidence


def _reference_frame(root, report, target, asof, sources):
    path = root / "after_fix" / f"diagnostic_{target}.parquet"
    evidence = sources.add(path)
    frame = pd.read_parquet(path)
    if "ticker" in frame.columns:
        frame = frame.set_index("ticker")
    for c in ("actual", "lgbm", "ma5"):
        _ticker_series(frame[c], c)
    expected = [r for r in report["rows"] if r["target"] == target and r["scope"] == "all"]
    if len(expected) != 1 or expected[0]["asof"] != asof or expected[0]["n"] != len(frame):
        raise ValueError(f"Reference report/date/count mismatch for {target}")
    if not (np.isfinite(frame[["actual", "lgbm", "ma5"]]).all().all()
            and frame[["actual", "lgbm", "ma5"]].gt(0).all().all()):
        raise ValueError("Saved reference should already contain valid common observations")
    for arm in ("lgbm", "ma5"):
        calculated = {"mape": float(np.abs(frame[arm] / frame.actual - 1).mean() * 100),
                      "log_mse": float(((np.log(frame[arm]) - np.log(frame.actual)) ** 2).mean())}
        for metric, value in calculated.items():
            if not np.isclose(value, expected[0][arm + "_" + metric], rtol=1e-11, atol=1e-11):
                raise ValueError(f"Reference after-fix metrics do not match {target}/{arm}/{metric}")
    return frame, {**evidence, "model_version": report["model_version"],
                   "trained_through": report["trained_through"],
                   "generated_at": None, "forecast_kind": "replay",
                   "timestamp_limit": "Legacy diagnostic has no prediction timestamp; file mtime is not forecast evidence",
                   "asof": asof, "target": target}


class CandidateServer:
    """One read-only artifact load and calendar snapshot; only new arm is served.

    Patches are scoped to this standalone single-threaded diagnostic and restored
    on exit. Historical earnings/splits are cache-only; the optional Mongo read
    obtains one future-calendar snapshot, saved in the diagnostic directory.
    """

    def __init__(self, art, out_dir, sources, future_calendar=None):
        self.art, self.out, self.sources = art, out_dir, sources
        self.future_calendar = future_calendar
        self.stack = ExitStack()

    def __enter__(self):
        from VolumePrediction import prod_model as pm
        from VolumePrediction.data import earnings_loader as el, splits_loader as sl
        self.pm = pm
        loaded = pm._load_artifact(self.art)
        model, per_ticker, meta = loaded
        if list(model._cols) != list(meta["feature_cols"]):
            raise ValueError("Candidate model feature order differs from metadata")
        self.sources.add(el.CACHE)
        self.sources.add(sl.CACHE)
        earnings = json.loads(el.CACHE.read_text())
        split_cache = json.loads(sl.CACHE.read_text())
        by_ticker = {}
        for row in split_cache["results"]:
            by_ticker.setdefault(row.get("ticker"), []).append(row)
        if self.future_calendar:
            self.sources.add(self.future_calendar)
            payload = json.loads(Path(self.future_calendar).read_text())
            future = payload.get("dates", payload)
        else:
            future = el.future_dates(per_ticker.index.tolist())
        future = {s: sorted({str(pd.Timestamp(d).date()) for d in dates})
                  for s, dates in future.items()}
        snapshot = self.out / "future_calendar_snapshot.json"
        _json(snapshot, {"captured_at": _now(), "historical_PIT": False, "dates": future})
        self.sources.add(snapshot)
        parsed = {s: [pd.Timestamp(d).date() for d in days] for s, days in future.items()}
        try:
            self.stack.enter_context(patch.object(el, "fetch_symbols", lambda symbols, refresh_days=3: earnings))
            self.stack.enter_context(patch.object(el, "future_dates", lambda symbols, horizon_days=400: {s: parsed.get(s, []) for s in symbols}))
            self.stack.enter_context(patch.object(sl, "splits_for", lambda ticker, since="2019-01-01": by_ticker.get(ticker, [])))
            self.stack.enter_context(patch.object(pm, "_load_artifact", lambda path: loaded if Path(path).resolve() == self.art else (_ for _ in ()).throw(ValueError("Unexpected artifact requested"))))
            original_read = pd.read_parquet

            def read_with_evidence(path, *args, **kwargs):
                if isinstance(path, (str, Path)):
                    self.sources.add(path)
                return original_read(path, *args, **kwargs)

            self.stack.enter_context(patch.object(pd, "read_parquet", read_with_evidence))
        except BaseException:
            self.stack.close()
            raise
        return self

    def serve(self, target):
        return self.pm.serve(self.art, target)

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def run(*, candidate_art, out_dir, start="2026-09-08", end="2026-09-17",
        reference_dir=REFERENCE, current_art=None, rnn_dir=None, rnn_version=RNN_VERSION,
        rnn_trained_through="2026-09-04", advice_dir=None, future_calendar=None,
        min_held=30, min_coverage=.8, server_factory=CandidateServer):
    candidate_art = Path(candidate_art).resolve()
    out_dir = Path(out_dir).resolve()
    allowed = [OUT / "diagnostics", Path("/tmp/vp_tests").resolve()]
    if not any(base.resolve() in out_dir.parents for base in allowed):
        raise ValueError("Output must be an isolated diagnostics or /tmp/vp_tests subdirectory")
    if out_dir.exists():
        raise FileExistsError("Use a new output directory; never overwrite diagnostic evidence")
    sources = Sources()
    candidate_meta = _artifact(candidate_art, sources)
    current_art = Path(current_art or OUT / "registry/artifacts" / CURRENT_VERSION).resolve()
    current_meta = _artifact(current_art, sources)
    if candidate_meta["version"] == current_meta["version"]:
        raise ValueError("Candidate must have a distinct version")
    if candidate_meta["feature_cols"] != current_meta["feature_cols"]:
        raise ValueError("Candidate and current feature schemas differ")
    reference_dir = Path(reference_dir).resolve()
    sources.add(reference_dir / "after_fix.json")
    report = json.loads((reference_dir / "after_fix.json").read_text())
    if report["model_version"] != current_meta["version"] or report["trained_through"] != current_meta["trained_through"] \
            or report.get("evaluation_kind") != "retrospective_diagnostic_not_prospective_acceptance":
        raise ValueError("After-fix diagnostic identity/kind does not match current LGBM")
    for path in [Path(__file__), PKG / "prod_model.py", PKG / "features/pipeline.py",
                 PKG / "data/earnings_loader.py", PKG / "data/splits_loader.py",
                 reference_dir / "vp_lgbm_diagnostic_20260917_fixed.py"]:
        sources.add(path)
    alias_path = REPO / "ticker_aliases.json"
    if alias_path.exists():
        sources.add(alias_path)
    dates = _sessions(start, end)
    if not dates or any(target <= candidate_meta["trained_through"] for _, target, _ in dates):
        raise ValueError("Evaluation dates are empty or inside candidate training period")
    rnn_dir = Path(rnn_dir or OUT / "shadow_rnn/candidates" / rnn_version).resolve()
    advice_dir = Path(advice_dir or OUT / "adapters").resolve()
    out_dir.mkdir(parents=True)
    plan = {"schema_version": 1, "declared_at": _now(), "evaluation_kind": "retrospective_diagnostic_not_forward_acceptance",
            "candidate_version": candidate_meta["version"], "current_version": current_meta["version"],
            "rnn_reference_version": rnn_version, "start": start, "end": end,
            "targets": [t for _, t, _ in dates], "min_held": min_held, "min_coverage": min_coverage,
            "metric_gates": "Held stock-day weighted candidate MAPE/log-MSE <= current, both < MA5; RNN reference only",
            "cohort_rule": "Four forecast arms and actual on the identical positive finite ticker intersection",
            "coverage_denominator": "Held tickers valid in saved actual/current/MA5 before candidate or RNN intersection",
            "promotion_authorized": False,
            "limitation": "Current snapshots, replayed LGBM and legacy timestamp gaps; no forward acceptance claim"}
    _json(out_dir / "plan.json", plan)
    rows, evidence = [], []
    try:
        # Validate every cheap stored input before doing any candidate serve.
        stored = []
        for asof, target, opening in dates:
            ref, ref_ev = _reference_frame(reference_dir, report, target, asof, sources)
            rpath = rnn_dir / f"rnn_pred_{asof}.parquet"
            rfile = sources.add(rpath)
            rnn, rnn_ev = validate_forecast(pd.read_parquet(rpath), version=rnn_version,
                                           trained_through=rnn_trained_through, asof=asof,
                                           target=target, market_open=opening)
            held, advice_ev = _held(asof, advice_dir, sources)
            stored.append((asof, target, opening, ref, ref_ev, rnn, {**rnn_ev, "source": rfile}, held, advice_ev))
        with server_factory(candidate_art, out_dir, sources, future_calendar) as server:
            for asof, target, opening, ref, ref_ev, rnn, rnn_ev, held, advice_ev in stored:
                frame = server.serve(target)
                candidate, candidate_ev = validate_forecast(frame, version=candidate_meta["version"],
                    trained_through=candidate_meta["trained_through"], asof=asof, target=target, market_open=opening)
                candidate_ev["forecast_kind"] = "replay"
                candidate_ev["reason"] = "Generated by retrospective diagnostic; never eligible for forward acceptance"
                path = out_dir / "predictions" / f"candidate_{asof}.parquet"
                path.parent.mkdir(exist_ok=True)
                frame.to_parquet(path, index=False)
                candidate_ev["source"] = sources.add(path)
                common, counts = cohorts(ref, candidate, rnn, held)
                for row in evaluate_cohort(common):
                    rows.append({**row, "asof": asof, "target": target, **counts})
                cohort_path = out_dir / "cohorts" / f"common_{target}.parquet"
                cohort_path.parent.mkdir(exist_ok=True)
                common.index.name = "ticker"
                common.to_parquet(cohort_path)
                evidence.append({"asof": asof, "target": target, "candidate": candidate_ev,
                                 "current_reference": ref_ev, "rnn_reference": rnn_ev,
                                 "held_advice_sources": advice_ev, "counts": counts,
                                 "cohort_source": sources.add(cohort_path)})
                _json(out_dir / "progress.json", {"completed_targets": [e["target"] for e in evidence],
                                                   "updated_at": _now()})
                print(json.dumps({"target": target, "n_common": counts["n_common"],
                                  "n_held": counts["n_held_common"]}), flush=True)
        sources.check()
        result = {**plan, "completed_at": _now(), **summarize(rows, min_held=min_held, min_coverage=min_coverage),
                  "rows": rows, "evidence": evidence, "source_files": list(sources.files.values()),
                  "rnn_forward_targets": [e["target"] for e in evidence if e["rnn_reference"]["forecast_kind"] == "forward"],
                  "rnn_replay_targets": [e["target"] for e in evidence if e["rnn_reference"]["forecast_kind"] != "forward"]}
        _json(out_dir / "result.json", result)
        pd.DataFrame(rows).to_csv(out_dir / "metrics.csv", index=False)
        return result
    except BaseException as exc:
        _json(out_dir / "failure.json", {"status": "diagnostic_error", "failed_at": _now(),
              "error_type": type(exc).__name__, "completed_targets": [e["target"] for e in evidence],
              "source_files": list(sources.files.values()), "promotion_authorized": False})
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-art", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--start", default="2026-09-08")
    ap.add_argument("--end", default="2026-09-17")
    ap.add_argument("--reference-dir", type=Path, default=REFERENCE)
    ap.add_argument("--current-art", type=Path)
    ap.add_argument("--rnn-dir", type=Path)
    ap.add_argument("--rnn-version", default=RNN_VERSION)
    ap.add_argument("--rnn-trained-through", default="2026-09-04")
    ap.add_argument("--advice-dir", type=Path)
    ap.add_argument("--future-calendar", type=Path, help="Optional saved future calendar JSON; otherwise one read-only Mongo query")
    ap.add_argument("--min-held", type=int, default=30)
    ap.add_argument("--min-coverage", type=float, default=.8)
    args = vars(ap.parse_args())
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    result = run(**args)
    print(json.dumps({"status": result["status"], "gates": result["gates"], "pooled": result["pooled"]}), flush=True)
    return 0 if result["status"] == "diagnostic_pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
