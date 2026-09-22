"""Derive a new training panel by rebuilding every calendar feature row.

Only the four calendar arrays are replaced. All other values, row order, Arrow
fields and pandas index metadata are verified against the immutable source.
This module neither trains models nor updates a registry or latest pointer.

Example::

    python -m VolumePrediction.rebuild_calendar_panel \
      --source VolumePrediction/outputs/panel/source.parquet \
      --output VolumePrediction/outputs/panel/source_calendarfix.parquet
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from VolumePrediction.common import REPO
from VolumePrediction.features import pipeline


CALENDAR_COLUMNS = (
    "cal_is_early_close", "cal_triple_witching",
    "cal_double_witching", "cal_russell_rebalance",
)
PROTOCOL = "calendar_panel_rebuild_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _allowed_destination(path: Path) -> bool:
    roots = (REPO / "VolumePrediction", REPO / "price_data" / "volume_prediction",
             Path("/tmp/vp_tests"))
    return any(path.is_relative_to(root.resolve()) for root in roots)


def _check_schema(schema: pa.Schema) -> None:
    required = {"date", "ticker", "eta", *CALENDAR_COLUMNS}
    missing = required.difference(schema.names)
    if missing:
        raise ValueError(f"source panel is missing columns: {sorted(missing)}")
    if len(schema.names) != len(set(schema.names)):
        raise ValueError("source panel has duplicate column names")
    dtype = schema.field("date").type
    if not pa.types.is_timestamp(dtype) or dtype.tz is not None:
        raise ValueError("date must be a timezone-naive Arrow timestamp")
    for name in CALENDAR_COLUMNS:
        if not (pa.types.is_floating(schema.field(name).type)
                or pa.types.is_integer(schema.field(name).type)):
            raise ValueError(f"calendar feature must be numeric: {name}")


def _flags_for_source(source: pq.ParquetFile, batch_size: int) -> pd.DataFrame:
    """Read only the date column; calculate the corrected flags once per date."""
    all_dates = set()
    for batch in source.iter_batches(batch_size=batch_size, columns=["date"]):
        dates = batch.column(0)
        if dates.null_count:
            raise ValueError("source panel contains null dates")
        all_dates.update(pd.DatetimeIndex(dates.to_numpy()).unique())
    if not all_dates:
        raise ValueError("source panel has no rows")
    dates = pd.DatetimeIndex(sorted(all_dates))
    if not dates.equals(dates.normalize()):
        raise ValueError("source panel dates must be normalized to midnight")
    index = pd.MultiIndex.from_product([dates, ["__calendar__"]],
                                      names=["date", "ticker"])
    return pipeline.add_calendar_flags(pd.DataFrame(index=index)).droplevel("ticker")


def _expected_flags(batch: pa.RecordBatch, flags: pd.DataFrame) -> pd.DataFrame:
    dates = pd.DatetimeIndex(batch.column(batch.schema.get_field_index("date")).to_numpy())
    result = flags.reindex(dates)
    if result.isna().any().any():
        raise ValueError("date is missing from rebuilt calendar")
    return result


def _exact_array_equal(left: pa.Array, right: pa.Array) -> bool:
    """Compare floating point bits too, preserving NaNs, signed zeros and nulls."""
    if left.type != right.type or len(left) != len(right):
        return False
    if not pa.types.is_floating(left.type):
        return left.equals(right)
    valid = left.is_valid().to_numpy(zero_copy_only=False)
    if not np.array_equal(valid, right.is_valid().to_numpy(zero_copy_only=False)):
        return False
    a = left.to_numpy(zero_copy_only=False)
    b = right.to_numpy(zero_copy_only=False)
    uint = np.dtype(f"u{a.dtype.itemsize}")
    return np.array_equal(a.view(uint)[valid], b.view(uint)[valid])


def _aligned_batches(source: pq.ParquetFile, output: pq.ParquetFile, batch_size: int):
    """Align stream boundaries even when the output has different row groups."""
    left_iter = iter(source.iter_batches(batch_size=batch_size))
    right_iter = iter(output.iter_batches(batch_size=batch_size))
    left = right = None
    while True:
        if left is None:
            left = next(left_iter, None)
        if right is None:
            right = next(right_iter, None)
        if left is None or right is None:
            if left is not None or right is not None:
                raise ValueError("output row stream length differs from source")
            break
        count = min(len(left), len(right))
        yield left.slice(0, count), right.slice(0, count)
        left = left.slice(count) if len(left) > count else None
        right = right.slice(count) if len(right) > count else None


def _verify_output(source: pq.ParquetFile, output: Path,
                   flags: pd.DataFrame, batch_size: int) -> dict:
    rebuilt = pq.ParquetFile(output)
    if not source.schema_arrow.equals(rebuilt.schema_arrow, check_metadata=True):
        raise ValueError("output Arrow schema or index metadata differs from source")
    if source.metadata.num_rows != rebuilt.metadata.num_rows:
        raise ValueError("output row count differs from source")
    checked = 0
    for old, new in _aligned_batches(source, rebuilt, batch_size):
        expected = _expected_flags(new, flags)
        for i, field in enumerate(source.schema_arrow):
            if field.name in CALENDAR_COLUMNS:
                reference = pa.array(expected[field.name].to_numpy(), type=field.type)
            else:
                reference = old.column(i)
            if not _exact_array_equal(reference, new.column(i)):
                raise ValueError(f"output verification failed for {field.name} near row {checked}")
        checked += len(old)
    return {"all_rows_checked": checked, "schema_and_index_metadata_equal": True,
            "non_calendar_columns_bitwise_equal": True,
            "calendar_columns_match_recomputed_flags": True,
            "output_row_groups": rebuilt.num_row_groups}


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def rebuild(source, output, *, batch_size: int = 65536, dry_run: bool = False) -> dict:
    """Publish a new parquet and a completion provenance file without overwriting.

    The parquet is installed atomically only after a complete reread validation.
    The provenance JSON is the completion marker and is published last. A process
    crash between those two publications can leave an orphan parquet; it is never
    accepted as complete or overwritten on retry. Existing destinations are errors.
    Dry runs inspect the footer only and create no directories, files, or pointers.
    """
    source = Path(source).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve()
    provenance_path = output.with_suffix(output.suffix + ".provenance.json")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if source == output:
        raise ValueError("output must differ from immutable source")
    if output.suffix != ".parquet" or not _allowed_destination(output):
        raise ValueError("output must be a .parquet under VolumePrediction/, its raw-data tree, or /tmp/vp_tests/")
    if output.exists() or provenance_path.exists():
        raise FileExistsError(f"refusing to overwrite existing output or provenance: {output}")
    original = pq.ParquetFile(source)
    schema = original.schema_arrow
    _check_schema(schema)
    plan = {"protocol": PROTOCOL, "status": "dry_run" if dry_run else "complete",
            "source": str(source), "output": str(output),
            "provenance": str(provenance_path), "batch_size": batch_size,
            "source_rows": original.metadata.num_rows,
            "source_row_groups": original.num_row_groups,
            "rebuilt_columns": list(CALENDAR_COLUMNS)}
    if dry_run:
        return plan

    source_hash = _sha256(source)
    flags = _flags_for_source(original, batch_size)
    affected = total_eta = processed = source_eta_non_null = 0
    per_column = {name: 0 for name in CALENDAR_COLUMNS}
    eta_per_column = {name: 0 for name in CALENDAR_COLUMNS}
    per_date = defaultdict(lambda: {"changed_rows": 0, "eta_non_null_affected_rows": 0,
                                   "by_column": {name: 0 for name in CALENDAR_COLUMNS}})
    compression = "snappy"
    if original.num_row_groups:
        codec = original.metadata.row_group(0).column(0).compression
        compression = None if codec == "UNCOMPRESSED" else codec.lower()
    output.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=f".{output.stem}.staging-", dir=output.parent) as staging:
        staged = Path(staging) / output.name
        staged_provenance = Path(staging) / provenance_path.name
        try:
            with pq.ParquetWriter(staged, schema, compression=compression, version="2.6") as writer:
                for row_group in range(original.num_row_groups):
                    for batch in original.iter_batches(batch_size=batch_size, row_groups=[row_group]):
                        corrected = _expected_flags(batch, flags)
                        date_keys = corrected.index.strftime("%Y-%m-%d").to_numpy()
                        eta = batch.column(schema.get_field_index("eta")).to_pandas()
                        eta_valid = np.asarray(pd.notna(eta))
                        source_eta_non_null += int(eta_valid.sum())
                        any_changed = np.zeros(len(batch), dtype=bool)
                        table = pa.Table.from_batches([batch], schema=schema)
                        for name in CALENDAR_COLUMNS:
                            index = schema.get_field_index(name)
                            replacement = corrected[name].to_numpy()
                            previous = batch.column(index).to_pandas()
                            changed = np.asarray(pd.isna(previous) | (previous != replacement))
                            any_changed |= changed
                            per_column[name] += int(changed.sum())
                            eta_per_column[name] += int((changed & eta_valid).sum())
                            days, counts = np.unique(date_keys[changed], return_counts=True)
                            for day, count in zip(days, counts):
                                per_date[day]["by_column"][name] += int(count)
                            table = table.set_column(index, schema.field(index),
                                                     pa.array(replacement, type=schema.field(index).type))
                        affected += int(any_changed.sum())
                        total_eta += int((any_changed & eta_valid).sum())
                        for mask, key in ((any_changed, "changed_rows"),
                                          (any_changed & eta_valid, "eta_non_null_affected_rows")):
                            days, counts = np.unique(date_keys[mask], return_counts=True)
                            for day, count in zip(days, counts):
                                per_date[day][key] += int(count)
                        writer.write_table(table, row_group_size=len(table))
                        processed += len(table)
            if processed != original.metadata.num_rows:
                raise ValueError("not all source rows were processed")
            verification = _verify_output(original, staged, flags, batch_size)
            if _sha256(source) != source_hash:
                raise RuntimeError("source changed during rebuild; refusing to publish")
            result = {**plan, "artifact_kind": "derived_training_panel",
                      "model_version": None, "trained_through": None,
                      "generated_at": datetime.now(timezone.utc).isoformat(),
                      "panel_start": str(flags.index.min().date()),
                      "panel_end": str(flags.index.max().date()),
                      "source_sha256": source_hash, "output_sha256": _sha256(staged),
                      "source_bytes": source.stat().st_size, "output_bytes": staged.stat().st_size,
                      "schema_sha256": hashlib.sha256(schema.serialize().to_pybytes()).hexdigest(),
                      "code_sha256": {"rebuild_calendar_panel.py": _sha256(Path(__file__)),
                                      "features/pipeline.py": _sha256(Path(pipeline.__file__))},
                      "changed_rows": affected, "changed_rows_by_column": per_column,
                      "changed_rows_by_date": dict(sorted(per_date.items())),
                      "source_eta_non_null_rows": source_eta_non_null,
                      "eta_non_null_affected_rows": total_eta,
                      "eta_non_null_affected_rows_by_column": eta_per_column,
                      "verification": verification}
            staged_provenance.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            _fsync_file(staged)
            _fsync_file(staged_provenance)
            # Same-filesystem hard links atomically publish complete files and
            # fail if another writer has claimed either destination meanwhile.
            os.link(staged, output)
            published.append(output)
            os.link(staged_provenance, provenance_path)
            published.append(provenance_path)
            directory_fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return result
        except BaseException:
            for path in reversed(published):
                path.unlink(missing_ok=True)
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-size", type=int, default=65536)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = rebuild(args.source, args.output, batch_size=args.batch_size, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
